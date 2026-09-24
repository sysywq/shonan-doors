#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fact Audit の新旧結果を比較する(APIは呼ばない)。

  python compare_audits.py 旧.json 新.json [--out compare.md]

記事単位(confirmed/fix/rewrite/review_required)と記述単位
(confirmed/wording_difference/not_found_in_primary/source_unavailable/contradicted)の
件数変化、判定が変わった記事の一覧を出力する。.jsonl やディレクトリも指定可。
"""
import argparse
import sys
import types

sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))
import fact_audit as fa  # noqa: E402


def load(path):
    return list(fa.load_previous_results([path]).values())


def compare(old, new):
    oc, ocl = fa.count_results(old)
    nc, ncl = fa.count_results(new)
    lines = ["# Fact Audit 新旧比較", "", f"旧 {len(old)} 件 / 新 {len(new)} 件", "",
             "| 記事単位 | 旧 | 新 | 増減 |", "|---|---|---|---|"]
    for k in fa.VERDICTS:
        lines.append(f"| {k} | {oc[k]} | {nc[k]} | {nc[k] - oc[k]:+d} |")
    lines += ["", "| 記述単位 | 旧 | 新 | 増減 |", "|---|---|---|---|"]
    for k in list(ncl):
        lines.append(f"| {k} | {ocl.get(k, 0)} | {ncl.get(k, 0)} | {ncl.get(k, 0) - ocl.get(k, 0):+d} |")
    old_by = {r["id"]: r for r in old if isinstance(r, dict) and "id" in r}
    changed = []
    for r in sorted((r for r in new if isinstance(r, dict) and "id" in r), key=lambda r: r["id"]):
        o = old_by.get(r["id"])
        ov = (o or {}).get("verdict")
        ov = "confirmed" if ov == "ok" else ov
        if ov != r.get("verdict"):
            changed.append(f"| {r['id']} | {r.get('title', '')[:30]} | {ov} | {r.get('verdict')} |")
    lines += ["", f"## 判定が変わった記事({len(changed)}件)", "", "| id | タイトル | 旧 | 新 |", "|---|---|---|---|"] + changed
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    text = compare(load(a.old), load(a.new))
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)


if __name__ == "__main__":
    main()
