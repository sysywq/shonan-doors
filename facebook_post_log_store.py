#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Facebook投稿済みログ(data/facebook_post_log.json)を、main へ直接 push せずに永続化する。
------------------------------------------------------------------------
x_post_log_store.py(bot/x-post-log)と同じ考え方で、Facebook専用のデータbranch(既定 bot/facebook-post-log)
にだけ push する。データbranchには data/facebook_post_log.json 1ファイルだけを置く。
このログは main には入れない(Facebook投稿の直前に毎回データbranchから取り込む)。

  pull … データbranchのログを手元の data/facebook_post_log.json に取り込む(article_id の和集合)。
         X用と違い fail-closed: データbranchがあるのに取得・解析できない場合は終了コード1で止め、
         後続の投稿ステップを走らせない(ログが読めないまま投稿して二重投稿するのを防ぐ)。
  push … 手元のログをデータbranchへ push する(先にデータbranchの内容と和集合をとる)。
         作業ツリー・現在のbranchは変更しない(git の plumbing コマンドでコミットを作る)

使い方:
  python facebook_post_log_store.py pull
  python facebook_post_log_store.py push [--message "chore: Facebook投稿済みログを更新"]
環境変数: FACEBOOK_POST_LOG_BRANCH(既定 bot/facebook-post-log) / FACEBOOK_POST_LOG_REMOTE(既定 origin)
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_NAME = "facebook_post_log.json"
LOG_REL = f"data/{LOG_NAME}"
LOG_PATH = os.path.join(ROOT, LOG_REL)
BRANCH = os.environ.get("FACEBOOK_POST_LOG_BRANCH", "bot/facebook-post-log")
REMOTE = os.environ.get("FACEBOOK_POST_LOG_REMOTE", "origin")
PUSH_ATTEMPTS = 3


class StoreError(Exception):
    pass


def _rank(r):
    """同じ記事の記録が複数あるとき、投稿IDのある posted を最優先、次に早い記録を残す。"""
    posted = r.get("status", "posted") == "posted" and bool(r.get("facebook_post_id"))
    return (0 if posted else 1, str(r.get("posted_at", "")))


def merge_logs(*logs):
    """複数のログを article_id で和集合にする(どれか1つにでも記録がある記事は投稿済み扱い)。"""
    by_id = {}
    for log in logs:
        for r in (log or {}).get("posts") or []:
            if not isinstance(r, dict) or "article_id" not in r:
                continue
            cur = by_id.get(r["article_id"])
            if cur is None or _rank(r) < _rank(cur):
                by_id[r["article_id"]] = r
    return {"posts": sorted(by_id.values(), key=lambda r: (str(r.get("posted_at", "")), str(r["article_id"])))}


def _git(*args, input_text=None, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, input=input_text, capture_output=True, text=True, check=check)


def _read_local():
    if not os.path.exists(LOG_PATH):
        return {"posts": []}
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise StoreError(f"手元の {LOG_REL} を読めません: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("posts"), list):
        raise StoreError(f"手元の {LOG_REL} の形式が不正です")
    return data


def _write_local(log):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def remote_branch_exists(git=_git):
    """データbranchの有無。確認自体に失敗したら StoreError(「無い」と誤判定しない)。"""
    r = git("ls-remote", "--exit-code", "--heads", REMOTE, f"refs/heads/{BRANCH}", check=False)
    if r.returncode == 0:
        return True
    if r.returncode == 2:
        return False
    raise StoreError(f"データbranch {BRANCH} の有無を確認できません: {r.stderr.strip()}")


def fetch_remote(git=_git):
    """データbranchを取得し、(コミットSHA, ログ) を返す。branchがまだ無ければ (None, None)。"""
    if not remote_branch_exists(git):
        return None, None
    r = git("fetch", "--no-tags", REMOTE, f"+refs/heads/{BRANCH}:refs/remotes/{REMOTE}/{BRANCH}", check=False)
    if r.returncode != 0:
        raise StoreError(f"データbranch {BRANCH} を取得できません: {r.stderr.strip()}")
    sha = git("rev-parse", f"refs/remotes/{REMOTE}/{BRANCH}").stdout.strip()
    shown = git("show", f"{sha}:{LOG_REL}", check=False)
    if shown.returncode != 0:
        raise StoreError(f"データbranch {BRANCH} に {LOG_REL} がありません")
    try:
        data = json.loads(shown.stdout)
    except ValueError as e:
        raise StoreError(f"データbranch {BRANCH} の {LOG_REL} を解析できません: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("posts"), list):
        raise StoreError(f"データbranch {BRANCH} の {LOG_REL} の形式が不正です")
    return sha, data


def pull():
    try:
        _sha, remote = fetch_remote()
        local = _read_local()
    except StoreError as e:
        print(f"::error::{e}(二重投稿防止のため Facebook投稿を止めます)", file=sys.stderr)
        return 1
    if remote is None:
        print(f"データbranch {BRANCH} はまだありません(取り込むログなし)。")
        if not os.path.exists(LOG_PATH):
            _write_local({"posts": []})
        return 0
    merged = merge_logs(local, remote)
    if merged != local or not os.path.exists(LOG_PATH):
        _write_local(merged)
    print(f"Facebook投稿済みログを取り込みました: 手元 {len(local['posts'])}件 + データbranch "
          f"{len(remote['posts'])}件 → {len(merged['posts'])}件")
    return 0


def _commit(log, parent, message):
    content = json.dumps(log, ensure_ascii=False, indent=2)
    blob = _git("hash-object", "-w", "--stdin", input_text=content).stdout.strip()
    data_tree = _git("mktree", input_text=f"100644 blob {blob}\t{LOG_NAME}\n").stdout.strip()
    root_tree = _git("mktree", input_text=f"040000 tree {data_tree}\tdata\n").stdout.strip()
    args = ["-c", "user.name=shonan-doors-bot", "-c", "user.email=bot@users.noreply.github.com",
            "commit-tree", root_tree, "-m", message]
    if parent:
        args += ["-p", parent]
    return _git(*args).stdout.strip()


def push(message):
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        try:
            parent, remote = fetch_remote()
            merged = merge_logs(_read_local(), remote)
        except StoreError as e:
            print(f"取得に失敗しました({attempt}/{PUSH_ATTEMPTS}): {e}", file=sys.stderr)
            continue
        _write_local(merged)
        if remote is not None and merge_logs(remote) == merged:
            print("データbranchのログは最新です(push不要)。")
            return 0
        if remote is None and not merged["posts"]:
            print("保存する投稿記録がありません(push不要)。")
            return 0
        commit = _commit(merged, parent, message)
        r = _git("push", REMOTE, f"{commit}:refs/heads/{BRANCH}", check=False)
        if r.returncode == 0:
            print(f"Facebook投稿済みログ({len(merged['posts'])}件)をデータbranch {BRANCH} に保存しました。")
            return 0
        print(f"push に失敗しました({attempt}/{PUSH_ATTEMPTS})。取り直して再試行します: {r.stderr.strip()}",
              file=sys.stderr)
    print(f"::error::Facebook投稿済みログをデータbranch {BRANCH} に保存できませんでした", file=sys.stderr)
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["pull", "push"])
    ap.add_argument("--message", default="chore: Facebook投稿済みログを更新")
    args = ap.parse_args(argv)
    return pull() if args.command == "pull" else push(args.message)


if __name__ == "__main__":
    sys.exit(main())
