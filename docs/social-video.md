# Social Video(記事 → short動画 → SNS配信)

公開済みの記事1本から縦型(9:16)のshort動画を1本作り、Instagram Reels / Facebook Reels / YouTube Shorts / TikTok に配信する共通基盤です。
動画のクリエイティブ(字幕デザイン・BGM・音声・尺・動画生成AI・テンプレート・投稿本数)はまだ決めていません。
`social_video/config.json` と renderer の差し替えで後から決められる作りにしています。

```
published article_id
 → articles.py            公開済み記事の読み込み(mergedInto・未来日付・終了イベント・本番URL≠200 は対象外)
 → manifest.py            short動画manifest(JSON)
 → renderers.py           renderer/provider(差し替え可)→ 9:16 MP4(H.264 + AAC)
 → storage.py             公開メディアストレージ(Cloudflare R2)→ 公開URL
 → publishers/            instagram / facebook / youtube / tiktok
 → distribution_log.py    data/social_video_log.json(データbranch bot/social-video-log に保存)
```

記事の公開フロー(Daily Articles / Deploy / Publish Articles After Merge)とは独立しています。このパイプラインが失敗しても記事の公開には影響しません。

## 使い方

```bash
# 確認だけ: credential の有無・二重投稿判定・送信予定の内容を表示する(アップロード・投稿・ログ更新はしない)
python3 -m social_video run --article-ids 77 --mode publish-dry-run [--skip-render]
# 生成だけ: 動画を作る(R2 が設定済みならアップロードまで)。全媒体ぶんの手動投稿キットを作る
python3 -m social_video run --article-ids 77 --mode generate-only
# 指定した媒体だけに投稿する
python3 -m social_video run --article-ids 77 --mode publish-selected-platforms --platforms instagram,facebook
# 有効な全媒体に投稿する(config で mode=manual の媒体は手動投稿キット)
python3 -m social_video run --latest 1 --mode full-auto

python3 -m social_video manifest --article-id 77     # manifest を表示
python3 -m social_video mark --article-id 77 --platform tiktok --status published --post-id 123
python3 -m social_video log pull | push              # 配信ログをデータbranchと同期
```

出力は `out/social_video/<article_id>/` に置かれます(`manifest.json`・`<video_asset_id>.mp4`・`manual/<platform>.md`)。

### モード

| mode | 動画生成 | R2アップロード | 投稿 | ログ更新 |
|---|---|---|---|---|
| `publish-dry-run` | する(`--skip-render` で省略可) | しない | しない(送信予定を表示) | しない |
| `generate-only` | する | R2設定があればする | しない(全媒体ぶん手動キット) | `manual_pending` |
| `publish-selected-platforms` | する | する | `--platforms` の媒体だけ | する |
| `full-auto` | する | する | config で enabled の全媒体 | する |

「動画生成は自動・投稿は手動」で運用するときは `generate-only` を使うか、媒体ごとに `platforms.<name>.mode` を `manual` にします。

## workflow

GitHub App の権限上、Claude は `.github/workflows/` を変更できません。テンプレートを [`docs/social-video-workflow.yml`](social-video-workflow.yml) に置いています。
オーナーが `.github/workflows/social-video.yml` にコピーしてコミットすると有効になります。

- 最初は `workflow_dispatch` で `publish-dry-run` → `generate-only` → `publish-selected-platforms` の順に試す
- 安定したら、テンプレート内でコメントアウトしている `schedule` か `workflow_run`(記事公開 workflow の完了をきっかけにする)を有効にする。inputs が無い起動では `--latest 1 --mode full-auto` になる
- 生成した MP4・manifest・手動投稿キットは artifact `social-video-<run_id>` に14日間残る

## 動画の仕様(全SNS共通で使える安全な仕様)

| 項目 | 値(`config.json` の `output`) |
|---|---|
| コンテナ | MP4(`+faststart`) |
| 映像 | H.264 High@4.1 / yuv420p / 1080×1920(9:16)/ 30fps / CRF20・最大8Mbps / 2秒GOP |
| 音声 | AAC-LC 128kbps / 48kHz / stereo(BGMが無いときは無音トラック) |
| 尺 | 3〜90秒の範囲で `manifest.duration_target_sec`(既定20秒) |

- 90秒以下にしているのは、Facebook Reels の上限に合わせて Instagram 用の同一MP4を Facebook にもそのまま使うためです
- 各SNSのwatermark・ロゴは焼き込みません(同じファイルを全媒体で使うため)
- レンダリング後に ffprobe でコーデック・比率・尺を確認し、外れていれば投稿しません

## クリエイティブを決めるとき(差し替えポイント)

| 決めること | 変える場所 |
|---|---|
| 尺 | `manifest.duration_target_sec` |
| シーン構成・本文シーン数・字幕の文字数 | `manifest.max_body_scenes` / `scene_text_max_chars` / `hook_max_chars`、または `manifest.py` |
| CTA・ハッシュタグ | `manifest.cta` / `base_hashtags` / `max_hashtags` |
| 字幕のフォント・色 | `renderer.options.font_file` / `font_color` / `text_box` |
| BGM | `renderer.options.audio_file`(権利処理済みの音源だけを使う) |
| 動画生成AI・テンプレートエンジン | `renderer.name = "command"` と `renderer.command`(例 `my-tool --manifest {manifest} --out {output}`)、または `renderers.py` に Renderer を追加 |
| 投稿本数 | workflow の `--latest N` / schedule の頻度 |
| 媒体ごとの自動/手動 | `platforms.<name>.enabled` / `mode`(`auto` / `manual`) |

上書きは `--config path/to/override.json` で渡せます(指定したキーだけが既定値に重なります)。
manifest のテキストは記事のタイトル・dek・本文からの抜粋だけで作ります(AIで新しい事実を足しません)。動画生成AIを使う場合も、記事にない事実(営業時間・料金・日付など)を足さないようにしてください。

## 二重投稿防止とログ

`data/social_video_log.json`(データbranch `bot/social-video-log` に保存。main には入れない)

- `records`: `article_id` + `video_asset_id` + `platform` + `status` + `post_id` / `permalink` / `error` / `updated_at`
- `assets`: 生成した動画(`video_asset_id`・R2の公開URL・sha256・サイズ)
- 同じ記事・同じ媒体に `published` / `submitted` / `unknown` があれば、動画を作り直しても自動投稿しない(`--force-repost` を明示したときだけ再投稿)
- `failed` / `manual_pending` は次回の実行で再試行される
- 1媒体の失敗で他の媒体は止めない。失敗・結果不明があれば終了コード1
- `unknown` は「投稿を確定させるリクエストを送った後にタイムアウトした」状態。投稿されている可能性があるため自動では再投稿しない。各SNSで実際に投稿されたかを確認して `mark` で `published`(post_id付き)か `failed` に直す
- `video_asset_id` は manifest(生成日時を除く)+ renderer + 出力設定のハッシュ。R2 のキーは `social-video/<article_id>/<video_asset_id>.mp4` で、同じ動画は再アップロードしない

## 手動fallback

自動投稿できない場合は `out/social_video/<article_id>/manual/<platform>.md`(artifact に含まれる)に手動投稿キットができます。
キットには動画ファイル名・R2の公開URL・タイトル・キャプション・手順・投稿後に実行する `mark` コマンドが入っています。

キットができるのは次の場合です。
- `generate-only` モード
- `platforms.<name>.mode = "manual"`(TikTok は既定で manual)
- credential が未設定(`manual_pending`)
- 投稿に失敗した(`failed`)
- TikTok の Upload(下書き)で受信箱に送った(`submitted`。キャプション入力と公開はアプリで行う)

手動で投稿したら、二重投稿を防ぐために記録します。
```bash
python3 -m social_video log pull
python3 -m social_video mark --article-id 77 --platform tiktok --status published --post-id <投稿ID> --permalink <URL>
python3 -m social_video log push
```

## 必要な GitHub Secrets

| Secret | 用途 | 未設定時 |
|---|---|---|
| `R2_ACCOUNT_ID` | Cloudflare アカウントID | 動画は artifact にだけ残る。公開URLが無いため Instagram / Facebook は手動キット |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | R2 APIトークン(S3互換、対象バケットの Object Read & Write) | 同上 |
| `R2_BUCKET` | バケット名 | 同上 |
| `R2_PUBLIC_BASE_URL` | 公開URLのベース(例 `https://media.shonandoors.com`) | 同上 |
| `INSTAGRAM_PUBLISH_ACCESS_TOKEN` | `instagram_content_publish` を含む長期トークン | Instagram は手動キット |
| `INSTAGRAM_BUSINESS_USER_ID` | 投稿先 Instagram プロアカウントID(既存Secretと共通) | 同上 |
| `FACEBOOK_PAGE_ID` | 投稿先FacebookページID | Facebook は手動キット |
| `FACEBOOK_PAGE_ACCESS_TOKEN` | ページアクセストークン(無期限のページトークン推奨) | 同上 |
| `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` | Google Cloud OAuth クライアント | YouTube は手動キット |
| `YOUTUBE_REFRESH_TOKEN` | `https://www.googleapis.com/auth/youtube.upload` で取得した refresh token | 同上 |
| `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` | TikTok for Developers のアプリ | TikTok は手動キット |
| `TIKTOK_REFRESH_TOKEN` | `video.upload`(と Direct Post なら `video.publish`)で取得した refresh token | 同上 |

- 既存の `INSTAGRAM_FACEBOOK_ACCESS_TOKEN` は Business Discovery 用で投稿権限が違うため、投稿には別の `INSTAGRAM_PUBLISH_ACCESS_TOKEN` を使います
- 手動テスト用に `TIKTOK_ACCESS_TOKEN`(24時間有効)を渡すこともできます
- 接続情報はログ・例外メッセージ・配信ログに出しません(トークンはURLのクエリに載せず、表示前に秘密値を伏せ字にします。R2 はエンドポイントURLも表示しません)

## 各SNSで人間側に必要な設定・審査

> 各社の審査要件・上限は変わることがあります。申請前に各社の最新の開発者ドキュメントを確認してください。

### Cloudflare R2
1. R2 バケットを作る(例 `shonan-media`)
2. 公開アクセスを設定する: カスタムドメイン(例 `media.shonandoors.com`)を推奨。`r2.dev` はレート制限がある開発用
3. R2 APIトークンを作る: 権限は対象バケットだけの Object Read & Write
4. 古い動画を消すライフサイクルルールを入れる(例 90日)。消すと、その動画を再利用するときは再アップロードになる
5. TikTok を `PULL_FROM_URL` で使う場合は、公開ドメインを TikTok 側で所有確認する(既定の `FILE_UPLOAD` なら不要)

### Instagram Reels
- Instagram アカウントをプロアカウント(ビジネス/クリエイター)にし、Facebookページとリンクする
- Meta for Developers でアプリを作り、Instagram Graph API(Facebook ログイン)を追加する
- 権限: `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`(構成によって `business_management`)
- **審査**: 投稿先が自社アカウントで、そのアカウントの管理者がアプリのロール(管理者/開発者/テスター)を持つ場合は、App Review なしの Standard Access で動きます。ロール外のアカウントに投稿する、または Advanced Access が必要な構成では App Review と Meta のビジネス認証が必要です
- 長期トークン(60日)は期限前に更新するか、システムユーザーのトークンを使う
- API経由の投稿は24時間あたりの上限があります(`/{ig-user-id}/content_publishing_limit` で確認)
- Reels の動画は Meta が `video_url` から取得します(R2 の公開URLが必須)

### Facebook Page Reels
- Instagram と同じ Meta アプリでよい。権限: `pages_manage_posts`, `pages_read_engagement`, `pages_show_list`
- ページアクセストークンは、長期ユーザートークンから取得した無期限のページトークンか、システムユーザーのトークンを使う
- **審査**: Instagram と同じ。ロールを持つ管理者が自社ページに投稿する範囲なら App Review は不要。それ以外は App Review とビジネス認証
- Reels は 3〜90秒・9:16。Instagram 用と同じ R2 上の MP4 を `file_url` で渡します

### YouTube Shorts
- Google Cloud プロジェクトで YouTube Data API v3 を有効にし、OAuth 同意画面と OAuth クライアント(デスクトップ/ウェブ)を作る
- 投稿先チャンネルのアカウントで `youtube.upload` スコープを許可し、refresh token を取得する
- **注意**: OAuth 同意画面が「テスト」のままだと refresh token が7日で失効します。「本番」に切り替えてください(機密スコープのため、Google の OAuth 審査が求められる場合があります)
- **審査**: 2020年7月28日以降に作った未監査の API プロジェクトからアップロードした動画は、`privacyStatus` の指定に関わらず非公開(private)に固定されます。公開で自動投稿するには YouTube API Services の監査(Audit and Quota Extension フォーム)を通す必要があります。通過までは、投稿後に YouTube Studio で手動で公開に切り替えます(配信ログの detail に `privacyStatus` が残ります)
- `videos.insert` は1回あたりのquota消費が大きいため、1日の投稿本数に注意します(既定のquotaは 10,000 units/日)
- 9:16・3分以内の動画は自動的に Shorts として扱われます。タイトルには既定で `#Shorts` を付けます(`platforms.youtube.add_shorts_hashtag`)

### TikTok
- TikTok for Developers でアプリを作り、Login Kit と Content Posting API を追加する
- スコープ: Upload(下書き)なら `video.upload`、Direct Post なら `video.publish`
- 投稿先アカウントで認可して refresh token を取得する(refresh token の有効期限は約1年。更新時に新しい値が返る場合があるため、期限前に Secrets を更新する)
- **審査**: 監査(Audit)を通過していないアプリは、Direct Post が `SELF_ONLY`(自分のみ表示)に制限され、利用できるユーザー数にも上限があります。公開投稿を自動化するには監査が必要です。監査では TikTok の UX ガイドライン(投稿前のプレビュー・プライバシー設定の選択・同意表示など)への準拠が求められ、サーバー側だけの全自動投稿は通りにくい前提で考えてください
- そのため既定は `platforms.tiktok.mode = "manual"`(手動キット)。credential を入れたら `mode = "auto"` + `post_mode = "upload"` で受信箱(下書き)に送り、キャプションを入れて公開する操作だけアプリで行う、という段階運用を想定しています
- `post_mode = "direct"` にした場合、アカウントで使えない `privacy_level` を指定すると投稿せず失敗(手動キット)にします。未監査のうちは `SELF_ONLY` にしてください
- 動画生成AIで作った動画にする場合は `platforms.tiktok.is_aigc = true` にします(AI生成コンテンツのラベル)

## テスト

`tests/test_social_video.py`(外部APIと ffmpeg はすべてスタブ。ffmpeg がある環境では実レンダリングも1本確認する)。
manifest / 公開記事判定 / 設定検証 / renderer の差し替え / R2 署名と秘密情報の非表示 / 各SNSのAPI手順 / credential 未設定 / タイムアウト(未投稿と結果不明の区別)/ 二重投稿防止 / 1媒体の失敗の隔離 / 各モードを確認しています。
