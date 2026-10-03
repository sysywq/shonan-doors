# -*- coding: utf-8 -*-
"""media storage interface と実装。

Instagram / Facebook / TikTok(PULL_FROM_URL)は「公開URLから動画を取りに来る」方式のため、
レンダリングした MP4 を公開URLで取得できる場所へ置く必要がある。第一候補は Cloudflare R2。

R2Storage は S3互換API(AWS Signature V4)を標準ライブラリだけで署名して PUT する。
接続情報は環境変数(GitHub Secrets)だけから読み、ログには一切出さない
(エンドポイントにアカウントIDが含まれるため、例外メッセージにもURLを載せない)。

必要な環境変数:
  R2_ACCOUNT_ID          Cloudflare アカウントID
  R2_ACCESS_KEY_ID       R2 APIトークン(S3互換)のアクセスキーID
  R2_SECRET_ACCESS_KEY   同シークレット
  R2_BUCKET              バケット名
  R2_PUBLIC_BASE_URL     公開URLのベース(カスタムドメイン or r2.dev。例 https://media.shonandoors.com)
"""
import hashlib
import hmac
import os
import shutil
import urllib.parse
from datetime import datetime, timezone

from .http import HttpError, HttpTimeout

R2_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PUBLIC_BASE_URL")


class StorageError(Exception):
    pass


class MissingCredentials(Exception):
    def __init__(self, what, names):
        self.names = list(names)
        super().__init__(f"{what}: 未設定の環境変数 {', '.join(self.names)}")


class MediaStorage:
    """公開メディアストレージの共通 interface。"""

    name = "base"

    def exists(self, key):
        raise NotImplementedError

    def put_file(self, key, path, content_type="video/mp4"):
        """アップロードして公開URLを返す。"""
        raise NotImplementedError

    def public_url(self, key):
        raise NotImplementedError


def _quote_key(key):
    return "/".join(urllib.parse.quote(seg, safe="-_.~") for seg in key.split("/"))


class R2Storage(MediaStorage):
    name = "r2"

    def __init__(self, http, environ=None, now=None):
        env = os.environ if environ is None else environ
        missing = [k for k in R2_ENV if not (env.get(k) or "").strip()]
        if missing:
            raise MissingCredentials("Cloudflare R2", missing)
        self._account = env["R2_ACCOUNT_ID"].strip()
        self._key_id = env["R2_ACCESS_KEY_ID"].strip()
        self._secret = env["R2_SECRET_ACCESS_KEY"].strip()
        self.bucket = env["R2_BUCKET"].strip()
        self.base_url = env["R2_PUBLIC_BASE_URL"].strip().rstrip("/")
        self.http = http
        self._now = now or (lambda: datetime.now(timezone.utc))

    def __repr__(self):  # 接続情報を出さない
        return "R2Storage(<redacted>)"

    @property
    def _host(self):
        return f"{self._account}.r2.cloudflarestorage.com"

    def _signed_headers(self, method, key, payload_hash, extra=None):
        t = self._now()
        amz_date = t.strftime("%Y%m%dT%H%M%SZ")
        date = t.strftime("%Y%m%d")
        headers = {"host": self._host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
        for k, v in (extra or {}).items():
            headers[k.lower()] = v
        names = sorted(headers)
        canonical = "\n".join([
            method, f"/{self.bucket}/{_quote_key(key)}", "",
            "".join(f"{n}:{str(headers[n]).strip()}\n" for n in names),
            ";".join(names), payload_hash,
        ])
        scope = f"{date}/auto/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])

        def _h(k, msg):
            return hmac.new(k, msg.encode(), hashlib.sha256).digest()

        k = _h(_h(_h(_h(("AWS4" + self._secret).encode(), date), "auto"), "s3"), "aws4_request")
        sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
        out = {n: headers[n] for n in names if n != "host"}
        out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={self._key_id}/{scope}, "
                                f"SignedHeaders={';'.join(names)}, Signature={sig}")
        return out

    def _url(self, key):
        return f"https://{self._host}/{self.bucket}/{_quote_key(key)}"

    def exists(self, key):
        empty = hashlib.sha256(b"").hexdigest()
        try:
            resp = self.http.request("HEAD", self._url(key), headers=self._signed_headers("HEAD", key, empty), timeout=30)
            return resp.status == 200
        except HttpError as e:
            if e.status == 404:
                return False
            raise StorageError(f"R2 HEAD が失敗しました(HTTP {e.status})") from None
        except HttpTimeout:
            raise StorageError("R2 HEAD がタイムアウトしました") from None

    def put_file(self, key, path, content_type="video/mp4"):
        with open(path, "rb") as f:
            data = f.read()
        payload_hash = hashlib.sha256(data).hexdigest()
        headers = self._signed_headers("PUT", key, payload_hash, {"content-type": content_type})
        try:
            self.http.request("PUT", self._url(key), headers=headers, data=data, timeout=600)
        except HttpError as e:
            raise StorageError(f"R2 へのアップロードが失敗しました(HTTP {e.status})") from None
        except HttpTimeout:
            raise StorageError("R2 へのアップロードがタイムアウトしました") from None
        return self.public_url(key)

    def public_url(self, key):
        return f"{self.base_url}/{_quote_key(key)}"


class LocalStorage(MediaStorage):
    """ローカルディレクトリ(テスト・dry-run・手動運用用)。public_url は base_url が無ければ file://。"""

    name = "local"

    def __init__(self, root, base_url=""):
        self.root = root
        self.base_url = base_url.rstrip("/")

    def exists(self, key):
        return os.path.isfile(os.path.join(self.root, key))

    def put_file(self, key, path, content_type="video/mp4"):
        dest = os.path.join(self.root, key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.abspath(dest) != os.path.abspath(path):
            shutil.copyfile(path, dest)
        return self.public_url(key)

    def public_url(self, key):
        if self.base_url:
            return f"{self.base_url}/{_quote_key(key)}"
        return "file://" + os.path.abspath(os.path.join(self.root, key))


def get_storage(cfg, http, environ=None, local_root=None):
    provider = cfg["storage"].get("provider", "r2")
    if provider == "r2":
        return R2Storage(http, environ)
    if provider == "local":
        return LocalStorage(local_root or os.path.join("out", "social_video", "storage"))
    raise StorageError(f"未知の storage.provider です: {provider}")


def object_key(cfg, article_id, asset_id):
    prefix = cfg["storage"].get("key_prefix", "social-video").strip("/")
    return f"{prefix}/{article_id}/{asset_id}.mp4"
