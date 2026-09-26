# -*- coding: utf-8 -*-
"""公式画像の読取り確度が足りない場合の人間確認(image_reading)と、オーナーの目視確認を
読取り補助にした再開(official_image.py / resume_image_check.py)、verify モードの公式画像のテスト。
APIもネットワークも使わない(HTTP取得・Anthropicクライアントはスタブに差し替える)。
実行: python -m unittest tests/test_official_image.py -v
"""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))
import fact_audit as fa  # noqa: E402
import fact_verify as fv  # noqa: E402
import official_image as oi  # noqa: E402
import publish_gate as pg  # noqa: E402
import report_gate_rejections as rgr  # noqa: E402
import resume_image_check as ric  # noqa: E402

PAGE = "https://shonan.terracemall.com/event/detail/?cd=001233"
IMG = "https://shonan-terracemall.pictona.jp/shops.png"


def text_claim(text="10月3・4日開催", role="central"):
    return {"claim": text, "role": role, "status": "confirmed", "basis": "primary_page",
            "imageReading": "not_applicable", "articleValue": "", "primaryValue": "",
            "logicallyCompatible": True, "primaryUrl": PAGE, "note": ""}


def img_claim(status="confirmed", reading="clear", role="dek", av="20店", pv="20店", img=IMG):
    return {"claim": "珈琲店が20店出店", "role": role, "status": status, "basis": "official_image",
            "imageReading": reading, "articleValue": av, "primaryValue": pv,
            "logicallyCompatible": status != "contradicted", "primaryUrl": PAGE, "imageUrl": img, "note": ""}


def raw_audit(claims, attached=None, readings=()):
    raw = {"primarySources": [PAGE], "claims": claims, "hasQuotedComment": False, "needsHuman": False,
           "verdict": "confirmed", "summary": "テスト", "_ownerReadings": list(readings)}
    if attached is not None:
        raw["_attachedImages"] = attached
    return raw


def normalized(claims, **kw):
    return fa.normalize_result(raw_audit(claims, **kw), 1)[0]


ENTRY = {"id": "today-run-pending-id", "articleType": "news", "cat": "g", "area": "藤沢", "scene": "",
         "title": "湘南海街珈琲祭2026", "dek": "湘南の珈琲店20店が集まる2日間。",
         "link": PAGE, "sources": [PAGE], "subjectNames": ["湘南海街珈琲祭"], "date": "2026-09-26",
         "body": "テラスモール湘南で10月3・4日に珈琲祭が開かれる。\n会場には珈琲店が並ぶ。入場は無料。",
         "tags": [], "slug": ""}
AMBIGUOUS = img_claim(status="not_found_in_primary", reading="ambiguous", av="20店", pv="20店(または26店)")


def reading(value, **kw):
    return dict({"imageUrl": IMG, "pageUrl": PAGE, "item": "珈琲店が20店出店", "value": value}, **kw)


class ImageReadingEscalationTest(unittest.TestCase):
    def test_notification_shows_image_candidate_and_item(self):
        r = normalized([text_claim(), dict(AMBIGUOUS)])
        esc = pg.escalation(ENTRY, r)
        self.assertEqual(esc["kind"], "image_reading")
        self.assertEqual(esc["imageChecks"][0]["imageUrl"], IMG)
        text = pg.format_escalation(esc)
        self.assertEqual(text.split("\n"), [
            "対象トピック: 湘南海街珈琲祭2026",
            f"公式画像: {IMG}(掲載ページ: {PAGE})",
            "AIが読み取れた候補: 20店(または26店)",
            "確認してほしい項目: 珈琲店が20店出店が「20店」で正しいか",
            f"[Approve] {esc['approve']}",
            f"[Reject] {esc['reject']}",
        ])

    def test_other_problems_are_not_image_reading(self):
        wrong = dict(text_claim(), status="contradicted", articleValue="10月3日", primaryValue="10月10日",
                     logicallyCompatible=False)
        r = normalized([wrong, dict(AMBIGUOUS)])
        self.assertNotEqual((pg.escalation(ENTRY, r) or {}).get("kind"), "image_reading")
        r = dict(normalized([text_claim(), dict(AMBIGUOUS)]), needsHuman=True)
        self.assertEqual(pg.escalation(ENTRY, r)["kind"], "source_conflict")

    def test_clear_image_needs_no_human(self):
        r = normalized([text_claim(), img_claim()])
        self.assertTrue(pg.evaluate(r)[0])
        self.assertIsNone(pg.escalation(ENTRY, r))

    def gate(self):
        r = normalized([text_claim(), dict(AMBIGUOUS)])
        passed, reasons, blocking = pg.evaluate(r)
        return {"passed": passed, "reasons": reasons, "claims": [pg._claim_summary(c) for c in blocking],
                "verdict": r["verdict"], "escalation": pg.escalation(ENTRY, r), "candidate": ENTRY}

    def test_issue_is_concise_and_resumable(self):
        rec = pg.rejection_record(ENTRY, self.gate(), "news")
        self.assertEqual(rec["imageChecks"][0]["candidate"], "20店(または26店)")
        body = rgr.image_check_body(rec, "2026-09-26")
        for label in ("対象トピック", "公式画像", "AIが読み取れた候補", "確認してほしい項目", "[Approve]", "[Reject]"):
            self.assertIn(label, body)
        self.assertNotIn("<details>", body)  # 長い説明は付けない
        payload = rgr.decode_payload(body)
        self.assertEqual(payload["draft"]["title"], ENTRY["title"])
        self.assertEqual(payload["imageChecks"][0]["imageUrl"], IMG)

    def test_report_uses_image_check_title(self):
        rec = pg.rejection_record(ENTRY, self.gate(), "news")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "report.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"date": "2026-09-26", "gate_rejected": [rec]}, f, ensure_ascii=False)
            posted = []

            def request(method, url, token, payload=None):
                if method == "POST":
                    posted.append(payload)
                    return {}
                return []
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r"}):
                self.assertEqual(rgr.main(["--report", path], request=request), 0)
        self.assertEqual(posted[0]["title"], f"{rgr.IMAGE_CHECK_TITLE_PREFIX} {ENTRY['title']} (2026-09-26)")


class AttachedImageRuleTest(unittest.TestCase):
    def test_image_not_attached_in_this_audit_is_not_evidence(self):
        r = normalized([text_claim(), img_claim()], attached=[{"pageUrl": PAGE, "imageUrl": IMG + "?other"}])
        self.assertEqual(r["claims"][1]["status"], "not_found_in_primary")
        self.assertIn("添付した公式画像ではない", r["claims"][1]["note"])

    def test_attached_image_is_evidence(self):
        r = normalized([text_claim(), img_claim()], attached=[{"pageUrl": PAGE, "imageUrl": IMG}])
        self.assertEqual(r["verdict"], "confirmed")


class FakeClient:
    def __init__(self, submit):
        self.submit, self.messages, self.sent = submit, self, []

    def create(self, **kw):
        self.sent.append(kw)
        block = types.SimpleNamespace(type="tool_use", name="submit_audit", input=self.submit)
        return types.SimpleNamespace(content=[block], stop_reason="tool_use")


class OwnerReadingTest(unittest.TestCase):
    def test_owner_readings_for_article(self):
        conf = [reading("20店", decision="approve"), reading("x", decision="reject"),
                reading("y", decision="approve", pageUrl="https://other.example.jp/", imageUrl="https://other.example.jp/a.png")]
        readings = oi.owner_readings_for(ENTRY, conf)
        self.assertEqual([r["value"] for r in readings], ["20店"])
        self.assertIn("ownerImageReadings", fa.article_prompt(ENTRY, readings))
        self.assertNotIn("ownerImageReadings", fa.article_prompt(ENTRY))

    def test_owner_reading_confirms_ambiguous_claim(self):
        r = normalized([text_claim(), dict(AMBIGUOUS)], readings=[reading("20店")])
        c = r["claims"][1]
        self.assertEqual((c["status"], c["imageReading"]), ("confirmed", "clear"))
        self.assertEqual(r["verdict"], "confirmed")
        self.assertTrue(pg.evaluate(r)[0])

    def test_owner_value_differs_then_autofix_is_unique(self):
        r = normalized([text_claim(), dict(AMBIGUOUS)], readings=[reading("26店")])
        c = r["claims"][1]
        self.assertEqual((c["status"], c["primaryValue"]), ("contradicted", "26店"))
        fixed, fixes = pg.safe_autofix(ENTRY, r)
        self.assertIsNotNone(fixed)
        self.assertIn("26店", fixed["dek"])

    def test_audit_one_passes_readings_and_records_attached_images(self):
        client = FakeClient(raw_audit([text_claim(), dict(AMBIGUOUS)]))
        images = [{"pageUrl": PAGE, "imageUrl": IMG, "mediaType": "image/png", "data": "AAAA"}]
        raw = fa.audit_one(client, ENTRY, [], images=images, confirmations=[reading("20店", decision="approve")])
        self.assertEqual(raw["_attachedImages"], [{"pageUrl": PAGE, "imageUrl": IMG}])
        self.assertEqual(raw["_ownerReadings"][0]["value"], "20店")
        first = client.sent[0]["messages"][0]["content"]
        self.assertIn("ownerImageReadings", first[0]["text"])
        self.assertEqual(fa.normalize_result(raw, 1)[0]["verdict"], "confirmed")

    def test_check_draft_uses_confirmations(self):
        client = FakeClient(raw_audit([text_claim(), dict(AMBIGUOUS)]))
        with mock.patch.object(fa, "official_page_images", return_value=[]):
            gate = pg.check_draft(dict(ENTRY), client=client, confirmations=[reading("20店", decision="approve")])
        self.assertTrue(gate["passed"], gate["reasons"])
        with mock.patch.object(fa, "official_page_images", return_value=[]):
            gate = pg.check_draft(dict(ENTRY), client=FakeClient(raw_audit([text_claim(), dict(AMBIGUOUS)])),
                                  confirmations=[])
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["escalation"]["kind"], "image_reading")


class FetchClaimImageTest(unittest.TestCase):
    HTML = f'<img src="/logo.png"><img src="{IMG}">'.encode()

    def getter(self, pages):
        def get(url, limit):
            if url not in pages:
                raise OSError("not found")
            return pages[url]
        return get

    def test_image_on_official_page_is_fetched(self):
        get = self.getter({PAGE: (self.HTML, "text/html", "utf-8"), IMG: (b"\x89PNG", "image/png", None)})
        block = oi.fetch_claim_image(img_claim(), getter=get)
        self.assertEqual(block["type"], "image")

    def test_image_not_on_page_or_secondary_is_refused(self):
        get = self.getter({PAGE: (b"<p>no image</p>", "text/html", "utf-8"), IMG: (b"\x89PNG", "image/png", None)})
        self.assertIsNone(oi.fetch_claim_image(img_claim(), getter=get))
        c = dict(img_claim(), primaryUrl="https://www.townnews.co.jp/0605/2026/09/01/1.html")
        self.assertIsNone(oi.fetch_claim_image(c, getter=get))


class VerifyWithImageTest(unittest.TestCase):
    def test_image_claim_is_sent_with_image(self):
        sent = {}

        class Client:
            def __init__(self):
                self.messages = self

            def create(self, **kw):
                sent.update(kw)
                block = types.SimpleNamespace(type="tool_use", name="submit_verification", input={
                    "results": [{"index": 0, "result": "resolved", "logicallyCompatible": True, "reason": "画像と一致"}]})
                return types.SimpleNamespace(content=[block], usage=None)

        article = {"id": 1, "title": ENTRY["title"], "dek": "湘南の珈琲店が集まる2日間。",
                   "body": "出店は珈琲店が中心。会場は1階の広場。"}
        c = img_claim(status="contradicted", role="detail", av="30店", pv="20店")
        c["claim"] = "会場は1階の広場"
        stats = fv.new_stats()
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
        res = fv.verify_article(article, [c], Client, lambda url: "", stats, image_fetcher=lambda _c: image)
        self.assertEqual(res[0]["verifyResult"], "resolved")
        self.assertIn(image, sent["messages"][0]["content"])


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        self.conf = os.path.join(d, "image_confirmations.json")
        self.arts = os.path.join(d, "articles.json")
        self.counter = os.path.join(d, "id_counter.json")
        with open(self.arts, "w", encoding="utf-8") as f:
            json.dump([], f)
        with open(self.counter, "w", encoding="utf-8") as f:
            json.dump({"next_id": 200}, f)
        self.payload = {"imageChecks": [pg.image_check_item(AMBIGUOUS)], "draft": dict(ENTRY)}
        self.patch = mock.patch.object(ric.g, "ID_COUNTER_PATH", self.counter)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.dir.cleanup()

    def run_resume(self, decision, passed=True, value=""):
        seen = {}

        def check_draft(entry, client=None, confirmations=None):
            seen["confirmations"] = confirmations
            return {"passed": passed, "entry": entry, "reasons": [] if passed else ["不合格"], "autofix": []}
        out = ric.resume(self.payload, decision, value=value, issue="40", confirmations_path=self.conf,
                         articles_path=self.arts, check_draft=check_draft)
        return out, seen

    def test_approve_records_reading_and_publishes_after_reaudit(self):
        (code, _msg, entry), seen = self.run_resume("approve")
        self.assertEqual(code, 0)
        self.assertEqual(entry["id"], 200)
        conf = seen["confirmations"][0]
        self.assertEqual((conf["value"], conf["imageUrl"], conf["pageUrl"]), ("20店", IMG, PAGE))
        # 記録した確認結果は、次の監査で同じ記事の読取り補助として渡される
        self.assertEqual(oi.owner_readings_for(ENTRY, seen["confirmations"])[0]["value"], "20店")
        with open(self.arts, encoding="utf-8") as f:
            self.assertEqual([a["id"] for a in json.load(f)], [200])

    def test_approve_with_corrected_value(self):
        (_code, _m, _e), seen = self.run_resume("approve", value="26店")
        self.assertEqual(seen["confirmations"][0]["value"], "26店")

    def test_reaudit_fail_publishes_nothing(self):
        (code, _m, entry), _ = self.run_resume("approve", passed=False)
        self.assertEqual(code, 3)
        self.assertIsNone(entry)
        with open(self.arts, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])

    def test_reject_skips_article(self):
        (code, _msg, entry), seen = self.run_resume("reject")
        self.assertEqual(code, 0)
        self.assertIsNone(entry)
        self.assertNotIn("confirmations", seen)  # 再監査しない
        with open(self.conf, encoding="utf-8") as f:
            self.assertEqual(json.load(f)[0]["decision"], "reject")

    def test_value_with_multiple_items_is_refused(self):
        self.payload["imageChecks"].append(dict(self.payload["imageChecks"][0], claim="開催期間"))
        (code, _m, _e), _ = self.run_resume("approve", value="10/1〜10/15")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
