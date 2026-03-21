#!/usr/bin/env python3
"""
PolyLens pipeline:
  Polymarket (signal) → Tavily search (news + snippets) → Gemini (insight) → static HTML
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
from jinja2 import Environment, FileSystemLoader
from tavily import TavilyClient

load_dotenv()

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TAVILY_API_KEY = os.environ["TAVILY_API_KEY"]
POLYMARKET_BASE = "https://gamma-api.polymarket.com"
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

GEMINI_MODELS = ["models/gemini-2.5-flash", "models/gemini-2.0-flash", "models/gemini-2.0-flash-lite"]
TOP_N = 10
MIN_VOLUME = 5_000
MIN_CHANGE = 0.02
RATE_LIMIT_SLEEP = 2
FETCH_LIMIT = 500
NEWS_PER_TOPIC = 6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

client = genai.Client(api_key=GEMINI_API_KEY)
tavily = TavilyClient(api_key=TAVILY_API_KEY)


# ---------------------------------------------------------------------------
# 1. Market Ingestion
# ---------------------------------------------------------------------------

def fetch_markets(limit: int = FETCH_LIMIT) -> list[dict]:
    """Fetch active, non-closed markets from Polymarket Gamma API."""
    try:
        resp = requests.get(
            f"{POLYMARKET_BASE}/markets",
            params={"limit": limit, "active": "true", "closed": "false"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("data", data.get("markets", []))
    except Exception as exc:
        log.error("Failed to fetch markets: %s", exc)
        return []


def parse_yes_probability(prices_raw) -> float:
    """Parse outcomePrices → YES probability (index 0)."""
    try:
        if isinstance(prices_raw, str):
            prices = json.loads(prices_raw)
        else:
            prices = prices_raw or []
        return round(float(prices[0]), 4)
    except Exception:
        return 0.5


def rank_markets(raw: list[dict], top_n: int = TOP_N) -> list[dict]:
    """Filter by volume/change thresholds, rank by volume × |Δ|, take top N."""
    candidates = []
    for m in raw:
        volume = float(m.get("volume24hr") or 0)
        change = float(m.get("oneDayPriceChange") or 0)

        if volume < MIN_VOLUME:
            continue
        if abs(change) < MIN_CHANGE:
            continue

        candidates.append(
            {
                "id": m.get("id", ""),
                "question": m.get("question", ""),
                "probability": parse_yes_probability(m.get("outcomePrices")),
                "change_24h": round(change, 4),
                "volume_24h": round(volume, 2),
                "score": round(volume * abs(change), 2),
                "url": f"https://polymarket.com/event/{m.get('slug', m.get('id', ''))}",
            }
        )

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_n]


# ---------------------------------------------------------------------------
# 2. News Aggregation (Tavily Search)
# ---------------------------------------------------------------------------

def fetch_news(query: str, n: int = NEWS_PER_TOPIC) -> list[dict]:
    """
    Search recent news via Tavily. Returns list of {title, url, snippet}.
    Tavily's 'news' topic focuses on articles from the last few days.
    """
    try:
        resp = tavily.search(
            query=query,
            topic="news",
            days=3,
            max_results=n,
            include_answer=False,
        )
        results = []
        for r in resp.get("results", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", "")[:300],
            })
        return results
    except Exception as exc:
        log.warning("Tavily fetch failed for '%s': %s", query[:40], exc)
        return []


# ---------------------------------------------------------------------------
# 3. Insight Generation (Gemini, no grounding required)
# ---------------------------------------------------------------------------

_JSON_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _extract_json(text: str) -> str:
    """Strip markdown code fences and return raw JSON string."""
    m = _JSON_BLOCK.search(text)
    if m:
        return m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start : end + 1]
    return text.strip()


def _call_gemini(prompt: str) -> str:
    """Try each model in order; retry once on 429 with backoff."""
    last_exc = None
    for model in GEMINI_MODELS:
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(temperature=0.3),
                )
                log.debug("Used model %s", model)
                return response.text
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    m_delay = re.search(r"retry[^0-9]*([0-9]+(?:\.[0-9]+)?)\s*s", msg, re.IGNORECASE)
                    wait = min(float(m_delay.group(1)) + 1 if m_delay else 10 * (attempt + 1), 15)
                    log.warning("429 on %s attempt %d, waiting %.0fs...", model, attempt + 1, wait)
                    time.sleep(wait)
                else:
                    log.debug("Non-quota error on %s: %s", model, msg[:80])
                    break
    raise last_exc


def generate_insight(market: dict, news: list[dict]) -> dict:
    """Generate structured insight from market data + Tavily news results."""
    question = market["question"]
    prob = market["probability"]
    change = market["change_24h"]
    direction = "risen" if change > 0 else "fallen"
    sign = "+" if change > 0 else ""

    if news:
        news_block = "\n".join(
            f"- {r['title']}: {r['snippet']}" for r in news
        )
    else:
        news_block = "No recent news found. Base your analysis on the market data and your knowledge."

    prompt = f"""You are a financial analyst specializing in prediction markets.

MARKET: {question}
CURRENT PROBABILITY: {prob:.0%}
24H CHANGE: {sign}{change:.1%} (probability has {direction})

RECENT HEADLINES:
{news_block}

TASK:
Analyze the prediction market data and headlines above.
Explain why the probability moved and what it means.

OUTPUT: Return ONLY a valid JSON object — no extra text, no markdown fences:
{{
  "title": "<8 words max, action-oriented>",
  "summary": "<2 sentences: what happened + why probability moved, cite specific events>",
  "drivers": [
    "<concrete driver with evidence, <=15 words>",
    "<concrete driver with evidence, <=15 words>",
    "<concrete driver with evidence, <=15 words>"
  ],
  "why_matters": "<1-2 sentences on broader significance>"
}}

RULES:
- Be specific. Name events, people, data points.
- Bad: "market sentiment improved". Good: "CPI fell to 2.4%, below 2.6% forecast".
- 2-4 drivers only.
- If headlines are sparse, still give your best analysis."""

    try:
        raw_text = _call_gemini(prompt)
        raw = _extract_json(raw_text)
        insight = json.loads(raw)
        for key in ("title", "summary", "drivers", "why_matters"):
            insight.setdefault(key, "")
        if not isinstance(insight["drivers"], list):
            insight["drivers"] = [str(insight["drivers"])]
        return insight
    except json.JSONDecodeError as exc:
        log.warning("JSON parse error for '%s': %s", question[:50], exc)
        return _fallback_insight(question, exc)
    except Exception as exc:
        log.error("Gemini error for '%s': %s", question[:50], exc)
        return _fallback_insight(question, exc)


def _fallback_insight(question: str, error: Exception) -> dict:
    return {
        "title": question[:70],
        "summary": "Insight generation encountered an error. Raw market data shown.",
        "drivers": [str(error)[:120]],
        "why_matters": "Please check API keys and retry.",
    }


# ---------------------------------------------------------------------------
# 4. Pipeline
# ---------------------------------------------------------------------------

def run_pipeline() -> dict:
    log.info("=== PolyLens pipeline starting ===")

    log.info("Fetching Polymarket data...")
    raw_markets = fetch_markets()
    log.info("Fetched %d markets total", len(raw_markets))

    top = rank_markets(raw_markets)
    log.info("Selected top %d markets after filtering", len(top))

    results = []
    for i, market in enumerate(top, 1):
        q = market["question"][:65]
        log.info("[%d/%d] %s (%.0f%% %+.1f%%)", i, len(top), q,
                 market["probability"] * 100, market["change_24h"] * 100)

        headlines = fetch_news(market["question"])
        log.info("  -> %d news results fetched", len(headlines))

        insight = generate_insight(market, headlines)
        results.append({
            "market": market,
            "news": headlines,
            "insight": insight,
        })
        if i < len(top):
            time.sleep(RATE_LIMIT_SLEEP)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_at_readable": datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC"),
        "topics": results,
    }

    json_path = OUTPUT_DIR / "data.json"
    json_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Saved %s", json_path)

    render_html(output)
    log.info("=== Pipeline complete. %d insights generated. ===", len(results))
    return output


# ---------------------------------------------------------------------------
# 5. HTML rendering
# ---------------------------------------------------------------------------

def render_html(data: dict) -> None:
    env = Environment(loader=FileSystemLoader(Path(__file__).parent))
    template = env.get_template("template.html")
    html = template.render(data=data)
    html_path = OUTPUT_DIR / "index.html"
    html_path.write_text(html, encoding="utf-8")
    log.info("Rendered HTML -> %s", html_path)


if __name__ == "__main__":
    run_pipeline()
