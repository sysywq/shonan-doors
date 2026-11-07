"""Record Claude API usage without making additional API calls.

Call record_usage(response, stage=..., run_id=...) after each API response.
Writes JSON Lines to a local file; persistence across Actions runs must be
configured separately before production use.
"""
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

PRICES_PER_MILLION = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_create": 3.75},
}

def record_usage(response, *, stage, run_id=None, path=None, article_id=None):
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    model = getattr(response, "model", "unknown")
    counts = {
        "input": int(getattr(usage, "input_tokens", 0) or 0),
        "output": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        "cache_create": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    }
    price = PRICES_PER_MILLION.get(model)
    estimated_usd = (sum(counts[k] * price[k] for k in counts) / 1_000_000) if price else None
    server = getattr(usage, "server_tool_use", None)
    searches = int(getattr(server, "web_search_requests", 0) or 0)
    search_usd = searches * 0.01
    if estimated_usd is not None:
        estimated_usd += search_usd
    row = {
        "call_id": str(uuid.uuid4()),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "article_id": article_id,
        "run_id": str(run_id or os.getenv("GITHUB_RUN_ID", "")),
        "model": model,
        "tokens": counts,
        "web_search_requests": searches,
        "web_search_usd": search_usd,
        "estimated_usd": estimated_usd,
        "pricing_status": "estimated" if price else "unknown_model",
    }
    destination = Path(path or os.getenv("CLAUDE_USAGE_LOG", "artifacts/claude_usage.jsonl"))
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass  # Metering must never block publishing.
    return row

def metered_create(client, *, stage, article_id=None, **kwargs):
    """Cache repeated context, call Claude once, and record response usage.

    Automatic caching advances the breakpoint as pause_turn conversations grow,
    including server-side search results. It changes billing, not prompt content.
    An explicit caller setting (including None to opt out) takes precedence.
    """
    from scripts.claude_budget import begin, finish
    ticket = begin(stage)
    if ticket:
        kwargs["max_tokens"] = min(kwargs.get("max_tokens", 6000), 6000)
        kwargs["tools"] = [dict(t, max_uses=min(t.get("max_uses", 3), 3))
                           if t.get("name") in ("web_search", "web_fetch") else t
                           for t in kwargs.get("tools", [])]
    kwargs.setdefault("cache_control", {"type": "ephemeral"})
    response = client.messages.create(**kwargs)
    try:
        row = record_usage(response, stage=stage, article_id=article_id)
    except Exception:
        if ticket:
            raise  # Keep reservation and stop if accounting fails in production.
        row = None
    finish(ticket, row)
    return response
