# Claude API cost analysis (Issue #260)

## 2026-11-02 recovery and cost controls

- The failed run [41248162466](https://github.com/sysywq/shonan-doors/actions/runs/41248162466) returned Anthropic `invalid_request_error`: specified API usage limits reached; access resumes at `2026-11-01 00:00 UTC` (09:00 JST). This is an API billing limit, not evidence of a Claude subscription limit.
- [Daily PR #367](https://github.com/sysywq/shonan-doors/pull/367) merged on November 2 JST with three full-audit-confirmed articles (251, 252, 253). Run [41358699424](https://github.com/sysywq/shonan-doors/actions/runs/41358699424) verified all three in production. No duplicate generation is needed to resume today.
- The archive currently contains 205 October calls, from October 11 through October 14 JST, estimated token cost **$206.914983**. This is a partial historical sample, not the October bill. News generation accounts for $143.602050 (69.4%), stock selection $33.017997, fact audit $28.877517, and stock-topic refill $1.417419.
- The November 2 run has 18 archived calls and $11.529696 in estimated token cost for three published articles. Candidate discovery/scoring, verify, Claude Code tasks, and search fees are absent from the historical token totals. Do not extrapolate a complete monthly bill from these figures.
- All 223 currently archived responses report zero cache-read and cache-write tokens. Automatic five-minute prompt caching is now requested through `metered_create`, allowing repeated search/continuation context to be reused. Actual savings require new production usage; first writes cost more than ordinary input and cache hits are not guaranteed.
- Generation and candidate discovery now specify `max_uses: 8` for each web-search tool request, matching the existing bounded-audit design. This is a per-request limit, not a daily dollar cap. Existing continuation/attempt limits, primary-source policy, full audit and minimum-three publication checks remain in place. If evidence is insufficient, the quality gates still reject/hold the article.
- Candidate discovery/scoring, verify and growth title/dek drafting now use the existing meter, without extra API calls. Growth draft records still require workflow-level persistence outside Daily Articles; historical missing costs cannot be recovered by this change.
- Issue-triggered Claude Code has a 40-turn limit, a $5 estimated budget per invocation, and a 45-minute job timeout. This budget covers that Claude Code session, not the Python article pipeline or Python API calls launched by it. The CLI checks cost after responses, so this is not an exact invoice ceiling. A capped task can finish incomplete.
- No Anthropic account spending limit or billing setting is changed. Model and editorial rules are preserved. A monthly/daily pipeline budget needs an owner-selected amount and durable accounting across runs, including tool fees.

Official references: [prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching), [web search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool), [Claude Code CLI limits](https://code.claude.com/docs/en/cli-reference).

This is a **read-only, on-demand** workflow. No dashboard, daily report, or extra Claude API request is required.

## Retrieve data

Usage logs are written by Daily Articles to the dedicated `bot/claude-api-usage-archive` branch at `data/claude-cost/usage.jsonl` (once an actual run has created it). Published article metadata lives on `main` at `data/articles.json`.

```bash
git fetch origin main bot/claude-api-usage-archive
git show origin/bot/claude-api-usage-archive:data/claude-cost/usage.jsonl > /tmp/shonan-usage.jsonl
python scripts/report_claude_costs.py --usage /tmp/shonan-usage.jsonl --articles data/articles.json --month 2026-10
python scripts/report_claude_costs.py --usage /tmp/shonan-usage.jsonl --articles data/articles.json --date 2026-10-11
```

If the archive branch is absent or the JSONL is empty, **report data unavailable**. Do not substitute $0 or infer successful metering. To query through ChatGPT, read both GitHub files via the connected GitHub tools and run the equivalent grouping. The script makes no network requests.

## Interpretation

- `estimated_total_usd`: estimated cost of logged, priced calls in the selected period; not an Anthropic invoice.
- `cost_by_day_stage_model`: per-stage and per-model token and USD breakdown.
- `daily_publication_cohorts`: `articles_in_main` from `data/articles.json`, including article titles, URLs and direct attributed costs.
- `estimated_usd_per_article_in_main`: **total logged daily pipeline cost / count of articles dated that day in main**. This includes shared costs; it is a cohort-level KPI, not a precise per-article charge.
- `unattributed_estimated_usd`: calls lacking an article ID; this is **not** rejected/wasted cost.
- `unknown_price_calls`: costs excluded from USD totals because no model pricing is configured.
- An article being in `main` is **not** proof that the production URL returns HTTP 200. Live publication verification is a separate operation.
- Rejected/held article attribution requires durable gate outcomes and per-article IDs on metered calls; it is **not yet available** from this dataset. Never present invented rejected-cost totals.
- Current logging only covers instrumented Anthropic calls, not other providers, infrastructure or historical invoices.

## Verification after first live run

Check `bot/claude-api-usage-archive` exists and has new records, that call IDs are unique, timestamps and stage names are present, and that records match the day's API calls. If not, inspect the `Claude API usageを永続保存` step; it is best-effort and cannot block publishing.
