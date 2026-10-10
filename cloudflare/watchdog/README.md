# Cloudflare external watchdog

Cloudflare Cron runs daily at **06:15 JST** (21:15 UTC). It checks the GitHub Actions Daily Articles runs for the current JST date. If no run exists, it dispatches the workflow once. Existing GitHub Daily Publication Watchdog handles failures, insufficient article count and deploy lag.

## Deploy (owner action required)

1. Set up a Cloudflare Workers account and authenticate with Wrangler.
2. From `cloudflare/watchdog`, run `npx wrangler secret put GITHUB_TOKEN`. Use a **fine-grained GitHub token** scoped only to `sysywq/shonan-doors`, with Actions **Read and write** permission. Do not commit tokens.
3. Set `GITHUB_REPO = "sysywq/shonan-doors"` as a Worker environment variable.
4. Run `npx wrangler deploy` and verify the scheduled trigger is enabled in Cloudflare.
5. Check Workers Logs after the first 06:15 JST invocation.

Safety: this worker dispatches only if **no** Daily Articles run exists for the JST date. It never calls Claude directly, never retries a failed run, and does not circumvent GitHub's publication quality gates or daily-state stop controls. A race with the existing GitHub watchdog remains theoretically possible; GitHub workflow concurrency queues overlapping runs. The Worker does not guarantee three articles published, and it is not a substitute for the existing GitHub publication watchdog.

Cloudflare Worker deployment and the GitHub token cannot be verified from the repository alone.
