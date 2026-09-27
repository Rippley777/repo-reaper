# Security and privacy notes

## Tenant isolation
All repository, scan, result, queue and API-cache records have a user owner. Protected views always use session identity; submitted owners/user IDs are ignored. Unknown and foreign UUIDs return the same 404. Import uses an encrypted user-owned discovery snapshot, then the worker checks access with that user's GitHub App token and numeric repository ID. Shared repositories imported by two users produce separate records and analyses. Caches never cross users.

PostgreSQL composite foreign keys enforce that scan→repository, analysis→scan and queue→repository relationships share the same user ID. UUIDs alone are not treated as authorization. SQLite quick tests cannot enforce these extra constraints; PostgreSQL tests do. No admin/public data browsing surface is installed.

## Credentials and sessions
OAuth start is POST + CSRF protected. Callback uses constant-time state comparison, a ten-minute expiry, single-use session state and S256 PKCE. Identity is keyed by GitHub numeric user ID, not renameable username. GitHub App tokens are accepted; classic scope-bearing OAuth tokens are not. Reconnecting another identity inside an existing session is rejected. GitHub permission settings remain the operator's responsibility.

Access and refresh tokens, user-supplied OpenAI API keys, and API caches use authenticated Fernet encryption with keys outside the database. Only a masked key suffix is rendered after an OpenAI key is saved. Refresh locks the GitHub credential row to avoid rotating a refresh token twice. Disconnect attempts GitHub grant revocation and always removes local GitHub credentials; failure to confirm remote revocation is shown with instructions for GitHub Settings.

Sessions reside in PostgreSQL, not the browser. Cookies are HttpOnly, SameSite=Lax and Secure in production. All mutations require POST and CSRF; logout flushes the session. Production configuration requires a strong session secret, encryption keys, PostgreSQL and an HTTPS app URL. HTTPS/HSTS, CSP, frame denial, no-sniff and same-origin referrer policy are enabled. Private responses set no-store.

## Repository and AI boundaries
GitHub content APIs are GET-only, with an allowlisted hostname, disabled redirects, bounded response bodies, pinned commit references, sampled files and no cloning/execution. The only GitHub mutation is explicit authorization revocation; repository contents are never changed. Symlinks/submodules, generated directories, large files and secret-looking filenames are excluded. Common credentials are redacted, but that is not comprehensive secret detection. Users must have permission to send selected content to the provider.

The provider prompt labels repository content as untrusted data. There are no model tools, network capabilities, or code execution. Pydantic validates fixed enums and rejects extra fields; evidence IDs must exist in the actual evidence inventory. Templates escape both repository text and AI output. Model text can still be mistaken or manipulated: confidence and uncertainty are visible, and no output triggers automatic code changes. Missing/truncated evidence is not treated as proof of absence. No vulnerability feed is consulted; dependency age/security claims should be independently verified.

OpenAI requests use the current user's key and set `store: false`. The key stays server-side and is never returned after saving. Replacing or deleting it increments the user's privacy epoch, cancels active scans, and prevents an older in-flight worker from saving its result. An already-started provider request cannot be recalled. `store: false` is not a guarantee of zero provider retention: abuse monitoring and contractual data controls may differ. See [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data). Repository file excerpts are not copied into scan records, but encrypted API caches temporarily contain fetched blobs. Reports and metadata can themselves be sensitive. Use encryption at rest and access restrictions on PostgreSQL and backups.

## Concurrency and cost
User row locks serialize enqueues, quota checks, deletion and final persistence. A partial unique constraint blocks duplicate active scans. Workers claim with skip-locked rows and five-minute leases, renew during evidence collection, and persist only with the current fencing token. Deletion/cancellation cannot be undone by a stale worker. Privacy epochs prevent pre-deletion HTTP requests recreating caches.

Pre-AI transient failures retry with bounded attempts/backoff. Provider timeouts are not automatically retried; exactly-once external billing cannot be guaranteed across process crashes. Already-started provider requests cannot be recalled after consent withdrawal or key removal, but their results are not persisted after cancellation. App quotas complement each user's OpenAI project spend controls.

## Logging and reporting
Do not log request headers, OAuth callback query strings, HTTP bodies, repository excerpts, or AI prompts. The default HTTP client logging is quiet, access logging is disabled in the supplied Gunicorn command, and Django request exception logging is suppressed to avoid accidentally exposing provider details. This reduces diagnostics: monitor sanitized job state/error codes and infrastructure metrics; any error-reporting integration needs explicit scrubbing. User-visible errors are fixed messages, not raw upstream responses.

Operational rate counters contain a hashed user/IP namespace and timestamps, not repository data. Deploy behind a trusted proxy with edge abuse controls. The built-in anonymous rate limiter uses REMOTE_ADDR rather than trusting spoofable forwarded IPs; configure per-client throttling at the edge when many clients share a proxy address.

## Public sharing
There are no public portfolio routes or publish flags in this version. Future publication must use explicit selected analyses, an allowlisted projection, additional private-repository confirmation, revocation and cache invalidation, and dedicated security tests. Public GitHub visibility does not imply consent to publish an analysis.
