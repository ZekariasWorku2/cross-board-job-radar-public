# Cross-Board Job Radar

Automated discovery, deduplication, original-publication verification and job matching.

## Privacy and data separation

This is the **public source-code repository**. It intentionally contains no personal
resume, email address, job search queries, SMTP credentials, job history or evaluation
reports. The production workflow retrieves its runtime configuration and persistent
state from a separately permission-restricted **private data repository**.

The production workflow is initially **manual-only**. Scheduled scans are not enabled
until the original private workflow is disabled and migration is explicitly approved.

## Workflows

- **Public Job Radar Unit Tests**: runs isolated tests using synthetic fixtures and
  never accesses personal data.
- **Cross-Board Job Radar (Private Data)**: manually executes the job search on
  `main`; the private data and Gmail credentials are available only to this
  trusted workflow. Results and diagnostics are committed back to the private
  data repository; detailed results are not exposed in public Actions logs.

## Local setup

For your own deployments, put your actual private `config.json`,
`profiles/data_engineer.json` and `state/state.json` locally,
then run `python run.py --dry-run`. The sample files under `tests/fixtures`
are fictitious, for unit testing only.

**Never commit** local configuration, resume profiles, GitHub access tokens,
Gmail app passwords, job history, or output reports into this public repository.
