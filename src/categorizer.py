"""
Email categorization using rules and AI.

AI calls are structured: the model fills a JSON schema in which every verdict
carries the id of the sender/email it belongs to, so a skipped or reordered
item cannot shift verdicts onto the wrong sender. One request builder serves
both the synchronous path and the Message Batches path (50% cheaper, async).

Failures raise AIError. Callers never record a verdict they did not get.
"""

import json
from typing import Optional, Tuple, List, Dict, Any

from .config import CATEGORIES, CLAUDE_API_KEY, CLAUDE_MODEL
from .imap_client import extract_domain


AI_MODEL = CLAUDE_MODEL
BATCH_SIZE = 10          # emails per request (per-email auto-review)
SENDER_BATCH_SIZE = 15   # senders per request
MAX_TOKENS = 8000

Verdict = Tuple[str, bool, str]   # (category, keep, reason)


class AIError(Exception):
    """The API call failed or returned nothing usable; no verdicts were produced."""


def get_anthropic_client():
    """Get Anthropic client using API key from config."""
    import anthropic
    return anthropic.Anthropic(api_key=CLAUDE_API_KEY)


# ── Rule-based ───────────────────────────────────────────────────────────────

def auto_categorize(sender: str, subject: str, body_preview: str = "") -> str:
    """
    Automatically categorize an email based on sender domain and keywords.
    Returns the category name.
    """
    domain = extract_domain(sender)
    text = f"{subject} {body_preview}".lower()

    # Check domain matches first (most reliable)
    for cat_name, cat_info in CATEGORIES.items():
        if cat_name == "other":
            continue
        for cat_domain in cat_info["domains"]:
            if cat_domain in domain:
                return cat_name

    # Check keyword matches
    for cat_name, cat_info in CATEGORIES.items():
        if cat_name == "other":
            continue
        for keyword in cat_info["keywords"]:
            if keyword.lower() in text:
                return cat_name

    return "other"


# ── Shared prompt / schema ───────────────────────────────────────────────────

_CATEGORY_LIST = "\n".join(f"- {name}: {info['description']}" for name, info in CATEGORIES.items())

KEEP_POLICY = f"""The mailbox owner wants to KEEP only mail that holds information they might need to look up later, and DELETE everything else.

KEEP (keep=true): mail from real people (friends, family, colleagues, recruiters writing personally); receipts, invoices, order and booking confirmations; bank, card, broker, insurance, tax and payroll mail; travel tickets and itineraries; government, legal, medical, school and landlord correspondence; account documents such as contracts, policies and statements.

DELETE (keep=false): marketing and promotional blasts; newsletters and digests; social-network and community notifications (LinkedIn, Meetup, Facebook, forums); product-update and engagement nudges; one-time codes, login alerts and "verify your email" messages; delivery-status pings; cold outreach and spam; event promotion; anything a no-reply address sends that has no lasting data.

When the same source sends both kinds (a shop that sends receipts AND promotions from one address), KEEP. When unsure, KEEP.

Categories (assign exactly one):
{_CATEGORY_LIST}"""

SENDER_SYSTEM = (
    "You help a person clean a personal mailbox of several hundred thousand emails. "
    "You judge SENDERS, not individual emails: one verdict applies to every email from that address.\n\n"
    + KEEP_POLICY +
    "\n\nEach sender comes with its address, number of emails, the share of them carrying a "
    "List-Unsubscribe header (a bulk-mail marker), the date range, and its most recent subjects. "
    "Give a reason of under 12 words."
)

EMAIL_SYSTEM = (
    "You help a person clean a personal mailbox. You judge individual emails.\n\n"
    + KEEP_POLICY +
    "\n\nGive a reason of under 12 words."
)

VERDICT_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "description": "The item number (the N in SENDER_N / EMAIL_N)."},
        "category": {"type": "string", "enum": list(CATEGORIES.keys())},
        "keep": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["id", "category", "keep", "reason"],
    "additionalProperties": False,
}

VERDICTS_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {"type": "array", "items": VERDICT_ITEM_SCHEMA},
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


def _output_config() -> Dict[str, Any]:
    cfg: Dict[str, Any] = {"format": {"type": "json_schema", "schema": VERDICTS_SCHEMA}}
    # Classification needs little reasoning. `effort` is rejected by Haiku 4.5
    # and Sonnet 4.5, so only send it to models that take it.
    if "haiku" not in AI_MODEL and "sonnet-4-5" not in AI_MODEL:
        cfg["effort"] = "low"
    return cfg


def build_sender_request(senders: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Messages API params classifying one batch of senders, ids 1..n in input order.

    Each sender dict: email, count, bulk_ratio, first_date, last_date, sample_subjects.
    """
    lines = ["Classify these senders. Return one verdict per sender; id = the SENDER number.", ""]
    for i, s in enumerate(senders, 1):
        lines.append(f"SENDER_{i}: {s.get('email', 'unknown')}")
        lines.append(
            f"  emails: {s.get('count', 0)}   bulk-marker share: {float(s.get('bulk_ratio') or 0):.0%}"
            f"   dates: {s.get('first_date') or '?'} → {s.get('last_date') or '?'}"
        )
        subs = list(s.get("sample_subjects") or [])
        for sub in reversed(subs[-3:]):   # newest first
            lines.append(f"  - {str(sub)[:120]}")
    return {
        "model": AI_MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SENDER_SYSTEM,
        "messages": [{"role": "user", "content": "\n".join(lines)}],
        "output_config": _output_config(),
    }


def build_email_request(emails: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Messages API params classifying one batch of emails, ids 1..n in input order.

    Each email dict: sender, subject, date, preview.
    """
    lines = ["Classify these emails. Return one verdict per email; id = the EMAIL number.", ""]
    for i, e in enumerate(emails, 1):
        lines.append(f"EMAIL_{i}:")
        lines.append(f"  From: {e.get('sender', '')}")
        lines.append(f"  Subject: {e.get('subject', '')}")
        lines.append(f"  Date: {e.get('date', '')}")
        if e.get("preview"):
            lines.append(f"  Preview: {str(e['preview'])[:200]}")
    return {
        "model": AI_MODEL,
        "max_tokens": MAX_TOKENS,
        "system": EMAIL_SYSTEM,
        "messages": [{"role": "user", "content": "\n".join(lines)}],
        "output_config": _output_config(),
    }


def parse_verdicts(message, expected: int) -> Dict[int, Verdict]:
    """Map id -> (category, keep, reason) from a structured response.

    Ids outside 1..expected are dropped; missing ids are simply absent, so the
    caller leaves those items unanalyzed rather than guessing.
    """
    if message.stop_reason == "refusal":
        detail = getattr(getattr(message, "stop_details", None), "explanation", "") or ""
        raise AIError(f"model refused the request {detail}".strip())
    text = next((b.text for b in message.content if b.type == "text"), "")
    if message.stop_reason == "max_tokens":
        raise AIError("response truncated at max_tokens")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise AIError(f"unparseable response: {e}")

    out: Dict[int, Verdict] = {}
    for v in data.get("verdicts", []) if isinstance(data, dict) else []:
        try:
            i = int(v["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (1 <= i <= expected):
            continue
        category = v.get("category")
        if category not in CATEGORIES:
            category = "other"
        out[i] = (category, bool(v.get("keep", True)), str(v.get("reason", ""))[:200])
    return out


def _call(params: Dict[str, Any]):
    import anthropic
    try:
        client = get_anthropic_client()
    except ImportError:
        raise AIError("the 'anthropic' package is not installed: pip install -r requirements.txt")
    try:
        return client.messages.create(**params)
    except anthropic.NotFoundError:
        raise AIError(f"model {AI_MODEL!r} not found — set CLAUDE_MODEL in .secrets to an available model")
    except anthropic.AuthenticationError:
        raise AIError("invalid CLAUDE_API_KEY")
    except anthropic.RateLimitError as e:
        raise AIError(f"rate limited: {e.message}")
    except anthropic.APIStatusError as e:
        raise AIError(f"API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError as e:
        raise AIError(f"network error: {e}")


# ── Synchronous ──────────────────────────────────────────────────────────────

def ai_batch_analyze_senders(senders: List[Dict[str, Any]]) -> Dict[int, Verdict]:
    """Classify a batch of senders now. Returns {1-based id: (category, keep, reason)}."""
    if not senders:
        return {}
    return parse_verdicts(_call(build_sender_request(senders)), len(senders))


def ai_batch_analyze(emails: List[Dict[str, Any]]) -> Dict[int, Verdict]:
    """Classify a batch of emails now. Returns {1-based id: (category, keep, reason)}."""
    if not emails:
        return {}
    return parse_verdicts(_call(build_email_request(emails)), len(emails))


def ai_categorize(subject: str, sender: str, preview: str) -> Verdict:
    """Classify one email. Raises AIError on failure."""
    verdicts = ai_batch_analyze([{"sender": sender, "subject": subject, "date": "", "preview": preview}])
    if 1 not in verdicts:
        raise AIError("no verdict returned")
    return verdicts[1]


# ── Message Batches (async, 50% cost) ────────────────────────────────────────

def submit_sender_batch(groups: Dict[str, List[Dict[str, Any]]]) -> str:
    """Submit one Message Batches job; `groups` maps custom_id -> senders. Returns the batch id."""
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    client = get_anthropic_client()
    requests = [
        Request(custom_id=cid, params=MessageCreateParamsNonStreaming(**build_sender_request(senders)))
        for cid, senders in groups.items()
    ]
    try:
        batch = client.messages.batches.create(requests=requests)
    except anthropic.APIStatusError as e:
        raise AIError(f"batch submit failed ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError as e:
        raise AIError(f"network error: {e}")
    return batch.id


def batch_status(batch_id: str) -> Tuple[str, Dict[str, int]]:
    """(processing_status, request counts) for a submitted batch."""
    import anthropic
    client = get_anthropic_client()
    try:
        b = client.messages.batches.retrieve(batch_id)
    except anthropic.APIStatusError as e:
        raise AIError(f"batch lookup failed ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError as e:
        raise AIError(f"network error: {e}")
    c = b.request_counts
    return b.processing_status, {
        "processing": c.processing, "succeeded": c.succeeded,
        "errored": c.errored, "canceled": c.canceled, "expired": c.expired,
    }


def collect_sender_batch(batch_id: str, sizes: Dict[str, int]) -> Tuple[Dict[str, Dict[int, Verdict]], Dict[str, str]]:
    """Fetch results of an ended batch.

    `sizes` maps custom_id -> number of senders in that request.
    Returns (verdicts by custom_id, error text by custom_id). Results arrive in
    any order; everything is keyed by custom_id, never by position.
    """
    import anthropic
    client = get_anthropic_client()
    verdicts: Dict[str, Dict[int, Verdict]] = {}
    errors: Dict[str, str] = {}
    try:
        for r in client.messages.batches.results(batch_id):
            cid = r.custom_id
            kind = r.result.type
            if kind == "succeeded":
                try:
                    verdicts[cid] = parse_verdicts(r.result.message, sizes.get(cid, 0))
                except AIError as e:
                    errors[cid] = str(e)
            elif kind == "errored":
                err = r.result.error
                errors[cid] = f"{getattr(err, 'type', 'error')}: {getattr(err, 'message', '')}"
            else:
                errors[cid] = kind
    except anthropic.APIStatusError as e:
        raise AIError(f"batch results failed ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError as e:
        raise AIError(f"network error: {e}")
    return verdicts, errors
