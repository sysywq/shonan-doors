"""Reuse a full audit for identical text within the same daily run only."""
import hashlib
import json
import os
from pathlib import Path


def fingerprint(entry):
    # Only identifiers assigned AFTER auditing are omitted; every factual field is hashed.
    value = {k: v for k, v in entry.items() if k not in ('id', 'slug', '_topicId')}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def location(entry):
    return Path('artifacts/full-audit-receipts') / (fingerprint(entry) + '.json')


def save(entry, result):
    if os.environ.get('DAILY_REUSE_FULL_AUDIT') != '1':
        return
    import fact_audit as fa
    import publish_gate as pg
    if not pg.evaluate(result)[0]:
        return
    path = location(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'run': os.environ.get('GITHUB_RUN_ID', 'local'),
        'policy': hashlib.sha256(fa.SYSTEM_PROMPT.encode()).hexdigest(), 'result': result}, ensure_ascii=False))


def load(entry):
    if os.environ.get('DAILY_REUSE_FULL_AUDIT') != '1':
        return None
    import fact_audit as fa
    import publish_gate as pg
    try:
        receipt = json.loads(location(entry).read_text())
        if receipt['run'] != os.environ.get('GITHUB_RUN_ID', 'local'):
            return None
        if receipt['policy'] != hashlib.sha256(fa.SYSTEM_PROMPT.encode()).hexdigest():
            return None
        result = receipt['result']
        if not pg.evaluate(result)[0]:
            return None
        return dict(result, id=entry.get('id'))
    except (OSError, ValueError, KeyError, TypeError):
        return None
