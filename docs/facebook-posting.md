# Facebookページ自動投稿

公開済み記事を Facebook ページへ自動投稿する仕組み(`post_to_facebook.py`)の運用メモ。
X投稿・IndexNow と同じく、**本番URLの HTTP 200 を確認した記事だけ**を、記事公開とは別ジョブで配信する。

## 仕組み

| 項目 | 内容 |
| --- | --- |
| スクリプト | `post_to_facebook.py --article-ids 93,94,95` |
| 投稿本文 | 記事タイトル + 導入(`dek` を最大120字)+ 記事URL。`link` にも記事URLを渡してリンクプレビューを出す |
| 対象 | 呼び出し元(公開確認ジョブ)が渡した記事IDのみ。`mergedInto` の記事、slug が不正な記事は投稿しない |
| 投稿済みログ | `data/facebook_post_log.json`(`article_id / facebook_post_id / posted_at / article_url / status`)。データbranch `bot/facebook-post-log` にだけ保存し、main には入れない(`facebook_post_log_store.py`) |
| 失敗時 | 別ジョブなので、Facebook の失敗は X / IndexNow / 記事公開に影響しない |

### 二重投稿の防止

1. 投稿直前に `facebook_post_log_store.py pull` でデータbranchのログを取り込む。データbranchがあるのに取得・解析できない場合は **投稿ステップを走らせない**(fail-closed)
2. ログに記録がある記事は投稿しない(1件投稿するごとにログを保存)
3. 投稿前に Graph API でページの最近の投稿(100件)を読み、本文に同じ記事URLがあれば投稿しない(ログ保存前に止まった実行の再実行対策)。読み取れない場合はその回の投稿をすべて見送る
4. タイムアウト・通信断・5xx は「Facebook側で投稿された可能性がある」ため再投稿しない。ログに `status: "uncertain"` で記録し、次回以降もスキップする(次回、ページ上に投稿が見つかれば `facebook_post_id` を補完して `posted` にする)
5. 4xx(権限不足・トークン失効など投稿されていないことが確定するエラー)はログに残さず、次回の再実行で投稿し直せる
6. workflow の `concurrency: facebook-post` で同時実行を防ぐ

`uncertain` の記事は、ページを目視で確認し、**投稿されていなかった場合だけ** `bot/facebook-post-log` の `data/facebook_post_log.json` からその記録を消してから再実行する。

### 終了コード

| コード | 意味 |
| --- | --- |
| 0 | 成功 / 対象なし / Secrets が2つとも未設定でスキップ(警告を表示) |
| 1 | 投稿失敗・結果不明・公開未確認・ログ読み取り不可のいずれかあり |
| 2 | Secrets の片方だけ未設定・形式不正、引数の誤り |

## GitHub 側の設定

### Secrets(Settings → Secrets and variables → Actions → Secrets)

| 名前 | 内容 |
| --- | --- |
| `FACEBOOK_PAGE_ID` | 投稿先 Facebook ページの ID(数字) |
| `FACEBOOK_PAGE_ACCESS_TOKEN` | そのページの **Page access token** |

Instagram 用の `INSTAGRAM_FACEBOOK_ACCESS_TOKEN` / `INSTAGRAM_BUSINESS_USER_ID` とは別物として扱う(流用しない)。
Instagram 用のトークンは User token / Business Discovery 向けの権限で、Page への投稿権限を持つとは限らない。

### Variables(同 → Variables)

| 名前 | 既定 | 内容 |
| --- | --- | --- |
| `FACEBOOK_POST_DRY_RUN` | `true`(未設定時) | `false` にすると本番投稿する。smoke test が通るまでは未設定のままにする |
| `FACEBOOK_GRAPH_API_VERSION` | 未設定 | 例: `v24.0`。未設定なら URL にバージョンを付けず、Meta App Dashboard でアプリに設定されている既定バージョンが使われる。Meta のバージョン廃止予定に合わせて更新する |

## Meta 側で人が行う設定

1. **Meta for Developers でアプリを用意**(Business タイプ)し、Business Manager(Meta ビジネスポートフォリオ)に紐づける
2. **権限**: `pages_manage_posts`(投稿)、`pages_read_engagement`(既存投稿の読み取り = 重複確認・smoke test)、`pages_show_list`
   - トークンを発行するユーザー(またはシステムユーザー)が、対象ページで「コンテンツの作成」タスク(CREATE_CONTENT)を持っていること
   - 自社で管理するページへの投稿のみなら、アプリの役割を持つユーザーで Standard Access で動く。第三者のページを扱う場合は App Review(Advanced Access)が必要
3. **アプリを Live モードにする**。Development モードのアプリで作った投稿は、アプリの役割を持つ人にしか表示されない
4. **失効しにくい Page access token を発行する**(推奨順)
   - Business Manager の **システムユーザー** にページとアプリを割り当て、上記権限でトークンを生成(有効期限なしを選択)し、`GET /me/accounts` または `GET /{page-id}?fields=access_token` でページのトークンを取得
   - または、長期 User token(60日)から `GET /me/accounts` で取得した Page token(期限なし。ただしユーザーのパスワード変更・権限取消などで失効する)
5. [アクセストークンデバッガー](https://developers.facebook.com/tools/debug/accesstoken/) で、Type が **Page**、対象ページの ID、上記スコープ、有効期限を確認する
6. GitHub の Secrets に `FACEBOOK_PAGE_ID` / `FACEBOOK_PAGE_ACCESS_TOKEN` を登録する

## 本番投稿までの手順

1. `Facebook Page Smoke Test` workflow を実行(既定は疎通確認のみ。投稿しない)
2. 同 workflow で `article_id` に公開済み記事のIDを入れ、`dry_run: true` で本文を確認
3. `dry_run: false` で1件だけ実投稿し、ページ上の表示(リンクプレビュー・公開範囲)を確認
4. リポジトリ Variables に `FACEBOOK_POST_DRY_RUN=false` を設定(以後、公開後配信で自動投稿される)

ローカル確認: `DRY_RUN=true python3 post_to_facebook.py --article-ids 77`

## workflow の変更(オーナーが適用する)

Claude の GitHub App は `.github/workflows/` を変更できないため、適用待ちの workflow を `pending-workflows/` に置いている
(`tests/test_daily_pipeline.py` の WorkflowTest は `pending-workflows/` を優先して YAML 構文・main への直接 push が無いことを確認する)。
オーナーが以下を `.github/workflows/` へ反映し、`pending-workflows/` を削除する。

| ファイル | 変更 |
| --- | --- |
| `pending-workflows/publish-after-merge.yml` | `post_to_facebook` job を追加(`needs: confirm`。公開確認済みの記事IDだけを使う) |
| `pending-workflows/daily-articles.yml` | `post_to_facebook` job を追加(`needs: generate`。自動マージ後の `daily_pr.py published` で HTTP 200 を確認できた記事IDだけを使う) |
| `pending-workflows/facebook-page-smoke.yml` | 新規。疎通確認(投稿しない)と、1記事の DRY_RUN / 実投稿テスト |

いずれも `concurrency: facebook-post`(`cancel-in-progress: false`)を共有し、X / IndexNow とは別 job なので、Facebook の失敗が他の配信・記事公開を失敗扱いにすることはない。
