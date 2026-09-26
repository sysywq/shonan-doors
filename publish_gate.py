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
          → 一次情報で修正内容が一意に決まるものは自動修正 → 再監査(最大 AUTOFIX_MAX_ROUNDS 回)
          → 品質ゲート判定(evaluate)
          → 不合格なら、人の判断が要るか(escalation)を決める

合格条件(すべて満たすこと):
  - 記事単位の判定が confirmed(fix / rewrite / review_required は不合格)
  - contradicted の claim が1件も残っていない
  - 骨格(title / dek / central)の claim に未確認・確認不能が残っていない
  - 人物の発言(他メディアの取材コメント流用の疑い)を含まない
  - 監査応答の形式異常・監査処理の例外がない(監査できなかった記事は公開しない)

自動判断の原則(オーナー確認を求めない):
  一次情報で修正内容が一意に決まる場合は、自動修正 → 再監査 → confirmed なら公開する(safe_autofix)。
  - 料金・日時・時刻・徒歩分数などの値が公式と明確に異なり、公式側の値が一意に分かる → 置き換える
    (骨格の値でも、単位や書式が同じ「値だけの差し替え」なら記事の主旨は変わらないので置き換える)
  - 置き換えられない細部の誤り → その1文を削除する(新しい事実は書き足さない)

人の判断が必要なのは次の4つだけ(escalation。Approve / Reject の2択で通知する):
  - source_conflict: 公式情報同士が実質的に矛盾している(needsHuman)
  - core_unverified: 重要な記述(title / dek / central)が公式情報で確認できない
  - premise_change : 修正すると記事の主旨そのものが変わる(骨格の値以外の誤り)
  - ambiguous      : どの情報を採用すべきか一意に決められない
それ以外の不合格(監査できなかった・人物の発言を含む等)は人に聞かずに見送り、別トピックで補充する。

不合格記事は公開せず、理由・claim・escalation を実行レポート(gate_rejected)に記録する。
Issue 化は report_gate_rejections.py が実行レポートを読んで行う(escalation がある記事だけ)。

他メディアは fact_audit と同じく blocked_domains で遮断しているため、
他メディアの記述を根拠に合格することはない。
"""
import os
import re

GATE_ENV = "PUBLISH_GATE"  # "off" のときだけゲートを無効化する(既定は有効)

AUTOFIX_MAX_LEN = 40       # 自動修正で置き換える値の最大文字数
AUTOFIX_MAX_REMOVALS = 2   # 自動修正で削除してよい文の数(1記事あたり)
AUTOFIX_MAX_ROUNDS = 2     # 自動修正 → 再監査を繰り返す上限(監査は最大 1 + この回数)
AUTOFIX_MAX_CORE_OCCURRENCES = 3  # 骨格の値を置き換えるとき、記事全体で置き換えてよい箇所数
# 値を置き換える対象の欄。骨格(title/dek/central)の値はタイトル・リードも含めて置き換える。
FIX_FIELDS = ("body", "address", "access", "hours", "closedDays")
CORE_FIX_FIELDS = ("title", "dek") + FIX_FIELDS
_DIGIT_RE = re.compile(r"[0-9０-９]")
_NUMBER_RE = re.compile(r"[0-9０-９][0-9０-９,，.．]*")
_SENTENCE_RE = re.compile(r"[^。]*。|[^。]+$")
_MONTH_DAY_RE = re.compile(r"([0-9]{1,2})月([0-9]{1,2})日")
_ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")

ESCALATION_KINDS = {
    "source_conflict": "公式情報同士が実質的に矛盾している",
    "core_unverified": "重要な記述が公式情報で確認できない",
    "premise_change": "修正すると記事の主旨そのものが変わる",
    "ambiguous": "どの情報を採用すべきか一意に決められない",
}
ESCALATION_MAX_ITEMS = 3  # 通知に載せる claim の数(簡潔さを優先)


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

def _value_re(value):
    """値の出現を探す正規表現。数字で始まる・終わる値は、前後に数字が続く箇所に一致させない
    (「3日」が「13日」に一致して別の日付を書き換えないため)。"""
    pat = re.escape(value)
    if _DIGIT_RE.match(value[0]):
        pat = r"(?<![0-9０-９])" + pat
    if _DIGIT_RE.match(value[-1]):
        pat += r"(?![0-9０-９,，.．])"
    return re.compile(pat)


def _count(text, value):
    return len(_value_re(value).findall(text)) if isinstance(text, str) and value else 0


def _shape(value):
    """数値を除いた書式(「10月3日」→「#月#日」、「1,000円」→「#円」)。単位・書式が同じなら値だけの差し替え。"""
    return _NUMBER_RE.sub("#", value)


def _is_core(claim):
    import fact_audit as fa
    return claim.get("role") in fa.CORE_ROLES


def _fixable(claim, entry):
    """一次情報の値で機械的に置き換えてよい(=修正内容が一意に決まる) claim か。
    共通: contradicted で、記事側・一次情報側とも短い1句(AUTOFIX_MAX_LEN 文字以下・文をまたがない)、
          一次情報URLがあり他メディアではない。
    - detail(細部): どちらかに数字を含む(料金・日時・時刻・徒歩分数など)、記事側の値が
      本文・店舗情報欄にちょうど1回だけ現れ、タイトル・リードには現れない
    - 骨格(title/dek/central): 両方に数字を含み、単位・書式が同じ(値だけの差し替えで記事の主旨は
      変わらない)、記事全体で1〜AUTOFIX_MAX_CORE_OCCURRENCES 箇所。骨格の文言そのものの誤りは直さない。"""
    import generate_articles as g

    if claim.get("status") != "contradicted":
        return False
    av, pv, url = claim.get("articleValue") or "", claim.get("primaryValue") or "", claim.get("primaryUrl") or ""
    if not av or not pv or av == pv:
        return False
    if len(av) > AUTOFIX_MAX_LEN or len(pv) > AUTOFIX_MAX_LEN:
        return False
    if any(ch in v for v in (av, pv) for ch in ("。", "\n")):
        return False
    if not url.startswith(("http://", "https://")) or g.is_secondary_media(url):
        return False
    if _is_core(claim):
        if not (_DIGIT_RE.search(av) and _DIGIT_RE.search(pv)) or _shape(av) != _shape(pv):
            return False
        n = sum(_count(entry.get(f), av) for f in CORE_FIX_FIELDS)
        return 1 <= n <= AUTOFIX_MAX_CORE_OCCURRENCES
    if not (_DIGIT_RE.search(av) or _DIGIT_RE.search(pv)):
        return False
    if _count(entry.get("title"), av) or _count(entry.get("dek"), av):
        return False
    return sum(_count(entry.get(f), av) for f in FIX_FIELDS) == 1


def _sync_event_dates(entry, av, pv):
    """開催日の値を置き換えたとき、構造化データ(eventStartDate / eventEndDate)も同じ日付なら合わせる。
    記事側・一次情報側とも「M月D日」をちょうど1つ含む場合だけ。戻り値: 更新した欄の一覧"""
    a = _MONTH_DAY_RE.findall(av.translate(_ZEN2HAN))
    p = _MONTH_DAY_RE.findall(pv.translate(_ZEN2HAN))
    if len(a) != 1 or len(p) != 1:
        return []
    (am, ad), (pm, pd) = a[0], p[0]
    changed = []
    for key in ("eventStartDate", "eventEndDate"):
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", entry.get(key) or "")
        if m and int(m.group(2)) == int(am) and int(m.group(3)) == int(ad):
            entry[key] = f"{m.group(1)}-{int(pm):02d}-{int(pd):02d}"
            changed.append(key)
    return changed


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


def _conflicting_values(claims):
    """同じ記事側の値に、一次情報側の値が複数ある(=どれを採るか一意に決まらない)か。"""
    seen = {}
    for c in claims:
        av, pv = c.get("articleValue") or "", c.get("primaryValue") or ""
        if av and pv and seen.setdefault(av, pv) != pv:
            return True
    return False


def safe_autofix(entry, result):
    """contradicted の claim のうち、一次情報で修正内容が一意に決まるものを直す。
    一次情報の値で置き換えられるもの(_fixable)は置き換え、置き換えられない細部は
    その1文を削除する(_sentence_to_remove。1記事につき AUTOFIX_MAX_REMOVALS 文まで)。
    1件でも直せない contradicted があれば何もしない(部分修正した記事を公開しないため)。
    骨格の未確認・形式異常・人物の発言・一次情報同士の食い違い(needsHuman)など、
    contradicted 以外の不合格理由があっても何もしない。同じ記事側の値に一次情報側の値が
    複数ある場合も、採る値が一意に決まらないので何もしない。
    修正した記事は呼び出し元(check_draft)で必ず再監査する。
    戻り値: (修正後のentry または None, 修正内容のリスト)"""
    if not isinstance(result, dict) or result.get("anomalies") or result.get("hasQuotedComment") \
            or result.get("needsHuman"):
        return None, []
    passed, _reasons, blocking = evaluate(result)
    if passed or not blocking:
        return None, []
    if any(c.get("status") != "contradicted" for c in blocking) or _conflicting_values(blocking):
        return None, []
    fixed, fixes, removals = dict(entry), [], 0
    for c in blocking:
        av, pv = c.get("articleValue") or "", c.get("primaryValue") or ""
        if _fixable(c, fixed):
            for f in (CORE_FIX_FIELDS if _is_core(c) else FIX_FIELDS):
                if isinstance(fixed.get(f), str):
                    fixed[f] = _value_re(av).sub(lambda _m: pv, fixed[f])
            dates = _sync_event_dates(fixed, av, pv) if _is_core(c) else []
            fixes.append({"claim": c.get("claim", ""), "from": av, "to": pv, "role": c.get("role", ""),
                          "primaryUrl": c.get("primaryUrl", ""), "action": "replace", "syncedFields": dates})
            continue
        sentence = _sentence_to_remove(c, fixed, fixed.get("body") or "")
        if sentence is None or removals >= AUTOFIX_MAX_REMOVALS:
            return None, []
        fixed["body"] = fixed["body"].replace(sentence, "", 1)
        removals += 1
        fixes.append({"claim": c.get("claim", ""), "from": sentence, "to": "", "role": c.get("role", ""),
                      "primaryUrl": c.get("primaryUrl", ""), "action": "remove"})
    # 置き換えに使った一次情報URLを sources に加える(事実確認に使った情報源として残す)
    sources = list(entry.get("sources") or [])
    for f in fixes:
        if f["action"] == "replace" and f["primaryUrl"] not in sources:
            sources.append(f["primaryUrl"])
    fixed["sources"] = sources
    return fixed, fixes


# ---------- 人の判断が必要か(escalation) ----------

def _official_text(c):
    pv = c.get("primaryValue") or ""
    if not pv:
        pv = {"not_found_in_primary": "公式情報に記載なし",
              "source_unavailable": "公式ページを確認できず"}.get(c.get("status"), c.get("note") or "(記載なし)")
    url = c.get("primaryUrl") or ""
    return f"{pv}({url})" if url else pv


def _quote(values):
    return "・".join(f"「{v}」" for v in values)


def escalation(entry, result):
    """不合格の記事について、人の判断が必要かを決める。必要なら通知内容の dict を、不要なら None を返す。
    人の判断が必要なのは ESCALATION_KINDS の4つだけ。監査できなかった記事(形式異常・例外)や
    人物の発言を含む記事は、人に聞いても直せないので聞かずに見送る(別トピックで補充する)。
    判断は Approve / Reject の2択で済むように、承認・見送りそれぞれで何をするかを書く。"""
    if not isinstance(result, dict) or result.get("anomalies") or result.get("hasQuotedComment"):
        return None
    passed, _reasons, blocking = evaluate(result)
    if passed:
        return None
    claims = [c for c in (result.get("claims") or []) if isinstance(c, dict)]
    core_contradicted = [c for c in blocking if c.get("status") == "contradicted" and _is_core(c)]
    core_unverified = [c for c in blocking if c.get("status") != "contradicted" and _is_core(c)]
    detail_contradicted = [c for c in blocking if c.get("status") == "contradicted" and not _is_core(c)]
    if result.get("needsHuman"):
        kind = "source_conflict"
        items = blocking or [c for c in claims if c.get("status") not in ("confirmed", "wording_difference")]
    elif core_contradicted:
        kind, items = "premise_change", core_contradicted
    elif core_unverified:
        kind, items = "core_unverified", core_unverified
    elif detail_contradicted:
        kind, items = "ambiguous", detail_contradicted
    else:
        return None
    items = items[:ESCALATION_MAX_ITEMS]
    avs = [c.get("articleValue") or c.get("claim") or "" for c in items]
    pvs = [c.get("primaryValue") for c in items if c.get("primaryValue")]
    article = " / ".join(avs) or "(該当する記載の特定なし)"
    official = " / ".join(_official_text(c) for c in items) or (result.get("summary") or "(記録なし)")
    then = "再監査し、confirmed なら公開します"
    if kind == "core_unverified":
        approve = f"確認できない{_quote(avs)}を記事から外し、公式情報で確認できる内容だけに書き直して{then}"
    elif kind == "premise_change":
        approve = (f"記事の主旨を公式情報{_quote(pvs)}に合わせて書き直し、{then}" if pvs
                   else f"記事の主旨を公式情報に合わせて書き直し、{then}")
    elif pvs:
        approve = f"記事内{_quote(avs)}を公式情報{_quote(pvs)}のとおりに直し、{then}"
    else:
        approve = f"{_quote(avs)}の記述を削除し、{then}"
    return {
        "kind": kind,
        "reason": ESCALATION_KINDS[kind],
        "topic": entry.get("title", ""),
        "article": article,
        "official": official,
        "approve": approve,
        "reject": "この記事は公開せずに見送ります(不足分は Daily Articles の補充生成で別トピックを公開します)",
    }


def format_escalation(esc):
    """オーナーに判断を求めるときの通知文(簡潔な固定形式。判断は Approve / Reject の2択)。"""
    return "\n".join([
        f"対象トピック: {esc.get('topic', '')}",
        f"記事内: {esc.get('article', '')}",
        f"公式情報: {esc.get('official', '')}",
        f"Approve: {esc.get('approve', '')}",
        f"Reject: {esc.get('reject', '')}",
    ])


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
    修正内容が一意に決まる誤りは自動修正して再監査する(最大 AUTOFIX_MAX_ROUNDS 回)。
    戻り値: dict
      passed     … True なら公開してよい
      entry      … 公開する記事(自動修正した場合は修正後。不合格なら元のドラフト)
      candidate  … 最後に監査した版(不合格時に Approve で続きから直すため)
      escalation … 人の判断が必要なら通知内容(escalation の戻り値)。不要なら None
      verdict / reasons / claims(不合格の原因になった claim)/ autofix / audits(監査回数)"""
    import fact_audit as fa

    if client is None:
        client = fa.make_client()
    result = _audit_safely(client, entry)
    passed, reasons, blocking = evaluate(result)
    audits, autofix, current = 1, [], entry
    for _ in range(AUTOFIX_MAX_ROUNDS):
        if passed:
            break
        fixed, fixes = safe_autofix(current, result)
        if fixed is None:
            break
        # 自動修正した記事は必ず再監査する。再監査で合格しなければ公開しない。
        result = _audit_safely(client, fixed)
        audits += 1
        autofix += fixes
        current = fixed
        passed, reasons, blocking = evaluate(result)
    if not passed and autofix:
        reasons = ["自動修正後の再監査で不合格"] + reasons
    return {
        "passed": passed,
        "entry": current if passed else entry,
        "candidate": current,
        "escalation": None if passed else escalation(current, result),
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
        # 人の判断が必要なときだけ通知内容が入る(None なら人に聞かずに見送り)
        "escalation": gate.get("escalation"),
        # Approve されたら続きから直せるよう、最後に監査した版を残す
        "draft": gate.get("candidate") or entry,
    }
