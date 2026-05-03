import logging
import os
import textwrap
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# =============================================================================
# CONFIG
# =============================================================================

GROQ_API_KEY = os.getenv("GROQ_API_KEY")  # <-- PUT YOUR GROQ API KEY HERE
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.1-70b-versatile"

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY environment variable is not set.")

SYSTEM_PROMPT = textwrap.dedent("""
    You are Vera, an AI assistant helping local merchants grow.

    Rules:
    - ALWAYS include one concrete fact (number, price, or stat)
    - Keep the message short — 2 to 3 lines maximum
    - Match the category tone:
        Dentists    → clinical and professional
        Salons      → warm and friendly
        Restaurants → business-like
    - No generic phrases like "grow your business"
    - End with exactly one clear CTA
    - Do NOT invent or assume data
    - Use Hinglish naturally when appropriate
""").strip()

# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class ContextBody(BaseModel):
    scope:      str
    context_id: str
    version:    int
    payload:    dict[str, Any]


class TickBody(BaseModel):
    available_triggers: list[str] = Field(default_factory=list)


class ReplyBody(BaseModel):
    conversation_id: str
    message:         str

# =============================================================================
# IN-MEMORY STORE  (replace with Redis for production)
# =============================================================================

_contexts:      dict[str, dict] = {}   # key = "scope:context_id"
_conversations: dict[str, list[str]] = {}


def ctx_key(scope: str, context_id: str) -> str:
    return f"{scope}:{context_id}"


def get_ctx(scope: str, cid: str) -> dict | None:
    entry = _contexts.get(ctx_key(scope, cid))
    return entry["payload"] if entry else None

# =============================================================================
# LLM
# =============================================================================

async def call_llm(prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json",
    }
    body = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        "temperature": 0,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            res  = await client.post(GROQ_URL, headers=headers, json=body)
            res.raise_for_status()
            data = res.json()
            return data["choices"][0]["message"]["content"].strip()

        except httpx.HTTPStatusError as exc:
            log.error("Groq HTTP error %s: %s", exc.response.status_code, exc.response.text)
        except httpx.RequestError as exc:
            log.error("Groq request error: %s", exc)
        except (KeyError, IndexError) as exc:
            log.error("Unexpected Groq response shape: %s", exc)

    return "Quick update for you — want me to help improve this?"

# =============================================================================
# TRIGGER DETECTION
# =============================================================================

_TRIGGER_MAP = {
    "research":   "RESEARCH",
    "perf":       "PERFORMANCE",
    "recall":     "CUSTOMER",
    "competitor": "COMPETITOR",
}


def detect_trigger_type(trigger: dict) -> str:
    kind = trigger.get("kind", "").lower()
    for keyword, label in _TRIGGER_MAP.items():
        if keyword in kind:
            return label
    return "GENERIC"

# =============================================================================
# DATA EXTRACTION
# =============================================================================

def extract_data(category: dict, merchant: dict, trigger: dict, customer: dict | None) -> dict:
    perf     = merchant.get("performance", {})
    offers   = merchant.get("offers", [])
    payload  = trigger.get("payload", {})
    top_item = payload.get("top_item", {})

    active_offers = [o["title"] for o in offers if o.get("status") == "active"]

    return {
        "ctr":              perf.get("ctr"),
        "views":            perf.get("views"),
        "peer_ctr":         category.get("peer_stats", {}).get("avg_ctr"),
        "offer":            active_offers[0] if active_offers else None,
        "research_title":   top_item.get("title"),
        "research_source":  top_item.get("source"),
        "customer_name":    customer.get("identity", {}).get("name") if customer else None,
    }

# =============================================================================
# PROMPT BUILDER
# =============================================================================

def build_prompt(category: dict, merchant: dict, trigger_type: str, data: dict) -> str:
    return textwrap.dedent(f"""
        CONTEXT:
        Merchant : {merchant.get('identity', {}).get('name', 'Unknown')}
        Category : {category.get('slug', 'Unknown')}
        Trigger  : {trigger_type}

        DATA:
        CTR            : {data.get('ctr', 'N/A')}
        Peer Avg CTR   : {data.get('peer_ctr', 'N/A')}
        Active Offer   : {data.get('offer', 'None')}
        Research Title : {data.get('research_title', 'N/A')}
        Customer       : {data.get('customer_name', 'N/A')}

        TASK:
        Write a WhatsApp message that:
        - Uses the strongest available data point above
        - Is engaging and specific
        - Ends with one clear CTA
    """).strip()

# =============================================================================
# COMPULSION ENGINE
# =============================================================================

_COMPULSION = {
    "RESEARCH":    " Want me to break this down for your patients?",
    "CUSTOMER":    " I can book it for you in 1 tap.",
    "COMPETITOR":  " Nearby businesses are already doing this.",
    "GENERIC":     " Want me to help?",
}


def apply_compulsion(trigger_type: str, msg: str, data: dict) -> str:
    if (
        trigger_type == "PERFORMANCE"
        and data.get("ctr")
        and data.get("peer_ctr")
    ):
        return f"{msg} That's below the {data['peer_ctr']} category average — you're losing visibility."

    suffix = _COMPULSION.get(trigger_type, " Want me to help?")
    return msg + suffix

# =============================================================================
# MESSAGE GENERATION
# =============================================================================

def _truncate_at_sentence(text: str, limit: int = 300) -> str:
    """Truncate at the last sentence boundary within `limit` characters."""
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    for sep in (".", "!", "?"):
        idx = truncated.rfind(sep)
        if idx != -1:
            return truncated[: idx + 1].strip()
    return truncated.strip()


async def generate_message(
    category: dict,
    merchant: dict,
    trigger:  dict,
    customer: dict | None = None,
) -> str:
    trigger_type = detect_trigger_type(trigger)
    data         = extract_data(category, merchant, trigger, customer)
    prompt       = build_prompt(category, merchant, trigger_type, data)
    msg          = await call_llm(prompt)
    msg          = apply_compulsion(trigger_type, msg, data)
    return _truncate_at_sentence(msg)

# =============================================================================
# COMPOSE
# =============================================================================

async def compose(
    category: dict,
    merchant: dict,
    trigger:  dict,
    customer: dict | None = None,
) -> dict:
    body = await generate_message(category, merchant, trigger, customer)
    return {
        "body":            body,
        "cta":             "open_ended",
        "send_as":         "merchant_on_behalf" if customer else "vera",
        "suppression_key": trigger.get("suppression_key", ""),
        "rationale":       f"Generated for trigger kind={trigger.get('kind')}",
    }

# =============================================================================
# REPLY INTENT HELPERS
# =============================================================================

_YES_WORDS = {"yes", "ok", "okay", "go ahead", "do it", "sure", "haan", "haa"}
_NO_WORDS  = {"stop", "no", "not interested", "nahi", "nope", "cancel"}


def _is_auto_close(msg: str, history: list[str]) -> bool:
    return history.count(msg) >= 2 or "thank you" in msg


def _is_yes(msg: str) -> bool:
    return any(word in msg for word in _YES_WORDS)


def _is_no(msg: str) -> bool:
    return any(word in msg for word in _NO_WORDS)

# =============================================================================
# APP
# =============================================================================

app = FastAPI(title="Vera API", version="1.0.0")


@app.get("/v1/healthz", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/v1/metadata", tags=["meta"])
def metadata():
    return {
        "team_name": "VeraElite",
        "model":     GROQ_MODEL,
    }


@app.post("/v1/context", tags=["context"])
def store_context(body: ContextBody):
    key = ctx_key(body.scope, body.context_id)
    _contexts[key] = {"version": body.version, "payload": body.payload}
    log.info("Context stored: %s", key)
    return {"accepted": True}


@app.post("/v1/tick", tags=["core"])
async def tick(body: TickBody):
    actions = []

    for trigger_id in body.available_triggers:
        trigger = get_ctx("trigger", trigger_id) or {}

        merchant_id = trigger.get("merchant_id", "default_merchant")

        merchant = get_ctx("merchant", merchant_id) or {
            "identity": {"name": "Business"},
            "category_slug": "general",
            "performance": {"ctr": 0.02},
            "offers": []
        }

        category_slug = merchant.get("category_slug", "general")

        category = get_ctx("category", category_slug) or {
            "slug": "general",
            "voice": {"tone": "neutral"},
            "peer_stats": {"avg_ctr": 0.03}
        }

        msg = await compose(category, merchant, trigger)

        actions.append({
            "conversation_id": f"{merchant_id}_{trigger_id}",
            "merchant_id":     merchant_id,
            "customer_id":     None,
            "send_as":         msg["send_as"],
            "trigger_id":      trigger_id,
            "body":            msg["body"],
            "cta":             msg["cta"],
            "suppression_key": msg["suppression_key"],
            "rationale":       msg["rationale"],
        })

    log.info("Tick processed — %d action(s) generated", len(actions))
    return {"actions": actions}


@app.post("/v1/reply", tags=["core"])
def reply(body: ReplyBody):
    conv    = body.conversation_id
    msg_raw = body.message.strip().lower()

    history = _conversations.setdefault(conv, [])
    history.append(msg_raw)

    if _is_auto_close(msg_raw, history):
        log.info("Conv %s — auto-close", conv)
        return {"action": "end"}

    if _is_yes(msg_raw):
        log.info("Conv %s — user said yes", conv)
        return {
            "action": "send",
            "body":   "Done 👍 I've set this up. Want me to optimize results further?",
            "cta":    "open_ended",
        }

    if _is_no(msg_raw):
        log.info("Conv %s — user said no", conv)
        return {"action": "end"}

    log.info("Conv %s — fallback reply", conv)
    return {
        "action": "send",
        "body":   "Got it 👍 I can improve your visibility quickly. Want to see how?",
        "cta":    "open_ended",
    }