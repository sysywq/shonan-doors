// Low-cost external watchdog: Cloudflare checks every 30 minutes; GitHub Actions
// only runs when publication is incomplete. The existing GitHub watchdog is the
// authority for recovery, limits, and Pages repair.
const JST_OFFSET = 9 * 60 * 60 * 1000;
const SITE = "https://www.shonandoors.com";
const ACTIVE = new Set(["queued", "in_progress", "waiting", "pending", "requested"]);
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
function decodeFile(file) {
  if (!file?.content) throw new Error("GitHub content missing; refusing to dispatch");
  return JSON.parse(atob(file.content.replace(/\s/g, "")));
}
async function isPublished(slug) {
  if (!/^[a-z0-9-]+$/.test(slug)) return false;
  const response = await fetch(`${SITE}/articles/${slug}/`, { redirect: "follow" });
  return response.ok;
}
export async function check(env, now = new Date()) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) throw new Error("GITHUB_TOKEN and GITHUB_REPO are required");
  const iso = now.toISOString();
  const hour = jstHour(iso);
  if (hour < 5 || hour >= 22) return { status: "outside_window" };
  const today = jstDate(iso);
  // Read the authoritative state first. Any error fails closed.
  const state = decodeFile(await github(env, "/contents/data/daily_state.json?ref=bot%2Fdaily-state"));
  const daily = (state.days || {})[today] || {};
  if (["system_failure", "round_limit"].includes(daily.status))
    return { status: "daily_state_blocks_dispatch", daily_status: daily.status };
  const articles = decodeFile(await github(env, "/contents/data/articles.json?ref=main"));
  if (!Array.isArray(articles)) throw new Error("Invalid articles data");
  const rows = articles.filter(a => a.date === today && !a.mergedInto && typeof a.slug === "string");
  if (rows.length >= 3) {
    const listingResponse = await fetch(SITE + "/", { headers: { "Cache-Control": "no-cache" } });
    if (!listingResponse.ok) throw new Error("Production listing unavailable");
    const listing = await listingResponse.text();
    let live = 0;
    for (const a of rows) {
      const path = `/articles/${a.slug}/`;
      if (listing.includes(path) && await isPublished(a.slug)) live++;
      if (live >= 3) return { status: "published_complete", live };
    }
  }
  if (daily.status === "complete") return { status: "state_complete_but_publication_unverified", main_count: rows.length };
  // Cloudflare never generates articles itself. Delegate recovery to the existing
  // GitHub watchdog, which enforces cooldowns, run limits and publication checks.
  const watchdog = await github(env, "/actions/workflows/daily-publication-watchdog.yml/runs?per_page=30");
  const recent = (watchdog.workflow_runs || []).some(r => {
    const age = now.getTime() - new Date(r.created_at).getTime();
    return age >= 0 && age < 28 * 60 * 1000;
  });
  if (recent) return { status: "watchdog_recent" };
  // Avoid waking GitHub before the primary daily job has had time to start.
  if (hour === 5 && now.getUTCMinutes() < 30) return { status: "awaiting_daily_start" };
  // Fail closed when the API cannot confirm the state or recent watchdog runs.
  await github(env, "/actions/workflows/daily-publication-watchdog.yml/dispatches", {
    method: "POST",
    body: JSON.stringify({ ref: "main" }),
    headers: { "Content-Type": "application/json" },
  });
  return { status: "watchdog_dispatched", date_jst: today, main_count: rows.length };
}
export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(check(env).then(result => console.log(JSON.stringify(result)))
      .catch(error => { console.error(error); throw error; }));
  },
};
