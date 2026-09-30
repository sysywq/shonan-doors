#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Articles の Fact Audit(full)と、confirmed 以外の記事の「記事単位の保留」
------------------------------------------------------------------------
generate_articles.py が公開前ゲートを通して articles.json に追加した記事(実行レポートの accepted_ids)を、
Fact Audit workflow(fact_audit.py)と同じ監査ロジック・同じ判定ルールで full 監査する。

  1. accepted_ids の記事を1件ずつ full 監査する(fact_audit.audit_one + normalize_result。
     公式サイト/公式SNSの画像内の文字も一次情報として扱う既存ルールのまま)
  2. 一次情報で修正内容が一意に決まる誤りは人に聞かずに自動修正し(publish_gate.safe_autofix)、
     修正箇所だけを verify モード(fact_verify.verify_article)で再確認する
  3. 最終判定が confirmed の記事だけを公開対象にする。confirmed 以外(fix / rewrite / review_required、
     監査できなかった記事を含む)は**その記事だけ**を保留にし、当日PRから外す。他の confirmed 記事は止めない

保留記事を当日PRから外すときの整理(confirmed 記事だけで build し直した状態を PR に載せるため):
  - data/articles.json   … 保留記事を取り除く(自動修正した記事は修正後の内容に置き換える)
  - data/stock_topics.json … 保留記事のテーマを status=held にする(Approve で generated / Reject で skipped)
  - data/event_series.json … 保留記事だけが今回新しく登録したシリーズを取り除く(Approve 時に登録し直す)
  - data/id_counter.json … そのまま(IDは再利用しない方針。保留記事は同じID・slugのまま Approve 後に公開する)
  - 生成HTML・sitemap.xml … このスクリプトの後に build.py を実行するので、保留記事のページは作られない
  - 実行レポート          … accepted_ids 等を公開対象だけに書き換え、held に保留記事を残す(shortfall も再計算)

最終 Fact Audit 後の confirmed が最低ライン(DAILY_MIN_ARTICLES)未満なら、post-audit top-up を行う:
  - 別候補を追加生成し、通常の公開前ゲート(generate_articles.run_publish_gate)→ Fact Audit(full)にかける
  - 最大 POST_AUDIT_TOPUP_MAX_ROUNDS 回・監査ドラフト合計 POST_AUDIT_TOPUP_MAX_DRAFTS 件まで。最低ラインに届いたら止める
  - 同日の公開対象・保留記事・公開前監査の不合格ドラフト・既存記事と重複する候補は採らない
  - IDは id_counter.json から新しく予約する(保留記事のIDを再利用しない)
  - 届かなければ confirmed だけを公開し、shortfall として報告する(品質基準は下げない)

保留記事の内容と監査結果は --holds-out(既定 /tmp/shonan_doors_daily_holds.json)に書き出す。
report_daily_holds.py がこれを読んで、保留記事1件につき Approve / Reject の Issue を1件作る。

監査結果は audit_reports/ に fact_audit.py と同じ形式で書き出す(Artifact 名 fact-audit-report)。
Fact Audit workflow の verify で、この Daily Articles の Run ID を previous_run_id に使える。

使い方:
  python daily_fact_audit.py
  python daily_fact_audit.py --report /tmp/shonan_doors_run_report.json --holds-out /tmp/holds.json
  DAILY_POST_AUDIT_TOPUP=0 python daily_fact_audit.py   # post-audit top-up を行わない

終了コード: 0=監査完了(保留があっても0) / 1=実行レポートの読み込み失敗など、監査そのものができなかった
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import generate_articles as g
import fact_audit as fa
import fact_verify as fv
import publish_gate as pg

RUN_REPORT_PATH = g.RUN_REPORT_PATH
HOLDS_PATH = os.environ.get("SHONAN_DOORS_DAILY_HOLDS_PATH", os.path.join("/tmp", "shonan_doors_daily_holds.json"))
# post-audit top-up の上限(APIコスト上限。品質基準は緩めず、試す候補の数だけを制限する)
POST_AUDIT_TOPUP_MAX_ROUNDS = int(os.environ.get("POST_AUDIT_TOPUP_MAX_ROUNDS", "3"))
POST_AUDIT_TOPUP_MAX_DRAFTS = int(os.environ.get("POST_AUDIT_TOPUP_MAX_DRAFTS", "6"))
POST_AUDIT_TOPUP_MAX_GATE_DRAFTS = int(os.environ.get("POST_AUDIT_TOPUP_MAX_GATE_DRAFTS", "8"))


# ---------- 1記事の監査 ----------

def audit_article(client, entry, confirmations=None, fetcher=None, image_fetcher=None):
    """1記事を full 監査し、一意に直せる誤りは自動修正 → verify で再確認する。
    戻り値: dict
      confirmed … True なら公開してよい
      entry     … 公開する記事(自動修正した場合は修正後。保留なら元の記事)
      verdict   … 最終判定(confirmed / fix / rewrite / review_required)
      result    … full 監査の正規化済み結果(fact_audit の形式)
      reasons / blocking / autofix / verify"""
    result = pg._audit_safely(client, entry, confirmations)
    passed, reasons, blocking = pg.evaluate(result)
    out = {"confirmed": passed, "entry": entry, "result": result, "reasons": reasons,
           "blocking": blocking, "autofix": [], "verify": []}
    if passed:
        return dict(out, verdict="confirmed")
    fixed, fixes = pg.safe_autofix(entry, result)
    if fixed is not None:
        # 修正後の確認は verify(前回 contradicted の claim だけを再判定する)。記事全体の再監査はしない。
        targets = [c for c in result.get("claims") or [] if c.get("status") == "contradicted"]
        kwargs = {"image_fetcher": image_fetcher} if image_fetcher else {}
        checked = fv.verify_article(fixed, targets, lambda: client, fetcher or fv.default_fetcher,
                                    fv.new_stats(), **kwargs)
        out.update(autofix=fixes, verify=checked)
        if fv.article_verdict(checked) == "resolved":
            return dict(out, confirmed=True, entry=fixed, verdict="confirmed", reasons=[])
        out["reasons"] = ["自動修正後の verify で未解消"] + reasons
    verdict = result.get("verdict") if isinstance(result, dict) else None
    if verdict not in fa.VERDICTS or verdict == "confirmed":
        # 記事単位では confirmed でも、公開の条件(人物の発言なし等)を満たさなければ要確認として保留する
        verdict = "review_required"
    return dict(out, verdict=verdict)


def hold_record(entry, audited, article_type, date, topic_id=None):
    """保留記事1件分の記録(Issue化と Approve 後の公開に使う)。"""
    result = audited.get("result") if isinstance(audited.get("result"), dict) else {}
    esc = pg.escalation(entry, result) if result else None
    record = {
        "id": entry.get("id"), "slug": entry.get("slug", ""), "title": entry.get("title", ""),
        "articleType": article_type, "area": entry.get("area", ""), "cat": entry.get("cat", ""),
        "date": date, "verdict": audited.get("verdict"), "reasons": audited.get("reasons") or [],
        "claims": [pg._claim_summary(c) for c in audited.get("blocking") or []],
        "summary": result.get("summary", ""), "primarySources": result.get("primarySources", []),
        "hasQuotedComment": bool(result.get("hasQuotedComment")), "anomalies": bool(result.get("anomalies")),
        "autofix": audited.get("autofix") or [], "escalation": esc, "stockTopicId": topic_id,
        # Approve 後はこの内容(公開前ゲートを通った元の記事)から再監査する
        "entry": entry,
    }
    if esc and esc.get("kind") == "image_reading":
        record["imageChecks"] = esc.get("imageChecks") or []
    return record


# ---------- 保留記事の分離 ----------

def base_event_series_keys(ref="HEAD"):
    """当日branchの起点(=main)の event_series.json にあるシリーズキー。取れなければ None。"""
    try:
        out = subprocess.run(["git", "show", f"{ref}:data/event_series.json"], cwd=g.ROOT,
                             capture_output=True, text=True, check=True).stdout
        data = json.loads(out)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
    return {s.get("seriesKey") for s in data if isinstance(s, dict)} if isinstance(data, list) else None


def separate_holds(articles, stock_topics, event_series, held, published, base_series_keys=None, now=""):
    """保留記事を公開対象から外したデータを返す。引数は変更しない。
    held      … 保留記事の記録(hold_record)のリスト
    published … 公開する記事(自動修正後の内容)のリスト。articles.json の同じIDを置き換える
    base_series_keys … main 側にあったシリーズキー(None なら、保留記事のタイトルで登録されたものだけを消す)
    戻り値: (articles, stock_topics, event_series)"""
    held_by_id = {h["id"]: h for h in held}
    fixed_by_id = {a["id"]: a for a in published}
    new_articles = [fixed_by_id.get(a.get("id"), a) for a in articles if a.get("id") not in held_by_id]

    new_topics = []
    for t in stock_topics:
        h = held_by_id.get(t.get("generatedArticleId"))
        if h is not None and t.get("status") == "generated":
            t = dict(t, status="held", heldAt=now,
                     holdReason="Fact Audit で保留: " + (" / ".join(h.get("reasons") or []) or str(h.get("verdict"))))
        new_topics.append(t)

    remaining_keys = {a.get("eventSeriesKey") for a in new_articles if a.get("eventSeriesKey")}
    held_series = {h["entry"].get("eventSeriesKey"): h["entry"].get("title", "") for h in held
                   if h["entry"].get("eventSeriesKey")}

    def introduced_by_hold(s):
        key = s.get("seriesKey")
        if key not in held_series or key in remaining_keys:
            return False
        if base_series_keys is not None:
            return key not in base_series_keys
        return s.get("canonicalName") == held_series[key]

    new_series = [s for s in event_series if not introduced_by_hold(s)]
    return new_articles, new_topics, new_series


def updated_report(report, published, held, results, topup=None, gate_rejected=None):
    """実行レポートを「公開対象だけ」の内容に書き換える(verify_daily_run.py・PR作成・X投稿が使う)。"""
    pub_ids = [a["id"] for a in published]
    news = [a for a in published if a.get("articleType") == "news"]
    stock = [a for a in published if a.get("articleType") == "stock"]
    out = dict(report)
    out.update(
        pre_audit_accepted_ids=report.get("pre_audit_accepted_ids", report.get("accepted_ids", [])),
        accepted_ids=pub_ids, accepted_slugs=[a["slug"] for a in published],
        news_ids=[a["id"] for a in news], news_slugs=[a["slug"] for a in news],
        stock_ids=[a["id"] for a in stock], stock_slugs=[a["slug"] for a in stock],
        news_count=len(news), stock_count=len(stock), total_count=len(published),
        held=[{k: h.get(k) for k in ("id", "slug", "title", "articleType", "verdict", "reasons")} for h in held],
        fact_audit={
            "mode": "full",
            "verdicts": {str(r["id"]): r["verdict"] for r in results},
            "autofixed_ids": [r["id"] for r in results if r["verdict"] == "confirmed" and r.get("autofix")],
            "all_published_confirmed": all(r["verdict"] == "confirmed" for r in results if r["id"] in pub_ids),
        },
    )
    if gate_rejected is not None:
        out["gate_rejected"] = gate_rejected
    if topup is not None:
        out["post_audit_topup"] = topup
    shortfall = report.get("shortfall") if isinstance(report.get("shortfall"), dict) else None
    if len(published) < g.DAILY_MIN_ARTICLES:
        shortfall = dict(shortfall or {"min": g.DAILY_MIN_ARTICLES, "target": g.DAILY_TARGET_ARTICLES, "reasons": []})
        shortfall["total"] = len(published)
        if held:
            shortfall["reasons"] = list(shortfall.get("reasons") or []) + [
                f"Fact Audit(full)で confirmed 以外の {len(held)}件を保留(公開対象から分離。品質基準は緩めていない)"]
        if topup and topup.get("rounds"):
            shortfall["reasons"] = list(shortfall.get("reasons") or []) + [
                f"Fact Audit 後の補充(post-audit top-up)を{topup['rounds']}回・監査{topup['drafts']}件試したが"
                f"最低ラインに届かなかった(confirmed {len(topup.get('confirmed_ids') or [])}件を追加。"
                f"停止理由: {topup.get('stopped') or '-'})"]
    else:
        shortfall = None
    out["shortfall"] = shortfall
    return out


# ---------- post-audit top-up ----------

def generate_topup(need, avoid, today, event_series, gate_rejections, round_no):
    """別候補を need 件まで追加生成し、通常の公開前ゲートに通った記事(ID未発行)を返す。
    avoid … 重複させない記事(既存記事・同日の公開対象・保留記事・不合格ドラフト)"""
    g.MAX_GATE_DRAFTS_PER_RUN = min(g.MAX_GATE_DRAFTS_PER_RUN, POST_AUDIT_TOPUP_MAX_GATE_DRAFTS)
    if g.gate_budget_exhausted():
        print(f"post-audit top-up: 公開前監査の件数上限({g.MAX_GATE_DRAFTS_PER_RUN}件)に達したため生成しません")
        return []
    entries, log_lines, _ = g.run_news_generation(
        avoid, today, event_series, gate_rejections=gate_rejections, count=need, max_refills=0,
        strict=False, label=f"post_audit_topup_{round_no}", defer_ids=True)
    for line in log_lines:
        print(line)
    return entries


def _duplicate_reason(entry, pool):
    return (g.is_duplicate(entry, pool, days=None) or g.check_series_year_duplicate(entry, pool)
            or g.find_same_subject(entry, pool))


# ---------- 実行 ----------

def _write_reports(out_dir, stamp, full_results, verify_results):
    os.makedirs(out_dir, exist_ok=True)
    for r in full_results:
        fa.append_checkpoint(os.path.join(out_dir, f"fact_audit_{stamp}.jsonl"), r)
    with open(os.path.join(out_dir, f"fact_audit_{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump(full_results, f, ensure_ascii=False, indent=1)
    report, _counts = fa.build_report(full_results, stamp)
    with open(os.path.join(out_dir, f"fact_audit_{stamp}.md"), "w", encoding="utf-8") as f:
        f.write(report)
    for r in verify_results:
        fa.append_checkpoint(os.path.join(out_dir, f"fact_verify_{stamp}.jsonl"), r)
    return report


def write_summary(published, held, results, topup=None):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = ["## Daily Articles: Fact Audit(full)", "",
             f"公開対象(confirmed) {len(published)}件 / 保留 {len(held)}件", ""]
    if topup:
        lines += [f"post-audit top-up: {topup['rounds']}回・監査{topup['drafts']}件 → confirmed "
                  f"{topup['confirmed_ids']} / 保留 {topup['held_ids']}(停止理由: {topup['stopped']})", ""]
    for r in results:
        mark = "公開" if r["verdict"] == "confirmed" else "保留"
        fixed = "(自動修正→verifyで解消)" if r["verdict"] == "confirmed" and r.get("autofix") else ""
        lines.append(f"- [{mark}] id:{r['id']} {r['verdict']}{fixed} {r['title']}")
    text = "\n".join(lines) + "\n\n"
    print(text)
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def main(argv=None, client=None, fetcher=None, image_fetcher=None, base_series_keys="git", topup=None,
         reserve_ids=None):
    """topup … post-audit top-up の候補生成関数(generate_topup と同じ引数)。None なら補充しない
    reserve_ids … 補充記事のID予約(既定 generate_articles.reserve_ids。id_counter.json を前進させる)"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=RUN_REPORT_PATH)
    ap.add_argument("--holds-out", default=HOLDS_PATH)
    ap.add_argument("--out-dir", default=fa.OUT_DIR)
    ap.add_argument("--articles", default=g.ARTICLES_JSON_PATH)
    ap.add_argument("--stock-topics", default=g.STOCK_TOPICS_PATH)
    ap.add_argument("--event-series", default=g.EVENT_SERIES_PATH)
    args = ap.parse_args(argv)

    try:
        with open(args.report, encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, ValueError) as e:
        print(f"::error::実行レポートを読み込めません({args.report}): {e}", file=sys.stderr)
        return 1
    ids = [i for i in report.get("accepted_ids") or [] if isinstance(i, int)]
    g.atomic_write_json(args.holds_out, [])
    if os.environ.get("DAILY_POST_AUDIT_TOPUP", "1") == "0":
        topup = None
    # 公開前ゲートで全件不合格だった日も、品質基準を変えずに別候補の補充は試す
    can_topup = topup is not None and report.get("status") in ("ok", "no_articles_passed_gate",
                                                                 "no_articles_accepted")
    if (report.get("status") != "ok" or not ids) and not can_topup:
        report = dict(report, held=[], fact_audit={"mode": "full", "verdicts": {}, "autofixed_ids": [],
                                                   "all_published_confirmed": True})
        g.atomic_write_json(args.report, report)
        print("Fact Audit の対象記事はありません(今回追加された記事なし)。")
        return 0

    articles = g.load_json(args.articles)
    by_id = {a["id"]: a for a in articles}
    missing = [i for i in ids if i not in by_id]
    if missing:
        print(f"::error::accepted_ids {missing} が data/articles.json にありません", file=sys.stderr)
        return 1
    stock_topics = g.load_json(args.stock_topics) if os.path.exists(args.stock_topics) else []
    event_series = g.load_json(args.event_series) if os.path.exists(args.event_series) else []
    topic_by_article = {t.get("generatedArticleId"): t.get("id") for t in stock_topics
                        if t.get("generatedArticleId") is not None}

    if client is None:
        client = fa.make_client()
    date = report.get("date") or datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    now = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M")
    # Manual recovery may declare already-live articles as a trusted baseline.
    # They were previously published through the normal audited path, so a recovery run
    # must preserve them and audit only newly generated replacement candidates.
    baseline_raw = os.environ.get("DAILY_BASELINE_CONFIRMED_IDS", "")
    baseline_ids = {int(x) for x in baseline_raw.split(",") if x.strip().isdigit()}
    baseline_ids = {i for i in baseline_ids if i in by_id and by_id[i].get("date") == date}
    published = [by_id[i] for i in ids if i in baseline_ids]
    held, results, full_results, verify_results = [], [], [], []
    for entry in published:
        results.append({"id": entry["id"], "title": entry.get("title", ""),
                        "verdict": "confirmed", "autofix": False, "baseline": True})

    def audit_and_sort(entry):
        i = entry["id"]
        print(f"[daily-audit] id:{i} {entry.get('title', '')}", flush=True)
        audited = audit_article(client, entry, fetcher=fetcher, image_fetcher=image_fetcher)
        full = dict(audited["result"], id=i, slug=entry.get("slug", ""), title=entry.get("title", ""))
        full_results.append(full)
        if audited["verify"]:
            verify_results.append({"mode": "verify", "id": i, "slug": entry.get("slug", ""),
                                   "title": entry.get("title", ""),
                                   "verdict": fv.article_verdict(audited["verify"]),
                                   "claims": audited["verify"], "rulesVersion": fa.RULES_VERSION})
        results.append({"id": i, "title": entry.get("title", ""), "verdict": audited["verdict"],
                        "autofix": audited["autofix"]})
        if audited["confirmed"]:
            published.append(audited["entry"])
        else:
            held.append(hold_record(entry, audited, entry.get("articleType", ""), date, topic_by_article.get(i)))
        return audited["confirmed"]

    for i in ids:
        if i not in baseline_ids:
            audit_and_sort(by_id[i])

    # ---- post-audit top-up: 最終 Fact Audit 後の confirmed が最低ライン未満なら、別候補を補充して監査する ----
    gate_rejected = list(report.get("gate_rejected") or [])
    topup_info = None
    if topup is not None and len(published) < g.DAILY_MIN_ARTICLES:
        reserve = reserve_ids or g.reserve_ids
        topup_info = {"rounds": 0, "drafts": 0, "added_ids": [], "confirmed_ids": [], "held_ids": [],
                      "stopped": ""}
        pool = list(articles)  # 既存記事+同日の公開対象+保留記事(分離前)。補充分も積んでいく
        while len(published) < g.DAILY_MIN_ARTICLES:
            if topup_info["rounds"] >= POST_AUDIT_TOPUP_MAX_ROUNDS:
                topup_info["stopped"] = f"補充回数の上限({POST_AUDIT_TOPUP_MAX_ROUNDS}回)"
                break
            left = POST_AUDIT_TOPUP_MAX_DRAFTS - topup_info["drafts"]
            if left <= 0:
                topup_info["stopped"] = f"監査ドラフトの上限({POST_AUDIT_TOPUP_MAX_DRAFTS}件)"
                break
            topup_info["rounds"] += 1
            round_no = topup_info["rounds"]
            need = min(g.DAILY_MIN_ARTICLES - len(published), left)
            print(f"post-audit top-up {round_no}/{POST_AUDIT_TOPUP_MAX_ROUNDS}: confirmed {len(published)}件 → "
                  f"別候補を最大{need}件補充します", flush=True)
            avoid = pool + [dict(r.get("draft") or r, id="gate-rejected") for r in gate_rejected
                            if isinstance(r, dict) and r.get("title")]
            try:
                candidates = topup(need, avoid, date, event_series, gate_rejected, round_no) or []
            except Exception as e:  # 補充の失敗で confirmed 記事の公開は止めない
                print(f"::warning::post-audit top-up {round_no} の候補生成に失敗: {type(e).__name__}: {e}")
                continue
            fresh = []
            for entry in candidates:
                dup = _duplicate_reason(entry, avoid + fresh)
                if dup:
                    print(f"post-audit top-up: 「{entry.get('title', '')}」は重複のため採らない({dup})")
                    continue
                fresh.append(entry)
            fresh = fresh[:need]
            if not fresh:
                continue
            used = {a.get("id") for a in pool} | {h["id"] for h in held}
            new_ids = reserve(len(fresh))
            if set(new_ids) & used or len(set(new_ids)) != len(new_ids):
                print(f"::error::補充記事に使用済みのIDが予約されました({new_ids})。補充を中止します", file=sys.stderr)
                topup_info["stopped"] = "ID予約の異常"
                break
            for entry, new_id in zip(fresh, new_ids):
                g.finalize_news_entry_id(entry, new_id)
                event_series, _ = g.register_event_series_if_new(entry, event_series)
            pool += fresh
            articles = articles + fresh
            topup_info["drafts"] += len(fresh)
            topup_info["added_ids"] += new_ids
            for entry in fresh:
                ok = audit_and_sort(entry)
                topup_info["confirmed_ids" if ok else "held_ids"].append(entry["id"])
        else:
            topup_info["stopped"] = "最低ラインに到達"
        print(f"post-audit top-up: {topup_info}")

    base_keys = base_event_series_keys() if base_series_keys == "git" else base_series_keys
    new_articles, new_topics, new_series = separate_holds(
        articles, stock_topics, event_series, held, published, base_keys, now)
    g.atomic_write_json(args.articles, new_articles)
    if os.path.exists(args.stock_topics) or new_topics:
        g.atomic_write_json(args.stock_topics, new_topics)
    if os.path.exists(args.event_series) or new_series:
        g.atomic_write_json(args.event_series, new_series)
    g.atomic_write_json(args.holds_out, held)
    base_report = report if report.get("status") == "ok" or not published else dict(report, status="ok")
    g.atomic_write_json(args.report, updated_report(base_report, published, held, results, topup_info,
                                                    gate_rejected if topup_info is not None else None))

    stamp = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d-%H%M%S")
    _write_reports(args.out_dir, stamp, full_results, verify_results)
    write_summary(published, held, results, topup_info)
    for h in held:
        print(f"::warning::id:{h['id']}「{h['title']}」は Fact Audit で {h['verdict']} のため保留しました"
              "(当日PRには含めず、Issue で Approve / Reject を確認します)")
    return 0


if __name__ == "__main__":
    sys.exit(main(topup=generate_topup))
