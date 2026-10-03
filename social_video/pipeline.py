# -*- coding: utf-8 -*-
"""配信パイプライン本体(モード・二重投稿防止・手動fallback)。

モード:
  generate-only               manifest → 動画生成 → (R2設定があれば)アップロード。投稿はしない。
                              有効な全媒体ぶんの手動投稿キットを作る(「生成は自動・投稿は手動」運用)。
  publish-dry-run             manifest → 動画生成(--skip-render で省略可)。アップロード・投稿・ログ更新はしない。
                              各媒体の credential 有無・二重投稿判定・送信予定の内容を表示する。
  publish-selected-platforms  --platforms で指定した媒体だけに投稿する。
  full-auto                   config で enabled かつ mode=auto の全媒体に投稿する(mode=manual は手動キット)。

1媒体の失敗で他媒体は止めない。失敗・結果不明があれば最後に終了コード1を返す。
"""
import hashlib
import json
import os
import subprocess

from . import articles as art
from .config import PLATFORMS
from .distribution_log import LOG_PATH, DistributionLog, load_log, save_log
from .http import HttpClient, redact
from .manifest import article_to_video_manifest, video_asset_id
from .publishers import PUBLISHERS, AmbiguousPublish, MissingCredentials, PublishError, VideoAsset
from .renderers import RenderError, get_renderer
from .storage import StorageError, get_storage, object_key
from .storage import MissingCredentials as StorageMissingCredentials

MODES = ("generate-only", "publish-dry-run", "publish-selected-platforms", "full-auto")
DEFAULT_OUT_DIR = os.path.join("out", "social_video")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def target_platforms(mode, cfg, selected=None):
    enabled = [p for p in PLATFORMS if cfg["platforms"].get(p, {}).get("enabled")]
    if mode == "publish-selected-platforms":
        if not selected:
            raise ValueError("publish-selected-platforms には --platforms が必要です")
        bad = [p for p in selected if p not in PLATFORMS]
        if bad:
            raise ValueError(f"未知のplatformです: {bad}(利用可能: {', '.join(PLATFORMS)})")
        return [p for p in PLATFORMS if p in selected]
    if selected:
        return [p for p in enabled if p in selected]
    return enabled


MANUAL_STEPS = {
    "instagram": "Instagramアプリ → 作成 → リール で動画を選び、キャプションを貼り付けて投稿",
    "facebook": "Facebookページ → リールを作成 で同じ動画を選び、説明を貼り付けて投稿",
    "youtube": "YouTube Studio / アプリ → ショート動画をアップロード。タイトル・説明を貼り付けて公開",
    "tiktok": "TikTokアプリ → 投稿(または受信箱の下書き)で動画を選び、キャプションを貼り付けて投稿。"
              "AIで生成した動画にする場合は「AI生成コンテンツ」ラベルをオンにする",
}


def write_manual_kit(out_dir, platform, manifest, asset, post, reason):
    """手動投稿キット(Markdown)。秘密情報は含めない。"""
    d = os.path.join(out_dir, str(manifest["article_id"]), "manual")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{platform}.md")
    text = post.get("caption") or post.get("description") or (post.get("snippet") or {}).get("description", "")
    title = (post.get("snippet") or {}).get("title", "")
    lines = [
        f"# 手動投稿キット: {platform} / 記事{manifest['article_id']}",
        "",
        f"- 理由: {reason}",
        f"- 記事: {manifest['article_url']}",
        f"- 動画ファイル: {os.path.basename(asset.local_path) if asset.local_path else '(未生成)'}"
        f"(workflow の artifact に含まれます)",
        f"- 動画の公開URL: {asset.public_url or '(R2未設定)'}",
        f"- video_asset_id: {asset.video_asset_id}",
        f"- 手順: {MANUAL_STEPS[platform]}",
        "",
    ]
    if title:
        lines += ["## タイトル", "", title, ""]
    lines += ["## キャプション / 説明", "", text, "",
              "## 投稿後(二重投稿防止のため記録する)", "",
              f"python3 -m social_video mark --article-id {manifest['article_id']} --platform {platform} "
              f"--asset-id {asset.video_asset_id} --status published --post-id <投稿ID>", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


class Pipeline:
    def __init__(self, cfg, *, http=None, environ=None, run=subprocess.run, sleep=None, clock=None,
                 log_path=LOG_PATH, out_dir=DEFAULT_OUT_DIR, articles=None, today=None, storage=None,
                 publishers=None, printer=print):
        self.cfg = cfg
        self.http = http or HttpClient()
        self.env = os.environ if environ is None else environ
        self.run_cmd = run
        self.sleep = sleep
        self.clock = clock
        self.log_path = log_path
        self.out_dir = out_dir
        self.articles = articles
        self.today = today
        self._storage = storage
        self.publishers = publishers or PUBLISHERS
        self._print = printer

    def say(self, msg):
        self._print(redact(msg, self.env))

    def _publisher(self, platform):
        kw = {"environ": self.env}
        if self.sleep:
            kw["sleep"] = self.sleep
        if self.clock:
            kw["clock"] = self.clock
        return self.publishers[platform](self.cfg["platforms"].get(platform, {}), self.http, **kw)

    def _storage_or_none(self):
        if self._storage is not None:
            return self._storage
        try:
            self._storage = get_storage(self.cfg, self.http, self.env)
        except StorageMissingCredentials as e:
            self.say(f"  [storage] {e}(公開URLが無いため Instagram/Facebook は手動投稿キットになります)")
            self._storage = False
        return self._storage

    # ------------------------------------------------------------------
    def run(self, article_ids, mode, *, platforms=None, skip_url_check=False, skip_render=False, force_repost=False):
        if mode not in MODES:
            raise ValueError(f"未知のmodeです: {mode}(利用可能: {', '.join(MODES)})")
        targets = target_platforms(mode, self.cfg, platforms)
        dry = mode == "publish-dry-run"
        log = DistributionLog(load_log(self.log_path))
        summary = []
        for aid in article_ids:
            try:
                summary.extend(self._run_article(aid, mode, targets, log, dry=dry, skip_url_check=skip_url_check,
                                                 skip_render=skip_render, force_repost=force_repost))
            except (art.ArticleNotPublished, RenderError, StorageError) as e:
                self.say(f"記事{aid}: スキップ({e})")
                summary.append({"article_id": aid, "platform": "-", "status": "error", "detail": str(e)})
            if not dry:
                save_log(log.data, self.log_path)
        self.say("\n== 結果 ==")
        for s in summary:
            self.say(f"  記事{s['article_id']} {s['platform']}: {s['status']} {s.get('post_id', '')} {s.get('detail', '')}".rstrip())
        exit_code = 1 if any(s["status"] in ("failed", "unknown", "error") for s in summary) else 0
        return exit_code, summary

    def _run_article(self, aid, mode, targets, log, *, dry, skip_url_check, skip_render, force_repost):
        skip_ended = self.cfg["manifest"].get("skip_ended_events", True)
        article = art.load_published_article(aid, articles=self.articles, today=self.today, skip_ended_events=skip_ended)
        if not skip_url_check:
            art.check_live(article, self.http)
        self.say(f"\n記事{article['id']}: {article['title']}")
        if targets and not dry and not force_repost and mode != "generate-only":
            blocked = {p: log.blocking_record(article["id"], p) for p in targets}
            if all(blocked.values()):
                self.say("  全対象媒体で投稿済みのため、動画生成もスキップします")
                return [{"article_id": article["id"], "platform": p, "status": "skipped_duplicate",
                         "post_id": r.get("post_id", "")} for p, r in blocked.items()]

        manifest = article_to_video_manifest(article, self.cfg)
        asset_id = video_asset_id(manifest, self.cfg)
        manifest["video_asset_id"] = asset_id
        adir = os.path.join(self.out_dir, str(article["id"]))
        os.makedirs(adir, exist_ok=True)
        with open(os.path.join(adir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        self.say(f"  manifest: {len(manifest['scenes'])}シーン / {manifest['duration_target_sec']}秒 / asset {asset_id}")

        video_path = os.path.abspath(os.path.join(adir, f"{asset_id}.mp4"))
        asset = VideoAsset(asset_id, article["id"], local_path="")
        if not skip_render:
            renderer = get_renderer(self.cfg, run=self.run_cmd, http=self.http)
            work = os.path.join(adir, "work")
            os.makedirs(work, exist_ok=True)
            meta = renderer.render(manifest, video_path, work)
            asset.local_path, asset.size_bytes, asset.sha256 = video_path, meta["size_bytes"], sha256_file(video_path)
            self.say(f"  動画: {os.path.basename(video_path)} {meta['width']}x{meta['height']} {meta['duration_sec']}秒 "
                     f"{meta['size_bytes'] // 1024}KB (renderer={renderer.name})")
        else:
            self.say("  動画生成をスキップしました(--skip-render)")

        key = object_key(self.cfg, article["id"], asset_id)
        storage = self._storage_or_none()
        if storage:
            if dry or not asset.local_path:
                asset.public_url = storage.public_url(key)
            elif storage.exists(key):
                asset.public_url = storage.public_url(key)
                self.say("  storage: 同じ動画がアップロード済みのため再利用します")
            else:
                asset.public_url = storage.put_file(key, asset.local_path)
                self.say(f"  storage: アップロードしました({storage.name})")
        if not dry and asset.local_path:
            log.add_asset({"video_asset_id": asset_id, "article_id": article["id"], "public_url": asset.public_url,
                           "storage_key": key if asset.public_url else "", "sha256": asset.sha256,
                           "size_bytes": asset.size_bytes, "renderer": self.cfg["renderer"]["name"]})

        results = []
        for platform in targets:
            results.append(self._one_platform(platform, mode, manifest, asset, log, dry=dry, force_repost=force_repost))
            if not dry:
                save_log(log.data, self.log_path)  # 途中で落ちても投稿済みの記録は残す
        return results

    def _one_platform(self, platform, mode, manifest, asset, log, *, dry, force_repost):
        aid = manifest["article_id"]
        pub = self._publisher(platform)
        pcfg = self.cfg["platforms"].get(platform, {})
        post = pub.build_post(manifest, asset)
        res = {"article_id": aid, "platform": platform}

        blocking = log.blocking_record(aid, platform)
        if blocking and not force_repost:
            self.say(f"  [{platform}] 二重投稿防止: 既に {blocking['status']}(post_id={blocking.get('post_id', '')})のためスキップ")
            return dict(res, status="skipped_duplicate", post_id=blocking.get("post_id", ""))

        missing = pub.missing_credentials()
        if dry:
            state = "manual(手動キット)" if pcfg.get("mode") == "manual" else (
                f"credential未設定: {', '.join(missing)}" if missing else "投稿可能")
            self.say(f"  [{platform}] dry-run: {state}")
            preview = {k: v for k, v in post.items() if k not in ("caption", "description")}
            self.say(f"    送信予定: {json.dumps(preview, ensure_ascii=False)[:400]}")
            text = post.get("caption") or post.get("description") or ""
            if text:
                self.say("    caption: " + text.replace("\n", " / ")[:200])
            return dict(res, status="dry_run", detail=state)

        def manual(reason, status="manual_pending", error=""):
            path = write_manual_kit(self.out_dir, platform, manifest, asset, post, reason)
            log.upsert(aid, asset.video_asset_id, platform, status, error=error or reason, mode=mode)
            self.say(f"  [{platform}] {status}: {reason} → 手動投稿キット {path}")
            return dict(res, status=status, detail=reason)

        if mode == "generate-only":
            return manual("generate-only(投稿は手動)")
        if pcfg.get("mode") == "manual":
            return manual("config で mode=manual")
        if missing:
            return manual(f"credential未設定: {', '.join(missing)}")

        try:
            r = pub.publish(manifest, asset)
        except MissingCredentials as e:
            return manual(str(e))
        except AmbiguousPublish as e:
            msg = redact(str(e), self.env)
            log.upsert(aid, asset.video_asset_id, platform, "unknown", error=msg, mode=mode)
            self.say(f"  [{platform}] unknown: {msg}(自動再投稿しません。確認後 mark で記録してください)")
            return dict(res, status="unknown", detail=msg)
        except PublishError as e:
            return manual(redact(str(e), self.env), status="failed")
        except Exception as e:  # 想定外の例外でも他の媒体は続ける
            return manual(redact(f"{type(e).__name__}: {e}", self.env), status="failed")

        log.upsert(aid, asset.video_asset_id, platform, r.status, post_id=r.post_id, permalink=r.permalink,
                   error="", mode=mode, extra={"detail": r.detail, **r.extra} if (r.detail or r.extra) else None)
        if r.status == "submitted" and platform == "tiktok":
            write_manual_kit(self.out_dir, platform, manifest, asset, post, r.detail)
        self.say(f"  [{platform}] {r.status}: post_id={r.post_id} {r.permalink} {r.detail}".rstrip())
        return dict(res, status=r.status, post_id=r.post_id, detail=r.detail)


def mark(log_path, article_id, platform, status, *, asset_id="", post_id="", permalink="", note=""):
    """手動投稿・結果不明(unknown)の確認結果をログに記録する。"""
    if platform not in PLATFORMS:
        raise ValueError(f"未知のplatformです: {platform}")
    log = DistributionLog(load_log(log_path))
    if not asset_id:
        assets = [a for a in log.data["assets"] if str(a.get("article_id")) == str(article_id)]
        asset_id = assets[-1]["video_asset_id"] if assets else "manual"
    r = log.upsert(int(article_id) if str(article_id).isdigit() else article_id, asset_id, platform, status,
                   post_id=post_id, permalink=permalink, error=note, mode="manual")
    save_log(log.data, log_path)
    return r
