# -*- coding: utf-8 -*-
"""配信ログ(data/social_video_log.json)を main へ直接 push せずに永続化する。

x_post_log_store.py と同じ方式で、保護対象ではない専用データbranch(既定 bot/social-video-log)に
data/social_video_log.json 1ファイルだけを置く。作業ツリー・現在のbranchは変更しない。

  python3 -m social_video log pull   … データbranchのログを手元へ取り込む(和集合)。配信の直前に実行する
  python3 -m social_video log push   … 手元のログをデータbranchへ保存する(先に和集合をとる)
環境変数: SOCIAL_VIDEO_LOG_BRANCH(既定 bot/social-video-log) / SOCIAL_VIDEO_LOG_REMOTE(既定 origin)
"""
import json
import os
import subprocess
import sys

from .config import ROOT
from .distribution_log import LOG_PATH, LOG_REL, load_log, merge_logs, save_log

BRANCH = os.environ.get("SOCIAL_VIDEO_LOG_BRANCH", "bot/social-video-log")
REMOTE = os.environ.get("SOCIAL_VIDEO_LOG_REMOTE", "origin")
PUSH_ATTEMPTS = 3


def _git(*args, input_text=None, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, input=input_text, capture_output=True, text=True, check=check)


def fetch_remote():
    r = _git("fetch", "--no-tags", REMOTE, f"+refs/heads/{BRANCH}:refs/remotes/{REMOTE}/{BRANCH}", check=False)
    if r.returncode != 0:
        return None, None
    sha = _git("rev-parse", f"refs/remotes/{REMOTE}/{BRANCH}").stdout.strip()
    shown = _git("show", f"{sha}:{LOG_REL}", check=False)
    try:
        return sha, json.loads(shown.stdout) if shown.returncode == 0 else {}
    except ValueError:
        return sha, {}


def pull(path=LOG_PATH):
    _sha, remote = fetch_remote()
    if remote is None:
        print(f"データbranch {BRANCH} はまだありません(取り込むログなし)。")
        return 0
    merged = merge_logs(load_log(path), remote)
    save_log(merged, path)
    print(f"配信ログを取り込みました: records {len(merged['records'])}件 / assets {len(merged['assets'])}件")
    return 0


def _commit(log, parent, message):
    content = json.dumps(log, ensure_ascii=False, indent=2) + "\n"
    blob = _git("hash-object", "-w", "--stdin", input_text=content).stdout.strip()
    data_tree = _git("mktree", input_text=f"100644 blob {blob}\t{os.path.basename(LOG_REL)}\n").stdout.strip()
    root_tree = _git("mktree", input_text=f"040000 tree {data_tree}\tdata\n").stdout.strip()
    args = ["-c", "user.name=shonan-doors-bot", "-c", "user.email=bot@users.noreply.github.com",
            "commit-tree", root_tree, "-m", message]
    if parent:
        args += ["-p", parent]
    return _git(*args).stdout.strip()


def push(message, path=LOG_PATH):
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        parent, remote = fetch_remote()
        merged = merge_logs(load_log(path), remote or {})
        save_log(merged, path)
        if remote is not None and merge_logs(remote) == merged:
            print("データbranchの配信ログは最新です(push不要)。")
            return 0
        commit = _commit(merged, parent, message)
        r = _git("push", REMOTE, f"{commit}:refs/heads/{BRANCH}", check=False)
        if r.returncode == 0:
            print(f"配信ログをデータbranch {BRANCH} に保存しました。")
            return 0
        print(f"push に失敗しました({attempt}/{PUSH_ATTEMPTS})。取り直して再試行します。", file=sys.stderr)
    print(f"::error::配信ログをデータbranch {BRANCH} に保存できませんでした", file=sys.stderr)
    return 1
