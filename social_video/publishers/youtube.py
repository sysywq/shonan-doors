# -*- coding: utf-8 -*-
"""YouTube Shorts(YouTube Data API v3 videos.insert / resumable upload)。

9:16・3分以内の動画は自動的に Shorts として扱われる。タイトル/説明は manifest から作る。
流れ: refresh_token → access_token → resumable セッション開始(snippet/status) → MP4本体を PUT。

必要な環境変数:
  YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET   Google Cloud の OAuth クライアント
  YOUTUBE_REFRESH_TOKEN                       youtube.upload スコープで取得した refresh token

注意: API監査(Audit)を通過していないプロジェクトからのアップロードは、
privacyStatus の指定に関わらず「非公開(private)」に固定される。
"""
import json
import re

from .base import PublishError, PublishResult, Publisher, clean_text, truncate

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
TITLE_MAX = 100
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500


class YouTubePublisher(Publisher):
    name = "youtube"
    required_env = ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")

    def build_post(self, manifest, asset):
        title = re.sub(r"[<>]", "", manifest["title"])
        if self.cfg.get("add_shorts_hashtag", True):
            title = truncate(title, TITLE_MAX - len(" #Shorts")) + " #Shorts"
        else:
            title = truncate(title, TITLE_MAX)
        desc = re.sub(r"[<>]", "", clean_text(manifest["caption"]))
        while len(desc.encode("utf-8")) > DESCRIPTION_MAX_BYTES:
            desc = desc[:-50]
        tags, total = [], 0
        for t in manifest.get("hashtags") or []:
            if total + len(t) + 1 > TAGS_MAX_CHARS:
                break
            tags.append(t)
            total += len(t) + 1
        return {
            "snippet": {"title": title, "description": desc, "tags": tags,
                        "categoryId": str(self.cfg.get("category_id", "19")), "defaultLanguage": "ja"},
            "status": {"privacyStatus": self.cfg.get("privacy_status", "public"),
                       "selfDeclaredMadeForKids": bool(self.cfg.get("made_for_kids", False))},
        }

    def _access_token(self):
        r = self._call("POST", TOKEN_URL, form={
            "grant_type": "refresh_token", "client_id": self._env("YOUTUBE_CLIENT_ID"),
            "client_secret": self._env("YOUTUBE_CLIENT_SECRET"), "refresh_token": self._env("YOUTUBE_REFRESH_TOKEN"),
        }, what="アクセストークン取得", timeout=30).json()
        token = r.get("access_token")
        if not token:
            raise PublishError("youtube: アクセストークンを取得できませんでした(refresh token の失効・取り消しの可能性)")
        return token

    def publish(self, manifest, asset):
        self.require_credentials()
        data = self._video_bytes(asset)
        token = self._access_token()
        meta = self.build_post(manifest, asset)
        start = self._call("POST", UPLOAD_URL, headers={
            "Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(len(data)),
        }, json_body=meta, what="resumable セッション開始", timeout=60)
        location = start.headers.get("location")
        if not location:
            raise PublishError("youtube: アップロードURL(Location)が返りませんでした")
        resp = self._call("PUT", location, headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"},
                          data=data, final=True, what="動画アップロード", timeout=900)
        video = resp.json()
        vid = video.get("id")
        if not vid:
            raise PublishError(f"youtube: 動画IDが返りませんでした: {self._redact(json.dumps(video)[:300])}")
        privacy = (video.get("status") or {}).get("privacyStatus", "")
        detail = f"privacyStatus={privacy}" if privacy else ""
        return PublishResult("published", post_id=vid, permalink=f"https://www.youtube.com/shorts/{vid}", detail=detail)
