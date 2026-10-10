# Issue #241: workflow の差し替え(オーナー操作が必要)

Claude(GitHub App)には `workflows` 権限がないため、`.github/workflows/` を変更できません。
このディレクトリの2ファイルは、#241 の修正に対応する workflow の完成版です。オーナーが次のとおり反映してください。

```sh
git switch -c fix/issue-241-workflows origin/main
cp docs/workflow-patches/issue-241/daily-publication-watchdog.yml .github/workflows/
cp docs/workflow-patches/issue-241/daily-articles.yml .github/workflows/
git rm -r docs/workflow-patches/issue-241
git commit -am "fix: watchdog を daily_watchdog.py に切り替え、cron を混雑しない時刻へ (#241)"
git push -u origin fix/issue-241-workflows   # → PR → CI → マージ
```

変更点:

- `daily-publication-watchdog.yml`: 判定を `daily_watchdog.py`(テスト付き)に置き換える。JST 05:07〜21:37 に30分ごと実行し、`pages: write` を追加
  - 旧版は `/articles/<id>/` で本番を確認していたため常に404となり、公開済みの日でも毎回 dispatch していた
  - 実行中の run の `updated_at` が進まないため、旧版は正常な run を15分で stale と判定していた
- `daily-articles.yml`: cron を `0 20 * * *`(05:00 JST)から `13 19 * * *`(04:13 JST)に変更する。毎時0分は GitHub の schedule が混雑し、10/8〜10/10 は3〜4時間遅延していた
