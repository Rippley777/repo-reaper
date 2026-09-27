# Deployment runbook

## Topology
Run one web service, one or more workers, and PostgreSQL 16+. Serve through a trusted HTTPS reverse proxy. Start with one worker and increase using observed GitHub/AI quotas; all processes must share database settings and encryption keys. Multiple workers require PostgreSQL. SQLite is deliberately rejected when `REAPER_DEBUG=false`.

## Container deployment
1. Create `.env` from `.env.example`; generate independent `SECRET_KEY` and Fernet keys. Set a long alphanumeric `POSTGRES_PASSWORD` (or URL-encode special characters in a manually configured database URL).
2. Configure the GitHub App as described in README and set the production callback exactly. Set the `AI_MODELS` allowlist. Users supply their own OpenAI API keys in Settings; no operator OpenAI key is required.
3. Set `REAPER_DEBUG=false`, `APP_URL=https://your-domain`, and `ALLOWED_HOSTS=your-domain`. Keep secrets in your hosting platform's secret manager where possible.
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
