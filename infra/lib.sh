# shellcheck shell=bash
# shellcheck disable=SC2034  # the names defined here are used by the scripts and the workflow that source this file
#
# Tifa's Google Cloud names, Cloud Run service settings and shell helpers. Sourced by infra/bootstrap.sh,
# infra/first-deploy.sh, infra/post-deploy.sh and the Deploy workflow, so they always agree. Sourcing it only
# defines names and functions: it changes nothing and needs no settings.
#
# Tifa runs in a Google Cloud project that may also host unrelated services. Every resource it creates is named
# tifa-* and, where the resource type has labels, labelled app=tifa (the budget counts only that label). The names
# are fixed here rather than being settings. Works with the bash 3.2 that macOS ships.

TIFA_INFRA=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TIFA_ROOT=$(dirname "$TIFA_INFRA")

# ---- names (custom role IDs cannot contain hyphens, hence tifa_guard)
TIFA_LABEL=app=tifa
AR_REPOSITORY=tifa-images
IMAGE_NAME=tifa
API_SERVICE=tifa-api
POLLER_SERVICE=tifa-poller
SCHEDULER_JOB=tifa-poll
SECRET_NAME=tifa-tfl-app-key
BUDGET_TOPIC=tifa-budget-alerts
BUDGET_SUBSCRIPTION=tifa-budget-alerts-to-poller
WIF_POOL=tifa-github
WIF_PROVIDER=tifa-github-actions
GUARD_ROLE=tifa_guard
GUARD_PERMISSIONS=run.services.get,run.services.getIamPolicy,run.services.setIamPolicy
LOG_EXCLUSION=tifa-api-requests-below-5xx
# Service account IDs; tifa_sa turns one into its email.
SA_DEPLOY=tifa-deploy
SA_API=tifa-api
SA_POLLER=tifa-poller
SA_SCHEDULER=tifa-scheduler
SA_PUSH=tifa-pubsub-push

# ---- Cloud Run settings for both services (the plan's sizes). infra/first-deploy.sh and the Deploy workflow
# deploy with exactly these; who may invoke a service is not set here (see infra/post-deploy.sh).
POLLER_RUN_FLAGS=(
  --execution-environment=gen1 --cpu=0.25 --memory=256Mi --concurrency=1
  --min-instances=0 --max-instances=2 --timeout=60s --cpu-throttling --no-cpu-boost --port=8080
  "--labels=$TIFA_LABEL,role=poller"
)
API_RUN_FLAGS=(
  --execution-environment=gen1 --cpu=1 --memory=512Mi --concurrency=40
  --min-instances=0 --max-instances=1 --timeout=30s --cpu-throttling --cpu-boost --port=8080 --ingress=all
  "--labels=$TIFA_LABEL,role=api"
)

# ---- settings. The scripts read them from infra/.env only (see infra/env.example): tifa_load_env ignores the
# environment, so a project ID exported for other work is never used by mistake. The Deploy workflow sources this
# file without loading infra/.env and passes the same names from its repository variables.
TIFA_SETTINGS="GCP_PROJECT_ID GCP_REGION FIRESTORE_LOCATION GITHUB_REPOSITORY GITHUB_REPOSITORY_OWNER_ID
BILLING_ACCOUNT_ID BUDGET_AMOUNT BUDGET_CURRENCY TFL_APP_KEY_VERSION TIFA_PUBLIC_SOURCES TIFA_ALLOWED_HOSTS
TIFA_POLLER_INGRESS"
GCP_PROJECT_ID=${GCP_PROJECT_ID:-}
GCP_REGION=${GCP_REGION:-}
FIRESTORE_LOCATION=${FIRESTORE_LOCATION:-}
GITHUB_REPOSITORY=${GITHUB_REPOSITORY:-}
GITHUB_REPOSITORY_OWNER_ID=${GITHUB_REPOSITORY_OWNER_ID:-}
BILLING_ACCOUNT_ID=${BILLING_ACCOUNT_ID:-}
BUDGET_AMOUNT=${BUDGET_AMOUNT:-}
BUDGET_CURRENCY=${BUDGET_CURRENCY:-}
TFL_APP_KEY_VERSION=${TFL_APP_KEY_VERSION:-}
TIFA_PUBLIC_SOURCES=${TIFA_PUBLIC_SOURCES:-}
TIFA_ALLOWED_HOSTS=${TIFA_ALLOWED_HOSTS:-}
TIFA_POLLER_INGRESS=${TIFA_POLLER_INGRESS:-}
DRY_RUN=${DRY_RUN:-0}

# Patterns live in variables: bash 3.2 and 5 then read them the same way.
_TIFA_RE_PROJECT='^[a-z][a-z0-9-]{4,28}[a-z0-9]$'
_TIFA_RE_REGION='^[a-z]+-[a-z]+[0-9]+$'
_TIFA_RE_SOURCES='^[af](,[af])?$'
_TIFA_RE_HOSTS='^[a-z0-9.:*-]+(,[a-z0-9.:*-]+)*$'
_TIFA_RE_VERSION='^[1-9][0-9]*$'

# ---- output. Commands are shown quoted, ready to copy; they carry names and IDs, never secrets.
_tifa_actions() { [ "${GITHUB_ACTIONS:-}" = true ]; }
step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() {
  if _tifa_actions; then printf '::warning::%s\n' "$*"; else printf '    warning: %s\n' "$*" >&2; fi
}
die() {
  if _tifa_actions; then printf '::error::%s\n' "$*"; else printf 'error: %s\n' "$*" >&2; fi
  exit 1
}
show() {
  if [ "$DRY_RUN" = 1 ]; then printf '    [dry run] $'; else printf '    $'; fi
  printf ' %q' "$@"
  printf '\n'
}
# run CMD...: show it, then run it (in a dry run, only show it).
run() {
  show "$@"
  [ "$DRY_RUN" = 1 ] || "$@"
}
# The same without stdout: IAM commands print the whole policy, members' emails included.
run_quiet() {
  show "$@"
  [ "$DRY_RUN" = 1 ] || "$@" >/dev/null
}
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

tifa_tmp() { [ -n "${TIFA_TMP:-}" ] || TIFA_TMP=$(mktemp -d); }
tifa_cleanup() { [ -z "${TIFA_TMP:-}" ] || rm -rf "$TIFA_TMP"; }

# ---- settings

# KEY=value lines, comments and blank lines; nothing in the file is executed. Unknown keys are an error, so a typo
# never passes silently. TIFA_ENV_FILE points at another file (the tests use it).
tifa_load_env() {
  local file=${TIFA_ENV_FILE:-$TIFA_INFRA/.env} line key value known name
  local re_skip='^[[:space:]]*(#|$)' re_pair='^[[:space:]]*([A-Z][A-Z0-9_]*)=(.*)$'
  [ -f "$file" ] || die "no $file: copy infra/env.example to infra/.env and fill it in (nothing was changed)"
  for name in $TIFA_SETTINGS; do printf -v "$name" '%s' ""; done
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    [[ $line =~ $re_skip ]] && continue
    [[ $line =~ $re_pair ]] || die "$file: cannot read a line (expected KEY=value)"
    key=${BASH_REMATCH[1]}
    value=${BASH_REMATCH[2]}
    known=0
    for name in $TIFA_SETTINGS; do [ "$name" != "$key" ] || known=1; done
    [ "$known" = 1 ] || die "$file: unknown setting $key (see infra/env.example)"
    value=${value%%[[:space:]]#*}
    value=${value%"${value##*[![:space:]]}"}
    if [[ $value == \"*\" || $value == \'*\' ]]; then value=${value:1:${#value}-2}; fi
    printf -v "$key" '%s' "$value"
  done <"$file"
}

tifa_require() {
  local name missing=""
  for name in "$@"; do
    [ -n "${!name:-}" ] || missing="$missing $name"
  done
  [ -z "$missing" ] || die "set$missing in infra/.env"
}

# Load infra/.env, check the settings every script needs, and derive the identities.
tifa_settings() {
  tifa_load_env
  tifa_require GCP_PROJECT_ID GCP_REGION
  [[ $GCP_PROJECT_ID =~ $_TIFA_RE_PROJECT ]] || die "GCP_PROJECT_ID is not a project ID"
  [ "$GCP_PROJECT_ID" != your-project-id ] || die "GCP_PROJECT_ID is still the placeholder"
  [[ $GCP_REGION =~ $_TIFA_RE_REGION ]] || die "GCP_REGION is not a region"
  tifa_check_deploy_settings
  command -v gcloud >/dev/null || die "gcloud is not installed"
  export CLOUDSDK_CORE_PROJECT=$GCP_PROJECT_ID CLOUDSDK_CORE_DISABLE_PROMPTS=1
  DEPLOY_SA=$(tifa_sa "$SA_DEPLOY")
  API_SA=$(tifa_sa "$SA_API")
  POLLER_SA=$(tifa_sa "$SA_POLLER")
  SCHEDULER_SA=$(tifa_sa "$SA_SCHEDULER")
  PUSH_SA=$(tifa_sa "$SA_PUSH")
}

# The settings that shape a deploy (the same names as the GitHub repository variables), with their defaults.
tifa_check_deploy_settings() {
  TIFA_PUBLIC_SOURCES=${TIFA_PUBLIC_SOURCES:-a}  # never "f" by default: TfL's FOI release is not licensed for re-use
  TIFA_POLLER_INGRESS=${TIFA_POLLER_INGRESS:-all}
  [ -z "$TFL_APP_KEY_VERSION" ] || [[ $TFL_APP_KEY_VERSION =~ $_TIFA_RE_VERSION ]] \
    || die "TFL_APP_KEY_VERSION must be a secret version number (not 'latest')"
  [[ $TIFA_PUBLIC_SOURCES =~ $_TIFA_RE_SOURCES ]] || die "TIFA_PUBLIC_SOURCES must be a or a,f"
  [ -z "$TIFA_ALLOWED_HOSTS" ] || [[ $TIFA_ALLOWED_HOSTS =~ $_TIFA_RE_HOSTS ]] \
    || die "TIFA_ALLOWED_HOSTS must be comma-separated host names"
  case $TIFA_POLLER_INGRESS in all | internal) ;; *) die "TIFA_POLLER_INGRESS must be all or internal" ;; esac
  case ,$TIFA_PUBLIC_SOURCES, in
    *,f,*) warn "TIFA_PUBLIC_SOURCES includes f: this publishes TfL's FOI data, which needs TfL's permission to re-use" ;;
  esac
}

tifa_sa() { printf '%s@%s.iam.gserviceaccount.com' "$1" "$GCP_PROJECT_ID"; }
tifa_image_repo() { printf '%s-docker.pkg.dev/%s/%s/%s' "$GCP_REGION" "$GCP_PROJECT_ID" "$AR_REPOSITORY" "$IMAGE_NAME"; }

# Sets PROJECT_NUMBER, after checking that gcloud is signed in and can see the project.
tifa_project_number() {
  [ -n "$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null || true)" ] \
    || die "gcloud has no active account: run gcloud auth login"
  PROJECT_NUMBER=$(gcloud projects describe "$GCP_PROJECT_ID" --format='value(projectNumber)' 2>/dev/null || true)
  [[ $PROJECT_NUMBER =~ ^[0-9]+$ ]] || die "cannot read project $GCP_PROJECT_ID (does it exist, and may this account see it?)"
}

# ---- IAM reads (nothing is printed: policies list members' emails)

# tifa_project_roles MEMBER: the roles MEMBER holds on the project itself, one per line.
tifa_project_roles() {
  { gcloud projects get-iam-policy "$GCP_PROJECT_ID" --flatten='bindings[].members' \
    --format='value(bindings.role,bindings.members)' 2>/dev/null || true; } | awk -F'\t' -v m="$1" '$2 == m { print $1 }'
}

# tifa_service_roles SERVICE MEMBER: the roles MEMBER holds on one Cloud Run service, one per line.
tifa_service_roles() {
  { gcloud run services get-iam-policy "$1" --region="$GCP_REGION" --flatten='bindings[].members' \
    --format='value(bindings.role,bindings.members)' 2>/dev/null || true; } | awk -F'\t' -v m="$2" '$2 == m { print $1 }'
}

# tifa_service_members SERVICE ROLE: the members holding ROLE on one Cloud Run service, one per line.
tifa_service_members() {
  { gcloud run services get-iam-policy "$1" --region="$GCP_REGION" --flatten='bindings[].members' \
    --format='value(bindings.role,bindings.members)' 2>/dev/null || true; } | awk -F'\t' -v r="$2" '$1 == r { print $2 }'
}

# ---- Cloud Run

# tifa_service_exists NAME: the service exists and this identity may read it.
tifa_service_exists() {
  [ -n "$(gcloud run services describe "$1" --region="$GCP_REGION" --format='value(metadata.name)' 2>/dev/null || true)" ]
}

# CI only updates the services; it cannot create them.
tifa_require_services() {
  local svc missing=""
  for svc in "$POLLER_SERVICE" "$API_SERVICE"; do
    tifa_service_exists "$svc" || missing="$missing $svc"
  done
  [ -z "$missing" ] || die "cannot read${missing} in $GCP_REGION: create the services once with infra/first-deploy.sh \
(an owner's credentials); it also grants this identity roles/run.developer on each"
}

# Host names the MCP endpoint accepts: the API's own run.app URLs plus TIFA_ALLOWED_HOSTS, comma-separated.
tifa_api_hosts() {
  local json=""
  json=$(gcloud run services describe "$API_SERVICE" --region="$GCP_REGION" --format=json 2>/dev/null || true)
  {
    if [ -n "$json" ]; then
      printf '%s' "$json" | jq -r '[(.metadata.annotations["run.googleapis.com/urls"] // "[]" | fromjson | .[]),
        .status.url] | map(select(. != null) | sub("^https://"; "")) | .[]' 2>/dev/null || true
    fi
    printf '%s\n' "$TIFA_ALLOWED_HOSTS" | tr ',' '\n'
  } | sed '/^$/d' | LC_ALL=C sort -u | paste -s -d , -
}

# Environment files (JSON is YAML): no secrets go in them.
tifa_poller_env() {
  jq -n --arg project "$GCP_PROJECT_ID" --arg region "$GCP_REGION" --arg api "$API_SERVICE" '{
    TIFA_ROLE: "poller", TIFA_STORE: "firestore", GOOGLE_CLOUD_PROJECT: $project, TIFA_REGION: $region,
    TIFA_API_SERVICE: $api, TIFA_GUARD_MODE: "enforce"
  }'
}

tifa_api_env() {
  jq -n --arg project "$GCP_PROJECT_ID" --arg region "$GCP_REGION" --arg sources "${TIFA_PUBLIC_SOURCES:-a}" \
    --arg hosts "$1" '{
    TIFA_ROLE: "api", TIFA_STORE: "firestore", GOOGLE_CLOUD_PROJECT: $project, TIFA_REGION: $region,
    TIFA_PUBLIC_SOURCES: $sources, TIFA_ALLOWED_HOSTS: $hosts
  }'
}

# tifa_deploy_poller IMAGE [gcloud flags...]: deploy the private poller (creating it needs an owner's credentials).
# The TfL key is mounted at the pinned TFL_APP_KEY_VERSION; without one the poller calls TfL anonymously.
tifa_deploy_poller() {
  local image=$1 secrets=""
  shift
  tifa_tmp
  tifa_poller_env >"$TIFA_TMP/poller-env.yaml"
  info "environment: $(jq -c . "$TIFA_TMP/poller-env.yaml")"
  if [ -n "$TFL_APP_KEY_VERSION" ]; then
    secrets="--set-secrets=TFL_APP_KEY=$SECRET_NAME:$TFL_APP_KEY_VERSION"
    info "TfL app key: $SECRET_NAME version $TFL_APP_KEY_VERSION"
  else
    warn "TFL_APP_KEY_VERSION is not set: the poller calls TfL anonymously"
    if tifa_service_exists "$POLLER_SERVICE"; then secrets=--clear-secrets; fi
  fi
  run gcloud run deploy "$POLLER_SERVICE" --image="$image" --region="$GCP_REGION" \
    --service-account="$(tifa_sa "$SA_POLLER")" "${POLLER_RUN_FLAGS[@]}" --ingress="${TIFA_POLLER_INGRESS:-all}" \
    --env-vars-file="$TIFA_TMP/poller-env.yaml" ${secrets:+"$secrets"} "$@" --quiet
  # A manual rollback pins traffic to one revision; a deploy takes it back to the latest.
  run_quiet gcloud run services update-traffic "$POLLER_SERVICE" --region="$GCP_REGION" --to-latest --quiet
}

# tifa_deploy_api IMAGE [gcloud flags...]: deploy the public API. Deploys never change who may call it
# (infra/post-deploy.sh and the fair-use guard own that binding).
tifa_deploy_api() {
  local image=$1 before after
  shift
  tifa_tmp
  before=$(tifa_api_hosts)
  tifa_api_env "$before" >"$TIFA_TMP/api-env.yaml"
  info "environment: $(jq -c . "$TIFA_TMP/api-env.yaml")"
  run gcloud run deploy "$API_SERVICE" --image="$image" --region="$GCP_REGION" \
    --service-account="$(tifa_sa "$SA_API")" "${API_RUN_FLAGS[@]}" \
    --env-vars-file="$TIFA_TMP/api-env.yaml" "$@" --quiet
  after=$(tifa_api_hosts)
  if [ "$after" != "$before" ]; then  # a new service: its run.app URLs exist only now
    tifa_api_env "$after" >"$TIFA_TMP/api-env.yaml"
    info "MCP host names: $after"
    run_quiet gcloud run services update "$API_SERVICE" --region="$GCP_REGION" \
      --env-vars-file="$TIFA_TMP/api-env.yaml" --quiet
  fi
  run_quiet gcloud run services update-traffic "$API_SERVICE" --region="$GCP_REGION" --to-latest --quiet
}
