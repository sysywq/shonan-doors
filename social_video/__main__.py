# -*- coding: utf-8 -*-
"""CLI。

  # 動画生成だけ(投稿は手動。R2設定があればアップロードまで)
  python3 -m social_video run --article-ids 77 --mode generate-only
  # 投稿内容・credential・二重投稿判定の確認(投稿・アップロード・ログ更新なし)
  python3 -m social_video run --article-ids 77 --mode publish-dry-run [--skip-render]
  # 指定媒体だけ投稿
  python3 -m social_video run --article-ids 77 --mode publish-selected-platforms --platforms instagram,facebook
  # 有効な全媒体へ投稿
  python3 -m social_video run --latest 1 --mode full-auto

  python3 -m social_video manifest --article-id 77          # manifest をJSONで表示
  python3 -m social_video mark --article-id 77 --platform tiktok --status published --post-id 123
  python3 -m social_video log pull | push                     # 配信ログをデータbranchと同期
"""
import argparse
import json
import sys

from . import articles as art
from .config import PLATFORMS, load_config
from .distribution_log import LOG_PATH, STATUSES
from .manifest import article_to_video_manifest, video_asset_id
from .pipeline import MODES, DEFAULT_OUT_DIR, Pipeline, mark


def _ids(text):
    return [s.strip() for s in (text or "").split(",") if s.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m social_video")
    sub = ap.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="manifest → 動画 → storage → 各SNS")
    r.add_argument("--article-ids", default="", help="カンマ区切りの記事ID")
    r.add_argument("--latest", type=int, default=0, help="公開日の新しい公開記事からN件(schedule連携用)")
    r.add_argument("--mode", required=True, choices=MODES)
    r.add_argument("--platforms", default="", help=f"カンマ区切り({','.join(PLATFORMS)})")
    r.add_argument("--config", default="", help="上書き設定JSON")
    r.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    r.add_argument("--skip-url-check", action="store_true", help="本番URLのHTTP 200確認を省略")
    r.add_argument("--skip-render", action="store_true", help="動画生成を省略(publish-dry-run のみ)")
    r.add_argument("--force-repost", action="store_true", help="投稿済みの記録があっても再投稿する(要注意)")

    m = sub.add_parser("manifest", help="manifest をJSONで表示")
    m.add_argument("--article-id", required=True)
    m.add_argument("--config", default="")

    k = sub.add_parser("mark", help="手動投稿や結果不明(unknown)の確認結果をログに記録")
    k.add_argument("--article-id", required=True)
    k.add_argument("--platform", required=True, choices=PLATFORMS)
    k.add_argument("--status", required=True, choices=STATUSES)
    k.add_argument("--asset-id", default="")
    k.add_argument("--post-id", default="")
    k.add_argument("--permalink", default="")
    k.add_argument("--note", default="")

    lg = sub.add_parser("log", help="配信ログをデータbranchと同期")
    lg.add_argument("action", choices=["pull", "push"])
    lg.add_argument("--message", default="chore: short動画の配信ログを更新")

    args = ap.parse_args(argv)

    if args.command == "log":
        from . import log_store
        return log_store.pull() if args.action == "pull" else log_store.push(args.message)

    if args.command == "mark":
        rec = mark(LOG_PATH, args.article_id, args.platform, args.status, asset_id=args.asset_id,
                   post_id=args.post_id, permalink=args.permalink, note=args.note)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0

    cfg = load_config(args.config or None)
    if args.command == "manifest":
        a = art.load_published_article(args.article_id, skip_ended_events=cfg["manifest"].get("skip_ended_events", True))
        man = article_to_video_manifest(a, cfg)
        man["video_asset_id"] = video_asset_id(man, cfg)
        print(json.dumps(man, ensure_ascii=False, indent=2))
        return 0

    if args.skip_render and args.mode != "publish-dry-run":
        ap.error("--skip-render は publish-dry-run でのみ使えます")
    ids = _ids(args.article_ids)
    if args.latest:
        ids += [str(i) for i in art.latest_published_ids(
            args.latest, skip_ended_events=cfg["manifest"].get("skip_ended_events", True)) if str(i) not in ids]
    if not ids:
        ap.error("--article-ids か --latest を指定してください")
    code, _ = Pipeline(cfg, out_dir=args.out_dir).run(
        ids, args.mode, platforms=_ids(args.platforms), skip_url_check=args.skip_url_check,
        skip_render=args.skip_render, force_repost=args.force_repost)
    return code


if __name__ == "__main__":
    sys.exit(main())
