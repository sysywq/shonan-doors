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


def build_tweet_text(article):
    """記事内容に応じて投稿文を生成する。固定文ではなく、
    title(読みたくなる1文目)・dek(短い要約)・area・URLを組み合わせ、
    X(Twitter)の280文字(weighted)制限に収まるよう本文を調整する。"""
    area = article["area"]
    url = f"{SITE_DOMAIN}/articles/{article['slug']}/"

    hashtags = ["#湘南", f"#{area}", "#ShonanDoors"]
    hashtag_line = " ".join(hashtags)

    title = article["title"]
    dek = article.get("dek", "")

    fixed_parts = [
        title,
        f"📍{area}",
        url,
        hashtag_line,
    ]
    fixed_weight = sum(weighted_length(p) for p in fixed_parts)
    newline_overhead = weighted_length("\n\n") * (len(fixed_parts) + 1)
    url_actual_weight = weighted_length(url)
    fixed_weight = fixed_weight - url_actual_weight + X_URL_WEIGHT

    budget_for_dek = X_MAX_WEIGHTED_LENGTH - fixed_weight - newline_overhead - 10
    dek_trimmed = dek
    if weighted_length(dek_trimmed) > budget_for_dek:
        approx_chars = max(0, budget_for_dek // 2 - 1)
        dek_trimmed = dek[:approx_chars]
        while weighted_length(dek_trimmed + "…") > budget_for_dek and len(dek_trimmed) > 0:
            dek_trimmed = dek_trimmed[:-1]
        dek_trimmed = dek_trimmed + "…" if dek_trimmed else ""

    lines = [title]
    if dek_trimmed:
        lines.append(dek_trimmed)
    lines.append(f"📍{area}\n🔗 記事はこちら\n{url}")
    lines.append(hashtag_line)
    text = "\n\n".join(lines)

    if weighted_length(text) > X_MAX_WEIGHTED_LENGTH:
        lines = [title, f"📍{area}\n🔗 記事はこちら\n{url}", hashtag_line]
        text = "\n\n".join(lines)

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--article-ids", required=True,
                         help="カンマ区切りの記事ID(例: 93,94,95)。単体テストなら1件だけでもよい。")
    parser.add_argument("--skip-url-check", action="store_true",
                         help="URL公開待ちを省略する(手元での動作確認等、DRY_RUN以外で待機したくない場合)")
    args = parser.parse_args()

    target_ids = [int(x) for x in args.article_ids.split(",") if x.strip()]
    if not target_ids:
        print("投稿対象の記事IDが指定されていません。何もせず終了します。")
        return

    articles = load_json(ARTICLES_JSON_PATH, [])
    articles_by_id = {a["id"]: a for a in articles}
    posted_ids, records = load_post_log()

    print(f"{'[DRY RUN] ' if DRY_RUN else ''}対象記事ID: {target_ids}")
    print(f"投稿済みログ件数(累計): {len(posted_ids)}")

    success_count = 0
    failure_count = 0
    timeout_count = 0

    for article_id in target_ids:
        article = articles_by_id.get(article_id)
        if article is None:
            print(f"  id={article_id}: articles.jsonに見つからないためスキップします。")
            continue

        if article_id in posted_ids:
            print(f"  id={article_id} ({article['slug']}): 投稿済みのためスキップします。")
            continue

        text = build_tweet_text(article)
        weight = weighted_length(text)
        url = f"{SITE_DOMAIN}/articles/{article['slug']}/"

        print(f"\n  --- id={article_id} ({article['slug']}) ---")
        print(f"  記事タイトル: {article['title']}")
        print(f"  記事URL: {url}")
        print(f"  投稿文({weight}/{X_MAX_WEIGHTED_LENGTH} weighted chars):")
        print("  " + text.replace("\n", "\n  "))

        if DRY_RUN:
            print("  [DRY RUN] 実際の投稿は行いません(URL公開待ちも省略します)。")
            success_count += 1
            continue

        if not args.skip_url_check:
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
            save_post_log(records)
            success_count += 1
        except Exception as e:
            print(f"  投稿失敗: {e}", file=sys.stderr)
            failure_count += 1

    print(f"\n{'[DRY RUN] ' if DRY_RUN else ''}完了: 成功{success_count}件 / 失敗{failure_count}件 "
          f"/ URL未公開でスキップ{timeout_count}件 "
          f"/ その他対象外(既投稿・データ不整合等){len(target_ids) - success_count - failure_count - timeout_count}件")

    if not DRY_RUN and (failure_count > 0 or timeout_count > 0):
        sys.exit(1)


if __name__ == "__main__":
    main()
