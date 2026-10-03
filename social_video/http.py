# -*- coding: utf-8 -*-
"""外部APIを呼ぶための最小HTTPクライアント(標準ライブラリのみ)。

publisher/storage はこのクラスのインスタンスを受け取るため、テストでは
同じ request() を持つスタブに差し替えて外部APIを一切呼ばずに検証できる。

秘密情報の扱い:
- アクセストークンは URL のクエリに載せず、ヘッダーかPOST本文で送る
- 例外メッセージにはURLのホスト・パスまでしか含めず、クエリは落とす
- 表示前に redact() で既知の秘密値(環境変数の値)を伏せ字にする
"""
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT = 60
USER_AGENT = "ShonanDoorsSocialVideo/1.0"

# 値をログに出してはいけない環境変数(名前の部分一致)
_SECRET_HINTS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "R2_ACCOUNT_ID", "R2_ENDPOINT")


class HttpError(Exception):
    """HTTP 4xx/5xx。status と(伏せ字済みの)本文の先頭だけを持つ。"""

    def __init__(self, status, body, url=""):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} {safe_url(url)}: {redact(str(body))[:500]}")


class HttpTimeout(Exception):
    """タイムアウト・接続断。リクエストが相手に届いたかどうかは分からない。"""

    def __init__(self, url="", reason=""):
        self.url = url
        super().__init__(f"timeout/接続エラー {safe_url(url)}: {redact(str(reason))[:200]}")


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.body = body

    def json(self):
        if not self.body:
            return {}
        if isinstance(self.body, (dict, list)):
            return self.body
        return json.loads(self.body.decode("utf-8") if isinstance(self.body, bytes) else self.body)


def safe_url(url):
    """ログ表示用: クエリ・認証情報を落としたURL。"""
    if not url:
        return ""
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    return f"{p.scheme}://{host}{p.path}" if p.scheme else p.path


def secret_values(environ=None):
    env = os.environ if environ is None else environ
    vals = []
    for k, v in env.items():
        if v and len(v) >= 6 and any(h in k.upper() for h in _SECRET_HINTS):
            vals.append(v)
    return sorted(set(vals), key=len, reverse=True)


def redact(text, environ=None):
    """既知の秘密値(環境変数の値)を *** に置き換える。"""
    if not text:
        return text
    out = str(text)
    for v in secret_values(environ):
        out = out.replace(v, "***")
    return out


class HttpClient:
    """urllib ベースの実装。"""

    def request(self, method, url, *, headers=None, data=None, json_body=None, form=None, timeout=DEFAULT_TIMEOUT):
        hdrs = {"User-Agent": USER_AGENT}
        hdrs.update(headers or {})
        body = data
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json; charset=UTF-8")
        elif form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Response(resp.status, dict(resp.headers.items()), resp.read())
        except urllib.error.HTTPError as e:
            raw = e.read() if hasattr(e, "read") else b""
            raise HttpError(e.code, raw.decode("utf-8", errors="replace"), url) from None
        except (socket.timeout, TimeoutError) as e:
            raise HttpTimeout(url, e) from None
        except urllib.error.URLError as e:
            raise HttpTimeout(url, getattr(e, "reason", e)) from None
