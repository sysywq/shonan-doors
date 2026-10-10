# Claude API cost analysis (Issue #260)

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
