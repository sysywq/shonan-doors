# -*- coding: utf-8 -*-
"""Facebookページ自動投稿(post_to_facebook.py / facebook_post_log_store.py)のテスト。
Graph API・git・URL確認はすべてスタブに差し替え、実APIは呼ばない。
実行: python -m unittest tests/test_post_to_facebook.py -v
"""
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import facebook_post_log_store as fls  # noqa: E402
import post_to_facebook as pf  # noqa: E402

TOKEN = "EAAB-secret-page-token-123"
ENV = {"FACEBOOK_PAGE_ID": "1234567890", "FACEBOOK_PAGE_ACCESS_TOKEN": TOKEN}


def article(i, **kw):
    a = {"id": i, "slug": f"fujisawa-event-{i:04d}", "title": f"記事タイトル{i}",
         "dek": "藤沢で開かれるイベントの紹介。会場は海岸。", "area": "藤沢", "cat": "e"}
    a.update(kw)
    return a


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, payload):
    return urllib.error.HTTPError("https://graph.facebook.com/x", code, "err", {},
                                  io.BytesIO(json.dumps(payload).encode("utf-8")))


class FakeGraph:
    """_urlopen の代わり。GET /posts と POST /feed を記録し、指定どおりに応答・失敗する。"""

    def __init__(self, recent=None, post_result=None, get_error=None):
        self.recent = recent or []
        self.post_result = post_result
        self.get_error = get_error
        self.requests = []
        self.counter = 0

    def __call__(self, req, timeout):
        self.requests.append(req)
        if req.get_method() == "GET":
            if self.get_error:
                raise self.get_error
            if "/posts?" not in req.full_url:
                return FakeResponse({"id": "1234567890", "name": "湘南Doors"})
            return FakeResponse({"data": self.recent})
        if isinstance(self.post_result, BaseException):
            raise self.post_result
        self.counter += 1
        return FakeResponse(self.post_result or {"id": f"1234567890_{self.counter}"})

    def posts(self):
        return [r for r in self.requests if r.get_method() == "POST"]


class FacebookTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.articles_path = os.path.join(self.tmp.name, "articles.json")
        self.log_path = os.path.join(self.tmp.name, "facebook_post_log.json")
        self.write_articles([article(1), article(2)])
        for name, value in (("ARTICLES_JSON_PATH", self.articles_path), ("FACEBOOK_POST_LOG_PATH", self.log_path)):
            p = mock.patch.object(pf, name, value)
            p.start()
            self.addCleanup(p.stop)

    def write_articles(self, articles):
        with open(self.articles_path, "w", encoding="utf-8") as f:
            json.dump(articles, f, ensure_ascii=False)

    def write_log(self, posts):
        with open(self.log_path, "w", encoding="utf-8") as f:
            json.dump({"posts": posts}, f, ensure_ascii=False)

    def read_log(self):
        with open(self.log_path, encoding="utf-8") as f:
            return json.load(f)["posts"]

    def run_main(self, ids, env=None, graph=None, check=lambda url: True, extra=()):
        graph = graph or FakeGraph()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(pf, "_urlopen", graph), redirect_stdout(out), redirect_stderr(err):
            code = pf.main(["--article-ids", ids, *extra], env=ENV if env is None else env,
                           check=check, sleep=lambda s: None)
        return code, graph, out.getvalue() + err.getvalue()


class MessageTest(unittest.TestCase):
    def test_message_is_title_intro_and_url(self):
        msg = pf.build_post_message(article(7))
        self.assertEqual(msg, "記事タイトル7\n\n藤沢で開かれるイベントの紹介。会場は海岸。\n\n"
                              "https://www.shonandoors.com/articles/fujisawa-event-0007/")

    def test_long_intro_is_shortened(self):
        long_dek = "あ" * 200
        msg = pf.build_post_message(article(7, dek=long_dek))
        intro = msg.split("\n\n")[1]
        self.assertEqual(len(intro), pf.INTRO_MAX_CHARS)
        self.assertTrue(intro.endswith("…"))
        self.assertTrue(msg.endswith("/articles/fujisawa-event-0007/"))

    def test_long_intro_prefers_sentence_boundary(self):
        dek = "い" * 80 + "。" + "う" * 100
        self.assertEqual(pf.shorten(dek, pf.INTRO_MAX_CHARS), "い" * 80 + "。")

    def test_missing_dek_omits_intro(self):
        self.assertEqual(pf.build_post_message(article(7, dek="")).count("\n\n"), 1)

    def test_only_production_article_urls(self):
        self.assertEqual(pf.article_url(article(3)), "https://www.shonandoors.com/articles/fujisawa-event-0003/")
        for bad in ("", "../x", "https://evil.example/", "Fujisawa", "a/b", "a b"):
            with self.assertRaises(ValueError):
                pf.article_url(article(3, slug=bad))


class ConfigTest(unittest.TestCase):
    def test_credentials_states(self):
        self.assertEqual(pf.resolve_credentials(ENV)[0], "ok")
        self.assertEqual(pf.resolve_credentials({})[0], "not_configured")
        self.assertEqual(pf.resolve_credentials({"FACEBOOK_PAGE_ID": "123"})[0], "invalid")
        self.assertEqual(pf.resolve_credentials({"FACEBOOK_PAGE_ACCESS_TOKEN": TOKEN})[0], "invalid")
        self.assertEqual(pf.resolve_credentials({"FACEBOOK_PAGE_ID": "my-page",
                                                 "FACEBOOK_PAGE_ACCESS_TOKEN": TOKEN})[0], "invalid")

    def test_graph_version_is_overridable(self):
        self.assertEqual(pf.graph_base({}), "https://graph.facebook.com")
        self.assertEqual(pf.graph_base({"FACEBOOK_GRAPH_API_VERSION": "v24.0"}), "https://graph.facebook.com/v24.0")
        with self.assertRaises(ValueError):
            pf.graph_base({"FACEBOOK_GRAPH_API_VERSION": "24/../../x"})


class MainTest(FacebookTestCase):
    def test_posts_and_records_log(self):
        code, graph, out = self.run_main("1,2")
        self.assertEqual(code, 0)
        self.assertEqual(len(graph.posts()), 2)
        req = graph.posts()[0]
        self.assertEqual(req.full_url, "https://graph.facebook.com/1234567890/feed")
        body = dict(x.split("=", 1) for x in req.data.decode().split("&"))
        self.assertIn("link", body)
        self.assertEqual(body.get("access_token"), TOKEN)
        self.assertIsNone(req.get_header("Authorization"))
        log = self.read_log()
        self.assertEqual([r["article_id"] for r in log], [1, 2])
        self.assertEqual(log[0]["facebook_post_id"], "1234567890_1")
        self.assertEqual(log[0]["article_url"], "https://www.shonandoors.com/articles/fujisawa-event-0001/")
        self.assertTrue(log[0]["posted_at"].endswith("Z"))
        self.assertNotIn(TOKEN, out)

    def test_rerun_skips_posted_articles(self):
        self.run_main("1,2")
        code, graph, out = self.run_main("1,2")
        self.assertEqual(code, 0)
        self.assertEqual(graph.posts(), [])
        self.assertEqual(graph.requests, [])  # ログで投稿済みなら Graph API も呼ばない

    def test_existing_page_post_prevents_duplicate(self):
        recent = [{"id": "1234567890_99",
                   "message": "記事タイトル1\n\nhttps://www.shonandoors.com/articles/fujisawa-event-0001/"}]
        code, graph, _ = self.run_main("1,2", graph=FakeGraph(recent=recent))
        self.assertEqual(code, 0)
        self.assertEqual(len(graph.posts()), 1)
        log = {r["article_id"]: r for r in self.read_log()}
        self.assertEqual(log[1]["facebook_post_id"], "1234567890_99")

    def test_cannot_check_page_posts_fails_closed(self):
        graph = FakeGraph(get_error=http_error(403, {"error": {"message": "(#10) permission", "code": 10}}))
        code, graph, out = self.run_main("1", graph=graph)
        self.assertEqual(code, 1)
        self.assertEqual(graph.posts(), [])
        self.assertFalse(os.path.exists(self.log_path))

    def test_api_error_is_not_recorded_and_can_retry(self):
        err = http_error(400, {"error": {"message": f"Invalid OAuth access token {TOKEN}", "code": 190,
                                          "fbtrace_id": "AbC"}})
        code, graph, out = self.run_main("1", graph=FakeGraph(post_result=err))
        self.assertEqual(code, 1)
        self.assertIn("code=190", out)
        self.assertIn("fbtrace_id=AbC", out)
        self.assertNotIn(TOKEN, out)
        self.assertFalse(os.path.exists(self.log_path))
        code, graph, _ = self.run_main("1")
        self.assertEqual(code, 0)
        self.assertEqual(len(graph.posts()), 1)

    def test_timeout_is_recorded_as_uncertain_and_never_reposted(self):
        code, graph, out = self.run_main("1,2", graph=FakeGraph(post_result=socket.timeout("timed out")))
        self.assertEqual(code, 1)
        log = {r["article_id"]: r for r in self.read_log()}
        self.assertEqual(log[1]["status"], "uncertain")
        self.assertIsNone(log[1]["facebook_post_id"])
        self.assertEqual(len(graph.posts()), 2)  # 1件目の失敗で2件目を巻き込まない
        code, graph, _ = self.run_main("1,2")
        self.assertEqual(code, 0)
        self.assertEqual(graph.posts(), [])

    def test_uncertain_record_is_resolved_from_page_posts(self):
        self.write_log([{"article_id": 1, "facebook_post_id": None, "posted_at": "2026-10-01T00:00:00Z",
                         "article_url": "https://www.shonandoors.com/articles/fujisawa-event-0001/",
                         "status": "uncertain"}])
        recent = [{"id": "1234567890_5", "message": "x https://www.shonandoors.com/articles/fujisawa-event-0001/"}]
        code, graph, _ = self.run_main("1", graph=FakeGraph(recent=recent))
        self.assertEqual(code, 0)
        self.assertEqual(graph.posts(), [])
        self.assertEqual(self.read_log()[0]["facebook_post_id"], "1234567890_5")
        self.assertEqual(self.read_log()[0]["status"], "posted")

    def test_server_error_is_uncertain(self):
        code, _, _ = self.run_main("1", graph=FakeGraph(post_result=http_error(500, {"error": {"code": 2}})))
        self.assertEqual(code, 1)
        self.assertEqual(self.read_log()[0]["status"], "uncertain")

    def test_unpublished_url_is_not_posted(self):
        code, graph, _ = self.run_main("1", check=lambda url: False)
        self.assertEqual(code, 1)
        self.assertEqual(graph.posts(), [])

    def test_merged_and_unknown_articles_are_skipped(self):
        self.write_articles([article(1, mergedInto=2), article(2)])
        code, graph, _ = self.run_main("1,2,999")
        self.assertEqual(code, 0)
        self.assertEqual(len(graph.posts()), 1)
        self.assertEqual([r["article_id"] for r in self.read_log()], [2])

    def test_missing_both_secrets_skips(self):
        code, graph, out = self.run_main("1", env={})
        self.assertEqual(code, 0)
        self.assertIn("未設定", out)
        self.assertEqual(graph.requests, [])

    def test_missing_one_secret_fails(self):
        code, graph, out = self.run_main("1", env={"FACEBOOK_PAGE_ID": "1234567890"})
        self.assertEqual(code, 2)
        self.assertIn("FACEBOOK_PAGE_ACCESS_TOKEN", out)
        self.assertEqual(graph.requests, [])

    def test_dry_run_calls_nothing_and_writes_nothing(self):
        checked = []
        code, graph, out = self.run_main("1,2", env={"DRY_RUN": "true"}, check=checked.append)
        self.assertEqual(code, 0)
        self.assertEqual(graph.requests, [])
        self.assertEqual(checked, [])
        self.assertFalse(os.path.exists(self.log_path))
        self.assertIn("https://www.shonandoors.com/articles/fujisawa-event-0001/", out)

    def test_empty_ids_is_noop(self):
        code, graph, _ = self.run_main("")
        self.assertEqual(code, 0)
        self.assertEqual(graph.requests, [])

    def test_smoke_reads_only(self):
        code, graph, out = self.run_main("", extra=("--smoke",))
        self.assertEqual(code, 0)
        self.assertIn("湘南Doors", out)
        self.assertEqual(graph.posts(), [])
        self.assertNotIn(TOKEN, out)

    def test_smoke_without_secrets_fails(self):
        code, graph, _ = self.run_main("", env={}, extra=("--smoke",))
        self.assertEqual(code, 2)
        self.assertEqual(graph.requests, [])

    def test_broken_log_fails_closed(self):
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write("{broken")
        code, graph, _ = self.run_main("1")
        self.assertEqual(code, 1)
        self.assertEqual(graph.requests, [])


def completed(rc, stdout="", stderr=""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


class LogStoreTest(unittest.TestCase):
    def test_merge_prefers_confirmed_post(self):
        a = {"posts": [{"article_id": 1, "facebook_post_id": None, "posted_at": "2026-09-01T00:00:00Z",
                        "status": "uncertain"}]}
        b = {"posts": [{"article_id": 1, "facebook_post_id": "p9", "posted_at": "2026-09-02T00:00:00Z",
                        "status": "posted"},
                       {"article_id": 2, "facebook_post_id": "p2", "posted_at": "2026-09-03T00:00:00Z"}]}
        merged = fls.merge_logs(a, b)
        self.assertEqual([(r["article_id"], r["facebook_post_id"]) for r in merged["posts"]], [(1, "p9"), (2, "p2")])
        self.assertEqual(fls.merge_logs(None, {"posts": [{"x": 1}]}), {"posts": []})

    def test_missing_branch_is_empty(self):
        git = mock.Mock(return_value=completed(2))
        self.assertEqual(fls.fetch_remote(git), (None, None))

    def test_ls_remote_failure_fails_closed(self):
        git = mock.Mock(return_value=completed(128, stderr="network"))
        with self.assertRaises(fls.StoreError):
            fls.fetch_remote(git)

    def test_fetch_failure_fails_closed(self):
        git = mock.Mock(side_effect=[completed(0), completed(1, stderr="fetch failed")])
        with self.assertRaises(fls.StoreError):
            fls.fetch_remote(git)

    def test_broken_remote_log_fails_closed(self):
        git = mock.Mock(side_effect=[completed(0), completed(0), completed(0, "abc\n"), completed(0, "{broken")])
        with self.assertRaises(fls.StoreError):
            fls.fetch_remote(git)

    def test_pull_returns_error_when_remote_unreadable(self):
        with mock.patch.object(fls, "fetch_remote", side_effect=fls.StoreError("x")), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(fls.pull(), 1)


if __name__ == "__main__":
    unittest.main()
