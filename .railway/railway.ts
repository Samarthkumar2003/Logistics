/**
 * Railway Infrastructure as Code — the deployed shape of this project.
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * READ THIS BEFORE RUNNING `railway config apply`
 * ─────────────────────────────────────────────────────────────────────────────
 * Apply is destructive by omission: one project definition, one apply, and any
 * resource NOT named in this file gets DELETED from the environment. If you built
 * the project by hand in the dashboard first, import that state before you touch
 * apply:
 *
 *     npm install railway            # provides the `railway/iac` module below
 *     railway link                   # pick the project + environment
 *     railway config pull --force    # rewrite this file from what exists
 *     railway config plan            # read every line of the diff
 *     railway config apply           # only once the plan says what you expect
 *
 * `plan` is not optional. A plan that proposes deleting a service or a variable
 * you did not touch means the file is out of date, not that the deletion is fine.
 *
 * This file is committed as documentation of the intended topology and as the
 * reproducible path to rebuild it. Documentation/06-deploying.md has the
 * click-through equivalent, which is what a first deploy normally uses.
 *
 * NOT railway.toml: Config as Code is deprecated, Railway stops reading those
 * files on 2026-12-01, and new services cannot opt into them at all — a
 * railway.toml added today would simply be ignored.
 */

import { defineRailway, github, preserve, project, service } from "railway/iac";

/**
 * From `git remote -v`. `rootDirectory` is omitted for the API because its
 * Dockerfile sits at the repo root, which is where Railway looks.
 */
const REPO = "Samarthkumar2003/Logistics";

export default defineRailway(() => {
  /**
   * The API, the scheduler, and everything else Python — one service.
   *
   * ONE service and exactly ONE replica, and that is a correctness constraint,
   * not a cost saving. The scheduler starts in the FastAPI lifespan handler, so
   * every process that boots the app gets its own copy of every background job —
   * four one-shot threads and seven repeating. Two replicas means two schedulers
   * scanning the same inbox every five
   * minutes: safe, because the atomic claim in the scan means no email is
   * processed twice — but the claim happens *after* classification, so you pay
   * OpenAI twice for identical work, silently and indefinitely.
   *
   * To scale the API, split the scheduler out first: a second service off this
   * same Dockerfile with RUN_SCHEDULER=1 and no public domain, then
   * RUN_SCHEDULER=0 here and replicas to taste. See
   * Documentation/06-deploying.md § Scaling past one replica.
   */
  const api = service("api", {
    source: github(REPO),

    // /health returns 200 when Supabase answers and 503 when it does not, so
    // the status code alone decides whether a deploy is promoted.
    healthcheck: "/health",
    // The lifespan handler does synchronous network I/O (check_connectivity)
    // before the app accepts a request, then starts four one-shot threads that
    // compete for CPU. 30s can fail a healthy cold boot.
    healthcheckTimeout: 120,

    replicas: 1,

    env: {
      // ── The one variable this file owns outright ──────────────────────────
      //
      // Set here rather than preserve()d because getting it wrong is the
      // expensive mistake in this deployment, and a value in the repo is a value
      // in code review.
      RUN_SCHEDULER: "1",

      // ── Container-appropriate logging ────────────────────────────────────
      // stdout is the log; a rotating file inside a container is written to the
      // overlay filesystem and dies with it. JSON so the four correlation ids
      // (request_id / job_id / scan_id / email_id) are queryable fields.
      LOG_TO_FILE: "0",
      LOG_JSON: "1",
      LOG_LEVEL: "INFO",

      // ── Everything secret stays in Railway ───────────────────────────────
      //
      // preserve() means "keep whatever is already set on the service". Set these
      // in the dashboard or with `railway variables --set`; they are never
      // written to this file, and `railway config pull` renders them as
      // preserve() rather than inlining them.
      SUPABASE_URL: preserve(),
      SUPABASE_KEY: preserve(),
      OPENAI_API_KEY: preserve(),
      EMAIL_ACCOUNT: preserve(),
      EMAIL_PASSWORD: preserve(),
      GOOGLE_OAUTH_CLIENT_ID: preserve(),
      GOOGLE_OAUTH_CLIENT_SECRET: preserve(),
      GMAIL_REFRESH_TOKEN: preserve(),
      JWT_SECRET: preserve(),

      // ── Not secret, but environment-specific ─────────────────────────────
      //
      // CORS_ORIGINS must be the frontend service's exact public origin —
      // scheme + host, no trailing slash, no wildcard. A browser compares the
      // Origin header literally.
      //
      // GOOGLE_OAUTH_REDIRECT_URI must stay http://localhost:8080/ in
      // production. It is not a runtime callback; it only has to match the URI
      // the refresh token was minted against. Pointing it at the deployed domain
      // breaks every token refresh, i.e. all inbox ingest.
      //
      // EMAIL_REDIRECT is the safe-mode valve. Leave it set to your own address
      // until you intend to mail real freight agents.
      CORS_ORIGINS: preserve(),
      GMAIL_MAILBOX: preserve(),
      GOOGLE_OAUTH_REDIRECT_URI: preserve(),
      EMAIL_REDIRECT: preserve(),
      REPORT_RECIPIENT: preserve(),
      SYNC_ALERT_RECIPIENT: preserve(),
    },
  });

  /**
   * The Next 16 dashboard.
   *
   * `rootDirectory` because the app is a subdirectory of the repo; without it
   * Railway builds the root, finds the Dockerfile, and deploys a second copy of
   * the API under the frontend's name.
   */
  const frontend = service("frontend", {
    source: github(REPO, { rootDirectory: "frontend" }),
    build: "npm run build",
    start: "npm run start",

    env: {
      // Build-time, not runtime. `next build` inlines NEXT_PUBLIC_* into the
      // JavaScript bundle, so this has to be set before the first build and the
      // service has to be REDEPLOYED (not restarted) whenever it changes.
      // Its value is the api service's public URL, e.g.
      // https://api-production-xxxx.up.railway.app — no trailing slash.
      //
      // Left as preserve() because the api service's generated domain is not
      // known until the project exists, and generated Railway domains are
      // deliberately kept out of IaC files.
      NEXT_PUBLIC_API_BASE: preserve(),
    },
  });

  return project("logistics-copilot", { resources: [api, frontend] });
});
