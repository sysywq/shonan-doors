# -*- coding: utf-8 -*-
"""per-platform publisher の共通 interface。

publisher は「1本の動画(VideoAsset)を1つのプラットフォームに投稿する」ことだけを担う。
二重投稿防止・ログ記録・手動fallbackキットの作成は pipeline 側が行う。

例外の使い分け(ログの status に対応):
  MissingCredentials … 必要な環境変数が無い → manual_pending(手動投稿キットを作る)
  PublishError       … 投稿されていないことが確実な失敗 → failed(次回の再実行で再試行してよい)
  AmbiguousPublish   … 最終リクエスト送信後のタイムアウト等で投稿されたか不明 → unknown
                       (自動では再投稿しない。人間が確認して mark で直す)
"""
import os
import re
import time
from dataclasses import dataclass, field

from ..http import HttpError, HttpTimeout, redact
from ..storage import MissingCredentials  # noqa: F401  (publisher からも同じ例外を使う)


class PublishError(Exception):
    pass


class AmbiguousPublish(Exception):
    pass


@dataclass
class VideoAsset:
    video_asset_id: str
    article_id: object
    local_path: str = ""
    public_url: str = ""
    size_bytes: int = 0
    sha256: str = ""


@dataclass
class PublishResult:
    status: str  # published / submitted
    post_id: str = ""
    permalink: str = ""
    detail: str = ""
    extra: dict = field(default_factory=dict)


def clean_text(text):
    return re.sub(r"[ \t]+\n", "\n", (text or "")).strip()


def truncate(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


class Publisher:
    name = "base"
    required_env = ()

    def __init__(self, cfg_platform, http, *, environ=None, sleep=time.sleep, clock=time.monotonic):
        self.cfg = cfg_platform or {}
        self.http = http
        self.env = os.environ if environ is None else environ
        self.sleep = sleep
        self.clock = clock

    # ---- credentials ----
    def missing_credentials(self):
        return [k for k in self.required_env if not (self.env.get(k) or "").strip()]

    def require_credentials(self):
        missing = self.missing_credentials()
        if missing:
            raise MissingCredentials(self.name, missing)

    def _env(self, key):
        return (self.env.get(key) or "").strip()

    # ---- 投稿内容(秘密情報を含まない。dry-run と手動キットでも使う) ----
    def build_post(self, manifest, asset):
        raise NotImplementedError

    def publish(self, manifest, asset):
        raise NotImplementedError

    # ---- helpers ----
    def _call(self, method, url, *, final=False, what="", **kw):
        """HTTP呼び出し。final=True のリクエスト(投稿を確定させるもの)でのタイムアウトは結果不明扱い。"""
        try:
            return self.http.request(method, url, **kw)
        except HttpTimeout as e:
            if final:
                raise AmbiguousPublish(f"{self.name}: {what} の応答がありません(投稿された可能性があります): {e}") from None
            raise PublishError(f"{self.name}: {what} がタイムアウトしました: {e}") from None
        except HttpError as e:
            raise PublishError(f"{self.name}: {what} が失敗しました: {e}") from None

    def _poll(self, fn, *, interval, timeout):
        """fn() が None 以外を返すまで待つ。タイムアウトなら None。"""
        deadline = self.clock() + float(timeout)
        while True:
            got = fn()
            if got is not None:
                return got
            if self.clock() >= deadline:
                return None
            self.sleep(float(interval))

    def _video_bytes(self, asset):
        if asset.local_path and os.path.isfile(asset.local_path):
            with open(asset.local_path, "rb") as f:
                return f.read()
        if asset.public_url and asset.public_url.startswith("http"):
            return self._call("GET", asset.public_url, what="動画の取得", timeout=300).body
        raise PublishError(f"{self.name}: 動画ファイルがありません(local_path / public_url)")

    def _redact(self, text):
        return redact(text, self.env)
