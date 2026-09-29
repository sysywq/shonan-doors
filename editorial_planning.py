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
            "searchIntent": {"type": "string"}},
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


def _tool_call(client, system, prompt, tool, with_search=False, turns=8):
    available = ([{"type": "web_search_20250305", "name": "web_search"}] if with_search else []) + [tool]
    messages = [{"role": "user", "content": prompt}]
    for _ in range(turns):
        response = client.messages.create(model="claude-sonnet-4-6", max_tokens=5000,
                                          system=system, tools=available, messages=messages)
        for block in response.content:
            if block.type == "tool_use" and block.name == tool["name"]:
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


def judge(client, candidates, existing):
    compact = [{k: c.get(k, "") for k in ("id", "query", "titleIdea", "area", "cat", "articleType", "searchIntent", "sourceUrl")}
               for c in candidates]
    system = ("候補を独立に評価する編集者。demand 0-25（検索意図の明確さと需要仮説）、"
              "timing 0-20（今出す意味）、usefulness 0-20（湘南読者への具体的な有用性）、"
              "originality 0-15（独自性と既存記事・競合との重複回避）を採点。"
              "GSC/GA4の実績と探索価値は別のプログラムが採点するので加算しない。"
              "根拠のない検索ボリュームを作らない。既存記事と同一対象・同一検索意図の候補は"
              "originalityを0点とする。")
    import json
    existing_digest = [{"id": a.get("id"), "title": a.get("title"),
                        "area": a.get("area"), "cat": a.get("cat"),
                        "subjects": (a.get("subjectNames") or [])[:4]}
                       for a in existing[-150:]]
    result = _tool_call(client, system, "全候補を採点しsubmit_editorial_scoresで提出。既存記事: "
                        + json.dumps(existing_digest, ensure_ascii=False)
                        + " 候補: " + json.dumps(compact, ensure_ascii=False), SCORE_TOOL, turns=3)
    scores = result.get("scores", [])
    if not isinstance(scores, list):
        raise ValueError("invalid score response")
    allowed = {c["id"] for c in candidates}
    result = {x["id"]: x for x in scores if isinstance(x, dict) and x.get("id") in allowed}
    if set(result) != allowed:
        raise ValueError("incomplete editorial scores")
    return result
