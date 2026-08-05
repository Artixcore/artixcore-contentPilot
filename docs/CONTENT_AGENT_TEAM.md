# Content Agent Team

ContentPilot now supports a workspace-scoped content intelligence workflow built around five coordinated agents:

- Ideator
- Hook & Script
- Planner
- Analyst
- DM Manager

The workflow can import owned and competitor Instagram post data through an allowlisted Apify actor, normalize and store the evidence, run the five AI agents, display draft artifacts in Streamlit, and send a compact Telegram report.

## Safety model

The feature is deliberately conservative:

- API credentials remain in environment variables or the deployment secret manager.
- Apify calls use a fixed HTTPS host, disabled redirects, bounded timeouts, and a 10 MB response limit.
- Actor IDs must be explicitly allowlisted.
- Instagram handles, post limits, intervals, URLs, and output sizes are validated.
- Raw Apify payloads are not stored. ContentPilot stores normalized records and a SHA-256 digest.
- Social captions and competitor content are treated as untrusted data, not instructions.
- AI output is stored as a draft and requires human review.
- The DM Manager creates playbooks only. It does not send messages.
- The agent team does not publish content automatically.
- All database records are scoped to the active workspace.
- Telegram recipients are explicit numeric chat IDs.

## Required environment variables

```env
# Apify
APIFY_API_TOKEN=
APIFY_ALLOWED_ACTOR_IDS=apify/instagram-scraper

# Telegram, optional
TELEGRAM_BOT_TOKEN=
TELEGRAM_REPORT_CHAT_IDS=

# Scheduled runner, set per scheduled job
CONTENTPILOT_WORKSPACE_ID=
```

A valid OpenAI or Anthropic provider must also be configured using the existing ContentPilot provider settings.

## Dashboard

Open the authenticated `Content Agent Team` Streamlit page. Configure:

1. Your Instagram handle.
2. Up to five competitor handles. Three to five is recommended.
3. An allowlisted Apify actor ID.
4. A bounded post count per profile.
5. Optional scheduling and Telegram reporting.

The dashboard displays validation and vulnerability alerts before the workflow runs.

## Manual workflow

Use the dashboard buttons in this order:

1. Sync Instagram data.
2. Run the five agents.
3. Review all draft artifacts.
4. Approve or adapt content using the existing ContentPilot publishing workflow.

The full-cycle button performs synchronization, agent execution, and Telegram reporting. It still does not publish content or send DMs.

## Scheduled workflow

Run one explicit workspace per scheduled job:

```bash
python scripts/run_content_agent_cycle.py --workspace-id 123
```

Example cron entry:

```cron
*/15 * * * * cd /app && python scripts/run_content_agent_cycle.py --workspace-id 123
```

The script checks the saved minimum interval and exits successfully when the cycle is not due. Use `--force` only for controlled operational testing.

## Operational checks

Before production use:

- Confirm database migrations complete successfully.
- Confirm the Apify actor input and output shape in a test workspace.
- Confirm provider generation succeeds for all five agents.
- Confirm Telegram reports reach only intended administrators.
- Review dependency and code-scanning alerts in CI.
- Keep human approval enabled for publishing and outbound replies.

No static review can prove that software has zero defects or vulnerabilities. Keep dependency scanning, secret scanning, access reviews, logging, and incident response active after deployment.
