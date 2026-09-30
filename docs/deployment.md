# Deployment runbook

## Topology
Run one web service, one or more workers, and PostgreSQL 16+. Serve through a trusted HTTPS reverse proxy. Start with one worker and increase using observed GitHub/AI quotas; all processes must share database settings and encryption keys. Multiple workers require PostgreSQL. SQLite is deliberately rejected when `REAPER_DEBUG=false`.

## Container deployment
1. Create `.env` from `.env.example`; generate independent `SECRET_KEY` and Fernet keys. Set a long alphanumeric `POSTGRES_PASSWORD` (or URL-encode special characters in a manually configured database URL).
2. Configure the GitHub App as described in README and set the production callback exactly. Set the `AI_MODELS` allowlist. Users supply their own OpenAI API keys in Settings; no operator OpenAI key is required.
3. Set `REAPER_DEBUG=false`, `APP_URL=https://your-domain`, `ALLOWED_HOSTS=your-domain`, and `CSRF_TRUSTED_ORIGINS=https://your-domain` (defaults to `APP_URL` if omitted). Hosts and origins accept comma-separated values with surrounding whitespace removed and empty entries ignored. Origins must not contain paths, credentials, query strings or fragments. Keep secrets in your hosting platform's secret manager where possible.
4. If the reverse proxy strips inbound `X-Forwarded-Proto` and sets its own trusted value, set `TRUST_PROXY=true`. Do not enable it when clients can directly reach the web container.
5. Build, start PostgreSQL, apply migrations once, then start application processes:

```sh
docker compose build
docker compose up -d db
docker compose run --rm web python manage.py migrate --noinput
docker compose run --rm web python manage.py check --deploy
docker compose up -d web worker
```

Migrations are a separate release step so web/worker replicas never race schema changes. WhiteNoise serves hashed static assets collected during image build. The container runs as a non-root user; the database is never published to the host. Only localhost port 8000 is exposed for the reverse proxy.

For a managed database, override `DATABASE_URL` in both services and omit the local `db` service. Require TLS (`?sslmode=require`) for remote PostgreSQL connections and encrypt disks/backups.

A minimal Caddy reverse proxy on the host:

```caddyfile
your-domain.example {
    reverse_proxy 127.0.0.1:8000
}
```

Caddy manages HTTPS certificates. Configure it to prevent inbound forwarded-header spoofing. Do not enable access logs that include OAuth callback query strings. Application Gunicorn access logging is disabled by default to avoid code/state URLs in logs. Apply the same rule at load balancers, tracing, and error reporting.

## Azure Container Apps and Cloudflare

### Diagnosed configuration (2026-09-29)

Read-only Azure inspection found `repo-reaper-web` in resource group `repo-reaper`, using HTTP ingress on port 8000 with `allowInsecure=false`, image `ghcr.io/rippley777/repo-reaper-app:sha-bf51517`, and no custom-domain binding. Both `APP_URL` and `ALLOWED_HOSTS` were set to `repo-reaper-web.wittyisland-9a5621a8.southcentralus.azurecontainerapps.io` (with `https://` on `APP_URL`). `TRUST_PROXY=true` was already enabled. No `CSRF_TRUSTED_ORIGINS` environment variable was configured; the old code ignored that variable and trusted only `APP_URL`.

Consequently, when a proxy forwards the Azure Host while the browser posts from `https://reporeaper.oddware.dev`, Django rejects the public Origin even with a valid CSRF cookie/token. The old authorization and token exchange also send GitHub the Azure callback. A preserved public Host would instead fail `ALLOWED_HOSTS`. A true GET in the original code returns 405, not a Django CSRF 403; the login form performed a POST. These are separate from Cloudflare's error 1010, which blocked automated checks through the public domain during diagnosis. Cloudflare routing configuration and GitHub registration were not available for inspection.

### Environment and ingress

Deploy the updated image and configure these values on the web container (and matching app settings on workers):

```dotenv
REAPER_DEBUG=false
APP_URL=https://reporeaper.oddware.dev
ALLOWED_HOSTS=reporeaper.oddware.dev,repo-reaper-web.wittyisland-9a5621a8.southcentralus.azurecontainerapps.io
CSRF_TRUSTED_ORIGINS=https://reporeaper.oddware.dev
TRUST_PROXY=true
```

The explicit Azure host supports the existing origin routing while it is inspected/migrated. It does not influence the canonical OAuth callback. If Cloudflare preserves the public Host and no health probe or origin request needs the Azure host, reduce `ALLOWED_HOSTS` to `reporeaper.oddware.dev`. Never add Azure URLs to the public `APP_URL` or public GitHub callback. Keep the existing secrets, encryption keys, database and GitHub credentials; changing `SECRET_KEY` would invalidate sessions.

This is **Azure Container Apps**, not App Service: no `WEBSITES_PORT` or App Service authentication setting is needed. Keep ingress target port 8000, HTTP transport, and `allowInsecure=false`. Azure's [HTTP ingress documentation](https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview#http-headers) states that ingress overwrites client-provided `X-Forwarded-Proto`. That makes the existing `TRUST_PROXY=true` appropriate here, setting `SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https")`. Do not expose the container directly around this ingress. [Django requires the proxy to sanitize this header](https://docs.djangoproject.com/en/5.2/ref/settings/#secure-proxy-ssl-header).

`USE_X_FORWARDED_HOST=False` is intentional: Azure does not document an equivalent trust guarantee for that header. Prefer preserving the actual public `Host`:

- For proxied DNS/CNAME routing, bind `reporeaper.oddware.dev` and a matching certificate in Azure Container Apps first. No such binding existed at inspection time. Preserve that Host through Cloudflare.
- If an existing Cloudflare Worker fetches the Azure origin URL, the explicit Azure allowed host above accommodates that upstream Host. Forward browser cookies, Origin and Referer to the app and return Set-Cookie, Location and Cache-Control unchanged. The canonical `APP_URL` and explicit public CSRF origin make this configuration work without trusting arbitrary forwarded hosts. A custom-domain binding is needed if switching to direct routing with the public Host.

Use Cloudflare [Full (strict) TLS](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/) with a valid origin certificate. A Worker must fetch the origin with `https://`. Flexible mode is incompatible with reliably preserving HTTPS through Azure ingress. Remove any redirect/response rewrite that exposes the Azure hostname. Bypass Cloudflare caching for `/login`, `/auth/*` and authenticated pages, and respect `Cache-Control: no-store, private` and Set-Cookie. Do not disable security checks site-wide to resolve the unrelated automated-client error 1010; inspect Cloudflare Security Events if real browsers also encounter it.

`SECURE_SSL_REDIRECT=True`, `SESSION_COOKIE_SECURE=True`, `CSRF_COOKIE_SECURE=True`, both SameSite values `Lax`, and both cookie domains `None` apply in production. No `.oddware.dev` or Azure cookie domain is needed. Clear old site cookies once when verifying if a previous deployment used different cookie scopes.

### Exact GitHub flow

This is custom Django/httpx code using a **GitHub App user authorization flow**, not django-allauth, social-auth, Authlib or a classic GitHub OAuth App. Configure the GitHub App's user authorization callback to exactly:

```text
https://reporeaper.oddware.dev/auth/github/callback
```

There is no trailing slash. Set its homepage to `https://reporeaper.oddware.dev`. The registration itself must be checked in GitHub; repository code cannot verify its configured callback.

1. GET `/auth/github` creates random state and an S256 PKCE verifier in the server-side session, then returns 302 to `https://github.com/login/oauth/authorize`. The callback URI is `APP_URL + reverse("oauth_callback")`. Existing POST clients remain CSRF protected.
2. GitHub returns a top-level GET to `/auth/github/callback` with code/state. The Lax session cookie accompanies this navigation.
3. Django consumes pending state once, checks state and its ten-minute expiry, then posts the code, verifier and **same callback URI** to `https://github.com/login/oauth/access_token` with the configured client credentials. It accepts only GitHub App user tokens.
4. The app fetches `/user`, identifies/updates the user by numeric GitHub ID, rejects changing identities during reconnect, encrypts provider tokens, calls Django `auth_login` (rotating the session on initial sign-in), and returns a relative redirect to `/dashboard`.

No query string, code, state, token, secret or session ID is included in the new OAuth diagnostics. Infrastructure access logging must also omit callback query strings.

### Verification

Run locally from the repository root:

```sh
.venv/bin/python -m pytest -q tests/test_proxy_auth.py tests/test_auth.py tests/test_security.py
.venv/bin/python -m ruff check .
.venv/bin/python -m djlint templates --check
```

The tests load production environment settings, simulate HTTP at the backend with `Host: reporeaper.oddware.dev` and `X-Forwarded-Proto: https`, verify the callback in authorization and token exchange, create/rotate the authenticated session with mocked GitHub responses, check cookie flags, reject replay and hostile origins, reproduce the original Azure/public-origin CSRF failure, and test its corrected configuration. `TRUST_PROXY=false` remains the safe default outside verified ingress. Ordinary localhost development uses `REAPER_DEBUG=true`, `APP_URL=http://localhost:8000` and the local callback.

After deploying and updating GitHub/Azure/Cloudflare:

1. Open a fresh browser session at `https://reporeaper.oddware.dev/auth/github`. Expect a single 302 to GitHub, not a 403, 405 or redirect loop. In browser developer tools, inspect the decoded `redirect_uri`: it must be exactly the public callback above. Do not copy or publish authorization query strings.
2. Complete GitHub authorization. Expect the public callback and then `/dashboard` on the public domain, with an authenticated session. Verify reconnect, refresh and logout.
3. Check cookies in browser storage: host `reporeaper.oddware.dev`, no Domain attribute, Secure, SameSite=Lax; session cookie also HttpOnly. Submit an ordinary settings form to verify CSRF-protected writes.
4. Inspect `reaper.oauth` messages in Azure logs: scheme `https`, `is_secure=true`, public callback URI. Host should be public with direct custom-domain routing, or the explicitly allowed Azure origin with the documented Worker routing. The browser must never navigate to the Azure hostname. Django's CSRF logger retains rejection reasons without logging submitted tokens.
5. Run `python manage.py check --deploy --fail-level WARNING` inside the deployed container. Confirm the intended image revision receives all ingress traffic.

## Non-container deployment
Use Python 3.13 and install `requirements.txt`. Set the same production environment variables. Run migrations and `collectstatic`, then:

```sh
gunicorn config.wsgi:application --bind 127.0.0.1:8000 --workers 2 --timeout 60 --access-logfile /dev/null
python manage.py scanworker
```

Supervise both processes with the hosting platform/systemd. Allow workers at least 180 seconds to finish after SIGTERM. If forcibly stopped, leases recover pre-AI work; jobs interrupted during AI analysis fail for manual retry to avoid blind rebilling.

## Pre-launch verification
- Run PostgreSQL tests and Chromium tests sequentially. Run `check --deploy` with production settings; it should report no security warnings.
- Confirm the App has only Contents/Metadata read, optional Issues read, and no write permission. Test public login, private installation selection, declined OAuth and revoked access with two separate GitHub accounts.
- Scan one small public repository, rescan unchanged content, verify cached AI usage is zero, then change content in your own normal workflow and verify scan history/diffs.
- Test one permitted private repository. Confirm other users cannot access its UUID route, queue, scan endpoint or analysis. Confirm a repository outside installation access fails safely.
- Test provider quota errors, stopped worker recovery, and disconnection while a job runs. No live credentials were assumed by the automated suite.
- Tell users to configure provider spend caps and alerts on their dedicated OpenAI projects. Token usage is per completed scan; a timed-out or crashed request may incur provider costs without a persisted usage record.
- Ensure `/health` returns 200 through HTTPS and 503 on database outage. Monitor worker process liveness, age of queued jobs, failures and DB capacity. Health reports database reachability, not worker liveness or provider connectivity.

## Routine operations
Run daily:

```sh
python manage.py prune
```

This removes API cache entries expired for over one day, expired sessions and old rate-limit counters. Expired API entries briefly remain to enable ETag revalidation; no entry authorizes a new scan without an ownership/access check. Privacy deletion clears user caches immediately and an epoch prevents older in-flight requests restoring them.

Back up PostgreSQL regularly; encrypt backups and keep encryption keys separately. Define a finite retention policy, test restoration, and communicate that policy to users. Do not restore deleted-user data into the serving application without replaying privacy deletions. User deletion removes live database records; backup and provider retention are separate.

Rotate database credentials normally. For application credential encryption rotation, set `TOKEN_ENCRYPTION_KEYS=new_key,old_key` everywhere, run `python manage.py rotate_tokens`, and verify successful decryption. This re-encrypts GitHub credentials, user OpenAI keys, and API caches. Keep the old key available to restore retained backups; remove it from runtime only after every active record is rotated. Changing `SECRET_KEY` invalidates sessions unless Django fallback keys are deliberately managed; plan this separately.

## Limits and scaling
Default 50 scan creations per user per 24-hour budget window and 20 per batch. Active scan uniqueness prevents duplicate tab submissions. GitHub evidence calls are bounded and sequential per worker; backoff reschedules jobs instead of sleeping in a web request. Start with one worker and use `docker compose up -d --scale worker=2` only after observing rate usage.

Dashboard filtering currently operates over each user's imported repository summaries; history records are retained until deletion. For very large accounts, move array filters into normalized indexed tables and paginate before materialization. PostgreSQL and Django remain the deployment boundary; a broker/microservices are unnecessary for the initial workload.
