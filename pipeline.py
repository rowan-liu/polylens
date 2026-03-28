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
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
POLYMARKET_BASE = "https://gamma-api.polymarket.com"
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

GEMINI_MODELS = ["models/gemini-2.5-flash", "models/gemini-2.0-flash", "models/gemini-2.0-flash-lite"]
OPENAI_MODEL = "gpt-4o-mini"
TOP_N = 30
MIN_VOLUME = 500     # lower bar — let scoring sort quality
HIGH_VOL = 50_000     # markets above this are included regardless of change
RATE_LIMIT_SLEEP = 20
FETCH_LIMIT = 500
NEWS_PER_TOPIC = 5

# Category definitions (order determines keyword-match priority — sports before geopolitics)
CATEGORIES = ["sports", "politics", "ai_tech", "stocks", "economy", "geopolitics", "business", "crypto"]
# Categories that get guaranteed fill-in slots if empty after main scoring
GUARANTEED_CATS = ["ai_tech", "stocks", "economy", "geopolitics", "crypto", "business"]
CAT_MIN_SLOTS = 1   # at least this many per guaranteed category

# Polymarket event tag slugs to supplement general market feed per category
_TAG_FETCH: dict[str, list[str]] = {
    "stocks":      ["stocks", "finance"],
    "geopolitics": ["middle-east", "iran", "geopolitics"],
    "economy":     ["economy", "tariffs"],
    "ai_tech":     ["technology"],
    "crypto":      ["crypto"],
}
_TAG_FETCH_LIMIT = 30   # events per tag

_CAT_KEYWORDS: dict[str, list[str]] = {
    "sports":   ["nba","nfl","nhl","mlb","fifa","world cup","championship","premier league",
                 "olympics","tournament","grand slam","wimbledon","super bowl","league title",
                 "masters","pga","ufc","formula 1","f1 ","rookie of the year","mvp award",
                 "stanley cup","march madness","ncaa","tennis","soccer","football season",
                 "boxing","wrestling","mma","esports"],
    "politics": ["election","president","senator","congress","parliament","prime minister",
                 "republican","democrat","vote","ballot","campaign","governor","nomination",
                 "white house","administration","impeach","cabinet","minister","chancellor",
                 "trump","biden","harris","executive order","legislation","approval rating",
                 "midterm","polling","political party","supreme court"],
    "ai_tech":  ["artificial intelligence","openai","gpt-","chatgpt","gemini","claude","llm",
                 "machine learning","neural network","deep learning","automation",
                 "nvidia","chip","semiconductor","big tech","software model",
                 "apple inc","google search","microsoft ","meta platforms","anthropic",
                 "robot","self-driving","autonomous","data center","quantum"],
    "stocks":   ["s&p 500","s&p500","dow jones","nasdaq","stock price","stock market",
                 "nyse","russell 2000","index fund","circuit breaker","market high",
                 "apple stock","tesla","amazon stock","google stock","meta stock","nvidia stock",
                 "netflix stock","microsoft stock","bull market","bear market","earnings report",
                 "ipo listing","short squeeze","market cap overtake"],
    "economy":  ["federal reserve","interest rate","inflation","gdp",
                 "recession","employment","jobs report","treasury","bond yield",
                 "tariff","trade war","economic","unemployment","cpi","pce",
                 "debt ceiling","fiscal","deficit","imf","world bank","wto",
                 "dollar index","oil price","gold price","energy price"],
    "geopolitics": ["war","ceasefire","diplomatic","treaty","sanction","nato",
                 "nuclear","missile","troops","invasion","ukraine","russia","israel",
                 "taiwan","iran","middle east","united nations","coup","regime",
                 "north korea","pakistan","india border","military strike",
                 "us forces","airstrike","hostage","siege","occupation","conflict"],
    "business": ["acquisition","merger","ceo","startup","valuation","revenue",
                 "bankruptcy","buyout","takeover","venture capital","funding round",
                 "layoffs","product launch","quarterly","profit","loss","deal"],
    "crypto":   ["bitcoin","ethereum","crypto","blockchain","token","defi","btc","eth",
                 "solana","binance","nft","web3","stablecoin","coinbase","xrp","dogecoin"],
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
    """Fetch markets: general feed + tag-targeted event markets for diversity."""
    all_markets: dict[str, dict] = {}  # id → market dict, deduped

    # 1. General feed (high-volume, any category)
    try:
        resp = requests.get(
            f"{POLYMARKET_BASE}/markets",
            params={"limit": limit, "active": "true", "closed": "false"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        markets = data if isinstance(data, list) else data.get("data", data.get("markets", []))
        for m in markets:
            mid = str(m.get("id", ""))
            if mid:
                all_markets[mid] = m
        log.info("General feed: %d markets", len(all_markets))
    except Exception as exc:
        log.error("Failed to fetch general markets: %s", exc)

    # 2. Tag-targeted event markets (ensures under-represented categories appear)
    for cat, tag_slugs in _TAG_FETCH.items():
        for tag_slug in tag_slugs:
            try:
                resp = requests.get(
                    f"{POLYMARKET_BASE}/events",
                    params={
                        "limit": _TAG_FETCH_LIMIT,
                        "active": "true",
                        "closed": "false",
                        "tag_slug": tag_slug,
                        "order": "volume24hr",
                        "ascending": "false",
                    },
                    timeout=30,
                )
                if not resp.ok:
                    continue
                events = resp.json()
                if not isinstance(events, list):
                    events = events.get("data", [])
                added = 0
                for event in events:
                    # Pick the highest-volume market from each event
                    event_markets = event.get("markets", [])
                    if not event_markets:
                        continue
                    best = max(
                        event_markets,
                        key=lambda m: float(m.get("volume24hr") or 0),
                    )
                    # Backfill event-level volume if market-level is missing
                    if not best.get("volume24hr"):
                        best = best.copy()
                        best["volume24hr"] = event.get("volume24hr", 0)
                    mid = str(best.get("id", ""))
                    if mid and mid not in all_markets:
                        all_markets[mid] = best
                        added += 1
                if added:
                    log.info("  Tag '%s' added %d new markets", tag_slug, added)
            except Exception as exc:
                log.warning("Tag fetch '%s' failed: %s", tag_slug, exc)

    log.info("Total unique markets fetched: %d", len(all_markets))
    return list(all_markets.values())


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
    Adaptive threshold: try 1% → 0.5% → 0.3% until top_n slots are filled.
    Then guarantee at least CAT_MIN_SLOTS per GUARANTEED_CATS by pulling
    best available market from that category (threshold as low as 0.2%).
    Score = abs_change*1000 + log10(volume)
    """
    import math

    def _score(volume: float, abs_change: float) -> float:
        return round(abs_change * 1000 + math.log10(max(volume, 1)), 2)

    def _parse(m: dict) -> dict | None:
        volume = float(m.get("volume24hr") or 0)
        change = float(m.get("oneDayPriceChange") or 0)
        if volume < MIN_VOLUME:
            return None
        return {
            "id": m.get("id", ""),
            "question": m.get("question", ""),
            "probability": parse_yes_probability(m.get("outcomePrices")),
            "change_24h": round(change, 4),
            "volume_24h": round(volume, 2),
            "score": _score(volume, abs(change)),
            "url": f"https://polymarket.com/event/{m.get('slug', m.get('id', ''))}",
        }

    # Pre-parse all markets above MIN_VOLUME
    parsed = [r for m in raw if (r := _parse(m)) is not None]

    # Step 1: adaptive threshold to fill main feed
    for threshold in (0.01, 0.005, 0.003):
        main = [p for p in parsed if abs(p["change_24h"]) >= threshold or p["volume_24h"] >= HIGH_VOL]
        if len(main) >= top_n:
            log.info("Threshold %.1f%% → %d candidates", threshold * 100, len(main))
            break
    else:
        main = [p for p in parsed if abs(p["change_24h"]) >= 0.003 or p["volume_24h"] >= HIGH_VOL]

    main.sort(key=lambda x: x["score"], reverse=True)
    selected = main[:top_n]
    selected_ids = {m["id"] for m in selected}

    # Step 2: guaranteed category fill-in (low threshold: 0.2%)
    cat_counts = {}
    for m in selected:
        cat = classify_category(m["question"])
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    for cat in GUARANTEED_CATS:
        if cat_counts.get(cat, 0) >= CAT_MIN_SLOTS:
            continue
        # Find best unselected market for this category (relax volume to 500)
        candidates = [
            p for p in parsed
            if p["id"] not in selected_ids
            and classify_category(p["question"]) == cat
            and abs(p["change_24h"]) >= 0.002
            and p["volume_24h"] >= 500
        ]
        candidates.sort(key=lambda x: x["score"], reverse=True)
        for fill in candidates[:CAT_MIN_SLOTS - cat_counts.get(cat, 0)]:
            selected.append(fill)
            selected_ids.add(fill["id"])
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            log.info("  Filled category '%s' with: %s", cat, fill["question"][:60])

    selected.sort(key=lambda x: x["score"], reverse=True)
    log.info("Final selection: %d markets", len(selected))
    return selected


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
24H CHANGE: {sign}{change:.1%}

RECENT HEADLINES:
{news_block}

TASK:
{"The probability barely moved today. Explain the CURRENT STATE — what drives it at this level, what signals to watch, why it matters." if abs(change) < 0.01 else "Explain what drove this probability movement. Be specific about events, people, data."}
Also generate a concise bull case and bear case for this market.
Produce output in BOTH English (en) and Simplified Chinese (zh).

OUTPUT: Return ONLY a valid JSON object — no markdown fences:
{{
  "category": "politics|ai_tech|economy|business|world|sports|crypto|other",
  "en": {{
    "title": "<8 words max, action-oriented>",
    "summary": "<2 sentences: current situation + key driver, cite specific events>",
    "drivers": ["<concrete driver <=15 words>", "<concrete driver <=15 words>", "<concrete driver <=15 words>"],
    "why_matters": "<1-2 sentences on broader significance>",
    "bull_case": "<1 sentence: strongest reason probability goes higher, <=25 words>",
    "bear_case": "<1 sentence: strongest reason probability goes lower, <=25 words>"
  }},
  "zh": {{
    "title": "<8字以内>",
    "summary": "<2句话：当前状况+核心驱动，引用具体事件>",
    "drivers": ["<具体驱动因素，15字以内>", "<具体驱动因素，15字以内>", "<具体驱动因素，15字以内>"],
    "why_matters": "<1-2句话，更广泛的意义>",
    "bull_case": "<看多理由，25字以内>",
    "bear_case": "<看空理由，25字以内>"
  }}
}}

RULES:
- Be specific. Name events, people, data points. Never say "market sentiment".
- {"If little changed, explain WHAT keeps the probability at this level and WHY." if abs(change) < 0.01 else "Explain the specific catalyst for this move."}
- 2-4 drivers only. Chinese must be natural, not a literal translation.
- category must be exactly one of: politics, ai_tech, economy, business, world, sports, crypto, other."""

    try:
        raw_text = _call_llm(prompt)
        raw = _extract_json(raw_text)
        insight = json.loads(raw)
        # Validate / backfill both language keys
        for lang in ("en", "zh"):
            insight.setdefault(lang, {})
            for key in ("title", "summary", "why_matters", "bull_case", "bear_case"):
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
    store_snapshots(output)
    save_archive(output)
    log.info("=== Pipeline complete. %d insights generated. ===", len(results))

    send_newsletter(output)
    send_telegram(output)
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
# 5b. Archive (Markdown + JSON snapshot + index)
# ---------------------------------------------------------------------------

ARCHIVE_DIR = OUTPUT_DIR / "archive"

_CAT_EMOJI = {
    "politics": "🗳️", "ai_tech": "🤖", "economy": "📈", "stocks": "📊",
    "business": "💼", "geopolitics": "🌍", "sports": "⚽",
    "crypto": "₿", "other": "🔥",
}


def _battle_card(item: dict) -> dict:
    """Extract battle-card fields from a topic item."""
    m   = item["market"]
    ins = item.get("insight") or {}
    en  = ins.get("en") or {}
    prob = (m.get("probability") or 0.5)
    yes_pct = round(prob * 100)
    no_pct  = 100 - yes_pct
    ch = (m.get("change_24h") or 0)
    ch_pct = ch * 100

    # Conviction label
    if yes_pct >= 75:
        signal = "YES conviction: Strong 💪" if ch_pct >= 0 else "YES losing ground ⚠️"
    elif yes_pct >= 55:
        if ch_pct > 0.5:
            signal = "YES momentum building 📈"
        elif ch_pct < -0.5:
            signal = "Momentum shifting to NO ⚡"
        else:
            signal = "YES favored — watch for catalysts 👀"
    elif yes_pct >= 45:
        signal = "Too close to call — next move is critical 🎲"
    elif yes_pct >= 25:
        signal = "NO momentum: Strong 📉" if ch_pct < -0.5 else "NO asymmetry: High risk/reward 🎯"
    else:
        signal = "Strong NO conviction 🔴"

    # Arrow
    arrow = f"▲+{ch_pct:.1f}%" if ch_pct >= 0 else f"▼{ch_pct:.1f}%"

    return {
        "question": m.get("question", ""),
        "yes_pct": yes_pct,
        "no_pct": no_pct,
        "arrow": arrow,
        "signal": signal,
        "bull": (en.get("bull_case") or "")[:140],
        "bear": (en.get("bear_case") or "")[:140],
        "summary": (en.get("summary") or "")[:160],
        "cat": ins.get("category", "other"),
        "vol": m.get("volume_24h") or 0,
        "url": m.get("url", SITE_URL),
    }


def _build_twitter_thread(data: dict, max_topics: int = 5) -> str:
    """Battle-card Twitter/X thread — one card per tweet."""
    topics = data["topics"][:max_topics]
    date_str = data["generated_at_readable"]
    n = len(topics)
    total = len(data["topics"])
    tweets = []

    tweets.append(
        f"🔮 PolyLens Battle Cards — {date_str}\n\n"
        f"AI breaks down {n} key prediction markets: who's winning, who's bluffing 👇\n\n"
        f"[1/{n + 1}]"
    )

    for i, item in enumerate(topics, 2):
        bc = _battle_card(item)
        emoji = _CAT_EMOJI.get(bc["cat"], "🔥")
        q = bc["question"][:72] + ("…" if len(bc["question"]) > 72 else "")
        bull_short = bc["bull"][:90] + ("…" if len(bc["bull"]) > 90 else "")
        bear_short = bc["bear"][:90] + ("…" if len(bc["bear"]) > 90 else "")

        card = (
            f"🥊 {emoji} {q}\n\n"
            f"✅ YES — {bc['yes_pct']}%  {bc['arrow']}\n"
        )
        if bull_short:
            card += f'"{bull_short}"\n'
        card += (
            f"\n❌ NO — {bc['no_pct']}%\n"
        )
        if bear_short:
            card += f'"{bear_short}"\n'
        card += f"\n🤖 {bc['signal']}\n📊 hika.fyi  [{i}/{n + 1}]"

        tweets.append(card[:280])

    tweets.append(
        f"🔗 Full analysis + {total} markets → https://www.hika.fyi\n\n"
        f"Free · Updated every 8h · AI-powered\n"
        f"#Polymarket #PredictionMarkets #BattleCard\n\n"
        f"[{n + 1}/{n + 1}]"
    )
    return "\n\n---\n\n".join(tweets)


def _build_reddit_post(data: dict, max_topics: int = 8) -> str:
    """Generate a Reddit post (Markdown) for r/Polymarket or r/PredictionMarkets."""
    topics = data["topics"][:max_topics]
    date_str = data["generated_at_readable"]
    lines = [
        f"**PolyLens Daily Digest — {date_str}**\n",
        "AI-powered summary of today's biggest Polymarket movers (updated every 8 hours).\n",
        "---\n",
    ]
    for i, item in enumerate(topics, 1):
        m = item["market"]
        en = item["insight"].get("en", item["insight"])
        emoji = _CAT_EMOJI.get(classify_category(m["question"]), "🔥")
        prob = f"{m['probability'] * 100:.0f}%"
        chg = f"{'+' if m['change_24h'] >= 0 else ''}{m['change_24h'] * 100:.1f}%"
        title = en.get("title") or m["question"]
        summary = en.get("summary") or ""
        drivers = en.get("drivers") or []

        lines.append(f"## {i}. {emoji} {title}\n")
        lines.append(f"**{prob} YES** | **{chg}** (24h) | Vol: ${m['volume_24h']:,.0f}\n")
        lines.append(f"{summary}\n")
        if drivers:
            lines.append("**Key Drivers:**")
            for d in drivers[:3]:
                lines.append(f"- {d}")
            lines.append("")
        lines.append(f"*[View on Polymarket]({m['url']})*\n")
        lines.append("---\n")

    lines.append(
        "*Source: [PolyLens](https://www.hika.fyi) — free AI prediction market intelligence, updated every 8 hours*\n"
        "*[Subscribe to the 8-hour email digest](https://www.hika.fyi)*"
    )
    return "\n".join(lines)


def _build_markdown(data: dict) -> str:
    """Render a clean, publishable Markdown article from insight data."""
    date_str = data["generated_at_readable"]
    topics = data["topics"]
    lines: list[str] = []

    lines.append(f"# PolyLens — {date_str}")
    lines.append("")
    lines.append("> AI-powered prediction market intelligence | [hika.fyi](https://www.hika.fyi)")
    lines.append("")
    lines.append(f"*{len(topics)} markets analysed · updated every 8 hours*")
    lines.append("")
    lines.append("---")
    lines.append("")

    for i, item in enumerate(topics, 1):
        m = item["market"]
        ins = item["insight"]
        en = ins.get("en", {})
        zh = ins.get("zh", {})
        prob_pct = f"{m['probability']:.0%}"
        change = m["change_24h"]
        sign = "+" if change >= 0 else ""
        arrow = "▲" if change >= 0 else "▼"
        cat = ins.get("category", "other")

        lines.append(f"## {i}. {en.get('title', m['question'])}")
        lines.append("")
        # Chinese title on next line if available
        if zh.get("title"):
            lines.append(f"**{zh['title']}**")
            lines.append("")
        lines.append(
            f"| Probability | 24h Change | Volume | Category |"
        )
        lines.append(
            f"|-------------|-----------|--------|----------|"
        )
        lines.append(
            f"| **{prob_pct}** | {arrow} {sign}{change:.1%} | ${m['volume_24h']:,.0f} | {cat} |"
        )
        lines.append("")
        lines.append(f"*Market: [{m['question']}]({m['url']})*")
        lines.append("")

        if en.get("summary"):
            lines.append(en["summary"])
            lines.append("")
        if zh.get("summary"):
            lines.append(f"> {zh['summary']}")
            lines.append("")

        if en.get("drivers"):
            lines.append("**Key Drivers:**")
            for d in en["drivers"]:
                lines.append(f"- {d}")
            lines.append("")

        if zh.get("drivers"):
            lines.append("**核心驱动：**")
            for d in zh["drivers"]:
                lines.append(f"- {d}")
            lines.append("")

        if en.get("why_matters"):
            lines.append(f"> **Why it matters:** {en['why_matters']}")
            lines.append("")
        if zh.get("why_matters"):
            lines.append(f"> **为何重要：** {zh['why_matters']}")
            lines.append("")

        # News sources
        news = item.get("news", [])
        if news:
            src_links = " · ".join(
                f"[{j+1}]({n['url']})" for j, n in enumerate(news[:3])
            )
            lines.append(f"📰 Sources: {src_links}")
            lines.append("")

        lines.append("---")
        lines.append("")

    lines.append("*Generated by [PolyLens](https://www.hika.fyi) — Markets decide what matters. AI explains why.*")
    return "\n".join(lines)


def store_snapshots(data: dict) -> None:
    """Persist every market snapshot to Supabase market_snapshots table."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        log.warning("SUPABASE_SERVICE_KEY not set — skipping snapshot storage")
        return

    run_at = data["generated_at"]
    rows = []
    for item in data["topics"]:
        m = item["market"]
        ins = item.get("insight", {})
        en = ins.get("en", {}) if isinstance(ins, dict) else {}
        zh = ins.get("zh", {}) if isinstance(ins, dict) else {}
        rows.append({
            "run_at": run_at,
            "market_id": str(m["id"]),
            "question": m.get("question", "")[:500],
            "category": ins.get("category", ""),
            "probability": m.get("probability"),
            "volume_24h": m.get("volume_24h"),
            "change_24h": m.get("change_24h"),
            "insight_en": en.get("summary", "")[:1000],
            "insight_zh": zh.get("summary", "")[:1000],
        })

    if not rows:
        return

    url = f"{SUPABASE_URL}/rest/v1/market_snapshots"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    try:
        r = requests.post(url, headers=headers, json=rows, timeout=15)
        if r.ok:
            log.info("Stored %d snapshots to Supabase", len(rows))
        else:
            log.warning("Supabase snapshot insert failed: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Supabase snapshot error: %s", e)


def save_archive(data: dict) -> None:
    """Save dated Markdown + JSON snapshot; update archive/index.json."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    # Use the UTC timestamp from data to create a stable filename
    ts = datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00"))
    slug = ts.strftime("%Y-%m-%d")           # one file per day, latest run wins
    date_label = ts.strftime("%b %d, %Y %H:%M UTC")

    # 1. Markdown
    md_path = ARCHIVE_DIR / f"{slug}.md"
    md_content = _build_markdown(data)
    md_path.write_text(md_content, encoding="utf-8")
    log.info("Saved Markdown archive -> %s", md_path)

    # 2. JSON snapshot (strip heavy news snippets to keep size manageable)
    snapshot = {
        "generated_at": data["generated_at"],
        "generated_at_readable": data["generated_at_readable"],
        "topics": [],
    }
    for item in data["topics"]:
        snapshot["topics"].append({
            "market": item["market"],
            "insight": item["insight"],
            # Keep first 3 news items but drop long content field
            "news": [
                {"title": n.get("title", ""), "url": n.get("url", "")}
                for n in item.get("news", [])[:3]
            ],
        })
    json_path = ARCHIVE_DIR / f"{slug}.json"
    snapshot["twitter_thread"] = _build_twitter_thread(data)
    snapshot["reddit_post"] = _build_reddit_post(data)
    json_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Saved JSON snapshot -> %s", json_path)

    # 3. Update index.json
    index_path = ARCHIVE_DIR / "index.json"
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            index = {"snapshots": []}
    else:
        index = {"snapshots": []}

    # Upsert by slug — always update entry for today (latest run wins)
    new_entry = {
        "slug": slug,
        "label": date_label,
        "generated_at": data["generated_at"],
        "count": len(data["topics"]),
    }
    index["snapshots"] = [s for s in index["snapshots"] if s["slug"] != slug]
    index["snapshots"].insert(0, new_entry)
    # Keep at most 90 days
    index["snapshots"] = index["snapshots"][:90]

    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Updated archive index: %d entries", len(index["snapshots"]))
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


def _build_telegram_image(data: dict) -> bytes | None:
    """Generate a 1200×630 digest card using Pillow. Returns PNG bytes or None."""
    try:
        import io
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    W, H = 1200, 630
    PAD = 44
    BG      = (13,  17,  23)
    SURFACE = (22,  27,  34)
    BORDER  = (33,  38,  45)
    TEXT    = (230, 237, 243)
    MUTED   = (125, 133, 144)
    ACCENT  = (124, 106, 247)
    UP      = (34,  197,  94)
    DOWN    = (239,  68,  68)
    CAT_LABELS = {
        "politics": "POLITICS", "ai_tech": "AI & TECH", "economy": "ECONOMY",
        "business": "BUSINESS", "world": "WORLD", "sports": "SPORTS", "crypto": "CRYPTO",
    }

    def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        for p in [
            "/System/Library/Fonts/Helvetica.ttc",              # macOS
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
                else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold
                else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if bold
                else "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        ]:
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
        return ImageFont.load_default(size=size)  # final fallback

    def _wrap(d: ImageDraw.ImageDraw, text: str, font, max_w: int, max_lines: int = 2) -> list[str]:
        words, lines, cur = text.split(), [], ""
        for w in words:
            test = (cur + " " + w).strip()
            if d.textlength(test, font=font) <= max_w:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = w
            if len(lines) == max_lines:
                cur = cur.rstrip() + "…"
                break
        if cur and len(lines) < max_lines:
            lines.append(cur)
        return lines

    # Pick one best market per category (in order)
    cat_order = ["politics", "ai_tech", "economy", "business", "world", "crypto", "sports"]
    seen: dict[str, dict] = {}
    for item in data["topics"]:
        cat = (item.get("insight") or {}).get("category", "world")
        if cat not in seen:
            seen[cat] = item
    items = [seen[c] for c in cat_order if c in seen][:6]

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # ── Header ──────────────────────────────────────────────────────
    f_logo   = _font(32, bold=True)
    f_date   = _font(20)
    f_cat    = _font(11, bold=True)
    f_title  = _font(18, bold=True)
    f_prob   = _font(26, bold=True)
    f_delta  = _font(16)
    f_summ   = _font(14)
    f_foot   = _font(18)

    HDR_H = 68
    d.rectangle([0, 0, W, HDR_H], fill=SURFACE)
    d.text((PAD, 16), "PolyLens", font=f_logo, fill=ACCENT)
    ts = datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00"))
    date_str = ts.strftime("%b %d, %Y  %H:%M UTC")
    dw = d.textlength(date_str, font=f_date)
    d.text((W - PAD - dw, 24), date_str, font=f_date, fill=MUTED)
    d.rectangle([0, HDR_H, W, HDR_H + 1], fill=BORDER)

    # ── 2-column grid ───────────────────────────────────────────────
def _build_telegram_image(data: dict) -> bytes | None:
    """Generate a 1200×630 Market Battle Card image using Pillow."""
    try:
        import io
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    if not data.get("topics"):
        return None

    W, H = 1200, 630
    PAD = 44
    BG      = (13,  17,  23)
    SURFACE = (22,  27,  34)
    BORDER  = (33,  38,  45)
    TEXT    = (230, 237, 243)
    MUTED   = (125, 133, 144)
    ACCENT  = (124, 106, 247)
    UP      = (34,  197,  94)
    DOWN    = (239,  68,  68)
    YES_BG  = (20,  50,  30)
    NO_BG   = (50,  18,  18)

    def _font(size: int, bold: bool = False):
        for p in [
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
                else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold
                else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]:
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
        return ImageFont.load_default(size=size)

    def _wrap(d, text, font, max_w, max_lines=2):
        words, lines, cur = text.split(), [], ""
        for w in words:
            test = (cur + " " + w).strip()
            if d.textlength(test, font=font) <= max_w:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = w
            if len(lines) == max_lines:
                cur = cur.rstrip() + "…"
                break
        if cur and len(lines) < max_lines:
            lines.append(cur)
        return lines

    # Use top market for the battle card
    bc = _battle_card(data["topics"][0])
    # Mini list: next 4 markets
    mini = [_battle_card(t) for t in data["topics"][1:5]]

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    f_logo  = _font(28, bold=True)
    f_date  = _font(17)
    f_tag   = _font(11, bold=True)
    f_q     = _font(22, bold=True)
    f_pct   = _font(52, bold=True)
    f_side  = _font(13, bold=True)
    f_body  = _font(14)
    f_sig   = _font(15, bold=True)
    f_mini  = _font(13)
    f_foot  = _font(15)

    HDR_H = 58
    FOOT_H = 36

    # ── Header ──────────────────────────────────────────────────────
    d.rectangle([0, 0, W, HDR_H], fill=SURFACE)
    d.text((PAD, 14), "PolyLens", font=f_logo, fill=ACCENT)
    d.text((PAD + 180, 20), "Market Battle Card", font=f_date, fill=MUTED)
    ts = datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00"))
    date_str = ts.strftime("%b %d, %Y  %H:%M UTC")
    dw = d.textlength(date_str, font=f_date)
    d.text((W - PAD - dw, 20), date_str, font=f_date, fill=MUTED)
    d.rectangle([0, HDR_H, W, HDR_H + 1], fill=BORDER)

    # ── Question ───────────────────────────────────────────────────
    MAIN_W = W - PAD * 2 - 260  # leave 260px for mini list on right
    q_lines = _wrap(d, bc["question"], f_q, MAIN_W, max_lines=2)
    QY = HDR_H + 16
    for i, ln in enumerate(q_lines):
        d.text((PAD, QY + i * 28), ln, font=f_q, fill=TEXT)
    QY += len(q_lines) * 28 + 10

    # Category chip
    cat_label = bc["cat"].replace("_", " ").upper()
    chip_w = int(d.textlength(cat_label, font=f_tag)) + 14
    d.rounded_rectangle([PAD, QY, PAD + chip_w, QY + 18], radius=4, fill=BORDER)
    d.text((PAD + 7, QY + 2), cat_label, font=f_tag, fill=MUTED)
    QY += 26

    # ── YES / NO columns ────────────────────────────────────────────
    COL_H = H - QY - FOOT_H - 8
    COL_W = (MAIN_W - 10) // 2
    YES_X, NO_X = PAD, PAD + COL_W + 10

    # YES panel
    d.rounded_rectangle([YES_X, QY, YES_X + COL_W, QY + COL_H], radius=10, fill=YES_BG)
    d.rounded_rectangle([YES_X, QY, YES_X + COL_W, QY + 36], radius=10, fill=(30, 70, 40))
    d.text((YES_X + 12, QY + 8), "✅  YES", font=f_side, fill=UP)
    d.text((YES_X + 12, QY + 42), f"{bc['yes_pct']}%", font=f_pct, fill=UP)
    d.text((YES_X + 12, QY + 100), bc["arrow"] if bc["yes_pct"] >= 50 else "", font=f_date, fill=UP)
    bull_lines = _wrap(d, bc["bull"] or "No bull case available", f_body, COL_W - 24, max_lines=4)
    for i, ln in enumerate(bull_lines):
        d.text((YES_X + 12, QY + 122 + i * 20), ln, font=f_body, fill=(180, 230, 180))

    # NO panel
    d.rounded_rectangle([NO_X, QY, NO_X + COL_W, QY + COL_H], radius=10, fill=NO_BG)
    d.rounded_rectangle([NO_X, QY, NO_X + COL_W, QY + 36], radius=10, fill=(70, 25, 25))
    d.text((NO_X + 12, QY + 8), "❌  NO", font=f_side, fill=DOWN)
    d.text((NO_X + 12, QY + 42), f"{bc['no_pct']}%", font=f_pct, fill=DOWN)
    bear_lines = _wrap(d, bc["bear"] or "No bear case available", f_body, COL_W - 24, max_lines=4)
    for i, ln in enumerate(bear_lines):
        d.text((NO_X + 12, QY + 122 + i * 20), ln, font=f_body, fill=(230, 180, 180))

    # Signal line
    SIG_Y = QY + COL_H - 30
    d.text((PAD, SIG_Y), f"AI  {bc['signal']}", font=f_sig, fill=ACCENT)

    # ── Mini list (right side) ───────────────────────────────────────
    MX = W - 260 + 10
    d.rectangle([MX - 8, HDR_H + 1, MX - 8, H - FOOT_H], fill=BORDER)
    d.text((MX, HDR_H + 14), "Also watching:", font=f_tag, fill=MUTED)
    for i, m2 in enumerate(mini):
        my = HDR_H + 36 + i * 112
        emoji2 = _CAT_EMOJI.get(m2["cat"], "🔥")
        q2 = (m2["question"][:38] + "…") if len(m2["question"]) > 38 else m2["question"]
        d.text((MX, my), q2, font=f_mini, fill=TEXT)
        col2 = UP if m2["yes_pct"] >= 50 else DOWN
        d.text((MX, my + 18), f"YES {m2['yes_pct']}%  {m2['arrow']}", font=f_mini, fill=col2)
        sig2 = m2["signal"][:38] + ("…" if len(m2["signal"]) > 38 else "")
        d.text((MX, my + 36), sig2, font=_font(11), fill=MUTED)
        if i < len(mini) - 1:
            d.rectangle([MX, my + 52, MX + 220, my + 53], fill=BORDER)

    # ── Footer ──────────────────────────────────────────────────────
    d.rectangle([0, H - FOOT_H, W, H], fill=SURFACE)
    foot = f"www.hika.fyi  ·  {len(data['topics'])} markets  ·  Updated every 8h"
    fw = d.textlength(foot, font=f_foot)
    d.text(((W - fw) // 2, H - FOOT_H + 10), foot, font=f_foot, fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def send_telegram(data: dict) -> None:
    """Push battle-card digest (image + text) to the configured Telegram channel."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        log.info("Telegram skipped: bot token or channel ID not set")
        return

    ts = datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00"))
    date_str = ts.strftime("%b %d, %Y %H:%M UTC")

    # Build battle-card message for top 5 markets (≤4096 chars)
    topics = data["topics"][:5]
    lines = [f"🔮 <b>PolyLens Battle Cards</b> · {date_str}\n"]

    for item in topics:
        bc = _battle_card(item)
        emoji = _CAT_EMOJI.get(bc["cat"], "🔥")
        q = bc["question"][:70] + ("…" if len(bc["question"]) > 70 else "")
        lines.append(f"🥊 {emoji} <b>{q}</b>")
        lines.append(f"✅ YES {bc['yes_pct']}%  {bc['arrow']}  |  ❌ NO {bc['no_pct']}%")
        if bc["bull"]:
            lines.append(f'<i>YES: "{bc["bull"][:100]}"</i>')
        if bc["bear"]:
            lines.append(f'<i>NO: "{bc["bear"][:100]}"</i>')
        lines.append(f"🤖 {bc['signal']}")
        lines.append("")

    lines.append(f'🌐 <a href="{SITE_URL}">{SITE_URL.replace("https://","")}</a> · {len(data["topics"])} markets tracked')
    text = "\n".join(lines)[:4096]

    bot_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    img_bytes = _build_telegram_image(data)

    try:
        import io as _io
        if img_bytes:
            # Send battle card image with shortened caption
            short_cap = "\n".join(lines[:3 + len(topics) * 2])[:1024]
            r = requests.post(f"{bot_url}/sendPhoto", data={
                "chat_id": TELEGRAM_CHANNEL_ID,
                "caption": short_cap,
                "parse_mode": "HTML",
            }, files={"photo": ("battle.png", _io.BytesIO(img_bytes), "image/png")}, timeout=30)
            if r.ok:
                # Send full battle-card text as follow-up message
                requests.post(f"{bot_url}/sendMessage", json={
                    "chat_id": TELEGRAM_CHANNEL_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }, timeout=15)
        else:
            r = requests.post(f"{bot_url}/sendMessage", json={
                "chat_id": TELEGRAM_CHANNEL_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=15)

        if r.ok:
            log.info("Telegram battle cards sent to %s", TELEGRAM_CHANNEL_ID)
        else:
            log.warning("Telegram send failed: %s %s", r.status_code, r.text[:300])
    except Exception as exc:
        log.warning("Telegram error: %s", exc)


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
