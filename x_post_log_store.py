#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
X投稿済みログ(data/x_post_log.json)を、main へ直接 push せずに永続化する。
------------------------------------------------------------------------
X投稿は記事の本番公開(=mainへのマージ・公開確認)の後に行うため、投稿結果を同じ当日PRに入れることはできない。
そこで投稿済みログは、保護対象ではない専用のデータbranch(既定 bot/x-post-log)にだけ push する。
データbranchには data/x_post_log.json 1ファイルだけを置く(サイトのコードや記事は含まない)。

  pull … データbranchのログを手元の data/x_post_log.json に取り込む(article_id の和集合)。
         X投稿の直前に実行し、main 側のログとデータbranch側のログのどちらかにある記事は投稿済みとして扱う
         (二重投稿の防止)。Daily Articles は当日PRを作る前にも実行し、main のログをデータbranchに追いつかせる
  push … 手元の data/x_post_log.json をデータbranchへ push する(先にデータbranchの内容と和集合をとる)。
         作業ツリー・現在のbranchは変更しない(git の plumbing コマンドでコミットを作る)

main は Daily Articles の当日PR(pull 済みのログを含む)経由でだけ更新されるため、main への直接 push は発生しない。

使い方:
  python x_post_log_store.py pull
  python x_post_log_store.py push [--message "chore: X投稿済みログを更新"]
環境変数: X_POST_LOG_BRANCH(既定 bot/x-post-log) / X_POST_LOG_REMOTE(既定 origin)
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_REL = "data/x_post_log.json"
LOG_PATH = os.path.join(ROOT, LOG_REL)
BRANCH = os.environ.get("X_POST_LOG_BRANCH", "bot/x-post-log")
REMOTE = os.environ.get("X_POST_LOG_REMOTE", "origin")
PUSH_ATTEMPTS = 3


def merge_logs(*logs):
    """複数のログを article_id で和集合にする。同じ記事は最初に投稿した記録を残す。"""
    by_id = {}
    for log in logs:
        for r in (log or {}).get("posts") or []:
            if not isinstance(r, dict) or "article_id" not in r:
                continue
            cur = by_id.get(r["article_id"])
            if cur is None or str(r.get("posted_at", "")) < str(cur.get("posted_at", "")):
                by_id[r["article_id"]] = r
    return {"posts": sorted(by_id.values(), key=lambda r: (str(r.get("posted_at", "")), str(r["article_id"])))}


def _git(*args, input_text=None, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, input=input_text, capture_output=True, text=True, check=check)


def _read_local():
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"posts": []}


def _write_local(log):
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def fetch_remote():
    """データbranchを取得し、(コミットSHA, ログ) を返す。branchがまだ無ければ (None, None)。"""
    r = _git("fetch", "--no-tags", REMOTE, f"+refs/heads/{BRANCH}:refs/remotes/{REMOTE}/{BRANCH}", check=False)
    if r.returncode != 0:
        return None, None
    sha = _git("rev-parse", f"refs/remotes/{REMOTE}/{BRANCH}").stdout.strip()
    shown = _git("show", f"{sha}:{LOG_REL}", check=False)
    try:
        return sha, json.loads(shown.stdout) if shown.returncode == 0 else {"posts": []}
    except ValueError:
        return sha, {"posts": []}


def pull():
    _sha, remote = fetch_remote()
    if remote is None:
        print(f"データbranch {BRANCH} はまだありません(取り込むログなし)。")
        return 0
    local = _read_local()
    merged = merge_logs(local, remote)
    if merged != local:
        _write_local(merged)
    print(f"投稿済みログを取り込みました: 手元 {len(local.get('posts') or [])}件 + データbranch "
          f"{len(remote.get('posts') or [])}件 → {len(merged['posts'])}件")
    return 0


def _commit(log, parent, message):
    content = json.dumps(log, ensure_ascii=False, indent=2)
    blob = _git("hash-object", "-w", "--stdin", input_text=content).stdout.strip()
    data_tree = _git("mktree", input_text=f"100644 blob {blob}\tx_post_log.json\n").stdout.strip()
    root_tree = _git("mktree", input_text=f"040000 tree {data_tree}\tdata\n").stdout.strip()
    args = ["-c", "user.name=shonan-doors-bot", "-c", "user.email=bot@users.noreply.github.com",
            "commit-tree", root_tree, "-m", message]
    if parent:
        args += ["-p", parent]
    return _git(*args).stdout.strip()


def push(message):
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        parent, remote = fetch_remote()
        merged = merge_logs(_read_local(), remote)
        _write_local(merged)
        if remote is not None and merge_logs(remote) == merged:
            print("データbranchのログは最新です(push不要)。")
            return 0
        commit = _commit(merged, parent, message)
        r = _git("push", REMOTE, f"{commit}:refs/heads/{BRANCH}", check=False)
        if r.returncode == 0:
            print(f"投稿済みログ({len(merged['posts'])}件)をデータbranch {BRANCH} に保存しました。")
            return 0
        print(f"push に失敗しました({attempt}/{PUSH_ATTEMPTS})。取り直して再試行します: {r.stderr.strip()}",
              file=sys.stderr)
    print(f"::error::投稿済みログをデータbranch {BRANCH} に保存できませんでした", file=sys.stderr)
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["pull", "push"])
    ap.add_argument("--message", default="chore: X投稿済みログを更新")
    args = ap.parse_args(argv)
    return pull() if args.command == "pull" else push(args.message)


if __name__ == "__main__":
    sys.exit(main())
