#!/usr/bin/env bash
# Tifa: create the two Cloud Run services, once, with your own credentials. The CI deploy identity may only update
# tifa-api and tifa-poller (roles/run.developer on each, granted here), never create a service, so the first deploy
# is an operator's. Idempotent: once both services exist it only re-checks that grant, unless --redeploy.
#
#   infra/first-deploy.sh [--dry-run] [--image=IMAGE] [--redeploy] [--allow-dirty]
#
#   --dry-run      show every change instead of making it (read-only checks still run)
#   --image=IMAGE  deploy an image already in Tifa's Artifact Registry repository (by digest or tag), not a new build
#   --redeploy     deploy even though both services exist (pushes to main normally do that)
#   --allow-dirty  build from a working tree with uncommitted changes to the image's files
#
# Builds the image locally for linux/amd64 with docker buildx and pushes it to tifa-images. Both services get the
# settings in infra/lib.sh (the same as the Deploy workflow), ingress "all", and no public access: the poller never
# gets it, and infra/post-deploy.sh opens the API. Needs gcloud (a project owner), jq, and for a build git and docker
# with buildx. Settings: infra/.env. Run infra/bootstrap.sh first.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=infra/lib.sh
. "$here/lib.sh"
trap tifa_cleanup EXIT

# The files the image is built from (.dockerignore lets in only these and the Dockerfile reads them).
IMAGE_INPUTS=(Dockerfile .dockerignore pyproject.toml uv.lock README.md LICENSE src)

preflight() {
  step "Checks (read-only)"
  local missing="" id state
  gcloud artifacts repositories describe "$AR_REPOSITORY" --location="$GCP_REGION" >/dev/null 2>&1 \
    || missing="$missing repository:$AR_REPOSITORY"
  for id in "$SA_DEPLOY" "$SA_API" "$SA_POLLER"; do
    gcloud iam service-accounts describe "$(tifa_sa "$id")" >/dev/null 2>&1 || missing="$missing service-account:$id"
  done
  if [ -n "$TFL_APP_KEY_VERSION" ]; then
    state=$(gcloud secrets versions describe "$TFL_APP_KEY_VERSION" --secret="$SECRET_NAME" --format='value(state)' \
      2>/dev/null || true)
    [ "$state" = ENABLED ] || missing="$missing secret:$SECRET_NAME/versions/$TFL_APP_KEY_VERSION(${state:-missing})"
  fi
  if [ -n "$missing" ]; then
    if [ "$DRY_RUN" = 1 ]; then warn "not set up yet:$missing (run infra/bootstrap.sh)"; else
      die "not set up yet:$missing: run infra/bootstrap.sh first"
    fi
  else
    info "repository, service accounts${TFL_APP_KEY_VERSION:+ and TfL key version} are in place"
  fi
  POLLER_EXISTS=0
  API_EXISTS=0
  if tifa_service_exists "$POLLER_SERVICE"; then POLLER_EXISTS=1; fi
  if tifa_service_exists "$API_SERVICE"; then API_EXISTS=1; fi
  info "$POLLER_SERVICE: $([ "$POLLER_EXISTS" = 1 ] && echo exists || echo 'not created yet')"
  info "$API_SERVICE: $([ "$API_EXISTS" = 1 ] && echo exists || echo 'not created yet')"
}

# gcloud as docker's credential helper for Artifact Registry (configure-docker edits the docker config file).
docker_auth() {
  local host=$1 config=${DOCKER_CONFIG:-$HOME/.docker}/config.json helper=""
  [ ! -f "$config" ] || helper=$(jq -r --arg h "$host" '.credHelpers[$h] // empty' "$config" 2>/dev/null || true)
  if [ "$helper" = gcloud ]; then
    info "docker signs in to $host through gcloud"
    return
  fi
  info "letting docker sign in to $host through gcloud (adds a credential helper entry to $config)"
  run gcloud auth configure-docker "$host" --quiet
}

build_image() {
  step "Build linux/amd64 and push to $AR_REPOSITORY"
  local repo sha tag dirty meta digest
  command -v git >/dev/null || die "git is not installed"
  command -v docker >/dev/null || die "docker is not installed (or pass --image)"
  docker buildx version >/dev/null 2>&1 || die "docker buildx is not available (or pass --image)"
  repo=$(tifa_image_repo)
  sha=$(git -C "$TIFA_ROOT" rev-parse HEAD 2>/dev/null) || die "$TIFA_ROOT is not a git checkout"
  dirty=$(git -C "$TIFA_ROOT" status --porcelain -- "${IMAGE_INPUTS[@]}")
  tag=$sha
  if [ -n "$dirty" ]; then
    [ "$ALLOW_DIRTY" = 1 ] || die "the image's files have uncommitted changes: commit them, or pass --allow-dirty"
    tag="$sha-dirty-$(date -u +%Y%m%d%H%M%S)"
    warn "building uncommitted changes as $tag"
  fi
  docker_auth "${GCP_REGION}-docker.pkg.dev"
  tifa_tmp
  meta=$TIFA_TMP/build-metadata.json
  run docker buildx build --platform=linux/amd64 --provenance=false --sbom=false \
    --label="org.opencontainers.image.revision=$sha" --tag="$repo:$tag" --metadata-file="$meta" --push "$TIFA_ROOT"
  if [ "$DRY_RUN" = 1 ]; then
    IMAGE="$repo:$tag"
    return
  fi
  digest=$(jq -r '."containerimage.digest" // empty' "$meta" 2>/dev/null || true)
  if ! [[ $digest =~ ^sha256:[0-9a-f]{64}$ ]]; then
    digest=$(gcloud artifacts docker images describe "$repo:$tag" --format='value(image_summary.digest)' 2>/dev/null || true)
  fi
  [[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || die "could not find the digest of $repo:$tag"
  IMAGE="$repo@$digest"
  info "image: $IMAGE"
}

# An image given with --image must be in Tifa's own repository; a tag is resolved to its digest.
use_image() {
  local repo digest
  repo=$(tifa_image_repo)
  case $IMAGE in
    "$repo"@sha256:*) [[ ${IMAGE#"$repo"@} =~ ^sha256:[0-9a-f]{64}$ ]] || die "--image has a malformed digest" ;;
    "$repo":*)
      digest=$(gcloud artifacts docker images describe "$IMAGE" --format='value(image_summary.digest)' 2>/dev/null || true)
      if [[ $digest =~ ^sha256:[0-9a-f]{64}$ ]]; then
        IMAGE="$repo@$digest"
      elif [ "$DRY_RUN" = 0 ]; then
        die "no image $IMAGE in $AR_REPOSITORY"
      fi
      ;;
    *) die "--image must be $repo@sha256:... or $repo:TAG" ;;
  esac
  info "image: $IMAGE"
}

deploy() {
  local -a create
  step "Deploy $POLLER_SERVICE (private: no public access, ever)"
  # --no-allow-unauthenticated also removes allUsers if anyone added it; the CI identity cannot change IAM.
  tifa_deploy_poller "$IMAGE" --no-allow-unauthenticated
  step "Deploy $API_SERVICE (public only once infra/post-deploy.sh opens it)"
  create=()
  [ "$API_EXISTS" = 1 ] || create=(--no-allow-unauthenticated)
  tifa_deploy_api "$IMAGE" ${create[@]+"${create[@]}"}
}

grant_deploy() {
  step "CI may update $POLLER_SERVICE and $API_SERVICE, and nothing else"
  local svc
  for svc in "$POLLER_SERVICE" "$API_SERVICE"; do
    if tifa_service_roles "$svc" "serviceAccount:$DEPLOY_SA" | grep -qxF roles/run.developer; then
      info "$SA_DEPLOY has roles/run.developer on $svc"
    else
      retry run_quiet gcloud run services add-iam-policy-binding "$svc" --region="$GCP_REGION" \
        --member="serviceAccount:$DEPLOY_SA" --role=roles/run.developer
    fi
  done
}

usage() { sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

IMAGE=""
REDEPLOY=0
ALLOW_DIRTY=0
for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN=1 ;;
    --image=?*) IMAGE=${arg#--image=} ;;
    --redeploy) REDEPLOY=1 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    -h | --help | help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

tifa_settings
command -v jq >/dev/null || die "jq is not installed"
tifa_project_number
info "project $GCP_PROJECT_ID, region $GCP_REGION"
[ "$DRY_RUN" = 0 ] || info "dry run: changes are shown, not made"
preflight
if [ "$POLLER_EXISTS" = 1 ] && [ "$API_EXISTS" = 1 ] && [ "$REDEPLOY" = 0 ]; then
  info "both services exist: pushes to main deploy them now (pass --redeploy to deploy from here anyway)"
else
  if [ -n "$IMAGE" ]; then use_image; else build_image; fi
  deploy
fi
grant_deploy

step "Done"
for svc in "$POLLER_SERVICE" "$API_SERVICE"; do
  url=$(gcloud run services describe "$svc" --region="$GCP_REGION" --format='value(status.url)' 2>/dev/null || true)
  info "$svc: ${url:-(not deployed)}"
done
cat <<EOF

Next: infra/post-deploy.sh wires the services (Scheduler and budget push, the guard's role, public access to the
API), creates the budget and the log exclusion, and runs the smoke tests.
EOF
