# Cloudflare external watchdog

Cloudflare Cron checks every 15 minutes from **04:07 to 21:52 JST**. The 04:07 check waits for the GitHub Actions schedule at 04:13; from 04:22 onward, an incomplete publication triggers the GitHub Daily Publication Watchdog. That workflow enforces active-run checks, cooldowns, daily limits, article count and production deployment checks.

## Deploy (owner action required)

1. Set up a Cloudflare Workers account and authenticate with Wrangler.
2. From `cloudflare/watchdog`, run `npx wrangler secret put GITHUB_TOKEN`. Use a **fine-grained GitHub token** scoped only to `sysywq/shonan-doors`, with Actions **Read and write** permission. Do not commit tokens.
3. Set `GITHUB_REPO = "sysywq/shonan-doors"` as a Worker environment variable.
4. Run `npx wrangler deploy` and verify the scheduled trigger is enabled in Cloudflare.
5. Check Workers Logs after the first 06:15 JST invocation.

Safety: this worker never calls Claude directly or bypasses GitHub's publication quality gates and daily-state stop controls. It only wakes the existing GitHub watchdog when fewer than three same-day articles are live; that workflow checks active runs and cooldowns before dispatching recovery. GitHub workflow concurrency queues any overlapping run. The Worker does not guarantee three articles will publish.

Cloudflare Worker deployment and the GitHub token cannot be verified from the repository alone.
