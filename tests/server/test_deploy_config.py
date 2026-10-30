"""Deploy readiness: main.bicep, docker-compose.yml, deploy.sh and the worker image agree with server/config.py.
Nothing here deploys or calls a cloud: the files are parsed as text and deploy.sh only runs against a stub `az`."""
import dataclasses
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from server import config, jobs, worker

ROOT = Path(__file__).resolve().parents[2]
BICEP = (ROOT / "deploy/azure/main.bicep").read_text(encoding="utf-8")
COMPOSE_TEXT = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
CONFIG_SRC = (ROOT / "server/config.py").read_text(encoding="utf-8")
CONFIG_ENV_NAMES = sorted(set(re.findall(r'env\("([A-Z0-9_]+)"', CONFIG_SRC)))

HARDENING = ("MAX_JOB_ATTEMPTS", "BACKOFF_BASE_S", "BACKOFF_MAX_S", "BACKOFF_RESET_H", "ERROR_BUDGET_WINDOW_H",
             "ERROR_BUDGET_MIN_JOBS", "ERROR_BUDGET_MAX_FAILURE_PCT", "NEEDS_LOGIN_REMIND_H")
# Not tunables: secrets, per-service wiring and values that are deliberately different on a hosted deployment.
NOT_COMPARED = {"database_url", "jwt_secret", "jwks_url", "profile_store_url", "profile_master_key",
                "proxy_url_template", "smtp_host", "smtp_password", "vapid_private_key", "ask_database_url",
                "anthropic_api_key"}


def block(name: str) -> str:
    """The text of one `resource <name> '...'` block of main.bicep."""
    start = BICEP.index(f"resource {name} '")
    end = re.search(r"\n(resource |output )", BICEP[start + 10:])
    return BICEP[start:start + 10 + end.start()] if end else BICEP[start:]


def bicep_params() -> dict[str, str]:
    return dict(re.findall(r"^param (\w+) string = '([^']*)'", BICEP, re.M))


def bicep_shared_env() -> dict[str, str]:
    """name -> resolved value for the `budgetEnv` list that the API, the worker and the scheduler all use."""
    body = re.search(r"var budgetEnv = \[\n(.*?)\n\]", BICEP, re.S).group(1)
    params = bicep_params()
    out = {}
    for name, expr in re.findall(r"\{ name: '(\w+)', value: ([^}]+?) \}", body):
        out[name] = expr[1:-1] if expr.startswith("'") else params[expr]
    return out


def compose_app_env() -> dict[str, str]:
    """x-app-env with every ${VAR:-default} resolved against an empty environment."""
    def resolve(v):
        return re.sub(r"\$\{[A-Z0-9_]+(?::-([^}]*))?\}", lambda m: m.group(1) or "", str(v))
    return {k: resolve(v) for k, v in COMPOSE["x-app-env"].items()}


@pytest.fixture
def clean_env(monkeypatch):
    for name in CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALLOW_DEV_PROFILE_KEY", "1")
    return monkeypatch


def comparable(s: config.Settings) -> dict:
    return {k: v for k, v in dataclasses.asdict(s).items() if k not in NOT_COMPARED}


# --- Bicep: the API ----------------------------------------------------------------------------------------------

def test_api_container_has_no_master_key():
    api = block("api")
    assert "PROFILE_MASTER_KEY" not in api
    assert "profile-master-key" not in api
    assert "profileMasterKey" not in api


def test_api_gets_store_url_and_jwks():
    api = block("api")
    assert "{ name: 'PROFILE_STORE_URL', secretRef: 'profile-store-url' }" in api
    assert "{ name: 'profile-store-url'," in api
    assert "{ name: 'SUPABASE_JWKS_URL', value: supabaseJwksUrl }" in api
    assert re.search(r"^param supabaseJwksUrl string$", BICEP, re.M)  # required: no default


def test_api_wires_the_ask_reader_login():
    api = block("api")
    assert "{ name: 'ask-database-url', value: askDatabaseUrl }" in api
    assert "{ name: 'ASK_DATABASE_URL', secretRef: 'ask-database-url' }" in api
    assert re.search(r"@secure\(\)\nparam askDatabaseUrl string", BICEP)
    # the worker and the scheduler never get the Ask login
    assert "ASK_DATABASE_URL" not in block("worker") and "ASK_DATABASE_URL" not in block("scheduler")


def test_compose_wires_the_ask_reader_login():
    api_env = COMPOSE["services"]["api"]["environment"]
    assert api_env["ASK_DATABASE_URL"].startswith("${COMPOSE_ASK_DATABASE_URL:-postgresql://ask_reader:")
    assert "@db:5432/postgres" in api_env["ASK_DATABASE_URL"]
    assert "ASK_DATABASE_URL" not in COMPOSE["services"]["worker"]["environment"]
    migrate = "\n".join(COMPOSE["services"]["migrate"]["command"])
    assert "alter role ask_reader login password" in migrate
    smoke = (ROOT / "scripts/smoke_compose.sh").read_text(encoding="utf-8")
    assert "/ask" in smoke and '"$code" = 200' in smoke
    assert "ASK_FAKE" not in COMPOSE_TEXT  # the fake is refused on a non-local DATABASE_URL (host db)


# --- Bicep: the worker, Key Vault, Log Analytics, KEDA -----------------------------------------------------------

def test_worker_tz_and_key_vault_reference():
    w = block("worker")
    assert "{ name: 'TZ', value: 'Asia/Kolkata' }" in w
    assert ("{ name: 'profile-master-key', keyVaultUrl: profileKeySecret.properties.secretUri, "
            "identity: identity.id }") in w
    assert "{ name: 'PROFILE_MASTER_KEY', secretRef: 'profile-master-key' }" in w
    assert "kvSecretsUser" in w.split("properties:")[0]  # the role exists before the app reads the vault


def test_key_vault_resource():
    kv = block("keyVault")
    assert "Microsoft.KeyVault/vaults@2023-07-01" in kv
    assert "name: '${prefix}-kv-${take(uniq, 6)}'" in kv
    for line in ("enableRbacAuthorization: true", "enableSoftDelete: true", "enablePurgeProtection: true"):
        assert line in kv
    secret = block("profileKeySecret")
    assert "name: 'profile-master-key'" in secret and "value: profileMasterKey" in secret
    assert "4633458b-17de-408a-b874-0445c86b69e6" in BICEP  # Key Vault Secrets User
    assert "scope: keyVault" in block("kvSecretsUser")
    assert "principalId: identity.properties.principalId" in block("kvSecretsUser")


def test_log_analytics_is_capped():
    assert "workspaceCapping: { dailyQuotaGb: json('0.2') }" in block("logs")
    assert "retentionInDays: 30" in block("logs")


def test_keda_query_matches_python():
    q = re.search(r"query: '(.*)'\n", BICEP).group(1).replace("\\'", "'")
    assert " ".join(q.split()) == " ".join(jobs.scaler_query(worker.JOB_TYPES).split())


# --- Env parity --------------------------------------------------------------------------------------------------

def test_no_leftover_openai_in_deploy_files():
    for rel in ("deploy/azure/main.bicep", "deploy/azure/deploy.sh", "deploy/azure/env.deploy.example",
                "docker-compose.yml", "docker/worker.Dockerfile", "docker/api.Dockerfile"):
        assert "openai" not in (ROOT / rel).read_text(encoding="utf-8").lower(), rel
    assert "{ name: 'ANTHROPIC_MODEL', value: anthropicModel }" in block("api")
    assert re.search(r"param anthropicModel string = 'claude-haiku-5-5'", BICEP)


def test_every_config_env_var_is_in_compose_and_bicep():
    assert "MAX_JOB_ATTEMPTS" in CONFIG_ENV_NAMES and "CART_FILLS_PER_DAY" in CONFIG_ENV_NAMES  # the regex found them
    bicep_names = set(re.findall(r"name: '([A-Z0-9_]+)'", BICEP))
    compose_names = set(re.findall(r"^\s+([A-Z0-9_]+):", COMPOSE_TEXT, re.M))
    only_local = {"ALLOW_DEV_PROFILE_KEY"}  # local file:// vault only: never in a deployment
    assert [n for n in CONFIG_ENV_NAMES if n not in compose_names] == []
    assert [n for n in CONFIG_ENV_NAMES if n not in bicep_names and n not in only_local] == []
    assert "ALLOW_DEV_PROFILE_KEY" not in BICEP


def test_hardening_tunables_set_everywhere():
    compose_env = COMPOSE["x-app-env"]
    shared = bicep_shared_env()
    for name in HARDENING:
        assert name in compose_env, f"{name} missing from compose x-app-env"
        assert name in shared, f"{name} missing from the Bicep shared env"
    # the shared list reaches the API, the worker and Plan 4's scheduler job
    for app in ("api", "worker", "scheduler"):
        assert re.search(r"\], budgetEnv, notifySecretEnv\)", block(app)), app
    # the worker's scale-in budget: DRAIN_S ends inside terminationGracePeriodSeconds
    grace = int(re.search(r"terminationGracePeriodSeconds: (\d+)", block("worker")).group(1))
    assert int(shared["DRAIN_S"]) < grace


def test_compose_env_values_parse(clean_env):
    defaults = comparable(config.settings())
    for name, value in compose_app_env().items():
        clean_env.setenv(name, value)
    got = comparable(config.settings())
    # compose runs against a local mail sink; every other value equals the config.py default
    assert got == defaults
    assert config.settings().max_job_attempts == 3
    assert config.settings().error_budget_max_failure_pct == 30
    assert config.settings().needs_login_remind_h == 72


def test_bicep_env_defaults_parse(clean_env):
    defaults = comparable(config.settings())
    for name, value in bicep_shared_env().items():
        clean_env.setenv(name, value)
    clean_env.setenv("CORS_ORIGINS", bicep_params()["corsOrigins"])
    assert comparable(config.settings()) == defaults


def test_compose_worker_is_in_ist():
    assert COMPOSE["services"]["worker"]["environment"]["TZ"] == "Asia/Kolkata"
    assert "STALE_JOB_S" in COMPOSE["x-app-env"]


# --- Worker image ------------------------------------------------------------------------------------------------

def test_worker_image_tz_and_webrtc_policy():
    df = (ROOT / "docker/worker.Dockerfile").read_text(encoding="utf-8")
    assert "tzdata" in df and "TZ=Asia/Kolkata" in df
    assert "COPY docker/chrome-policies/webrtc.json /etc/opt/chrome/policies/managed/" in df
    import json
    policy = json.loads((ROOT / "docker/chrome-policies/webrtc.json").read_text(encoding="utf-8"))
    assert policy == {"WebRtcIPHandlingPolicy": "disable_non_proxied_udp"}


# --- deploy.sh and its env example -------------------------------------------------------------------------------

def test_env_example_has_pooler_url_and_jwks():
    ex = (ROOT / "deploy/azure/env.deploy.example").read_text(encoding="utf-8")
    assert re.search(r"^DATABASE_URL=postgresql://.*:5432/postgres\?sslmode=require$", ex, re.M)
    assert "# never the transaction pooler (port 6543)" in ex
    assert "SUPABASE_JWKS_URL=https://<ref>.supabase.co/auth/v1/.well-known/jwks.json" in ex
    assert not re.search(r"^[A-Z_]+=\S*(sk-|eyJ)", ex, re.M)  # placeholders only


def test_deploy_script_requires_clean_tree_jwks_and_checks_the_url():
    sh = (ROOT / "deploy/azure/deploy.sh").read_text(encoding="utf-8")
    assert "refusing to deploy: the git tree has uncommitted or untracked changes" in sh
    assert sh.index("git_tree_clean") < sh.index("az group create")
    assert "SUPABASE_JWKS_URL" in sh
    assert 'check_database_url(os.environ["DATABASE_URL"], "worker")' in sh
    assert "DATABASE_URL" not in re.findall(r'--parameters[^\n]*', sh)[0]  # secrets stay out of argv


def _bash() -> str | None:
    """A bash that can run deploy.sh: on Windows `bash` on PATH may be the WSL stub, so prefer Git's own."""
    cands = []
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent
        cands += [str(root / "bin" / "bash.exe"), str(root / "usr" / "bin" / "bash.exe")]
    cands.append(shutil.which("bash") or "")
    for c in cands:
        if c and Path(c).exists():
            try:
                r = subprocess.run([c, "-c", "echo ok"], capture_output=True, encoding="utf-8", timeout=20)
            except Exception:
                continue
            if r.stdout.strip() == "ok":
                return c
    return None


@pytest.mark.skipif(_bash() is None, reason="needs a working bash")
def test_deploy_script_refuses_a_dirty_tree_before_any_az_call(tmp_path):
    repo = tmp_path / "repo"
    (repo / "deploy/azure").mkdir(parents=True)
    for name in ("deploy.sh", "guard.sh"):
        shutil.copy(ROOT / "deploy/azure" / name, repo / "deploy/azure" / name)
    git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    run = dict(cwd=repo, check=True, capture_output=True, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], **run)
    subprocess.run(["git", "add", "-A"], **run)
    subprocess.run(git + ["commit", "-q", "-m", "init"], **run)
    stub = tmp_path / "bin"
    stub.mkdir()
    marker = tmp_path / "az-was-called"
    az = stub / "az"
    az.write_text(f'#!/bin/sh\ntouch "{marker.as_posix()}"\nexit 0\n', encoding="utf-8", newline="\n")
    az.chmod(0o755)
    env = {**os.environ, "PATH": f"{stub.as_posix()}{os.pathsep}{os.environ['PATH']}"}

    def deploy():
        return subprocess.run([_bash(), "deploy/azure/deploy.sh", "rg"], cwd=repo, env=env, capture_output=True,
                              encoding="utf-8")

    clean = deploy()  # a clean tree gets past the guard (and stops at the missing env file)
    assert "refusing to deploy" not in clean.stderr and "missing" in clean.stderr

    (repo / "untracked.txt").write_text("x", encoding="utf-8")
    dirty = deploy()
    assert dirty.returncode == 1
    assert "refusing to deploy: the git tree has uncommitted or untracked changes" in dirty.stderr
    assert not marker.exists()
