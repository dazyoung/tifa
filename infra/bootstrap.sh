#!/usr/bin/env bash
# Tifa: one-time Google Cloud setup, in a project that may also host unrelated services.
# Idempotent: it creates what is missing and brings Tifa's own settings back in line, so run it again whenever you
# like. It creates and changes only resources named tifa-* (labelled app=tifa where the type has labels) plus the
# project-level settings listed in docs/operations.md, and it never deletes anything or prints a secret.
#
#   infra/bootstrap.sh [--dry-run] [--github] [--adopt-firestore]
#   infra/bootstrap.sh tfl-key [--dry-run]
#
#   --dry-run          show every change instead of making it (read-only checks still run)
#   --github           also set the GitHub repository variables with the gh CLI
#   --adopt-firestore  use a (default) Firestore database that exists already but was not set up by Tifa
#   tfl-key            add a TfL app key to Secret Manager from a hidden prompt (or stdin)
#
# Needs gcloud, signed in as a project owner (gcloud auth login), and infra/.env (copy infra/env.example).
# Then: infra/first-deploy.sh creates the two services, and infra/post-deploy.sh wires them.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=infra/lib.sh
. "$here/lib.sh"
trap tifa_cleanup EXIT

APIS=(
  run.googleapis.com firestore.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com
  artifactregistry.googleapis.com iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com
  monitoring.googleapis.com logging.googleapis.com pubsub.googleapis.com cloudbilling.googleapis.com
  billingbudgets.googleapis.com cloudresourcemanager.googleapis.com
)
# Collection group and field: blobs, arrays and maps that are never queried, so they are not indexed.
# The first one doubles as the mark of a database that this script set up.
UNINDEXED=(
  "meta status" "meta open" "meta lines" "meta months" "meta run"
  "months payload" "snapshots payload" "polls at" "polls failed"
)

enable_apis() {
  step "Enable APIs (already enabled ones are left as they are; nothing is ever disabled)"
  run gcloud services enable "${APIS[@]}"
  [ "$(gcloud billing projects describe "$GCP_PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null || true)" = True ] \
    || die "the project has no billing account linked: link one, then re-run"
  info "billing is enabled"
}

field_unindexed() {  # GROUP FIELD
  [ "$(gcloud firestore indexes fields describe "$2" --collection-group="$1" --database='(default)' \
    --format='value(indexConfig.usesAncestorConfig)' 2>/dev/null || true)" = False ]
}

firestore() {
  step "Firestore: the (default) database, Native mode"
  local found err others pair group field
  others=$({ gcloud firestore databases list --format='value(name)' 2>/dev/null || true; } \
    | sed 's|.*/||' | grep -vxF '(default)' | paste -s -d , - || true)
  [ -z "$others" ] || info "other databases in the project, never touched: $others"
  tifa_tmp
  err=$TIFA_TMP/firestore-describe.err
  if found=$(gcloud firestore databases describe --database='(default)' --format='value(type,locationId)' 2>"$err"); then
    found=${found//$'\t'/ }
    if field_unindexed "${UNINDEXED[0]%% *}" "${UNINDEXED[0]#* }"; then
      info "exists, set up by Tifa: $found"
    elif [ "$ADOPT_FIRESTORE" = 1 ]; then
      info "exists and was not set up by Tifa: using it, as asked (--adopt-firestore): $found"
    else
      die "the project already has a (default) Firestore database ($found) that Tifa did not set up, and Tifa \
never changes an existing database. If it is unused, or may be shared, re-run with --adopt-firestore: Tifa then \
exempts some fields of its own collection groups (meta, months, polls, snapshots) from indexing and stores its \
documents there."
    fi
    [[ $found == FIRESTORE_NATIVE* ]] || die "the (default) database is not in Native mode"
    [[ $found == *" $FIRESTORE_LOCATION" ]] || warn "the database is not in $FIRESTORE_LOCATION (a location is fixed once created)"
  elif grep -qiE 'not.?found|does not exist' "$err"; then
    info "none yet: creating it in $FIRESTORE_LOCATION, with delete protection"
    run gcloud firestore databases create --database='(default)' --location="$FIRESTORE_LOCATION" \
      --type=firestore-native --delete-protection
  else
    printf '    %s\n' "$(head -n 1 "$err")" >&2
    die "cannot read the (default) Firestore database (see above)"
  fi

  step "Firestore single-field index exemptions (Tifa's collection groups only)"
  for pair in "${UNINDEXED[@]}"; do
    group=${pair%% *}
    field=${pair#* }
    if field_unindexed "$group" "$field"; then
      info "$group.$field: not indexed"
    else
      run gcloud firestore indexes fields update "$field" --collection-group="$group" --database='(default)' \
        --disable-indexes
    fi
  done
}

artifact_registry() {
  step "Artifact Registry repository $AR_REPOSITORY (keeps the 3 newest images)"
  local label
  if label=$(gcloud artifacts repositories describe "$AR_REPOSITORY" --location="$GCP_REGION" \
    --format='value(labels.app)' 2>/dev/null); then
    info "exists"
    [ "$label" = tifa ] || run_quiet gcloud artifacts repositories update "$AR_REPOSITORY" --location="$GCP_REGION" \
      --update-labels="$TIFA_LABEL"
  else
    run gcloud artifacts repositories create "$AR_REPOSITORY" --repository-format=docker \
      --location="$GCP_REGION" --description="Tifa container images" --labels="$TIFA_LABEL"
  fi
  tifa_tmp
  cat >"$TIFA_TMP/cleanup-policy.json" <<'JSON'
[
  {"name": "keep-newest-3", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 3}},
  {"name": "delete-older", "action": {"type": "Delete"}, "condition": {"tagState": "any", "olderThan": "1d"}}
]
JSON
  run_quiet gcloud artifacts repositories set-cleanup-policies "$AR_REPOSITORY" --location="$GCP_REGION" \
    --policy="$TIFA_TMP/cleanup-policy.json"
}

service_accounts() {
  step "Service accounts"
  local id name
  while IFS='|' read -r id name; do
    if gcloud iam service-accounts describe "$(tifa_sa "$id")" >/dev/null 2>&1; then
      info "$id exists"
    else
      run gcloud iam service-accounts create "$id" --display-name="$name"
    fi
  done <<LIST
$SA_DEPLOY|Tifa deploy (GitHub Actions)
$SA_API|Tifa public API
$SA_POLLER|Tifa poller and guard
$SA_SCHEDULER|Tifa Cloud Scheduler invoker
$SA_PUSH|Tifa budget alerts push
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
  info "granted on $API_SERVICE only, by infra/post-deploy.sh once the service exists"
}

iam() {
  step "IAM: least privilege for each identity"
  local member role sa
  # Firestore and Monitoring roles exist only at project level.
  while read -r member role; do
    retry run_quiet gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" --member="$member" --role="$role" \
      --condition=None
  done <<LIST
serviceAccount:$API_SA roles/datastore.viewer
serviceAccount:$POLLER_SA roles/datastore.user
serviceAccount:$POLLER_SA roles/monitoring.viewer
LIST
  info "$SA_DEPLOY gets no project-wide role: it may run the services as their own accounts and push images,"
  info "and infra/first-deploy.sh grants it roles/run.developer on $API_SERVICE and $POLLER_SERVICE only"
  for sa in "$API_SA" "$POLLER_SA"; do
    retry run_quiet gcloud iam service-accounts add-iam-policy-binding "$sa" \
      --member="serviceAccount:$DEPLOY_SA" --role=roles/iam.serviceAccountUser
  done
  retry run_quiet gcloud artifacts repositories add-iam-policy-binding "$AR_REPOSITORY" --location="$GCP_REGION" \
    --member="serviceAccount:$DEPLOY_SA" --role=roles/artifactregistry.writer
  local roles
  roles=$(tifa_project_roles "serviceAccount:$DEPLOY_SA" | paste -s -d ' ' - || true)
  if [ -n "$roles" ]; then
    warn "$SA_DEPLOY holds project-wide roles ($roles), so CI could change other services; remove them:"
    for role in $roles; do
      info "  gcloud projects remove-iam-policy-binding $GCP_PROJECT_ID --member=serviceAccount:$DEPLOY_SA --role=$role"
    done
  fi
}

secret() {
  step "Secret Manager: $SECRET_NAME (the TfL app key; only the poller reads it)"
  local label enabled
  if label=$(gcloud secrets describe "$SECRET_NAME" --format='value(labels.app)' 2>/dev/null); then
    info "exists"
    [ "$label" = tifa ] || run_quiet gcloud secrets update "$SECRET_NAME" --update-labels="$TIFA_LABEL"
  else
    run gcloud secrets create "$SECRET_NAME" --replication-policy=automatic --labels="$TIFA_LABEL"
  fi
  retry run_quiet gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
    --member="serviceAccount:$POLLER_SA" --role=roles/secretmanager.secretAccessor
  enabled=$({ gcloud secrets versions list "$SECRET_NAME" --filter=state=ENABLED --format='value(name)' 2>/dev/null \
    || true; } | sed '/^$/d' | wc -l | tr -d ' ')
  if [ "$enabled" = 0 ]; then
    info "no key yet: the poller calls TfL anonymously until one is added (infra/bootstrap.sh tfl-key)"
  else
    info "$enabled enabled version(s); deploys use the one named by TFL_APP_KEY_VERSION"
  fi
}

pubsub() {
  step "Pub/Sub topic $BUDGET_TOPIC (budget notifications for the poller)"
  local label
  if label=$(gcloud pubsub topics describe "$BUDGET_TOPIC" --format='value(labels.app)' 2>/dev/null); then
    info "exists"
    [ "$label" = tifa ] || run_quiet gcloud pubsub topics update "$BUDGET_TOPIC" --update-labels="$TIFA_LABEL"
  else
    run gcloud pubsub topics create "$BUDGET_TOPIC" --labels="$TIFA_LABEL"
  fi
  # Pub/Sub signs the push requests' OIDC tokens as the push identity.
  if [ "$DRY_RUN" = 1 ]; then
    show gcloud beta services identity create --service=pubsub.googleapis.com
  else
    gcloud beta services identity create --service=pubsub.googleapis.com >/dev/null 2>&1 \
      || warn "could not confirm the Pub/Sub service agent (it is usually created with the API)"
  fi
  retry run_quiet gcloud iam service-accounts add-iam-policy-binding "$PUSH_SA" \
    --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com" \
    --role=roles/iam.serviceAccountTokenCreator
}

workload_identity() {
  step "Workload Identity Federation: pool $WIF_POOL, for GitHub Actions on $GITHUB_REPOSITORY, main branch only"
  local state condition mapping principal
  state=$(gcloud iam workload-identity-pools describe "$WIF_POOL" --location=global --format='value(state)' 2>/dev/null || true)
  case $state in
    "") run gcloud iam workload-identity-pools create "$WIF_POOL" --location=global --display-name="Tifa GitHub Actions" \
      --description="GitHub Actions deploys of Tifa" ;;
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
        --workload-identity-pool="$WIF_POOL" --display-name="Tifa GitHub repository" \
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
  step "Cloud Scheduler job $SCHEDULER_JOB: every minute (UTC), OIDC, created paused (jobs have no labels)"
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

summary() {
  local provider="projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$WIF_POOL/providers/$WIF_PROVIDER"
  # Identifiers go into Actions SECRETS so this public repository's logs mask them; the rest are variables.
  step "Done. GitHub settings for $GITHUB_REPOSITORY (Settings > Secrets and variables > Actions):"
  local secrets=("GCP_PROJECT_ID=$GCP_PROJECT_ID" "GCP_WIF_PROVIDER=$provider" "GCP_DEPLOY_SA=$DEPLOY_SA")
  local vars=("GCP_REGION=$GCP_REGION" "TIFA_DEPLOY=true")
  local pair
  for pair in "${secrets[@]}"; do
    info "secret ${pair%%=*} (value not shown)"
    if [ "$SET_GITHUB" = 1 ]; then
      printf %s "${pair#*=}" | run_quiet gh secret set "${pair%%=*}" --repo "$GITHUB_REPOSITORY"
    fi
  done
  for pair in "${vars[@]}"; do
    info "variable $pair"
    if [ "$SET_GITHUB" = 1 ]; then
      run_quiet gh variable set "${pair%%=*}" --repo "$GITHUB_REPOSITORY" --body "${pair#*=}"
    fi
  done
  cat <<EOF

Next:
  1. Create the GitHub environment "production" (deployments only from main).
  2. infra/first-deploy.sh: builds the image and creates both services with your credentials. CI cannot create
     services; from then on every push to main updates them.
  3. infra/post-deploy.sh: wires the services, opens the API, creates the budget and runs the smoke tests.
  4. When a TfL app key is available: infra/bootstrap.sh tfl-key, then set TFL_APP_KEY_VERSION (GitHub variable
     and infra/.env) and redeploy.
EOF
}

setup() {
  tifa_settings
  tifa_require FIRESTORE_LOCATION GITHUB_REPOSITORY GITHUB_REPOSITORY_OWNER_ID
  [[ $FIRESTORE_LOCATION =~ ^[a-z0-9-]+$ ]] || die "FIRESTORE_LOCATION is not a location"
  [[ $GITHUB_REPOSITORY =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "GITHUB_REPOSITORY must be owner/repo"
  [ "$GITHUB_REPOSITORY" != owner/repo ] || die "GITHUB_REPOSITORY is still the placeholder"
  [[ $GITHUB_REPOSITORY_OWNER_ID =~ ^[1-9][0-9]*$ ]] || die "GITHUB_REPOSITORY_OWNER_ID must be the owner's numeric ID"
  if [ "$SET_GITHUB" = 1 ]; then command -v gh >/dev/null || die "--github needs the gh CLI"; fi
  tifa_project_number
  info "project $GCP_PROJECT_ID, region $GCP_REGION"
  [ "$DRY_RUN" = 0 ] || info "dry run: changes are shown, not made"
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
  summary
}

# Reads the key without echoing it and sends it to Secret Manager on stdin; it never reaches a command line.
tfl_key() {
  tifa_settings
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
  if [ "$DRY_RUN" = 1 ]; then
    key=""
    show gcloud secrets versions add "$SECRET_NAME" --data-file=-
    info "the key looks valid; nothing was stored (dry run)"
    return
  fi
  version=$(printf '%s' "$key" | gcloud secrets versions add "$SECRET_NAME" --data-file=- --format='value(name)')
  key=""
  version=${version##*/}
  info "stored as version $version of $SECRET_NAME (the key was not printed)"
  info "next: set TFL_APP_KEY_VERSION=$version (GitHub variable and infra/.env) and redeploy"
  info "then disable the version it replaces: gcloud secrets versions disable OLD --secret=$SECRET_NAME"
}

usage() { sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

SET_GITHUB=0
ADOPT_FIRESTORE=0
COMMAND=setup
for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN=1 ;;
    --github) SET_GITHUB=1 ;;
    --adopt-firestore) ADOPT_FIRESTORE=1 ;;
    tfl-key) COMMAND=tfl_key ;;
    -h | --help | help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
"$COMMAND"
