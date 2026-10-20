# Operations

How to set up, deploy, run and roll back Tifa on Google Cloud. Everything here is reproducible from the repository
plus a gitignored `infra/.env`; no identifier of a real deployment is committed.

## A shared project

Tifa runs inside an existing Google Cloud project that may also host unrelated services. To keep them apart:

- **Names and labels.** Every resource Tifa creates is named `tifa-*` and, where the resource type has labels,
  labelled `app=tifa`. The names are fixed in `infra/lib.sh`, not settings.
- **Scoped deploys.** The CI identity (`tifa-deploy`) has no project-wide role. It may update the two Tifa services
  (`roles/run.developer` granted on each service), push to the `tifa-images` repository and run the services as
  their own accounts. It cannot create a service, change who may call one, or touch anything else. The first
  deploy, which creates the services, is therefore run by an operator: `infra/first-deploy.sh`.
- **A budget for Tifa alone.** The budget counts only resources labelled `app=tifa` in this project, so the other
  services' spending never trips Tifa's guard (and Tifa's budget says nothing about theirs).
- **Firestore.** Tifa uses the project's `(default)` database (the free tier applies only to that one).
  `infra/bootstrap.sh` creates it only when the project has none, and never changes a database it did not set up:
  it stops and asks for `--adopt-firestore` instead. Other databases are listed and left alone.
- **Its own Workload Identity pool**, `tifa-github`, trusted only for this repository's `main` branch.

What Tifa changes outside its own `tifa-*` resources, all additive, none of it ever removed by the scripts:

| Change | Why |
| --- | --- |
| Enables the APIs it uses (Cloud Run, Firestore, Scheduler, Secret Manager, Artifact Registry, IAM, STS, Monitoring, Logging, Pub/Sub, Billing Budgets, Resource Manager) | Already-enabled APIs are left as they are; nothing is disabled |
| Project-level roles for `tifa-api` (`datastore.viewer`) and `tifa-poller` (`datastore.user`, `monitoring.viewer`) | Firestore and Monitoring grant access per project, not per resource |
| Custom role `tifa_guard` (role IDs cannot contain hyphens), granted on `tifa-api` only | The guard reads and sets that one service's IAM policy |
| One named exclusion, `tifa-api-requests-below-5xx`, on the project's `_Default` log sink | Its filter matches only `tifa-api`'s request logs; other exclusions are untouched |
| The `(default)` Firestore database and index exemptions on Tifa's own collection groups | Only when the project had no database, or with `--adopt-firestore` |

If another service later uses Firestore in this project, note that the poller's `datastore.user` role reaches every
database in the project; an IAM condition on the database name can narrow it.

## What runs where

| Piece | What it is | Who can reach it |
| --- | --- | --- |
| `tifa-poller` | Cloud Run, 0.25 vCPU, 256 MiB, concurrency 1, at most 2 instances, 60 s timeout, gen1 | Cloud Scheduler and the budget push only (IAM); never public |
| `tifa-api` | Cloud Run, 1 vCPU, 512 MiB, concurrency 40, 30 s timeout, at most 1 instance, gen1, startup CPU boost | Everyone (`allUsers` invoker), until the guard closes it |
| `tifa-poll` | Cloud Scheduler job, every minute (Etc/UTC), OIDC as `tifa-scheduler`, 2 retries 5-15 s apart, 40 s deadline (jobs have no labels) | |
| Firestore `(default)` | Native mode, delete protection on | `tifa-poller` reads and writes, `tifa-api` reads |
| `tifa-tfl-app-key` | Secret Manager secret, mounted into the poller as `TFL_APP_KEY` at a pinned version | `tifa-poller` |
| `tifa-budget-alerts` | Pub/Sub topic; subscription `tifa-budget-alerts-to-poller` pushes (OIDC as `tifa-pubsub-push`) to the poller's `/internal/budget` | Cloud Billing publishes |
| `tifa-images` | Artifact Registry Docker repository; keeps the 3 newest images | `tifa-deploy` writes |
| `tifa-budget-PROJECT_ID` | Monthly budget on the billing account, filtered to this project and the label `app=tifa` | Billing account administrators |

Both services run the same image; `TIFA_ROLE` picks the app. Their sizes, environment and labels are defined once,
in `infra/lib.sh`, and used by both `infra/first-deploy.sh` and the Deploy workflow (see `src/tifa/config.py` for
every setting and its default). Both use ingress `all`; IAM decides who may call them.

Identities, each with only what it needs:

| Service account | Roles |
| --- | --- |
| `tifa-deploy` | `run.developer` on `tifa-api` and on `tifa-poller` (not the project), `artifactregistry.writer` on `tifa-images`, `iam.serviceAccountUser` on `tifa-api` and `tifa-poller`; impersonated from GitHub Actions through the `tifa-github` pool, from `main` of this repository only |
| `tifa-api` | `datastore.viewer` |
| `tifa-poller` | `datastore.user`, `monitoring.viewer`, `secretmanager.secretAccessor` on `tifa-tfl-app-key`, `tifa_guard` (get and set the IAM policy) on `tifa-api` only |
| `tifa-scheduler` | `run.invoker` on `tifa-poller` |
| `tifa-pubsub-push` | `run.invoker` on `tifa-poller` |

## The scripts

Three scripts in `infra/`, run from a clone with `gcloud` signed in as a project owner:

| Script | Does |
| --- | --- |
| `bootstrap.sh` | Everything that does not need the services: APIs, Firestore, Artifact Registry, service accounts and roles, Workload Identity, the secret, the topic, the Scheduler job (paused) |
| `first-deploy.sh` | Builds the image (linux/amd64, `docker buildx`), creates both services, grants CI `run.developer` on them |
| `post-deploy.sh` | Invokers, the guard's role, public access to the API, the Scheduler target, the budget push, the budget, the log exclusion, then smoke tests |

All three:

- refuse to run without `infra/.env` (copy `infra/env.example`), and read settings from that file only, so a
  project ID exported in your shell for other work is never used; unknown settings are an error;
- print every command that changes something, quoted and ready to copy;
- take `--dry-run`, which shows those commands without running them (the read-only checks still run, so a dry run
  needs `gcloud` signed in);
- are idempotent: they create what is missing and bring Tifa's settings back in line, so run them again whenever
  you like;
- never print a secret or a token, never delete a resource, and work with the bash 3.2 that macOS ships.

`tests/test_infra.py` runs them against fake `gcloud`, `docker` and `curl` commands to check these rules.

## First-time setup

Prerequisites: `gcloud` signed in as an owner of the project (and as an administrator of its billing account, for
the budget), `jq`, `curl`, `docker` with `buildx`, and admin rights on the GitHub repository.

1. **Settings.** `cp infra/env.example infra/.env` and fill it in. `infra/.env` is gitignored; never commit it.
   The repository owner's numeric ID comes from `gh api repos/OWNER/REPO --jq .owner.id`.
2. **Bootstrap.** `infra/bootstrap.sh --dry-run` to review, then `infra/bootstrap.sh` (add `--github` to set the
   repository secrets and variables with `gh`). It never pauses or resumes an existing Scheduler job.
3. **GitHub.** In the repository settings:
   - Actions secrets: `GCP_PROJECT_ID`, `GCP_WIF_PROVIDER`, `GCP_DEPLOY_SA`. None is a credential, but as secrets
     they are masked in the logs of a public repository. `GITLEAKS_PRIVATE_TERMS` (see "Secret scanning").
   - Actions variables: `GCP_REGION`, `TIFA_DEPLOY=true` (deployment is skipped without it). Optional, with the same names and
     meanings as in `infra/.env`: `TFL_APP_KEY_VERSION` (see below), `TIFA_PUBLIC_SOURCES` (`a`; see "TfL's FOI
     data"), `TIFA_ALLOWED_HOSTS` (custom domains for the MCP endpoint), `TIFA_POLLER_INGRESS` (`all` by default;
     `internal` also hides the poller from the internet).
   - Optional Actions secret: `GITLEAKS_PRIVATE_TERMS` (see "Secret scanning").
   - An environment named `production`, limited to the `main` branch.
   - Branch protection on `main` requiring the CI checks; secret scanning with push protection; Dependabot alerts;
     private vulnerability reporting.
4. **First deploy.** `infra/first-deploy.sh` builds the image from the current commit, pushes it to `tifa-images`
   and creates both services, private. It refuses uncommitted changes to the image's files unless
   `--allow-dirty`; `--image=REGION-docker.pkg.dev/PROJECT_ID/tifa-images/tifa@sha256:...` deploys an image that
   is already there instead. Once both services exist it only re-checks CI's grant (`--redeploy` deploys again).
5. **Wiring.** `infra/post-deploy.sh`: invoker bindings for the Scheduler and the budget push, the guard role on
   `tifa-api`, `allUsers` on `tifa-api` (and never on the poller: it removes public access from the poller if
   anyone added it), the Scheduler target, the push subscription, the budget, the log exclusion, then smoke tests.
   Run `infra/post-deploy.sh smoke` at any time for the smoke tests alone; they record no poll and change nothing.
6. **Start recording.** Import existing history first if there is any (see the tools), then
   `gcloud scheduler jobs resume tifa-poll --location=REGION`. The first poll within 15 minutes of the imported
   state's last poll continues its open episodes.

## Deploying

Every push to `main` runs `.github/workflows/deploy.yml`:

1. the full CI (lint, tests, emulator tests, image build, secret scan);
2. keyless authentication as `tifa-deploy` (Workload Identity Federation; the provider accepts only this
   repository, by owner ID and name, on `refs/heads/main`);
3. a check that both services exist and that it may update them, before anything is built (if not, the job fails
   and says to run `infra/first-deploy.sh`);
4. one `linux/amd64` image, pushed to `tifa-images` and tagged with the commit;
5. `tifa-poller`, then `tifa-api`, deployed from that image by digest with the settings in `infra/lib.sh`, with
   traffic sent to the new revisions.

Deploys never change who may invoke a service: `tifa-api` stays open or closed as it was, and the deploy identity
could not change it anyway. Only one deploy runs at a time; a newer push waits for the running one.

Note: workflow logs of a public repository are public. They show the project ID, the service accounts and the
service URLs. None of these is a credential (access is controlled by IAM and the Workload Identity condition),
but do not paste anything else from `infra/.env` into workflow logs, issues or pull requests.

## Secret scanning

gitleaks runs on every commit (pre-commit) and over the full history in CI, with the default credential rules plus
the custom rules in `.gitleaks.toml`: billing account IDs, service account and other email addresses, TfL app keys,
private and link-local IPv4, IPv6 unique local addresses, tailnet host names, home directory paths, Cloud Run URLs,
and Google Cloud project IDs and numbers in resource names, image paths and settings. Use placeholders instead:
`your-project-id`, `example-*` or `demo-*` (emulator) project IDs, `example.com` addresses, and the documentation
address ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, `2001:db8::/32`).

On pull requests, CI also scans with the base branch's rules and ignore list, and fails if the change adds an
inline allow comment, so a pull request cannot quietly loosen the scan (changes to the workflows still need review).

**Private terms.** Names that must never appear in the repository (people, hosts, private deployments) are not
listed in the public rules, since a rule would publish them. Keep them in one regular expression, such as
`term1|term2` (matched case-insensitively):

- CI: a repository secret `GITLEAKS_PRIVATE_TERMS` (Settings > Secrets and variables > Actions > Secrets). Runs
  without it, such as forks and Dependabot, warn and skip that scan. A finding is reported as a count only.
- Locally: a gitleaks config outside the repository that extends this one, used by the pre-commit hook and by a
  full scan when `GITLEAKS_CONFIG` points at it (run from the repository root):

  ```toml
  [extend]
  path = ".gitleaks.toml"

  [[rules]]
  id = "private-term"
  description = "A private term"
  regex = '''(?i)(?:term1|term2)'''
  ```

  ```sh
  export GITLEAKS_CONFIG=/path/outside/the/repo/gitleaks-private.toml
  gitleaks git --redact --verbose .   # the full history; the pre-commit hook then checks staged changes too
  ```

## TfL app key

Until a key exists the poller calls TfL anonymously (`/healthz` reports `tfl_auth: anonymous`).

Add or rotate a key:

```sh
infra/bootstrap.sh tfl-key          # hidden prompt; the key goes to Secret Manager on stdin and is never printed
```

It prints the new version number. Set `TFL_APP_KEY_VERSION` to it (the repository variable, and `infra/.env` so
that a manual deploy agrees) and run the Deploy workflow: the poller mounts that exact version (never `latest`, so a
rotation is a reviewed, reversible deploy). Once the new revision is healthy, disable the old version:

```sh
gcloud secrets versions disable OLD_VERSION --secret=tifa-tfl-app-key
```

To roll back a bad key, set `TFL_APP_KEY_VERSION` back (re-enable that version first if you disabled it) and
redeploy. If TfL rejects the key, the poller makes one anonymous request instead and `/healthz` reports
`tfl_auth: degraded`; recording continues.

The key is never logged: the HTTP client's request logging is silenced and every log line is scrubbed of
`app_key=` values and of the key itself.

## TfL's FOI data

TfL's freedom-of-information release of its 2020-2025 disruption records does not grant permission to re-use the
data for publishing. So Tifa publishes only its own recording (source `a`): `TIFA_PUBLIC_SOURCES` defaults to `a`
everywhere (settings, `infra/.env`, the Deploy workflow), and the scripts warn if it includes `f`.
`tools/import_foi.py` refuses to run unless given `--i-have-reuse-permission`. Set `a,f` only after TfL has
granted re-use in writing.

## Fair use, the guard and the budget

Three layers keep the monthly cost bounded; recording never stops in any of them.

1. **In the API** (per instance, in memory): a token bucket per client IP (the right-most `X-Forwarded-For` entry,
   which Google's front end sets; IPv6 grouped by /64), a global request rate, a cap on uncached computations and
   an hourly egress cap. Over a limit: 429 or 503 with `Retry-After`.
2. **The guard** (in the poller, at most every 2 minutes): reads the API's request count from Cloud Monitoring and
   closes the public API by removing `allUsers` from its invoker binding:
   - more than 9,000 requests in 5 minutes: closed for 30 minutes, doubling on repeats up to 24 hours;
   - more than 36,000 requests in an hour: closed for 2 hours;
   - estimated month-to-date API cost over $2: closed until the 1st of next month.
   It reopens the API when the closure ends. Its state is in Firestore at `guard/state`. Thresholds can be lowered
   for a dry run with the `TIFA_GUARD_*` variables and `TIFA_GUARD_MODE=dry-run` (decide and log only).
3. **The budget** (hours of lag): a monthly budget, filtered to this project and the label `app=tifa`, with emails
   to the billing account's admins at 50%, 90% and 100% of actual spend and 100% of forecast spend. Every
   notification is also pushed to the poller; at 100% of actual spend it closes the public API until the 1st.
   Costs of resources without labels (the Scheduler job, Firestore, Logging, Monitoring) do not count towards it:
   expected about $0.10 a month, as the rest sits in free tiers.

While the API is closed it answers 403 to everyone. Nothing is deleted and billing is never detached.

To close or reopen by hand, use `tools/guard_cli.py` (it also records the decision in the guard's state).
`infra/post-deploy.sh` adds `allUsers` back too, but the guard closes the API again on its next run while its
closure lasts; run `infra/post-deploy.sh --keep-closed` to re-run the wiring without touching the binding.

## Rolling back

Cloud Run keeps earlier revisions. To send all traffic back to the previous one:

```sh
gcloud run revisions list --service=tifa-api --region=REGION
gcloud run services update-traffic tifa-api --region=REGION --to-revisions=REVISION=100
```

(and the same for `tifa-poller`). The next deploy from `main` moves traffic to the new revision again, so fix
forward or revert the commit. Artifact Registry keeps the 3 newest images, so revisions older than that may no
longer start; redeploy an older commit (revert and merge) instead.

Pause and resume recording:

```sh
gcloud scheduler jobs pause tifa-poll --location=REGION
gcloud scheduler jobs resume tifa-poll --location=REGION
```

Polls missed for up to 15 minutes do not break episodes; after a longer gap open episodes end at the last poll with
`end_estimated: true`, and the gap shows in `/v1/coverage`.

## Costs

Expected, in USD a month, with the free tiers already used by other projects on the billing account:

| Item | Expected |
| --- | --- |
| Poller: 43,800 polls of about 3.5 s at 0.25 vCPU | 1.06 |
| API: about 100,000 requests | 0.36 |
| Cloud Scheduler: 1 job | 0.10 |
| Egress, Artifact Registry, Secret Manager | 0.17 |
| Firestore, Logging, Monitoring, Pub/Sub (per-project free tiers) | 0 |
| **Total** | **about 1.70** |

Firestore, Logging and Monitoring free tiers are per project, so another service in the same project shares them;
Tifa's own use (about 3,000 writes and 5,000 reads a day) is well under them.

The worst case under abuse is held near the budget by the three layers above. What keeps it low:

- Cloud Run bills per request (`--cpu-throttling`), scales to zero, and the API has one instance at most.
- The API serves from memory, so Firestore reads do not grow with traffic; there are no composite indexes, and
  blobs and arrays are exempt from indexing (`infra/bootstrap.sh` lists them).
- API request logs below HTTP 500 are excluded from Cloud Logging (cheaper, and no client IPs in logs).
- The image is small (Python slim base, about 70 MB) and Artifact Registry keeps 3 images.

## Troubleshooting

| Symptom | Look at |
| --- | --- |
| `/healthz` 503 | The last poll is over 10 minutes old: is the Scheduler job enabled? Poller logs: `gcloud run services logs read tifa-poller --region=REGION` |
| Scheduler attempts fail with 401 or 403 | `infra/post-deploy.sh` (invoker binding, target URL and audience) |
| Scheduler attempts fail with 502 | TfL was unreachable or sent a bad payload; the slot is recorded as failed and retried within the minute |
| API answers 403 to everyone | Closed by the guard or the budget (see `guard/state`), or `infra/post-deploy.sh` has not run yet |
| Deploy fails at authentication | The repository secrets, and the Workload Identity condition (owner ID, repository name, `main`) |
| Deploy fails at "Find the services" | The services do not exist yet, or `tifa-deploy` lacks `run.developer` on them: run `infra/first-deploy.sh` |
| `bootstrap.sh` stops at Firestore | The project has a `(default)` database Tifa did not set up: check who uses it, then `--adopt-firestore` |
| MCP clients get 421 or "invalid host" | Add the host to `TIFA_ALLOWED_HOSTS` and redeploy |

Behind a load balancer of your own, the client is no longer the right-most `X-Forwarded-For` entry; Tifa assumes
it is reached on its `run.app` URL or a Cloud Run domain mapping.
