# Repo Reaper architecture (implementation plan)

## 1. Stack
Python 3.13, Django 5.2 LTS, PostgreSQL 16+, server-rendered HTML, CSS and small progressive-enhancement JavaScript. HTTPX talks to GitHub and OpenAI. Pydantic validates AI output. Gunicorn and WhiteNoise serve the application behind an HTTPS reverse proxy. One codebase, two processes (web and worker), one database. SQLite is permitted only for local development and quick tests.

## 2. Application
Django owns sessions, CSRF, templates, validation and authorization. Domain services separate GitHub, evidence inspection, AI providers and scan jobs. All pages except landing/login/health require authentication. No public publishing routes exist. Repository contents are never executed or modified. No frontend build or distributed task broker is required.

## 3. Authentication
Use **GitHub App user OAuth**, not a classic OAuth App: classic private-repository `repo` scope includes write capabilities. Register a GitHub App with Metadata: read and Contents: read (Issues: read optional); no write permissions. Public repositories work via user authorization; private access requires the user's explicit installation/repository selection and an additional in-app opt-in. Request no OAuth scopes. Session-bound, expiring, single-use state and PKCE protect callback. Exchange code server-side, fetch `/user`, key identity by numeric GitHub ID, rotate session. Encrypt both access and refresh tokens with a separate Fernet key; refresh under a database row lock. Sessions are server-side with HttpOnly, SameSite=Lax and production Secure cookies. Account model can be supplemented by another provider without changing repository ownership.

## 4. GitHub API
User-token requests to a fixed GitHub API host; never accept arbitrary URLs. Enumerate paginated `/user/repos`; selection submits numeric IDs from a user-scoped discovery cache. Revalidate access by numeric ID at scan time. Repository owner/name is display data, never an authorization claim. Conditional requests with user-scoped ETag cache, bounded responses, per-worker request deduplication and rate-limit retry timestamps. Scan latest metadata even when commit is unchanged. Cache invalidation accompanies reconnect, privacy changes and deletion.

## 5. AI analysis
Collect bounded metadata, language statistics, tree paths, manifests, README/docs, test/CI/deployment evidence and representative source excerpts at an immutable commit. Sample recent commits/releases/issues and report missing/truncated evidence explicitly. Never clone, execute code, follow symlinks or send all source. Redact obvious credentials; detection is best-effort, and UI explains data sharing. Hash sanitized evidence; reuse only same-user, same-repository results with the same analysis version, model and input hash. Schema-validated enums, evidence references and uncertainty separate observations from inference. Provider interface initially implements OpenAI Responses structured outputs with `store: false`; no tools or code execution. Each request uses the scanning user's encrypted, server-side OpenAI project key. Prompt instructs model to treat repository text as untrusted evidence. Track input/output tokens. No automatic AI retry after an ambiguous timeout.

## 6. Jobs
RepositoryScan doubles as a durable job: Queued → Fetching Repository → Inspecting Files → Analyzing → Complete/Failed. PostgreSQL `SELECT FOR UPDATE SKIP LOCKED` claims jobs; a partial unique constraint allows only one active scan per user/repository. Lease and fencing token prevent stale workers persisting results. Expired pre-AI jobs retry with bounds; expired Analyzing jobs fail for manual retry to avoid duplicate charges. Rate-limited jobs are rescheduled, not held in sleeping HTTP requests. User lock serializes enqueue/quota checks, completion and privacy deletion. Worker updates progress, browser polls a protected status endpoint. Independent jobs isolate failures. Multiple workers are supported with PostgreSQL.

## 7. Schema
- User: Django identity + GitHub numeric ID, username, display name, avatar; privacy/model preferences.
- GitHubAccount: one-to-one user; encrypted access/refresh credentials, expiry, scope metadata.
- AIAccount: one-to-one user; encrypted OpenAI API key and masked display hint.
- Repository: UUID, user FK, numeric GitHub ID, metadata, latest SHA; unique(user, github ID).
- RepositoryScan: UUID, user FK, repository FK, state, version, model, input hash, usage, lease, retry and timestamp fields. Active-scan unique constraint.
- RepositoryAnalysis: one-to-one scan + user FK; validated structured result, indexed status/value/effort.
- ResurrectionQueueItem: user FK, repository FK, priority/notes; unique(user, repository).
- GitHubCache: user FK, URL key, encrypted response body, ETag and expiry.
- RateBucket: hashed session/IP/user namespace, window, counter (operational metadata only).
PostgreSQL composite foreign keys enforce matching tenant IDs across repository, scan, analysis and queue relationships, supplementing service scoping. Cascades remove dependent data. Indexes cover tenant lists, job claims and result filters.

## 8. Authorization
Every protected lookup begins with authenticated `request.user`; UUIDs are not security boundaries. Foreign rows return 404, including progress, queue, rescan and deletion endpoints. Creation uses server-side identity; batch selection must match the user's discovery snapshot. Worker reconstructs identity from the stored job and verifies relationships. GitHub API is an additional live access check, not a substitute for local ownership. No cross-tenant AI cache reuse. Automated tests exercise two users across every object endpoint and persistence path.

## 9. Threat model
Threats: OAuth CSRF/code interception; stolen DB/session/token; IDOR; forged repository selection; prompt injection; repository secrets in prompts; stored XSS; SSRF; malicious/huge files; duplicate-cost abuse; worker races and deletion resurrection. Controls: state+PKCE+CSRF, encrypted tokens/cache and secret separation, tenant-scoped queries/composite FKs, access revalidation, fixed-host HTTP, autoescaping/CSP, bounded sampling, quotas, idempotent queueing and fenced transactional writes. Residual risks: provider sees selected evidence; stored analyses can contain sensitive information; best-effort redaction is not a secret scanner; dependency/security conclusions are not a vulnerability audit; compromised application server can decrypt tokens. Use encrypted database disks/backups, restricted operators and provider retention policy review. No publication or repository write feature.

## 10. Operating costs
Two small processes plus managed PostgreSQL are sufficient initially. Choose hosting for measured load; no unverified dollar estimate. Each user pays OpenAI directly through their own project key. AI cost = input tokens × model input rate + output tokens × output rate. Default bounds: 48k evidence characters, 5k output tokens, 20 selected repositories/request, 50 new scans/user/day. Commit/evidence caches avoid repeated AI calls; usage is stored per scan. GitHub API allowance, database retention, worker count and model availability limit throughput. Configure quotas and model allowlist centrally. Users should configure provider spending limits because infrastructure failure cannot provide exactly-once external billing.

## References
- [GitHub App OAuth and PKCE](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app)
- [GitHub permission model](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)
- [Django deployment checklist](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/)
- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Responses storage configuration](https://developers.openai.com/api/docs/guides/migrate-to-responses)
