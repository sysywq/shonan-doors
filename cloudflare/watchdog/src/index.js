// Independent Cloudflare Cron watchdog for Shonan Doors.
// Intentionally dispatches only when today's Daily Articles run is entirely absent.
// Existing GitHub watchdog handles failed runs, shortfalls, and deployment lag.
const JST_OFFSET = 9 * 60 * 60 * 1000;
function jstDate(iso) { return new Date(new Date(iso).getTime() + JST_OFFSET).toISOString().slice(0, 10); }
function jstHour(iso) { return new Date(new Date(iso).getTime() + JST_OFFSET).getUTCHours(); }
async function github(env, path, options = {}) {
  const response = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}${path}`, {
    ...options,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "User-Agent": "shonan-doors-cloudflare-watchdog",
      "X-GitHub-Api-Version": "2022-11-28",
      ...(options.headers || {}),
    },
  });
  if (!response.ok) throw new Error(`GitHub ${response.status} ${path}: ${(await response.text()).slice(0, 300)}`);
  if (response.status === 204) return null;
  return response.json();
}
export async function check(env, now = new Date()) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) throw new Error("GITHUB_TOKEN and GITHUB_REPO are required");
  const iso = now.toISOString();
  const hour = jstHour(iso);
  if (hour < 6 || hour >= 22) return { status: "outside_window" };
  const today = jstDate(iso);
  // Fail closed if the authoritative daily state cannot be read.
  // Never bypass system_failure or round_limit safeguards.
  const stateFile = await github(env, "/contents/data/daily_state.json?ref=bot%2Fdaily-state");
  const state = JSON.parse(atob((stateFile.content || "").replace(/\\s/g, "")));
  const daily = (state.days || {})[today] || {};
  if (["system_failure", "round_limit", "complete"].includes(daily.status)) {
    return { status: "daily_state_blocks_dispatch", daily_status: daily.status };
  }
  const data = await github(env, "/actions/workflows/daily-articles.yml/runs?per_page=100");
  const runs = data.workflow_runs || [];
  const todays = runs.filter(r => jstDate(r.created_at) === today);
  if (todays.length) return { status: "run_exists", run_id: todays[0].id };
  // Only the first 6am check dispatches. No retries: avoid duplicate runs on
  // eventual consistency / GitHub API failures. GitHub's own watchdog remains primary.
  if (hour !== 6) return { status: "missing_run_after_dispatch_window" };
  // Double-check immediately before dispatch to narrow the race with GitHub watchdog.
  const again = await github(env, "/actions/workflows/daily-articles.yml/runs?per_page=100");
  if ((again.workflow_runs || []).some(r => jstDate(r.created_at) === today)) return { status: "run_appeared" };
  await github(env, "/actions/workflows/daily-articles.yml/dispatches", {
    method: "POST", body: JSON.stringify({ ref: "main" }),
    headers: { "Content-Type": "application/json" },
  });
  return { status: "dispatched", date_jst: today };
}
export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(check(env).then(result => console.log(JSON.stringify(result)))
      .catch(error => { console.error(error); throw error; }));
  },
};
