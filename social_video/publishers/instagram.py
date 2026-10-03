# -*- coding: utf-8 -*-
"""Instagram Reels(Instagram Graph API / Content Publishing)。

流れ: media container 作成(media_type=REELS, video_url) → status_code が FINISHED になるまで待つ → media_publish。
Instagram は動画を公開URLから取りに来るため、asset.public_url(R2)が必須。

必要な環境変数:
  INSTAGRAM_PUBLISH_ACCESS_TOKEN  instagram_content_publish を含む長期トークン
                                   (既存の INSTAGRAM_FACEBOOK_ACCESS_TOKEN は Business Discovery 用で権限が違う)
  INSTAGRAM_BUSINESS_USER_ID      投稿先 Instagram プロアカウントのID(既存Secretと共通)
"""
from .base import PublishError, PublishResult, Publisher, clean_text, truncate

CAPTION_MAX = 2200
HASHTAG_MAX = 30


class InstagramPublisher(Publisher):
    name = "instagram"
    required_env = ("INSTAGRAM_PUBLISH_ACCESS_TOKEN", "INSTAGRAM_BUSINESS_USER_ID")

    @property
    def graph(self):
        return f"https://graph.facebook.com/{self.cfg.get('graph_api_version', 'v23.0')}"

    def _auth(self):
        return {"Authorization": f"OAuth {self._env('INSTAGRAM_PUBLISH_ACCESS_TOKEN')}"}

    def build_post(self, manifest, asset):
        caption = manifest["caption"]
        if len(manifest.get("hashtags") or []) > HASHTAG_MAX:
            raise PublishError(f"instagram: ハッシュタグは{HASHTAG_MAX}個までです(manifest.max_hashtags を下げてください)")
        return {
            "media_type": "REELS",
            "video_url": asset.public_url,
            "caption": truncate(clean_text(caption), CAPTION_MAX),
            "share_to_feed": "true" if self.cfg.get("share_to_feed", True) else "false",
        }

    def publish(self, manifest, asset):
        self.require_credentials()
        if not (asset.public_url or "").startswith("https://"):
            raise PublishError("instagram: 公開URL(https)の動画が必要です。R2 の設定を確認してください")
        ig = self._env("INSTAGRAM_BUSINESS_USER_ID")
        post = self.build_post(manifest, asset)
        r = self._call("POST", f"{self.graph}/{ig}/media", headers=self._auth(), form=post,
                       what="media container 作成", timeout=60).json()
        container = r.get("id")
        if not container:
            raise PublishError(f"instagram: container IDがありません: {self._redact(r)}")

        def check():
            st = self._call("GET", f"{self.graph}/{container}?fields=status_code,status", headers=self._auth(),
                            what="container 状態確認", timeout=30).json()
            code = st.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return code
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"instagram: container の処理に失敗しました({code}): {self._redact(st.get('status'))}")
            return None

        if self._poll(check, interval=self.cfg.get("poll_interval_sec", 10), timeout=self.cfg.get("poll_timeout_sec", 600)) is None:
            # container は24時間で失効し、media_publish しない限り投稿されない
            raise PublishError("instagram: container の処理が時間内に終わりませんでした(未投稿)")

        pub = self._call("POST", f"{self.graph}/{ig}/media_publish", headers=self._auth(),
                         form={"creation_id": container}, final=True, what="media_publish", timeout=120).json()
        media_id = pub.get("id")
        if not media_id:
            raise PublishError(f"instagram: media_publish の応答にIDがありません: {self._redact(pub)}")
        permalink = ""
        try:
            permalink = self.http.request("GET", f"{self.graph}/{media_id}?fields=permalink", headers=self._auth(),
                                          timeout=30).json().get("permalink", "")
        except Exception:  # permalink の取得失敗で投稿成功を失敗扱いにしない
            pass
        return PublishResult("published", post_id=str(media_id), permalink=permalink,
                             extra={"container_id": str(container)})
