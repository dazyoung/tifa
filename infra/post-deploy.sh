#!/usr/bin/env bash
# Tifa: wiring that needs the deployed services, the budget, then smoke tests. Idempotent; run it after the
# first deploy and again whenever you like. It never deletes anything and never prints a secret or a token.
#
#   infra/post-deploy.sh                 wire, open the public API, create or update the budget, smoke test
#   infra/post-deploy.sh --keep-closed   the same, but leave the public API as it is (for example closed by the guard)
#   infra/post-deploy.sh --skip-budget   the same, without the budget
#   infra/post-deploy.sh smoke           smoke tests only
#
# Needs gcloud (signed in as a project owner and billing account administrator) and curl. Settings: infra/.env.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ENV_FILE=${TIFA_ENV_FILE:-$here/.env}

GUARD_ROLE=tifaGuard
BUDGET_TOPIC=budget-alerts
BUDGET_SUBSCRIPTION=budget-alerts-to-poller
# Cloud Billing publishes budget notifications as this Google-owned identity.
BUDGET_PUBLISHER=billing-budget-alert@system.gserviceaccount.com
ATTRIBUTION="Powered by TfL Open Data"

step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
show() { printf '    $'; printf ' %q' "$@"; printf '\n'; }
run_quiet() { show "$@"; "$@" >/dev/null; }

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
  API_SERVICE=${API_SERVICE:-tifa-api}
  POLLER_SERVICE=${POLLER_SERVICE:-tifa-poller}
  SCHEDULER_JOB=${SCHEDULER_JOB:-tifa-poll}
  BUDGET_NAME=${BUDGET_NAME:-tifa}
  [[ $GCP_PROJECT_ID =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || die "GCP_PROJECT_ID is not a project ID"
  [[ $GCP_REGION =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || die "GCP_REGION is not a region"
  command -v gcloud >/dev/null || die "gcloud is not installed"
  command -v curl >/dev/null || die "curl is not installed"
  export CLOUDSDK_CORE_PROJECT=$GCP_PROJECT_ID CLOUDSDK_CORE_DISABLE_PROMPTS=1
  sa() { printf '%s@%s.iam.gserviceaccount.com' "$1" "$GCP_PROJECT_ID"; }
  POLLER_SA=$(sa tifa-poller)
  SCHEDULER_SA=$(sa tifa-scheduler)
  PUSH_SA=$(sa tifa-pubsub-push)
  gcloud projects describe "$GCP_PROJECT_ID" --format='value(projectNumber)' >/dev/null \
    || die "cannot read project $GCP_PROJECT_ID"
  API_URL=$(service_url "$API_SERVICE")
  POLLER_URL=$(service_url "$POLLER_SERVICE")
}

service_url() {
  local url
  url=$(gcloud run services describe "$1" --region="$GCP_REGION" --format='value(status.url)' 2>/dev/null || true)
  [ -n "$url" ] || die "Cloud Run service $1 not found in $GCP_REGION: deploy first (push to main)"
  printf '%s' "$url"
}

invokers() {
  step "Who may call what"
  local member
  for member in "serviceAccount:$SCHEDULER_SA" "serviceAccount:$PUSH_SA"; do
    run_quiet gcloud run services add-iam-policy-binding "$POLLER_SERVICE" --region="$GCP_REGION" \
      --member="$member" --role=roles/run.invoker
  done
  info "$POLLER_SERVICE: only Cloud Scheduler and the budget push may invoke it"
  run_quiet gcloud run services add-iam-policy-binding "$API_SERVICE" --region="$GCP_REGION" \
    --member="serviceAccount:$POLLER_SA" --role="projects/$GCP_PROJECT_ID/roles/$GUARD_ROLE"
  info "$API_SERVICE: the guard (running as the poller) may open and close it"
  if [ "$KEEP_CLOSED" = 1 ]; then
    info "$API_SERVICE: left as it is (--keep-closed)"
  else
    # If the guard has closed the API, this reopens it; the guard closes it again on its next run while
    # its closure lasts. Use --keep-closed to avoid that.
    run_quiet gcloud run services add-iam-policy-binding "$API_SERVICE" --region="$GCP_REGION" \
      --member=allUsers --role=roles/run.invoker \
      || die "could not make $API_SERVICE public (an organisation policy may forbid allUsers)"
    info "$API_SERVICE: public"
  fi
}

scheduler_target() {
  step "Cloud Scheduler job $SCHEDULER_JOB targets $POLLER_SERVICE"
  local uri audience state
  uri=$(gcloud scheduler jobs describe "$SCHEDULER_JOB" --location="$GCP_REGION" --format='value(httpTarget.uri)' 2>/dev/null) \
    || die "no Scheduler job $SCHEDULER_JOB: run infra/bootstrap.sh first"
  audience=$(gcloud scheduler jobs describe "$SCHEDULER_JOB" --location="$GCP_REGION" \
    --format='value(httpTarget.oidcToken.audience)')
  if [ "$uri" = "$POLLER_URL/internal/poll" ] && [ "$audience" = "$POLLER_URL" ]; then
    info "target is current"
  else
    run_quiet gcloud scheduler jobs update http "$SCHEDULER_JOB" --location="$GCP_REGION" \
      --uri="$POLLER_URL/internal/poll" --oidc-service-account-email="$SCHEDULER_SA" --oidc-token-audience="$POLLER_URL"
  fi
  state=$(gcloud scheduler jobs describe "$SCHEDULER_JOB" --location="$GCP_REGION" --format='value(state)')
  info "state: $state"
}

budget_push() {
  step "Budget notifications: $BUDGET_TOPIC -> $POLLER_SERVICE /internal/budget (OIDC)"
  local flags=(
    --push-endpoint="$POLLER_URL/internal/budget" --push-auth-service-account="$PUSH_SA"
    --push-auth-token-audience="$POLLER_URL" --ack-deadline=30 --min-retry-delay=10s --max-retry-delay=600s
    --message-retention-duration=1d --expiration-period=never
  )
  if gcloud pubsub subscriptions describe "$BUDGET_SUBSCRIPTION" >/dev/null 2>&1; then
    run_quiet gcloud pubsub subscriptions update "$BUDGET_SUBSCRIPTION" "${flags[@]}"
  else
    run_quiet gcloud pubsub subscriptions create "$BUDGET_SUBSCRIPTION" --topic="$BUDGET_TOPIC" "${flags[@]}"
  fi
  run_quiet gcloud pubsub topics add-iam-policy-binding "$BUDGET_TOPIC" \
    --member="serviceAccount:$BUDGET_PUBLISHER" --role=roles/pubsub.publisher \
    || warn "could not let Cloud Billing publish to $BUDGET_TOPIC (connecting the budget below may still grant it)"
}

# Checked before any change is made, so a missing budget setting stops the run before the API is opened.
budget_settings() {
  require BILLING_ACCOUNT_ID BUDGET_AMOUNT BUDGET_CURRENCY
  [[ $BILLING_ACCOUNT_ID =~ ^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$ ]] || die "BILLING_ACCOUNT_ID is not a billing account ID"
  [[ $BUDGET_AMOUNT =~ ^[0-9]+(\.[0-9]{1,2})?$ ]] || die "BUDGET_AMOUNT must be a number such as 3.75"
  [[ $BUDGET_CURRENCY =~ ^[A-Z]{3}$ ]] || die "BUDGET_CURRENCY must be a currency code such as GBP"
}

budget() {
  budget_settings # before the header, which uses the values (set -u)
  step "Budget $BUDGET_NAME: ${BUDGET_AMOUNT} ${BUDGET_CURRENCY} a month on this project"
  local existing common
  common=(
    --billing-account="$BILLING_ACCOUNT_ID" --billing-project="$GCP_PROJECT_ID"
    --display-name="$BUDGET_NAME" --budget-amount="${BUDGET_AMOUNT}${BUDGET_CURRENCY}"
    --filter-projects="projects/$GCP_PROJECT_ID" --calendar-period=month
    --notifications-rule-pubsub-topic="projects/$GCP_PROJECT_ID/topics/$BUDGET_TOPIC"
  )
  # 50, 90 and 100% of actual spend, and 100% of forecast spend. Emails go to the billing account's admins.
  existing=$(gcloud billing budgets list --billing-account="$BILLING_ACCOUNT_ID" --billing-project="$GCP_PROJECT_ID" \
    --filter="displayName=$BUDGET_NAME" --format='value(name)')
  existing=${existing%%$'\n'*}
  if [ -n "$existing" ]; then
    run_quiet gcloud billing budgets update "${existing##*/}" "${common[@]}" --clear-threshold-rules \
      --add-threshold-rule=percent=0.5 --add-threshold-rule=percent=0.9 --add-threshold-rule=percent=1.0 \
      --add-threshold-rule=percent=1.0,basis=forecasted-spend
  else
    run_quiet gcloud billing budgets create "${common[@]}" \
      --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 \
      --threshold-rule=percent=1.0,basis=forecasted-spend
  fi
  info "at 100% the poller closes the public API until the 1st; recording continues"
}

# ---- smoke tests (read-only: nothing here records a poll or changes state)

PASSED=0
FAILED=0
pass() { PASSED=$((PASSED + 1)); printf '    ok    %s\n' "$*"; }
fail() { FAILED=$((FAILED + 1)); printf '    FAIL  %s\n' "$*"; }

# http URL [curl args...]: sets CODE, BODY and HEADERS.
http() {
  local dir
  dir=$(mktemp -d)
  CODE=$(curl -s -o "$dir/body" -D "$dir/head" -w '%{http_code}' --max-time 30 "$@" 2>/dev/null || true)
  BODY=$(cat "$dir/body" 2>/dev/null || true)
  HEADERS=$(tr -d '\r' <"$dir/head" 2>/dev/null || true)
  rm -rf "$dir"
}

header() { printf '%s\n' "$HEADERS" | grep -i "^$1:" | tail -n 1 | cut -d: -f2- | sed 's/^ *//'; }

smoke() {
  step "Smoke tests"
  if [ "$KEEP_CLOSED" = 1 ]; then
    http "$API_URL/v1/meta"
    case $CODE in
      403) pass "API is closed (403), as asked" ;;
      200) pass "API is open" ;;
      *) fail "API /v1/meta answered ${CODE:-nothing}" ;;
    esac
  else
    http "$API_URL/healthz"
    case $CODE in
      200) pass "API /healthz 200" ;;
      503) pass "API /healthz 503: no poll in the last 10 minutes (expected while the Scheduler is paused)" ;;
      *) fail "API /healthz answered ${CODE:-nothing}" ;;
    esac

    http "$API_URL/v1/meta"
    if [ "$CODE" != 200 ]; then fail "API /v1/meta answered ${CODE:-nothing}"
    elif [[ $BODY != *"$ATTRIBUTION"* ]]; then fail "API /v1/meta has no attribution"
    else pass "API /v1/meta 200 with the attribution"; fi

    http "$API_URL/v1/status"
    local etag
    etag=$(header etag)
    if [ "$CODE" = 200 ] && [ -n "$etag" ] && [ -n "$(header cache-control)" ]; then
      pass "API /v1/status 200 with ETag and Cache-Control"
      http "$API_URL/v1/status" -H "If-None-Match: $etag"
      if [ "$CODE" = 304 ]; then pass "API /v1/status If-None-Match 304"; else fail "API /v1/status If-None-Match answered $CODE"; fi
    else
      fail "API /v1/status answered ${CODE:-nothing} (ETag: ${etag:-none})"
    fi

    http "$API_URL/v1/status" -X OPTIONS -H "Origin: https://example.org" -H "Access-Control-Request-Method: GET"
    if [ "$(header access-control-allow-origin)" = "*" ]; then pass "API CORS allows any origin"
    else fail "API CORS preflight answered ${CODE:-nothing} without Access-Control-Allow-Origin: *"; fi

    http "$API_URL/mcp" -L -X POST -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
      --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"tifa-smoke","version":"1"}}}'
    if [ "$CODE" = 200 ] && [[ $BODY == *'"serverInfo"'* ]]; then pass "MCP initialize 200"
    else fail "MCP initialize answered ${CODE:-nothing}"; fi
  fi

  http "$POLLER_URL/healthz"
  case $CODE in
    401 | 403 | 404) pass "poller refuses anonymous requests ($CODE)" ;;
    *) fail "poller answered an anonymous request with ${CODE:-nothing}" ;;
  esac
  http "$POLLER_URL/internal/poll" -X POST
  case $CODE in
    401 | 403 | 404) pass "poller refuses an anonymous poll ($CODE)" ;;
    *) fail "poller answered an anonymous poll with ${CODE:-nothing}" ;;
  esac

  # The token goes to curl through a pipe: never on a command line or on disk.
  local token
  if token=$(gcloud auth print-identity-token 2>/dev/null) && [ -n "$token" ]; then
    CODE=$(printf 'Authorization: Bearer %s\n' "$token" \
      | curl -s -o /dev/null -w '%{http_code}' --max-time 30 -H @- "$POLLER_URL/healthz" 2>/dev/null || true)
    token=""
    case $CODE in
      200 | 503) pass "poller /healthz with an identity token ($CODE)" ;;
      403 | 404) info "skip  poller /healthz with an identity token: $CODE (ingress internal, or no run.invoker for you)" ;;
      *) fail "poller /healthz with an identity token answered ${CODE:-nothing}" ;;
    esac
  else
    info "skip  poller /healthz with an identity token (gcloud could not mint one)"
  fi

  local state
  state=$(gcloud scheduler jobs describe "$SCHEDULER_JOB" --location="$GCP_REGION" --format='value(state)' 2>/dev/null || true)
  info "Scheduler job: ${state:-missing}"
  [ "$state" != PAUSED ] || info "recording is paused: resume with gcloud scheduler jobs resume $SCHEDULER_JOB --location=$GCP_REGION"

  printf '\n    %d passed, %d failed\n' "$PASSED" "$FAILED"
  [ "$FAILED" = 0 ]
}

usage() { sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

KEEP_CLOSED=0
SKIP_BUDGET=0
SMOKE_ONLY=0
for arg in "$@"; do
  case $arg in
    --keep-closed) KEEP_CLOSED=1 ;;
    --skip-budget) SKIP_BUDGET=1 ;;
    smoke) SMOKE_ONLY=1 ;;
    -h | --help | help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

settings
[ "$SMOKE_ONLY" = 1 ] || [ "$SKIP_BUDGET" = 1 ] || budget_settings
info "project $GCP_PROJECT_ID, region $GCP_REGION"
info "API: $API_URL"
if [ "$SMOKE_ONLY" = 0 ]; then
  invokers
  scheduler_target
  budget_push
  if [ "$SKIP_BUDGET" = 1 ]; then info "budget skipped (--skip-budget)"; else budget; fi
fi
smoke
