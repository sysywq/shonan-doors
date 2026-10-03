# -*- coding: utf-8 -*-
"""配信ログ(data/social_video_log.json)。

records: 1件 = article_id + video_asset_id + platform の配信状態
  status
    published       投稿完了(post_id あり)
    submitted       プラットフォームへ渡し済みで処理中、または TikTok の下書き(inbox)送付済み
    unknown         最終リクエスト送信後にタイムアウト等で結果不明(投稿されている可能性がある)
    manual_pending  手動投稿用キットを作成済み・未投稿(credential未設定 / mode=manual / 失敗時)
    failed          失敗(投稿されていないことが確実)
assets: 1件 = 生成した動画ファイル(video_asset_id・公開URL・sha256 等)

二重投稿防止: 同じ article_id + platform に published / submitted / unknown の記録があれば、
video_asset_id が違っても自動投稿しない(--force-repost で明示したときだけ再投稿)。
unknown は人間が実際の投稿有無を確認し、`python3 -m social_video mark` で published / failed に直す。
"""
import json
import os
from datetime import datetime, timezone

from .config import ROOT

LOG_REL = "data/social_video_log.json"
LOG_PATH = os.path.join(ROOT, LOG_REL)

STATUSES = ("published", "submitted", "unknown", "manual_pending", "failed")
BLOCKING_STATUSES = ("published", "submitted", "unknown")
_RANK = {s: i for i, s in enumerate(reversed(STATUSES))}


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def empty_log():
    return {"assets": [], "records": []}


def load_log(path=LOG_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return empty_log()
    data.setdefault("assets", [])
    data.setdefault("records", [])
    return data


def save_log(log, path=LOG_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _rkey(r):
    return (str(r["article_id"]), str(r.get("video_asset_id", "")), r["platform"])


def merge_logs(*logs):
    """複数のログの和集合。同じキーは updated_at が新しい方、同時刻なら投稿が進んだ status を残す。"""
    recs, assets = {}, {}
    for log in logs:
        for r in (log or {}).get("records") or []:
            if not isinstance(r, dict) or "article_id" not in r or "platform" not in r:
                continue
            k = _rkey(r)
            cur = recs.get(k)
            score = (str(r.get("updated_at", "")), _RANK.get(r.get("status"), -1))
            if cur is None or score > (str(cur.get("updated_at", "")), _RANK.get(cur.get("status"), -1)):
                recs[k] = r
        for a in (log or {}).get("assets") or []:
            if isinstance(a, dict) and a.get("video_asset_id"):
                assets.setdefault(a["video_asset_id"], a)
    return {
        "assets": sorted(assets.values(), key=lambda a: (str(a.get("created_at", "")), a["video_asset_id"])),
        "records": sorted(recs.values(), key=lambda r: (str(r.get("updated_at", "")), *_rkey(r))),
    }


class DistributionLog:
    def __init__(self, data=None):
        self.data = data if data is not None else empty_log()

    def blocking_record(self, article_id, platform):
        """自動投稿を止めるべき既存記録(published / submitted / unknown)を返す。無ければ None。"""
        hits = [r for r in self.data["records"]
                if str(r["article_id"]) == str(article_id) and r["platform"] == platform
                and r.get("status") in BLOCKING_STATUSES]
        return max(hits, key=lambda r: str(r.get("updated_at", ""))) if hits else None

    def get(self, article_id, asset_id, platform):
        for r in self.data["records"]:
            if _rkey(r) == (str(article_id), str(asset_id), platform):
                return r
        return None

    def upsert(self, article_id, asset_id, platform, status, *, post_id="", permalink="", error="", mode="", extra=None):
        if status not in STATUSES:
            raise ValueError(f"未知の status です: {status}")
        r = self.get(article_id, asset_id, platform)
        if r is None:
            r = {"article_id": article_id, "video_asset_id": asset_id, "platform": platform, "attempts": 0}
            self.data["records"].append(r)
        r.update({"status": status, "post_id": post_id or r.get("post_id", ""),
                  "permalink": permalink or r.get("permalink", ""), "error": error, "mode": mode,
                  "updated_at": utc_now()})
        r["attempts"] = int(r.get("attempts", 0)) + 1
        if extra:
            r.update(extra)
        return r

    def add_asset(self, asset):
        if not any(a["video_asset_id"] == asset["video_asset_id"] for a in self.data["assets"]):
            self.data["assets"].append(dict(asset, created_at=asset.get("created_at") or utc_now()))
        else:
            for a in self.data["assets"]:
                if a["video_asset_id"] == asset["video_asset_id"]:
                    a.update({k: v for k, v in asset.items() if v})
