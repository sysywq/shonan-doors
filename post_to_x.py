"""
post_to_x.py
--------------
湘南Doorsの記事を、X(Twitter)へ自動投稿する。

設計方針:
- generate_articles.py / build.py とは完全に疎結合。
  このスクリプトが失敗しても、記事生成・ビルド・GitHub Pagesへの
  デプロイには一切影響しない(呼び出し元のGitHub Actions側で、
  別ジョブとして実行することで担保する)。
- 対象記事は、呼び出し元から渡される article ID のリストのみ
  (日次自動投稿では今日生成された記事のみ。単体テスト用workflowでは
  任意の1件を指定できる)。
- 投稿前に、対象記事のURLが実際にHTTP 200で取得できるまで待機する
  (GitHub Pagesへの反映タイムラグを吸収し、公開前のURLを投稿しないため)。
  一定時間待っても公開されない記事はスキップし、その旨を分かるように
  終了コードを非ゼロにする(ただし記事生成・build・deployには影響しない)。
- 重複投稿防止のため、投稿に成功した記事の
    - article_id
    - tweet_id
    - posted_at (UTC ISO8601)
  をdata/x_post_log.jsonに追記し、次回以降は同じarticle_idをスキップする。
  この台帳はdata/articles.json本体とは別ファイルにしており、
  記事が数百・数千件になってもこのファイル自体は
  「投稿済みレコードの配列」だけなので肥大化しにくい。
- DRY_RUN=true の場合、実際にはXへ投稿せず、URL公開待ちも行わず、
  投稿予定記事・生成した投稿文・URL・投稿済み判定をログに出力するだけ。
  x_post_log.jsonの更新も行わない(何度でも再実行して確認できるようにするため)。

必要な環境変数(本番投稿時。DRY_RUN=trueの場合は無くても動作する):
  X_API_KEY
  X_API_SECRET
  X_ACCESS_TOKEN
  X_ACCESS_TOKEN_SECRET

呼び出し方法:
  python post_to_x.py --article-ids 93,94,95
  DRY_RUN=true python post_to_x.py --article-ids 93,94,95
  python post_to_x.py --article-ids 77 --skip-url-check   (単体テスト等で待機を省略したい場合)
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
ARTICLES_JSON_PATH = os.path.join(ROOT, "data", "articles.json")
X_POST_LOG_PATH = os.path.join(ROOT, "data", "x_post_log.json")
SITE_DOMAIN = "https://www.shonandoors.com"

AREA_EN = {
    "藤沢": "fujisawa", "茅ヶ崎": "chigasaki", "鎌倉": "kamakura", "平塚": "hiratsuka",
    "大磯": "oiso", "二宮": "ninomiya", "逗子": "zushi", "葉山": "hayama",
}

# カテゴリー表示名・絵文字・ハッシュタグ用の単語。
# build.py の CATS(表示名)・CAT_EN(英語slug)と値の対応を揃えている
# (post_to_x.py は他ファイルに依存せず単体で動かせる設計のため、値を複製している)。
# 表示名はbuild.py側の既存定義(CATS[key]["label"])をそのまま踏襲する。
CATEGORY_INFO = {
    "t": {"label": "観光", "emoji": "🌊", "hashtag_word": "観光"},
    "b": {"label": "企業・店舗", "emoji": "💼", "hashtag_word": "企業店舗"},  # "・"はハッシュタグに使わない
    "g": {"label": "グルメ", "emoji": "🍽️", "hashtag_word": "グルメ"},
    "p": {"label": "人", "emoji": "👤", "hashtag_word": "人"},
    "c": {"label": "文化", "emoji": "🎨", "hashtag_word": "文化"},
    "e": {"label": "イベント", "emoji": "🎉", "hashtag_word": "イベント"},
    "l": {"label": "暮らし", "emoji": "🏠", "hashtag_word": "暮らし"},
}

DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

X_MAX_WEIGHTED_LENGTH = 280
X_URL_WEIGHT = 23

URL_CHECK_INTERVAL_SEC = 15
URL_CHECK_TIMEOUT_SEC = 300  # 最大5分


def weighted_length(text):
    """X(Twitter)のweighted length計算の簡易近似。
    U+1100以降の主な全角相当の文字(ひらがな・カタカナ・漢字・全角記号等)を
    2文字分としてカウントする。半角英数・記号は1文字分。"""
    total = 0
    for ch in text:
        total += 2 if ord(ch) >= 0x1100 else 1
    return total


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_post_log():
    """戻り値: (posted_article_ids: set, full_records: list)"""
    data = load_json(X_POST_LOG_PATH, {"posts": []})
    records = data.get("posts", [])
    posted_ids = {r["article_id"] for r in records}
    return posted_ids, records


def save_post_log(records):
    data = {"posts": records}
    with open(X_POST_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def wait_for_url_published(url):
    """対象URLがHTTP 200で取得できるまで、一定間隔でリトライする。
    タイムアウトした場合はFalseを返す(記事生成・build・deployには影響させず、
    このスクリプト側でその記事をスキップする判断材料にするだけ)。"""
    deadline = time.monotonic() + URL_CHECK_TIMEOUT_SEC
    attempt = 0
    while True:
        attempt += 1
        try:
            req = urllib.request.Request(url, method="GET", headers={"User-Agent": "ShonanDoorsXBot/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    print(f"    [{attempt}回目] {url} -> HTTP {resp.status}(公開確認OK)")
                    return True
                print(f"    [{attempt}回目] {url} -> HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            print(f"    [{attempt}回目] {url} -> HTTP {e.code}")
        except Exception as e:
            print(f"    [{attempt}回目] {url} -> 取得失敗({e})")

        if time.monotonic() >= deadline:
            print(f"    タイムアウト({URL_CHECK_TIMEOUT_SEC}秒)。このURLはまだ公開されていません。")
            return False
        time.sleep(URL_CHECK_INTERVAL_SEC)


def effective_weight(text, url):
    """実際にX上でカウントされる重みを計算する。text内のURL部分は
    t.co短縮後の固定23として扱い、実URLの文字数の影響を受けないようにする。"""
    text_without_url = text.replace(url, "")
    return weighted_length(text_without_url) + X_URL_WEIGHT


def build_tweet_text(article):
    """投稿文を生成する。

    フォーマット:
        {タイトル}

        📍{エリア}
        {カテゴリ絵文字} {カテゴリ名}

        {ハッシュタグ(3〜4個)}

        {URL}

    タイトルはarticles.jsonの正式なタイトルをそのまま使う(AI再生成はしない)。
    エリア・カテゴリ表示名はbuild.py側の既存マッピングと値を揃えた
    AREA_EN/CATEGORY_INFOを使う。未知のカテゴリコードが来た場合は、
    絵文字なし・カテゴリコードそのままの表示に安全にfallbackする。

    280文字(weighted)を超える場合の優先順位:
      1. URLは必ず残す
      2. エリア表示を残す
      3. カテゴリ表示を残す
      4. ハッシュタグを残す
      5. タイトルを安全に短縮する
      6. それでも超える場合はハッシュタグを後ろから減らす
    """
    url = f"{SITE_DOMAIN}/articles/{article['slug']}/"
    title = article["title"]
    area = article["area"]
    cat_code = article.get("cat", "")
    cat_info = CATEGORY_INFO.get(cat_code)
    if cat_info:
        cat_label = cat_info["label"]
        cat_emoji = cat_info["emoji"]
        cat_hashtag_word = cat_info["hashtag_word"]
    else:
        # 未知のカテゴリコード: 絵文字なしでコードをそのまま表示する安全fallback
        cat_label = cat_code
        cat_emoji = ""
        cat_hashtag_word = cat_code

    area_line = f"📍{area}"
    cat_line = f"{cat_emoji} {cat_label}".strip()

    hashtags = ["#湘南", f"#{area}", f"#湘南{cat_hashtag_word}", "#ShonanDoors"]

    def compose(title_text, hashtag_list):
        hashtag_line = " ".join(hashtag_list)
        parts = [title_text, f"{area_line}\n{cat_line}"]
        if hashtag_line:
            parts.append(hashtag_line)
        parts.append(url)
        return "\n\n".join(parts)

    # 1〜4(URL・エリア・カテゴリ・ハッシュタグ)を固定した状態で、
    # タイトルに使える予算を計算する。
    fixed_text = compose("", hashtags)
    fixed_weight = effective_weight(fixed_text, url) - weighted_length("\n\n")  # タイトル分の空行1つを後で足す
    budget_for_title = X_MAX_WEIGHTED_LENGTH - fixed_weight - 5  # 安全マージン5

    title_trimmed = title
    if weighted_length(title_trimmed) > budget_for_title:
        approx_chars = max(0, budget_for_title // 2 - 1)
        title_trimmed = title[:approx_chars]
        while weighted_length(title_trimmed + "…") > budget_for_title and len(title_trimmed) > 0:
            title_trimmed = title_trimmed[:-1]
        title_trimmed = title_trimmed + "…" if title_trimmed else title[:1]

    text = compose(title_trimmed, hashtags)

    # 5でも収まらない場合(タイトルを最小限まで削ってもまだ超える場合)は、
    # ハッシュタグを後ろから減らして再構成する。URL・エリア・カテゴリは最後まで残す。
    remaining_hashtags = list(hashtags)
    while effective_weight(text, url) > X_MAX_WEIGHTED_LENGTH and len(remaining_hashtags) > 0:
        remaining_hashtags = remaining_hashtags[:-1]
        text = compose(title_trimmed, remaining_hashtags)

    # 最終安全チェック(それでも超える想定外のケース)
    if effective_weight(text, url) > X_MAX_WEIGHTED_LENGTH:
        text = compose(f"{title[:1]}…", [])

    return text


def post_tweet(text):
    """実際にXへ投稿する。tweepy(OAuth 1.0a User Context)を使用する。
    DRY_RUN時はこの関数自体を呼ばない。"""
    import tweepy

    api_key = os.environ["X_API_KEY"]
    api_secret = os.environ["X_API_SECRET"]
    access_token = os.environ["X_ACCESS_TOKEN"]
    access_token_secret = os.environ["X_ACCESS_TOKEN_SECRET"]

    client = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_token_secret,
    )
    response = client.create_tweet(text=text)
    return response.data["id"]


def process_articles(target_ids, articles_by_id, posted_ids, records, skip_url_check=False, interval_sec=0):
    """記事IDのリストを順番に処理する共通ロジック。
    通常の日次投稿・単体テスト・Backfillのいずれからも呼ばれる。
    interval_secを指定すると、投稿(またはDRY RUN確認)ごとにその秒数だけ待機する
    (Backfillで短時間に大量投稿しないようにするため。通常投稿では0のまま)。"""
    success_count = 0
    failure_count = 0
    timeout_count = 0
    skipped_count = 0

    for i, article_id in enumerate(target_ids):
        article = articles_by_id.get(article_id)
        if article is None:
            print(f"  id={article_id}: articles.jsonに見つからないためスキップします。")
            skipped_count += 1
            continue

        if article_id in posted_ids:
            print(f"  id={article_id} ({article['slug']}): 投稿済みのためスキップします。")
            skipped_count += 1
            continue

        text = build_tweet_text(article)
        url = f"{SITE_DOMAIN}/articles/{article['slug']}/"
        weight = effective_weight(text, url)

        print(f"\n  --- id={article_id} ({article['slug']}) [{i + 1}/{len(target_ids)}] ---")
        print(f"  記事タイトル: {article['title']}")
        print(f"  記事URL: {url}")
        print(f"  投稿文({weight}/{X_MAX_WEIGHTED_LENGTH} weighted chars):")
        print("  " + text.replace("\n", "\n  "))

        if DRY_RUN:
            print("  [DRY RUN] 実際の投稿は行いません(URL公開待ちも省略します)。")
            success_count += 1
            continue

        if not skip_url_check:
            print(f"  記事URLが公開されるまで待機します(最大{URL_CHECK_TIMEOUT_SEC}秒、{URL_CHECK_INTERVAL_SEC}秒間隔)...")
            if not wait_for_url_published(url):
                print(f"  id={article_id}: URLが公開されないためこの記事の投稿をスキップします。")
                timeout_count += 1
                continue

        try:
            tweet_id = post_tweet(text)
            posted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            print(f"  投稿成功: tweet_id={tweet_id} posted_at={posted_at}")
            records.append({"article_id": article_id, "tweet_id": str(tweet_id), "posted_at": posted_at})
            posted_ids.add(article_id)
            save_post_log(records)  # 1件成功するごとに都度保存(途中失敗時も再開できるようにするため)
            success_count += 1
        except Exception as e:
            print(f"  投稿失敗: {e}", file=sys.stderr)
            failure_count += 1

        if interval_sec > 0 and i < len(target_ids) - 1:
            print(f"  次の投稿まで{interval_sec}秒待機します...")
            time.sleep(interval_sec)

    return success_count, failure_count, timeout_count, skipped_count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--article-ids",
                         help="カンマ区切りの記事ID(例: 93,94,95)。単体テストなら1件だけでもよい。"
                              "--backfillと同時には指定できない。")
    parser.add_argument("--backfill", action="store_true",
                         help="articles.jsonの全記事のうち、x_post_log.jsonにまだ記録が無いものを"
                              "古い記事から順に対象にする(過去記事の一括投稿用)。")
    parser.add_argument("--limit", type=int, default=0,
                         help="--backfillと併用。0は無制限(未投稿の全記事が対象)。"
                              "正の値を指定すると、古い記事からその件数だけに絞る。")
    parser.add_argument("--skip-url-check", action="store_true",
                         help="URL公開待ちを省略する(手元での動作確認等、DRY_RUN以外で待機したくない場合)")
    args = parser.parse_args()

    if args.backfill and args.article_ids:
        print("--backfill と --article-ids は同時に指定できません。", file=sys.stderr)
        sys.exit(2)
    if not args.backfill and not args.article_ids:
        print("--article-ids または --backfill のいずれかを指定してください。", file=sys.stderr)
        sys.exit(2)

    articles = load_json(ARTICLES_JSON_PATH, [])
    articles_by_id = {a["id"]: a for a in articles}
    posted_ids, records = load_post_log()

    interval_sec = 0
    if args.backfill:
        # 古い記事から順に、まだx_post_log.jsonに記録の無いものだけを対象にする。
        unposted = [a for a in articles if a["id"] not in posted_ids]
        unposted.sort(key=lambda a: (a["date"], a["id"]))  # 日付が同じ場合はID順で安定させる
        if args.limit > 0:
            unposted = unposted[: args.limit]
        target_ids = [a["id"] for a in unposted]
        interval_sec = int(os.environ.get("X_BACKFILL_INTERVAL_SECONDS", "45"))
        print(f"{'[DRY RUN] ' if DRY_RUN else ''}[BACKFILL] 未投稿記事{len(target_ids)}件が対象です"
              f"(limit={args.limit or '無制限'}, 投稿間隔={interval_sec}秒)")
    else:
        target_ids = [int(x) for x in args.article_ids.split(",") if x.strip()]
        if not target_ids:
            print("投稿対象の記事IDが指定されていません。何もせず終了します。")
            return

    print(f"{'[DRY RUN] ' if DRY_RUN else ''}対象記事ID: {target_ids}")
    print(f"投稿済みログ件数(累計): {len(posted_ids)}")

    success_count, failure_count, timeout_count, skipped_count = process_articles(
        target_ids, articles_by_id, posted_ids, records,
        skip_url_check=args.skip_url_check, interval_sec=interval_sec,
    )

    print(f"\n{'[DRY RUN] ' if DRY_RUN else ''}完了: 成功{success_count}件 / 失敗{failure_count}件 "
          f"/ URL未公開でスキップ{timeout_count}件 "
          f"/ その他対象外(既投稿・データ不整合等){skipped_count}件")

    if not DRY_RUN and (failure_count > 0 or timeout_count > 0):
        sys.exit(1)


if __name__ == "__main__":
    main()
