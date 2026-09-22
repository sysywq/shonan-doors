"""
indexnow_submit.py
--------------------
湘南Doorsで新しく公開された記事URLを、IndexNow(Bing等の対応検索エンジン)へ
まとめて通知する。

設計方針:
- generate_articles.py / build.py / post_to_x.py とは完全に疎結合。
  このスクリプトが失敗しても、記事生成・ビルド・GitHub Pagesへの
  デプロイ・X自動投稿には一切影響しない(呼び出し元のGitHub Actions側で、
  別ジョブとして実行することで担保する)。
- IndexNowはSEOの補助的な即時通知であり、Google Search Console・
  sitemap.xml・robots.txt等の既存SEO実装とは独立した仕組み。
  このスクリプト・keyファイル以外、それらには一切手を加えない。
- 対象URLは、呼び出し元から渡される article ID のリストのみ
  (今日生成された記事に限定する。articles.json全体を毎回スキャンしない)。
- IndexNowのkeyはその性質上「公開ファイルとして配信される」ことが仕様上
  前提のため、GitHub Secretsで隠す必要はない(秘密情報ではない)。
  X APIのcredential等とは明確に別枠で扱う。
- 重複排除は「1回のrun内」に限定する。IndexNowは同一URLの再通知を
  問題なく許容する設計のため、日をまたいだ送信履歴の永続管理は行わない
  (状態管理を複雑にしないため。x_post_log.jsonのような台帳は持たない)。

IndexNow仕様(2024年時点の推奨方式):
  POST https://api.indexnow.org/indexnow
  Content-Type: application/json; charset=utf-8
  Body: {"host": "...", "key": "...", "keyLocation": "...", "urlList": [...]}

  成功: 200 or 202
  429 / 5xx: 検索エンジン側のレート制限・一時的な問題 → warning扱い
  400 / 403 / 422 等: リクエスト内容・key不一致等の設定ミス → error扱い
  いずれの場合も、このスクリプト自体は原則exit 0とし、
  「IndexNow送信失敗で記事生成workflow全体を失敗扱いにしない」という
  要件を満たす(ただし全滅した場合のみexit 1とし、GitHub Actions上で
  このジョブ自体は目立つように失敗表示させる。post_to_x.pyと同じ方針)。

呼び出し方法:
  python indexnow_submit.py --article-ids 93,94,95
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
ARTICLES_JSON_PATH = os.path.join(ROOT, "data", "articles.json")

SITE_HOST = "www.shonandoors.com"
SITE_DOMAIN = f"https://{SITE_HOST}"
INDEXNOW_KEY = "ce88ea6295073371cba4698a77227ca3"
INDEXNOW_KEY_LOCATION = f"{SITE_DOMAIN}/{INDEXNOW_KEY}.txt"
INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"

REQUEST_TIMEOUT_SEC = 15
# レスポンスbodyに万一APIキー等が写り込んでいた場合でも、ログへ全文出力しない
# ための上限文字数(通常IndexNowのレスポンスbodyは空か短いエラーメッセージのみ)。
RESPONSE_LOG_MAX_CHARS = 300


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_target_urls(article_ids):
    """記事IDのリストから、IndexNowへ通知するURL一覧を作る。
    - data/articles.json から実際のslugを解決する(IDだけでは通知できないため)。
    - 重複を排除する(1回のrun内)。
    - shonandoors.com配下のURLだけに限定する(念のための安全策。
      URL自体をこの関数内で組み立てているため通常は外部ドメインが
      混じることはないが、仕様通り明示的にフィルタする)。
    - articles.jsonに存在しないIDは静かにスキップする。
    """
    articles = load_json(ARTICLES_JSON_PATH, [])
    articles_by_id = {a["id"]: a for a in articles}

    urls = []
    seen = set()
    for article_id in article_ids:
        article = articles_by_id.get(article_id)
        if article is None:
            continue
        url = f"{SITE_DOMAIN}/articles/{article['slug']}/"
        if url in seen:
            continue
        if not url.startswith(f"{SITE_DOMAIN}/"):
            continue
        seen.add(url)
        urls.append(url)
    return urls


def submit_to_indexnow(url_list):
    """IndexNowへJSON POSTで一括通知する。
    戻り値: (ok: bool, message: str) — okがFalseでも例外は投げない
    (呼び出し元でexit codeを判断する)。"""
    payload = {
        "host": SITE_HOST,
        "key": INDEXNOW_KEY,
        "keyLocation": INDEXNOW_KEY_LOCATION,
        "urlList": url_list,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        INDEXNOW_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "ShonanDoorsIndexNowBot/1.0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            status = resp.status
            resp_text = resp.read().decode("utf-8", errors="replace")[:RESPONSE_LOG_MAX_CHARS]
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            resp_text = e.read().decode("utf-8", errors="replace")[:RESPONSE_LOG_MAX_CHARS]
        except Exception:
            resp_text = "(レスポンス本文を取得できませんでした)"
    except urllib.error.URLError as e:
        return False, f"IndexNow warning: request failed ({e.reason})"
    except Exception as e:
        return False, f"IndexNow warning: unexpected error ({e})"

    if status in (200, 202):
        return True, f"IndexNow: {len(url_list)} URLs submitted (HTTP {status})"
    if status == 429 or 500 <= status < 600:
        return False, f"IndexNow warning: HTTP {status} - {resp_text}"
    if status in (400, 403, 422):
        return False, f"IndexNow error: invalid key configuration (HTTP {status}) - {resp_text}"
    return False, f"IndexNow warning: unexpected HTTP {status} - {resp_text}"


def check_url_reachable(url):
    """軽量な1回きりの到達性チェック(リトライしない)。
    IndexNowは「まだ完全に反映されていないURLの通知」もある程度許容する
    設計のため、post_to_x.pyのような長い待機・リトライループは行わない。
    ここでは「まだ本番に反映されていないかもしれない」ことをログで
    分かるようにするだけで、到達確認できなくても通知対象からは外さない
    (除外してしまうと、タイミング次第でそのURLが永久に通知されなくなる
    リスクの方が大きいため)。"""
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "ShonanDoorsIndexNowBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--article-ids", required=True,
                         help="カンマ区切りの記事ID(例: 93,94,95)。空文字列なら何もしない。")
    parser.add_argument("--skip-reachability-check", action="store_true",
                         help="本番URLの到達性チェックを省略する(手元でのテスト等で使う)")
    args = parser.parse_args()

    raw_ids = [x.strip() for x in args.article_ids.split(",") if x.strip()]
    if not raw_ids:
        print("IndexNow: skipped (no new URLs)")
        return

    article_ids = [int(x) for x in raw_ids]
    url_list = build_target_urls(article_ids)

    if not url_list:
        print("IndexNow: skipped (no new URLs)")
        return

    print(f"IndexNow: submitting {len(url_list)} URL(s):")
    for u in url_list:
        if not args.skip_reachability_check:
            reachable = check_url_reachable(u)
            status_note = "" if reachable else "(まだHTTP 200で取得できません。反映待ちの可能性がありますが通知は続行します)"
            print(f"  - {u} {status_note}".rstrip())
        else:
            print(f"  - {u}")

    ok, message = submit_to_indexnow(url_list)
    print(message)

    if not ok:
        # IndexNow送信の失敗は、このジョブ自体は目立つように失敗表示にするが
        # (post_to_x.pyと同じ方針)、別ジョブとして疎結合にしてあるため、
        # 記事生成・build・GitHub Pagesへのデプロイには影響しない。
        sys.exit(1)


if __name__ == "__main__":
    main()
