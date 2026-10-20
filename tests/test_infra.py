"""The infra scripts, run for real against fake gcloud, docker, curl and gh executables (nothing reaches Google Cloud).

The fakes log every call and answer from a state directory, so each test can check what a script would change:
only tifa-* resources, labelled app=tifa; a deploy identity that may update the two services and nothing else; a
poller that is never public; dry runs that change nothing; secrets and tokens that are never printed.
The scripts run under the bash in /usr/bin or /bin: 3.2 on macOS, 5 on CI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
SCRIPTS = ("bootstrap.sh", "first-deploy.sh", "post-deploy.sh")
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

PROJECT = "example-tifa-1"
REGION = "europe-west1"
BILLING = "-".join(["0" * 6] * 3)  # built at run time: written out, it would trip the billing-ID secret scan
TOKEN = "fake-identity-token-" + "7" * 20
KEY = "abcd" * 8  # the length of a TfL key, not a real one
REPO = f"{REGION}-docker.pkg.dev/{PROJECT}/tifa-images/tifa"
TAGGED_DIGEST = "sha256:" + "a" * 64  # what the fake registry resolves any tag to
BUILT_DIGEST = "sha256:" + "b" * 64  # what the fake docker build reports
DEPLOY_MEMBER = "serviceAccount:tifa-deploy@"  # followed by the project's service account domain
READ_ONLY = {"describe", "list", "get-iam-policy", "print-identity-token"}

pytestmark = pytest.mark.skipif(
    not (Path("/bin/bash").exists() or Path("/usr/bin/bash").exists()) or shutil.which("jq") is None,
    reason="needs bash and jq",
)

FAKE_GCLOUD = r"""#!/bin/bash
# Fake gcloud: logs each call, then answers from the state directory $FAKE_STATE.
set -u
S=$FAKE_STATE
{ for a in "$@"; do printf '%s\037' "$a"; done; printf '\n'; } >>"$S/gcloud.log"
ARGS=("$@")
W=()
for a in "$@"; do case $a in -*) ;; *) W+=("$a") ;; esac; done
w0=${W[0]:-}; w1=${W[1]:-}; w2=${W[2]:-}; w3=${W[3]:-}; w4=${W[4]:-}
opt() {
  local a
  for a in "${ARGS[@]}"; do case $a in "--$1="*) printf '%s' "${a#--$1=}"; return 0 ;; esac; done
  return 1
}
fmt=$(opt format || true)
has() { [ -e "$S/exists/$1" ]; }
mk() { mkdir -p "$S/exists"; : >"$S/exists/$1"; }
missing() { echo "ERROR: (gcloud) NOT_FOUND: $1 does not exist" >&2; exit 1; }
num=12345
bind() {
  local line
  line=$(printf '%s\t%s' "$(opt role)" "$(opt member)")
  grep -qxF "$line" "$S/iam-$1" 2>/dev/null || printf '%s\n' "$line" >>"$S/iam-$1"
}
unbind() {
  local line
  line=$(printf '%s\t%s' "$(opt role)" "$(opt member)")
  [ -f "$S/iam-$1" ] || return 0
  grep -vxF "$line" "$S/iam-$1" >"$S/iam-$1.new" || true
  mv "$S/iam-$1.new" "$S/iam-$1"
}
case "$w0 $w1" in
  "auth list") echo operator@example.com ;;
  "auth print-identity-token") echo "$FAKE_TOKEN" ;;
  "projects describe") echo "$num" ;;
  "projects get-iam-policy") cat "$S/iam-project" 2>/dev/null || true ;;
  "projects add-iam-policy-binding") bind project ;;
  "billing projects") echo True ;;
  "billing budgets")
    case $w2 in
      list) cat "$S/budgets" 2>/dev/null || true ;;
      create) printf 'billingAccounts/x/budgets/b1\t%s\n' "$(opt display-name)" >>"$S/budgets" ;;
    esac ;;
  "firestore databases")
    case $w2 in
      list) cat "$S/firestore-list" 2>/dev/null || true ;;
      describe)
        has firestore || missing "database (default)"
        printf 'FIRESTORE_NATIVE\t%s\n' "${FAKE_FS_LOCATION:-europe-west1}" ;;
      create) mk firestore ;;
    esac ;;
  "firestore indexes")
    g=$(opt collection-group)
    case $w3 in
      describe) if has "index-$g-$w4"; then echo False; else echo True; fi ;;
      update) mk "index-$g-$w4" ;;
    esac ;;
  "artifacts repositories")
    case $w2 in describe) has "ar-$w3" || missing "$w3"; echo tifa ;; create) mk "ar-$w3" ;; esac ;;
  "artifacts docker") echo "$FAKE_TAGGED_DIGEST" ;;
  "iam service-accounts")
    id=${w3%%@*}
    case $w2 in describe) has "sa-$id" || missing "$id" ;; create) mk "sa-$id" ;; esac ;;
  "iam roles")
    case $w2 in describe) has "role-$w3" || missing "$w3" ;; create) mk "role-$w3" ;; esac ;;
  "iam workload-identity-pools")
    if [ "$w2" = providers ]; then
      case $w3 in describe) has "provider-$w4" || missing "$w4"; echo ACTIVE ;; create-oidc) mk "provider-$w4" ;; esac
    else
      case $w2 in describe) has "pool-$w3" || missing "$w3"; echo ACTIVE ;; create) mk "pool-$w3" ;; esac
    fi ;;
  "secrets describe") has "secret-$w2" || missing "$w2"; echo tifa ;;
  "secrets create") mk "secret-$w2" ;;
  "secrets versions")
    case $w2 in
      list) cat "$S/secret-versions" 2>/dev/null || true ;;
      describe) has "secret-version-$w3" || missing "$w3"; echo ENABLED ;;
      add) cat >"$S/secret-payload"; printf 'projects/%s/secrets/%s/versions/7\n' "$num" "$w3" ;;
    esac ;;
  "pubsub topics")
    case $w2 in describe) has "topic-$w3" || missing "$w3"; echo tifa ;; create) mk "topic-$w3" ;; esac ;;
  "pubsub subscriptions")
    case $w2 in describe) has "sub-$w3" || missing "$w3" ;; create) mk "sub-$w3" ;; esac ;;
  "scheduler jobs")
    case $w2 in
      describe)
        has "job-$w3" || missing "$w3"
        case $fmt in
          *state*) echo PAUSED ;;
          *audience*) cat "$S/job-audience" ;;
          *uri*) cat "$S/job-uri" ;;
        esac ;;
      create | update)
        [ "$w2" = update ] || mk "job-$w4"
        if opt uri >/dev/null; then opt uri >"$S/job-uri"; opt oidc-token-audience >"$S/job-audience"; fi ;;
    esac ;;
  "logging sinks") [ "$w2" != describe ] || cat "$S/exclusions" 2>/dev/null || true ;;
  "run deploy") mk "run-$w2" ;;
  "run services")
    case $w2 in
      describe)
        has "run-$w3" || missing "$w3"
        url="https://$w3-$num.$REGION_NAME.run.app"
        case $fmt in
          json)
            printf '{"metadata":{"name":"%s","annotations":{"run.googleapis.com/urls":"[\\"%s\\"]"}},' "$w3" "$url"
            printf '"status":{"url":"%s"}}\n' "$url" ;;
          *metadata.name*) echo "$w3" ;;
          *labels.app*) echo tifa ;;
          *status.url*) echo "$url" ;;
          *Revision*) echo "$w3-00001-abc" ;;
        esac ;;
      get-iam-policy) cat "$S/iam-run-$w3" 2>/dev/null || true ;;
      add-iam-policy-binding) bind "run-$w3" ;;
      remove-iam-policy-binding) unbind "run-$w3" ;;
    esac ;;
esac
exit 0
"""

FAKE_CURL = r"""#!/bin/bash
# Fake curl: answers like a healthy Tifa with the Scheduler paused (the poller is always private).
set -u
S=$FAKE_STATE
{ for a in "$@"; do printf '%s\037' "$a"; done; printf '\n'; } >>"$S/curl.log"
out=/dev/null head="" url="" method=GET inm=0 auth=0
while [ $# -gt 0 ]; do
  case $1 in
    -o) out=$2; shift ;;
    -D) head=$2; shift ;;
    -w | --max-time | --data) shift ;;
    -X) method=$2; shift ;;
    -H)
      case $2 in
        @-) if grep -q "^Authorization: Bearer $FAKE_TOKEN\$"; then auth=1; fi ;;
        If-None-Match:*) inm=1 ;;
      esac
      shift ;;
    http*) url=$1 ;;
  esac
  shift
done
code=404 body="" hdrs=""
case $url in
  *tifa-poller*) if [ "$auth" = 1 ]; then code=200; else code=403; fi ;;
  *)
    # Public only once allUsers may invoke it (infra/post-deploy.sh), as on Cloud Run.
    grep -q allUsers "$S/iam-run-tifa-api" 2>/dev/null || method=CLOSED
    case "$method ${url#https://*/}" in
      CLOSED*) code=403 ;;
      "GET health") code=503; body='{"error":{"code":"stale"}}' ;;
      "GET v1/meta") code=200; body='{"data":{},"meta":{"attribution":"Powered by TfL Open Data"}}' ;;
      "GET v1/status")
        code=200
        hdrs=$'ETag: "abc"\r\nCache-Control: public, max-age=30\r\n'
        [ "$inm" = 0 ] || code=304 ;;
      "OPTIONS v1/status") code=204; hdrs=$'Access-Control-Allow-Origin: *\r\n' ;;
      "POST mcp") code=200; body='{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"tifa"}}}' ;;
    esac ;;
esac
[ "$out" = /dev/null ] || printf '%s' "$body" >"$out"
[ -z "$head" ] || printf 'HTTP/2 %s\r\n%s\r\n' "$code" "$hdrs" >"$head"
printf '%s' "$code"
"""

FAKE_DOCKER = r"""#!/bin/bash
set -u
{ for a in "$@"; do printf '%s\037' "$a"; done; printf '\n'; } >>"$FAKE_STATE/docker.log"
case "${1:-} ${2:-}" in
  "buildx version") echo "github.com/docker/buildx v0.29.1" ;;
  "buildx build")
    for a in "$@"; do
      case $a in
        --metadata-file=*) printf '{"containerimage.digest": "%s"}\n' "$FAKE_BUILT_DIGEST" >"${a#--metadata-file=}" ;;
      esac
    done ;;
esac
"""

FAKE_GH = r"""#!/bin/bash
{ for a in "$@"; do printf '%s\037' "$a"; done; printf '\n'; } >>"$FAKE_STATE/gh.log"
"""


def env_text(**overrides: str) -> str:
    values = {
        "GCP_PROJECT_ID": PROJECT,
        "GCP_REGION": REGION,
        "FIRESTORE_LOCATION": REGION,
        "GITHUB_REPOSITORY": "example-owner/tifa",
        "GITHUB_REPOSITORY_OWNER_ID": "4242",
        "BILLING_ACCOUNT_ID": BILLING,
        "BUDGET_AMOUNT": "3.75",
        "BUDGET_CURRENCY": "GBP",
    } | overrides
    return "# test settings\n" + "".join(f"{k}={v}\n" for k, v in values.items())


def words(call: list[str]) -> list[str]:
    return [a for a in call if not a.startswith("-")]


def read_only(call: list[str]) -> bool:
    w = words(call)
    return bool(READ_ONLY & set(w)) or w[:2] == ["auth", "list"]


def flag(call: list[str], name: str) -> str | None:
    for a in call:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return None


class Infra:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.state = tmp / "state"
        self.bin = tmp / "bin"
        self.home = tmp / "home"
        for d in (self.state, self.bin, self.home, tmp / "docker"):
            d.mkdir()
        self.env_file = tmp / "tifa.env"
        self.env_file.write_text(env_text())
        for name, body in {"gcloud": FAKE_GCLOUD, "curl": FAKE_CURL, "docker": FAKE_DOCKER, "gh": FAKE_GH}.items():
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        for tool in ("jq", "git"):  # real tools that may live next to a real gcloud: link them, not their directory
            found = shutil.which(tool)
            if found and str(Path(found).parent) not in SYSTEM_PATH.split(":"):
                (self.bin / tool).symlink_to(found)

    def environ(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}:{SYSTEM_PATH}",
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp),
            "LC_ALL": "C",
            "FAKE_STATE": str(self.state),
            "FAKE_TOKEN": TOKEN,
            "FAKE_TAGGED_DIGEST": TAGGED_DIGEST,
            "FAKE_BUILT_DIGEST": BUILT_DIGEST,
            "REGION_NAME": REGION,
            "TIFA_ENV_FILE": str(self.env_file),
            "DOCKER_CONFIG": str(self.tmp / "docker"),
        }
        return env | (extra or {})

    def run(self, script: str, *args: str, stdin: str | None = None, env: dict[str, str] | None = None):
        return subprocess.run(
            [str(INFRA / script), *args],
            env=self.environ(env),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=ROOT,
        )

    def bash(self, code: str, env: dict[str, str] | None = None):
        """Run bash code with the library sourced, as the Deploy workflow does (no infra/.env)."""
        return subprocess.run(
            ["bash", "-eo", "pipefail", "-c", f". infra/lib.sh\n{code}"],
            env=self.environ(env),
            capture_output=True,
            text=True,
            timeout=60,
            cwd=ROOT,
        )

    def calls(self, tool: str = "gcloud") -> list[list[str]]:
        log = self.state / f"{tool}.log"
        if not log.exists():
            return []
        return [line.split("\x1f")[:-1] for line in log.read_text().splitlines()]

    def mutations(self) -> list[list[str]]:
        return [c for c in self.calls() if not read_only(c)]

    def find(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls() if words(c)[: len(prefix)] == list(prefix)]

    def seed(self, *keys: str) -> None:
        (self.state / "exists").mkdir(exist_ok=True)
        for key in keys:
            (self.state / "exists" / key).touch()

    def exists(self, key: str) -> bool:
        return (self.state / "exists" / key).exists()

    def iam(self, scope: str) -> set[tuple[str, str]]:
        path = self.state / f"iam-{scope}"
        if not path.exists():
            return set()
        return {tuple(line.split("\t", 1)) for line in path.read_text().splitlines()}

    def reset_log(self) -> None:
        for log in self.state.glob("*.log"):
            log.unlink()


@pytest.fixture
def infra(tmp_path):
    return Infra(tmp_path)


def ok(proc):
    assert proc.returncode == 0, f"exit {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    return proc


def deployed(infra: Infra) -> None:
    ok(infra.run("bootstrap.sh"))
    ok(infra.run("first-deploy.sh", f"--image={REPO}:v1"))


# ---- static checks


@pytest.mark.parametrize("script", [*SCRIPTS, "lib.sh"])
def test_scripts_parse(script):
    subprocess.run(["bash", "-n", str(INFRA / script)], check=True)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_shellcheck():
    subprocess.run(["shellcheck", "-x", *(f"infra/{s}" for s in (*SCRIPTS, "lib.sh"))], check=True, cwd=ROOT)


def test_every_name_is_prefixed(infra):
    names = "AR_REPOSITORY API_SERVICE POLLER_SERVICE SCHEDULER_JOB SECRET_NAME BUDGET_TOPIC BUDGET_SUBSCRIPTION"
    names += " WIF_POOL WIF_PROVIDER LOG_EXCLUSION SA_DEPLOY SA_API SA_POLLER SA_SCHEDULER SA_PUSH"
    proc = ok(infra.bash(f'for n in {names}; do printf "%s\\n" "${{!n}}"; done; echo "$GUARD_ROLE $TIFA_LABEL"'))
    *values, last = proc.stdout.splitlines()
    assert values and all(v.startswith("tifa-") for v in values), values
    assert last == "tifa_guard app=tifa"  # custom role IDs cannot contain hyphens


def test_the_deploy_workflow_takes_its_settings_from_the_library():
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    for needed in (
        "tifa_require_services",
        "tifa_deploy_poller",
        "tifa_deploy_api",
        "workloadIdentityPools/tifa-github",
    ):
        assert needed in workflow
    for forbidden in ("gcloud run deploy", "allow-unauthenticated", "add-iam-policy-binding", "set-iam-policy"):
        assert forbidden not in workflow


# ---- settings


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("args", [(), ("--dry-run",)])
def test_no_env_file_no_run(infra, script, args):
    proc = infra.run(script, *args, env={"TIFA_ENV_FILE": str(infra.tmp / "missing.env")})
    assert proc.returncode == 1
    assert "infra/env.example" in proc.stderr and "nothing was changed" in proc.stderr
    assert infra.calls() == []


def test_unknown_settings_are_refused(infra):
    infra.env_file.write_text(env_text() + "API_SERVICE=website\n")
    proc = infra.run("bootstrap.sh", "--dry-run")
    assert proc.returncode == 1 and "unknown setting API_SERVICE" in proc.stderr
    assert infra.calls() == []


def test_placeholders_are_refused(infra):
    infra.env_file.write_text(env_text(GCP_PROJECT_ID="your-project-id"))
    proc = infra.run("bootstrap.sh", "--dry-run")
    assert proc.returncode == 1 and "placeholder" in proc.stderr


def test_the_shell_cannot_redirect_a_script_to_another_project(infra):
    ok(infra.run("bootstrap.sh", "--dry-run", env={"GCP_PROJECT_ID": "example-website-2"}))
    described = infra.find("projects", "describe")
    assert described and all(words(c)[2] == PROJECT for c in described)


# ---- bootstrap


def test_bootstrap_dry_run_changes_nothing(infra):
    proc = ok(infra.run("bootstrap.sh", "--dry-run"))
    assert infra.mutations() == []
    assert "[dry run] $ gcloud firestore databases create" in proc.stdout
    assert "[dry run] $ gcloud artifacts repositories create tifa-images" in proc.stdout


def test_bootstrap_creates_only_tifa_resources(infra):
    ok(infra.run("bootstrap.sh"))
    created = [c for c in infra.mutations() if {"create", "create-oidc"} & set(words(c))]
    names = []
    for call in created:
        w = words(call)
        verb = "create" if "create" in w else "create-oidc"
        rest = w[w.index(verb) + 1 :]
        if w[:2] == ["firestore", "databases"]:
            assert flag(call, "database") == "(default)"
            continue
        if w[:3] == ["beta", "services", "identity"]:
            continue  # the Pub/Sub service agent: Google's own identity for the project
        names.append(rest[-1] if w[:2] == ["scheduler", "jobs"] else rest[0])
    assert names and all(n.startswith(("tifa-", "tifa_")) for n in names), names
    for group in (["artifacts", "repositories"], ["secrets"], ["pubsub", "topics"]):
        (call,) = [c for c in created if words(c)[: len(group)] == group]
        assert flag(call, "labels") == "app=tifa", call


def test_bootstrap_firestore_database(infra):
    ok(infra.run("bootstrap.sh"))
    (create,) = infra.find("firestore", "databases", "create")
    assert flag(create, "location") == REGION and flag(create, "type") == "firestore-native"
    assert "--delete-protection" in create
    assert infra.exists("index-meta-status") and infra.exists("index-polls-failed")


def test_bootstrap_never_touches_a_database_it_did_not_set_up(infra):
    infra.seed("firestore")
    (infra.state / "firestore-list").write_text(
        f"projects/{PROJECT}/databases/(default)\nprojects/{PROJECT}/databases/web\n"
    )
    proc = infra.run("bootstrap.sh")
    assert proc.returncode == 1 and "never changes an existing database" in proc.stderr
    assert "other databases in the project, never touched: web" in proc.stdout
    assert not [c for c in infra.mutations() if words(c)[0] == "firestore"]

    infra.reset_log()
    ok(infra.run("bootstrap.sh", "--adopt-firestore"))
    assert not infra.find("firestore", "databases", "create")
    assert infra.exists("index-meta-status")


def test_bootstrap_is_idempotent(infra):
    ok(infra.run("bootstrap.sh"))
    infra.reset_log()
    proc = ok(infra.run("bootstrap.sh"))
    created = [c for c in infra.mutations() if {"create", "create-oidc", "undelete"} & set(words(c))]
    assert [words(c)[:3] for c in created] == [["beta", "services", "identity"]]  # "make sure it exists", every time
    assert "exists, set up by Tifa" in proc.stdout


def test_bootstrap_identities(infra):
    ok(infra.run("bootstrap.sh"))
    project = infra.iam("project")
    assert {role for role, member in project if member.startswith("serviceAccount:tifa-api@")} == {
        "roles/datastore.viewer"
    }
    assert {role for role, member in project if member.startswith("serviceAccount:tifa-poller@")} == {
        "roles/datastore.user",
        "roles/monitoring.viewer",
    }
    assert not [m for _, m in project if m.startswith(DEPLOY_MEMBER)]  # no project-wide role for CI
    deploy_grants = [c for c in infra.mutations() if f"--member={DEPLOY_MEMBER}" in " ".join(c)]
    targets = {(words(c)[0], words(c)[3].split("@")[0], flag(c, "role")) for c in deploy_grants}
    assert targets == {
        ("iam", "tifa-api", "roles/iam.serviceAccountUser"),
        ("iam", "tifa-poller", "roles/iam.serviceAccountUser"),
        ("artifacts", "tifa-images", "roles/artifactregistry.writer"),
    }


def test_bootstrap_workload_identity(infra):
    ok(infra.run("bootstrap.sh"))
    (pool,) = infra.find("iam", "workload-identity-pools", "create")
    assert words(pool)[3] == "tifa-github"
    (provider,) = infra.find("iam", "workload-identity-pools", "providers", "create-oidc")
    assert words(provider)[4] == "tifa-github-actions"
    condition = flag(provider, "attribute-condition")
    assert "assertion.repository_owner_id == '4242'" in condition
    assert "assertion.repository == 'example-owner/tifa'" in condition
    assert "assertion.ref == 'refs/heads/main'" in condition


def test_bootstrap_scheduler_job_starts_paused(infra):
    ok(infra.run("bootstrap.sh"))
    assert infra.find("scheduler", "jobs", "create", "http", "tifa-poll")
    assert infra.find("scheduler", "jobs", "pause", "tifa-poll")
    assert not [c for c in infra.calls() if "resume" in words(c)]


def test_tfl_key_is_never_printed(infra):
    infra.seed("secret-tifa-tfl-app-key")
    proc = ok(infra.run("bootstrap.sh", "tfl-key", stdin=KEY + "\n"))
    assert (infra.state / "secret-payload").read_text() == KEY
    assert KEY not in proc.stdout + proc.stderr
    assert not [c for c in infra.calls() if KEY in " ".join(c)]
    assert "version 7" in proc.stdout


def test_tfl_key_dry_run_stores_nothing(infra):
    infra.seed("secret-tifa-tfl-app-key")
    proc = ok(infra.run("bootstrap.sh", "tfl-key", "--dry-run", stdin=KEY + "\n"))
    assert not (infra.state / "secret-payload").exists()
    assert KEY not in proc.stdout + proc.stderr


def test_tfl_key_rejects_junk(infra):
    infra.seed("secret-tifa-tfl-app-key")
    proc = infra.run("bootstrap.sh", "tfl-key", stdin="not a key\n")
    assert proc.returncode == 1 and "nothing was stored" in proc.stderr
    assert not (infra.state / "secret-payload").exists()


# ---- first deploy


def test_first_deploy_dry_run_changes_nothing(infra):
    proc = ok(infra.run("first-deploy.sh", "--dry-run", f"--image={REPO}:v1"))
    assert infra.mutations() == []
    assert "[dry run] $ gcloud run deploy tifa-poller" in proc.stdout


def test_first_deploy_needs_the_bootstrap(infra):
    proc = infra.run("first-deploy.sh", f"--image={REPO}:v1")
    assert proc.returncode == 1 and "infra/bootstrap.sh" in proc.stderr
    assert infra.mutations() == []


def test_first_deploy_creates_both_services(infra):
    deployed(infra)
    (poller,) = infra.find("run", "deploy", "tifa-poller")
    (api,) = infra.find("run", "deploy", "tifa-api")
    for call in (poller, api):
        assert flag(call, "image") == f"{REPO}@{TAGGED_DIGEST}"  # deployed by digest
        assert flag(call, "ingress") == "all"
        assert "--no-allow-unauthenticated" in call and "--allow-unauthenticated" not in call
        assert flag(call, "execution-environment") == "gen1" and "--cpu-throttling" in call
    assert flag(poller, "labels") == "app=tifa,role=poller"
    assert (flag(poller, "cpu"), flag(poller, "memory"), flag(poller, "concurrency")) == ("0.25", "256Mi", "1")
    assert (flag(poller, "max-instances"), flag(poller, "timeout")) == ("2", "60s")
    assert flag(poller, "service-account").startswith("tifa-poller@")
    assert flag(api, "labels") == "app=tifa,role=api"
    assert (flag(api, "cpu"), flag(api, "memory"), flag(api, "concurrency")) == ("1", "512Mi", "40")
    assert (flag(api, "max-instances"), flag(api, "timeout")) == ("1", "30s")
    assert flag(api, "service-account").startswith("tifa-api@")
    assert not flag(poller, "set-secrets") and "--clear-secrets" not in poller  # no key yet, a new service


def test_first_deploy_grants_ci_the_two_services_only(infra):
    deployed(infra)
    for svc in ("tifa-poller", "tifa-api"):
        grants = {role for role, member in infra.iam(f"run-{svc}") if member.startswith(DEPLOY_MEMBER)}
        assert grants == {"roles/run.developer"}
    assert not [m for _, m in infra.iam("project") if m.startswith(DEPLOY_MEMBER)]
    assert not [c for c in infra.calls() if "--member=allUsers" in c]


def test_first_deploy_mounts_the_pinned_key(infra):
    infra.env_file.write_text(env_text(TFL_APP_KEY_VERSION="3"))
    ok(infra.run("bootstrap.sh"))
    infra.seed("secret-version-3")
    ok(infra.run("first-deploy.sh", f"--image={REPO}:v1"))
    (poller,) = infra.find("run", "deploy", "tifa-poller")
    assert flag(poller, "set-secrets") == "TFL_APP_KEY=tifa-tfl-app-key:3"
    (api,) = infra.find("run", "deploy", "tifa-api")
    assert not flag(api, "set-secrets")


def test_first_deploy_refuses_a_missing_key_version(infra):
    infra.env_file.write_text(env_text(TFL_APP_KEY_VERSION="9"))
    ok(infra.run("bootstrap.sh"))
    proc = infra.run("first-deploy.sh", f"--image={REPO}:v1")
    assert proc.returncode == 1 and "versions/9" in proc.stderr
    assert not infra.find("run", "deploy")


def test_first_deploy_only_takes_images_from_the_tifa_repository(infra):
    ok(infra.run("bootstrap.sh"))
    proc = infra.run("first-deploy.sh", "--image=docker.io/library/python:3.13")
    assert proc.returncode == 1 and "--image must be" in proc.stderr
    assert not infra.find("run", "deploy")


def test_first_deploy_builds_for_amd64(infra):
    ok(infra.run("bootstrap.sh"))
    ok(infra.run("first-deploy.sh", "--allow-dirty"))
    builds = [c for c in infra.calls("docker") if c[:2] == ["buildx", "build"]]
    (build,) = builds
    assert "--platform=linux/amd64" in build and "--push" in build and Path(build[-1]).resolve() == ROOT
    assert flag(build, "tag").startswith(f"{REPO}:")
    assert infra.find("auth", "configure-docker")
    (poller,) = infra.find("run", "deploy", "tifa-poller")
    assert flag(poller, "image") == f"{REPO}@{BUILT_DIGEST}"


def test_first_deploy_leaves_existing_services_to_ci(infra):
    deployed(infra)
    infra.reset_log()
    proc = ok(infra.run("first-deploy.sh", f"--image={REPO}:v2"))
    assert infra.mutations() == []
    assert "pushes to main deploy them now" in proc.stdout


def test_first_deploy_redeploy_keeps_the_api_closed_or_open(infra):
    deployed(infra)
    infra.reset_log()
    ok(infra.run("first-deploy.sh", f"--image={REPO}:v2", "--redeploy"))
    (api,) = infra.find("run", "deploy", "tifa-api")
    assert not [a for a in api if "allow-unauthenticated" in a]  # its public access is post-deploy's and the guard's
    (poller,) = infra.find("run", "deploy", "tifa-poller")
    assert "--no-allow-unauthenticated" in poller and "--clear-secrets" in poller


# ---- post-deploy


def test_post_deploy_dry_run_changes_nothing(infra):
    deployed(infra)
    infra.reset_log()
    proc = ok(infra.run("post-deploy.sh", "--dry-run"))
    assert infra.mutations() == []
    assert not infra.calls("curl")
    assert "[dry run] $ gcloud billing budgets create" in proc.stdout


def test_post_deploy_wires_everything_and_passes_its_smoke_tests(infra):
    deployed(infra)
    infra.reset_log()
    proc = ok(infra.run("post-deploy.sh"))
    assert "0 failed" in proc.stdout

    poller = infra.iam("run-tifa-poller")
    assert {m.split("@")[0] for r, m in poller if r == "roles/run.invoker"} == {
        "serviceAccount:tifa-scheduler",
        "serviceAccount:tifa-pubsub-push",
    }
    api = infra.iam("run-tifa-api")
    assert ("roles/run.invoker", "allUsers") in api
    assert {m.split("@")[0] for r, m in api if r.endswith("/roles/tifa_guard")} == {"serviceAccount:tifa-poller"}
    public = [c for c in infra.calls() if "--member=allUsers" in c]
    assert public and all(words(c)[3] == "tifa-api" for c in public)

    (sub,) = infra.find("pubsub", "subscriptions", "create")
    assert words(sub)[3] == "tifa-budget-alerts-to-poller" and flag(sub, "topic") == "tifa-budget-alerts"
    assert flag(sub, "push-endpoint") == f"https://tifa-poller-12345.{REGION}.run.app/internal/budget"
    assert flag(sub, "push-auth-service-account").startswith("tifa-pubsub-push@")
    assert flag(sub, "labels") == "app=tifa"

    (budget,) = infra.find("billing", "budgets", "create")
    assert flag(budget, "filter-labels") == "app=tifa"
    assert flag(budget, "filter-projects") == f"projects/{PROJECT}"
    assert flag(budget, "budget-amount") == "3.75GBP"
    assert flag(budget, "display-name") == f"tifa-budget-{PROJECT}"
    assert flag(budget, "notifications-rule-pubsub-topic").endswith("/topics/tifa-budget-alerts")

    (exclusion,) = infra.find("logging", "sinks", "update", "_Default")
    spec = flag(exclusion, "add-exclusion")
    assert spec.startswith("name=tifa-api-requests-below-5xx,")
    assert 'resource.labels.service_name="tifa-api"' in spec and "httpRequest.status<500" in spec

    assert TOKEN not in proc.stdout + proc.stderr
    assert not [c for c in infra.calls("curl") if TOKEN in " ".join(c)]


def test_post_deploy_is_idempotent(infra):
    deployed(infra)
    ok(infra.run("post-deploy.sh"))
    infra.reset_log()
    ok(infra.run("post-deploy.sh"))
    assert not infra.find("pubsub", "subscriptions", "create")
    assert not infra.find("billing", "budgets", "create")
    assert infra.find("billing", "budgets", "update", "b1")


def test_post_deploy_takes_public_access_away_from_the_poller(infra):
    deployed(infra)
    with (infra.state / "iam-run-tifa-poller").open("a") as policy:
        policy.write("roles/run.invoker\tallUsers\n")
    ok(infra.run("post-deploy.sh", "--skip-budget"))
    assert ("roles/run.invoker", "allUsers") not in infra.iam("run-tifa-poller")


def test_post_deploy_keep_closed_leaves_the_api_alone(infra):
    deployed(infra)
    ok(infra.run("post-deploy.sh", "--keep-closed", "--skip-budget"))
    assert not [c for c in infra.calls() if "--member=allUsers" in c]
    assert not infra.find("billing", "budgets")


def test_post_deploy_smoke_only_changes_nothing(infra):
    deployed(infra)
    infra.reset_log()
    proc = infra.run("post-deploy.sh", "smoke")
    assert infra.mutations() == []
    assert proc.returncode == 1  # not wired yet: the API is still closed
    assert "FAIL  API /health answered 403" in proc.stdout and "FAIL  MCP initialize answered 403" in proc.stdout
    assert "ok    tifa-poller has no public invoker" in proc.stdout  # every check ran


# ---- the library as the Deploy workflow uses it


def ci_env(**extra: str) -> dict[str, str]:
    return {"GITHUB_ACTIONS": "true", "GCP_PROJECT_ID": PROJECT, "GCP_REGION": REGION} | extra


def test_ci_stops_before_building_when_the_services_are_missing(infra):
    proc = infra.bash("tifa_check_deploy_settings; tifa_require_services", env=ci_env())
    assert proc.returncode == 1
    assert "::error::" in proc.stdout and "infra/first-deploy.sh" in proc.stdout


def test_ci_deploys_change_no_iam(infra):
    infra.seed("run-tifa-poller", "run-tifa-api")
    image = f"{REPO}@{BUILT_DIGEST}"
    proc = ok(
        infra.bash(f'tifa_check_deploy_settings; tifa_deploy_poller "{image}"; tifa_deploy_api "{image}"', env=ci_env())
    )
    assert "::warning::TFL_APP_KEY_VERSION is not set" in proc.stdout
    assert not [c for c in infra.mutations() if "iam" in " ".join(words(c)) or "allow-unauthenticated" in " ".join(c)]
    (poller,) = infra.find("run", "deploy", "tifa-poller")
    assert "--clear-secrets" in poller and flag(poller, "ingress") == "all"
    (api,) = infra.find("run", "deploy", "tifa-api")
    assert flag(api, "labels") == "app=tifa,role=api"


def test_ci_mounts_the_pinned_key_and_publishes_only_live_data(infra):
    infra.seed("run-tifa-poller", "run-tifa-api")
    image = f"{REPO}@{BUILT_DIGEST}"
    code = (
        f'tifa_check_deploy_settings; tifa_deploy_poller "{image}"; tifa_deploy_api "{image}"; cat "$TIFA_TMP"/*.yaml'
    )
    proc = ok(infra.bash(code, env=ci_env(TFL_APP_KEY_VERSION="3")))
    (poller,) = infra.find("run", "deploy", "tifa-poller")
    assert flag(poller, "set-secrets") == "TFL_APP_KEY=tifa-tfl-app-key:3"
    assert '"TIFA_PUBLIC_SOURCES": "a"' in proc.stdout
    assert f'"TIFA_ALLOWED_HOSTS": "tifa-api-12345.{REGION}.run.app"' in proc.stdout


def test_ci_rejects_bad_variables(infra):
    proc = infra.bash("tifa_check_deploy_settings", env=ci_env(TFL_APP_KEY_VERSION="latest"))
    assert proc.returncode == 1 and "TFL_APP_KEY_VERSION" in proc.stdout


def test_the_library_needs_no_settings_to_be_sourced(infra):
    proc = ok(infra.bash("echo sourced", env={"TIFA_ENV_FILE": os.devnull}))
    assert proc.stdout.strip() == "sourced" and infra.calls() == []
