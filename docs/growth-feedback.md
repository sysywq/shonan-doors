# Growth Feedback（運用手順）

Daily Articles は公開前に Google Sheets の既存タブを読み、GSC の観測クエリを編集上の参考情報として渡します。一次情報確認、同一対象判定、Fact Audit の公開基準は既存どおりです。取得が失敗した場合は通常の生成を続けます。検索クエリやシートの実データは git に保存しません。

Weekly Growth Analysis は月曜 06:00 JST に既存記事の低CTRかつ掲載順位3〜20位のクエリを集計し、`AI_Analysis` に重複しないレビュー案を最大10件追記します。この分析workflow自体は記事を変更しません。別の `Growth Auto Revision` が月曜06:30 JSTに厳格な条件を満たす既存記事のtitle/dekを改稿し、マージ後に `Action_Log` へ記録します。

## 初期設定（リポジトリ管理者）

1. Google Cloud の既存プロジェクト `shonan-doors-data` で Google Sheets API を有効化し、Growth 用サービスアカウントを作る。
2. Workload Identity Pool と GitHub OIDC provider を作る。issuer は `https://token.actions.githubusercontent.com`、attribute mapping は `google.subject=assertion.sub`、`attribute.repository=assertion.repository`。attribute condition で `assertion.repository == 'sysywq/shonan-doors'` に限定する。サービスアカウントにはこの provider からの `roles/iam.workloadIdentityUser` を付与する。JSON鍵は作らない。
3. 対象スプレッドシートをサービスアカウントのメールアドレスに **編集者** として共有する。週次で `AI_Analysis` へ追記するため読み取り専用では不足する。
4. GitHub `Settings > Secrets and variables > Actions > Variables` に `GCP_WIF_PROVIDER`（`projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL/providers/PROVIDER`）、`GCP_SERVICE_ACCOUNT`（メールアドレス）、`GROWTH_SPREADSHEET_ID`（`16mX87VhqO05qkHcyqKAK1bGcj1oabPlTx9VohFe598k`）を登録する。
5. `Weekly Growth Analysis` を手動実行し、`AI_Analysis` の末尾と Actions ログで結果を確認する。14日連続の GSC 日次データがそろうまでトレンド判定は `false`。

## 分析案の範囲

- GSC Query/Page の詳細行はサイト全体の合計とはみなさず、サイト全体の合計は GSC_Daily を基準にする。GA4 の日次境界とは混ぜない。
- 20 impression 未満・8%以上のCTR・順位3〜20位の範囲外は提案しない。少数データのため提案の確信度は Low とする。
- 検索意図の衝突はレビュー案としてのみ扱う。統合やリダイレクトを自動実行しない。
- Weekly Growth Analysis は `AI_Analysis` の既存13列だけを利用する。原表、Article_Master、Dashboard、Action_Log は変更しない。自動改稿後の Action_Log 記録は別workflowが行う。
- アクセス設定前は Daily への影響なし。週次ジョブは設定がそろうまで起動されない。

ローカルで個人データを含まない合成fixtureを使う場合は `python growth_feedback.py daily --snapshot fixture.json`。実データsnapshotや一時signalはリポジトリへ含めない。

## 手動で提案を記事改善へ進めるとき（任意）

手動レビューを希望する場合、`AI_Analysis` の `Proposed` は未承認です。観測クエリ、検索意図、現行記事と一次情報を確認し、取り上げる提案の `Status` を `Approved` に変更します。確信度 `Low` は因果を示さず、特に新しい記事は集計期間や露出数が少ないため判断を保留できます。見送る場合は `Rejected` にします。自動改稿はこのStatusと独立した、後述の厳しい数値・監査条件で判定します。

GitHub Actions の `Growth Proposal Review` を手動実行し、その行の `Analysis_ID` を入力します。`Approved` の記事だけについて、期間・観測・仮説・現行 title/dek・一次情報へのリンクを Markdown の artifact にまとめます。資料は7日後に消えます。Google Sheet の値をログや git に書きません。

手動レビュー経路では、編集者が資料をもとに検索意図と事実関係を検証し、title/dek/本文の具体的な差分を別途 PR にします。PR のレビューと既存の公開ゲートを通過した変更だけを反映し、反映後に `Action_Log` を記録します。`Growth Proposal Review` を再実行しても記事と `Action_Log` は変更されません。

## 自動改稿・実施記録・効果観測（2026-09-27追加）

`Growth Auto Revision` は月曜06:30 JSTに1記事まで自動選定します。毎日07:00 JSTには過去の実施後の観測を確認します。`AI_Analysis` の Proposed/Approved はこの自動選定の許可条件に使わず、独立に厳格な条件を満たす対象だけを扱います。既存の手動レビュー資料は任意の編集用経路として残します。

自動改稿の条件は、GSC日次14日が連続し最終日が5日以内、同じ記事とクエリの直近7日詳細が100表示以上、クリック7件以下・CTR4%未満・順位3〜20位、記事公開後28日以上、タイトルにクエリ未反映、イベント日付なし、重複クエリや統合記事でない、過去の自動変更記録なし、です。該当しない場合はAPIで改稿せず正常終了します。検索語は事実の根拠にせず、元記事の本文と一次情報を基にtitle/dekのみを提案します。Fact Audit(full)がconfirmedでない場合、または監査で他の欄が変わる場合も見送ります。

対象があれば元データを書き換えてビルドし、記事データと生成物だけを作業branchへpushします。PR、明示的なCI実行、PR headと変更ファイルの再検証を経て、成功したものだけマージします。マージ後に `Action_Log` へ変更前後、根拠、URL、実行者を一度だけ追記します。Sheets追記に失敗した場合は次回実行時にマージ履歴と記事差分から未記録の変更を復旧します。毎朝の測定で、変更後14日連続のGSC日次データが揃い、その記事・クエリの詳細が存在すれば結果行を追記します。GSC詳細は標本なので因果効果と断定しません。

2026-09-28 05:00 JSTの新規記事生成は従来の `Daily Shonan Doors Articles` が担当します。自動改稿はその後の06:30以降に条件を満たした記事だけを扱います。14日分が無ければ自動的に見送ります。
