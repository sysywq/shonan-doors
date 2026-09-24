# -*- coding: utf-8 -*-
"""fact_audit.py のテスト(APIは呼ばない。クライアントをモックに差し替える)
実行: python -m unittest tests/test_fact_audit.py -v
"""
import json
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))
import fact_audit as fa  # noqa: E402

GOOD = {
    "primarySources": ["https://example.com/"],
    "claims": [
        {"claim": "9月4日オープン", "status": "confirmed", "primaryUrl": "https://example.com/", "note": ""},
        {"claim": "40席", "status": "not_found_in_primary", "primaryUrl": "", "note": "公式に記載なし"},
    ],
    "hasQuotedComment": False,
    "verdict": "fix",
    "summary": "席数が一次情報で確認できない",
}


def article(i):
    return {"id": i, "slug": f"test-{i:04d}", "title": f"テスト記事{i}", "area": "藤沢", "body": "本文"}


class FakeBlock:
    def __init__(self, inp):
        self.type, self.name, self.input = "tool_use", "submit_audit", inp


class FakeResp:
    def __init__(self, inp):
        self.content, self.stop_reason = [FakeBlock(inp)], "tool_use"


class FakeClient:
    """記事IDごとに返す応答(または送出する例外)を決められるモック"""
    def __init__(self, by_id):
        self.by_id = by_id
        self.calls = []
        self.messages = self

    def create(self, **kw):
        aid = json.loads(kw["messages"][0]["content"].split("\n\n", 1)[1])["id"]
        self.calls.append(aid)
        v = self.by_id[aid]
        if isinstance(v, Exception):
            raise v
        return FakeResp(v)


class NormalizeTest(unittest.TestCase):
    def test_good_dict(self):
        r, an = fa.normalize_result(GOOD, 1)
        self.assertEqual(an, [])
        self.assertEqual(r["verdict"], "fix")
        self.assertEqual(len(r["claims"]), 2)

    def test_ok_is_confirmed(self):
        r, an = fa.normalize_result(dict(GOOD, verdict="ok"), 1)
        self.assertEqual((r["verdict"], an), ("confirmed", []))

    def test_whole_response_is_plain_str(self):
        r, an = fa.normalize_result("申し訳ありませんが照合できませんでした", 2)
        self.assertEqual(r["verdict"], "review_required")
        self.assertEqual(an[0]["type"], "str")
        self.assertIn("照合できません", an[0]["snippet"])

    def test_whole_response_is_json_str(self):
        r, an = fa.normalize_result(json.dumps(GOOD, ensure_ascii=False), 3)
        self.assertEqual((r["verdict"], an), ("fix", []))

    def test_claims_as_json_string(self):
        raw = dict(GOOD, claims=json.dumps(GOOD["claims"], ensure_ascii=False))
        r, an = fa.normalize_result(raw, 4)
        self.assertEqual((r["verdict"], an), ("fix", []))

    def test_claims_elements_are_str(self):  # 今回のクラッシュの再現ケース
        raw = dict(GOOD, claims=["9月4日オープン", "40席"])
        r, an = fa.normalize_result(raw, 5)
        self.assertEqual(r["verdict"], "review_required")
        self.assertEqual([c["status"] for c in r["claims"]], ["unparsed", "unparsed"])
        self.assertEqual(an[0]["field"], "claims[0]")

    def test_missing_fields(self):
        r, an = fa.normalize_result({"summary": "途中まで"}, 6)
        self.assertEqual(r["verdict"], "review_required")
        self.assertTrue(any(a["field"] == "claims" for a in an))
        self.assertTrue(any(a["field"] == "verdict" for a in an))

    def test_claim_missing_status_and_bad_types(self):
        raw = dict(GOOD, claims=[{"claim": "x"}], primarySources="https://a", hasQuotedComment="true", verdict="maybe")
        r, an = fa.normalize_result(raw, 7)
        self.assertEqual(r["verdict"], "review_required")
        self.assertEqual(r["claims"][0]["status"], "unparsed")
        self.assertTrue(r["hasQuotedComment"])

    def test_none_and_list_response(self):
        for raw in (None, [1, 2], 123):
            r, _ = fa.normalize_result(raw, 8)
            self.assertEqual(r["verdict"], "review_required")

    def test_report_survives_legacy_unnormalized_results(self):
        # 正規化前の形式(claimsに文字列、verdict欠損)が混ざってもレポート生成は落ちない
        report, counts = fa.build_report([
            {"id": 1, "title": "a", "verdict": "fix", "claims": ["文字列"]},
            {"id": 2, "title": "b"},
            {"id": 3, "title": "c", "verdict": "confirmed", "claims": []},
        ], "test")
        self.assertEqual(counts, {"confirmed": 1, "fix": 1, "rewrite": 0, "review_required": 1})
        self.assertIn("unparsed", report)


class MainRunTest(unittest.TestCase):
    """main()全体を、正常・str・欠損・例外が混在する状態で最後まで実行する"""

    def run_main(self, by_id, extra_args=(), out_dir=None):
        out_dir = out_dir or tempfile.mkdtemp()
        client = FakeClient(by_id)
        arts = [article(i) for i in sorted(by_id)] if by_id else []
        code = fa.main(["--out-dir", out_dir, *extra_args], client=client,
                       articles_override=arts, sleep_sec=0)
        return code, out_dir, client

    def read_json(self, out_dir):
        f = [n for n in os.listdir(out_dir) if n.endswith(".json")]
        with open(os.path.join(out_dir, sorted(f)[-1]), encoding="utf-8") as fh:
            return json.load(fh)

    def test_mixed_responses_do_not_crash(self):
        by_id = {
            1: GOOD,
            2: dict(GOOD, verdict="ok", claims=[]),
            3: "文字列だけの応答",
            4: dict(GOOD, claims=["文字列の要素"]),
            5: {"summary": "欠損"},
            6: RuntimeError("API error"),
            7: dict(GOOD, verdict="rewrite"),
        }
        code, out_dir, _ = self.run_main(by_id)
        self.assertEqual(code, 0)
        res = {r["id"]: r["verdict"] for r in self.read_json(out_dir)}
        self.assertEqual(res, {1: "fix", 2: "confirmed", 3: "review_required", 4: "review_required",
                               5: "review_required", 6: "review_required", 7: "rewrite"})
        md = [n for n in os.listdir(out_dir) if n.endswith(".md")][0]
        text = open(os.path.join(out_dir, md), encoding="utf-8").read()
        self.assertIn("confirmed: 1件 / fix: 1件 / rewrite: 1件 / review_required: 4件", text)

    def test_checkpoint_written_per_article(self):
        code, out_dir, _ = self.run_main({1: GOOD, 2: "壊れた応答"})
        jl = [n for n in os.listdir(out_dir) if n.endswith(".jsonl")][0]
        lines = open(os.path.join(out_dir, jl), encoding="utf-8").read().splitlines()
        self.assertEqual([json.loads(l)["id"] for l in lines], [1, 2])

    def test_resume_skips_completed_and_retries_review_required(self):
        _, out_dir, _ = self.run_main({1: GOOD, 2: "壊れた応答", 3: dict(GOOD, verdict="ok")})
        # 2回目: 1と3は引き継ぎ、review_requiredだった2だけ再監査される
        _, out2, client2 = self.run_main({1: GOOD, 2: GOOD, 3: GOOD}, extra_args=["--resume", out_dir])
        self.assertEqual(client2.calls, [2])
        res = {r["id"]: r["verdict"] for r in self.read_json(out2)}
        self.assertEqual(res, {1: "fix", 2: "fix", 3: "confirmed"})

    def test_resume_tolerates_broken_checkpoint_lines(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "old.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": 1, "verdict": "fix", "claims": []}) + "\n")
            f.write("{壊れた行\n")
            f.write('"str行"\n')
        _, _, client = self.run_main({1: GOOD, 2: GOOD}, extra_args=["--resume", d])
        self.assertEqual(client.calls, [2])


if __name__ == "__main__":
    unittest.main()
