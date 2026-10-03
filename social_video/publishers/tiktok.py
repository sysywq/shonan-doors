# -*- coding: utf-8 -*-
"""TikTok(Content Posting API)。

post_mode(config.platforms.tiktok.post_mode)で切り替える:
  direct … Direct Post(/v2/post/publish/video/init/)。video.publish スコープが必要。
            アプリが監査(Audit)を通過するまでは privacy_level=SELF_ONLY(自分のみ)の投稿しかできない。
  upload … Upload(/v2/post/publish/inbox/video/init/)。video.upload スコープ。
            動画は投稿者の TikTok アプリの受信箱(下書き)に届き、キャプション入力と公開はアプリで人が行う。
            → 記録は submitted。キャプションは手動投稿キット(manual/tiktok.txt)からコピーする。

source(config.platforms.tiktok.source):
  FILE_UPLOAD   … MP4 をチャンク分割して PUT(既定。ドメイン認証不要)
  PULL_FROM_URL … R2 の公開URLを TikTok に取りに来させる(TikTok側でURLプレフィックスの所有確認が必要)

必要な環境変数(どちらか):
  TIKTOK_ACCESS_TOKEN(24時間有効。手動テスト向け)
  TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / TIKTOK_REFRESH_TOKEN(定期運用向け)
"""
from .base import AmbiguousPublish, MissingCredentials, PublishError, PublishResult, Publisher, clean_text, truncate

API = "https://open.tiktokapis.com/v2"
TITLE_MAX = 2200
SINGLE_CHUNK_MAX = 64 * 1024 * 1024
CHUNK_SIZE = 10 * 1024 * 1024
REFRESH_ENV = ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REFRESH_TOKEN")


def chunk_plan(size):
    """(chunk_size, total_chunk_count)。64MB以下は1チャンク、それ以上は10MBずつ(最後のチャンクが端数を含む)。"""
    if size <= SINGLE_CHUNK_MAX:
        return size, 1
    return CHUNK_SIZE, size // CHUNK_SIZE


class TikTokPublisher(Publisher):
    name = "tiktok"
    required_env = REFRESH_ENV

    def missing_credentials(self):
        if self._env("TIKTOK_ACCESS_TOKEN"):
            return []
        return [k for k in REFRESH_ENV if not self._env(k)]

    @property
    def post_mode(self):
        return self.cfg.get("post_mode", "upload")

    @property
    def source(self):
        return self.cfg.get("source", "FILE_UPLOAD")

    def build_post(self, manifest, asset):
        post = {"post_mode": self.post_mode, "source": self.source,
                "caption": truncate(clean_text(manifest["caption"]), TITLE_MAX)}
        if self.post_mode == "direct":
            post.update({k: self.cfg.get(k) for k in ("privacy_level", "disable_comment", "disable_duet",
                                                       "disable_stitch", "is_aigc")})
        return post

    def _access_token(self):
        if self._env("TIKTOK_ACCESS_TOKEN"):
            return self._env("TIKTOK_ACCESS_TOKEN")
        r = self._call("POST", f"{API}/oauth/token/", form={
            "client_key": self._env("TIKTOK_CLIENT_KEY"), "client_secret": self._env("TIKTOK_CLIENT_SECRET"),
            "grant_type": "refresh_token", "refresh_token": self._env("TIKTOK_REFRESH_TOKEN"),
        }, what="アクセストークン取得", timeout=30).json()
        if not r.get("access_token"):
            raise PublishError("tiktok: アクセストークンを取得できませんでした(refresh token の失効の可能性)")
        return r["access_token"]

    def _api(self, token, path, body, *, final=False, what=""):
        r = self._call("POST", f"{API}{path}", headers={"Authorization": f"Bearer {token}"},
                       json_body=body, final=final, what=what, timeout=60).json()
        err = r.get("error") or {}
        if err.get("code") not in (None, "", "ok"):
            raise PublishError(f"tiktok: {what} が失敗しました: {err.get('code')} {self._redact(err.get('message', ''))}")
        return r.get("data") or {}

    def publish(self, manifest, asset):
        if self.missing_credentials():
            raise MissingCredentials(self.name, self.missing_credentials())
        token = self._access_token()
        post = self.build_post(manifest, asset)

        if self.source == "PULL_FROM_URL":
            if not (asset.public_url or "").startswith("https://"):
                raise PublishError("tiktok: PULL_FROM_URL には公開URL(https)の動画が必要です")
            data, source_info = None, {"source": "PULL_FROM_URL", "video_url": asset.public_url}
        else:
            data = self._video_bytes(asset)
            chunk_size, count = chunk_plan(len(data))
            source_info = {"source": "FILE_UPLOAD", "video_size": len(data), "chunk_size": chunk_size,
                           "total_chunk_count": count}

        if self.post_mode == "direct":
            info = self._api(token, "/post/publish/creator_info/query/", {}, what="creator_info")
            options = info.get("privacy_level_options") or []
            if post["privacy_level"] not in options:
                raise PublishError(f"tiktok: privacy_level={post['privacy_level']} はこのアカウントで使えません"
                                   f"(利用可能: {options})。未監査アプリは SELF_ONLY のみです")
            body = {"post_info": {"title": post["caption"], "privacy_level": post["privacy_level"],
                                  "disable_comment": bool(post["disable_comment"]),
                                  "disable_duet": bool(post["disable_duet"]),
                                  "disable_stitch": bool(post["disable_stitch"]),
                                  "is_aigc": bool(post["is_aigc"])},
                    "source_info": source_info}
            path = "/post/publish/video/init/"
        else:
            body, path = {"source_info": source_info}, "/post/publish/inbox/video/init/"

        # PULL_FROM_URL では init が投稿の開始そのものなので、応答不明は unknown
        init = self._api(token, path, body, final=data is None, what="init")
        publish_id = init.get("publish_id")
        if not publish_id:
            raise PublishError("tiktok: publish_id が返りませんでした")

        if data is not None:
            upload_url = init.get("upload_url")
            if not upload_url:
                raise PublishError("tiktok: upload_url が返りませんでした")
            chunk_size, count = source_info["chunk_size"], source_info["total_chunk_count"]
            for i in range(count):
                start = i * chunk_size
                end = len(data) if i == count - 1 else start + chunk_size
                self._call("PUT", upload_url, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(end - start),
                    "Content-Range": f"bytes {start}-{end - 1}/{len(data)}",
                }, data=data[start:end], final=(i == count - 1), what=f"チャンク{i + 1}/{count}のアップロード", timeout=600)

        def check():
            st = self._api(token, "/post/publish/status/fetch/", {"publish_id": publish_id}, what="status")
            return st if st.get("status") in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX", "FAILED") else None

        try:
            st = self._poll(check, interval=self.cfg.get("poll_interval_sec", 10), timeout=self.cfg.get("poll_timeout_sec", 600))
        except PublishError as e:
            # アップロード完了後に状態を確認できない場合、投稿の有無が分からないため unknown にする
            raise AmbiguousPublish(str(e)) from None
        if st is not None and st.get("status") == "FAILED":
            raise PublishError(f"tiktok: 投稿処理に失敗しました: {st.get('fail_reason', '')}")
        if st is None:
            return PublishResult("submitted", post_id=publish_id, detail="TikTok側で処理中です")
        if st.get("status") == "SEND_TO_USER_INBOX":
            return PublishResult("submitted", post_id=publish_id,
                                 detail="TikTokアプリの受信箱に下書きとして送付済み。アプリでキャプションを入れて投稿してください")
        ids = st.get("publicaly_available_post_id") or []
        post_id = str(ids[0]) if ids else publish_id
        return PublishResult("published", post_id=post_id, detail=f"privacy_level={post.get('privacy_level', '')}",
                             extra={"publish_id": publish_id})
