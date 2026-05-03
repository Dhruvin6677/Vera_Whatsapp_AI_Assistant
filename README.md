# Vera API · v2.0

> **AI-powered WhatsApp messaging engine for local merchant growth.**
> Vera scores every trigger, routes by priority, and fires hyper-personalised messages — automatically.

---

## Table of Contents

- [Overview](#overview)
- [Algorithm](#algorithm)
- [Installation](#installation)
- [Environment Variables](#environment-variables)
- [API Reference](#api-reference)
- [Priority Scoring Formula](#priority-scoring-formula)
- [Tier Routing](#tier-routing)
- [Trigger Kinds](#trigger-kinds)
- [Compulsion Injection](#compulsion-injection)
- [Suppression & Cooldowns](#suppression--cooldowns)
- [Project Structure](#project-structure)
- [Production Notes](#production-notes)

---

## Overview

Vera is a FastAPI service that sits between your data platform and WhatsApp.  
On every `/v1/tick` call it:

1. Receives a batch of trigger IDs
2. Scores and filters them by priority
3. Generates a personalised 2–3 line message via Groq LLM
4. Returns structured dispatch actions ready to send

Categories supported: **Dentists · Salons · Restaurants** (and any `general` fallback)  
Language support: **English + Hinglish**

---

## Algorithm

Vera's core is a **7-stage Priority Scoring Pipeline**:

```
Trigger ingestion
      │
      ▼
Dedup + Suppression check   ──► skip if within cooldown window
      │
      ▼
Priority Scoring Engine
  score = (urgency × ctr_gap × recency_decay × customer_value) ^ 0.25
      │
      ▼
Tier Routing
  HIGH ≥ 0.70  →  full compulsion suffix
  MED  ≥ 0.40  →  soft CTA suffix
  LOW  < 0.40  →  silently skipped
      │
      ▼
Context Enrichment
  merchant signals + customer signals + peer benchmarks
      │
      ▼
Ranked Prompt Builder
  picks the single strongest data point as the message hook
      │
      ▼
LLM + Compulsion Injection
      │
      ▼
Truncate → Log → Emit action payload
```

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/your-org/vera-api.git
cd vera-api

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install fastapi uvicorn httpx pydantic

# 4. Set your Groq API key
export GROQ_API_KEY=gsk_your_key_here

# 5. Run the server
uvicorn bot:app --reload --port 8000
```

---

## Environment Variables

| Variable       | Required | Default                              | Description                     |
|----------------|----------|--------------------------------------|---------------------------------|
| `GROQ_API_KEY` | Yes      | —                                    | Your Groq API key               |
| `GROQ_MODEL`   | No       | `llama-3.1-70b-versatile`            | LLM model to use                |

---

## API Reference

### `GET /v1/healthz`
Health check.

**Response**
```json
{ "status": "ok" }
```

---

### `GET /v1/metadata`
Returns team and model info.

**Response**
```json
{
  "team_name": "VeraElite",
  "model": "llama-3.1-70b-versatile",
  "algo_version": "priority-scoring-v2"
}
```

---

### `POST /v1/context`
Store any object (merchant, category, trigger, customer) into the in-memory context store.

**Request body**
```json
{
  "scope":      "merchant",
  "context_id": "merchant_123",
  "version":    1,
  "payload": {
    "identity":      { "name": "Smile Dental" },
    "category_slug": "dentist",
    "performance":   { "ctr": 0.018, "views": 420 },
    "offers": [
      { "title": "Free whitening consult", "status": "active" }
    ]
  }
}
```

**Supported scopes:** `merchant` · `category` · `trigger` · `customer`

**Response**
```json
{ "accepted": true }
```

---

### `POST /v1/tick`
Core engine endpoint. Processes a batch of triggers and returns dispatch actions.

**Request body**
```json
{
  "available_triggers": ["trigger_001", "trigger_002"]
}
```

Each trigger should be pre-loaded via `/v1/context` with `scope: "trigger"` and a payload like:

```json
{
  "kind":            "perf_drop",
  "merchant_id":     "merchant_123",
  "customer_id":     "customer_456",
  "suppression_key": "merchant_123_perf",
  "cooldown_secs":   21600,
  "created_at":      1717000000,
  "payload": {
    "top_item": {
      "title":  "New whitening technique trending in Hyderabad",
      "source": "Google Trends"
    }
  }
}
```

**Response**
```json
{
  "actions": [
    {
      "conversation_id": "merchant_123_trigger_001",
      "merchant_id":     "merchant_123",
      "customer_id":     "customer_456",
      "send_as":         "merchant_on_behalf",
      "trigger_id":      "trigger_001",
      "body":            "Your CTR is 40% below the dentist category average this week. One profile update can close that gap — want me to do it now?",
      "cta":             "open_ended",
      "suppression_key": "merchant_123_perf",
      "score":           0.7312,
      "tier":            "HIGH",
      "rationale":       "kind=perf_drop score=0.7312 tier=HIGH"
    }
  ]
}
```

---

### `POST /v1/reply`
Handle an inbound reply from a merchant or customer. Returns a follow-up action.

**Request body**
```json
{
  "conversation_id": "merchant_123_trigger_001",
  "message":         "haan kar do"
}
```

**Response (yes intent)**
```json
{
  "action": "send",
  "body":   "Done 👍 I've set this up. Results usually show within 48h — want daily updates?",
  "cta":    "open_ended"
}
```

**Response (no / end intent)**
```json
{ "action": "end" }
```

---

## Priority Scoring Formula

```
score = (urgency × ctr_gap × recency_decay × customer_value) ^ 0.25
```

Each factor is normalised to `[0, 1]`. The geometric mean ensures no single factor can dominate.

| Factor            | Source                                      | Notes                                         |
|-------------------|---------------------------------------------|-----------------------------------------------|
| `urgency`         | Trigger `kind` keyword                      | competitor=1.0, recall/perf=0.8, research=0.6 |
| `ctr_gap`         | `(peer_ctr - merchant_ctr) / peer_ctr`      | Higher when merchant underperforms peers      |
| `recency_decay`   | `exp(-0.1 × age_hours)`                     | ~0.9 at 1h, ~0.37 at 10h, ~0.1 at 23h        |
| `customer_value`  | Customer LTV + tier bonus                   | VIP +0.3, Loyal +0.2, New +0.05               |

**Hard floor:** competitor triggers always score ≥ `0.70` so they're never silently dropped.

---

## Tier Routing

| Tier   | Score Range | Behaviour                                   |
|--------|-------------|---------------------------------------------|
| `HIGH` | ≥ 0.70      | Full compulsion suffix — FOMO / urgency      |
| `MED`  | 0.40–0.69   | Soft CTA suffix — helpful and low pressure  |
| `LOW`  | < 0.40      | Silently skipped — no message sent           |

---

## Trigger Kinds

Vera recognises these keywords in the trigger `kind` field:

| Keyword in `kind` | Classified as   | Urgency |
|-------------------|-----------------|---------|
| `competitor`      | `COMPETITOR`    | 5 / 5   |
| `recall`          | `CUSTOMER`      | 4 / 5   |
| `perf`            | `PERFORMANCE`   | 4 / 5   |
| `research`        | `RESEARCH`      | 3 / 5   |
| `promo`           | `GENERIC`       | 3 / 5   |
| anything else     | `GENERIC`       | 1 / 5   |

---

## Compulsion Injection

After the LLM generates a message, Vera appends a psychologically tuned suffix based on tier and trigger kind.

**HIGH tier examples**

| Trigger kind  | Suffix appended                                                              |
|---------------|------------------------------------------------------------------------------|
| `COMPETITOR`  | *"Competitors nearby are already running this. Want to match them today?"*  |
| `PERFORMANCE` | *"You're losing {ctr_gap}% visibility vs peers — this is recoverable."*     |
| `CUSTOMER`    | *"{name} hasn't visited in {N} days — this could bring them back."*         |
| `RESEARCH`    | *"Clinics using this saw a 2× inquiry jump last quarter."*                  |

**MED tier** uses softer variants. **LOW tier** — no suffix, no send.

---

## Suppression & Cooldowns

Vera tracks every `suppression_key` with a `last_sent` timestamp.  
If the same key is triggered again within `cooldown_secs`, it is silently dropped.

Default cooldown: **6 hours** (`21600` seconds).  
Override per-trigger by setting `cooldown_secs` in the trigger payload.

> **Production note:** Replace the in-memory `_suppression` dict with Redis `SETEX` for multi-instance deployments.

---

## Project Structure

```
vera-api/
├── bot.py          # Full application (single-file, self-contained)
├── README.md       # This file
└── requirements.txt
```

**`requirements.txt`**
```
fastapi>=0.111.0
uvicorn[standard]>=0.29.0
httpx>=0.27.0
pydantic>=2.7.0
```

---

## Production Notes

| Concern              | Current (dev)              | Recommended (prod)                        |
|----------------------|----------------------------|-------------------------------------------|
| Context store        | In-memory Python dict      | Redis with TTL per scope                  |
| Suppression store    | In-memory Python dict      | Redis `SETEX`                             |
| Conversation history | In-memory Python dict      | Redis list per conversation ID            |
| Phrase dedup         | In-memory set per merchant | Persistent store keyed by merchant ID     |
| LLM provider         | Groq                       | Any OpenAI-compatible endpoint            |
| Auth                 | None                       | API key header or JWT middleware          |
| Observability        | `logging` to stdout        | Structured JSON logs → Datadog / Grafana  |

---

*Built by VeraElite · Powered by Groq + LLaMA 3.1*
