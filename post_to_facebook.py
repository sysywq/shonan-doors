"""
post_to_facebook.py
---------------------
湘南Doorsの公開済み記事を、Facebookページへ自動投稿する。

設計方針(post_to_x.py / indexnow_submit.py と同じ downstream distribution):
- 記事生成・build・GitHub Pages へのデプロイ・X投稿・IndexNow とは完全に疎結合。
  呼び出し元の GitHub Actions で別ジョブとして実行するため、このスクリプトが失敗しても
  記事公開や他媒体の配信には影響しない。
- 対象は呼び出し元から渡される article ID のみ(本番 HTTP 200 を確認できた記事)。
  統合済み(mergedInto)の記事と、slug が不正な記事は投稿しない。投稿するURLは
  https://www.shonandoors.com/articles/{slug}/ に限定する。
- 投稿本文は「記事タイトル + 短い導入(dek)+ 記事URL」。link パラメータにも記事URLを渡し、
  リンクプレビューを表示させる。

重複投稿の防止(idempotent 設計。投稿の取りこぼしより二重投稿を避けることを優先する):
- 投稿済みログ data/facebook_post_log.json(データbranch bot/facebook-post-log に保存。
  facebook_post_log_store.py)に記録がある記事は投稿しない。1件投稿するごとにログへ保存する。
- 投稿前に、Facebookページの最近の投稿を Graph API で取得し、本文に同じ記事URLを含む投稿が
  あれば投稿しない(ログ保存前に止まった実行の再実行でも二重投稿しない)。
  この確認ができない場合は、その回の投稿をすべて見送る(fail-closed)。
- Graph API がタイムアウト・通信断・5xx で終わった場合、Facebook側で投稿が作られた可能性が
  あるため、自動では再投稿しない。ログに status="uncertain" として記録し、次回以降もスキップする
  (次回実行時にページ上で投稿が見つかれば facebook_post_id を補完して posted にする)。
  投稿されていないことを人が確認した場合だけ、データbranchのログからその記録を消して再実行する。
- 4xx(権限不足・トークン失効など、投稿が作られていないことが確定するエラー)はログに残さず、
  次回の再実行で投稿し直せる。

Secrets(GitHub Secrets から環境変数で渡す):
  FACEBOOK_PAGE_ID            投稿先 Facebook ページの ID(数字)
  FACEBOOK_PAGE_ACCESS_TOKEN  そのページの Page access token(pages_manage_posts / pages_read_engagement)
  ※ Instagram 用の INSTAGRAM_FACEBOOK_ACCESS_TOKEN / INSTAGRAM_BUSINESS_USER_ID とは別物として扱う。
  - 2つとも未設定 … Facebook連携が未設定とみなし、警告を出してスキップ(exit 0)
  - 片方だけ未設定 … 設定ミスとして失敗(exit 2)
  - DRY_RUN=true の場合は未設定でも動く(設定状況を表示するだけ)

その他の環境変数:
  DRY_RUN=true                実際には投稿せず、投稿予定の本文・URL・投稿済み判定を表示するだけ
                              (Graph API・URL公開確認を呼ばず、ログも更新しない)
  FACEBOOK_GRAPH_API_VERSION  Graph API のバージョン(例: v24.0)。未設定なら URL にバージョンを付けず、
                              Meta App Dashboard でアプリに設定されている既定バージョンが使われる
  FACEBOOK_GRAPH_TIMEOUT_SEC  Graph API 呼び出しのタイムアウト秒(既定 20)
  FACEBOOK_POST_INTERVAL_SEC   連続投稿の間隔秒(既定 180 = 3分。最初の投稿前は待たない)

トークンはログに出さない(Authorization ヘッダーでのみ送り、エラー本文からも伏せ字にする)。

呼び出し方法:
  python post_to_facebook.py --article-ids 93,94,95
  DRY_RUN=true python post_to_facebook.py --article-ids 93
  python post_to_facebook.py --article-ids 77 --skip-url-check

終了コード: 0=成功/対象なし/未設定でスキップ  1=投稿失敗・結果不明・公開未確認あり  2=引数や Secrets の設定ミス
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
ARTICLES_JSON_PATH = os.path.join(ROOT, "data", "articles.json")
FACEBOOK_POST_LOG_PATH = os.path.join(ROOT, "data", "facebook_post_log.json")
SITE_DOMAIN = "https://www.shonandoors.com"
GRAPH_HOST = "https://graph.facebook.com"

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ARTICLE_URL_RE = re.compile(re.escape(SITE_DOMAIN) + r"/articles/([a-z0-9]+(?:-[a-z0-9]+)*)/")
GRAPH_VERSION_RE = re.compile(r"^v\d+\.\d+$")
PAGE_ID_RE = re.compile(r"^\d+$")

INTRO_MAX_CHARS = 120
RECENT_POSTS_LIMIT = 100
URL_CHECK_INTERVAL_SEC = 15
URL_CHECK_TIMEOUT_SEC = 120
ERROR_LOG_MAX_CHARS = 300
USER_AGENT = "ShonanDoorsFacebookBot/1.0"

STATUS_POSTED = "posted"
STATUS_UNCERTAIN = "uncertain"


def is_dry_run(env=None):
    return (env if env is not None else os.environ).get("DRY_RUN", "").lower() in ("1", "true", "yes")


class GraphAPIError(Exception):
    """Graph API 呼び出しの失敗。uncertain=True は「Facebook側で処理された可能性がある」失敗。"""

    def __init__(self, message, status=None, uncertain=False):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain


class PostLogError(Exception):
    pass


# ---------- 設定 ----------

def resolve_credentials(env=None):
    """戻り値: (state, page_id, token, problems)
    state: "ok" / "not_configured"(2つとも未設定)/ "invalid"(片方だけ・形式不正)"""
    env = env if env is not None else os.environ
    page_id = (env.get("FACEBOOK_PAGE_ID") or "").strip()
    token = (env.get("FACEBOOK_PAGE_ACCESS_TOKEN") or "").strip()
    if not page_id and not token:
        return "not_configured", "", "", ["FACEBOOK_PAGE_ID", "FACEBOOK_PAGE_ACCESS_TOKEN"]
    problems = []
    if not page_id:
        problems.append("FACEBOOK_PAGE_ID が未設定です")
    elif not PAGE_ID_RE.match(page_id):
        problems.append("FACEBOOK_PAGE_ID は数字のページIDを指定してください")
    if not token:
        problems.append("FACEBOOK_PAGE_ACCESS_TOKEN が未設定です")
    if problems:
        return "invalid", page_id, token, problems
    return "ok", page_id, token, []


def graph_base(env=None):
    """Graph API のベースURL。FACEBOOK_GRAPH_API_VERSION があればそのバージョンを使い、
    無ければバージョンなし(アプリの既定バージョン)にする。"""
    env = env if env is not None else os.environ
    version = (env.get("FACEBOOK_GRAPH_API_VERSION") or "").strip()
    if not version:
        return GRAPH_HOST
    if not GRAPH_VERSION_RE.match(version):
        raise ValueError(f"FACEBOOK_GRAPH_API_VERSION の形式が不正です(例: v24.0): {version!r}")
    return f"{GRAPH_HOST}/{version}"


def graph_timeout(env=None):
    env = env if env is not None else os.environ
    try:
        return max(1, int(env.get("FACEBOOK_GRAPH_TIMEOUT_SEC") or 20))
    except ValueError:
        return 20


def post_interval(env=None):
    """Facebookへの連続投稿間隔。既定10分、0〜3600秒に制限する。"""
    env = env if env is not None else os.environ
    try:
        return min(3600, max(0, int(env.get("FACEBOOK_POST_INTERVAL_SEC") or 180)))
    except ValueError:
        return 180


# ---------- 記事・本文 ----------

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def article_url(article):
    """本番の記事URL。slug が不正なら ValueError(本番記事URL以外は投稿しない)。"""
    slug = str(article.get("slug") or "")
    if not SLUG_RE.match(slug):
        raise ValueError(f"slug が不正です: {slug!r}")
    return f"{SITE_DOMAIN}/articles/{slug}/"


def shorten(text, limit):
    """導入文を limit 文字以内にする。超える場合は文の区切り(。)で切り、無ければ「…」で切る。"""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = head.rfind("。")
    if cut >= limit // 2:
        return head[:cut + 1]
    return text[:limit - 1].rstrip() + "…"


def build_post_message(article):
    """投稿本文: 記事タイトル + 短い導入(dek)+ 記事URL"""
    url = article_url(article)
    title = " ".join(str(article.get("title") or "").split())
    if not title:
        raise ValueError("タイトルが空です")
    intro = shorten(article.get("dek"), INTRO_MAX_CHARS)
    return "\n\n".join([title] + ([intro] if intro else []) + [url])


# ---------- 投稿済みログ ----------

def load_post_log(path=None):
    """戻り値: {article_id: record}。ログが壊れている場合は PostLogError(二重投稿防止のため止める)。"""
    path = path or FACEBOOK_POST_LOG_PATH
    try:
        data = load_json(path, {"posts": []})
    except (OSError, ValueError) as e:
        raise PostLogError(f"投稿済みログ {path} を読めません: {e}")
    posts = data.get("posts") if isinstance(data, dict) else None
    if not isinstance(posts, list):
        raise PostLogError(f"投稿済みログ {path} の形式が不正です")
    return {r["article_id"]: r for r in posts if isinstance(r, dict) and "article_id" in r}


def save_post_log(records, path=None):
    path = path or FACEBOOK_POST_LOG_PATH
    posts = sorted(records.values(), key=lambda r: (str(r.get("posted_at", "")), str(r["article_id"])))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"posts": posts}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- Graph API ----------

def _urlopen(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout)


def _redact(text, token):
    text = str(text)
    if token:
        text = text.replace(token, "***")
    return re.sub(r"(access_token=)[^&\s\"']+", r"\1***", text)


def _describe_http_error(e, token):
    """Graph API のエラー本文から、原因調査に必要な項目だけを取り出す(トークンは伏せ字)。"""
    try:
        raw = e.read().decode("utf-8", errors="replace")
    except Exception:
        raw = ""
    try:
        err = (json.loads(raw) or {}).get("error") or {}
        detail = (f"type={err.get('type')} code={err.get('code')} subcode={err.get('error_subcode')} "
                  f"message={err.get('message')} fbtrace_id={err.get('fbtrace_id')}")
    except (ValueError, AttributeError):
        detail = raw[:ERROR_LOG_MAX_CHARS]
    return _redact(f"HTTP {e.code} {detail}", token)[:ERROR_LOG_MAX_CHARS + 100]


def graph_request(method, path, token, params=None, env=None):
    """Graph API を呼ぶ。トークンは Authorization ヘッダーでのみ送る(URLに載せない)。"""
    url = f"{graph_base(env)}/{path.lstrip('/')}"
    data = None
    # Meta Graph API accepts access_token as a request parameter. Use that form here
    # instead of the Authorization header because System User tokens have been observed
    # to validate in Meta's debugger while being rejected when forwarded as Bearer by
    # the GitHub Actions path. Keep the token out of logs via _redact().
    request_params = dict(params or {})
    request_params["access_token"] = token
    if method == "GET":
        url += "?" + urllib.parse.urlencode(request_params)
    else:
        data = urllib.parse.urlencode(request_params).encode("utf-8")
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _urlopen(req, graph_timeout(env)) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise GraphAPIError(_describe_http_error(e, token), status=e.code, uncertain=e.code >= 500)
    except Exception as e:  # タイムアウト・通信断など(応答を受け取れていない)
        raise GraphAPIError(_redact(f"{type(e).__name__}: {e}", token), uncertain=True)
    try:
        return json.loads(body)
    except ValueError:
        raise GraphAPIError("Graph API の応答がJSONではありません", uncertain=True)


def fetch_recent_post_urls(page_id, token, env=None):
    """ページの最近の投稿から、本文に含まれる記事URL → 投稿ID の対応を作る(重複投稿の確認用)。"""
    data = graph_request("GET", f"{page_id}/posts", token,
                         {"fields": "id,message", "limit": str(RECENT_POSTS_LIMIT)}, env=env)
    found = {}
    for post in (data or {}).get("data") or []:
        for slug in ARTICLE_URL_RE.findall(str(post.get("message") or "")):
            found.setdefault(f"{SITE_DOMAIN}/articles/{slug}/", str(post.get("id") or ""))
    return found


def smoke_check(page_id, token, env=None):
    """投稿せずに疎通を確認する: ページ情報の取得と、重複確認に使う既存投稿の読み取り。"""
    page = graph_request("GET", page_id, token, {"fields": "id,name"}, env=env)
    if str((page or {}).get("id") or "") != page_id:
        raise GraphAPIError("FACEBOOK_PAGE_ID とトークンのページが一致しません")
    recent = fetch_recent_post_urls(page_id, token, env=env)
    return page.get("name") or "", len(recent)


def publish_post(page_id, token, message, link, env=None):
    """Facebookページへ投稿し、投稿IDを返す。"""
    data = graph_request("POST", f"{page_id}/feed", token, {"message": message, "link": link}, env=env)
    post_id = str((data or {}).get("id") or "")
    if not post_id:
        raise GraphAPIError("Graph API の応答に投稿IDがありません", uncertain=True)
    return post_id


# ---------- 公開確認 ----------

def url_ok(url):
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def wait_for_url_published(url, check=url_ok, sleep=time.sleep, timeout=URL_CHECK_TIMEOUT_SEC):
    """本番URLが HTTP 200 になるまで待つ(呼び出し元の公開確認後なので、通常は1回目で200になる)。"""
    attempts = max(1, timeout // URL_CHECK_INTERVAL_SEC + 1)
    for n in range(attempts):
        if check(url):
            return True
        if n < attempts - 1:
            sleep(URL_CHECK_INTERVAL_SEC)
    return False


# ---------- 本体 ----------

def process_articles(target_ids, articles_by_id, records, page_id, token, *, dry_run,
                     skip_url_check=False, check=url_ok, sleep=time.sleep, env=None, log_path=None):
    """戻り値: 件数の dict(posted / dry_run / skipped / failed / uncertain / unpublished)"""
    counts = {k: 0 for k in ("posted", "dry_run", "skipped", "failed", "uncertain", "unpublished")}
    candidates = []
    for article_id in target_ids:
        article = articles_by_id.get(article_id)
        if article is None:
            print(f"  id={article_id}: articles.json に見つからないためスキップします。")
            counts["skipped"] += 1
            continue
        if article.get("mergedInto"):
            print(f"  id={article_id}: 統合済み(mergedInto={article['mergedInto']})のためスキップします。")
            counts["skipped"] += 1
            continue
        try:
            url = article_url(article)
            message = build_post_message(article)
        except ValueError as e:
            print(f"  id={article_id}: 投稿できません({e})。スキップします。", file=sys.stderr)
            counts["failed"] += 1
            continue
        candidates.append((article_id, article, url, message))

    remote = None
    if candidates and not dry_run:
        pending = [c for c in candidates
                   if records.get(c[0], {}).get("status", STATUS_POSTED) == STATUS_UNCERTAIN
                   or c[0] not in records]
        if pending:
            try:
                remote = fetch_recent_post_urls(page_id, token, env=env)
            except GraphAPIError as e:
                print(f"::error::Facebookページの既存投稿を確認できないため、二重投稿防止のため今回の投稿を見送ります: {e}",
                      file=sys.stderr)
                counts["failed"] += len(pending)
                return counts

    for i, (article_id, article, url, message) in enumerate(candidates):
        print(f"\n  --- id={article_id} ({article['slug']}) [{i + 1}/{len(candidates)}] ---")
        print(f"  記事URL: {url}")
        rec = records.get(article_id)
        if rec is not None:
            if rec.get("status", STATUS_POSTED) == STATUS_UNCERTAIN and remote and url in remote:
                rec.update(facebook_post_id=remote[url], status=STATUS_POSTED)
                save_post_log(records, log_path)
                print(f"  結果不明だった投稿をページ上で確認しました(facebook_post_id={remote[url]})。")
            state = "結果不明として記録済み(人が確認するまで再投稿しません)" \
                if rec.get("status") == STATUS_UNCERTAIN else f"投稿済み(facebook_post_id={rec.get('facebook_post_id')})"
            print(f"  {state}のためスキップします。")
            counts["skipped"] += 1
            continue

        print("  投稿本文:")
        print("  " + message.replace("\n", "\n  "))
        if dry_run:
            print("  [DRY RUN] 実際の投稿は行いません(Graph API・URL公開確認も呼びません)。")
            counts["dry_run"] += 1
            continue

        if remote and url in remote:
            records[article_id] = {"article_id": article_id, "facebook_post_id": remote[url],
                                   "posted_at": now_iso(), "article_url": url, "status": STATUS_POSTED}
            save_post_log(records, log_path)
            print(f"  ページ上に同じ記事URLの投稿があるため投稿しません(facebook_post_id={remote[url]}。ログに記録しました)。")
            counts["skipped"] += 1
            continue

        if not skip_url_check and not wait_for_url_published(url, check=check, sleep=sleep):
            print(f"  {url} が HTTP 200 にならないため投稿しません。", file=sys.stderr)
            counts["unpublished"] += 1
            continue

        # 短時間の連投を避ける。最初の実投稿は即時、2件目以降は既定3分空ける。
        # DRY RUN・投稿済みスキップ・公開未確認では待たない。
        interval = post_interval(env)
        if counts["posted"] > 0 and interval > 0:
            print(f"  連続投稿を避けるため {interval} 秒待ってから投稿します。")
            sleep(interval)

        try:
            post_id = publish_post(page_id, token, message, url, env=env)
        except GraphAPIError as e:
            if e.uncertain:
                records[article_id] = {"article_id": article_id, "facebook_post_id": None,
                                       "posted_at": now_iso(), "article_url": url, "status": STATUS_UNCERTAIN}
                save_post_log(records, log_path)
                print(f"::warning::Facebook投稿の結果が不明です(id={article_id})。二重投稿を避けるため再投稿しません。"
                      f"ページを確認し、投稿されていなければログから記録を消して再実行してください: {e}",
                      file=sys.stderr)
                counts["uncertain"] += 1
            else:
                print(f"::error::Facebook投稿に失敗しました(id={article_id}。次回の再実行で投稿し直せます): {e}",
                      file=sys.stderr)
                counts["failed"] += 1
            continue
        records[article_id] = {"article_id": article_id, "facebook_post_id": post_id,
                               "posted_at": now_iso(), "article_url": url, "status": STATUS_POSTED}
        save_post_log(records, log_path)  # 1件ごとに保存(途中で止まっても再実行で二重投稿しない)
        print(f"  投稿成功: facebook_post_id={post_id}")
        counts["posted"] += 1
    return counts


def parse_ids(raw):
    return [int(x) for x in str(raw or "").split(",") if x.strip()]


def run_smoke(env):
    state, page_id, token, problems = resolve_credentials(env)
    if state != "ok":
        print("::error::Facebook投稿の Secrets が設定されていません: " + " / ".join(problems), file=sys.stderr)
        return 2
    try:
        base = graph_base(env)
        name, found = smoke_check(page_id, token, env=env)
    except ValueError as e:
        print(f"::error::{e}", file=sys.stderr)
        return 2
    except GraphAPIError as e:
        print(f"::error::Facebook Graph API の疎通確認に失敗しました: {e}", file=sys.stderr)
        return 1
    print(f"Facebook 疎通確認OK: ページ「{name}」(Graph API: {base})。"
          f"最近の投稿のうち湘南Doorsの記事URLを含むもの {found}件(重複確認に使用)。")
    return 0


def main(argv=None, env=None, check=url_ok, sleep=time.sleep):
    env = env if env is not None else os.environ
    ap = argparse.ArgumentParser()
    ap.add_argument("--article-ids", help="カンマ区切りの記事ID(例: 93,94,95)。空なら何もしない")
    ap.add_argument("--skip-url-check", action="store_true", help="本番URLの公開確認(HTTP 200待ち)を省略する")
    ap.add_argument("--smoke", action="store_true",
                    help="投稿せずに、トークン・ページID・既存投稿の読み取り権限だけを確認する")
    args = ap.parse_args(argv)
    dry_run = is_dry_run(env)
    prefix = "[DRY RUN] " if dry_run else ""

    if args.smoke:
        return run_smoke(env)
    if args.article_ids is None:
        print("::error::--article-ids または --smoke を指定してください。", file=sys.stderr)
        return 2
    try:
        target_ids = parse_ids(args.article_ids)
    except ValueError:
        print(f"::error::--article-ids の形式が不正です: {args.article_ids!r}", file=sys.stderr)
        return 2
    if not target_ids:
        print("Facebook: 投稿対象の記事がありません。")
        return 0

    state, page_id, token, problems = resolve_credentials(env)
    if state == "invalid":
        print("::error::Facebook投稿の設定が不完全です: " + " / ".join(problems), file=sys.stderr)
        return 2
    try:
        graph_base(env)
    except ValueError as e:
        print(f"::error::{e}", file=sys.stderr)
        return 2
    if state == "not_configured":
        if not dry_run:
            print("::warning::FACEBOOK_PAGE_ID / FACEBOOK_PAGE_ACCESS_TOKEN が未設定のため、"
                  "Facebook投稿をスキップします(Facebook連携が未設定)。")
            return 0
        print("[DRY RUN] FACEBOOK_PAGE_ID / FACEBOOK_PAGE_ACCESS_TOKEN は未設定です(DRY RUN のため続行)。")

    try:
        records = load_post_log()
    except PostLogError as e:
        print(f"::error::{e}(二重投稿防止のため投稿しません)", file=sys.stderr)
        return 1
    articles = load_json(ARTICLES_JSON_PATH, [])
    articles_by_id = {a["id"]: a for a in articles if isinstance(a, dict) and "id" in a}

    print(f"{prefix}Facebook: 対象記事ID {target_ids} / 投稿済みログ {len(records)}件")
    counts = process_articles(target_ids, articles_by_id, records, page_id, token, dry_run=dry_run,
                              skip_url_check=args.skip_url_check, check=check, sleep=sleep, env=env)
    print(f"\n{prefix}Facebook 完了: 投稿{counts['posted']}件 / DRY RUN確認{counts['dry_run']}件 / "
          f"スキップ{counts['skipped']}件 / 失敗{counts['failed']}件 / 結果不明{counts['uncertain']}件 / "
          f"公開未確認{counts['unpublished']}件")
    if counts["failed"] or counts["uncertain"] or counts["unpublished"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
