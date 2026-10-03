# -*- coding: utf-8 -*-
"""
social_video
------------
公開済み記事 → short動画manifest → renderer → 9:16 MP4 → 公開メディアストレージ(R2) → 各SNS
という配信パイプラインの共通基盤。

記事の生成・公開フロー(Daily Articles / build.py / deploy)とは完全に独立しており、
このパッケージが失敗しても記事の公開には影響しない。

モジュール構成:
  articles.py           公開済み記事の読み込み(mergedInto・未来日付・終了イベントを除外)
  manifest.py           article_to_video_manifest(動画テイストに依存しない中間表現)
  renderers.py          renderer/provider interface と実装(ffmpeg_slideshow / command)
  storage.py            media storage interface と実装(Cloudflare R2 / ローカル)
  publishers/           各SNSの投稿アダプタ(Instagram / Facebook / YouTube / TikTok)
  distribution_log.py   配信ログ(article_id + video_asset_id + platform + post_id/status)
  log_store.py          配信ログをデータbranchへ永続化(main へは push しない)
  pipeline.py           モード(generate-only / publish-dry-run / publish-selected-platforms / full-auto)
  __main__.py           CLI(python3 -m social_video ...)

クリエイティブ(字幕デザイン・BGM・音声・尺・動画生成AI・テンプレート・投稿本数)は
social_video/config.json と renderer の差し替えで後から決める。
詳細は docs/social-video.md を参照。
"""
