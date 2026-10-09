#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Articles の日次状態(終了条件 confirmed_today >= DAILY_MIN_ARTICLES)を run をまたいで引き継ぐ
------------------------------------------------------------------------
1日の処理は「その日の confirmed 記事(main の data/articles.json で date が当日の記事)が最低ライン
(DAILY_MIN_ARTICLES、既定3件)以上」になったときだけ Complete になる。届かない間は未完了として扱い、
1 run の上限(DAILY_TOPUP_MAX_ATTEMPTS / MAX_GATE_DRAFTS_PER_RUN 等)に達したら、不足分だけを対象にした
次の補充 run(round)を daily-articles.yml の workflow_dispatch で自動起動する。

  round 0 … 通常の当日 run(branch daily/YYYY-MM-DD)
  round n … 補充 run(branch daily/YYYY-MM-DD-rN)。不足分(最低ライン − confirmed_today)だけを生成・監査する

状態はデータbranch(既定 bot/daily-state)の data/daily_state.json に保存する(main へは push しない)。
  - rounds    … round ごとの branch・公開/保留の記事ID・補充 run を起動済みか・SNSへ渡したか
  - rejected  … 同じ日に公開前監査で不合格・Fact Audit で保留・企画で見送った対象(次の round で再生成しない)
  - status    … incomplete / complete / system_failure / round_limit
  - failure   … 停止理由(システム障害・round 上限)

止める条件(明示的に failure + Issue で通知。候補を変えても解決しないもの):
  - システム障害 … Anthropic API の認証/権限/課金/レート制限/5xx/接続障害、GitHub API の障害
  - round 上限   … DAILY_MAX_REFILL_ROUNDS(暴走防止。1日の run 数の上限)
止めた日は、bot(watchdog・補充 run)が起動した run では生成しない。オーナーが手動で
workflow_dispatch した run だけが停止を解除して続きから再開する。

Fact Audit・一次情報・重複の基準はこのモジュールでは一切扱わない(件数と状態の管理だけ)。
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
CONTEXT_PATH = os.environ.get("SHONAN_DOORS_DAILY_STATE_PATH", os.path.join("/tmp", "shonan_doors_daily_state.json"))
ERRORS_PATH = os.environ.get("SHONAN_DOORS_SYSTEM_ERRORS_PATH", os.path.join("/tmp", "shonan_doors_system_errors.json"))
STATE_NAME = "daily_state.json"
STATE_REL = f"data/{STATE_NAME}"
BRANCH = os.environ.get("DAILY_STATE_BRANCH", "bot/daily-state")
REMOTE = os.environ.get("DAILY_STATE_REMOTE", "origin")
PUSH_ATTEMPTS = 3
KEEP_DAYS = 30
DAILY_MIN_ARTICLES = int(os.environ.get("DAILY_MIN_ARTICLES", "3"))
# 補充 run の上限(round 0 を除く)。1 run の上限と合わせて、1日の API 費用と Actions の実行回数を有限にする
DAILY_MAX_REFILL_ROUNDS = int(os.environ.get("DAILY_MAX_REFILL_ROUNDS", "4"))

COMPLETE, INCOMPLETE, SYSTEM_FAILURE, ROUND_LIMIT = "complete", "incomplete", "system_failure", "round_limit"
STOPPED = (SYSTEM_FAILURE, ROUND_LIMIT)


class StoreError(Exception):
    pass


def now_jst():
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M")


def new_state(date):
    return {"date": date, "status": INCOMPLETE, "rounds": {}, "rejected": [], "failure": None,
            "confirmed_today": 0, "updated_at": ""}


def normalize_state(state, date):
    s = dict(new_state(date), **(state or {}))
    s["rounds"] = {str(k): dict(v) for k, v in (s.get("rounds") or {}).items() if isinstance(v, dict)}
    s["rejected"] = [r for r in s.get("rejected") or [] if isinstance(r, dict) and r.get("title")]
    return s


# ---------- 終了条件と次の行動(純粋関数) ----------

def today_ids(articles, date):
    """その日の confirmed 記事(公開済み=main の articles.json にある当日の記事。統合済みは除く)のID。"""
    return [a.get("id") for a in articles or [] if a.get("date") == date and not a.get("mergedInto")]


def run_quota(confirmed_today, round_no, target, minimum=None):
    """この run で必要な件数 (最低, 目標)。
    round 0 は従来どおり最低3件・目標5件(当日の既存分を差し引く)。補充 round は不足分だけを目標にする。"""
    minimum = DAILY_MIN_ARTICLES if minimum is None else minimum
    need = max(0, minimum - confirmed_today)
    if round_no and round_no > 0:
        return need, need
    return need, max(need, target - confirmed_today)


def next_action(confirmed_today, round_no, system_errors=(), max_rounds=None, minimum=None):
    """1 run の終わりに、その日をどうするかを決める。
    戻り値: complete(終了条件を満たした)/ system_failure(候補を変えても解決しない障害。停止)
            / round_limit(補充 run の上限。停止)/ refill(不足分の補充 run を起動する)"""
    minimum = DAILY_MIN_ARTICLES if minimum is None else minimum
    max_rounds = DAILY_MAX_REFILL_ROUNDS if max_rounds is None else max_rounds
    if confirmed_today >= minimum:
        return COMPLETE
    if system_errors:
        return SYSTEM_FAILURE
    if round_no >= max_rounds:
        return ROUND_LIMIT
    return "refill"


# ---------- 同じ日に見送った対象(次の round で再生成しない) ----------

def compact(entry, reason, round_no):
    """重複チェック(find_same_subject / is_duplicate / candidate_pool)に必要な項目だけを残す。"""
    e = entry or {}
    return {"title": e.get("title") or e.get("titleIdea") or "", "dek": e.get("dek", ""),
            "area": e.get("area", ""), "cat": e.get("cat", ""), "link": e.get("link") or e.get("sourceUrl") or "",
            "subjectNames": list(e.get("subjectNames") or ([e["subject"]] if e.get("subject") else [])),
            "tags": list(e.get("tags") or []), "articleType": e.get("articleType", ""),
            "topicId": e.get("_topicId") or e.get("stockTopicId") or "", "date": e.get("date", ""),
            "eventSeriesKey": e.get("eventSeriesKey", ""),
            "reason": reason, "round": round_no}


def _rejected_key(r):
    return (r.get("title", ""), r.get("link", ""), r.get("topicId", ""))


def add_rejected(state, entries):
    seen = {_rejected_key(r) for r in state["rejected"]}
    for r in entries:
        if r.get("title") and _rejected_key(r) not in seen:
            seen.add(_rejected_key(r))
            state["rejected"].append(r)
    return state


def rejected_from_run(report, holds, round_no):
    """実行レポート・保留記事から、この run で見送った対象を集める。"""
    out = []
    for r in report.get("gate_rejected") or []:
        if isinstance(r, dict):
            out.append(compact(dict(r.get("draft") or {}, **{k: r[k] for k in ("title", "area", "cat", "link")
                                                              if r.get(k)}), "公開前監査で不合格", round_no))
    for h in holds or []:
        if isinstance(h, dict):
            out.append(compact(dict(h.get("entry") or h, stockTopicId=h.get("stockTopicId") or ""),
                               f"Fact Audit で {h.get('verdict')}(保留)", round_no))
    for r in report.get("freshness_rejected") or []:
        if isinstance(r, dict):
            out.append(compact(r, f"ニュース鮮度ゲートで対象外({r.get('reason', '')})", round_no))
    titles = {r["title"] for r in out}
    for a in ((report.get("planning") or {}).get("attempts") or []):
        # 採用した企画(selected)は公開対象か保留記事として上で扱う
        if isinstance(a, dict) and a.get("titleIdea") and not a.get("selected") and a["titleIdea"] not in titles:
            out.append(compact(a, "企画で試したが採用されなかった", round_no))
    return [r for r in out if r.get("title")]


def avoid_articles(state):
    """見送った対象を、重複チェック用の記事(id='rejected-today')として返す。articles.json には書かない。"""
    return [dict(r, id="rejected-today") for r in (state or {}).get("rejected") or [] if r.get("title")]


def rejected_topic_ids(state):
    return {r["topicId"] for r in (state or {}).get("rejected") or [] if r.get("topicId")}


# ---------- run コンテキスト(plan が書き、generate / fact audit / record が読む) ----------

def load_context(path=None):
    try:
        with open(path or CONTEXT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_context(ctx, path=None):
    with open(path or CONTEXT_PATH, "w", encoding="utf-8") as f:
        json.dump(ctx, f, ensure_ascii=False, indent=1)


# ---------- システム障害(候補を変えても解決しない障害)の判定 ----------

SYSTEM_ERROR_NAMES = ("AuthenticationError", "PermissionDeniedError", "RateLimitError", "InternalServerError",
                      "APIConnectionError", "APITimeoutError", "OverloadedError", "ServiceUnavailableError")
SYSTEM_ERROR_RE = re.compile(
    r"\b(" + "|".join(SYSTEM_ERROR_NAMES) + r")\b"
    r"|authentication_error|permission_error|rate_limit_error|overloaded_error|billing_error"
    r"|credit balance is too low|insufficient[_ ]quota|invalid x-api-key", re.I)
SYSTEM_STATUS = {401, 402, 403, 429, 500, 502, 503, 504, 529}


def system_error_reason(exc):
    """Anthropic API の例外がシステム障害なら理由を返す(記事・候補ごとの問題なら None)。
    公式サイト取得の HTTPError 等は候補ごとの問題なので対象外(Anthropic SDK の例外と文言だけを見る)。"""
    if exc is None:
        return None
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if name in SYSTEM_ERROR_NAMES or (status in SYSTEM_STATUS and type(exc).__module__.startswith("anthropic")):
        return f"{name}: {str(exc)[:200]}"
    return system_error_in_text(f"{name}: {exc}")


def system_error_in_text(text):
    m = SYSTEM_ERROR_RE.search(text or "")
    return (text or "")[:300] if m else None


def github_error_reason(exc):
    """GitHub API 呼び出しの例外がシステム障害(認証/権限/レート制限/5xx/接続)なら理由を返す。"""
    import urllib.error
    if isinstance(exc, urllib.error.HTTPError):
        return f"GitHub API HTTP {exc.code}" if exc.code in SYSTEM_STATUS else None
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return f"GitHub API 接続障害: {type(exc).__name__}: {exc}"
    return None


def note_system_error(exc, where, path=None):
    """候補ごとの例外を握りつぶす箇所から呼ぶ。システム障害なら記録して True を返す。"""
    reason = system_error_reason(exc)
    if not reason:
        return False
    errors = load_system_errors(path)
    errors.append({"where": where, "reason": reason, "at": now_jst()})
    try:
        with open(path or ERRORS_PATH, "w", encoding="utf-8") as f:
            json.dump(errors[-50:], f, ensure_ascii=False, indent=1)
    except OSError as e:
        print(f"警告: システム障害の記録に失敗しました: {e}", file=sys.stderr)
    print(f"::error::システム障害を検出しました({where}): {reason}", file=sys.stderr)
    return True


def load_system_errors(path=None):
    try:
        with open(path or ERRORS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def system_errors_in_run(report, holds, path=None):
    """この run のシステム障害。記録ファイルに加え、監査できなかった記事の理由文(監査処理で例外: …)も見る。"""
    errors = list(load_system_errors(path))
    for r in list(report.get("gate_rejected") or []) + list(holds or []):
        if not isinstance(r, dict):
            continue
        text = " ".join([str(r.get("summary", ""))] + [str(x) for x in r.get("reasons") or []])
        reason = system_error_in_text(text) if "例外" in text else None
        if reason:
            errors.append({"where": f"監査: {r.get('title', '')}", "reason": reason})
    return errors


# ---------- 永続化(データbranch) ----------

class MemoryStore:
    """テスト・ローカル実行用(永続化しない)。"""
    def __init__(self, days=None):
        self.days = {k: dict(v) for k, v in (days or {}).items()}
        self.saved = []

    def load(self, date):
        return normalize_state(self.days.get(date), date)

    def save(self, state):
        self.days[state["date"]] = json.loads(json.dumps(state))
        self.saved.append(self.days[state["date"]])


def _git(*args, input_text=None, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, input=input_text, capture_output=True, text=True, check=check)


class GitStore:
    """データbranch(bot/daily-state)に data/daily_state.json だけを置く。作業ツリー・現在の branch は変えない。"""
    def __init__(self, git=_git):
        self.git = git

    def _fetch(self):
        r = self.git("ls-remote", "--exit-code", "--heads", REMOTE, f"refs/heads/{BRANCH}", check=False)
        if r.returncode == 2:
            return None, {"days": {}}
        if r.returncode != 0:
            raise StoreError(f"データbranch {BRANCH} の有無を確認できません: {r.stderr.strip()}")
        r = self.git("fetch", "--no-tags", REMOTE, f"+refs/heads/{BRANCH}:refs/remotes/{REMOTE}/{BRANCH}",
                     check=False)
        if r.returncode != 0:
            raise StoreError(f"データbranch {BRANCH} を取得できません: {r.stderr.strip()}")
        sha = self.git("rev-parse", f"refs/remotes/{REMOTE}/{BRANCH}").stdout.strip()
        shown = self.git("show", f"{sha}:{STATE_REL}", check=False)
        try:
            data = json.loads(shown.stdout) if shown.returncode == 0 else {"days": {}}
        except ValueError as e:
            raise StoreError(f"データbranch {BRANCH} の {STATE_REL} を解析できません: {e}")
        if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
            raise StoreError(f"データbranch {BRANCH} の {STATE_REL} の形式が不正です")
        return sha, data

    def load(self, date):
        _sha, data = self._fetch()
        return normalize_state(data["days"].get(date), date)

    def save(self, state):
        for attempt in range(1, PUSH_ATTEMPTS + 1):
            parent, data = self._fetch()
            days = dict(data["days"])
            remote = normalize_state(days.get(state["date"]), state["date"])
            merged = add_rejected(json.loads(json.dumps(state)), remote["rejected"])
            days[state["date"]] = merged
            days = {k: days[k] for k in sorted(days)[-KEEP_DAYS:]}
            content = json.dumps({"days": days}, ensure_ascii=False, indent=1)
            blob = self.git("hash-object", "-w", "--stdin", input_text=content).stdout.strip()
            data_tree = self.git("mktree", input_text=f"100644 blob {blob}\t{STATE_NAME}\n").stdout.strip()
            root_tree = self.git("mktree", input_text=f"040000 tree {data_tree}\tdata\n").stdout.strip()
            args = ["-c", "user.name=shonan-doors-bot", "-c", "user.email=bot@users.noreply.github.com",
                    "commit-tree", root_tree, "-m", f"chore: Daily Articles の日次状態を更新 ({state['date']})"]
            if parent:
                args += ["-p", parent]
            commit = self.git(*args).stdout.strip()
            r = self.git("push", REMOTE, f"{commit}:refs/heads/{BRANCH}", check=False)
            if r.returncode == 0:
                return
            print(f"日次状態の push に失敗しました({attempt}/{PUSH_ATTEMPTS}): {r.stderr.strip()}", file=sys.stderr)
        raise StoreError(f"日次状態をデータbranch {BRANCH} に保存できませんでした")


def store_from_env():
    return MemoryStore() if os.environ.get("DAILY_STATE_STORE") == "memory" else GitStore()


def mark_system_failure_from_crash(exc, where, store=None):
    """generate_articles.py / daily_fact_audit.py が例外で止まったとき、システム障害なら日次状態を停止にする
    (以後 bot が起動した run は生成しない)。保存できなくても元の例外で失敗させる。"""
    reason = system_error_reason(exc)
    if not reason:
        return False
    note_system_error(exc, where)
    ctx = load_context()
    date = ctx.get("date")
    if not date:  # Daily Articles の plan を経ていない実行(手動の補充など)では日次状態を変えない
        return True
    try:
        store = store or store_from_env()
        state = store.load(date)
        state.update(status=SYSTEM_FAILURE, updated_at=now_jst(),
                     failure={"kind": SYSTEM_FAILURE, "reason": f"{where}: {reason}", "at": now_jst(),
                              "run_id": os.environ.get("GITHUB_RUN_ID", ""), "notified": False})
        store.save(state)
    except Exception as e:  # 状態を保存できなくても、run 自体は失敗で終わる
        print(f"::warning::日次状態を保存できませんでした: {e}", file=sys.stderr)
    return True
