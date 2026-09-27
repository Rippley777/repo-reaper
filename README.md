# Repo Reaper

**Good code deserves a second life.** A private, multi-user GitHub portfolio audit application. Discover repositories, select what to scan, inspect evidence-backed AI assessments, and prioritize promising projects in a Resurrection Queue. The application never writes to a GitHub repository.

Built with Django 5.2 LTS, PostgreSQL, server-rendered HTML/CSS, a durable database job queue, and an interchangeable AI provider interface. No Node build or Redis service is needed.

## Run locally

Requires Python 3.13. PostgreSQL 16+ is recommended; SQLite works for single-process development and fast tests only.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.lock
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(64))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the generated values into `SECRET_KEY` and `TOKEN_ENCRYPTION_KEYS` in `.env`. Configure the GitHub App below. Keep `REAPER_DEBUG=true` for localhost. Then:

```sh
python manage.py migrate
python manage.py runserver
```

In another terminal, with the same environment:

```sh
source .venv/bin/activate
python manage.py scanworker
```

Visit **http://localhost:8000**. Sign in, open **Settings & privacy**, add your own OpenAI API key, give AI consent, then choose repositories to scan. The key is encrypted at rest, used server-side, and billed to the user's OpenAI project; the operator does not supply a shared AI key. Use a dedicated OpenAI project with a spending limit and expiring project key. Missing credentials produce setup guidance rather than fabricated analyses.

## GitHub App setup (read-only private access)

A classic OAuth App cannot offer private repository access without the broad `repo` scope, which includes writes. Repo Reaper therefore uses a **GitHub App's user OAuth flow** with fine-grained read-only permissions.

1. In GitHub developer settings, register a GitHub App. Use the application URL as the homepage.
2. Set the user authorization callback to `http://localhost:8000/auth/github/callback` locally, or `https://your-domain/auth/github/callback` in production.
3. Enable expiring user access tokens. Disable device flow and webhooks; neither is needed.
4. Grant **Repository permissions → Contents: Read-only** and **Metadata: Read-only**. Optionally grant **Issues: Read-only** to inspect issue titles. Grant **no write permissions**, no organization permissions, and no account permissions.
5. Leave “Request user authorization (OAuth) during installation” disabled. Users begin the state-protected authorization flow in the application. After installing the app, return to Repo Reaper and sign in/reconnect.
6. Set `GITHUB_CLIENT_ID` to the App's **client ID**, not numeric App ID. Set its client secret and `GITHUB_APP_SLUG` in `.env`. No app private key is necessary because the scanner uses user access tokens, not installation tokens.
7. Users can authorize public access without granting private content access. To scan private repositories, they install the app for selected repositories and separately enable private access in Repo Reaper Settings. Organization policy may require approval.

GitHub token scopes are deliberately omitted. Tokens with classic OAuth scopes are rejected. The app registration itself is the permission boundary: operators must preserve the read-only configuration. Tokens are encrypted and stay on the server. PKCE and expiring, single-use OAuth state protect sign-in.

## What is implemented

- Landing, login, dashboard, repository selection, repository detail, Graveyard, queue, and settings pages.
- Responsive dark/light UI; expandable table rows; text, status, language, framework, effort, value, visibility and archive filters; sorting and dashboard pagination.
- GitHub identity by numeric ID, persistent sessions, encrypted rotating GitHub and per-user OpenAI credentials, explicit private access and AI consent.
- Paginated discovery (100 repositories/page; filters and select-all operate on the current page). Selection is capped at 20 repositories per submission.
- Background jobs with progress polling, quotas, active-job deduplication, row locks, lease recovery and stale-worker fencing.
- Bounded inspection of metadata, languages, immutable commit trees, README, manifests, test/CI/deployment configuration, representative source, recent commits, release metadata and optional issue titles. Lockfile presence is recorded; giant/non-text/generated/secret-looking files are skipped.
- Structured AI result validation, fixed status/effort/value enums, confidence, uncertainty, evidence references, technical debt, portfolio angle and concrete next steps.
- Per-user, per-repository evidence hash/version/model caching, token usage, scan history and field-level comparisons.
- Individual/all-analysis deletion, disconnect/revocation attempt, account deletion and safe cancellation.
- PostgreSQL composite foreign keys plus tenant-scoped application authorization. No public sharing routes.

## Tests and checks

```sh
pytest -q
ruff check .
djlint templates --check
ruff format --check config reaper tests manage.py
python manage.py makemigrations --check --dry-run
python manage.py check
playwright install chromium
pytest -q -m browser
```

Use a dedicated PostgreSQL database to exercise composite foreign keys and concurrent worker tests:

```sh
DATABASE_URL=postgresql://user:password@localhost:5432/reaper_test_source pytest -q
```

The test role must be allowed to create a temporary `test_*` database. Run the browser suite and main suite sequentially; both create and remove that database. Tests mock GitHub and AI responses; no live credentials or billed requests are required. Browser tests authenticate through test fixtures, never a production bypass, and save screenshots under ignored `artifacts/`.

CI runs PostgreSQL-backed tests, migration checks, formatting, static collection, and real Chromium interactions. The critical suite checks that User A cannot read, rescan, delete, queue, import, or poll User B's data.

## Project layout

```text
config/                    Django settings, routes, WSGI
reaper/models.py           Tenant-owned entities and indexes
reaper/migrations/         Schema and PostgreSQL tenant constraints
reaper/services/github.py  OAuth exchange, refresh, bounded API client, ETags
reaper/services/evidence.py Selective evidence and redaction
reaper/services/analysis.py Provider protocol, schema, OpenAI implementation
reaper/services/jobs.py     Durable jobs, quotas, caching, persistence
reaper/views.py             Session-scoped pages and actions
reaper/management/commands/ Worker and operational maintenance
static/                    CSS, progressive-enhancement JavaScript
templates/                Server-rendered application pages
tests/                    Security, lifecycle, integration, browser tests
```

See [architecture and threat model](docs/architecture.md), [deployment instructions](docs/deployment.md), and [security notes](docs/security.md).

## Boundaries and deployment status

Source inspection is an evidence sample, not a full static analyzer, dependency vulnerability feed, or deployment verification. “Meaningful commit” uses an explicitly labeled recent-message heuristic. AI findings require engineering judgment. Scan records retain the evidence inventory and metadata, not file excerpts; encrypted API caches can temporarily retain excerpts until pruning. Analyses may themselves contain sensitive project information.

Public portfolio publication is intentionally absent. A future implementation must use separate explicit publication records, curated fields, private-repository confirmation, and dedicated authorization tests.

This repository contains deployable application code and automated validation. Actual GitHub OAuth/private-installation behavior and paid model calls must be smoke-tested with a user-supplied credential before a public launch. Infrastructure deployment, DNS, backup retention, and independent security review remain operator responsibilities. Each user controls their own provider billing limits. See the deployment runbook for the exact sequence.
