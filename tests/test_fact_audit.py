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

def claim(text, status, role="detail", av="", pv="", compat=None, basis="primary_page"):
    c = {"claim": text, "role": role, "status": status, "basis": basis,
         "articleValue": av, "primaryValue": pv, "primaryUrl": "https://example.com/", "note": ""}
    if compat is not None:
        c["logicallyCompatible"] = compat
    return c


# 正常応答(明確な誤りが細部に1件 → fix)
GOOD = {
    "primarySources": ["https://example.com/"],
    "claims": [
        claim("9月4日オープン", "confirmed", role="central"),
        claim("明治期から続く", "contradicted", av="明治期から続く", pv="昭和4年創業", compat=False),
    ],
    "hasQuotedComment": False,
    "needsHuman": False,
    "verdict": "fix",
    "summary": "創業時期が一次情報と食い違う",
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
        # モデルの "ok" は参考値として "confirmed" に読み替える(最終判定はルールで決まる)
        r, an = fa.normalize_result(dict(GOOD, verdict="ok"), 1)
        self.assertEqual((r["modelVerdict"], r["verdict"], an), ("confirmed", "fix", []))

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

    def test_missing_optional_fields_in_claim(self):
        # role/basis/値/両立フラグが欠けていても落ちず、roleはdetail扱いになる
        raw = dict(GOOD, claims=[{"claim": "x", "status": "not_found_in_primary"}])
        r, an = fa.normalize_result(raw, 6)
        self.assertEqual(an, [])
        self.assertEqual(r["claims"][0]["role"], "detail")
        self.assertEqual(r["verdict"], "confirmed")

    def test_claim_missing_status_and_bad_types(self):
        raw = dict(GOOD, claims=[{"claim": "x"}], primarySources="https://a", hasQuotedComment="true")
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
            2: dict(GOOD, verdict="ok", claims=[claim("住所", "confirmed")]),
            3: "文字列だけの応答",
            4: dict(GOOD, claims=["文字列の要素"]),
            5: {"summary": "欠損"},
            6: RuntimeError("API error"),
            7: dict(GOOD, claims=[
                claim("タイトルの主張", "contradicted", role="title", av="徒歩数分", pv="タクシー約10分", compat=False),
                claim("中心の主張", "contradicted", role="central", av="普段非公開", pv="通常公開", compat=False)]),
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
        _, out_dir, _ = self.run_main({1: GOOD, 2: "壊れた応答", 3: dict(GOOD, claims=[claim("住所", "confirmed")])})
        # 2回目: 1と3は引き継ぎ、review_requiredだった2だけ再監査される
        _, out2, client2 = self.run_main({1: GOOD, 2: GOOD, 3: GOOD}, extra_args=["--resume", out_dir])
        self.assertEqual(client2.calls, [2])
        res = {r["id"]: r["verdict"] for r in self.read_json(out2)}
        self.assertEqual(res, {1: "fix", 2: "fix", 3: "confirmed"})

    def test_resume_tolerates_broken_checkpoint_lines(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "old.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": 1, "verdict": "fix", "claims": [], "rulesVersion": fa.RULES_VERSION}) + "\n")
            f.write("{壊れた行\n")
            f.write('"str行"\n')
        _, _, client = self.run_main({1: GOOD, 2: GOOD}, extra_args=["--resume", d])
        self.assertEqual(client.calls, [2])

    def test_resume_does_not_reuse_old_rules_version(self):
        # 旧判定基準(rulesVersionなし=v1)の結果は引き継がず、再判定する
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "old.json"), "w", encoding="utf-8") as f:
            json.dump([{"id": 1, "verdict": "rewrite", "claims": []}], f)
        _, _, client = self.run_main({1: GOOD}, extra_args=["--resume", d])
        self.assertEqual(client.calls, [1])


class RulesV2Test(unittest.TestCase):
    """判定基準v2: 今回の1〜40で確認した代表例"""

    def verdict_of(self, claims, **kw):
        r, an = fa.normalize_result(dict(GOOD, claims=claims, **kw), 99)
        self.assertEqual(an, [])
        return r

    # --- 過剰判定だった例 → 誤りにしない ---
    def test_parking_approximation_is_not_contradicted(self):
        # 辻堂海浜公園「駐車場800台規模」/ 公式826台(モデルがcontradictedと答えても概数として許容)
        r = self.verdict_of([claim("駐車場800台規模", "contradicted", av="駐車場800台規模", pv="826台")])
        self.assertEqual(r["claims"][0]["status"], "wording_difference")
        self.assertEqual(r["verdict"], "confirmed")

    def test_approximation_outside_tolerance_stays_contradicted(self):
        r = self.verdict_of([claim("駐車場500台規模", "contradicted", av="駐車場500台規模", pv="826台", compat=False)])
        self.assertEqual(r["claims"][0]["status"], "contradicted")
        self.assertEqual(r["verdict"], "fix")

    def test_exact_number_without_approx_word_stays_contradicted(self):
        r = self.verdict_of([claim("駐車場800台", "contradicted", av="駐車場800台", pv="826台", compat=False)])
        self.assertEqual(r["claims"][0]["status"], "contradicted")

    def test_established_fact_is_confirmed(self):
        # 源実朝「三代将軍」: ページに無くても確立した事実として confirmed
        r = self.verdict_of([claim("源実朝は三代将軍", "confirmed", basis="established_fact")])
        self.assertEqual(r["verdict"], "confirmed")

    def test_detail_not_found_does_not_trigger_fix(self):
        # モデルが not_found にしてしまった場合も、細部なら記事は confirmed のまま
        r = self.verdict_of([claim("源実朝は三代将軍", "not_found_in_primary"),
                             claim("住所", "confirmed", role="central")])
        self.assertEqual(r["verdict"], "confirmed")

    def test_compatible_wording_is_not_contradicted(self):
        # 温室遺構 記事「最古級」/ 公式「現存する唯一」→ 両立しうる
        r = self.verdict_of([claim("最古級の温室遺構", "contradicted", role="central",
                                   av="最古級", pv="現存する唯一", compat=True)])
        self.assertEqual(r["claims"][0]["status"], "wording_difference")
        self.assertEqual(r["verdict"], "confirmed")

    def test_source_unavailable_detail(self):
        # 関東の駅百選: 公式がJS依存で確認不能 → source_unavailable。細部なら confirmed
        r = self.verdict_of([claim("関東の駅百選に選定", "source_unavailable")])
        self.assertEqual(r["claims"][0]["status"], "source_unavailable")
        self.assertEqual(r["verdict"], "confirmed")

    def test_source_unavailable_core_is_review_required(self):
        r = self.verdict_of([claim("関東の駅百選に選定", "source_unavailable", role="title")])
        self.assertEqual(r["verdict"], "review_required")

    def test_many_not_found_never_rewrite(self):
        claims = [claim(f"細部{i}", "not_found_in_primary") for i in range(15)]
        claims.append(claim("中心テーマ", "confirmed", role="central"))
        self.assertEqual(self.verdict_of(claims)["verdict"], "confirmed")

    def test_core_not_found_is_review_required_not_rewrite(self):
        claims = [claim("タイトルの主張", "not_found_in_primary", role="title"),
                  claim("リードの主張", "not_found_in_primary", role="dek")]
        self.assertEqual(self.verdict_of(claims)["verdict"], "review_required")

    # --- 適切だった例 → 誤りとして残す ---
    def test_tsite_distance_contradicted(self):
        r = self.verdict_of([claim("辻堂駅北口から徒歩数分", "contradicted", av="徒歩数分",
                                   pv="辻堂駅からタクシー約10分", compat=False)])
        self.assertEqual((r["claims"][0]["status"], r["verdict"]), ("contradicted", "fix"))

    def test_tanakaya_founding_contradicted(self):
        r = self.verdict_of([claim("明治期から続く", "contradicted", av="明治期から続く", pv="昭和4年創業", compat=False)])
        self.assertEqual(r["verdict"], "fix")

    def test_greenhouse_closed_contradicted(self):
        r = self.verdict_of([claim("普段非公開", "contradicted", av="普段は非公開", pv="通常公開", compat=False)])
        self.assertEqual(r["verdict"], "fix")

    def test_age_calculation_contradicted(self):
        # 1845年生まれ・1869年来日で27歳 → 計算上24歳(数値差が大きく、概数表現もない)
        r = self.verdict_of([claim("来日時27歳", "contradicted", av="27歳", pv="1845年生まれ・1869年来日(24歳)",
                                   compat=False, basis="calculation")])
        self.assertEqual((r["claims"][0]["status"], r["verdict"]), ("contradicted", "fix"))

    def test_multiple_core_contradictions_rewrite(self):
        r = self.verdict_of([claim("t", "contradicted", role="title", compat=False),
                             claim("d", "contradicted", role="dek", compat=False)])
        self.assertEqual(r["verdict"], "rewrite")

    def test_single_core_contradiction_is_fix(self):
        r = self.verdict_of([claim("t", "contradicted", role="title", compat=False)])
        self.assertEqual(r["verdict"], "fix")

    def test_needs_human_is_review_required(self):
        r = self.verdict_of([claim("住所", "confirmed")], needsHuman=True)
        self.assertEqual(r["verdict"], "review_required")

    def test_model_verdict_is_reference_only(self):
        # モデルが rewrite と答えても、明確な誤りが無ければ confirmed
        r = self.verdict_of([claim(f"細部{i}", "not_found_in_primary") for i in range(5)], verdict="rewrite")
        self.assertEqual((r["verdict"], r["modelVerdict"]), ("confirmed", "rewrite"))


class CompareTest(unittest.TestCase):
    def test_compare_old_and_new(self):
        import compare_audits as ca
        old = [{"id": 1, "verdict": "fix", "claims": [{"status": "contradicted"}, {"status": "not_found_in_primary"}]},
               {"id": 2, "verdict": "rewrite", "claims": ["壊れた要素"]}]
        new = [{"id": 1, "verdict": "confirmed", "claims": [{"status": "wording_difference"}]},
               {"id": 2, "verdict": "fix", "claims": [{"status": "contradicted"}, {"status": "source_unavailable"}]}]
        text = ca.compare(old, new)
        self.assertIn("| fix | 1 | 1 | +0 |", text)
        self.assertIn("| rewrite | 1 | 0 | -1 |", text)
        self.assertIn("| source_unavailable | 0 | 1 | +1 |", text)
        self.assertIn("判定が変わった記事(2件)", text)


if __name__ == "__main__":
    unittest.main()
