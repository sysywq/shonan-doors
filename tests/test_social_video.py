# -*- coding: utf-8 -*-
"""short動画パイプライン(social_video/)のテスト。
外部API(Meta Graph / YouTube / TikTok / R2)・ffmpeg はすべてスタブに差し替え、ネットワークには出ない。
実行: python3 -m unittest tests/test_social_video.py -v
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from social_video import articles as art  # noqa: E402
from social_video import config as svconfig  # noqa: E402
from social_video import manifest as mf  # noqa: E402
from social_video import renderers as rd  # noqa: E402
from social_video import storage as st  # noqa: E402
from social_video.distribution_log import DistributionLog, load_log, merge_logs  # noqa: E402
from social_video.http import HttpError, HttpTimeout, Response, redact  # noqa: E402
from social_video.pipeline import Pipeline, mark, target_platforms  # noqa: E402
from social_video.publishers import (  # noqa: E402
    AmbiguousPublish, MissingCredentials, PublishError, PublishResult, Publisher, VideoAsset)
from social_video.publishers.facebook import FacebookPublisher  # noqa: E402
from social_video.publishers.instagram import InstagramPublisher  # noqa: E402
from social_video.publishers.tiktok import TikTokPublisher, chunk_plan  # noqa: E402
from social_video.publishers.youtube import YouTubePublisher  # noqa: E402

TODAY = "2026-10-03"


def make_article(**kw):
    a = {
        "id": 501, "slug": "fujisawa-test-event", "cat": "e", "area": "藤沢", "scene": "beach",
        "title": "藤沢でテストイベント、10月12日まで開催", "dek": "藤沢のテストイベントが開催中。家族で楽しめる。",
        "date": "2026-10-01", "eventStartDate": "2026-10-01", "eventEndDate": "2026-10-12",
        "body": "最初の段落です。二文目です。\n\n二つ目の段落です。\n\n三つ目の段落。\n\n四つ目。",
        "tags": ["藤沢", "イベント", "秋 祭り"], "link": "https://example.jp/",
    }
    a.update(kw)
    return a


def cfg(**overrides):
    return svconfig.load_config(overrides=overrides)


class FakeHttp:
    """(method, URLの部分文字列) → 応答。応答がlistなら順に返す。callable/例外も可。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for m, sub, handler in self.routes:
            if m == method and sub in url:
                h = handler.pop(0) if isinstance(handler, list) else handler
                if isinstance(h, Exception):
                    raise h
                if callable(h):
                    h = h(method, url, kw)
                return h
        raise AssertionError(f"想定外のHTTP呼び出し: {method} {url}")


def ok(body=None, headers=None, status=200):
    return Response(status, headers or {}, json.dumps(body or {}).encode())


def no_sleep(_):
    return None


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 5
        return self.t


def asset(tmp=None, url="https://media.example.com/social-video/501/sv-501-abc.mp4"):
    path = ""
    if tmp:
        path = os.path.join(tmp, "v.mp4")
        with open(path, "wb") as f:
            f.write(b"\x00" * 1000)
    return VideoAsset("sv-501-abc", 501, local_path=path, public_url=url, size_bytes=1000)


# ---------------------------------------------------------------------------
class ManifestTest(unittest.TestCase):
    def test_manifest_has_required_fields(self):
        m = mf.article_to_video_manifest(make_article(), cfg(), now=datetime(2026, 10, 3, tzinfo=timezone.utc))
        for k in ("article_id", "article_url", "title", "hook", "scenes", "source_image_urls", "cta", "caption",
                  "hashtags", "duration_target_sec", "generated_at"):
            self.assertIn(k, m)
        self.assertEqual(m["article_url"], "https://www.shonandoors.com/articles/fujisawa-test-event/")
        self.assertEqual(m["generated_at"], "2026-10-03T00:00:00Z")
        self.assertIn(m["article_url"], m["caption"])
        self.assertEqual(m["scenes"][0]["type"], "title")
        self.assertEqual(m["scenes"][-1]["type"], "cta")
        self.assertAlmostEqual(sum(s["duration_sec"] for s in m["scenes"]), m["duration_target_sec"], places=2)
        self.assertTrue(all(s["image_url"] for s in m["scenes"]))
        # 本文シーンは max_body_scenes(3)まで
        self.assertEqual(len([s for s in m["scenes"] if s["type"] == "point"]), 3)

    def test_hashtags_are_sanitized_and_deduped(self):
        m = mf.article_to_video_manifest(make_article(), cfg())
        self.assertEqual(m["hashtags"][:2], ["湘南", "ShonanDoors"])
        self.assertEqual(m["hashtags"].count("藤沢"), 1)
        self.assertIn("秋祭り", m["hashtags"])

    def test_images_prefer_article_hero_then_category(self):
        urls = art.source_image_urls(make_article(heroImage="/assets/images/articles/157.jpg"))
        self.assertEqual(urls[0], "https://www.shonandoors.com/assets/images/articles/157.jpg")
        self.assertIn("https://www.shonandoors.com/assets/images/category-photos/event.webp", urls)

    def test_asset_id_is_stable_and_renderer_dependent(self):
        c = cfg()
        m1 = mf.article_to_video_manifest(make_article(), c, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        m2 = mf.article_to_video_manifest(make_article(), c, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(mf.video_asset_id(m1, c), mf.video_asset_id(m2, c))
        self.assertTrue(mf.video_asset_id(m1, c).startswith("sv-501-"))
        c2 = cfg(renderer={"name": "command", "command": "x {manifest} {output}"})
        self.assertNotEqual(mf.video_asset_id(m1, c), mf.video_asset_id(m1, c2))

    def test_duration_target_is_configurable(self):
        m = mf.article_to_video_manifest(make_article(), cfg(manifest={"duration_target_sec": 45}))
        self.assertAlmostEqual(sum(s["duration_sec"] for s in m["scenes"]), 45, places=2)


class ArticleLoaderTest(unittest.TestCase):
    def test_only_published_articles(self):
        arts = [make_article(), make_article(id=502, mergedInto="x"), make_article(id=503, date="2026-12-01"),
                make_article(id=504, eventEndDate="2026-09-30")]
        self.assertEqual(art.load_published_article(501, articles=arts, today=TODAY)["id"], 501)
        for aid in (502, 503, 504, 999):
            with self.assertRaises(art.ArticleNotPublished):
                art.load_published_article(aid, articles=arts, today=TODAY)
        # 終了イベントも設定で対象にできる
        art.load_published_article(504, articles=arts, today=TODAY, skip_ended_events=False)
        self.assertEqual(art.latest_published_ids(5, articles=arts, today=TODAY), [501])

    def test_check_live(self):
        art.check_live(make_article(), FakeHttp([("GET", "/articles/", ok())]))
        with self.assertRaises(art.ArticleNotPublished):
            art.check_live(make_article(), FakeHttp([("GET", "/articles/", HttpError(404, "nf"))]))

    def test_real_articles_json_loads(self):
        ids = art.latest_published_ids(3)
        self.assertTrue(ids)


class ConfigTest(unittest.TestCase):
    def test_rejects_non_vertical(self):
        with self.assertRaises(ValueError):
            cfg(output={"width": 1920, "height": 1080})

    def test_rejects_bad_tiktok_mode(self):
        with self.assertRaises(ValueError):
            cfg(platforms={"tiktok": {"post_mode": "auto"}})

    def test_rejects_out_of_range_duration(self):
        with self.assertRaises(ValueError):
            cfg(manifest={"duration_target_sec": 600})

    def test_target_platforms(self):
        c = cfg()
        self.assertEqual(target_platforms("full-auto", c), ["instagram", "facebook", "youtube", "tiktok"])
        self.assertEqual(target_platforms("publish-selected-platforms", c, ["youtube", "instagram"]),
                         ["instagram", "youtube"])
        with self.assertRaises(ValueError):
            target_platforms("publish-selected-platforms", c, [])
        with self.assertRaises(ValueError):
            target_platforms("publish-selected-platforms", c, ["x"])


# ---------------------------------------------------------------------------
class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def probe_json(vcodec="h264", acodec="aac", w=1080, h=1920, duration=20.0, pix="yuv420p"):
    return json.dumps({"streams": [{"codec_type": "video", "codec_name": vcodec, "width": w, "height": h, "pix_fmt": pix},
                                   {"codec_type": "audio", "codec_name": acodec}],
                       "format": {"duration": str(duration)}})


def fake_run_factory(probe=None, calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        if cmd[0] == "ffprobe":
            return FakeProc(0, probe or probe_json())
        with open(cmd[-1], "wb") as f:
            f.write(b"\x00" * 2048)
        return FakeProc(0)
    return run


class RendererTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ffmpeg_command_uses_safe_common_spec(self):
        c = cfg()
        m = mf.article_to_video_manifest(make_article(), c)
        r = rd.FfmpegSlideshowRenderer(c, run=fake_run_factory())
        with mock_fonts(False), redirect_stdout(io.StringIO()):
            cmd = r.build_command(m, os.path.join(self.tmp, "o.mp4"), self.tmp)
        joined = " ".join(cmd)
        for needle in ("libx264", "yuv420p", "aac", "+faststart", "scale=1080:1920", "crop=1080:1920", "anullsrc",
                       "-bufsize 16M"):
            self.assertIn(needle, joined)
        self.assertNotIn("drawtext", joined)  # フォントが無ければ字幕を焼かない
        # サイト内画像はローカルファイルを使う(ネットワーク不要)
        self.assertTrue(any(p.endswith("category-photos/event.webp") for p in cmd))

    def test_drawtext_when_font_available(self):
        font = os.path.join(self.tmp, "font.ttc")
        open(font, "wb").close()
        c = cfg(renderer={"options": {"font_file": font}})
        m = mf.article_to_video_manifest(make_article(), c)
        cmd = rd.FfmpegSlideshowRenderer(c, run=fake_run_factory()).build_command(m, os.path.join(self.tmp, "o.mp4"), self.tmp)
        self.assertIn("drawtext", " ".join(cmd))

    def test_render_validates_output(self):
        c = cfg()
        m = mf.article_to_video_manifest(make_article(), c)
        out = os.path.join(self.tmp, "o.mp4")
        with mock_fonts(False), redirect_stdout(io.StringIO()):
            meta = rd.FfmpegSlideshowRenderer(c, run=fake_run_factory()).render(m, out, self.tmp)
        self.assertEqual((meta["width"], meta["height"]), (1080, 1920))
        for bad in (probe_json(vcodec="hevc"), probe_json(acodec="opus"), probe_json(w=1080, h=1080),
                    probe_json(duration=200), probe_json(pix="yuv444p")):
            with self.assertRaises(rd.RenderError):
                rd.validate_output(out, c, run=fake_run_factory(probe=bad))

    def test_ffmpeg_failure(self):
        c = cfg()
        m = mf.article_to_video_manifest(make_article(), c)
        with mock_fonts(False), redirect_stdout(io.StringIO()), self.assertRaises(rd.RenderError):
            rd.FfmpegSlideshowRenderer(c, run=lambda cmd, **kw: FakeProc(1, "", "boom")).render(
                m, os.path.join(self.tmp, "o.mp4"), self.tmp)

    def test_command_renderer_is_swappable(self):
        calls = []
        c = cfg(renderer={"name": "command", "command": "my-ai-video --in {manifest} --out {output}"})
        r = rd.get_renderer(c, run=fake_run_factory(calls=calls))
        self.assertIsInstance(r, rd.CommandRenderer)
        out = os.path.join(self.tmp, "o.mp4")
        r.render(mf.article_to_video_manifest(make_article(), c), out, self.tmp)
        self.assertEqual(calls[0][:2], ["my-ai-video", "--in"])
        self.assertTrue(calls[0][2].endswith("manifest.json"))
        self.assertEqual(calls[0][-1], out)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg が無い環境では実レンダリングを省略")
    def test_real_ffmpeg_render(self):
        """ffmpeg がある環境では、実際に H.264 + AAC / 9:16 のMP4ができることを確認する。"""
        c = cfg(output={"width": 360, "height": 640}, manifest={"duration_target_sec": 3})
        m = mf.article_to_video_manifest(make_article(), c)
        out = os.path.join(self.tmp, "real.mp4")
        with mock_fonts(False), redirect_stdout(io.StringIO()):
            meta = rd.FfmpegSlideshowRenderer(c).render(m, out, self.tmp)
        self.assertEqual((meta["width"], meta["height"], meta["video_codec"], meta["audio_codec"]),
                         (360, 640, "h264", "aac"))

    def test_unknown_renderer(self):
        with self.assertRaises(rd.RenderError):
            rd.get_renderer(cfg(renderer={"name": "nope"}))


class mock_fonts:
    def __init__(self, available):
        self.available = available

    def __enter__(self):
        self.orig = rd.FONT_CANDIDATES
        if not self.available:
            rd.FONT_CANDIDATES = ()

    def __exit__(self, *a):
        rd.FONT_CANDIDATES = self.orig


# ---------------------------------------------------------------------------
R2_ENV = {"R2_ACCOUNT_ID": "acct1234567890", "R2_ACCESS_KEY_ID": "AKIDEXAMPLE123",
          "R2_SECRET_ACCESS_KEY": "supersecretvalue999", "R2_BUCKET": "shonan-media",
          "R2_PUBLIC_BASE_URL": "https://media.example.com/"}


class StorageTest(unittest.TestCase):
    def test_missing_credentials(self):
        with self.assertRaises(st.MissingCredentials) as cm:
            st.R2Storage(FakeHttp([]), environ={"R2_BUCKET": "b"})
        self.assertIn("R2_ACCOUNT_ID", cm.exception.names)
        self.assertNotIn("R2_BUCKET", cm.exception.names)

    def test_put_signs_and_returns_public_url(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "v.mp4")
            with open(path, "wb") as f:
                f.write(b"video")
            http = FakeHttp([("PUT", "r2.cloudflarestorage.com", ok())])
            s = st.R2Storage(http, environ=R2_ENV)
            url = s.put_file("social-video/501/sv-501-abc.mp4", path)
            self.assertEqual(url, "https://media.example.com/social-video/501/sv-501-abc.mp4")
            method, req_url, kw = http.calls[0]
            self.assertTrue(req_url.startswith("https://acct1234567890.r2.cloudflarestorage.com/shonan-media/"))
            self.assertIn("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE123/", kw["headers"]["Authorization"])
            self.assertNotIn("supersecretvalue999", json.dumps(kw["headers"]))
            self.assertEqual(kw["headers"]["content-type"], "video/mp4")
            self.assertNotIn("acct", repr(s))
        finally:
            shutil.rmtree(tmp)

    def test_errors_do_not_leak_connection_info(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "v.mp4")
            open(path, "wb").close()
            for exc in (HttpError(403, "denied", "https://acct1234567890.r2.cloudflarestorage.com/x"),
                        HttpTimeout("https://acct1234567890.r2.cloudflarestorage.com/x", "timed out")):
                s = st.R2Storage(FakeHttp([("PUT", "r2", exc)]), environ=R2_ENV)
                with self.assertRaises(st.StorageError) as cm:
                    s.put_file("k.mp4", path)
                for secret in R2_ENV.values():
                    if "media.example" not in secret:
                        self.assertNotIn(secret, str(cm.exception))
        finally:
            shutil.rmtree(tmp)

    def test_exists(self):
        self.assertTrue(st.R2Storage(FakeHttp([("HEAD", "r2", ok())]), environ=R2_ENV).exists("k"))
        self.assertFalse(st.R2Storage(FakeHttp([("HEAD", "r2", HttpError(404, ""))]), environ=R2_ENV).exists("k"))

    def test_redact(self):
        env = {"YOUTUBE_REFRESH_TOKEN": "1//refresh-abcdef", "R2_BUCKET": "public-bucket"}
        self.assertEqual(redact("token=1//refresh-abcdef ok", env), "token=*** ok")
        self.assertIn("public-bucket", redact("public-bucket", env))


# ---------------------------------------------------------------------------
IG_ENV = {"INSTAGRAM_PUBLISH_ACCESS_TOKEN": "IGTOKEN-secret", "INSTAGRAM_BUSINESS_USER_ID": "1784"}
FB_ENV = {"FACEBOOK_PAGE_ID": "9999", "FACEBOOK_PAGE_ACCESS_TOKEN": "FBTOKEN-secret"}
YT_ENV = {"YOUTUBE_CLIENT_ID": "cid", "YOUTUBE_CLIENT_SECRET": "csecret", "YOUTUBE_REFRESH_TOKEN": "rtoken"}
TT_ENV = {"TIKTOK_CLIENT_KEY": "ck", "TIKTOK_CLIENT_SECRET": "cs", "TIKTOK_REFRESH_TOKEN": "rt"}


def manifest_for_tests():
    return mf.article_to_video_manifest(make_article(), cfg())


def pub(cls, http, env, platform_cfg=None):
    c = dict(cfg()["platforms"][cls.name])
    c.update(platform_cfg or {})
    return cls(c, http, environ=env, sleep=no_sleep, clock=Clock())


class InstagramTest(unittest.TestCase):
    def test_container_poll_publish(self):
        http = FakeHttp([
            ("POST", "/1784/media_publish", ok({"id": "IGMEDIA1"})),
            ("POST", "/1784/media", ok({"id": "C1"})),
            ("GET", "/C1?fields=status_code", [ok({"status_code": "IN_PROGRESS"}), ok({"status_code": "FINISHED"})]),
            ("GET", "/IGMEDIA1?fields=permalink", ok({"permalink": "https://www.instagram.com/reel/x/"})),
        ])
        r = pub(InstagramPublisher, http, IG_ENV).publish(manifest_for_tests(), asset())
        self.assertEqual((r.status, r.post_id), ("published", "IGMEDIA1"))
        self.assertEqual(r.permalink, "https://www.instagram.com/reel/x/")
        create = http.calls[0][2]["form"]
        self.assertEqual(create["media_type"], "REELS")
        self.assertEqual(create["video_url"], asset().public_url)
        for _m, url, kw in http.calls:  # トークンをURLに載せない
            self.assertNotIn("IGTOKEN-secret", url)

    def test_container_error(self):
        http = FakeHttp([("POST", "/media", ok({"id": "C1"})), ("GET", "/C1", ok({"status_code": "ERROR", "status": "bad"}))])
        with self.assertRaises(PublishError):
            pub(InstagramPublisher, http, IG_ENV).publish(manifest_for_tests(), asset())

    def test_processing_timeout_is_not_published(self):
        http = FakeHttp([("POST", "/media", ok({"id": "C1"})), ("GET", "/C1", lambda *a: ok({"status_code": "IN_PROGRESS"}))])
        with self.assertRaises(PublishError):
            pub(InstagramPublisher, http, IG_ENV, {"poll_timeout_sec": 30}).publish(manifest_for_tests(), asset())
        self.assertFalse(any("media_publish" in u for _m, u, _k in http.calls))

    def test_publish_timeout_is_ambiguous(self):
        http = FakeHttp([
            ("POST", "/media_publish", HttpTimeout("https://graph.facebook.com/x", "timed out")),
            ("POST", "/media", ok({"id": "C1"})), ("GET", "/C1", ok({"status_code": "FINISHED"})),
        ])
        with self.assertRaises(AmbiguousPublish):
            pub(InstagramPublisher, http, IG_ENV).publish(manifest_for_tests(), asset())

    def test_missing_credentials_and_url(self):
        with self.assertRaises(MissingCredentials):
            pub(InstagramPublisher, FakeHttp([]), {}).publish(manifest_for_tests(), asset())
        with self.assertRaises(PublishError):
            pub(InstagramPublisher, FakeHttp([]), IG_ENV).publish(manifest_for_tests(), asset(url=""))


class FacebookTest(unittest.TestCase):
    def routes(self, status_responses):
        return [
            ("POST", "/9999/video_reels", [ok({"video_id": "V1", "upload_url": "https://rupload.facebook.com/video-upload/v23.0/V1"}),
                                          ok({"success": True})]),
            ("POST", "rupload.facebook.com", ok({"success": True})),
            ("GET", "/V1?fields=status", status_responses),
        ]

    def test_reels_publish_uses_same_hosted_mp4(self):
        http = FakeHttp(self.routes([ok({"status": {"video_status": "processing"}}),
                                     ok({"status": {"publishing_phase": {"status": "complete"}}})]))
        r = pub(FacebookPublisher, http, FB_ENV).publish(manifest_for_tests(), asset())
        self.assertEqual((r.status, r.post_id), ("published", "V1"))
        upload = [c for c in http.calls if "rupload" in c[1]][0]
        self.assertEqual(upload[2]["headers"]["file_url"], asset().public_url)
        finish = http.calls[2][2]["form"]
        self.assertEqual((finish["upload_phase"], finish["video_state"]), ("finish", "PUBLISHED"))

    def test_slow_processing_is_submitted(self):
        http = FakeHttp(self.routes(lambda *a: ok({"status": {"video_status": "processing"}})))
        r = pub(FacebookPublisher, http, FB_ENV, {"poll_timeout_sec": 20}).publish(manifest_for_tests(), asset())
        self.assertEqual(r.status, "submitted")

    def test_finish_timeout_is_ambiguous(self):
        http = FakeHttp([
            ("POST", "/9999/video_reels", [ok({"video_id": "V1"}), HttpTimeout("u", "timed out")]),
            ("POST", "rupload.facebook.com", ok({"success": True})),
        ])
        with self.assertRaises(AmbiguousPublish):
            pub(FacebookPublisher, http, FB_ENV).publish(manifest_for_tests(), asset())


class YouTubeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_resumable_upload(self):
        http = FakeHttp([
            ("POST", "oauth2.googleapis.com/token", ok({"access_token": "AT"})),
            ("POST", "upload/youtube/v3/videos", ok({}, headers={"Location": "https://www.googleapis.com/upload/session1"})),
            ("PUT", "upload/session1", ok({"id": "YT123", "status": {"privacyStatus": "private"}})),
        ])
        r = pub(YouTubePublisher, http, YT_ENV).publish(manifest_for_tests(), asset(self.tmp))
        self.assertEqual((r.status, r.post_id), ("published", "YT123"))
        self.assertEqual(r.permalink, "https://www.youtube.com/shorts/YT123")
        self.assertIn("privacyStatus=private", r.detail)
        meta = http.calls[1][2]["json_body"]
        self.assertTrue(meta["snippet"]["title"].endswith("#Shorts"))
        self.assertLessEqual(len(meta["snippet"]["title"]), 100)
        self.assertEqual(meta["status"]["privacyStatus"], "public")
        self.assertEqual(http.calls[1][2]["headers"]["X-Upload-Content-Length"], "1000")

    def test_long_title_is_truncated(self):
        m = manifest_for_tests()
        m["title"] = "あ" * 200
        post = pub(YouTubePublisher, FakeHttp([]), YT_ENV).build_post(m, asset())
        self.assertLessEqual(len(post["snippet"]["title"]), 100)

    def test_token_failure(self):
        http = FakeHttp([("POST", "oauth2", HttpError(400, '{"error":"invalid_grant"}'))])
        with self.assertRaises(PublishError):
            pub(YouTubePublisher, http, YT_ENV).publish(manifest_for_tests(), asset(self.tmp))

    def test_upload_timeout_is_ambiguous(self):
        http = FakeHttp([
            ("POST", "oauth2", ok({"access_token": "AT"})),
            ("POST", "upload/youtube", ok({}, headers={"Location": "https://www.googleapis.com/upload/s"})),
            ("PUT", "upload/s", HttpTimeout("u", "timed out")),
        ])
        with self.assertRaises(AmbiguousPublish):
            pub(YouTubePublisher, http, YT_ENV).publish(manifest_for_tests(), asset(self.tmp))

    def test_missing_credentials(self):
        p = pub(YouTubePublisher, FakeHttp([]), {"YOUTUBE_CLIENT_ID": "x"})
        self.assertEqual(p.missing_credentials(), ["YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"])


class TikTokTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_chunk_plan(self):
        self.assertEqual(chunk_plan(1000), (1000, 1))
        self.assertEqual(chunk_plan(64 * 1024 * 1024), (64 * 1024 * 1024, 1))
        size = 105 * 1024 * 1024
        cs, n = chunk_plan(size)
        self.assertEqual((cs, n), (10 * 1024 * 1024, 10))  # 最後のチャンクが端数(15MB)を含む

    def test_upload_mode_goes_to_inbox(self):
        http = FakeHttp([
            ("POST", "/oauth/token/", ok({"access_token": "AT"})),
            ("POST", "/inbox/video/init/", ok({"data": {"publish_id": "P1", "upload_url": "https://open-upload.tiktokapis.com/u"}, "error": {"code": "ok"}})),
            ("PUT", "open-upload.tiktokapis.com", ok()),
            ("POST", "/status/fetch/", [ok({"data": {"status": "PROCESSING_UPLOAD"}}), ok({"data": {"status": "SEND_TO_USER_INBOX"}})]),
        ])
        r = pub(TikTokPublisher, http, TT_ENV, {"post_mode": "upload"}).publish(manifest_for_tests(), asset(self.tmp))
        self.assertEqual((r.status, r.post_id), ("submitted", "P1"))
        init = [c for c in http.calls if "init" in c[1]][0][2]["json_body"]
        self.assertEqual(init["source_info"], {"source": "FILE_UPLOAD", "video_size": 1000, "chunk_size": 1000, "total_chunk_count": 1})
        put = [c for c in http.calls if c[0] == "PUT"][0][2]["headers"]
        self.assertEqual(put["Content-Range"], "bytes 0-999/1000")

    def test_direct_post_requires_allowed_privacy(self):
        http = FakeHttp([
            ("POST", "/oauth/token/", ok({"access_token": "AT"})),
            ("POST", "/creator_info/query/", ok({"data": {"privacy_level_options": ["SELF_ONLY"]}, "error": {"code": "ok"}})),
        ])
        with self.assertRaises(PublishError) as cm:
            pub(TikTokPublisher, http, TT_ENV, {"post_mode": "direct", "privacy_level": "PUBLIC_TO_EVERYONE"}).publish(
                manifest_for_tests(), asset(self.tmp))
        self.assertIn("SELF_ONLY", str(cm.exception))

    def test_direct_post_complete(self):
        http = FakeHttp([
            ("POST", "/creator_info/query/", ok({"data": {"privacy_level_options": ["SELF_ONLY"]}, "error": {"code": "ok"}})),
            ("POST", "/post/publish/video/init/", ok({"data": {"publish_id": "P2"}, "error": {"code": "ok"}})),
            ("POST", "/status/fetch/", ok({"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": []}})),
        ])
        env = {"TIKTOK_ACCESS_TOKEN": "direct-access-token"}
        r = pub(TikTokPublisher, http, env, {"post_mode": "direct", "source": "PULL_FROM_URL"}).publish(
            manifest_for_tests(), asset())
        self.assertEqual((r.status, r.post_id), ("published", "P2"))
        body = [c for c in http.calls if "video/init" in c[1]][0][2]["json_body"]
        self.assertEqual(body["post_info"]["privacy_level"], "SELF_ONLY")
        self.assertEqual(body["source_info"], {"source": "PULL_FROM_URL", "video_url": asset().public_url})
        self.assertFalse(any("oauth" in c[1] for c in http.calls))

    def test_failed_status(self):
        http = FakeHttp([
            ("POST", "/oauth/token/", ok({"access_token": "AT"})),
            ("POST", "/inbox/video/init/", ok({"data": {"publish_id": "P1", "upload_url": "https://up/u"}})),
            ("PUT", "https://up/u", ok()),
            ("POST", "/status/fetch/", ok({"data": {"status": "FAILED", "fail_reason": "file_format_check_failed"}})),
        ])
        with self.assertRaises(PublishError):
            pub(TikTokPublisher, http, TT_ENV).publish(manifest_for_tests(), asset(self.tmp))

    def test_api_error_code(self):
        http = FakeHttp([
            ("POST", "/oauth/token/", ok({"access_token": "AT"})),
            ("POST", "/inbox/video/init/", ok({"error": {"code": "spam_risk_too_many_pending_share", "message": "m"}})),
        ])
        with self.assertRaises(PublishError):
            pub(TikTokPublisher, http, TT_ENV).publish(manifest_for_tests(), asset(self.tmp))

    def test_missing_credentials(self):
        self.assertEqual(pub(TikTokPublisher, FakeHttp([]), {}).missing_credentials(), list(TT_ENV))
        self.assertEqual(pub(TikTokPublisher, FakeHttp([]), {"TIKTOK_ACCESS_TOKEN": "x"}).missing_credentials(), [])


# ---------------------------------------------------------------------------
class FakePublisher(Publisher):
    """pipeline テスト用。behavior: 'ok' / 'fail' / 'timeout' / 'boom' / 'submitted'"""
    behaviors = {}
    calls = []

    def build_post(self, manifest, asset):
        return {"caption": manifest["caption"]}

    def publish(self, manifest, asset):
        FakePublisher.calls.append((self.name, asset.video_asset_id))
        b = self.behaviors.get(self.name, "ok")
        if b == "fail":
            raise PublishError(f"{self.name}: HTTP 500 token=SECRETVALUE123")
        if b == "timeout":
            raise AmbiguousPublish(f"{self.name}: timeout")
        if b == "boom":
            raise RuntimeError("unexpected")
        if b == "submitted":
            return PublishResult("submitted", post_id=f"{self.name}-p", detail="inbox")
        return PublishResult("published", post_id=f"{self.name}-1", permalink=f"https://{self.name}/1")


def fake_publishers(required=None):
    out = {}
    for name in ("instagram", "facebook", "youtube", "tiktok"):
        out[name] = type(f"Fake{name}", (FakePublisher,), {"name": name, "required_env": (required or {}).get(name, ())})
    return out


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log_path = os.path.join(self.tmp, "log.json")
        self.out = []
        FakePublisher.calls = []
        FakePublisher.behaviors = {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pipeline(self, *, environ=None, required=None, config=None, articles=None):
        storage = st.LocalStorage(os.path.join(self.tmp, "storage"), base_url="https://media.example.com")
        return Pipeline(config or cfg(platforms={"tiktok": {"mode": "auto"}}),
                        http=FakeHttp([]), environ=environ if environ is not None else {"SOME_TOKEN": "SECRETVALUE123"},
                        run=fake_run_factory(), log_path=self.log_path, out_dir=os.path.join(self.tmp, "out"),
                        articles=articles or [make_article()], today=TODAY, storage=storage,
                        publishers=fake_publishers(required), printer=self.out.append)

    def run_p(self, p, mode="full-auto", ids=(501,), **kw):
        with mock_fonts(False), redirect_stdout(io.StringIO()):
            return p.run(list(ids), mode, skip_url_check=True, **kw)

    def statuses(self):
        return {(r["platform"]): r["status"] for r in load_log(self.log_path)["records"]}

    def test_full_auto_one_failure_does_not_stop_others(self):
        FakePublisher.behaviors = {"facebook": "fail"}
        code, summary = self.run_p(self.pipeline())
        self.assertEqual(code, 1)
        self.assertEqual(self.statuses(), {"instagram": "published", "facebook": "failed",
                                           "youtube": "published", "tiktok": "published"})
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "out", "501", "manual", "facebook.md")))
        log = load_log(self.log_path)
        self.assertEqual(len(log["assets"]), 1)
        self.assertTrue(log["assets"][0]["public_url"].startswith("https://media.example.com/social-video/501/sv-501-"))
        # 失敗理由に含まれる秘密値は伏せ字で記録・表示される
        self.assertNotIn("SECRETVALUE123", json.dumps(log, ensure_ascii=False))
        self.assertNotIn("SECRETVALUE123", "\n".join(self.out))

    def test_rerun_does_not_double_post(self):
        self.run_p(self.pipeline())
        self.assertEqual(len(FakePublisher.calls), 4)
        code, summary = self.run_p(self.pipeline())
        self.assertEqual(code, 0)
        self.assertEqual(len(FakePublisher.calls), 4)  # 2回目は投稿しない
        self.assertEqual({s["status"] for s in summary}, {"skipped_duplicate"})

    def test_rerun_retries_only_failed(self):
        FakePublisher.behaviors = {"youtube": "fail"}
        self.run_p(self.pipeline())
        FakePublisher.behaviors = {}
        FakePublisher.calls = []
        code, _ = self.run_p(self.pipeline())
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in FakePublisher.calls], ["youtube"])
        self.assertEqual(self.statuses()["youtube"], "published")

    def test_timeout_is_unknown_and_blocks_retry(self):
        FakePublisher.behaviors = {"instagram": "timeout"}
        code, _ = self.run_p(self.pipeline(), mode="publish-selected-platforms", platforms=["instagram"])
        self.assertEqual(code, 1)
        self.assertEqual(self.statuses()["instagram"], "unknown")
        FakePublisher.behaviors = {}
        FakePublisher.calls = []
        self.run_p(self.pipeline(), mode="publish-selected-platforms", platforms=["instagram"])
        self.assertEqual(FakePublisher.calls, [])
        # 人間が確認して failed にすれば再試行される
        mark(self.log_path, 501, "instagram", "failed", note="投稿されていないことを確認")
        self.run_p(self.pipeline(), mode="publish-selected-platforms", platforms=["instagram"])
        self.assertEqual([c[0] for c in FakePublisher.calls], ["instagram"])

    def test_force_repost(self):
        self.run_p(self.pipeline(), mode="publish-selected-platforms", platforms=["youtube"])
        self.run_p(self.pipeline(), mode="publish-selected-platforms", platforms=["youtube"], force_repost=True)
        self.assertEqual(len(FakePublisher.calls), 2)

    def test_missing_credentials_falls_back_to_manual(self):
        code, _ = self.run_p(self.pipeline(required={"tiktok": ("TIKTOK_CLIENT_KEY",)}))
        self.assertEqual(code, 0)
        self.assertEqual(self.statuses()["tiktok"], "manual_pending")
        kit = open(os.path.join(self.tmp, "out", "501", "manual", "tiktok.md"), encoding="utf-8").read()
        self.assertIn("TIKTOK_CLIENT_KEY", kit)
        self.assertIn("https://media.example.com/social-video/501/", kit)
        self.assertIn("python3 -m social_video mark", kit)
        # manual_pending は二重投稿防止の対象外(credential設定後に自動投稿できる)
        FakePublisher.calls = []
        self.run_p(self.pipeline())
        self.assertEqual([c[0] for c in FakePublisher.calls], ["tiktok"])

    def test_platform_manual_mode(self):
        p = self.pipeline(config=cfg())  # 既定は tiktok.mode=manual
        self.run_p(p)
        self.assertEqual(self.statuses()["tiktok"], "manual_pending")
        self.assertNotIn("tiktok", [c[0] for c in FakePublisher.calls])

    def test_unexpected_exception_is_isolated(self):
        FakePublisher.behaviors = {"instagram": "boom"}
        code, _ = self.run_p(self.pipeline())
        self.assertEqual(code, 1)
        self.assertEqual(self.statuses()["facebook"], "published")
        self.assertEqual(self.statuses()["instagram"], "failed")

    def test_dry_run_has_no_side_effects(self):
        code, summary = self.run_p(self.pipeline(required={"youtube": ("YOUTUBE_REFRESH_TOKEN",)}), mode="publish-dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(FakePublisher.calls, [])
        self.assertFalse(os.path.exists(self.log_path))
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "storage")))
        self.assertIn("credential未設定: YOUTUBE_REFRESH_TOKEN", "\n".join(self.out))

    def test_dry_run_shows_existing_duplicates(self):
        self.run_p(self.pipeline(), mode="publish-selected-platforms", platforms=["youtube"])
        _, summary = self.run_p(self.pipeline(), mode="publish-dry-run")
        self.assertEqual({s["platform"]: s["status"] for s in summary}["youtube"], "skipped_duplicate")

    def test_generate_only(self):
        code, _ = self.run_p(self.pipeline(), mode="generate-only")
        self.assertEqual(code, 0)
        self.assertEqual(FakePublisher.calls, [])
        self.assertEqual(set(self.statuses().values()), {"manual_pending"})
        adir = os.path.join(self.tmp, "out", "501")
        self.assertTrue(os.path.isfile(os.path.join(adir, "manifest.json")))
        self.assertTrue(any(f.endswith(".mp4") for f in os.listdir(adir)))
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "storage", "social-video", "501")))

    def test_selected_platforms_only(self):
        self.run_p(self.pipeline(), mode="publish-selected-platforms", platforms=["instagram", "facebook"])
        self.assertEqual([c[0] for c in FakePublisher.calls], ["instagram", "facebook"])
        # Instagram と Facebook は同じ動画(同じ video_asset_id)を使う
        self.assertEqual(len({c[1] for c in FakePublisher.calls}), 1)

    def test_unpublished_article_is_skipped_and_others_continue(self):
        arts = [make_article(), make_article(id=502, slug="x", date="2027-01-01")]
        code, summary = self.run_p(self.pipeline(articles=arts), ids=(502, 501))
        self.assertEqual(code, 1)
        self.assertEqual(summary[0]["status"], "error")
        self.assertEqual(len(FakePublisher.calls), 4)

    def test_without_storage_meta_platforms_go_manual(self):
        """R2未設定でも動画生成とYouTube等は進む(実publisherは公開URLが無いとPublishError→手動キット)。"""
        p = Pipeline(cfg(), http=FakeHttp([]), environ={}, run=fake_run_factory(), log_path=self.log_path,
                     out_dir=os.path.join(self.tmp, "out"), articles=[make_article()], today=TODAY,
                     printer=self.out.append)
        code, summary = self.run_p(p)
        self.assertEqual(code, 0)
        self.assertIn("Cloudflare R2", "\n".join(self.out))
        self.assertEqual(set(self.statuses().values()), {"manual_pending"})


class DistributionLogTest(unittest.TestCase):
    def test_merge_prefers_newer(self):
        a = {"records": [{"article_id": 1, "video_asset_id": "v", "platform": "youtube", "status": "unknown",
                          "updated_at": "2026-10-01T00:00:00Z"}]}
        b = {"records": [{"article_id": 1, "video_asset_id": "v", "platform": "youtube", "status": "published",
                          "updated_at": "2026-10-02T00:00:00Z"}], "assets": [{"video_asset_id": "v"}]}
        m = merge_logs(a, b)
        self.assertEqual([r["status"] for r in m["records"]], ["published"])
        self.assertEqual(len(m["assets"]), 1)
        self.assertEqual(merge_logs(b, a), m)

    def test_blocking(self):
        log = DistributionLog()
        log.upsert(1, "v1", "tiktok", "failed")
        self.assertIsNone(log.blocking_record(1, "tiktok"))
        log.upsert(1, "v1", "tiktok", "submitted", post_id="p")
        self.assertEqual(log.blocking_record(1, "tiktok")["post_id"], "p")
        # 動画を作り直しても(asset違い)同じ記事・媒体には再投稿しない
        self.assertIsNotNone(log.blocking_record("1", "tiktok"))
        with self.assertRaises(ValueError):
            log.upsert(1, "v1", "tiktok", "bogus")


if __name__ == "__main__":
    unittest.main()
