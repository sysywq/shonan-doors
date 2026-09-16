#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日次自動投稿の「サイレント障害」を防ぐための検証スクリプト。

generate_articles.py が書き出す実行レポート(RUN_REPORT_PATH)をもとに、
「生成したと報告された記事」が実際に

  1. data/articles.json に永続化されているか
  2. build.py実行後、対応する記事ページ(articles/{slug}/index.html)が
     生成されているか
  3. git の変更(git status --porcelain)にそれらのファイルが実際に
     含まれているか

を確認する。1つでも不整合があれば、コミット・pushを行う前にこのスクリプトが
非ゼロ終了し、ワークフロー全体を失敗させる。

generate_articles.pyが「3件追加しました」と報告したにもかかわらず
git diffが0件、というような食い違いは、これまでは気づかれないまま
ワークフローがSuccess扱いになっていた。このスクリプトはその状態を
明示的なエラーに変える。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
RUN_REPORT_PATH = os.environ.get(
    "SHONAN_DOORS_RUN_REPORT_PATH",
    os.path.join("/tmp", "shonan_doors_run_report.json"),
)


def fail(msg):
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


def main():
    if not os.path.exists(RUN_REPORT_PATH):
        fail(
            f"実行レポート({RUN_REPORT_PATH})が見つかりません。"
            "generate_articles.pyが正常に完了したか確認してください。"
        )

    with open(RUN_REPORT_PATH, encoding="utf-8") as f:
        report = json.load(f)

    status = report.get("status")
    accepted_ids = report.get("accepted_ids", [])
    accepted_slugs = report.get("accepted_slugs", [])

    if status == "error":
        fail(f"generate_articles.pyがエラーを報告しています: {report.get('error')}")

    if not accepted_ids:
        print("今回追加された記事はありません(全件スキップ、または対象0件)。永続化検証は不要のためスキップします。")
        return

    # 1) articles.json に永続化されているか
    articles_path = os.path.join(ROOT, "data", "articles.json")
    with open(articles_path, encoding="utf-8") as f:
        articles = json.load(f)
    persisted_ids = {a["id"] for a in articles}
    missing_ids = [i for i in accepted_ids if i not in persisted_ids]
    if missing_ids:
        fail(
            f"data/articles.json に記事ID {missing_ids} が存在しません。"
            "generate_articles.pyは追加したと報告していますが、永続化されていません。"
        )
    print(f"[OK] data/articles.json に記事ID {accepted_ids} が存在することを確認しました。")

    # 2) build.py実行後、記事ページが生成されているか
    missing_pages = []
    for slug in accepted_slugs:
        page = os.path.join(ROOT, "articles", slug, "index.html")
        if not os.path.isfile(page):
            missing_pages.append(slug)
    if missing_pages:
        fail(f"build.py実行後も、以下の記事の静的ページが生成されていません: {missing_pages}")
    print(f"[OK] {len(accepted_slugs)}件すべての記事ページ(articles/{{slug}}/index.html)の生成を確認しました。")

    # 3) 実際にgit add(コミット対象へのステージング)された変更に、期待する
    #    ファイルが含まれているか。ここは必ず「git add実行後・git commit実行前」
    #    に呼び出すこと。`git status --porcelain`はstaged/unstagedを区別せず
    #    working tree全体の差分を返してしまうため、「build.pyは実行されたが
    #    git addの対象パスが不足していた」というケースを見逃してしまう。
    #    そのため、ここでは必ず `git diff --cached`(=staged分のみ)を見る。
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        fail(f"git diff --cachedの実行に失敗しました: {result.stderr}")
    staged = result.stdout

    if "data/articles.json" not in staged:
        fail(
            "git add後のステージ内容(git diff --cached)に data/articles.json の"
            "変更が含まれていません。ワークフローのgit addの対象パスを確認してください"
            "(このスクリプトは必ずgit add実行後・git commit実行前に呼び出す想定です)。"
        )
    for slug in accepted_slugs:
        if f"articles/{slug}/" not in staged and f"articles/{slug}" not in staged:
            fail(
                f"git add後のステージ内容に新規記事 articles/{slug}/ の変更が"
                "含まれていません。ワークフローのgit addの対象パスを確認してください。"
            )
    print("[OK] git add後のステージ内容(git diff --cached)に articles.json および新規記事ページの変更が含まれていることを確認しました。")

    print(
        f"\n検証OK: {len(accepted_ids)}件の新規記事(id: {accepted_ids})が、"
        "永続データ・生成ページ・gitの変更検知のすべてで一貫していることを確認しました。"
        "このあとのコミット・pushステップに進んで問題ありません。"
    )


if __name__ == "__main__":
    main()
