#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
湘南Doors 既存記事の一次情報照合監査(読み取り専用。articles.json は一切変更しない)
------------------------------------------------------------------------
各記事の本文から「事実の記述」(日付・場所・住所・営業時間・価格・席数・数値・
経歴・設備・立地・人物の発言など)を抜き出し、一次情報(店舗・企業・団体・主催者・
自治体の公式サイト/公式SNS/本人のプレスリリース)で確認できるかを1件ずつ判定する。

他メディア(新聞・ニュースサイト・地域まとめメディア・ブログ・口コミサイト等)は
web_search / web_fetch の blocked_domains で機械的に遮断しているため、
他メディアの記述を根拠に「確認済み」と判定されることはない。

使い方:
  python fact_audit.py                 # 全記事
  python fact_audit.py --ids 65,74     # 指定IDのみ
  python fact_audit.py --start 1 --end 40   # ID範囲(分割実行用)

出力:
  audit_reports/fact_audit_<日時>.jsonl … 1記事ごとに追記する途中経過(クラッシュしても残る)
  audit_reports/fact_audit_<日時>.json  … 記事ごとの判定詳細
  audit_reports/fact_audit_<日時>.md    … 要修正記事の一覧(人が読む用)

判定: confirmed(問題なし) / fix / rewrite / review_required(応答が想定外の形式・
例外などで自動判定できず、人の確認が必要)

モード:
  full   (既定) 新規記事・初回監査。記事全体から claim を抽出して一次情報と照合する。
  verify 修正後の確認。前回 contradicted の claim だけを再判定する(記事全文は送らない)。
         python fact_audit.py --mode verify --ids 1,2,3 --previous previous_reports/
         詳細は fact_verify.py を参照。

再開:
  python fact_audit.py --start 1 --end 40 --resume audit_reports/
  … 過去結果で confirmed/fix/rewrite 済みの記事は再監査せず引き継ぎ、
    review_required と未処理の記事だけを監査する
"""
import argparse
import json
import re
import os
import sys
import time
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

# generate_articles.py の定数(他メディアのドメイン一覧)を共有する。
# generate_articles.py 自体は anthropic を import するだけなので、そのまま読み込める。
import generate_articles as g

MODEL = os.environ.get("FACT_AUDIT_MODEL", "claude-sonnet-4-6")
MAX_TURNS = 12
OUT_DIR = os.path.join(g.ROOT, "audit_reports")

# 判定ルールのバージョン。判定基準を変えたら上げる。
# --resume は同じバージョンの結果だけを引き継ぐ(旧基準の判定を持ち越さないため)。
RULES_VERSION = 2

CLAIM_STATUSES = (
    "confirmed",             # 一次情報・公的情報・一般に確立した事実・単純計算で確認できた
    "wording_difference",    # 表現や概数の違いはあるが、一次情報と両立する
    "not_found_in_primary",  # 情報源は確認できたが、その記述は見つからなかった(未確認・要確認)
    "source_unavailable",    # 情報源そのものを確認できなかった(JS依存・アクセス制限・取得エラー等)
    "contradicted",          # 一次情報と明確に両立しない
)
CLAIM_ROLES = ("title", "dek", "central", "detail")  # 記事の骨格=title/dek/central
CORE_ROLES = ("title", "dek", "central")

AUDIT_TOOL = {
    "name": "submit_audit",
    "description": "1記事分の一次情報照合結果を提出する。",
    "input_schema": {
        "type": "object",
        "properties": {
            "primarySources": {
                "type": "array", "items": {"type": "string"},
                "description": "実際に開いて確認した一次情報のURL",
            },
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string", "description": "記事中の事実の記述(要約)"},
                        "role": {
                            "type": "string", "enum": list(CLAIM_ROLES),
                            "description": "title=タイトルの主張 / dek=リードの主張 / central=記事の中心となる主張 / detail=それ以外の細部",
                        },
                        "status": {"type": "string", "enum": list(CLAIM_STATUSES)},
                        "basis": {
                            "type": "string",
                            "enum": ["primary_page", "official_record", "established_fact", "calculation", "none"],
                            "description": "confirmed/wording_difference の根拠の種類。primary_page=対象の公式ページ / official_record=自治体・公的機関・運営会社などの公式情報 / established_fact=一般に確立した事実 / calculation=単純計算 / none=それ以外",
                        },
                        "articleValue": {"type": "string", "description": "記事側の値・表現(例: 駐車場800台規模)"},
                        "primaryValue": {"type": "string", "description": "一次情報側の値・表現(例: 826台)。無ければ空文字"},
                        "logicallyCompatible": {
                            "type": "boolean",
                            "description": "記事の記述と一次情報の記述が、論理的に両立しうるか(概数・言い換え・包含関係なら true)",
                        },
                        "primaryUrl": {"type": "string", "description": "確認に使った一次情報のURL。無ければ空文字"},
                        "note": {"type": "string", "description": "補足(食い違いの内容、取得できなかった理由など)"},
                    },
                    "required": ["claim", "role", "status", "basis", "articleValue", "primaryValue",
                                 "logicallyCompatible", "primaryUrl", "note"],
                },
            },
            "hasQuotedComment": {
                "type": "boolean",
                "description": "他メディアの取材コメントと思われる人物の発言を使っているか",
            },
            "needsHuman": {
                "type": "boolean",
                "description": "一次情報同士が食い違う、AIでは判断が難しいなど、人の確認が必要な事情があるか",
            },
            "verdict": {
                "type": "string", "enum": ["confirmed", "fix", "rewrite", "review_required"],
                "description": "参考として提出する記事単位の判定(最終判定はシステムが判定基準に沿って決める)",
            },
            "summary": {"type": "string", "description": "判定理由を1〜2文で"},
        },
        "required": ["primarySources", "claims", "hasQuotedComment", "needsHuman", "verdict", "summary"],
    },
}

SYSTEM_PROMPT = """あなたは地域メディア「湘南Doors」の校閲担当です。
渡された記事の事実の記述を照合してください。

【この校閲の目的】
一次情報ポリシーの目的は「他メディアの記事を根拠なく転載しないこと」と
「事実誤認を見つけること」です。「すべての文章が1つの公式ページに逐語的に
載っていなければならない」という意味ではありません。
新聞・ニュースサイト・地域まとめメディア・ブログ・口コミサイト・Wikipedia等の
他メディアは根拠にしないでください(ツール側でも遮断されています)。

【手順】
1. 本文・dek・タイトル・店舗情報欄から、確認が必要な事実の記述を抜き出す
   (日付、場所、住所、営業時間、価格、台数・席数などの数値、沿革、立地、
    公開状況、人物の経歴・年齢、発言など)。編集部の主観的な感想は対象外。
   各記述に role を付ける: タイトルの主張=title、リードの主張=dek、
   記事の中心テーマとなる主張=central、それ以外=detail。
2. 記事のlinkがあればまず開き、足りなければweb_searchで公式の情報源を探して開く。
3. 各記述を次の5つに分類する。

   confirmed: 次のいずれかで確認できた。
     - 対象の公式ページ(basis=primary_page)
     - 自治体・公的機関・運営会社・鉄道会社などの公式情報(official_record)
     - 一般に確立した事実(established_fact)
       例: 源実朝は鎌倉幕府第3代将軍。対象ページに書いていなくても confirmed。
     - 単純な計算や位置関係の導出(calculation)
       例: 生年と来日年からの年齢計算、路線図上の駅の並び、駅数。
     「そのページに書いていない」という理由だけで not_found_in_primary にしないこと。

   wording_difference: 表現・概数は違うが、一次情報と両立する。
     例: 記事「駐車場800台規模」/公式「826台」→ 概数として妥当。
     例: 記事「最古級」/公式「現存する唯一」→ 論理的に両立しうる。

   not_found_in_primary: 情報源は開けたが、その記述が見つからず、上記の公的情報・
     確立した事実・計算でも確認できなかった(=未確認。誤りとは限らない)。

   source_unavailable: 情報源そのものを確認できなかった
     (JavaScript依存で本文が取れない、アクセス制限、取得エラー等)。
     「確認できなかった」と「確認したが載っていなかった」を必ず区別すること。

   contradicted: 一次情報と明確に両立しない場合のみ。
     例: 記事「辻堂駅北口から徒歩数分」/公式「辻堂駅からタクシー約10分」
     例: 記事「明治期から続く」/公式「昭和4年創業」
     例: 記事「普段は非公開」/公式「通常公開」
     例: 記事「1845年生まれ、1869年来日時27歳」→ 計算上24歳
     概数・言い換え・包含関係で両立しうるものは contradicted にしないこと。
     contradicted にする場合は articleValue と primaryValue を必ず書き、
     logicallyCompatible=false とすること。

4. 他メディアの取材コメントと思われる人物の発言があれば hasQuotedComment=true。
5. 一次情報同士が食い違う、判断が難しいなど人の確認が必要なら needsHuman=true。
6. 記事単位の判定は次の基準で参考値を出す(最終判定はシステムが行う)。
   confirmed: 重要な事実に明確な矛盾がない(軽微な未確認情報のみ)。
   fix: 明確な contradicted があるが、局所修正で記事の価値を保てる。
   rewrite: タイトル・dek・中心テーマに複数の明確な事実誤認がある。
            not_found_in_primary がいくら多くても、それだけで rewrite にしない。
   review_required: 情報源にアクセスできない、情報源同士が食い違う、判断困難。
最後に必ず submit_audit ツールで提出すること。"""


def article_prompt(a):
    info = {
        k: a.get(k) for k in (
            "id", "title", "dek", "area", "link", "date", "address", "access",
            "hours", "closedDays", "snsLinks", "eventStartDate", "eventEndDate", "body",
        )
    }
    return "以下の記事を照合してください。\n\n" + json.dumps(info, ensure_ascii=False, indent=1)


def audit_one(client, a, blocked):
    tools = [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 6, "blocked_domains": blocked},
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 8, "blocked_domains": blocked},
        AUDIT_TOOL,
    ]
    messages = [{"role": "user", "content": article_prompt(a)}]
    for _ in range(MAX_TURNS):
        resp = client.messages.create(
            model=MODEL, max_tokens=8000, system=SYSTEM_PROMPT,
            tools=tools, messages=messages,
            extra_headers={"anthropic-beta": "web-fetch-2025-09-10"},
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "submit_audit":
                return block.input
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":
            continue
        messages.append({"role": "user", "content": "照合を続け、終わったらsubmit_auditで提出してください。"})
    raise RuntimeError("submit_auditが呼ばれませんでした")


def blocked_domains():
    # blocked_domains は完全なドメイン指定が必要なため、部分一致用の語("tripadvisor")は除外し、
    # 代表的なドメインを補う。
    out = [d for d in g.SECONDARY_MEDIA_DOMAINS if "." in d]
    out += ["tripadvisor.jp", "tripadvisor.com"]
    return sorted(set(out))


# ---------- 応答の正規化(想定外の形式でも落ちないようにする) ----------
# LLMのツール入力は、まれに配列が「JSON文字列」のまま返る・要素がdictではなく
# 文字列になる・フィールドが欠ける、といった形で崩れることがある。
# 集計・レポート処理はすべて normalize_result() を通した結果だけを扱う。

VERDICTS = ("confirmed", "fix", "rewrite", "review_required")
COMPLETED_VERDICTS = ("confirmed", "fix", "rewrite")  # 再開時にスキップしてよい判定
SNIPPET_LEN = 300


def _snippet(value):
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = repr(value)
    return text[:SNIPPET_LEN] + ("…" if len(text) > SNIPPET_LEN else "")


def _maybe_json(value):
    """JSON文字列ならデコードして返す。デコードできなければ元の値を返す。"""
    if isinstance(value, str):
        t = value.strip()
        if t[:1] in ("{", "["):
            try:
                return json.loads(t)
            except (ValueError, TypeError):
                return value
    return value


def normalize_result(raw, article_id):
    """監査ツールの応答を、集計可能な正規形に変換する。
    戻り値: (result, anomalies)
      result   … 必ず dict。verdict は VERDICTS のいずれか。claims は dict のリスト。
      anomalies… 想定外形式の記録(空なら正常)。1件でもあれば verdict=review_required。
    """
    anomalies = []

    def note(field, value, reason):
        anomalies.append({
            "field": field, "type": type(value).__name__,
            "reason": reason, "snippet": _snippet(value),
        })

    raw = _maybe_json(raw)
    if not isinstance(raw, dict):
        note("(response)", raw, "応答がdictではない")
        return ({
            "verdict": "review_required", "summary": "監査応答が想定外の形式のため要確認",
            "claims": [], "primarySources": [], "hasQuotedComment": False, "anomalies": anomalies,
            "rulesVersion": RULES_VERSION,
        }, anomalies)

    # claims
    claims_raw = _maybe_json(raw.get("claims"))
    claims = []
    if claims_raw is None:
        note("claims", claims_raw, "claimsが欠損")
    elif not isinstance(claims_raw, list):
        note("claims", claims_raw, "claimsがリストではない")
    else:
        for idx, c in enumerate(claims_raw):
            c = _maybe_json(c)
            if not isinstance(c, dict):
                note(f"claims[{idx}]", c, "要素がdictではない")
                claims.append({"claim": _snippet(c), "status": "unparsed", "primaryUrl": "", "note": "想定外形式の要素"})
                continue
            status = c.get("status")
            if status not in CLAIM_STATUSES:
                note(f"claims[{idx}].status", status, "statusが欠損または想定外の値")
                status = "unparsed"
            role = c.get("role")
            if role not in CLAIM_ROLES:
                role = "detail"  # 欠損時は骨格扱いしない(過剰判定を避ける)
            compat = c.get("logicallyCompatible")
            if isinstance(compat, str):
                compat = compat.strip().lower() == "true"
            elif not isinstance(compat, bool):
                compat = None
            claims.append({
                "claim": str(c.get("claim") or ""),
                "role": role,
                "status": status,
                "basis": str(c.get("basis") or ""),
                "articleValue": str(c.get("articleValue") or ""),
                "primaryValue": str(c.get("primaryValue") or ""),
                "logicallyCompatible": compat,
                "primaryUrl": str(c.get("primaryUrl") or ""),
                "note": str(c.get("note") or ""),
            })

    # primarySources
    sources_raw = _maybe_json(raw.get("primarySources"))
    if isinstance(sources_raw, list):
        sources = [str(u) for u in sources_raw if isinstance(u, (str, int, float))]
    else:
        if sources_raw is not None:
            note("primarySources", sources_raw, "primarySourcesがリストではない")
        sources = []

    # hasQuotedComment
    quoted = raw.get("hasQuotedComment")
    if isinstance(quoted, str):
        quoted = quoted.strip().lower() == "true"
    elif not isinstance(quoted, bool):
        if quoted is not None:
            note("hasQuotedComment", quoted, "booleanではない")
        quoted = False

    # needsHuman
    needs_human = raw.get("needsHuman")
    if isinstance(needs_human, str):
        needs_human = needs_human.strip().lower() == "true"
    elif not isinstance(needs_human, bool):
        needs_human = False

    # モデルの判定は参考値として保存するだけ。最終判定は decide_verdict() で決める。
    model_verdict = raw.get("verdict")
    if model_verdict == "ok":
        model_verdict = "confirmed"
    if model_verdict not in VERDICTS:
        model_verdict = None

    summary = raw.get("summary")
    summary = summary if isinstance(summary, str) else ("" if summary is None else _snippet(summary))

    claims = [apply_tolerance(c) for c in claims]
    verdict, reasons = decide_verdict(claims, needs_human, anomalies)
    return ({
        "verdict": verdict, "verdictReasons": reasons, "modelVerdict": model_verdict,
        "summary": summary, "claims": claims, "primarySources": sources,
        "hasQuotedComment": quoted, "needsHuman": needs_human,
        "anomalies": anomalies, "rulesVersion": RULES_VERSION,
    }, anomalies)


# ---------- 判定ルール(RULES_VERSION 2) ----------
APPROX_WORDS = ("約", "およそ", "規模", "程度", "前後", "近く", "超", "余り", "以上", "級", "ほど", "強", "弱")
APPROX_TOLERANCE = 0.10  # 概数表現なら±10%までは両立とみなす


def _first_number(text):
    m = re.search(r"\d[\d,]*(?:\.\d+)?", text or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def apply_tolerance(c):
    """contradicted のうち、両立しうるものを wording_difference に戻す(過剰判定の抑止)。
    1) モデル自身が logicallyCompatible=true と答えている
    2) 記事側が概数表現で、数値の差が±10%以内(例: 800台規模 / 826台)"""
    if c.get("status") != "contradicted":
        return c
    if c.get("logicallyCompatible") is True:
        return dict(c, status="wording_difference", note=(c.get("note", "") + " [自動: 両立しうるため wording_difference]").strip())
    av, pv = c.get("articleValue", ""), c.get("primaryValue", "")
    a_num, p_num = _first_number(av), _first_number(pv)
    if a_num and p_num and any(w in av for w in APPROX_WORDS):
        if abs(a_num - p_num) / p_num <= APPROX_TOLERANCE:
            return dict(c, status="wording_difference",
                        note=(c.get("note", "") + " [自動: 概数として許容範囲]").strip())
    return c


def decide_verdict(claims, needs_human=False, anomalies=None):
    """記事単位の最終判定。
    - 応答形式の異常 → review_required
    - 骨格(title/dek/central)に明確なcontradictedが2件以上 → rewrite
    - 明確なcontradictedが1件以上 → fix
    - 骨格の情報が未確認(not_found)・確認不能(source_unavailable)、または needsHuman → review_required
    - それ以外(detailの未確認のみを含む)→ confirmed
    not_found_in_primary は件数がいくら多くても rewrite/fix の理由にしない。"""
    reasons = []
    if anomalies:
        return "review_required", ["応答形式の異常"]
    contradicted = [c for c in claims if c.get("status") == "contradicted"]
    core_contradicted = [c for c in contradicted if c.get("role") in CORE_ROLES]
    core_unverified = [c for c in claims if c.get("role") in CORE_ROLES
                       and c.get("status") in ("not_found_in_primary", "source_unavailable")]
    if len(core_contradicted) >= 2:
        return "rewrite", [f"骨格の明確な誤り{len(core_contradicted)}件"]
    if contradicted:
        reasons.append(f"明確な誤り{len(contradicted)}件")
        if core_unverified or needs_human:
            reasons.append("骨格の未確認・要人確認あり")
        return "fix", reasons
    if core_unverified:
        return "review_required", [f"骨格の情報が未確認・確認不能{len(core_unverified)}件"]
    if needs_human:
        return "review_required", ["AIでは判断困難・情報源の食い違い"]
    return "confirmed", reasons


def log_anomalies(article_id, anomalies):
    for an in anomalies:
        print(f"[anomaly] id:{article_id} field={an['field']} type={an['type']} "
              f"reason={an['reason']} snippet={an['snippet']}", flush=True)


# ---------- 途中結果の保存と再開 ----------

def append_checkpoint(path, result):
    """1記事終わるごとにJSONLへ追記する。途中でクラッシュしても処理済みの結果は残る。"""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def load_previous_results(paths):
    """過去の監査結果(.jsonl / .json / それらを含むディレクトリ)を読み込み、
    記事IDごとに最新の結果を返す。壊れた行は読み飛ばす。"""
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, names in os.walk(p):
                files += [os.path.join(root, n) for n in names if n.endswith((".jsonl", ".json"))]
        elif os.path.isfile(p):
            files.append(p)
    prev = {}
    for fp in sorted(files):
        try:
            with open(fp, encoding="utf-8") as f:
                if fp.endswith(".jsonl"):
                    items = []
                    for line in f:
                        try:
                            items.append(json.loads(line))
                        except ValueError:
                            continue
                else:
                    data = json.load(f)
                    items = data if isinstance(data, list) else []
        except (OSError, ValueError):
            print(f"[resume] 読み込めないファイルをスキップ: {fp}", flush=True)
            continue
        for it in items:
            # verify モードの結果は claim の一部しか持たないため、full の再開・比較には使わない
            if isinstance(it, dict) and isinstance(it.get("id"), int) and it.get("mode") != "verify":
                prev[it["id"]] = it
    return prev


# ---------- レポート ----------

STATUS_LABELS = {
    "contradicted": "明確な誤り",
    "source_unavailable": "情報源を確認できず",
    "not_found_in_primary": "未確認(誤りとは限らない)",
    "wording_difference": "表現・概数の違い(許容)",
    "unparsed": "形式異常",
}


def count_results(results):
    """記事単位と記述単位の件数を数える。想定外の値があっても落ちない。"""
    counts = {v: 0 for v in VERDICTS}
    claim_counts = {k: 0 for k in CLAIM_STATUSES}
    claim_counts["unparsed"] = 0
    for r in results:
        if not isinstance(r, dict):
            counts["review_required"] += 1
            continue
        v = r.get("verdict")
        v = "confirmed" if v == "ok" else v
        counts[v if v in counts else "review_required"] += 1
        claims = r.get("claims")
        for c in claims if isinstance(claims, list) else []:
            st = c.get("status") if isinstance(c, dict) else "unparsed"
            st = st if st in claim_counts else "unparsed"
            claim_counts[st] += 1
    return counts, claim_counts


def build_report(results, stamp):
    """正規化済みの結果からMarkdownレポートを作る。想定外の値があっても落ちない。"""
    order = {"rewrite": 0, "fix": 1, "review_required": 2, "confirmed": 3}
    counts, claim_counts = count_results(results)
    lines = [f"# 一次情報照合レポート({stamp} / 判定ルール v{RULES_VERSION})", "",
             f"対象 {len(results)} 件",
             "記事単位: " + " / ".join(f"{k}: {counts[k]}件" for k in VERDICTS),
             "記述単位: " + " / ".join(f"{k}: {v}件" for k, v in claim_counts.items()),
             ""]
    status_order = list(STATUS_LABELS)
    for r in sorted([r for r in results if isinstance(r, dict)],
                    key=lambda r: (order.get(r.get("verdict"), 2), r.get("id", 0))):
        claims = [c for c in (r.get("claims") or []) if isinstance(c, dict)] if isinstance(r.get("claims"), list) else []
        notable = [c for c in claims if c.get("status") not in ("confirmed", "wording_difference")]
        if r.get("verdict") == "confirmed" and not notable:
            continue
        lines.append(f"## [{r.get('verdict')}] id:{r.get('id')} {r.get('title', '')}")
        if r.get("verdictReasons"):
            lines.append(f"- 判定理由: {' / '.join(r['verdictReasons'])}")
        if r.get("summary"):
            lines.append(f"- 校閲メモ: {r['summary']}")
        if r.get("hasQuotedComment"):
            lines.append("- 人物の発言・取材コメントあり")
        for an in r.get("anomalies") or []:
            if isinstance(an, dict):
                lines.append(f"- 想定外の応答: {an.get('field')} (型:{an.get('type')}) {an.get('reason')} / {an.get('snippet')}")
        for c in sorted(notable, key=lambda c: status_order.index(c["status"]) if c.get("status") in status_order else 99):
            role = "【骨格】" if c.get("role") in CORE_ROLES else ""
            vals = ""
            if c.get("status") == "contradicted" and (c.get("articleValue") or c.get("primaryValue")):
                vals = f" 記事「{c.get('articleValue', '')}」/ 一次情報「{c.get('primaryValue', '')}」"
            extra = f"({c['note']})" if c.get("note") else ""
            lines.append(f"- {STATUS_LABELS.get(c.get('status'), c.get('status'))}{role}: {c.get('claim', '')}{vals}{extra}")
        lines.append("")
    return "\n".join(lines), counts


def make_client():
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def main(argv=None, client=None, articles_override=None, sleep_sec=2, fetcher=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--start", type=int, default=None)
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--resume", action="append", default=[],
                    help="過去の監査結果(.jsonl/.json/ディレクトリ)。confirmed/fix/rewrite済みの記事は再監査しない")
    ap.add_argument("--out-dir", default=OUT_DIR)
    ap.add_argument("--mode", choices=["full", "verify"], default="full",
                    help="full=記事全体を監査(新規・初回) / verify=前回contradictedのclaimだけ再確認(修正後)")
    ap.add_argument("--previous", action="append", default=[],
                    help="verify用: 前回の監査結果(.jsonl/.json/ディレクトリ)")
    args = ap.parse_args(argv)

    source = articles_override if articles_override is not None else g.load_json(g.ARTICLES_JSON_PATH)
    articles = sorted([a for a in source if not a.get("mergedInto")], key=lambda a: a["id"])
    if args.ids:
        want = {int(x) for x in args.ids.split(",") if x.strip()}
        articles = [a for a in articles if a["id"] in want]
    if args.start is not None:
        articles = [a for a in articles if a["id"] >= args.start]
    if args.end is not None:
        articles = [a for a in articles if a["id"] <= args.end]

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d-%H%M%S")

    if args.mode == "verify":
        # 修正後の再確認: 前回 contradicted の claim だけを、コード判定→記事単位の一括APIで確認する。
        # 記事全文の再監査は行わない。
        import fact_verify
        if not args.previous:
            print("verify モードには --previous(前回の監査結果)が必要です", flush=True)
            return 2
        fact_verify.run_verify(articles, args.previous, args.out_dir, stamp,
                               client=client, fetcher=fetcher, sleep_sec=sleep_sec)
        return 0
    ckpt_path = os.path.join(args.out_dir, f"fact_audit_{stamp}.jsonl")
    json_path = os.path.join(args.out_dir, f"fact_audit_{stamp}.json")
    md_path = os.path.join(args.out_dir, f"fact_audit_{stamp}.md")

    prev = load_previous_results(args.resume) if args.resume else {}
    results = []
    todo = []
    for a in articles:
        p = prev.get(a["id"])
        if (isinstance(p, dict) and p.get("verdict") in COMPLETED_VERDICTS
                and p.get("rulesVersion") == RULES_VERSION):
            p = dict(p, resumed=True)
            results.append(p)
            append_checkpoint(ckpt_path, p)
        else:
            todo.append(a)
    if prev:
        print(f"[resume] 過去結果から{len(results)}件を引き継ぎ、{len(todo)}件を監査します", flush=True)

    if todo and client is None:
        client = make_client()
    blocked = blocked_domains()
    for a in todo:
        print(f"[audit] id:{a['id']} {a['title']}", flush=True)
        try:
            raw = audit_one(client, a, blocked)
            r, anomalies = normalize_result(raw, a["id"])
            if anomalies:
                log_anomalies(a["id"], anomalies)
        except Exception as e:  # 1記事の失敗で全体を止めない
            print(f"[anomaly] id:{a['id']} 監査処理で例外: {type(e).__name__}: {_snippet(str(e))}", flush=True)
            r = {"verdict": "review_required", "summary": f"監査処理で例外: {type(e).__name__}: {e}",
                 "claims": [], "primarySources": [], "hasQuotedComment": False,
                 "rulesVersion": RULES_VERSION,
                 "anomalies": [{"field": "(exception)", "type": type(e).__name__,
                                "reason": "監査処理で例外", "snippet": _snippet(str(e))}]}
        r.update({"id": a["id"], "slug": a.get("slug", ""), "title": a.get("title", "")})
        results.append(r)
        append_checkpoint(ckpt_path, r)
        if sleep_sec:
            time.sleep(sleep_sec)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    report, counts = build_report(results, stamp)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(report)
    print("集計: " + " / ".join(f"{k}: {v}" for k, v in counts.items()), flush=True)
    print(f"完了: {json_path}", flush=True)
    # review_required は「要確認」であり異常終了ではないため、終了コードは0とする
    return 0


if __name__ == "__main__":
    sys.exit(main())
