# -*- coding: utf-8 -*-
"""Facebook Page Reels(Graph API /{page-id}/video_reels)。

Instagram 用に生成・アップロードした同一MP4(R2の公開URL)を使う。
流れ: upload_phase=start → rupload へ file_url(ホスト済み動画)を渡す → upload_phase=finish(video_state=PUBLISHED)
      → status をポーリング(時間内に完了しなければ submitted として記録。Meta側で処理は続く)。

必要な環境変数:
  FACEBOOK_PAGE_ID             投稿先FacebookページのID
  FACEBOOK_PAGE_ACCESS_TOKEN   ページアクセストークン(pages_manage_posts / pages_read_engagement / pages_show_list)
"""
from ..http import HttpError, HttpTimeout
from .base import PublishError, PublishResult, Publisher, clean_text, truncate

DESCRIPTION_MAX = 2200


class FacebookPublisher(Publisher):
    name = "facebook"
    required_env = ("FACEBOOK_PAGE_ID", "FACEBOOK_PAGE_ACCESS_TOKEN")

    @property
    def version(self):
        return self.cfg.get("graph_api_version", "v23.0")

    @property
    def graph(self):
        return f"https://graph.facebook.com/{self.version}"

    def _auth(self):
        return {"Authorization": f"OAuth {self._env('FACEBOOK_PAGE_ACCESS_TOKEN')}"}

    def build_post(self, manifest, asset):
        return {"description": truncate(clean_text(manifest["caption"]), DESCRIPTION_MAX), "file_url": asset.public_url}

    def publish(self, manifest, asset):
        self.require_credentials()
        if not (asset.public_url or "").startswith("https://"):
            raise PublishError("facebook: 公開URL(https)の動画が必要です。R2 の設定を確認してください")
        page = self._env("FACEBOOK_PAGE_ID")
        post = self.build_post(manifest, asset)

        start = self._call("POST", f"{self.graph}/{page}/video_reels", headers=self._auth(),
                           form={"upload_phase": "start"}, what="upload start", timeout=60).json()
        video_id = start.get("video_id")
        if not video_id:
            raise PublishError(f"facebook: video_id がありません: {self._redact(start)}")
        upload_url = start.get("upload_url") or f"https://rupload.facebook.com/video-upload/{self.version}/{video_id}"

        up = self._call("POST", upload_url, headers=dict(self._auth(), file_url=asset.public_url),
                        what="動画アップロード(file_url)", timeout=300).json()
        if up.get("success") is not True:
            raise PublishError(f"facebook: アップロードが成功しませんでした: {self._redact(up)}")

        fin = self._call("POST", f"{self.graph}/{page}/video_reels", headers=self._auth(),
                         form={"upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED",
                               "description": post["description"]},
                         final=True, what="upload finish(公開)", timeout=120).json()
        if fin.get("success") is not True:
            raise PublishError(f"facebook: 公開リクエストが成功しませんでした: {self._redact(fin)}")

        def check():
            # 公開リクエストは受理済みなので、状態確認の一時的な失敗は「処理中」とみなして待ち続ける
            try:
                st = self.http.request("GET", f"{self.graph}/{video_id}?fields=status", headers=self._auth(),
                                       timeout=30).json().get("status") or {}
            except (HttpError, HttpTimeout, ValueError):
                return None
            if st.get("video_status") == "error" or (st.get("processing_phase") or {}).get("status") == "error":
                raise PublishError(f"facebook: 動画の処理に失敗しました: {self._redact(st)}")
            if (st.get("publishing_phase") or {}).get("status") == "complete":
                return "complete"
            return None

        permalink = f"https://www.facebook.com/reel/{video_id}"
        done = self._poll(check, interval=self.cfg.get("poll_interval_sec", 10), timeout=self.cfg.get("poll_timeout_sec", 600))
        if done is None:
            return PublishResult("submitted", post_id=str(video_id), permalink=permalink,
                                 detail="Meta側で処理中(公開リクエスト済み)。後でページ上で公開を確認してください")
        return PublishResult("published", post_id=str(video_id), permalink=permalink)
