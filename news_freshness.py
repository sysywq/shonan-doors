"""ニュース記事の鮮度ゲート。発表日・開店日・開催日を分けてコードで判定する。

2026-10-09 に「9月16日オープン」の新店が23日後に新店ニュースとして公開された。鮮度ルールが
プロンプトにしか無かったためで、ここでは生成されたドラフト(と企画候補)を日付だけで機械的に判定する。

- 新店(store_opening): 開店日(openingDate)基準。未来の開店は、公式発表がどれだけ前でも対象にする。
  開店済みは 0〜7日なら通常、8〜14日は freshnessException(高い読者価値の理由)がある時だけ、
  15日以上は新店ニュースの対象外(過去の開店を今日の速報として扱わない)。
- イベント(event / cat='e'): 開催日(eventStartDate / eventEndDate)基準。終了済みは不可、
  開催中・開催前は対象(開催までの日数は判定理由に残す)。発表日の古さでは排除しない。
- その他のニュース(news): 実施日(effectiveDate)が未来なら対象。過去なら実施日、無ければ発表日
  (announcementDate)基準で 0〜7日通常・8〜14日は例外理由がある時だけ・15日以上は対象外。
- 判定に必要な日付が一次情報で確認できていない(空・形式不正)ときは、推測で補完せず対象外にする。
- ストックSEO記事(エバーグリーン)はこのゲートの対象外(呼び出し元がニュースだけに適用する)。
- today は JST の日付(YYYY-MM-DD)。年跨ぎは日付の差分で扱う。
"""
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
KINDS = ("store_opening", "event", "news")
NORMAL_DAYS = 7         # 開店・発表から何日までを通常のニュースとして扱うか
EXCEPTION_DAYS = 14     # これ以下なら freshnessException(高い読者価値の理由)がある時だけ許容
MAX_FUTURE_DAYS = 365   # これより先の日付は年の取り違えを疑って対象外にする

# タイトル・リードに新店・開業を示す語があれば、newsKind の申告に関係なく開店日基準で判定する
OPENING_RE = re.compile(r"新店|出店|開店|開業|グランドオープン|リニューアルオープン"
                        r"|(?:に|で|が|を|へ|、|・|\d日)オープン|オープン(?:予定|へ|します|しました|した|！|!|$)")
# 「9月16日オープン」「10月9日グランドオープン」のような、タイトル・リード内の開店日
TITLE_OPENING_DATE_RE = re.compile(
    r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日[^。、\s]{0,8}?(?:グランド|リニューアル)?(?:オープン|開店|開業)")


def today_jst(now=None):
    """JST の今日(YYYY-MM-DD)。now は timezone 付き datetime(UTC で動く Actions でも JST で判定する)。"""
    now = now or datetime.now(JST)
    if now.tzinfo is None:
        raise ValueError("timezone の無い datetime では JST の日付を決められません")
    return now.astimezone(JST).date().isoformat()


def parse_date(value):
    """YYYY-MM-DD だけを日付として受け付ける。それ以外(空・和暦・日付なし)は None(補完しない)。"""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def classify(item):
    """event / store_opening / news のどれで判定するか。cat='e' は常にイベント。"""
    if item.get("cat") == "e" or item.get("newsKind") == "event":
        return "event"
    text = f"{item.get('title') or item.get('titleIdea') or ''} {item.get('dek') or ''}".strip()
    if item.get("newsKind") == "store_opening" or OPENING_RE.search(text):
        return "store_opening"
    return "news"


def _result(ok, kind, reason, days=None, basis="", exception=""):
    return {"ok": ok, "kind": kind, "reason": reason, "days": days, "basis": basis, "exception": exception}


def _bad_format(item, key):
    value = item.get(key)
    return bool(isinstance(value, str) and value.strip() and parse_date(value) is None)


def _within_recent(kind, base, label, today, item):
    """過去の日付 base(開店日・実施日・発表日)からの経過日数で判定する。"""
    days = (today - base).days
    exception = str(item.get("freshnessException") or "").strip()
    basis = f"{label}{base.isoformat()}"
    if days <= NORMAL_DAYS:
        return _result(True, kind, f"{label}から{days}日", days, basis)
    if days <= EXCEPTION_DAYS:
        if exception:
            return _result(True, kind, f"{label}から{days}日(例外: {exception[:80]})", days, basis, exception)
        return _result(False, kind, f"{label}から{days}日経過。8〜14日は高い読者価値の理由(freshnessException)が"
                                    "ある場合だけ例外的に扱う", days, basis)
    return _result(False, kind, f"{label}から{days}日経過。15日以上前の出来事は今日のニュースとして扱わない", days, basis)


def check(item, today):
    """ドラフト1件の鮮度を判定する。戻り値 dict(ok, kind, reason, days, basis, exception)。
    days は「基準日からの経過日数」(未来なら負の値 = あと何日)。"""
    today_d = parse_date(today) if isinstance(today, str) else today
    if today_d is None:
        raise ValueError(f"today は YYYY-MM-DD で渡してください: {today!r}")
    kind = classify(item)
    for key in ("announcementDate", "openingDate", "effectiveDate", "eventStartDate", "eventEndDate"):
        if _bad_format(item, key):
            return _result(False, kind, f"{key} の形式が不正({item.get(key)})。日付を推測で補完しない")
    announced = parse_date(item.get("announcementDate"))
    if announced and announced > today_d:
        return _result(False, kind, f"発表日 {announced.isoformat()} が今日より後(日付の誤り)")

    if kind == "event":
        start = parse_date(item.get("eventStartDate"))
        end = parse_date(item.get("eventEndDate")) or start
        if start is None:
            return _result(False, kind, "開催日(eventStartDate)が一次情報で確認できていない")
        if end < start:
            return _result(False, kind, f"終了日 {end.isoformat()} が開始日 {start.isoformat()} より前")
        if end < today_d:
            return _result(False, kind, f"{end.isoformat()} に終了済みのイベント(終了後の告知はしない)",
                           (today_d - end).days, f"終了日{end.isoformat()}")
        until = (start - today_d).days
        if until > MAX_FUTURE_DAYS:
            return _result(False, kind, f"開催日 {start.isoformat()} が{until}日後(年の取り違えの疑い)", -until)
        if until > 0:
            return _result(True, kind, f"開催まであと{until}日", -until, f"開催日{start.isoformat()}")
        return _result(True, kind, f"開催中(終了まであと{(end - today_d).days}日)", -until,
                       f"開催期間{start.isoformat()}〜{end.isoformat()}")

    if kind == "store_opening":
        opened = parse_date(item.get("openingDate"))
        if opened is None:
            return _result(False, kind, "開店日(openingDate)が一次情報で確認できていない。推測で補完しない")
        text = f"{item.get('title') or item.get('titleIdea') or ''} {item.get('dek') or ''}"
        m = TITLE_OPENING_DATE_RE.search(text)
        if m and ((m.group(1) and int(m.group(1)) != opened.year)
                  or (int(m.group(2)), int(m.group(3))) != (opened.month, opened.day)):
            return _result(False, kind, f"タイトル・リードの開店日「{m.group(0)}」と openingDate {opened.isoformat()} が一致しない")
        until = (opened - today_d).days
        if until > MAX_FUTURE_DAYS:
            return _result(False, kind, f"開店日 {opened.isoformat()} が{until}日後(年の取り違えの疑い)", -until)
        if until > 0:
            return _result(True, kind, f"開店まであと{until}日", -until, f"開店日{opened.isoformat()}")
        return _within_recent(kind, opened, "開店", today_d, item)

    effective = parse_date(item.get("effectiveDate"))
    if effective and effective >= today_d:
        until = (effective - today_d).days
        if until > MAX_FUTURE_DAYS:
            return _result(False, kind, f"実施日 {effective.isoformat()} が{until}日後(年の取り違えの疑い)", -until)
        return _result(True, kind, f"実施まであと{until}日", -until, f"実施日{effective.isoformat()}")
    if effective:
        return _within_recent(kind, effective, "実施", today_d, item)
    if announced:
        return _within_recent(kind, announced, "発表", today_d, item)
    return _result(False, kind, "発表日・実施日(announcementDate / effectiveDate)が一次情報で確認できていない")


def candidate_is_stale(candidate, today):
    """企画候補(執筆前のメタデータ)で、日付が分かっていて明らかに鮮度切れの時だけ理由を返す。
    日付が無い候補はここでは落とさない(執筆後のドラフトを check() で必ず判定する)。"""
    if candidate.get("articleType") != "news":
        return None
    probe = {k: candidate.get(k) for k in ("cat", "newsKind", "titleIdea", "dek", "openingDate",
                                           "announcementDate", "effectiveDate", "eventStartDate", "eventEndDate")}
    kind = classify(probe)
    dates = {"event": ("eventStartDate", "eventEndDate"), "store_opening": ("openingDate",),
             "news": ("effectiveDate", "announcementDate")}[kind]
    if not any(parse_date(candidate.get(k)) for k in dates):
        return None
    result = check(probe, today)
    if result["ok"] or result["days"] is None:
        return None
    if kind != "event" and 0 <= result["days"] <= EXCEPTION_DAYS:
        return None  # 8〜14日は執筆時に例外理由が付くかもしれないので、ドラフトで判定する
    return result["reason"]
