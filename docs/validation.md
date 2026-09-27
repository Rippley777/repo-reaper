# Validation record

Validated locally on 2026-09-27 with Python 3.13, Django 5.2.17, PostgreSQL 14.15 (local test instance) and Playwright Chromium. Deployment configuration and CI target PostgreSQL 16.

## Completed

- `pytest -q -m ''` against PostgreSQL: **58 passed** (57 backend/security/integration tests, one end-to-end Chromium test).
- Cross-user repository/detail/scan access, rescan, delete, queue and forged import rejection.
- PostgreSQL composite tenant foreign keys, concurrent enqueue deduplication, distinct worker claims and persistent quotas after analysis deletion.
- OAuth state, expiry, PKCE, numeric identity, session rotation, CSRF and callback replay rejection.
- Token encryption/refresh, per-user cache isolation, ETags, in-flight privacy deletion protection, rate limits, fixed-host requests and safe upstream failure handling.
- Evidence sampling, redaction, empty repositories, malformed manifest text, hard input budget, structured output validation, usage recording and unchanged-result reuse.
- Scan persistence, history, independent failure handling, cancellation, stale-worker fencing and interrupted-AI recovery behavior.
- Desktop and mobile navigation, expandable audits, queue editing, theme switching, sign-out and JavaScript errors checked in Chromium. Screenshots are written to ignored `artifacts/`.
- Ruff checks and formatting; template format check; migration consistency; static asset collection.
- Django `check --deploy --fail-level WARNING` with production-mode environment: **no issues**.
- Web route smoke checks: landing/login/health return 200, dashboard redirects unauthenticated visitors.
- Worker `--once` and operational cleanup commands run against the local development database.
- Numeric-ID GitHub metadata, commit and language endpoint shapes checked using a public example repository, without credentials or writes.

## Not represented as live validation

GitHub OAuth exchange, private installation access, refresh/revocation and OpenAI responses use mocked providers in automated tests. Real account credentials were not supplied and no paid model requests were made. A live two-account OAuth/private-repository smoke test remains required before public launch. Docker is not installed in this workspace, so the container build and hosted deployment were not executed. CI is supplied but has not been run by a remote host.

The browser fixture uses clearly synthetic project data in an isolated test database. It does not add a production login bypass or fake analyses to the application database.
