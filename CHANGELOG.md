# Changelog

All notable changes to Tifa are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The REST API is versioned separately (`/v1`); breaking API changes get a new prefix.

## Unreleased

### Added

- Project scaffold: uv project for Python 3.12+, MIT licence, security policy.
- `tifa.config`: settings from environment variables with safe local defaults, Cloud Run defaults and validation;
  fair-use and guard limits overridable through `TIFA_*` variables.
- `tifa.main`: one entry point for both services; `TIFA_ROLE` picks the API or the poller, uvicorn serves it on
  `$PORT`, and client addresses come from the right-most `X-Forwarded-For` entry. JSON logs for Cloud Logging with
  app keys redacted.
- Container image: Python 3.13 slim pinned by digest, locked dependencies installed with uv, non-root user.
- CI: Ruff, pytest on Python 3.12 and 3.13, every Firestore emulator test in the suite, image build and start-up
  check, gitleaks over the full history, workflow and shell script linting.
- Deployment on push to `main`: keyless Workload Identity Federation, one image pushed to Artifact Registry and
  deployed as two Cloud Run services (`tifa-api`, public; `tifa-poller`, private).
- Daily TfL canary that checks the shape of TfL's line-status response and opens an issue when it changes.
- `infra/bootstrap.sh` and `infra/post-deploy.sh`: idempotent Google Cloud setup and post-deploy wiring (Firestore,
  Artifact Registry, service accounts, Workload Identity Federation, Secret Manager, Pub/Sub, Cloud Scheduler,
  budget, log exclusion) and smoke tests.
- Secret and privacy scanning with gitleaks (pre-commit and CI) using custom rules for this public repository
  (including Google Cloud project IDs and private network addresses); private terms come from a CI secret rather
  than the public rules, and pull requests are also scanned with the base branch's rules and ignore list.
- `docs/operations.md`: bootstrap, deploy, secrets rotation, guard and budget behaviour, rollback and costs.
