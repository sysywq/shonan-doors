# workflow の更新(オーナーが適用する)

Issue #32(Daily Articles を PR経由・Fact Audit自動実行・合格時のみ自動マージに変更)の workflow 変更です。
Claude(GitHub App)には `workflows` 権限が無く `.github/workflows/` を push できないため、ここに置いています。

このPRのbranchで次を実行し、commit・push してください(内容は `.github/workflows/` への置き換え・追加だけです)。

```sh
cp .github/workflow-updates/*.yml .github/workflows/
git rm -r .github/workflow-updates
git add .github/workflows
git commit -m "Daily Articles の workflow を PR経由・自動マージ方式に更新 (#32)"
git push
```

| ファイル | 変更 |
|---|---|
| `daily-articles.yml` | 当日branch → Fact Audit(full) → tests/build/永続化検証 → PR → CI → 全合格時のみ自動マージ。main への直接pushを廃止。`data/event_series.json` の git add 漏れを修正 |
| `daily-publish.yml`(新規) | マージ後に本番公開を確認できた記事だけ X投稿・IndexNow |
| `ci.yml` | `workflow_dispatch` を追加(GITHUB_TOKEN が作った当日PRでも CI を実行するため) |
| `post-to-x.yml` / `x-backfill.yml` | X投稿済みログの main 直pushを廃止し、専用branch `x-post-log` に保存 |

`tests/test_daily_pipeline.py` の workflow テストは、このディレクトリがあればこちらを、無ければ `.github/workflows/` を検査します。
