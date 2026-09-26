# CLAUDE.md

shonan-doors で作業する際の恒久ルール。

## Git運用

- mainへ直接pushしない
- 毎タスク、最新の origin/main から新しい作業branchを作る
- branch名は内容が分かる名前にする
- 修正後は git diff を確認する
- 問題なければ作業branchへpushする
- PRを作成する
- CI成功を確認する
- mergeはオーナーの確認後にのみ行う

## Shonan Doorsの編集ルール

- 記事の元データは原則 `data/articles.json` を編集する
- 生成済みHTML、一覧ページ、`sitemap.xml` などは直接編集しない
- 生成物は `python3 build.py` で再生成する
- パッチやPRでは可能な限り元データ・ソースコードを変更し、`build.py` で生成されるHTML・`sitemap.xml` 等の生成物は直接編集しない
- 一次情報のみを事実確認の根拠にする
- 他メディアはネタ探しには使えるが、事実確認の根拠にはしない
- 公式サイト/公式SNS/主催者/自治体が掲載した画像内の文字・数値も一次情報として扱う(本文テキストに同じ文言がなくてよい)。ただし、公式の発信元が掲載した画像であること(切り抜き・第三者転載は不可)、文字・数値が明瞭に読めること、対象のイベント/店舗/企画に直接紐づいていることが条件。読み取りに曖昧さがある場合のみ人間確認へ回す
- 営業時間、料金、開催日、会期、休館日、駅名、徒歩分数などの鮮度情報は必ず公式一次情報を確認する
- 公式の紹介ページだけでなく、最新のお知らせ・休業・閉店・変更情報も確認する
- URL / slug / article ID は原則変更しない
- fix判定の記事は局所修正を優先し、必要以上に全文を書き換えない
- review_required は誤り確定ではないので、一次情報を確認してから判断する
- 重複記事は `mergedInto` の既存仕組みを使って整理する

## 公式画像の確認Issue(`[公式画像の確認]`)への対応

- 画像内の文字をAIが十分な確度で読めない場合だけ、このIssueでオーナーに目視確認を求める(推測で確定しない)
- `@claude Approve`(値が違うときは `正しい値: …`)→ 作業branchで `python3 resume_image_check.py --issue <番号> --decision approve [--value …]` → `python3 build.py` → PR
- `@claude Reject` → `python3 resume_image_check.py --issue <番号> --decision reject`(記事は公開しない。確認記録の変更だけPRにする)
- オーナーの確認結果は `data/image_confirmations.json` に残り、以後の監査でも同じ画像の読取り補助として使われる

## 必須チェック

- `python3 -m unittest discover -s tests -v`
- `python3 build.py`
- 旧誤記の残存チェック
- 情報源ポリシー違反がないか確認
- 意図しない記事や生成物の変更がないか確認

## Fact Audit運用

- 新規・初回監査は full
- 修正後の確認は verify
- confirmed記事は原則再監査しない
- verifyは前回のfullまたはverifyのRun IDを使う
- APIコストを抑えるため、修正確認ではfullを使わない
- verify結果が一次情報と矛盾する場合は、verifyを絶対視せず一次情報を優先し、その差異を報告する

## 作業完了時の報告

以下をまとめて報告する。

- 変更内容
- 使用した一次情報
- テスト結果
- ビルド結果
- 変更ファイル
- PR URL
- CI結果
