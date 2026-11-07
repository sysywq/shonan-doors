"""Durable estimated-spend guard shared by all instrumented Python API jobs.

Reservations use GitHub's contents SHA compare-and-swap, including across runs.
The estimate is not an invoice cap: a server-tool response can exceed a reservation.
Owner-approved account ceiling is $200; the local guard leaves $10 headroom.
"""
import base64
import calendar
import copy
import json
import os
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
BRANCH = 'bot/claude-api-budget'
FILE = 'data/claude_budget.json'


class BudgetExceeded(RuntimeError):
    pass


class BudgetStoreError(RuntimeError):
    pass


def policy():
    return json.loads((ROOT / 'data/claude_budget_policy.json').read_text())


def reserve(state, settings, now, call_id, stage):
    """Pure transition, rerun on a CAS conflict. No paid call occurs on refusal."""
    state = copy.deepcopy(state)
    month = now.astimezone(timezone.utc).strftime('%Y-%m')
    day = now.astimezone(ZoneInfo('Asia/Tokyo')).date().isoformat()
    calls = state.setdefault('calls', {})
    if call_id in calls:
        return state
    monthly = sum(r['usd'] for r in calls.values() if r['month'] == month)
    monthly += settings.get('historical_spend_usd', {}).get(month, 0)
    daily = sum(r['usd'] for r in calls.values() if r['day'] == day)
    # Rebase pacing on actual remaining money when installed mid-month.
    openings = state.setdefault('month_openings', {})
    opening = openings.setdefault(month, {'day': now.day, 'usd': monthly})
    total_days = calendar.monthrange(now.year, now.month)[1]
    remaining_span = max(1, total_days - opening['day'] + 1)
    elapsed = max(1, now.day - opening['day'] + 1)
    allowance = opening['usd'] + max(0, settings['monthly_usd'] - opening['usd']) * elapsed / remaining_span
    # Minimum-publication calls may use the daily ceiling; only optional work
    # uses paced allowance and must protect the future minimum reserve.
    optional = os.environ.get('CLAUDE_BUDGET_PURPOSE') == 'optional'
    if not optional:
        allowance = settings['monthly_usd']
    protected_days = total_days - now.day + (1 if optional and os.environ.get('CLAUDE_BUDGET_MINIMUM_SECURED') != '1' else 0)
    if monthly + settings['reservation_usd'] + protected_days * settings.get('minimum_daily_reserve_usd', 0) > settings['monthly_usd']:
        raise BudgetExceeded('Future minimum-publication reserve protects remaining days')
    daily_limit = settings['daily_usd']
    if optional and os.environ.get('CLAUDE_BUDGET_MINIMUM_SECURED') != '1':
        daily_limit = max(0, daily_limit - settings.get('minimum_daily_reserve_usd', 3.5))
    hold = settings['reservation_usd']
    if monthly + hold > min(settings['monthly_usd'], allowance) or daily + hold > daily_limit:
        raise BudgetExceeded(f'Claude estimated budget exhausted: month=${monthly:.2f}, day=${daily:.2f}; defer until next budget window')
    calls[call_id] = dict(month=month, day=day, usd=hold, stage=stage, status='reserved')
    return state


def settle(state, call_id, usd):
    state = copy.deepcopy(state)
    if call_id not in state.get('calls', {}):
        raise BudgetStoreError('Missing reservation')
    if usd is not None:
        state['calls'][call_id].update(usd=float(usd), status='recorded')
    return state


class GitHubLedger:
    def __init__(self):
        self.repo = os.environ.get('GITHUB_REPOSITORY', '')
        self.token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
        if not self.repo or not self.token:
            raise BudgetStoreError('Budget ledger credentials unavailable; paid calls are disabled')

    def request(self, method, path, data=None):
        req = urllib.request.Request('https://api.github.com/repos/' + self.repo + path,
            data=json.dumps(data).encode() if data is not None else None, method=method,
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)

    def initialize(self):
        try:
            self.request('GET', '/git/ref/heads/' + BRANCH)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            sha = self.request('GET', '/git/ref/heads/main')['object']['sha']
            try:
                self.request('POST', '/git/refs', {'ref': 'refs/heads/' + BRANCH, 'sha': sha})
            except urllib.error.HTTPError as race:
                if race.code != 422:
                    raise

    def snapshot(self):
        try:
            doc = self.request('GET', '/contents/' + FILE + '?ref=' + BRANCH)
            return json.loads(base64.b64decode(doc['content']))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {'calls': {}}
            raise

    def update(self, transition):
        self.initialize()
        for _ in range(6):
            sha = None
            try:
                doc = self.request('GET', '/contents/' + FILE + '?ref=' + BRANCH)
                sha = doc['sha']
                state = json.loads(base64.b64decode(doc['content']))
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                state = {'version': 1, 'calls': {}}
            changed = transition(state)
            body = dict(message='Record Claude budget reservation', branch=BRANCH,
                        content=base64.b64encode(json.dumps(changed).encode()).decode())
            if sha:
                body['sha'] = sha
            try:
                self.request('PUT', '/contents/' + FILE, body)
                return
            except urllib.error.HTTPError as exc:
                if exc.code not in (409, 422):
                    raise
        raise BudgetStoreError('Concurrent ledger updates did not converge')


def begin(stage):
    if os.environ.get('CLAUDE_BUDGET_ENFORCE') == '0':
        return None
    if os.environ.get('GITHUB_ACTIONS') != 'true' and os.environ.get('CLAUDE_BUDGET_ENFORCE') != '1':
        return None
    ledger = GitHubLedger()
    ident = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    try:
        ledger.update(lambda s: reserve(s, policy(), now, ident, stage))
    except BudgetExceeded:
        raise
    except Exception as exc:
        raise BudgetStoreError('Cannot persist Claude budget reservation; paid call blocked') from exc
    return ledger, ident


def finish(ticket, row):
    if ticket:
        ledger, ident = ticket
        # On failure keep the reservation charged; never silently release unknown spend.
        ledger.update(lambda s: settle(s, ident, row.get('estimated_usd') if row else None))


def optional_article_available(now=None):
    """Fail closed on missing balance or stale/missing editorial observations."""
    if os.environ.get('CLAUDE_BUDGET_ENFORCE') == '0' or os.environ.get('DAILY_ADAPTIVE_TARGET') != '1':
        return False
    now = now or datetime.now(timezone.utc)
    settings = policy()
    try:
        state = GitHubLedger().snapshot()
        month = now.strftime('%Y-%m')
        spent = settings.get('historical_spend_usd', {}).get(month, 0) + sum(
            r['usd'] for r in state.get('calls', {}).values() if r['month'] == month)
        future = calendar.monthrange(now.year, now.month)[1] - now.day
        spare = settings['monthly_usd'] - spent - future * settings['minimum_daily_reserve_usd']
        return spare >= settings['optional_article_allowance_usd']
    except Exception:
        return False
