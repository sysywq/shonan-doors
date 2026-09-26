# 適用待ちの workflow(Issue #32)

Claude(GitHub App)の権限では `.github/workflows/` を変更できないため、Issue #32 の workflow 変更をここに置いています。
**このPRをマージする前に**、オーナーが次の手順で `.github/workflows/` に反映してください
(`verify_daily_run.py` は台帳のステージ漏れを検出するようになったため、旧 `daily-articles.yml` のままマージすると
`data/event_series.json` が変わった日に Daily Articles が失敗します)。

```sh
git fetch origin claude/issue-32-20260926-0549
git switch claude/issue-32-20260926-0549
cp pending-workflows/*.yml .github/workflows/
git rm -r pending-workflows
git add .github/workflows
git commit -m "ci: Issue #32 の workflow 変更を反映"
git push
```

| ファイル | 変更内容 |
|---|---|
| `daily-articles.yml` | main直pushを廃止。当日branch → 生成 → Fact Audit full(記事単位の保留)→ tests → build → 永続化検証 → PR → CI → 条件合格時のみ自動マージ → 公開確認 → X投稿・IndexNow。`data/event_series.json` を git add に追加 |
| `ci.yml` | `workflow_dispatch` を追加(GITHUB_TOKEN で作った当日PRでは pull_request の CI が起動しないため、Daily Articles が起動する) |
| `post-to-x.yml` / `x-backfill.yml` | X投稿ログの main 直pushを廃止し、データbranch `bot/x-post-log` に保存 |
| `publish-after-merge.yml`(新規) | 人がマージしたPR(保留記事の Approve など)で main に新しく入った記事について、公開確認後に X投稿・IndexNow |
| `claude.yml` | `resolve_daily_hold.py` / `resume_image_check.py` の実行を許可リストに追加 |

反映後、このディレクトリは不要です(テスト `tests/test_daily_pipeline.py` は、このディレクトリが無ければ `.github/workflows/` を確認します)。
