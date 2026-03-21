#!/usr/bin/env python3
"""
PolyLens pipeline:
  Polymarket (signal) → Tavily search (news + snippets) → Gemini/GPT (insight) → static HTML
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
from openai import OpenAI
from tavily import TavilyClient

load_dotenv()

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TAVILY_API_KEY = os.environ["TAVILY_API_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_AUDIENCE_ID = os.environ.get("RESEND_AUDIENCE_ID", "")
SITE_URL = os.environ.get("SITE_URL", "https://www.hika.fyi")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "PolyLens <newsletter@hika.fyi>")
POLYMARKET_BASE = "https://gamma-api.polymarket.com"
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

GEMINI_MODELS = ["models/gemini-2.5-flash", "models/gemini-2.0-flash", "models/gemini-2.0-flash-lite"]
OPENAI_MODEL = "gpt-4o-mini"
TOP_N = 20
MIN_VOLUME = 1_000    # lower bar — let scoring sort quality
MIN_CHANGE = 0.003    # 0.3% — prediction markets rarely move more than 1-2%/day
HIGH_VOL = 50_000     # markets above this are included regardless of change
RATE_LIMIT_SLEEP = 20
FETCH_LIMIT = 500
NEWS_PER_TOPIC = 5

# Category definitions (order matters for keyword matching)
CATEGORIES = ["politics", "ai_tech", "economy", "business", "world", "sports", "crypto"]

_CAT_KEYWORDS: dict[str, list[str]] = {
    "politics": ["election","president","senator","congress","parliament","prime minister",
                 "republican","democrat","vote","ballot","campaign","governor","nomination",
                 "white house","administration","impeach","cabinet","minister","chancellor"],
    "ai_tech":  ["artificial intelligence"," ai ","gpt","openai","gemini","claude","llm",
                 "machine learning","neural","robot","automation","tech ","software","apple",
                 "google","microsoft","meta ","nvidia","chip","semiconductor"],
    "economy":  ["fed ","federal reserve","rate cut","interest rate","inflation","gdp",
                 "recession","employment","jobs","treasury","yield","tariff","trade war",
                 "economic","economy","unemployment","cpi","pce"],
    "business": ["acquisition","merger","ipo","ceo","startup","valuation","revenue",
                 "earnings","stock","shares","billion","company","market cap","deal"],
    "world":    ["war","conflict","peace","ceasefire","diplomatic","treaty","sanction",
                 "nato","nuclear","missile","troops","invasion","ukraine","russia","israel",
                 "china","taiwan","iran","middle east","un "],
    "sports":   ["nba","nfl","fifa","world cup","championship","league","tournament",
                 "olympics","player","team","game","season","title","win the"],
    "crypto":   ["bitcoin","ethereum","crypto","blockchain","token","defi","btc","eth",
                 "solana","binance","nft","web3","stablecoin"],
}


def classify_category(question: str, llm_category: str | None = None) -> str:
    """Return a category slug. Use LLM hint first, fall back to keyword matching."""
    valid = set(CATEGORIES)
    if llm_category and llm_category in valid:
        return llm_category

    q = question.lower()
    for cat, keywords in _CAT_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return cat
    return "other"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
oai_client = OpenAI(api_key=OPENAI_API_KEY)
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
    """
    Filter by volume/change thresholds, rank by score, take top N.
    Score = volume24hr * (abs_change + 0.005)
    The +0.005 bonus ensures high-volume stable markets still rank above noise.
    High-volume markets (>HIGH_VOL) bypass the change filter entirely.
    """
    candidates = []
    for m in raw:
        volume = float(m.get("volume24hr") or 0)
        change = float(m.get("oneDayPriceChange") or 0)
        abs_change = abs(change)

        if volume < MIN_VOLUME:
            continue
        # Bypass change filter for very high-volume markets
        if abs_change < MIN_CHANGE and volume < HIGH_VOL:
            continue

        # Score: prioritise movement; volume is tie-breaker within same change tier
        # log(volume) smooths out the 100x gap between top and mid markets
        import math
        score = round(abs_change * 1000 + math.log10(max(volume, 1)), 2)

        candidates.append(
            {
                "id": m.get("id", ""),
                "question": m.get("question", ""),
                "probability": parse_yes_probability(m.get("outcomePrices")),
                "change_24h": round(change, 4),
                "volume_24h": round(volume, 2),
                "score": score,
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
                "snippet": r.get("content", "")[:150],  # keep tokens lean
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


def _call_llm(prompt: str) -> str:
    """
    Try Gemini models first; if all are rate-limited, fall back to OpenAI gpt-4o-mini.
    """
    # --- Gemini ---
    last_exc = None
    gemini_exhausted = True
    for model in GEMINI_MODELS:
        for attempt in range(2):
            try:
                response = gemini_client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(temperature=0.3),
                )
                log.debug("Used Gemini model %s", model)
                return response.text
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    m_delay = re.search(r"retry[^0-9]*([0-9]+(?:\.[0-9]+)?)\s*s", msg, re.IGNORECASE)
                    wait = float(m_delay.group(1)) + 2 if m_delay else 30 * (attempt + 1)
                    log.warning("429 on %s attempt %d, waiting %.0fs...", model, attempt + 1, wait)
                    time.sleep(wait)
                else:
                    gemini_exhausted = False
                    log.debug("Non-quota error on %s: %s", model, msg[:80])
                    break  # non-quota error → try next Gemini model

    # --- OpenAI fallback ---
    log.info("Gemini quota exhausted, falling back to OpenAI %s", OPENAI_MODEL)
    try:
        resp = oai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        log.debug("Used OpenAI model %s", OPENAI_MODEL)
        return resp.choices[0].message.content
    except Exception as exc:
        log.error("OpenAI also failed: %s", exc)
        raise exc


def generate_insight(market: dict, news: list[dict]) -> dict:
    """Generate bilingual (EN + ZH) structured insight from market data + Tavily news."""
    question = market["question"]
    prob = market["probability"]
    change = market["change_24h"]
    direction = "risen" if change > 0 else "fallen"
    sign = "+" if change > 0 else ""

    if news:
        news_block = "\n".join(f"- {r['title']}: {r['snippet']}" for r in news)
    else:
        news_block = "No recent news found. Base your analysis on the market data and your knowledge."

    prompt = f"""You are a financial analyst specializing in prediction markets.

MARKET: {question}
CURRENT PROBABILITY: {prob:.0%}
24H CHANGE: {sign}{change:.1%} (probability has {direction})

RECENT HEADLINES:
{news_block}

TASK:
Analyze the prediction market data and headlines. Explain why the probability moved.
Produce output in BOTH English (en) and Simplified Chinese (zh).

OUTPUT: Return ONLY a valid JSON object — no extra text, no markdown fences:
{{
  "category": "politics|ai_tech|economy|business|world|sports|crypto|other",
  "en": {{
    "title": "<8 words max, action-oriented>",
    "summary": "<2 sentences: what happened + why probability moved, cite specific events>",
    "drivers": ["<concrete driver <=15 words>", "<concrete driver <=15 words>", "<concrete driver <=15 words>"],
    "why_matters": "<1-2 sentences on broader significance>"
  }},
  "zh": {{
    "title": "<8字以内，动作导向>",
    "summary": "<2句话：发生了什么 + 为何概率变动，引用具体事件>",
    "drivers": ["<具体驱动因素，15字以内>", "<具体驱动因素，15字以内>", "<具体驱动因素，15字以内>"],
    "why_matters": "<1-2句话，说明更广泛的意义>"
  }}
}}

RULES:
- Be specific. Name events, people, data points.
- Bad: "market sentiment improved". Good: "CPI fell to 2.4%, below 2.6% forecast".
- 2-4 drivers only. Chinese must be natural, not literal translation.
- category must be exactly one of: politics, ai_tech, economy, business, world, sports, crypto, other."""

    try:
        raw_text = _call_llm(prompt)
        raw = _extract_json(raw_text)
        insight = json.loads(raw)
        # Validate / backfill both language keys
        for lang in ("en", "zh"):
            insight.setdefault(lang, {})
            for key in ("title", "summary", "why_matters"):
                insight[lang].setdefault(key, "")
            insight[lang].setdefault("drivers", [])
            if not isinstance(insight[lang]["drivers"], list):
                insight[lang]["drivers"] = [str(insight[lang]["drivers"])]
        # Resolve category
        insight["category"] = classify_category(
            market["question"], insight.get("category")
        )
        return insight
    except json.JSONDecodeError as exc:
        log.warning("JSON parse error for '%s': %s", question[:50], exc)
        return _fallback_insight(question, exc)
    except Exception as exc:
        log.error("Gemini error for '%s': %s", question[:50], exc)
        return _fallback_insight(question, exc)



def _fallback_insight(question: str, error: Exception) -> dict:
    err = str(error)[:120]
    return {
        "category": classify_category(question),
        "en": {
            "title": question[:70],
            "summary": "Insight generation encountered an error. Raw market data shown.",
            "drivers": [err],
            "why_matters": "Please check API keys and retry.",
        },
        "zh": {
            "title": question[:70],
            "summary": "洞察生成遇到错误，仅显示市场原始数据。",
            "drivers": [err],
            "why_matters": "请检查 API 密钥后重试。",
        },
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

    send_newsletter(output)
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


# ---------------------------------------------------------------------------
# 6. Newsletter (Resend)
# ---------------------------------------------------------------------------

def _build_email_html(data: dict) -> str:
    """Build a clean digest email with top 3 insights."""
    topics = data["topics"][:3]
    date_str = data["generated_at_readable"]

    def card(item: dict) -> str:
        m = item["market"]
        ins = item["insight"]
        prob = f"{m['probability']:.0%}"
        change = m["change_24h"]
        sign = "+" if change > 0 else ""
        color = "#22c55e" if change > 0 else "#ef4444"
        arrow = "▲" if change > 0 else "▼"
        return f"""
        <tr><td style="padding:16px 0 0;">
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#1a1a1e;border:1px solid #2a2a2e;border-radius:8px;">
            <tr><td style="padding:18px 20px;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="font-size:15px;font-weight:600;color:#e8e8ec;line-height:1.4;">{ins['en']['title']}</td>
                  <td align="right" style="white-space:nowrap;padding-left:12px;">
                    <span style="font-size:20px;font-weight:700;color:{color};">{prob}</span>
                    <span style="display:inline-block;margin-left:6px;font-size:11px;font-weight:600;color:{color};background:{'rgba(34,197,94,0.12)' if change>0 else 'rgba(239,68,68,0.12)'};padding:2px 7px;border-radius:4px;">{arrow} {sign}{change:.1%}</span>
                  </td>
                </tr>
              </table>
              <p style="margin:10px 0 0;font-size:13px;color:#b0b0bc;line-height:1.6;">{ins['en']['summary']}</p>
              <p style="margin:12px 0 0;">
                <a href="{SITE_URL}" style="font-size:12px;color:#7c6af7;text-decoration:none;">Read full analysis →</a>
              </p>
            </td></tr>
          </table>
        </td></tr>"""

    cards_html = "".join(card(t) for t in topics)
    count = len(data["topics"])

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0c0c0e;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0c0c0e;padding:32px 16px;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

        <!-- Header -->
        <tr><td style="padding-bottom:24px;border-bottom:1px solid #222226;">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="font-size:20px;font-weight:700;color:#e8e8ec;">Poly<span style="color:#7c6af7;">Lens</span></td>
              <td align="right" style="font-size:12px;color:#666672;">{date_str}</td>
            </tr>
          </table>
          <p style="margin:8px 0 0;font-size:13px;color:#666672;">Your AI-powered prediction market digest · {count} insights today</p>
        </td></tr>

        <!-- Cards -->
        {cards_html}

        <!-- CTA -->
        <tr><td style="padding:28px 0 0;text-align:center;">
          <a href="{SITE_URL}" style="display:inline-block;background:#7c6af7;color:#fff;font-size:14px;font-weight:600;padding:12px 28px;border-radius:8px;text-decoration:none;">
            View All {count} Insights →
          </a>
        </td></tr>

        <!-- Footer -->
        <tr><td style="padding:28px 0 0;border-top:1px solid #222226;margin-top:28px;font-size:11px;color:#444450;text-align:center;">
          <p style="margin:0;">Markets decide what matters. AI explains why.</p>
          <p style="margin:6px 0 0;">
            <a href="{SITE_URL}" style="color:#555560;text-decoration:none;">PolyLens</a> ·
            <a href="{SITE_URL}?unsubscribe=1" style="color:#555560;text-decoration:none;">Unsubscribe</a>
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def send_newsletter(data: dict) -> None:
    """Fetch all Resend audience contacts and send the digest email."""
    if not RESEND_API_KEY or not RESEND_AUDIENCE_ID:
        log.info("Newsletter skipped: RESEND_API_KEY or RESEND_AUDIENCE_ID not set")
        return

    # Fetch contacts from Resend Audience
    try:
        resp = requests.get(
            f"https://api.resend.com/audiences/{RESEND_AUDIENCE_ID}/contacts",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            timeout=15,
        )
        resp.raise_for_status()
        contacts = [
            c["email"] for c in resp.json().get("data", [])
            if not c.get("unsubscribed", False)
        ]
    except Exception as exc:
        log.error("Failed to fetch Resend contacts: %s", exc)
        return

    if not contacts:
        log.info("No subscribers yet — skipping newsletter send")
        return

    log.info("Sending newsletter to %d subscribers...", len(contacts))
    email_html = _build_email_html(data)
    date_str = data["generated_at_readable"]
    subject = f"PolyLens: {len(data['topics'])} Market Insights — {date_str}"

    # Resend batch endpoint (up to 100 per call)
    batch = [
        {
            "from": FROM_EMAIL,
            "to": [email],
            "subject": subject,
            "html": email_html,
            "tags": [{"name": "type", "value": "digest"}],
        }
        for email in contacts
    ]

    try:
        # Send in chunks of 100
        for i in range(0, len(batch), 100):
            chunk = batch[i : i + 100]
            r = requests.post(
                "https://api.resend.com/emails/batch",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=chunk,
                timeout=30,
            )
            r.raise_for_status()
            log.info("Sent batch %d-%d: %s", i + 1, i + len(chunk), r.status_code)
    except Exception as exc:
        log.error("Newsletter send failed: %s", exc)
        return

    log.info("Newsletter sent to %d subscribers.", len(contacts))


if __name__ == "__main__":
    run_pipeline()
