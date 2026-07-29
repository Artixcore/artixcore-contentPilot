# ContentPilot Product Architecture

## Runtime entrypoints

The complete product runs with:

```bash
streamlit run app_complete.py
```

The production container already uses this entrypoint. The background worker runs separately:

```bash
python -m workers.runner
```

## Tenant boundary

The hierarchy is:

```text
Organization
  Workspace
    Members and roles
    Brand profile and knowledge
    Campaigns and calendar
    Posts and approvals
    Publishing connections
    Leads and automations
    Analytics and usage
    Jobs, notifications, audit, and credentials
```

Every business and operational record carries a `workspace_id`. SQLAlchemy automatically applies the active workspace to reads and new writes. Flush-time guards reject updates or deletes when the model workspace differs from the bound session workspace. Worker processes claim jobs globally only long enough to identify a job, then reopen a workspace-bound session before executing it.

A session changing workspace clears its SQLAlchemy identity map and refuses to switch while writes are pending. This prevents cached objects from one workspace being reused after switching to another.

## Roles

Global account roles and workspace membership roles are both enforced. A global role cannot override a more restrictive workspace role.

Workspace roles:

- owner
- admin
- editor
- reviewer
- viewer

High-risk actions such as member administration, API keys, automation creation, OAuth connections, credential management, publishing, and approvals require the appropriate global and workspace permissions.

## Credentials and API keys

Application credentials are encrypted with rotating Fernet keys and associated context containing the workspace ID and credential name. Moving ciphertext to another workspace or another credential name causes integrity validation to fail.

Workspace API keys:

- are shown once
- use a fixed `cp_` prefix and random secret
- are stored only as HMAC-SHA256 hashes
- require a separate deployment pepper
- support explicit scopes and expiration
- use constant-time verification
- are rejected for inactive workspaces

## OAuth

OAuth connections use authorization code flow with PKCE.

Controls include:

- hashed one-time state
- encrypted PKCE verifier
- ten-minute state expiration
- initiating-user binding
- workspace binding
- exact stored redirect URI
- configurable provider endpoints
- HTTPS-only endpoints
- deployment hostname allowlists
- public-DNS checks
- no redirects during token exchange
- strict timeouts and response-size limits
- encrypted access and refresh tokens
- one-time state consumption
- refresh-token rotation support

Provider endpoints and scopes must be copied from current official provider documentation into deployment configuration. They are not trusted from user input.

## Campaigns and content

Campaigns support validated date ranges, platform selection, frequency, status, and calendar items. Calendar scheduling prevents duplicate workspace items for the same platform and timestamp. Reusable content templates are workspace scoped and archived instead of silently deleted.

Publishing remains human controlled. Only approved or scheduled posts can enter the publishing delivery queue. Brand Brain output always starts as `draft`.

## Lead intelligence

Lead scoring is deterministic and auditable. It considers contact completeness, buying intent, enterprise indicators, urgency, detail, support signals, and known spam patterns. It does not secretly call an AI provider.

Urgent leads create workspace notifications. Assignment is limited to active members of the same workspace.

## Automation

Automation supports fixed triggers, condition operators, and actions. It does not use `eval`, arbitrary Python, dynamic imports, or user-provided commands.

Controls include:

- validated JSON configuration
- allowlisted operators and actions
- event deduplication
- cooldowns
- tenant-bound targets
- savepoint isolation per rule
- staged jobs and notifications committed with the run
- approval checks before publishing
- execution history and sanitized failures

Outbound webhook automation additionally requires:

- a configured website integration
- exact endpoint matching
- HTTPS
- deployment hostname allowlist
- public DNS results
- HMAC-SHA256 signature
- no redirects
- strict timeout
- 256 KB request and response limits

## Brand Brain

Brand Brain stores workspace knowledge from pasted text and PDF, TXT, MD, CSV, or JSON uploads.

Ingestion controls include:

- safe filenames
- 10 MB upload limit
- PDF signature verification
- 500-page PDF limit
- UTF-8 validation
- 2,000,000-character extracted-text limit
- checksum deduplication

Retrieval is deterministic token matching. Retrieved documents are wrapped as untrusted reference material. The system prompt explicitly rejects instructions, role changes, secret requests, and prompt injection found inside documents.

Generated provider output must be structured JSON, respect platform limits, and is persisted only as a draft. Token usage is metered and the knowledge document IDs used for generation are audited.

## Analytics

Analytics aggregates content, campaigns, platform engagement, leads, worker reliability, usage, and estimated provider cost. Date ranges are bounded to prevent unbounded database scans. Rates handle zero denominators safely.

## Error handling and alerts

Application exceptions are mapped into structured user-safe errors. Transactions are rolled back before error presentation. Production responses hide internal reasons, metadata, stack traces, credentials, and provider payloads.

Critical operational failures can generate Telegram alerts with cooldown and duplicate suppression. Failed jobs enter a dead-letter state and create persistent in-app notifications.

## Required production gates

Before production traffic:

1. Use TLS-enabled PostgreSQL with `sslmode=require`, `verify-ca`, or `verify-full`.
2. Set `CONTENTPILOT_ENCRYPTION_KEYS` through a secret manager.
3. Set a separate `WORKSPACE_API_KEY_PEPPER` with at least 32 random characters.
4. Configure the first owner and default tenant.
5. Remove the plaintext bootstrap password after first startup.
6. Put Streamlit behind Cloudflare Access, VPN, private networking, or an authenticated reverse proxy.
7. Enable HTTPS and the supplied security headers.
8. Configure OAuth endpoints and host allowlists from current official documentation.
9. Configure `AUTOMATION_WEBHOOK_ALLOWED_HOSTS` only for approved destinations.
10. Run Product CI, CodeQL, dependency auditing, PostgreSQL schema verification, and the container smoke test.
11. Verify encrypted backups and perform a restoration test.
12. Perform staging access-control and tenant-isolation testing.

No engineering process can responsibly promise zero vulnerabilities or zero defects. Deployment approval depends on successful automated checks, staging verification, dependency review, and operational monitoring.
