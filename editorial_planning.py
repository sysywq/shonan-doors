"""Bounded topic discovery and editorial judgment for daily article planning."""
import re
import unicodedata


def key(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def candidate_pool(discovered, stock_topics, existing, max_pool=30):
    """Deduplicate metadata before paying for article drafts or Fact Audit."""
    existing_terms = {key(v) for a in existing for v in [a.get("title"), *(a.get("subjectNames") or [])] if v}
    seen, result = set(), []
    for item in discovered + stock_topics:
        query = key(item.get("query"))
        subject = key(item.get("subject"))
        if not query or query in seen or (subject and subject in existing_terms):
            continue
        if item.get("articleType") not in ("news", "stock"):
            continue
        if not item.get("id") or not item.get("area") or not item.get("cat"):
            continue
        seen.add(query)
        result.append(item)
        if len(result) == max_pool:
            break
    return result


DISCOVERY_TOOL = {
    "name": "submit_topic_candidates", "description": "調査した湘南の新規記事テーマ候補を提出",
    "input_schema": {"type": "object", "properties": {"candidates": {"type": "array", "items": {
        "type": "object", "properties": {
            "query": {"type": "string"}, "titleIdea": {"type": "string"},
            "area": {"type": "string"}, "cat": {"type": "string"},
            "subject": {"type": "string"}, "sourceUrl": {"type": "string"},
            "searchIntent": {"type": "string"},
            "newsKind": {"type": "string", "enum": ["store_opening", "event", "news"],
                         "description": "新店・開業=store_opening、イベント=event、それ以外=news"},
            "announcementDate": {"type": "string", "description": "公式発表日 YYYY-MM-DD。不明なら空文字"},
            "openingDate": {"type": "string", "description": "店舗・施設の開店日 YYYY-MM-DD。不明なら空文字"},
            "eventStartDate": {"type": "string", "description": "イベント開催初日 YYYY-MM-DD。不明なら空文字"},
            "eventEndDate": {"type": "string", "description": "イベント最終日 YYYY-MM-DD。単日・不明なら空文字"}},
        "required": ["query", "titleIdea", "area", "cat", "subject", "sourceUrl", "searchIntent"]}}},
        "required": ["candidates"]}
}

SCORE_TOOL = {
    "name": "submit_editorial_scores", "description": "候補ごとの編集評価を提出",
    "input_schema": {"type": "object", "properties": {"scores": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "string"},
            "demand": {"type": "number"}, "timing": {"type": "number"},
            "usefulness": {"type": "number"}, "originality": {"type": "number"}},
        "required": ["id", "demand", "timing", "usefulness", "originality"]}}},
        "required": ["scores"]}
}


def _tool_call(client, system, prompt, tool, with_search=False, turns=8, meta=None):
    available = ([{"type": "web_search_20250305", "name": "web_search"}] if with_search else []) + [tool]
    messages = [{"role": "user", "content": prompt}]
    for _ in range(turns):
        response = client.messages.create(model="claude-sonnet-4-6", max_tokens=5000,
                                          system=system, tools=available, messages=messages)
        for block in response.content:
            if block.type == "tool_use" and block.name == tool["name"]:
                if meta is not None:
                    # max_tokens で打ち切られた tool_use は配列が途中で切れていることがある
                    meta["stop_reason"] = getattr(response, "stop_reason", None)
                return block.input
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "pause_turn":
            messages.append({"role": "user", "content": f"調査を終えて{tool['name']}で提出してください。"})
    raise RuntimeError(f"{tool['name']} was not called")


def discover(client, areas, cats, existing, hints, count=30):
    digest = "\n".join(f"- {a.get('title','')} / {','.join(a.get('subjectNames') or [])}" for a in existing[-150:])
    system = ("湘南Doorsの編集者。湘南8エリアのニュース、イベント、新店、グルメ、観光、暮らし、"
              "子育て、高単価領域などを横断して新規テーマを調査する。候補は記事本文ではない。"
              "直近2週間の発表か今後の確定イベントを優先し、公式一次情報のURLを確認する。"
              "新店は発表日ではなく開店日で鮮度を判断する(開店から15日以上たった店舗、終了済みのイベントは候補にしない。"
              "これから開店・開催するものは発表が1か月以上前でもよい)。日付は一次情報で確認できたものだけを入れ、推測しない。"
              "Google Trendsは参照可能な時だけ季節性の補助情報として使い、数値を推測しない。"
              "他媒体は事実の根拠にしない。既存と同一対象・検索意図は候補にしない。"
              f"エリア: {areas}。カテゴリ: {cats}。既存記事:\n{digest}")
    result = _tool_call(client, system, f"異なる地域・検索意図から最大{count}件の候補を広く調査。候補段階では本文を書かず、メタデータだけを提出。"
                        f"GSCの参考語句: {hints[:5]}。すべてsubmit_topic_candidatesで提出。",
                        DISCOVERY_TOOL, with_search=True)
    raw = result.get("candidates", [])
    if not isinstance(raw, list) or len(raw) > count or any(not isinstance(x, dict) for x in raw):
        raise ValueError("invalid discovery response")
    return [{"id": f"news-{i:02d}", "articleType": "news", **x}
            for i, x in enumerate(raw) if x.get("area") in areas and x.get("cat") in cats
            and x.get("query") and x.get("sourceUrl")]


class EditorialScoreError(ValueError):
    """採点応答が使えない(構造異常・有効な採点が1件も無い)。API障害(anthropic の例外)とは別系統。
    diagnostics に、機密を含まない診断情報(件数・不正な候補と理由・値の抜粋)を持つ。"""

    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def _excerpt(raw):
    """診断ログ用の抜粋。採点4項目とidだけを、短い repr で残す(候補本文・URL等は出さない)。"""
    if not isinstance(raw, dict):
        return repr(raw)[:80]
    return {k: repr(raw.get(k))[:40] for k in ("id", "demand", "timing", "usefulness", "originality") if k in raw}


def _score_entries(result):
    """submit_editorial_scores の入力から採点の配列を取り出す。戻り値 (entries, note)。
    配列が JSON 文字列で返る・id をキーにした dict で返る、という既知の崩れ方だけを復元し、
    それ以外の構造は EditorialScoreError にする。"""
    import json
    if not isinstance(result, dict):
        raise EditorialScoreError(f"採点応答がオブジェクトではない(型={type(result).__name__})")
    if "scores" not in result:
        raise EditorialScoreError(f"採点応答に scores が無い(キー={sorted(result)[:10]})")
    scores = result["scores"]
    if isinstance(scores, str):
        try:
            scores = json.loads(scores)
        except ValueError:
            raise EditorialScoreError(f"scores が配列ではなく、JSONとして読めない文字列(長さ{len(result['scores'])})")
        if not isinstance(scores, list):
            raise EditorialScoreError(f"scores の文字列を読んだ結果が配列ではない(型={type(scores).__name__})")
        return scores, "scores が配列ではなくJSON文字列で返されたため、配列として読み直した"
    if isinstance(scores, dict) and scores and all(isinstance(v, dict) for v in scores.values()):
        return ([dict(v, id=v.get("id", k)) for k, v in scores.items()],
                "scores が配列ではなく id をキーにしたオブジェクトで返されたため、配列に直した")
    if not isinstance(scores, list):
        raise EditorialScoreError(f"scores が配列ではない(型={type(scores).__name__})")
    return scores, None


def _collect_scores(result, alias_to_id, diag):
    """採点応答を候補ごとに検証する。有効な採点 {候補id: judgment} を返し、問題は diag に記録する。"""
    from editorial_selection import validate_judgment

    entries, note = _score_entries(result)
    if note:
        diag["notes"].append(note)
    by_alias = {}
    for raw in entries:
        alias = raw.get("id") if isinstance(raw, dict) else None
        m = re.fullmatch(r"\s*c?0*(\d{1,3})\s*", str(alias)) if isinstance(alias, (str, int)) else None
        if m and not isinstance(alias, bool) and f"c{int(m.group(1)):02d}" in alias_to_id:
            if alias != f"c{int(m.group(1)):02d}":
                diag["notes"].append(f"id {alias!r} を c{int(m.group(1)):02d} として読んだ")
            alias = f"c{int(m.group(1)):02d}"
        if not isinstance(alias, str) or alias not in alias_to_id:
            diag["unknown"].append(_excerpt(raw))
            continue
        by_alias.setdefault(alias, []).append(raw)
    valid = {}
    for alias, raws in by_alias.items():
        cid = alias_to_id[alias]
        checked = [validate_judgment(r) for r in raws]
        if len(raws) > 1:
            distinct = {tuple(sorted(j.items())) for j, _p, _n in checked if j}
            if len(distinct) > 1 or any(j is None for j, _p, _n in checked):
                diag["invalid"][cid] = {"problems": [f"同じidの採点が{len(raws)}件あり内容が食い違う"],
                                        "raw": [_excerpt(r) for r in raws]}
                continue
            diag["notes"].append(f"{cid}: 同じ内容の採点が{len(raws)}件重複していたため1件として扱った")
        judgment, problems, notes = checked[0]
        diag["notes"] += [f"{cid}: {n}" for n in notes]
        if problems:
            diag["invalid"][cid] = {"problems": problems, "raw": _excerpt(raws[0])}
            continue
        diag["invalid"].pop(cid, None)
        valid[cid] = judgment
    return valid


def judge(client, candidates, existing, diagnostics=None):
    """候補を採点し {候補id: judgment} を返す。
    候補の id は Places / X 由来の長い文字列のことがあり、モデルが写し間違えると採点が失われる。
    そのため採点には短い連番(c01…)を使い、戻すときに元の id に対応付ける。
    不正・欠落した候補は1回だけ再採点し、それでも不正な候補だけを除外する(0点扱いにはしない)。
    有効な採点が1件も無いとき・応答の構造が壊れているときだけ EditorialScoreError を送出する。
    diagnostics(dict)を渡すと、件数・除外した候補と理由・値の抜粋(機密を含まない)を書き込む。"""
    import json
    diag = diagnostics if diagnostics is not None else {}
    diag.update(candidates=len(candidates), valid=0, invalid={}, missing=[], unknown=[], notes=[],
                stop_reason=None, retried=False)
    ids = [c["id"] for c in candidates]
    if len(set(ids)) != len(ids):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        raise EditorialScoreError(f"候補idが重複している: {dup[:5]}", diag)
    aliases = {f"c{i + 1:02d}": c["id"] for i, c in enumerate(candidates)}
    alias_of = {cid: alias for alias, cid in aliases.items()}
    system, existing_digest = _judge_context(existing)

    def ask(targets):
        compact = [dict({k: c.get(k, "") for k in _JUDGE_FIELDS}, id=alias_of[c["id"]]) for c in targets]
        meta = {}
        result = _tool_call(client, system, "全候補を採点しsubmit_editorial_scoresで提出。"
                            "各scoreのidには候補のid(c01のような短いid)をそのまま使い、全候補を1件ずつ採点する。"
                            "値はJSONの数値で、範囲内に収める。既存記事: "
                            + json.dumps(existing_digest, ensure_ascii=False)
                            + " 候補: " + json.dumps(compact, ensure_ascii=False), SCORE_TOOL, turns=3, meta=meta)
        diag["stop_reason"] = meta.get("stop_reason")
        return result

    valid = _collect_scores(ask(candidates), aliases, diag)
    pending = [c for c in candidates if c["id"] not in valid]
    if pending:
        diag["retried"] = True
        diag["notes"].append(f"採点が欠落・不正な{len(pending)}件だけを1回再採点した"
                             f"(初回 stop_reason={diag['stop_reason']})")
        try:
            valid.update(_collect_scores(ask(pending), {alias_of[c["id"]]: c["id"] for c in pending}, diag))
        except EditorialScoreError as exc:
            diag["notes"].append(f"再採点の応答も使えなかった: {exc}")
    diag["missing"] = [c["id"] for c in candidates if c["id"] not in valid and c["id"] not in diag["invalid"]]
    for cid in valid:
        diag["invalid"].pop(cid, None)
    diag["valid"] = len(valid)
    if not valid:
        raise EditorialScoreError(f"有効な採点が0件(候補{len(candidates)}件・不正{len(diag['invalid'])}件・"
                                  f"欠落{len(diag['missing'])}件)", diag)
    return valid


_JUDGE_FIELDS = ("id", "query", "titleIdea", "area", "cat", "articleType",
                 "searchIntent", "sourceUrl", "leadUrl", "leadSourceType", "openingDate",
                 "announcementDate", "eventStartDate", "eventEndDate",
                 "leadAuthor", "leadCreatedAt", "leadEngagement")


def _judge_context(existing):
    system = ("候補を独立に評価する編集者。demand 0-25（検索意図の明確さと需要仮説）、"
              "timing 0-20（今出す意味）、usefulness 0-20（湘南読者への具体的な有用性）、"
              "originality 0-15（独自性と既存記事・競合との重複回避）を採点。"
              "GSC/GA4の実績と探索価値は別のプログラムが採点するので加算しない。"
              "根拠のない検索ボリュームを作らない。Google Places等のleadSourceType付き候補は"
              "一次情報ではなく発見シグナルとしてのみ扱う。FUTURE_OPENINGやopeningDate、"
              "XのleadCreatedAtやleadEngagementはtiming/需要仮説の補助材料にしてよいが、"
              "Xの投稿内容自体を事実認定には使わない。既存記事と同一対象・同一検索意図の候補は"
              "originalityを0点とする。ニュースは発表日ではなく開店日・開催日で鮮度を見る。"
              "開店から15日以上たった店舗や終了済みのイベントはtimingを0点とする"
              "(鮮度はプログラムでも判定する)。")
    # 既存記事の id は数値で候補の id と紛れやすいので渡さない(題名・対象名で重複を判断する)
    existing_digest = [{"title": a.get("title"), "area": a.get("area"), "cat": a.get("cat"),
                        "subjects": (a.get("subjectNames") or [])[:4]}
                       for a in existing[-150:]]
    return system, existing_digest
