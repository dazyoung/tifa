#!/usr/bin/env bash
# Tifa: one-time Google Cloud setup. Idempotent: it creates what is missing, brings settings back in line,
# never deletes anything and never prints a secret. Run it again whenever you like.
#
#   infra/bootstrap.sh              set up the project described by infra/.env
#   infra/bootstrap.sh --github     ... and set the GitHub repository variables with the gh CLI
#   infra/bootstrap.sh tfl-key      add a TfL app key to Secret Manager from a hidden prompt (or stdin)
#
# Needs gcloud, signed in as a project owner (gcloud auth login). Settings: infra/env.example.
# The services do not exist yet: infra/post-deploy.sh does the wiring that needs them, after the first deploy.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ENV_FILE=${TIFA_ENV_FILE:-$here/.env}

APIS=(
  run.googleapis.com firestore.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com
  artifactregistry.googleapis.com iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com
  monitoring.googleapis.com logging.googleapis.com pubsub.googleapis.com cloudbilling.googleapis.com
  billingbudgets.googleapis.com cloudresourcemanager.googleapis.com
)
# Collection group and field: blobs, arrays and maps that are never queried, so they are not indexed.
UNINDEXED=(
  "meta status" "meta open" "meta lines" "meta months" "meta run"
  "months payload" "snapshots payload" "polls at" "polls failed"
)
GUARD_ROLE=tifaGuard
GUARD_PERMISSIONS=run.services.get,run.services.getIamPolicy,run.services.setIamPolicy
SECRET_NAME=tfl-app-key
BUDGET_TOPIC=budget-alerts
EXCLUSION=tifa-api-requests-below-5xx

step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
# Show a command (quoted, ready to copy), then run it. Commands carry names and IDs, never secrets.
show() { printf '    $'; printf ' %q' "$@"; printf '\n'; }
run() { show "$@"; "$@"; }
# The same without stdout: IAM commands print the whole policy, members' emails included.
run_quiet() { show "$@"; "$@" >/dev/null; }
# New service accounts take a few seconds to become usable in IAM policies.
retry() {
  local attempt=1
  until "$@"; do
    [ "$attempt" -ge 5 ] && return 1
    warn "retrying in $((attempt * 5)) s"
    sleep $((attempt * 5))
    attempt=$((attempt + 1))
  done
}

# KEY=value lines, comments and blank lines; nothing is executed. The environment overrides the file.
load_env() {
  local line key value
  [ -f "$ENV_FILE" ] || die "no $ENV_FILE: copy infra/env.example to infra/.env and fill it in"
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
    [[ $line =~ ^[[:space:]]*([A-Z][A-Z0-9_]*)=(.*)$ ]] || die "$ENV_FILE: cannot read a line (expected KEY=value)"
    key=${BASH_REMATCH[1]}
    value=${BASH_REMATCH[2]}
    value=${value%%[[:space:]]#*}
    value=${value%"${value##*[![:space:]]}"}
    if [[ $value == \"*\" || $value == \'*\' ]]; then value=${value:1:${#value}-2}; fi
    [ -n "${!key+set}" ] || printf -v "$key" '%s' "$value"
  done <"$ENV_FILE"
}

require() {
  local name missing=""
  for name in "$@"; do
    [ -n "${!name:-}" ] || missing="$missing $name"
  done
  [ -z "$missing" ] || die "set$missing in $ENV_FILE"
}

settings() {
  load_env
  require GCP_PROJECT_ID GCP_REGION
  AR_REPOSITORY=${AR_REPOSITORY:-tifa}
  API_SERVICE=${API_SERVICE:-tifa-api}
  POLLER_SERVICE=${POLLER_SERVICE:-tifa-poller}
  SCHEDULER_JOB=${SCHEDULER_JOB:-tifa-poll}
  WIF_POOL=${WIF_POOL:-github}
  WIF_PROVIDER=${WIF_PROVIDER:-github-actions}
  [[ $GCP_PROJECT_ID =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || die "GCP_PROJECT_ID is not a project ID"
  [[ $GCP_PROJECT_ID != your-project-id ]] || die "GCP_PROJECT_ID is still the placeholder"
  [[ $GCP_REGION =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || die "GCP_REGION is not a region"
  command -v gcloud >/dev/null || die "gcloud is not installed"
  export CLOUDSDK_CORE_PROJECT=$GCP_PROJECT_ID CLOUDSDK_CORE_DISABLE_PROMPTS=1
  sa() { printf '%s@%s.iam.gserviceaccount.com' "$1" "$GCP_PROJECT_ID"; }
  DEPLOY_SA=$(sa tifa-deploy)
  API_SA=$(sa tifa-api)
  POLLER_SA=$(sa tifa-poller)
  SCHEDULER_SA=$(sa tifa-scheduler)
  PUSH_SA=$(sa tifa-pubsub-push)
}

project_number() {
  [ -n "$(gcloud auth list --filter=status:ACTIVE --format='value(account)')" ] \
    || die "gcloud has no active account: run gcloud auth login"
  PROJECT_NUMBER=$(gcloud projects describe "$GCP_PROJECT_ID" --format='value(projectNumber)') \
    || die "cannot read project $GCP_PROJECT_ID (does it exist, and may this account see it?)"
}

enable_apis() {
  step "Enable APIs"
  run gcloud services enable "${APIS[@]}"
  [ "$(gcloud billing projects describe "$GCP_PROJECT_ID" --format='value(billingEnabled)')" = True ] \
    || die "the project has no billing account linked: link one, then re-run"
  info "billing is enabled"
}

firestore() {
  step "Firestore (default) database, Native mode, in $FIRESTORE_LOCATION"
  local found
  if found=$(gcloud firestore databases describe --database='(default)' --format='value(type,locationId)' 2>/dev/null); then
    info "exists: $found"
    [[ $found == FIRESTORE_NATIVE* ]] || die "the (default) database is not in Native mode"
    [[ $found == *"$FIRESTORE_LOCATION" ]] || warn "the database is not in $FIRESTORE_LOCATION (a location is fixed once created)"
  else
    run gcloud firestore databases create --database='(default)' --location="$FIRESTORE_LOCATION" \
      --type=firestore-native --delete-protection
  fi

  step "Firestore single-field index exemptions"
  local pair group field inherits
  for pair in "${UNINDEXED[@]}"; do
    group=${pair%% *}
    field=${pair#* }
    inherits=$(gcloud firestore indexes fields describe "$field" --collection-group="$group" --database='(default)' \
      --format='value(indexConfig.usesAncestorConfig)' 2>/dev/null || true)
    if [ "$inherits" = False ]; then
      info "$group.$field: not indexed"
    else
      run gcloud firestore indexes fields update "$field" --collection-group="$group" --database='(default)' \
        --disable-indexes
    fi
  done
}

artifact_registry() {
  step "Artifact Registry repository $AR_REPOSITORY (keeps the 3 newest images)"
  if gcloud artifacts repositories describe "$AR_REPOSITORY" --location="$GCP_REGION" >/dev/null 2>&1; then
    info "exists"
  else
    run gcloud artifacts repositories create "$AR_REPOSITORY" --repository-format=docker \
      --location="$GCP_REGION" --description="Tifa container images"
  fi
  local policy
  policy=$(mktemp)
  cat >"$policy" <<'JSON'
[
  {"name": "keep-newest-3", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 3}},
  {"name": "delete-older", "action": {"type": "Delete"}, "condition": {"tagState": "any", "olderThan": "1d"}}
]
JSON
  run_quiet gcloud artifacts repositories set-cleanup-policies "$AR_REPOSITORY" --location="$GCP_REGION" \
    --policy="$policy"
  rm -f "$policy"
}

service_accounts() {
  step "Service accounts"
  local id name
  while IFS='|' read -r id name; do
    if gcloud iam service-accounts describe "$(sa "$id")" >/dev/null 2>&1; then
      info "$id exists"
    else
      run gcloud iam service-accounts create "$id" --display-name="$name"
    fi
  done <<'LIST'
tifa-deploy|Tifa deploy (GitHub Actions)
tifa-api|Tifa public API
tifa-poller|Tifa poller and guard
tifa-scheduler|Tifa Cloud Scheduler invoker
tifa-pubsub-push|Tifa budget alerts push
LIST
}

guard_role() {
  step "Custom role $GUARD_ROLE (the guard opens and closes the public API)"
  local deleted
  if deleted=$(gcloud iam roles describe "$GUARD_ROLE" --project="$GCP_PROJECT_ID" --format='value(deleted)' 2>/dev/null); then
    [ "$deleted" != True ] || run_quiet gcloud iam roles undelete "$GUARD_ROLE" --project="$GCP_PROJECT_ID"
    run_quiet gcloud iam roles update "$GUARD_ROLE" --project="$GCP_PROJECT_ID" --permissions="$GUARD_PERMISSIONS"
  else
    run_quiet gcloud iam roles create "$GUARD_ROLE" --project="$GCP_PROJECT_ID" --title="Tifa guard" \
      --description="Read and set the IAM policy of the public Tifa service" \
      --permissions="$GUARD_PERMISSIONS" --stage=GA
  fi
  info "bound to $API_SERVICE only, by infra/post-deploy.sh once the service exists"
}

iam() {
  step "IAM: least privilege for each identity"
  local binding member role
  while read -r member role; do
    retry run_quiet gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" --member="$member" --role="$role" \
      --condition=None
  done <<LIST
serviceAccount:$API_SA roles/datastore.viewer
serviceAccount:$POLLER_SA roles/datastore.user
serviceAccount:$POLLER_SA roles/monitoring.viewer
serviceAccount:$DEPLOY_SA roles/run.developer
LIST
  for binding in "$API_SA" "$POLLER_SA"; do  # the deploy identity may run the services as their own accounts
    retry run_quiet gcloud iam service-accounts add-iam-policy-binding "$binding" \
      --member="serviceAccount:$DEPLOY_SA" --role=roles/iam.serviceAccountUser
  done
  retry run_quiet gcloud artifacts repositories add-iam-policy-binding "$AR_REPOSITORY" --location="$GCP_REGION" \
    --member="serviceAccount:$DEPLOY_SA" --role=roles/artifactregistry.writer
}

secret() {
  step "Secret Manager: $SECRET_NAME (the TfL app key; the poller reads it)"
  if gcloud secrets describe "$SECRET_NAME" >/dev/null 2>&1; then
    info "exists"
  else
    run gcloud secrets create "$SECRET_NAME" --replication-policy=automatic --labels=app=tifa
  fi
  retry run_quiet gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
    --member="serviceAccount:$POLLER_SA" --role=roles/secretmanager.secretAccessor
  local enabled
  enabled=$(gcloud secrets versions list "$SECRET_NAME" --filter=state=ENABLED --format='value(name)' | wc -l | tr -d ' ')
  if [ "$enabled" = 0 ]; then
    info "no key yet: the poller calls TfL anonymously until one is added (infra/bootstrap.sh tfl-key)"
  else
    info "$enabled enabled version(s); the deploy uses the one in the TFL_APP_KEY_VERSION variable"
  fi
}

pubsub() {
  step "Pub/Sub topic $BUDGET_TOPIC (budget notifications for the poller)"
  if gcloud pubsub topics describe "$BUDGET_TOPIC" >/dev/null 2>&1; then
    info "exists"
  else
    run gcloud pubsub topics create "$BUDGET_TOPIC" --labels=app=tifa
  fi
  # Pub/Sub signs the push requests' OIDC tokens as the push identity.
  gcloud beta services identity create --service=pubsub.googleapis.com >/dev/null 2>&1 \
    || warn "could not confirm the Pub/Sub service agent (it is usually created with the API)"
  retry run_quiet gcloud iam service-accounts add-iam-policy-binding "$PUSH_SA" \
    --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com" \
    --role=roles/iam.serviceAccountTokenCreator
}

workload_identity() {
  step "Workload Identity Federation: GitHub Actions on $GITHUB_REPOSITORY, main branch only"
  local state condition mapping principal
  state=$(gcloud iam workload-identity-pools describe "$WIF_POOL" --location=global --format='value(state)' 2>/dev/null || true)
  case $state in
    "") run gcloud iam workload-identity-pools create "$WIF_POOL" --location=global --display-name="GitHub Actions" ;;
    DELETED) run gcloud iam workload-identity-pools undelete "$WIF_POOL" --location=global ;;
    *) info "pool $WIF_POOL exists" ;;
  esac
  mapping="google.subject=assertion.sub,attribute.repository=assertion.repository"
  mapping="$mapping,attribute.repository_owner_id=assertion.repository_owner_id,attribute.ref=assertion.ref"
  condition="assertion.repository_owner_id == '$GITHUB_REPOSITORY_OWNER_ID'"
  condition="$condition && assertion.repository == '$GITHUB_REPOSITORY' && assertion.ref == 'refs/heads/main'"
  state=$(gcloud iam workload-identity-pools providers describe "$WIF_PROVIDER" --location=global \
    --workload-identity-pool="$WIF_POOL" --format='value(state)' 2>/dev/null || true)
  case $state in
    "")
      run_quiet gcloud iam workload-identity-pools providers create-oidc "$WIF_PROVIDER" --location=global \
        --workload-identity-pool="$WIF_POOL" --display-name="GitHub repository" \
        --issuer-uri=https://token.actions.githubusercontent.com \
        --attribute-mapping="$mapping" --attribute-condition="$condition"
      ;;
    *)
      [ "$state" != DELETED ] || run_quiet gcloud iam workload-identity-pools providers undelete "$WIF_PROVIDER" \
        --location=global --workload-identity-pool="$WIF_POOL"
      run_quiet gcloud iam workload-identity-pools providers update-oidc "$WIF_PROVIDER" --location=global \
        --workload-identity-pool="$WIF_POOL" --issuer-uri=https://token.actions.githubusercontent.com \
        --attribute-mapping="$mapping" --attribute-condition="$condition"
      ;;
  esac
  info "condition: $condition"
  principal="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global"
  principal="$principal/workloadIdentityPools/$WIF_POOL/attribute.repository/$GITHUB_REPOSITORY"
  retry run_quiet gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" \
    --member="$principal" --role=roles/iam.workloadIdentityUser
}

scheduler() {
  step "Cloud Scheduler job $SCHEDULER_JOB: every minute (UTC), OIDC, created paused"
  # Cloud Run's deterministic URL is known before the first deploy; post-deploy.sh re-checks it.
  local url state
  url="https://$POLLER_SERVICE-$PROJECT_NUMBER.$GCP_REGION.run.app"
  local flags=(
    --location="$GCP_REGION" --schedule="* * * * *" --time-zone=Etc/UTC
    --uri="$url/internal/poll" --http-method=POST
    --oidc-service-account-email="$SCHEDULER_SA" --oidc-token-audience="$url"
    --attempt-deadline=40s --max-retry-attempts=2 --min-backoff=5s --max-backoff=15s --max-doublings=1
    --description="Tifa: record TfL line status"
  )
  state=$(gcloud scheduler jobs describe "$SCHEDULER_JOB" --location="$GCP_REGION" --format='value(state)' 2>/dev/null || true)
  if [ -z "$state" ]; then
    run_quiet gcloud scheduler jobs create http "$SCHEDULER_JOB" "${flags[@]}"
    run_quiet gcloud scheduler jobs pause "$SCHEDULER_JOB" --location="$GCP_REGION"
    state=PAUSED
  else
    run_quiet gcloud scheduler jobs update http "$SCHEDULER_JOB" "${flags[@]}"  # keeps its paused/enabled state
  fi
  info "state: $state (resume it once the first import is done: see docs/operations.md)"
}

log_exclusion() {
  step "Logging: drop $API_SERVICE request logs below HTTP 500 (cost, and no client IPs in logs)"
  local filter spec existing
  filter="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$API_SERVICE\""
  filter="$filter AND log_id(\"run.googleapis.com/requests\") AND httpRequest.status<500"
  spec="name=$EXCLUSION,description=Tifa API requests below 5xx,filter=$filter"
  existing=$(gcloud logging sinks describe _Default --format='value(exclusions[].name)' 2>/dev/null || true)
  if [[ ";$existing;" == *";$EXCLUSION;"* ]]; then
    run_quiet gcloud logging sinks update _Default --update-exclusion="$spec"
  else
    run_quiet gcloud logging sinks update _Default --add-exclusion="$spec"
  fi
}

summary() {
  local provider="projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$WIF_POOL/providers/$WIF_PROVIDER"
  step "Done. GitHub repository variables for $GITHUB_REPOSITORY (Settings > Secrets and variables > Actions > Variables):"
  local vars=(
    "GCP_PROJECT_ID=$GCP_PROJECT_ID" "GCP_REGION=$GCP_REGION" "GCP_WIF_PROVIDER=$provider"
    "GCP_DEPLOY_SA=$DEPLOY_SA" "TIFA_API_SA=$API_SA" "TIFA_POLLER_SA=$POLLER_SA"
  )
  local pair
  for pair in "${vars[@]}"; do
    info "$pair"
    if [ "$SET_GITHUB" = 1 ]; then
      gh variable set "${pair%%=*}" --repo "$GITHUB_REPOSITORY" --body "${pair#*=}" >/dev/null
    fi
  done
  if [ "$SET_GITHUB" = 1 ]; then info "(set with gh)"; fi
  cat <<EOF

Next:
  1. Create the GitHub environment "production" (deployments only from main).
  2. Push to main (or run the Deploy workflow) for the first deploy.
  3. Run infra/post-deploy.sh to wire the services, create the budget and run the smoke tests.
  4. When a TfL app key is available: infra/bootstrap.sh tfl-key, then set TFL_APP_KEY_VERSION and redeploy.
EOF
}

setup() {
  settings
  require FIRESTORE_LOCATION GITHUB_REPOSITORY GITHUB_REPOSITORY_OWNER_ID
  [[ $GITHUB_REPOSITORY =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "GITHUB_REPOSITORY must be owner/repo"
  [[ $GITHUB_REPOSITORY != owner/repo ]] || die "GITHUB_REPOSITORY is still the placeholder"
  [[ $GITHUB_REPOSITORY_OWNER_ID =~ ^[1-9][0-9]*$ ]] || die "GITHUB_REPOSITORY_OWNER_ID must be the owner's numeric ID"
  if [ "$SET_GITHUB" = 1 ]; then command -v gh >/dev/null || die "--github needs the gh CLI"; fi
  project_number
  info "project $GCP_PROJECT_ID, region $GCP_REGION"
  enable_apis
  firestore
  artifact_registry
  service_accounts
  guard_role
  iam
  secret
  pubsub
  workload_identity
  scheduler
  log_exclusion
  summary
}

# Reads the key without echoing it and sends it to Secret Manager on stdin; it never reaches a command line.
tfl_key() {
  settings
  local key version
  if [ -t 0 ]; then
    printf 'TfL app key (input hidden): ' >&2
    IFS= read -rs key || true
    printf '\n' >&2
  else
    IFS= read -r key || true
  fi
  key=${key//[[:space:]]/}
  if ! [[ $key =~ ^[A-Za-z0-9._~-]+$ ]] || [ "${#key}" -lt 8 ] || [ "${#key}" -gt 256 ]; then
    key=""
    die "that does not look like a TfL app key; nothing was stored"
  fi
  gcloud secrets describe "$SECRET_NAME" >/dev/null 2>&1 || die "no secret $SECRET_NAME: run infra/bootstrap.sh first"
  version=$(printf '%s' "$key" | gcloud secrets versions add "$SECRET_NAME" --data-file=- --format='value(name)')
  key=""
  version=${version##*/}
  info "stored as version $version of $SECRET_NAME (the key was not printed)"
  info "next: set the GitHub variable TFL_APP_KEY_VERSION=$version and redeploy"
  info "then disable the version it replaces: gcloud secrets versions disable OLD --secret=$SECRET_NAME"
}

usage() { sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

SET_GITHUB=0
case "${1:-}" in
  "") setup ;;
  --github) SET_GITHUB=1; setup ;;
  tfl-key) tfl_key ;;
  -h | --help | help) usage ;;
  *) usage >&2; exit 2 ;;
esac
