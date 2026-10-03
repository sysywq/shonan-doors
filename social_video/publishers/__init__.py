# -*- coding: utf-8 -*-
"""per-platform publisher の登録表。新しい媒体は Publisher を継承して PUBLISHERS に追加する。"""
from .base import AmbiguousPublish, MissingCredentials, PublishError, PublishResult, Publisher, VideoAsset  # noqa: F401
from .facebook import FacebookPublisher
from .instagram import InstagramPublisher
from .tiktok import TikTokPublisher
from .youtube import YouTubePublisher

# 配信順。Instagram → Facebook は同じR2上のMP4を使う。
PUBLISHERS = {
    "instagram": InstagramPublisher,
    "facebook": FacebookPublisher,
    "youtube": YouTubePublisher,
    "tiktok": TikTokPublisher,
}
