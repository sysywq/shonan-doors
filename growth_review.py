#!/usr/bin/env python3
"""Build an editorial review packet from an approved growth proposal.

The packet is an Actions artifact. This command never edits articles or Sheets.
"""
import argparse
import json
import os
from pathlib import Path

from growth_feedback import read_sheet, rows


def review_packet(tables, articles, analysis_id):
    matches = [r for r in rows(tables["AI_Analysis"])
               if str(r.get("Analysis_ID", "")) == analysis_id]
    if len(matches) != 1:
        raise ValueError("Analysis_ID must identify exactly one proposal")
    proposal = matches[0]
    if str(proposal.get("Status", "")).strip() != "Approved":
        raise ValueError("proposal must have Status=Approved before drafting")
    if proposal.get("Entity_Type") != "Article":
        raise ValueError("only Article proposals are supported")
    matches = [a for a in articles if str(a.get("id")) == str(proposal.get("Entity_ID"))]
    if len(matches) != 1 or matches[0].get("mergedInto"):
        raise ValueError("article is missing or merged")
    article = matches[0]
    if not article.get("slug"):
        raise ValueError("article has no slug")
    return proposal, article


def render(proposal, article):
    def cell(name):
        return str(proposal.get(name, "")).replace("\n", " ").strip()

    sources = article.get("sources") or [article.get("link", "")]
    source_lines = "\n".join(f"- {url}" for url in sources if url)
    return f"""# 記事改善レビュー: {cell('Analysis_ID')}

## 観測と判断

- 対象期間: {cell('Period_Start')} ～ {cell('Period_End')}
- GSC観測: {cell('Observation')}
- 診断: {cell('Diagnosis')}
- 仮説: {cell('Hypothesis')}
- 推奨: {cell('Recommended_Action')}
- 確信度: {cell('Confidence')}（検索クエリの詳細は集計上の一部であり、CTR改善の因果は未確認）

## 現在の記事

- URL: https://www.shonandoors.com/articles/{article['slug']}/
- ID: {article['id']}
- title: {article.get('title', '')}
- dek: {article.get('dek', '')}
- 更新日: {article.get('updated', article.get('date', ''))}

## 一次情報

{source_lines}

## 修正案の作成と確認

1. 観測クエリが記事内容と一致するか確認する。意図が違えば見送る。
2. 一次情報の現況を確認し、期限・価格・営業時間などを再検証する。
3. 新しい title / dek / 本文の差分を作る。検索語を機械的に挿入しない。
4. 差分と公開ページを確認し、レビューを通した後に公開する。
5. 実施後に Action_Log へ記録し、以後の GSC データで効果を見る。

このファイルは編集用資料です。記事データとサイトには変更を加えていません。
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--snapshot", help="synthetic offline fixture only")
    args = parser.parse_args()
    spreadsheet_id = os.environ.get("GROWTH_SPREADSHEET_ID", "")
    if not args.snapshot and not spreadsheet_id:
        parser.error("GROWTH_SPREADSHEET_ID is required")
    tables = json.loads(Path(args.snapshot).read_text(encoding="utf-8")) if args.snapshot else read_sheet(spreadsheet_id)
    articles = json.loads((Path(__file__).parent / "data/articles.json").read_text(encoding="utf-8"))
    proposal, article = review_packet(tables, articles, args.analysis_id)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(proposal, article), encoding="utf-8")
    print(f"Review packet written for article {article['id']}")


if __name__ == "__main__":
    main()
