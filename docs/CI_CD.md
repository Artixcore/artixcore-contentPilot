# ContentPilot CI/CD

## Continuous integration

The `CI` workflow runs on pull requests to `master`, pushes to `master` or `agent/**`, and manual workflow dispatches.

It verifies:

- Python 3.11 and 3.12 compatibility
- dependency consistency
- source compilation
- likely runtime errors with Ruff
- the complete Pytest suite
- PostgreSQL initialization and schema health
- Bandit medium and high severity findings
- dependency vulnerabilities with pip-audit
- Docker image construction
- non-root container startup
- Streamlit health at `/_stcore/health`

A failed job blocks the container smoke test and should block merging through branch protection.

## Continuous delivery

The `Release Container` workflow runs after pushes to `master`, version tags beginning with `v`, or manual dispatch.

It repeats release verification before publishing. A successful release publishes:

```text
ghcr.io/artixcore/artixcore-contentpilot
```

Published images include immutable SHA tags, version tags, an SBOM, build provenance, and a registry attestation.

Publishing to GitHub Container Registry uses the repository's `GITHUB_TOKEN`. No external deployment credentials are required.

## Recommended repository settings

Configure the `master` branch protection rule to require these checks before merging:

- Python 3.11 tests
- Python 3.12 tests
- PostgreSQL integration
- Security scans
- Container build and smoke test
- CodeQL

Also enable:

- Require a pull request before merging
- Require branches to be up to date
- Require conversation resolution
- Block force pushes
- Block branch deletion
- Dependabot alerts and security updates
- Secret scanning and push protection

## Running CI manually

Open GitHub, choose **Actions**, select **CI**, and choose **Run workflow**. Select the branch to test.

## Release process

1. Merge a fully passing pull request into `master`.
2. The release workflow verifies the source again.
3. GitHub publishes the container to GHCR.
4. Deploy the immutable `sha-...` image digest to staging.
5. Promote the same digest to production after health and security checks.

Do not deploy `latest` as the only production reference. Use an immutable digest or SHA tag so rollback remains deterministic.
