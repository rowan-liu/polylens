# 📄 agent.md

---

## 🧠 Project Overview

Build an AI-powered information product that:

> Uses prediction markets (Polymarket) to identify important topics, and generates concise insights using news as supporting evidence.

---

## 🎯 Core Concept

```text
Market (signal) → Topic → News → Insight
```

NOT a news aggregator.

Instead:

* Market = ground truth (what matters)
* News = supporting evidence
* Insight = final product

---

## 🧱 System Architecture

### 4 Core Modules

```text
1. Market Ingestion
2. Topic Extraction
3. News Aggregation
4. Insight Generation
```

---

## 1️⃣ Market Ingestion

### Data Source

Use Polymarket Gamma API (public, no auth):

* Base URL:
  `https://gamma-api.polymarket.com` ([Polymarket Documentation][1])

* Key endpoints:

  * `/markets`
  * `/events` ([Polymarket Documentation][2])

---

### Required Fields

From each market:

```json
{
  "id": "...",
  "question": "...",
  "volume24hr": 12345,
  "outcomePrices": ["0.62", "0.38"],
  "oneDayPriceChange": 0.08
}
```

Notes:

* outcomePrices[0] = YES probability ([Polymarket Documentation][2])
* price = probability

---

### Filtering Logic

```python
if volume24hr < 10000:
    ignore

if abs(oneDayPriceChange) < 0.02:
    ignore
```

---

### Ranking

```python
score = volume24hr * abs(oneDayPriceChange)
```

Select:

```python
top_markets = top 10 by score
```

---

## 2️⃣ Topic Extraction

### Rule (MVP)

```python
topic = market.question
```

---

### Optional Enhancement

Use LLM:

```text
Convert question into a short topic phrase

Input:
"Will the Fed cut rates before June?"

Output:
"Fed Rate Cuts"
```

---

## 3️⃣ News Aggregation

### Input

```python
query = topic
```

---

### Requirements

* Fetch top 5–10 headlines
* Deduplicate
* Keep only recent (≤ 24h)

---

### Filtering (IMPORTANT)

Only keep news that can explain market movement:

```python
if not relevant_to_market_change:
    discard
```

---

## 4️⃣ Insight Generation ⭐ CORE

---

### Input

```json
{
  "question": "...",
  "probability": 0.62,
  "change_24h": 0.08,
  "news": [...]
}
```

---

### Prompt (Production Version)

```text
You are analyzing a prediction market.

Market:
{question}

Data:
- Current probability: {prob}
- 24h change: {change}

News:
{headlines}

Task:
1. Explain why the probability moved
2. Identify 2-4 concrete drivers
3. Explain why this matters

Rules:
- Be concise
- Avoid generic statements
- Focus on causality
- If uncertain, say so

Output format:

Title:
<short title>

Summary:
<2 sentences max>

Key Drivers:
- ...
- ...

Why it matters:
<1-2 sentences>
```

---

### Output Example

```text
Title:
Fed Rate Cut Expectations Rising

Summary:
Probability increased from 48% to 62% following new inflation data and central bank signals.

Key Drivers:
- Lower-than-expected CPI data
- Dovish comments from Fed officials
- Bond yields declining

Why it matters:
Markets are pricing in a shift toward looser monetary policy.
```

---

## 🖥️ API Design

---

### Internal Functions

```python
get_markets()
rank_markets()
extract_topic(market)
get_news(topic)
generate_insight(data)
```

---

### External API (future-ready)

```http
GET /topics
GET /topics/{id}
GET /topics/{id}/insight
GET /topics/{id}/news
```

---

## 🗄️ Data Schema

---

### markets

```sql
id TEXT
question TEXT
probability FLOAT
change_24h FLOAT
volume FLOAT
```

---

### topics

```sql
id TEXT
title TEXT
market_id TEXT
score FLOAT
```

---

### signals (news)

```sql
id TEXT
topic_id TEXT
title TEXT
source TEXT
timestamp DATETIME
```

---

### insights

```sql
topic_id TEXT
title TEXT
summary TEXT
drivers JSON
why TEXT
created_at DATETIME
```

---

## 🔁 Pipeline

---

### Cron Jobs

#### Every 10 min:

```text
1. Fetch markets
2. Rank markets
3. Select top 10
```

---

#### Every 15 min:

```text
4. Fetch news per topic
5. Generate insight
6. Store results
```

---

---

## 🧩 Frontend Output Format

Each card:

```text
----------------------------------
Fed Rate Cut → 62% ↑ +14%

Summary:
...

Key Drivers:
- ...
- ...

Why it matters:
...
----------------------------------
```

---

## ⚠️ Constraints

---

### 1. Do NOT build:

* trading features
* orderbook integration
* user accounts

---

### 2. Focus only on:

* insight quality
* topic selection
* clarity

---

## 🧪 Evaluation Criteria

---

### Good Output

* Explains **why probability moved**
* Uses **specific events**
* Not generic

---

### Bad Output

* “Market sentiment increased”
* “There is uncertainty”
* No causality

---

## 🚀 Future Extensions (Do NOT build now)

---

### 1. Signal Layer Expansion

* Twitter
* Reddit
* On-chain data

---

### 2. Research Mode

```text
Bull case
Bear case
Timeline
```

---

### 3. Agent Skills

```text
explain_market
latest_updates
deep_research
```

---

## 🧠 Core Principle

```text
Markets decide what matters
AI explains why
```

---

## ✅ MVP Definition

The system is complete if:

* It outputs 10 topics
* Each topic has:

  * probability
  * insight
* Users can understand “what happened today” in < 2 minutes

---

# ✅ END

