"""Record Claude API usage without making additional API calls.

Call record_usage(response, stage=..., run_id=...) after each API response.
Writes JSON Lines to a local file; persistence across Actions runs must be
configured separately before production use.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

PRICES_PER_MILLION = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_create": 3.75},
}

def record_usage(response, *, stage, run_id=None, path=None):
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
    row = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "article_id": os.getenv("CLAUDE_COST_ARTICLE_ID") or None,
        "run_id": str(run_id or os.getenv("GITHUB_RUN_ID", "")),
        "model": model,
        "tokens": counts,
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

def metered_create(client, *, stage, **kwargs):
    """Call Claude once, then best-effort record the response usage."""
    response = client.messages.create(**kwargs)
    try:
        record_usage(response, stage=stage)
    except Exception:
        pass
    return response
