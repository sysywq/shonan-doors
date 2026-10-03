# Manual Editorial Intake

手動取材・自前写真の記事を、Daily Articlesとは別枠で安全に追加/更新する入口です。

1. `editorial/manual/<slug>/` を作る。
2. 同フォルダへ `article.json` と写真を置く。
3. article.json の `images` に写真ファイル名を掲載順で指定する。先頭がhero。
4. GitHub Actionsの **Manual Editorial Preview** を実行し、packageにarticle.jsonのパスを入力する。
5. Workflowがbuild/test後にDraft PRを作る。内容を確認してからmergeする。

既存slugなら更新、新規slugなら新規記事。Daily Articlesの最低3本にはカウントしない。

article.json 最小例:
{
  "slug":"fujisawa-example",
  "articleType":"report",
  "cat":"e",
  "area":"藤沢",
  "title":"タイトル",
  "dek":"概要",
  "date":"2026-10-03",
  "body":"本文",
  "images":["venue.jpg","poster.jpg"],
  "sources":["https://example.com/"]
}
