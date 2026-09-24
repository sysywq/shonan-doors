#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fact Audit verify モード(修正済み記事の再確認用・省コスト版)
------------------------------------------------------------------------
前回の監査結果で contradicted だった claim だけを再判定する。記事全文は送らない。

処理の流れ(1記事ごと):
  1. 前回結果から対象 claim を取り出す
       - 前回が full 監査   → status == contradicted の claim
       - 前回が verify 結果 → verifyResult が resolved 以外の claim(未解決の持ち越し)
     confirmed / wording_difference / not_found_in_primary / source_unavailable は対象外。
  2. コードだけで判定できるものは API を呼ばずに確定する
       - 旧記述(articleValue)が記事に残っている          → still_contradicted
       - 一次情報側の値(primaryValue)が該当箇所に入っている → resolved
       - 数値・概数・年齢・日付・時刻が一次情報と一致       → resolved
       - 該当する記述が記事から消えている                  → resolved(削除で解消)
       - 一次情報URLが取得できない/他メディア              → review_required
  3. 残った claim だけを「記事単位で1回」の API リクエストにまとめて送る
       送るのは: claim・旧値・一次情報の値・修正後の該当箇所と前後の文・一次情報の関連抜粋
       Web検索・Web取得ツールは使わせない(一次情報はコード側で取得して抜粋を渡す)
  4. 判定は resolved / still_contradicted / review_required の3種類
"""
import html as html_lib
import json
import os
import re
import urllib.request

import fact_audit as fa

VERIFY_RESULTS = ("resolved", "still_contradicted", "review_required")
SNIPPET_CONTEXT = 1          # 該当文の前後に何文つけるか
PRIMARY_EXCERPT_CHARS = 1200  # 一次情報の抜粋の最大文字数(1 claim あたり)
REMOVED_SIM_THRESHOLD = 0.25  # これ未満なら「該当する記述が消えた」とみなす
FETCH_TIMEOUT = 15
USER_AGENT = "ShonanDoors-FactVerify/1.0 (+https://www.shonandoors.com/)"

VERIFY_TOOL = {
    "name": "submit_verification",
    "description": "修正後の記事で、前回 contradicted だった記述が解消されたかを提出する。",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "claim の番号(入力の [n])"},
                        "result": {"type": "string", "enum": list(VERIFY_RESULTS)},
                        "logicallyCompatible": {
                            "type": "boolean",
                            "description": "修正後の記述と一次情報の記述が、表現の違いを除いて意味として両立するか",
                        },
                        "reason": {"type": "string", "description": "判定理由を1文で"},
                    },
                    "required": ["index", "result", "logicallyCompatible", "reason"],
                },
            },
        },
        "required": ["results"],
    },
}

VERIFY_SYSTEM_PROMPT = """あなたは地域メディア「湘南Doors」の校閲担当です。
前回の校閲で「一次情報と矛盾する」と判定された記述が、記事の修正で解消されたかだけを確認します。
記事全体の再校閲はしないでください。渡された claim 以外は判定対象外です。

各 claim について、渡された「修正後の該当箇所」と「一次情報の抜粋」だけを根拠に判定してください。
- resolved: 修正後の記述が一次情報と両立する(概数・言い換え・包含関係で両立するものも含む)、
            または誤っていた記述が削除されている
- still_contradicted: 修正後も一次情報と明確に両立しない記述が残っている(両立しうる場合は使わない)
- review_required: 渡された情報だけでは判断できない
logicallyCompatible には、修正後の記述と一次情報が意味として両立するかを true/false で答えてください。
「ほぼ両立する」「表現が異なるだけ」と判断した場合は、result は resolved、logicallyCompatible は true です。
最後に必ず submit_verification で全 claim の結果を提出してください。"""


# ---------- 前回結果の読み込み ----------

def load_previous_for_verify(paths):
    """過去の監査結果を読み込み、記事IDごとに「full の最新結果」と「verify の最新結果」を返す。
    壊れた行・想定外の形式は読み飛ばす。"""
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, names in os.walk(p):
                files += [os.path.join(root, n) for n in names if n.endswith((".jsonl", ".json"))]
        elif os.path.isfile(p):
            files.append(p)
    full, verify = {}, {}
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
            print(f"[verify] 読み込めないファイルをスキップ: {fp}", flush=True)
            continue
        for it in items:
            if not (isinstance(it, dict) and isinstance(it.get("id"), int)):
                continue
            (verify if it.get("mode") == "verify" else full)[it["id"]] = it
    return full, verify


def target_claims(full_prev, verify_prev):
    """再判定の対象 claim を返す。前回が verify なら未解決分、そうでなければ contradicted 分。"""
    if isinstance(verify_prev, dict) and isinstance(verify_prev.get("claims"), list):
        out = []
        for c in verify_prev["claims"]:
            if isinstance(c, dict) and c.get("verifyResult") != "resolved":
                out.append({k: c.get(k, "") for k in ("claim", "role", "articleValue", "primaryValue", "primaryUrl", "note")})
        return out
    if isinstance(full_prev, dict) and isinstance(full_prev.get("claims"), list):
        return [dict(c) for c in full_prev["claims"]
                if isinstance(c, dict) and c.get("status") == "contradicted"]
    return []


# ---------- 記事側の該当箇所の抽出 ----------

def _norm(s):
    return re.sub(r"[\s\u3000]", "", s or "")


def split_sentences(text):
    parts = re.split(r"(?<=[。！？!?])|\n+", text or "")
    return [p.strip() for p in parts if p and p.strip()]


def _bigrams(s):
    s = _norm(s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _sim(a, b):
    A, B = _bigrams(a), _bigrams(b)
    return len(A & B) / len(A) if A else 0.0   # claim 側の bigram がどれだけ文に含まれるか


def locate_passage(article, claim):
    """claim に対応する修正後の文章(該当文+前後の文)と、最も近い文との類似度を返す。"""
    sentences = [("title", article.get("title", "")), ("dek", article.get("dek", ""))]
    sentences += [("body", s) for s in split_sentences(article.get("body", ""))]
    probe = " ".join(x for x in (claim.get("claim"), claim.get("articleValue")) if x)
    best_i, best = -1, 0.0
    for i, (_, s) in enumerate(sentences):
        sc = _sim(probe, s)
        if sc > best:
            best_i, best = i, sc
    if best_i < 0:
        return "", 0.0
    lo, hi = max(0, best_i - SNIPPET_CONTEXT), min(len(sentences), best_i + SNIPPET_CONTEXT + 1)
    return "".join(s for _, s in sentences[lo:hi]), best


# ---------- 数値・日付の比較 ----------

NUM_UNIT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(台|歳|年|月|日|発|km|キロ|キロメートル|m|メートル|分|時間|円|館|種|本|席|人|店舗|件|%|％)")
TIME_RE = re.compile(r"(\d{1,2})[:：](\d{2})")
APPROX_WORDS = fa.APPROX_WORDS


def numbers_with_units(text):
    out = []
    for n, u in NUM_UNIT_RE.findall(text or ""):
        try:
            v = float(n.replace(",", ""))
        except ValueError:
            continue
        u = {"キロ": "km", "キロメートル": "km", "メートル": "m", "％": "%"}.get(u, u)
        out.append((v, u))
    return out


def times(text):
    return {f"{int(h):02d}:{m}" for h, m in TIME_RE.findall(text or "")}


def numeric_resolution(passage, primary_value):
    """一次情報の数値(単位つき)が修正後の該当箇所と一致/概数として両立すれば True。"""
    pv = numbers_with_units(primary_value)
    if not pv:
        return False
    got = numbers_with_units(passage)
    approx = any(w in passage for w in APPROX_WORDS)
    for v, u in pv:
        ok = False
        for gv, gu in got:
            if gu != u:
                continue
            if gv == v or (approx and v and abs(gv - v) / v <= fa.APPROX_TOLERANCE):
                ok = True
                break
        if not ok:
            return False
    return True


# ---------- コード判定 ----------

def code_check(article, claim, primary_text_available=True):
    """API を使わずに判定できれば (result, reason) を返す。判定できなければ None。"""
    full_text = _norm(article.get("title", "") + article.get("dek", "") + article.get("body", ""))
    passage, sim = locate_passage(article, claim)
    old = _norm(claim.get("articleValue"))
    pv = claim.get("primaryValue") or ""

    # 1) 旧記述が残っている → 未修正
    if old and len(old) >= 2 and old in full_text:
        return "still_contradicted", f"旧記述「{claim.get('articleValue')}」が記事に残っている"
    # 2) 一次情報の値そのものが該当箇所に入っている
    if pv and len(_norm(pv)) >= 2 and _norm(pv) in _norm(passage):
        return "resolved", "一次情報の値と一致する記述に修正済み"
    # 3) 時刻の完全一致(営業時間など)
    if times(pv) and times(pv) <= times(passage):
        return "resolved", "時刻が一次情報と一致"
    # 4) 数値・概数・年齢・日付(単位つき)の一致
    if numeric_resolution(passage, pv):
        return "resolved", "数値が一次情報と一致(概数の許容範囲を含む)"
    # 5) 該当する記述自体が見当たらない → 削除で解消
    if sim < REMOVED_SIM_THRESHOLD:
        return "resolved", "誤っていた記述が記事から削除されている"
    # 6) 一次情報を確認できない
    if not primary_text_available:
        return "review_required", "一次情報URLを取得できず、意味的な照合ができない"
    return None


# ---------- 一次情報の取得と抜粋 ----------

def default_fetcher(url):
    """一次情報ページを取得してプレーンテキストを返す。失敗時は None。"""
    if not url or not url.startswith(("http://", "https://")) or fa.g.is_secondary_media(url):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            raw = r.read(2_000_000).decode(r.headers.get_content_charset() or "utf-8", "ignore")
    except Exception:
        return None
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    text = html_lib.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def primary_excerpt(text, claim):
    """一次情報の本文から、claim に関係する部分だけを抜き出す(記事ではなく一次情報側の抜粋)。"""
    if not text:
        return ""
    probe = " ".join(x for x in (claim.get("claim"), claim.get("primaryValue")) if x)
    sentences = split_sentences(text.replace(" ", "\n")) or [text]
    scored = sorted(((_sim(probe, s), i) for i, s in enumerate(sentences)), reverse=True)
    picked = sorted(i for sc, i in scored[:6] if sc > 0)
    out, total = [], 0
    for i in picked:
        s = sentences[i]
        if total + len(s) > PRIMARY_EXCERPT_CHARS:
            break
        out.append(s)
        total += len(s)
    return " … ".join(out)


# ---------- API(記事単位で1回) ----------

def build_verify_prompt(article, items):
    lines = [f"記事: id:{article['id']}「{article.get('title', '')}」", ""]
    for n, it in enumerate(items):
        c = it["claim"]
        lines += [
            f"[{n}] 前回の指摘: {c.get('claim', '')}",
            f"    記事側の旧記述: {c.get('articleValue', '')}",
            f"    一次情報の記述: {c.get('primaryValue', '')}",
            f"    一次情報URL: {c.get('primaryUrl', '')}",
            f"    修正後の該当箇所: {it['passage']}",
            f"    一次情報の抜粋: {it['excerpt'] or '(抜粋なし)'}",
            "",
        ]
    return "\n".join(lines)


def call_verify_api(client, article, items, model=None):
    prompt = build_verify_prompt(article, items)
    resp = client.messages.create(
        model=model or fa.MODEL, max_tokens=2000, system=VERIFY_SYSTEM_PROMPT,
        tools=[VERIFY_TOOL], tool_choice={"type": "tool", "name": "submit_verification"},
        messages=[{"role": "user", "content": prompt}],
    )
    usage = getattr(resp, "usage", None)
    stats = {
        "input_chars": len(VERIFY_SYSTEM_PROMPT) + len(prompt),
        "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
    }
    raw = None
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "submit_verification":
            raw = block.input
    return raw, stats


# 判定理由に「両立する」旨が書かれているかの簡易判定(否定形は除く)
COMPATIBLE_PHRASES = ("両立する", "両立している", "ほぼ両立", "概ね両立", "おおむね両立", "矛盾しない",
                      "矛盾はない", "表現の違い", "表現が異なるだけ", "同じ意味", "実質的に同じ", "ほぼ一致")
INCOMPATIBLE_PHRASES = ("両立しない", "両立せず", "両立しておらず", "矛盾する", "矛盾している", "食い違")


def reason_suggests_compatible(reason):
    r = reason or ""
    return any(p in r for p in COMPATIBLE_PHRASES) and not any(p in r for p in INCOMPATIBLE_PHRASES)


def reconcile_result(res, compatible, reason):
    """API の result と、両立フラグ・判定理由の整合を取る。
    - still_contradicted なのに logicallyCompatible=true → resolved(表現差として許容。Fact Audit v2 の
      wording_difference と同じ扱い)
    - still_contradicted なのにフラグが無く、理由が「両立する」旨 → review_required(自己矛盾のため人が確認)
    - resolved なのに logicallyCompatible=false → review_required"""
    if res == "still_contradicted":
        if compatible is True:
            return "resolved", (reason + " [自動: 両立すると判断されているため resolved]").strip()
        if compatible is None and reason_suggests_compatible(reason):
            return "review_required", (reason + " [自動: 理由と判定が矛盾するため要確認]").strip()
    if res == "resolved" and compatible is False:
        return "review_required", (reason + " [自動: 両立しないと判断されているため要確認]").strip()
    return res, reason


def normalize_verify_response(raw, n_items):
    """API 応答を index → (result, reason) に変換する。崩れていても落ちない。"""
    out = {}
    raw = fa._maybe_json(raw)
    results = fa._maybe_json(raw.get("results")) if isinstance(raw, dict) else None
    if not isinstance(results, list):
        return out, [f"応答の形式が想定外({type(raw).__name__})"]
    anomalies = []
    for r in results:
        r = fa._maybe_json(r)
        if not isinstance(r, dict):
            anomalies.append(f"要素がdictではない: {fa._snippet(r)}")
            continue
        idx, res = r.get("index"), r.get("result")
        if not isinstance(idx, int) or not (0 <= idx < n_items):
            anomalies.append(f"index が不正: {fa._snippet(idx)}")
            continue
        if res not in VERIFY_RESULTS:
            anomalies.append(f"result が不正: {fa._snippet(res)}")
            res = "review_required"
        compatible = r.get("logicallyCompatible")
        if isinstance(compatible, str):
            compatible = {"true": True, "false": False}.get(compatible.strip().lower())
        elif not isinstance(compatible, bool):
            compatible = None
        reason = str(r.get("reason") or "")
        out[idx] = reconcile_result(res, compatible, reason)
    return out, anomalies


# ---------- 実行 ----------

def verify_article(article, claims, client_getter, fetcher, stats):
    """1記事分の verify。client_getter は API が必要になった時だけ呼ばれる。"""
    fetched = {}
    results, pending = [], []
    for c in claims:
        url = c.get("primaryUrl") or ""
        if url not in fetched:
            fetched[url] = None  # コード判定で済めば取得しない(遅延取得)
        verdict = code_check(article, c, primary_text_available=True)
        if verdict is None:
            if fetched[url] is None and url:
                fetched[url] = fetcher(url) or ""
            text = fetched.get(url) or ""
            verdict = code_check(article, c, primary_text_available=bool(text))
            if verdict is None:
                passage, _ = locate_passage(article, c)
                pending.append({"claim": c, "passage": passage, "excerpt": primary_excerpt(text, c)})
                continue
        res, reason = verdict
        results.append(dict(c, verifyResult=res, method="code", verifyReason=reason))
        stats["code_resolved" if res == "resolved" else "code_decided"] += 1

    if pending:
        try:
            raw, usage = call_verify_api(client_getter(), article, pending)
            stats["api_calls"] += 1
            stats["api_claims"] += len(pending)
            stats["api_input_chars"] += usage["input_chars"]
            for k in ("input_tokens", "output_tokens"):
                if isinstance(usage.get(k), int):
                    stats[f"api_{k}"] += usage[k]
            decided, anomalies = normalize_verify_response(raw, len(pending))
            for a in anomalies:
                print(f"[anomaly] id:{article['id']} verify応答: {a}", flush=True)
        except Exception as e:
            print(f"[anomaly] id:{article['id']} verify API例外: {type(e).__name__}: {fa._snippet(str(e))}", flush=True)
            decided = {}
        for n, it in enumerate(pending):
            res, reason = decided.get(n, ("review_required", "API応答から判定を取得できなかった"))
            results.append(dict(it["claim"], verifyResult=res, method="api", verifyReason=reason,
                                verifiedPassage=it["passage"]))
    return results


def article_verdict(claim_results):
    rs = [c["verifyResult"] for c in claim_results]
    if not rs:
        return "resolved"
    if "still_contradicted" in rs:
        return "still_contradicted"
    if "review_required" in rs:
        return "review_required"
    return "resolved"


def new_stats():
    return {k: 0 for k in ("articles", "target_claims", "code_resolved", "code_decided",
                           "api_calls", "api_claims", "api_input_chars", "api_input_tokens",
                           "api_output_tokens", "skipped_no_previous", "skipped_no_target")}


def build_verify_report(results, stats, stamp):
    counts = {k: 0 for k in VERIFY_RESULTS}
    for r in results:
        for c in r.get("claims", []):
            counts[c["verifyResult"]] = counts.get(c["verifyResult"], 0) + 1
    tok = (f"{stats['api_input_tokens']:,} tokens(実測)" if stats["api_input_tokens"]
           else f"約{stats['api_input_chars'] // 2:,} tokens(文字数からの推定)")
    lines = [
        f"# Fact Audit verify レポート({stamp})", "",
        "| 項目 | 件数 |", "|---|---|",
        f"| 対象記事数 | {stats['articles']} |",
        f"| 対象contradicted claim数 | {stats['target_claims']} |",
        f"| APIで再判定したclaim数 | {stats['api_claims']}(API呼び出し {stats['api_calls']}回) |",
        f"| コードだけで解決したclaim数 | {stats['code_resolved']} |",
        f"| コードだけで判定したclaim数(未解決・要確認) | {stats['code_decided']} |",
        f"| resolved | {counts['resolved']} |",
        f"| still_contradicted | {counts['still_contradicted']} |",
        f"| review_required | {counts['review_required']} |",
        f"| APIへの入力 | {stats['api_input_chars']:,}文字 / {tok} |",
        "",
    ]
    if stats["skipped_no_previous"]:
        lines.append(f"※前回結果が見つからずスキップした記事: {stats['skipped_no_previous']}件(full での監査が必要)")
    if stats["skipped_no_target"]:
        lines.append(f"※前回 contradicted が無くスキップした記事: {stats['skipped_no_target']}件")
    lines.append("")
    for r in results:
        if r["verdict"] == "resolved":
            continue
        lines.append(f"## [{r['verdict']}] id:{r['id']} {r.get('title', '')}")
        for c in r["claims"]:
            if c["verifyResult"] != "resolved":
                lines.append(f"- {c['verifyResult']}({c['method']}): {c.get('claim', '')} — {c.get('verifyReason', '')}")
        lines.append("")
    return "\n".join(lines), counts


def run_verify(articles, previous_paths, out_dir, stamp, client=None, fetcher=None, sleep_sec=0):
    import time
    full_prev, verify_prev = load_previous_for_verify(previous_paths)
    fetcher = fetcher or default_fetcher
    holder = {"client": client}

    def client_getter():
        if holder["client"] is None:
            holder["client"] = fa.make_client()
        return holder["client"]

    stats = new_stats()
    results = []
    ckpt = os.path.join(out_dir, f"fact_verify_{stamp}.jsonl")
    for a in articles:
        fp, vp = full_prev.get(a["id"]), verify_prev.get(a["id"])
        if fp is None and vp is None:
            stats["skipped_no_previous"] += 1
            print(f"[verify] id:{a['id']} 前回結果なし → スキップ", flush=True)
            continue
        claims = target_claims(fp, vp)
        if not claims:
            stats["skipped_no_target"] += 1
            continue
        stats["articles"] += 1
        stats["target_claims"] += len(claims)
        print(f"[verify] id:{a['id']} 対象claim {len(claims)}件", flush=True)
        cr = verify_article(a, claims, client_getter, fetcher, stats)
        r = {"mode": "verify", "id": a["id"], "slug": a.get("slug", ""), "title": a.get("title", ""),
             "verdict": article_verdict(cr), "claims": cr, "rulesVersion": fa.RULES_VERSION}
        results.append(r)
        fa.append_checkpoint(ckpt, r)
        if sleep_sec and any(c["method"] == "api" for c in cr):
            time.sleep(sleep_sec)

    with open(os.path.join(out_dir, f"fact_verify_{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump({"stats": stats, "results": results}, f, ensure_ascii=False, indent=1)
    report, counts = build_verify_report(results, stats, stamp)
    with open(os.path.join(out_dir, f"fact_verify_{stamp}.md"), "w", encoding="utf-8") as f:
        f.write(report)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(report)
    print(report, flush=True)
    return results, stats, counts
