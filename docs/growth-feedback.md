# Growth Feedback（初期導入）

Daily Articles は公開前に Google Sheets の既存タブを読み、GSC の観測クエリを編集上の参考情報として渡します。一次情報確認、同一対象判定、Fact Audit の公開基準は既存どおりです。取得が失敗した場合は通常の生成を続けます。検索クエリやシートの実データは git に保存しません。

Weekly Growth Analysis は月曜 06:00 JST に既存記事の低CTRかつ掲載順位3〜20位のクエリを集計し、`AI_Analysis` に重複しないレビュー案を最大10件追記します。既存記事の本文、メタ情報、canonical を自動変更しません。`Action_Log` は実施済みの変更を記録する表であり、提案のみの段階では書き込みません。

## 初期設定（リポジトリ管理者）

1. Google Cloud の既存プロジェクト `shonan-doors-data` で Google Sheets API を有効化し、Growth 用サービスアカウントを作る。
2. Workload Identity Pool と GitHub OIDC provider を作る。issuer は `https://token.actions.githubusercontent.com`、attribute mapping は `google.subject=assertion.sub`、`attribute.repository=assertion.repository`。attribute condition で `assertion.repository == 'sysywq/shonan-doors'` に限定する。サービスアカウントにはこの provider からの `roles/iam.workloadIdentityUser` を付与する。JSON鍵は作らない。
3. 対象スプレッドシートをサービスアカウントのメールアドレスに **編集者** として共有する。週次で `AI_Analysis` へ追記するため読み取り専用では不足する。
4. GitHub `Settings > Secrets and variables > Actions > Variables` に `GCP_WIF_PROVIDER`（`projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL/providers/PROVIDER`）、`GCP_SERVICE_ACCOUNT`（メールアドレス）、`GROWTH_SPREADSHEET_ID`（`16mX87VhqO05qkHcyqKAK1bGcj1oabPlTx9VohFe598k`）を登録する。
5. `Weekly Growth Analysis` を手動実行し、`AI_Analysis` の末尾と Actions ログで結果を確認する。14日連続の GSC 日次データがそろうまでトレンド判定は `false`。

## 現段階の範囲

- GSC Query/Page の詳細行はサイト全体の合計とはみなさず、サイト全体の合計は GSC_Daily を基準にする。GA4 の日次境界とは混ぜない。
- 20 impression 未満・8%以上のCTR・順位3〜20位の範囲外は提案しない。少数データのため提案の確信度は Low とする。
- 検索意図の衝突はレビュー案としてのみ扱う。統合やリダイレクトを自動実行しない。
- `AI_Analysis` の既存13列だけを利用する。原表、Article_Master、Dashboard、Action_Log は変更しない。
- アクセス設定前は Daily への影響なし。週次ジョブは設定がそろうまで起動されない。

ローカルで個人データを含まない合成fixtureを使う場合は `python growth_feedback.py daily --snapshot fixture.json`。実データsnapshotや一時signalはリポジトリへ含めない。

## 提案を記事改善へ進めるとき

`AI_Analysis` の `Proposed` は未承認です。観測クエリ、検索意図、現行記事と一次情報を確認し、取り上げる提案の `Status` を `Approved` に変更します。確信度 `Low` は因果を示さず、特に新しい記事は集計期間や露出数が少ないため判断を保留できます。見送る場合は `Rejected` にします。

GitHub Actions の `Growth Proposal Review` を手動実行し、その行の `Analysis_ID` を入力します。`Approved` の記事だけについて、期間・観測・仮説・現行 title/dek・一次情報へのリンクを Markdown の artifact にまとめます。資料は7日後に消えます。Google Sheet の値をログや git に書きません。

編集者は資料をもとに検索意図と事実関係を検証し、title/dek/本文の具体的な差分を別途 PR にします。PR のレビューと既存の公開ゲートを通過した変更だけを反映し、反映後に `Action_Log` を記録します。`Approved` は記事変更の自動許可ではありません。ワークフローを再実行しても記事と `Action_Log` は変更されません。
