# -*- coding: utf-8 -*-
"""fact_verify.py(verify モード)のテスト。API・ネットワークは使わない。
実行: python -m unittest tests/test_fact_verify.py -v
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
import fact_verify as fv  # noqa: E402


def claim(text, status, av="", pv="", url="https://example.com/official", role="detail"):
    return {"claim": text, "role": role, "status": status, "basis": "primary_page",
            "articleValue": av, "primaryValue": pv, "logicallyCompatible": status != "contradicted",
            "primaryUrl": url, "note": ""}


# 修正後の記事。本文の後半には、前回 confirmed だった記述と無関係な長い段落がある。
UNRELATED = "ここは前回の監査で問題がなかった段落で、再監査のためにAPIへ送ってはいけない本文である。"
ARTICLE = {
    "id": 7, "slug": "test-0007", "title": "テストの庭園", "dek": "明治の庭園を紹介する。",
    "body": ("江の島の頂上に広がる庭園は、明治の貿易商が造成した。"
             "園内の温室遺構は、実際に中に入って見学することができる。"
             "現存する唯一の温室遺構として貴重なものだ。\n\n"
             "駐車場は東西あわせて800台を超える規模だ。\n\n"
             + UNRELATED * 30),
}

# 前回の full 監査結果: 5 claim のうち contradicted は 2 件だけ
PREV_FULL = {
    "id": 7, "verdict": "fix", "rulesVersion": 2,
    "claims": [
        claim("明治の貿易商が造成", "confirmed", role="central"),
        claim("温室遺構は普段非公開", "contradicted", av="普段は非公開", pv="実際に中に入って雰囲気を味わえる"),
        claim("現存最古級の温室遺構", "contradicted", av="最古級", pv="現存する唯一の温室遺構"),
        claim("富士山が見える", "not_found_in_primary"),
        claim("椿が咲く", "wording_difference"),
    ],
}


class FakeBlock:
    def __init__(self, inp):
        self.type, self.name, self.input = "tool_use", "submit_verification", inp


class FakeUsage:
    input_tokens, output_tokens = 321, 45


class FakeResp:
    def __init__(self, inp):
        self.content, self.usage = [FakeBlock(inp)], FakeUsage()


class FakeClient:
    def __init__(self, responder):
        self.responder, self.calls = responder, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return FakeResp(self.responder(kw))


def all_resolved(kw):
    n = kw["messages"][0]["content"].count("] 前回の指摘:")
    return {"results": [{"index": i, "result": "resolved", "reason": "一次情報と両立"} for i in range(n)]}


def write_prev(items):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "fact_audit_prev.jsonl"), "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    return d


def run(articles, prev_items, client, fetcher=lambda url: "公式ページ本文"):
    out = tempfile.mkdtemp()
    return fv.run_verify(articles, [write_prev(prev_items)], out, "test", client=client, fetcher=fetcher), out


class OnlyContradictedClaimsTest(unittest.TestCase):
    def test_only_two_contradicted_claims_reach_the_api(self):
        """前回 contradicted が2件の記事では、その2件だけが再判定され、他の claim は API に送られない"""
        # コード判定で先に解決しないよう、一次情報の値が本文に無い claim にする
        prev = dict(PREV_FULL, claims=[
            claim("明治の貿易商が造成", "confirmed", role="central"),
            claim("温室遺構は普段非公開", "contradicted", av="普段は非公開", pv="通常公開"),
            claim("現存最古級の温室遺構", "contradicted", av="最古級", pv="唯一の遺構"),
            claim("富士山が見える", "not_found_in_primary"),
            claim("椿が咲く", "wording_difference"),
        ])
        client = FakeClient(all_resolved)
        fetched = []
        (results, stats, counts), _ = run([ARTICLE], [prev], client,
                                          fetcher=lambda u: fetched.append(u) or "温室は通常公開。唯一の遺構。")
        self.assertEqual(len(client.calls), 1)                      # 記事単位で1回だけ
        prompt = client.calls[0]["messages"][0]["content"]
        self.assertEqual(prompt.count("] 前回の指摘:"), 2)          # 送られた claim は2件
        self.assertIn("温室遺構は普段非公開", prompt)
        self.assertIn("現存最古級の温室遺構", prompt)
        judged = [l.split("前回の指摘:", 1)[1].strip() for l in prompt.splitlines() if "前回の指摘:" in l]
        self.assertEqual(judged, ["温室遺構は普段非公開", "現存最古級の温室遺構"])
        for other in ("明治の貿易商が造成", "富士山が見える", "椿が咲く"):
            self.assertNotIn(other, judged)                          # 他の claim は判定対象として送らない
        for other in ("富士山", "椿"):
            self.assertNotIn(other, prompt)                          # 無関係な claim の情報も含めない
        self.assertNotIn(UNRELATED, prompt)                         # 記事全文も送らない
        self.assertLess(len(prompt), len(ARTICLE["body"]))
        self.assertNotIn("tools", [k for k in client.calls[0] if k == "tools" and
                                   any(t.get("type", "").startswith("web_") for t in client.calls[0]["tools"])])
        self.assertEqual(stats["target_claims"], 2)
        self.assertEqual(stats["api_claims"], 2)
        self.assertEqual(stats["api_input_tokens"], 321)
        self.assertEqual(counts["resolved"], 2)

    def test_no_web_tools_are_given_to_the_model(self):
        prev = dict(PREV_FULL, claims=[claim("温室遺構は普段非公開", "contradicted", av="普段は非公開", pv="通常公開")])
        client = FakeClient(all_resolved)
        run([ARTICLE], [prev], client)
        tools = client.calls[0]["tools"]
        self.assertEqual([t["name"] for t in tools], ["submit_verification"])

    def test_article_without_contradicted_claims_is_skipped(self):
        prev = dict(PREV_FULL, verdict="confirmed", claims=[claim("x", "confirmed"), claim("y", "not_found_in_primary")])
        client = FakeClient(all_resolved)
        (results, stats, _), _ = run([ARTICLE], [prev], client)
        self.assertEqual(client.calls, [])
        self.assertEqual((stats["articles"], stats["skipped_no_target"]), (0, 1))

    def test_article_without_previous_result_is_skipped(self):
        client = FakeClient(all_resolved)
        (results, stats, _), _ = run([ARTICLE], [], client)
        self.assertEqual(client.calls, [])
        self.assertEqual(stats["skipped_no_previous"], 1)


class CodeResolutionTest(unittest.TestCase):
    """API を呼ばずにコードだけで判定できるケース"""

    def check(self, article, c, fetcher=lambda u: "本文"):
        client = FakeClient(all_resolved)
        (results, stats, counts), _ = run([article], [dict(PREV_FULL, claims=[c])], client, fetcher=fetcher)
        self.assertEqual(client.calls, [], "コードで判定できるのに API が呼ばれた")
        return results[0]["claims"][0], stats

    def test_primary_value_now_in_article(self):
        r, stats = self.check(ARTICLE, claim("温室遺構は普段非公開", "contradicted", av="普段は非公開",
                                             pv="実際に中に入って見学することができる"))
        self.assertEqual((r["verifyResult"], r["method"]), ("resolved", "code"))
        self.assertEqual(stats["code_resolved"], 1)

    def test_old_value_still_present_is_still_contradicted(self):
        art = dict(ARTICLE, body=ARTICLE["body"] + "温室遺構は普段は非公開だ。")
        r, _ = self.check(art, claim("温室遺構は普段非公開", "contradicted", av="普段は非公開", pv="通常公開"))
        self.assertEqual(r["verifyResult"], "still_contradicted")

    def test_approximate_number_resolved(self):
        # 一次情報 826台 / 修正後「800台を超える規模」→ 概数として両立
        r, _ = self.check(ARTICLE, claim("駐車場の台数", "contradicted", av="駐車場は500台", pv="826台"))
        self.assertEqual(r["verifyResult"], "resolved")

    def test_age_calculation_resolved(self):
        art = dict(ARTICLE, body="コッキングは明治元年に来日した。当時24歳だった。")
        r, _ = self.check(art, claim("来日時の年齢", "contradicted", av="27歳", pv="24歳"))
        self.assertEqual(r["verifyResult"], "resolved")

    def test_date_resolved(self):
        art = dict(ARTICLE, body="禁漁期間は1月1日から3月10日までだ。")
        r, _ = self.check(art, claim("禁漁期間", "contradicted", av="3月中旬", pv="3月10日"))
        self.assertEqual(r["verifyResult"], "resolved")

    def test_opening_hours_exact_match(self):
        art = dict(ARTICLE, body="営業は10:00〜18:00で、定休日は不定期だ。")
        r, _ = self.check(art, claim("営業時間", "contradicted", av="10:00〜17:00", pv="月〜日 10:00-18:00"))
        self.assertEqual(r["verifyResult"], "resolved")

    def test_removed_claim_is_resolved(self):
        r, _ = self.check(ARTICLE, claim("フラワーフェスタの期間中のみ特別公開", "contradicted",
                                         av="フラワーフェスタ期間のみ", pv="通常公開"))
        self.assertEqual(r["verifyResult"], "resolved")
        self.assertIn("削除", r["verifyReason"])

    def test_unfetchable_primary_is_review_required_without_api(self):
        r, _ = self.check(ARTICLE, claim("温室遺構は普段非公開", "contradicted", av="普段は非公開", pv="通常公開"),
                          fetcher=lambda u: None)
        self.assertEqual((r["verifyResult"], r["method"]), ("review_required", "code"))

    def test_primary_not_fetched_when_code_resolves(self):
        fetched = []
        self.check(ARTICLE, claim("温室遺構は普段非公開", "contradicted", av="普段は非公開",
                                  pv="実際に中に入って見学することができる"),
                   fetcher=lambda u: fetched.append(u) or "本文")
        self.assertEqual(fetched, [])   # コードで解決できれば一次情報の取得もしない


class MixedAndRobustnessTest(unittest.TestCase):
    def test_code_and_api_are_mixed_in_one_article(self):
        prev = dict(PREV_FULL, claims=[
            claim("駐車場の台数", "contradicted", av="駐車場は500台", pv="826台"),              # code
            claim("温室遺構は普段非公開", "contradicted", av="普段は非公開", pv="通常公開"),   # api
        ])
        client = FakeClient(all_resolved)
        (results, stats, _), _ = run([ARTICLE], [prev], client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual((stats["code_resolved"], stats["api_claims"]), (1, 1))
        prompt = client.calls[0]["messages"][0]["content"]
        self.assertNotIn("駐車場の台数", prompt)

    def test_articles_are_batched_per_article(self):
        a2 = dict(ARTICLE, id=8, slug="test-0008")
        c = claim("温室遺構は普段非公開", "contradicted", av="普段は非公開", pv="通常公開")
        prev = [dict(PREV_FULL, claims=[c, dict(c, claim="現存最古級", av="最古級", articleValue="最古級")]),
                dict(PREV_FULL, id=8, claims=[c])]
        client = FakeClient(all_resolved)
        run([ARTICLE, a2], prev, client)
        self.assertEqual(len(client.calls), 2)   # 記事ごとに1回。claim ごとではない

    def test_malformed_api_response_becomes_review_required(self):
        prev = dict(PREV_FULL, claims=[claim("温室遺構は普段非公開", "contradicted", av="普段は非公開", pv="通常公開")])
        for bad in ("文字列だけ", {"results": "壊れた"}, {"results": [{"index": 9, "result": "resolved"}]},
                    {"results": [{"index": 0, "result": "maybe"}]}, None):
            client = FakeClient(lambda kw, b=bad: b)
            (results, _, counts), _ = run([ARTICLE], [prev], client)
            self.assertEqual(results[0]["claims"][0]["verifyResult"], "review_required", bad)

    def test_api_exception_does_not_crash(self):
        class Boom(FakeClient):
            def create(self, **kw):
                raise RuntimeError("API down")
        prev = dict(PREV_FULL, claims=[claim("温室遺構は普段非公開", "contradicted", av="普段は非公開", pv="通常公開")])
        (results, _, _), _ = run([ARTICLE], [prev], Boom(all_resolved))
        self.assertEqual(results[0]["verdict"], "review_required")

    def test_verify_after_verify_targets_only_unresolved(self):
        prev_full = PREV_FULL
        prev_verify = {"mode": "verify", "id": 7, "verdict": "still_contradicted", "claims": [
            dict(claim("温室遺構は普段非公開", "contradicted", av="普段は非公開", pv="通常公開"),
                 verifyResult="still_contradicted"),
            dict(claim("現存最古級", "contradicted", av="最古級", pv="唯一"), verifyResult="resolved"),
        ]}
        client = FakeClient(all_resolved)
        (results, stats, _), _ = run([ARTICLE], [prev_full, prev_verify], client)
        self.assertEqual(stats["target_claims"], 1)
        prompt = client.calls[0]["messages"][0]["content"]
        self.assertIn("温室遺構は普段非公開", prompt)
        self.assertNotIn("現存最古級", prompt)

    def test_summary_contains_required_counts(self):
        prev = dict(PREV_FULL, claims=[
            claim("駐車場の台数", "contradicted", av="駐車場は500台", pv="826台"),
            claim("温室遺構は普段非公開", "contradicted", av="普段は非公開", pv="通常公開"),
        ])
        (_, _, _), out = run([ARTICLE], [prev], FakeClient(all_resolved))
        md = open([os.path.join(out, f) for f in os.listdir(out) if f.endswith(".md")][0], encoding="utf-8").read()
        for label in ("対象記事数", "対象contradicted claim数", "APIで再判定したclaim数",
                      "コードだけで解決したclaim数", "| resolved |", "| still_contradicted |",
                      "| review_required |", "APIへの入力"):
            self.assertIn(label, md)


class FullModeUnaffectedTest(unittest.TestCase):
    def test_full_resume_ignores_verify_results(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "a.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": 1, "verdict": "fix", "rulesVersion": 2, "claims": []}) + "\n")
            f.write(json.dumps({"id": 1, "mode": "verify", "verdict": "resolved", "claims": []}) + "\n")
        prev = fa.load_previous_results([d])
        self.assertEqual(prev[1]["verdict"], "fix")

    def test_main_verify_requires_previous(self):
        self.assertEqual(fa.main(["--mode", "verify", "--out-dir", tempfile.mkdtemp()],
                                 client=FakeClient(all_resolved), articles_override=[ARTICLE], sleep_sec=0), 2)

    def test_main_dispatches_verify(self):
        prev_dir = write_prev([dict(PREV_FULL, claims=[claim("温室遺構は普段非公開", "contradicted",
                                                              av="普段は非公開", pv="通常公開")])])
        client = FakeClient(all_resolved)
        code = fa.main(["--mode", "verify", "--previous", prev_dir, "--out-dir", tempfile.mkdtemp()],
                       client=client, articles_override=[ARTICLE], sleep_sec=0, fetcher=lambda u: "本文")
        self.assertEqual(code, 0)
        self.assertEqual(len(client.calls), 1)
        self.assertNotIn("web_search", json.dumps(client.calls[0]["tools"]))


class ResultConsistencyTest(unittest.TestCase):
    """API の result と理由・両立フラグの不整合(id:14 で発生)を補正する"""

    def api_result(self, item):
        prev = dict(PREV_FULL, claims=[claim("レンバイは朝8時から営業している", "contradicted",
                                             av="早朝から昼過ぎにかけて営業",
                                             pv="朝8時頃までには販売準備が整う")])
        art = dict(ARTICLE, body="レンバイは朝8時から営業している。野菜が売り切れ次第終わる。")
        client = FakeClient(lambda kw: {"results": [dict(item, index=0)]})
        (results, _, _), _ = run([art], [prev], client)
        self.assertEqual(len(client.calls), 1)
        return results[0]["claims"][0]

    def test_still_contradicted_but_compatible_flag_true_becomes_resolved(self):
        c = self.api_result({"result": "still_contradicted", "logicallyCompatible": True,
                             "reason": "「8時から」と「8時頃までに準備が整う」はほぼ両立する"})
        self.assertEqual(c["verifyResult"], "resolved")

    def test_still_contradicted_with_compatible_reason_and_no_flag_is_review_required(self):
        # 今回の id:14 の再現: 理由は「ほぼ両立する」なのに still_contradicted
        c = self.api_result({"result": "still_contradicted",
                             "reason": "表現はやや異なるが、ほぼ両立する"})
        self.assertEqual(c["verifyResult"], "review_required")
        self.assertIn("矛盾", c["verifyReason"])

    def test_still_contradicted_with_incompatible_reason_is_kept(self):
        c = self.api_result({"result": "still_contradicted", "logicallyCompatible": False,
                             "reason": "開店時刻が一次情報と両立しない"})
        self.assertEqual(c["verifyResult"], "still_contradicted")

    def test_negated_compatibility_phrase_is_not_treated_as_compatible(self):
        c = self.api_result({"result": "still_contradicted", "reason": "ほぼ一致に見えるが、時刻が両立しない"})
        self.assertEqual(c["verifyResult"], "still_contradicted")

    def test_resolved_but_flag_false_is_review_required(self):
        c = self.api_result({"result": "resolved", "logicallyCompatible": False, "reason": "修正済み"})
        self.assertEqual(c["verifyResult"], "review_required")

    def test_string_flag_is_accepted(self):
        c = self.api_result({"result": "still_contradicted", "logicallyCompatible": "true", "reason": "両立する"})
        self.assertEqual(c["verifyResult"], "resolved")

    def test_schema_requires_compatibility_flag(self):
        item = fv.VERIFY_TOOL["input_schema"]["properties"]["results"]["items"]
        self.assertIn("logicallyCompatible", item["required"])


if __name__ == "__main__":
    unittest.main()
