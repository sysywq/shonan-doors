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

再開:
  python fact_audit.py --start 1 --end 40 --resume audit_reports/
  … 過去結果で confirmed/fix/rewrite 済みの記事は再監査せず引き継ぎ、
    review_required と未処理の記事だけを監査する
"""
import argparse
import json
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
                        "status": {
                            "type": "string",
                            "enum": ["confirmed", "not_found_in_primary", "contradicted"],
                            "description": "confirmed=一次情報で確認できた / not_found_in_primary=一次情報に記載なし(他メディア由来の疑い) / contradicted=一次情報と食い違う",
                        },
                        "primaryUrl": {"type": "string", "description": "確認できた一次情報のURL。無ければ空文字"},
                        "note": {"type": "string", "description": "補足(食い違いの内容など)"},
                    },
                    "required": ["claim", "status", "primaryUrl", "note"],
                },
            },
            "hasQuotedComment": {
                "type": "boolean",
                "description": "人物の発言を「」で引用している、または取材コメントを地の文で使っているか",
            },
            "verdict": {
                "type": "string",
                "enum": ["ok", "fix", "rewrite"],
                "description": "ok=問題なし / fix=該当箇所の削除・修正で足りる / rewrite=一次情報で書き直しが必要",
            },
            "summary": {"type": "string", "description": "判定理由を1〜2文で"},
        },
        "required": ["primarySources", "claims", "hasQuotedComment", "verdict", "summary"],
    },
}

SYSTEM_PROMPT = """あなたは地域メディア「湘南Doors」の校閲担当です。
渡された記事の事実の記述を、一次情報だけで照合してください。

【一次情報の定義】
その店舗・企業・団体・主催者・自治体自身が出している公式サイト、公式SNS、
本人が発表したプレスリリースのみ。新聞・ニュースサイト・地域まとめメディア・
ブログ・口コミサイト・Wikipedia等の他メディアは、たとえ内容が正しくても
根拠として扱わないこと(ツール側でも遮断されています)。

【手順】
1. 本文・dek・店舗情報欄から、確認が必要な事実の記述をすべて抜き出す
   (日付、場所、住所、営業時間、定休日、価格、席数、メニュー数、数値、
    沿革・経歴、設備や立地の説明、人物の発言、評判の記述など)。
   一般的な地理・歴史の常識や、編集部の主観的な感想は対象外。
2. 記事のlinkがあればまずそれを開き、足りなければweb_searchで公式の情報源を探して開く。
3. 各記述を confirmed / not_found_in_primary / contradicted に分類する。
   一次情報に書かれていない記述は、他メディア由来の疑いがあるので
   not_found_in_primary とすること(推測で confirmed にしない)。
4. 人物の発言の引用・取材コメントがあれば hasQuotedComment=true。
5. verdict: 全て confirmed かつ引用なし → ok / 一部削除・修正で済む → fix /
   記事の骨格が他メディア由来 → rewrite。
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
CLAIM_STATUSES = ("confirmed", "not_found_in_primary", "contradicted")
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
            claims.append({
                "claim": str(c.get("claim") or ""),
                "status": status,
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

    # verdict(モデル側の "ok" は "confirmed" として扱う)
    verdict = raw.get("verdict")
    if verdict == "ok":
        verdict = "confirmed"
    if verdict not in VERDICTS:
        note("verdict", verdict, "verdictが欠損または想定外の値")
        verdict = "review_required"

    summary = raw.get("summary")
    summary = summary if isinstance(summary, str) else ("" if summary is None else _snippet(summary))

    if anomalies:
        verdict = "review_required"
    return ({
        "verdict": verdict, "summary": summary, "claims": claims,
        "primarySources": sources, "hasQuotedComment": quoted, "anomalies": anomalies,
    }, anomalies)


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
            if isinstance(it, dict) and isinstance(it.get("id"), int):
                prev[it["id"]] = it
    return prev


# ---------- レポート ----------

def build_report(results, stamp):
    """正規化済みの結果からMarkdownレポートを作る。想定外の値があっても落ちない。"""
    order = {"rewrite": 0, "fix": 1, "review_required": 2, "confirmed": 3}
    counts = {v: 0 for v in VERDICTS}
    claim_counts = {}
    for r in results:
        v = r.get("verdict") if isinstance(r, dict) else None
        v = v if v in counts else "review_required"
        counts[v] += 1
        for c in (r.get("claims") or []) if isinstance(r, dict) else []:
            st = c.get("status") if isinstance(c, dict) else "unparsed"
            claim_counts[st] = claim_counts.get(st, 0) + 1

    lines = [f"# 一次情報照合レポート({stamp})", "",
             f"対象 {len(results)} 件",
             "判定: " + " / ".join(f"{k}: {counts[k]}件" for k in VERDICTS),
             "記述単位: " + (" / ".join(f"{k}: {v}件" for k, v in sorted(claim_counts.items())) or "なし"),
             ""]
    for r in sorted(results, key=lambda r: (order.get(r.get("verdict"), 2), r.get("id", 0))):
        if r.get("verdict") == "confirmed":
            continue
        lines.append(f"## [{r.get('verdict')}] id:{r.get('id')} {r.get('title', '')}")
        if r.get("summary"):
            lines.append(f"- 理由: {r['summary']}")
        if r.get("hasQuotedComment"):
            lines.append("- 人物の発言・取材コメントあり")
        for an in r.get("anomalies") or []:
            if isinstance(an, dict):
                lines.append(f"- 想定外の応答: {an.get('field')} (型:{an.get('type')}) {an.get('reason')} / {an.get('snippet')}")
        for c in r.get("claims") or []:
            if not isinstance(c, dict):
                lines.append(f"- unparsed: {_snippet(c)}")
                continue
            if c.get("status") != "confirmed":
                extra = f"({c['note']})" if c.get("note") else ""
                lines.append(f"- {c.get('status', 'unparsed')}: {c.get('claim', '')}{extra}")
        lines.append("")
    return "\n".join(lines), counts


def make_client():
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def main(argv=None, client=None, articles_override=None, sleep_sec=2):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--start", type=int, default=None)
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--resume", action="append", default=[],
                    help="過去の監査結果(.jsonl/.json/ディレクトリ)。confirmed/fix/rewrite済みの記事は再監査しない")
    ap.add_argument("--out-dir", default=OUT_DIR)
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
    ckpt_path = os.path.join(args.out_dir, f"fact_audit_{stamp}.jsonl")
    json_path = os.path.join(args.out_dir, f"fact_audit_{stamp}.json")
    md_path = os.path.join(args.out_dir, f"fact_audit_{stamp}.md")

    prev = load_previous_results(args.resume) if args.resume else {}
    results = []
    todo = []
    for a in articles:
        p = prev.get(a["id"])
        if isinstance(p, dict) and p.get("verdict") in COMPLETED_VERDICTS:
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
