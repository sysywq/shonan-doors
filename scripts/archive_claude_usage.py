"""Merge Claude API usage JSONL files into a durable, deduplicated archive.

Usage: python scripts/archive_claude_usage.py --archive data/claude-cost/usage.jsonl artifacts/claude_usage.jsonl
Commit the archive file to a persistent Git branch; GitHub Actions artifacts alone expire.
"""
import argparse
import json
import os
from pathlib import Path


def identity(row):
    call_id = row.get("call_id")
    if call_id:
        return ("call", str(call_id))
    # Legacy records without a call_id cannot be safely deduplicated.
    return None


def load(path):
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{number}: expected JSON object")
        rows.append(value)
    return rows


def merge(existing, incoming):
    result = list(existing)
    seen = {key for row in existing if (key := identity(row)) is not None}
    for row in incoming:
        key = identity(row)
        if key is None:
            # Preserve legacy rows rather than silently merging distinct calls.
            result.append(row)
        elif key not in seen:
            seen.add(key)
            result.append(row)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()
    previous = load(args.archive)
    incoming = [row for path in args.inputs for row in load(path)]
    combined = merge(previous, incoming)
    args.archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.archive.with_suffix(args.archive.suffix + ".tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in combined), encoding="utf-8")
    os.replace(temporary, args.archive)
    print(json.dumps({"previous": len(previous), "incoming": len(incoming), "total": len(combined), "added": len(combined) - len(previous)}))


if __name__ == "__main__":
    main()
