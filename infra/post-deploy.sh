#!/usr/bin/env bash
# Tifa: the wiring that needs the deployed services, the budget and the log exclusion, then smoke tests.
# Idempotent: run it after infra/first-deploy.sh and again whenever you like. It changes only Tifa's own resources
# (plus one named exclusion on the project's _Default log sink) and never prints a secret. It deletes nothing; the
# one thing it ever removes is public access to the poller, should anyone have granted it.
#
#   infra/post-deploy.sh [--dry-run] [--keep-closed] [--skip-budget]
#   infra/post-deploy.sh smoke
#
#   --dry-run       show every change instead of making it (read-only checks still run; no smoke tests)
#   --keep-closed   leave the API's public access as it is (for example while the guard has closed it)
#   --skip-budget   leave the budget alone
#   smoke           the smoke tests only (read-only)
#
# Needs gcloud (a project owner; for the budget also a billing account administrator) and curl. Settings: infra/.env.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=infra/lib.sh
. "$here/lib.sh"
trap tifa_cleanup EXIT

# Cloud Billing publishes budget notifications as this Google-owned identity.
BUDGET_PUBLISHER=billing-budget-alert@system.gserviceaccount.com
ATTRIBUTION="Powered by TfL Open Data"

service_url() {
  local url
  url=$(gcloud run services describe "$1" --region="$GCP_REGION" --format='value(status.url)' 2>/dev/null || true)
  if [ -z "$url" ]; then
    [ "$DRY_RUN" = 1 ] || die "Cloud Run service $1 not found in $GCP_REGION: run infra/first-deploy.sh first"
    url="https://$1-$PROJECT_NUMBER.$GCP_REGION.run.app"
    warn "$1 is not deployed yet; assuming $url"
  fi
  printf '%s' "$url"
}

settings() {
  tifa_settings
  command -v curl >/dev/null || die "curl is not installed"
  tifa_project_number
  BUDGET_NAME="tifa-budget-$GCP_PROJECT_ID"  # unique on a billing account shared by several projects
  API_URL=$(service_url "$API_SERVICE")
  POLLER_URL=$(service_url "$POLLER_SERVICE")
}

has_line() { printf '%s\n' "$1" | grep -qxF -- "$2"; }

invokers() {
  step "Who may call what"
  local member public
  for member in "serviceAccount:$SCHEDULER_SA" "serviceAccount:$PUSH_SA"; do
    run_quiet gcloud run services add-iam-policy-binding "$POLLER_SERVICE" --region="$GCP_REGION" \
      --member="$member" --role=roles/run.invoker
  done
  public=$(tifa_service_members "$POLLER_SERVICE" roles/run.invoker | grep -xE 'allUsers|allAuthenticatedUsers' || true)
  for member in $public; do
    warn "$POLLER_SERVICE had a public invoker ($member): removing it"
    run_quiet gcloud run services remove-iam-policy-binding "$POLLER_SERVICE" --region="$GCP_REGION" \
      --member="$member" --role=roles/run.invoker
  done
  info "$POLLER_SERVICE: only Cloud Scheduler and the budget push may invoke it"
  run_quiet gcloud run services add-iam-policy-binding "$API_SERVICE" --region="$GCP_REGION" \
    --member="serviceAccount:$POLLER_SA" --role="projects/$GCP_PROJECT_ID/roles/$GUARD_ROLE"
  info "$API_SERVICE: the guard (running as the poller) may open and close it; that role covers no other service"
  if [ "$KEEP_CLOSED" = 1 ]; then
    info "$API_SERVICE: public access left as it is (--keep-closed)"
  else
    # If the guard has closed the API, this reopens it; the guard closes it again on its next run while its closure
    # lasts. Use --keep-closed to avoid that.
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
    --format='value(httpTarget.oidcToken.audience)' 2>/dev/null || true)
  if [ "$uri" = "$POLLER_URL/internal/poll" ] && [ "$audience" = "$POLLER_URL" ]; then
    info "target is current"
  else
    run_quiet gcloud scheduler jobs update http "$SCHEDULER_JOB" --location="$GCP_REGION" \
      --uri="$POLLER_URL/internal/poll" --oidc-service-account-email="$SCHEDULER_SA" --oidc-token-audience="$POLLER_URL"
  fi
  state=$(gcloud scheduler jobs describe "$SCHEDULER_JOB" --location="$GCP_REGION" --format='value(state)' 2>/dev/null || true)
  info "state: ${state:-unknown}"
}

budget_push() {
  step "Budget notifications: $BUDGET_TOPIC -> $POLLER_SERVICE /internal/budget (OIDC)"
  local flags=(
    --push-endpoint="$POLLER_URL/internal/budget" --push-auth-service-account="$PUSH_SA"
    --push-auth-token-audience="$POLLER_URL" --ack-deadline=30 --min-retry-delay=10s --max-retry-delay=600s
    --message-retention-duration=1d --expiration-period=never
  )
  if gcloud pubsub subscriptions describe "$BUDGET_SUBSCRIPTION" >/dev/null 2>&1; then
    run_quiet gcloud pubsub subscriptions update "$BUDGET_SUBSCRIPTION" "${flags[@]}" --update-labels="$TIFA_LABEL"
  else
    run_quiet gcloud pubsub subscriptions create "$BUDGET_SUBSCRIPTION" --topic="$BUDGET_TOPIC" "${flags[@]}" \
      --labels="$TIFA_LABEL"
  fi
  run_quiet gcloud pubsub topics add-iam-policy-binding "$BUDGET_TOPIC" \
    --member="serviceAccount:$BUDGET_PUBLISHER" --role=roles/pubsub.publisher \
    || warn "could not let Cloud Billing publish to $BUDGET_TOPIC (connecting the budget below may still grant it)"
}

# Checked before any change is made, so a missing budget setting stops the run before the API is opened.
budget_settings() {
  tifa_require BILLING_ACCOUNT_ID BUDGET_AMOUNT BUDGET_CURRENCY
  [[ $BILLING_ACCOUNT_ID =~ ^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$ ]] || die "BILLING_ACCOUNT_ID is not a billing account ID"
  [[ $BUDGET_AMOUNT =~ ^[0-9]+(\.[0-9]{1,2})?$ ]] || die "BUDGET_AMOUNT must be a number such as 3.75"
  [[ $BUDGET_CURRENCY =~ ^[A-Z]{3}$ ]] || die "BUDGET_CURRENCY must be a currency code such as GBP"
}

budget() {
  step "Budget $BUDGET_NAME: $BUDGET_AMOUNT $BUDGET_CURRENCY a month, counting only resources labelled $TIFA_LABEL"
  local existing common
  common=(
    --billing-account="$BILLING_ACCOUNT_ID" --billing-project="$GCP_PROJECT_ID"
    --display-name="$BUDGET_NAME" --budget-amount="${BUDGET_AMOUNT}${BUDGET_CURRENCY}"
    --filter-projects="projects/$GCP_PROJECT_ID" --filter-labels="$TIFA_LABEL" --calendar-period=month
    --notifications-rule-pubsub-topic="projects/$GCP_PROJECT_ID/topics/$BUDGET_TOPIC"
  )
  # 50, 90 and 100% of actual spend, and 100% of forecast spend. Emails go to the billing account's admins.
  existing=$({ gcloud billing budgets list --billing-account="$BILLING_ACCOUNT_ID" --billing-project="$GCP_PROJECT_ID" \
    --format='value(name,displayName)' 2>/dev/null || true; } | awk -F'\t' -v n="$BUDGET_NAME" '$2 == n { print $1; exit }')
  if [ -n "$existing" ]; then
    run_quiet gcloud billing budgets update "${existing##*/}" "${common[@]}" --clear-threshold-rules \
      --add-threshold-rule=percent=0.5 --add-threshold-rule=percent=0.9 --add-threshold-rule=percent=1.0 \
      --add-threshold-rule=percent=1.0,basis=forecasted-spend
  else
    run_quiet gcloud billing budgets create "${common[@]}" \
      --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 \
      --threshold-rule=percent=1.0,basis=forecasted-spend
  fi
  info "other services in the project do not count towards it; at 100% the poller closes the public API until the 1st"
}

log_exclusion() {
  step "Logging: drop $API_SERVICE request logs below HTTP 500 (cost, and no client IPs in logs)"
  info "one named exclusion on the project's _Default sink; its filter matches $API_SERVICE's request logs only"
  local filter spec existing
  filter="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$API_SERVICE\""
  filter="$filter AND log_id(\"run.googleapis.com/requests\") AND httpRequest.status<500"
  spec="name=$LOG_EXCLUSION,description=Tifa API requests below 5xx,filter=$filter"
  existing=$(gcloud logging sinks describe _Default --format='value(exclusions[].name)' 2>/dev/null || true)
  if [[ ";$existing;" == *";$LOG_EXCLUSION;"* ]]; then
    run_quiet gcloud logging sinks update _Default --update-exclusion="$spec"
  else
    run_quiet gcloud logging sinks update _Default --add-exclusion="$spec"
  fi
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

# header NAME: the last value of that response header, or nothing (never a failure: set -e would end the tests).
header() { printf '%s\n' "$HEADERS" | { grep -i "^$1:" || true; } | tail -n 1 | cut -d: -f2- | sed 's/^ *//'; }

smoke_api() {
  if [ "$KEEP_CLOSED" = 1 ]; then
    http "$API_URL/v1/meta"
    case $CODE in
      403) pass "API is closed (403), as asked" ;;
      200) pass "API is open" ;;
      *) fail "API /v1/meta answered ${CODE:-nothing}" ;;
    esac
    return
  fi
  http "$API_URL/health"
  case $CODE in
    200) pass "API /health 200" ;;
    503) pass "API /health 503: no poll in the last 10 minutes (expected while the Scheduler is paused)" ;;
    *) fail "API /health answered ${CODE:-nothing}" ;;
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
}

smoke_poller() {
  http "$POLLER_URL/health"
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
      | curl -s -o /dev/null -w '%{http_code}' --max-time 30 -H @- "$POLLER_URL/health" 2>/dev/null || true)
    token=""
    case $CODE in
      200 | 503) pass "poller /health with an identity token ($CODE)" ;;
      403 | 404) info "skip  poller /health with an identity token: $CODE (ingress internal, or no run.invoker for you)" ;;
      *) fail "poller /health with an identity token answered ${CODE:-nothing}" ;;
    esac
  else
    info "skip  poller /health with an identity token (gcloud could not mint one)"
  fi
}

# Who may do what: read from the IAM policies, never printed.
smoke_iam() {
  local svc roles label invokers
  invokers=$(tifa_service_members "$POLLER_SERVICE" roles/run.invoker)
  if has_line "$invokers" allUsers || has_line "$invokers" allAuthenticatedUsers; then
    fail "$POLLER_SERVICE has a public invoker"
  else
    pass "$POLLER_SERVICE has no public invoker"
  fi
  roles=$(tifa_project_roles "serviceAccount:$DEPLOY_SA" | paste -s -d ' ' - || true)
  if [ -z "$roles" ]; then pass "$SA_DEPLOY has no project-wide role"; else fail "$SA_DEPLOY has project-wide roles: $roles"; fi
  for svc in "$POLLER_SERVICE" "$API_SERVICE"; do
    if has_line "$(tifa_service_roles "$svc" "serviceAccount:$DEPLOY_SA")" roles/run.developer; then
      pass "$SA_DEPLOY may update $svc"
    else
      fail "$SA_DEPLOY has no roles/run.developer on $svc (infra/first-deploy.sh grants it)"
    fi
    label=$(gcloud run services describe "$svc" --region="$GCP_REGION" --format='value(metadata.labels.app)' 2>/dev/null || true)
    if [ "$label" = tifa ]; then pass "$svc is labelled $TIFA_LABEL"; else fail "$svc is not labelled $TIFA_LABEL"; fi
  done
}

smoke() {
  step "Smoke tests"
  smoke_api
  smoke_poller
  smoke_iam
  local state
  state=$(gcloud scheduler jobs describe "$SCHEDULER_JOB" --location="$GCP_REGION" --format='value(state)' 2>/dev/null || true)
  info "Scheduler job: ${state:-missing}"
  [ "$state" != PAUSED ] || info "recording is paused: resume with gcloud scheduler jobs resume $SCHEDULER_JOB --location=$GCP_REGION"

  printf '\n    %d passed, %d failed\n' "$PASSED" "$FAILED"
  [ "$FAILED" = 0 ]
}

usage() { sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

KEEP_CLOSED=0
SKIP_BUDGET=0
SMOKE_ONLY=0
for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN=1 ;;
    --keep-closed) KEEP_CLOSED=1 ;;
    --skip-budget) SKIP_BUDGET=1 ;;
    smoke) SMOKE_ONLY=1 ;;
    -h | --help | help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[ "$SMOKE_ONLY" = 0 ] || DRY_RUN=0  # the smoke tests change nothing

settings
[ "$SMOKE_ONLY" = 1 ] || [ "$SKIP_BUDGET" = 1 ] || budget_settings
info "project $GCP_PROJECT_ID, region $GCP_REGION"
[ "$DRY_RUN" = 0 ] || info "dry run: changes are shown, not made"
info "API: $API_URL"
if [ "$SMOKE_ONLY" = 0 ]; then
  invokers
  scheduler_target
  budget_push
  if [ "$SKIP_BUDGET" = 1 ]; then info "budget left alone (--skip-budget)"; else budget; fi
  log_exclusion
fi
if [ "$DRY_RUN" = 1 ]; then
  info "smoke tests skipped in a dry run (infra/post-deploy.sh smoke runs them alone)"
else
  smoke
fi
