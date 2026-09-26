#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
湘南Doors 公開前監査ゲート(Daily Articles 用)
------------------------------------------------------------------------
generate_articles.py が生成したドラフトを、articles.json に書き込む(=公開する)前に
Fact Audit(fact_audit.py と同じ判定ルール)にかけ、合格した記事だけを公開対象にする。

流れ(1ドラフトごと):
  ドラフト → 一次情報チェック(generate_articles.check_source_policy。呼び出し元で実施済み)
          → Fact Audit(fact_audit.audit_one + normalize_result)
          → 不合格のうち安全に直せるものだけ自動修正 → 再監査(1回だけ)
          → 品質ゲート判定(evaluate)

合格条件(すべて満たすこと):
  - 記事単位の判定が confirmed(fix / rewrite / review_required は不合格)
  - contradicted の claim が1件も残っていない
  - 骨格(title / dek / central)の claim に未確認・確認不能が残っていない
  - 人物の発言(他メディアの取材コメント流用の疑い)を含まない
  - 監査応答の形式異常・監査処理の例外がない(監査できなかった記事は公開しない)

自動修正は「一次情報側の値で単純に置き換えられる数値・日時の誤り」の置き換えと、
置き換えられない細部の誤りを含む1文の削除に限る(safe_autofix を参照)。
新しい事実の書き足しや、骨格(title / dek / central)の書き換えは自動では行わない。

不合格記事は公開せず、理由と claim を実行レポート(gate_rejected)に記録する。
Issue 化は report_gate_rejections.py が実行レポートを読んで行う。

他メディアは fact_audit と同じく blocked_domains で遮断しているため、
他メディアの記述を根拠に合格することはない。
"""
import os
import re

GATE_ENV = "PUBLISH_GATE"  # "off" のときだけゲートを無効化する(既定は有効)

AUTOFIX_MAX_LEN = 40       # 自動修正で置き換える値の最大文字数
AUTOFIX_MAX_REMOVALS = 2   # 自動修正で削除してよい文の数(1記事あたり)
_DIGIT_RE = re.compile(r"[0-9０-９]")
_SENTENCE_RE = re.compile(r"[^。]*。|[^。]+$")


def enabled():
    return os.environ.get(GATE_ENV, "on").strip().lower() not in ("off", "0", "false", "no")


# ---------- 判定 ----------

def evaluate(result):
    """正規化済みの監査結果(fact_audit.normalize_result の戻り値)から合否を決める。
    戻り値: (passed, reasons, blocking_claims)"""
    # 循環 import を避けるため、使う時点で読み込む(fact_audit は generate_articles を import する)
    import fact_audit as fa

    reasons, blocking = [], []
    if not isinstance(result, dict):
        return False, ["監査結果が想定外の形式"], []
    if result.get("anomalies"):
        reasons.append("監査応答の形式異常・監査処理の例外(監査できていない)")
    claims = [c for c in (result.get("claims") or []) if isinstance(c, dict)]
    contradicted = [c for c in claims if c.get("status") == "contradicted"]
    core_unverified = [c for c in claims if c.get("role") in fa.CORE_ROLES
                       and c.get("status") in ("not_found_in_primary", "source_unavailable", "unparsed")]
    if contradicted:
        reasons.append(f"一次情報と矛盾する記述(contradicted)が{len(contradicted)}件")
        blocking += contradicted
    if core_unverified:
        reasons.append(f"骨格(title/dek/central)の記述が一次情報で未確認・確認不能{len(core_unverified)}件")
        blocking += core_unverified
    if result.get("hasQuotedComment"):
        reasons.append("人物の発言(他メディアの取材コメント流用の疑い)を含む")
    if result.get("needsHuman"):
        reasons.append("一次情報同士の食い違いなど、人の確認が必要(review_required)")
    verdict = result.get("verdict")
    if verdict != "confirmed" and not reasons:
        reasons.append(f"Fact Audit の判定が {verdict}")
    if not claims and not reasons:
        reasons.append("確認できた claim が0件(監査できていない)")
    return (not reasons), reasons, blocking


# ---------- 安全な自動修正 ----------

def _fixable(claim, body):
    """一次情報の値で機械的に置き換えてよい claim か。
    - detail(細部)の contradicted に限る(骨格の誤りは記事の前提が崩れるため直さない)
    - 記事側の値が本文にちょうど1回だけ現れる(置換箇所が一意に決まる)
    - 記事側・一次情報側とも短い数値・日時の表現(数字を含み AUTOFIX_MAX_LEN 文字以下)
    - 一次情報URLがあり、他メディアではない"""
    import generate_articles as g

    if claim.get("status") != "contradicted" or claim.get("role") != "detail":
        return False
    av, pv, url = claim.get("articleValue") or "", claim.get("primaryValue") or "", claim.get("primaryUrl") or ""
    if not av or not pv or av == pv:
        return False
    if len(av) > AUTOFIX_MAX_LEN or len(pv) > AUTOFIX_MAX_LEN:
        return False
    if not (_DIGIT_RE.search(av) and _DIGIT_RE.search(pv)):
        return False
    if not url.startswith(("http://", "https://")) or g.is_secondary_media(url):
        return False
    return body.count(av) == 1


def _sentence_to_remove(claim, entry, body):
    """置き換えでは直せない detail の contradicted について、その記述を含む1文を削除してよいか。
    削除してよければ削除する文を、だめなら None を返す。
    - detail(細部)の contradicted に限る(骨格の誤りは記事の前提が崩れるため直さない)
    - 記事側の値が本文にちょうど1回だけ現れ、タイトル・リードには現れない
    - その文(「。」区切り)が段落内で一意に決まり、段落にほかの文が残る
    - 一次情報URLがあり、他メディアではない
    誤った細部を消すだけで、新しい事実は書き足さない(書き足すと一次情報の裏付けが要るため)。"""
    import generate_articles as g

    if claim.get("status") != "contradicted" or claim.get("role") != "detail":
        return None
    av, url = claim.get("articleValue") or "", claim.get("primaryUrl") or ""
    if not av or "。" in av or body.count(av) != 1:
        return None
    if av in (entry.get("title") or "") or av in (entry.get("dek") or ""):
        return None
    if not url.startswith(("http://", "https://")) or g.is_secondary_media(url):
        return None
    i = body.find(av)
    start = body.rfind("\n", 0, i) + 1
    end = body.find("\n", i)
    paragraph = body[start:end if end != -1 else len(body)]
    sentences = [s for s in _SENTENCE_RE.findall(paragraph) if s.strip()]
    hits = [s for s in sentences if av in s]
    if len(hits) != 1 or len(sentences) < 2 or body.count(hits[0]) != 1:
        return None
    return hits[0]


def safe_autofix(entry, result):
    """contradicted の claim のうち安全に直せるものだけを直す。
    一次情報の値で単純に置き換えられるもの(_fixable)は置き換え、置き換えられない細部は
    その1文を削除する(_sentence_to_remove。1記事につき AUTOFIX_MAX_REMOVALS 文まで)。
    1件でも直せない contradicted があれば何もしない(部分修正した記事を公開しないため)。
    骨格の未確認・形式異常・人物の発言など、contradicted 以外の不合格理由があっても何もしない。
    修正した記事は呼び出し元(check_draft)で必ず再監査する。
    戻り値: (修正後のentry または None, 修正内容のリスト)"""
    if not isinstance(result, dict) or result.get("anomalies") or result.get("hasQuotedComment") \
            or result.get("needsHuman"):
        return None, []
    passed, _reasons, blocking = evaluate(result)
    if passed or not blocking:
        return None, []
    body = entry.get("body") or ""
    if any(c.get("status") != "contradicted" for c in blocking):
        return None, []
    fixes, removals = [], 0
    for c in blocking:
        if _fixable(c, body):
            body = body.replace(c["articleValue"], c["primaryValue"], 1)
            fixes.append({"claim": c.get("claim", ""), "from": c["articleValue"], "to": c["primaryValue"],
                          "primaryUrl": c.get("primaryUrl", ""), "action": "replace"})
            continue
        sentence = _sentence_to_remove(c, entry, body)
        if sentence is None or removals >= AUTOFIX_MAX_REMOVALS:
            return None, []
        body = body.replace(sentence, "", 1)
        removals += 1
        fixes.append({"claim": c.get("claim", ""), "from": sentence, "to": "",
                      "primaryUrl": c.get("primaryUrl", ""), "action": "remove"})
    fixed = dict(entry, body=body)
    # 置き換えに使った一次情報URLを sources に加える(事実確認に使った情報源として残す)
    sources = list(entry.get("sources") or [])
    for f in fixes:
        if f["action"] == "replace" and f["primaryUrl"] not in sources:
            sources.append(f["primaryUrl"])
    fixed["sources"] = sources
    return fixed, fixes


# ---------- 監査の実行 ----------

def audit_draft(client, entry):
    """1ドラフトを Fact Audit にかけ、正規化済みの結果を返す。例外は呼び出し元で扱う。"""
    import fact_audit as fa

    raw = fa.audit_one(client, entry, fa.blocked_domains())
    result, anomalies = fa.normalize_result(raw, entry.get("id"))
    if anomalies:
        fa.log_anomalies(entry.get("id"), anomalies)
    return result


def _audit_safely(client, entry):
    try:
        return audit_draft(client, entry)
    except Exception as e:  # 監査できなかった記事は公開しない(fail-closed)
        return {"verdict": "review_required", "claims": [], "primarySources": [],
                "hasQuotedComment": False, "needsHuman": False,
                "summary": f"監査処理で例外: {type(e).__name__}: {e}",
                "anomalies": [{"field": "(exception)", "type": type(e).__name__,
                               "reason": "監査処理で例外", "snippet": str(e)[:300]}]}


def _claim_summary(c):
    return {k: c.get(k, "") for k in ("claim", "role", "status", "articleValue", "primaryValue",
                                        "primaryUrl", "note")}


def check_draft(entry, client=None):
    """ドラフト1件を公開前監査にかける。
    戻り値: dict
      passed   … True なら公開してよい
      entry    … 公開する記事(自動修正した場合は修正後。不合格なら元のドラフト)
      verdict / reasons / claims(不合格の原因になった claim)/ autofix / audits(監査回数)"""
    import fact_audit as fa

    if client is None:
        client = fa.make_client()
    result = _audit_safely(client, entry)
    passed, reasons, blocking = evaluate(result)
    audits, autofix = 1, []
    if not passed:
        fixed, fixes = safe_autofix(entry, result)
        if fixed is not None:
            # 自動修正した記事は必ず再監査する。再監査で合格しなければ公開しない。
            re_result = _audit_safely(client, fixed)
            audits += 1
            re_passed, re_reasons, re_blocking = evaluate(re_result)
            autofix = fixes
            if re_passed:
                entry, result, passed, reasons, blocking = fixed, re_result, True, [], []
            else:
                result, reasons, blocking = re_result, ["自動修正後の再監査で不合格"] + re_reasons, re_blocking
    return {
        "passed": passed,
        "entry": entry,
        "verdict": result.get("verdict") if isinstance(result, dict) else None,
        "reasons": reasons,
        "claims": [_claim_summary(c) for c in blocking],
        "summary": result.get("summary", "") if isinstance(result, dict) else "",
        "primarySources": result.get("primarySources", []) if isinstance(result, dict) else [],
        "autofix": autofix,
        "audits": audits,
    }


def rejection_record(entry, gate, article_type):
    """実行レポート(gate_rejected)に残す、不合格記事1件分の記録。"""
    return {
        "articleType": article_type,
        "title": entry.get("title", ""),
        "dek": entry.get("dek", ""),
        "area": entry.get("area", ""),
        "cat": entry.get("cat", ""),
        "link": entry.get("link", ""),
        "sources": entry.get("sources", []),
        "verdict": gate.get("verdict"),
        "reasons": gate.get("reasons", []),
        "claims": gate.get("claims", []),
        "summary": gate.get("summary", ""),
        "primarySources": gate.get("primarySources", []),
        "autofix": gate.get("autofix", []),
    }
