"""D-1 (Deployment lane) — LOCAL container integration + readiness + restore/restart drills.

Wires I-5's ``Dockerfile.api`` into a runnable ``compose.coolify.yml`` stack, proves it boots +
is ready, and records the restart + restore durability drills so II-11's public deploy inherits a
working, repeatable stack. **LOCAL ``docker compose`` ONLY** — nothing here touches production
Coolify/VPS/DNS, and only TEST/dummy secrets are used.

RED-first rows (this file is authored before the implementation exists):

1. **LOCAL compose boot + required-env guard** — ``docker compose ... config`` FAILS CLOSED when a
   required secret is missing (the ``:?`` guard fires; never a silent localhost), and a full-env
   boot brings up api+Postgres and ``/readyz`` becomes ready.
2. **Readiness fails closed** — the readiness probe is NOT-ready when Postgres / the AgentOS session
   DB / the ReplayPack catalog is down (each simulated), and ready only when all three are up.
3. **Container restart preserves state** — an Agent-Ops event enqueued to the WAL (not yet
   committed) + the pinned pack loaded into the capture volume SURVIVE a real container
   force-recreate against the same mounted ``WAL_DIR`` + capture volumes (exercises I-4 WAL replay;
   feeds III-1 AC-13).
4. **Restore drill** — ``pg_dump`` → ``pg_restore -l`` dry-run → a sample row reads back.

Docker-backed rows auto-skip when the Docker CLI/daemon is unavailable; the compose-structure and
readiness-unit rows are pure and always run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.coolify.yml"
PINNED_PACK = ROOT / "scripts" / "fixtures" / "demo_pack_real"
SMOKE = ROOT / "scripts" / "smoke_public.sh"
RESTORE_DRILL = ROOT / "scripts" / "restore_drill.sh"

# The seed-pack target the readiness catalog resolves via REPLAY_PACK_ROOT (I-10 pinned pack is
# bind-mounted here for the LOCAL stack; the operator points it at /srv/... in production). The leaf dir
# name IS the pack_id, so the mount target leaf is demo_pack_real (catalogs as pack_id="demo_pack_real",
# the id the F1 Official-Replay-League seed's phase-1 assert_pack requires).
CURATED_TARGET = "/var/lib/veridex/replay-packs/demo_pack_real"
WAL_TARGET = "/data/wal"  # matches Dockerfile.api's ENV WAL_DIR default (frozen; consumed unchanged)

# Required secrets that MUST carry a `:?` guard so a missing value fails the stack closed.
REQUIRED_GUARDED_ENV = {
    "DATABASE_URL",
    "CORS_ORIGINS",
    "OPERATOR_TOKEN",
    "PRIVY_APP_ID",
    "PRIVY_VERIFICATION_KEY",
}


# --------------------------------------------------------------------------------------------------
# TEST env matrix (dummy/local values ONLY — never a real production credential)
# --------------------------------------------------------------------------------------------------

LOCAL_TEST_ENV: dict[str, str] = {
    "POSTGRES_USER": "veridex",
    "POSTGRES_PASSWORD": "veridex_local_test_pw",  # noqa: S105 — dummy LOCAL value, not a real secret
    "POSTGRES_DB": "veridex",
    "DATABASE_URL": "postgresql://veridex:veridex_local_test_pw@postgres:5432/veridex",
    "CORS_ORIGINS": "http://localhost:3000",
    "OPERATOR_TOKEN": "local-test-operator-token",  # noqa: S105 — dummy LOCAL value
    "OPERATOR_ID": "op-local",
    "APP_ENV": "development",
    "AUTH_MODE": "dev",
    "PRIVY_APP_ID": "test-privy-app-id",
    "PRIVY_VERIFICATION_KEY": "test-privy-verification-key",  # noqa: S105 — dummy LOCAL value
    "WAL_DIR": WAL_TARGET,
    "REPLAY_PACK_ROOT": CURATED_TARGET,
    "NEXT_PUBLIC_API_BASE": "http://localhost:8000",
    # Required web build-arg (`:?`-guarded in compose.coolify.yml). Test-only placeholder so
    # `docker compose config` interpolates cleanly; must match the backend PRIVY_APP_ID at build time.
    "NEXT_PUBLIC_PRIVY_APP_ID": "test-privy-app-id",
    "API_HOST_PORT": "8000",
    "WEB_HOST_PORT": "3000",
    "CURATED_PACKS_HOST": "./scripts/fixtures/demo_pack_real",
}


# --------------------------------------------------------------------------------------------------
# compose loader
# --------------------------------------------------------------------------------------------------


def _load_compose() -> dict:
    assert COMPOSE.is_file(), f"missing {COMPOSE.relative_to(ROOT)}"
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "compose.coolify.yml must parse to a mapping"
    return data


def _api_service() -> dict:
    return _load_compose()["services"]["api-runtime"]


def _env_map(service: dict) -> dict[str, str]:
    """Return a service's ``environment:`` as a name->raw-value dict (list or mapping form)."""
    env = service.get("environment", {})
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    out: dict[str, str] = {}
    for item in env:
        name, _, value = str(item).partition("=")
        out[name] = value
    return out


# ══════════════════════════════════════════════════════════════════════════════════════════════
# RED row 1 (static half): compose injects env with `:?` required-var guards, one replica, readyz
# healthcheck, and reconciles WAL_DIR + REPLAY_PACK_ROOT with real mounts. No values committed.
# ══════════════════════════════════════════════════════════════════════════════════════════════


class TestComposeD1Wiring:
    def test_api_runtime_injects_required_env_with_failclosed_guards(self) -> None:
        env = _env_map(_api_service())
        for name in REQUIRED_GUARDED_ENV:
            assert name in env, f"api-runtime must inject {name!r} (D-1 env matrix)"
            raw = env[name]
            assert "${" in raw and ":?" in raw, (
                f"{name} must use a `${{{name}:?...}}` required-var guard so a missing value fails "
                f"the stack closed (never a silent localhost); got {raw!r}"
            )

    def test_api_runtime_consumes_frozen_dockerfile_api(self) -> None:
        build = _api_service()["build"]
        assert build["dockerfile"] == "Dockerfile.api", "D-1 consumes I-5's Dockerfile.api unchanged"

    def test_api_runtime_is_single_replica(self) -> None:
        replicas = _api_service().get("deploy", {}).get("replicas", 1)
        assert replicas == 1, f"exactly ONE api replica (AgentOS mounted IN the api service); got {replicas}"

    def test_api_runtime_healthcheck_targets_readyz(self) -> None:
        hc = _api_service().get("healthcheck", {})
        test = hc.get("test", [])
        blob = " ".join(test) if isinstance(test, list) else str(test)
        assert "/readyz" in blob, f"api-runtime healthcheck must probe deployment readiness /readyz; got {test!r}"

    def test_api_runtime_waits_for_postgres_healthy(self) -> None:
        depends = _api_service().get("depends_on", {})
        assert "postgres" in depends, "api-runtime must depend_on postgres"
        cond = depends["postgres"].get("condition") if isinstance(depends["postgres"], dict) else None
        assert cond == "service_healthy", "api-runtime must wait for postgres service_healthy (init_db needs it up)"

    def test_postgres_has_healthcheck(self) -> None:
        hc = _load_compose()["services"]["postgres"].get("healthcheck", {})
        test = hc.get("test", [])
        blob = " ".join(test) if isinstance(test, list) else str(test)
        assert "pg_isready" in blob or "pg_isready" in blob.lower(), "postgres needs a pg_isready healthcheck"

    def test_wal_dir_env_matches_mounted_spool_volume(self) -> None:
        svc = _api_service()
        wal_dir = _env_map(svc).get("WAL_DIR", "")
        # WAL_DIR must resolve to the value the wal-spool volume is mounted at (else the WAL writes to
        # the ephemeral container layer and is lost on restart — AC-13).
        wal_env_resolved = wal_dir.replace("${WAL_DIR:-", "").rstrip("}") or WAL_TARGET
        mount_targets = {str(v).split(":", 1)[1].split(":", 1)[0] for v in svc.get("volumes", []) if ":" in str(v)}
        wal_sources = {str(v).split(":", 1)[0] for v in svc.get("volumes", [])}
        assert "wal-spool" in wal_sources, "api-runtime must mount the wal-spool named volume"
        assert wal_env_resolved in mount_targets, (
            f"WAL_DIR ({wal_env_resolved!r}) must equal the wal-spool mount target; targets={sorted(mount_targets)}"
        )

    def test_replay_pack_root_points_at_curated_ro_mount(self) -> None:
        svc = _api_service()
        pack_root = _env_map(svc).get("REPLAY_PACK_ROOT", "")
        pack_resolved = pack_root.replace("${REPLAY_PACK_ROOT:-", "").rstrip("}") or CURATED_TARGET
        ro_targets = {
            str(v).rsplit(":", 1)[0].split(":", 1)[1]
            for v in svc.get("volumes", [])
            if str(v).endswith(":ro") and str(v).count(":") >= 2
        }
        assert pack_resolved in ro_targets, (
            f"REPLAY_PACK_ROOT ({pack_resolved!r}) must be the read-only curated seed-pack mount; "
            f":ro targets={sorted(ro_targets)}"
        )

    def test_no_plaintext_secret_values_committed(self) -> None:
        text = COMPOSE.read_text(encoding="utf-8")
        for marker in ("BEGIN PRIVATE KEY", "BEGIN RSA", "BEGIN EC", "BEGIN OPENSSH PRIVATE KEY"):
            assert marker not in text, "compose contains key material"
        # Every required secret is referenced ONLY through interpolation, never as a literal value.
        for name in REQUIRED_GUARDED_ENV:
            env = _env_map(_api_service())
            assert env[name].startswith("${"), f"{name} must be interpolated, never a committed literal"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# RED row 2: readiness fails closed (pure unit — injected probes; no Docker)
# ══════════════════════════════════════════════════════════════════════════════════════════════


def _probe(value: bool) -> Callable[[], Awaitable[bool]]:
    async def _p() -> bool:
        return value

    return _p


def _raising_probe() -> Callable[[], Awaitable[bool]]:
    async def _p() -> bool:
        raise RuntimeError("subsystem down")

    return _p


class TestReadinessFailClosed:
    async def test_ready_when_all_subsystems_up(self) -> None:
        from veridex.api.readiness import check_readiness

        report = await check_readiness(
            postgres=_probe(True), runtime_event_spool=_probe(True), replay_pack_catalog=_probe(True)
        )
        assert report.ready is True
        assert report.checks == {"postgres": True, "runtime_event_spool": True, "replay_pack_catalog": True}

    @pytest.mark.parametrize("down", ["postgres", "runtime_event_spool", "replay_pack_catalog"])
    async def test_not_ready_when_any_subsystem_down(self, down: str) -> None:
        from veridex.api.readiness import check_readiness

        probes: dict[str, Callable[[], Awaitable[bool]]] = {
            "postgres": _probe(True),
            "runtime_event_spool": _probe(True),
            "replay_pack_catalog": _probe(True),
        }
        probes[down] = _probe(False)
        report = await check_readiness(**probes)
        assert report.ready is False, f"a down {down} must make the stack NOT ready (fail closed)"
        assert report.checks[down] is False

    @pytest.mark.parametrize("down", ["postgres", "runtime_event_spool", "replay_pack_catalog"])
    async def test_probe_that_raises_is_treated_as_down(self, down: str) -> None:
        from veridex.api.readiness import check_readiness

        probes: dict[str, Callable[[], Awaitable[bool]]] = {
            "postgres": _probe(True),
            "runtime_event_spool": _probe(True),
            "replay_pack_catalog": _probe(True),
        }
        probes[down] = _raising_probe()
        report = await check_readiness(**probes)
        assert report.ready is False, "an exception in a probe is fail-closed (not-ready), never fail-open"
        assert report.checks[down] is False


class TestReplayPackCatalogProbe:
    async def test_empty_catalog_is_not_ready(self, tmp_path: Path) -> None:
        from veridex.api.readiness import make_replay_pack_probe

        probe = make_replay_pack_probe(str(tmp_path))
        assert await probe() is False

    async def test_missing_root_is_not_ready(self, tmp_path: Path) -> None:
        from veridex.api.readiness import make_replay_pack_probe

        probe = make_replay_pack_probe(str(tmp_path / "does-not-exist"))
        assert await probe() is False

    async def test_pinned_pack_is_ready(self) -> None:
        from veridex.api.readiness import make_replay_pack_probe

        # The real I-10 pinned pack (pack.json + record files) must read as a loaded catalog.
        probe = make_replay_pack_probe(str(PINNED_PACK))
        assert await probe() is True

    async def test_malformed_pack_json_is_not_ready(self, tmp_path: Path) -> None:
        from veridex.api.readiness import make_replay_pack_probe

        (tmp_path / "pack.json").write_text("{not valid json", encoding="utf-8")
        probe = make_replay_pack_probe(str(tmp_path))
        assert await probe() is False

    async def test_hashvalid_but_unloadable_pack_is_not_ready(self, tmp_path: Path) -> None:
        """Foundation-gate MAJOR-2: a pack whose ``content_hash`` is correct but whose ``records``
        file is NOT valid JSONL must read NOT-ready — the hash alone is insufficient; readiness must
        prove the pack loads through the real runtime loader, else /readyz fails open (a judge's
        replay then fails to start on a pack /readyz advertised as ready).
        """
        from veridex.api.readiness import make_replay_pack_probe
        from veridex.ingest.replay_pack import _compute_content_hash, verify_content_hash

        pack_dir = tmp_path / "poison"
        pack_dir.mkdir()
        # A records file the runtime loader can NOT parse (json.loads raises), yet whose bytes are
        # faithfully covered by a correctly-computed content_hash.
        (pack_dir / "odds_1.jsonl").write_text("not-json\n", encoding="utf-8")
        fixtures = [{"fixture_id": 1, "records": "odds_1.jsonl"}]
        content_hash = _compute_content_hash(pack_dir, fixtures)
        (pack_dir / "pack.json").write_text(
            json.dumps(
                {
                    "pack_version": 1,
                    "capture": {},
                    "fixtures": fixtures,
                    "closing_policy": "con-040_last_pre_inrunning",
                    "content_hash": content_hash,
                }
            ),
            encoding="utf-8",
        )
        # The hash check PASSES for this pack — proving hash verification alone does not catch it.
        assert verify_content_hash(pack_dir) is True
        # ...but the runtime loader cannot parse the records, so readiness must fail closed.
        probe = make_replay_pack_probe(str(pack_dir))
        assert await probe() is False

    async def test_genuine_pinned_pack_still_loadable(self) -> None:
        """No-regression guard: the real pinned pack still reads loadable through the tightened check
        (the fix must not reject a genuinely-loadable pack).
        """
        from veridex.api.readiness import make_replay_pack_probe

        probe = make_replay_pack_probe(str(PINNED_PACK))
        assert await probe() is True

    async def test_hash_mismatch_pack_is_not_ready(self, tmp_path: Path) -> None:
        """A pack whose declared ``content_hash`` does not match its data is NOT loadable — the
        verified runtime loader refuses it (fail-closed).
        """
        from veridex.api.readiness import make_replay_pack_probe
        from veridex.ingest.replay_pack import verify_content_hash

        pack_dir = tmp_path / "tampered"
        pack_dir.mkdir()
        # A well-formed, parseable records file — but a declared content_hash that does NOT match.
        (pack_dir / "odds_1.jsonl").write_text(json.dumps({"FixtureId": 1}) + "\n", encoding="utf-8")
        fixtures = [{"fixture_id": 1, "records": "odds_1.jsonl"}]
        (pack_dir / "pack.json").write_text(
            json.dumps(
                {
                    "pack_version": 1,
                    "capture": {},
                    "fixtures": fixtures,
                    "closing_policy": "con-040_last_pre_inrunning",
                    "content_hash": "0" * 64,
                }
            ),
            encoding="utf-8",
        )
        assert verify_content_hash(pack_dir) is False
        probe = make_replay_pack_probe(str(pack_dir))
        assert await probe() is False


class TestReadyzRoute:
    def _client(self, *, ready: bool):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from veridex.api.readiness import build_readiness_router

        app = FastAPI()
        app.include_router(
            build_readiness_router(
                postgres_probe=_probe(ready),
                runtime_event_spool_probe=_probe(ready),
                pack_probe=_probe(ready),
            )
        )
        return TestClient(app)

    def test_readyz_200_when_ready(self) -> None:
        resp = self._client(ready=True).get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["ready"] is True

    def test_readyz_503_when_not_ready(self) -> None:
        resp = self._client(ready=False).get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["ready"] is False


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Docker-backed rows (RED 1 boot / 3 restart / 4 restore). Auto-skip without a Docker daemon.
# ══════════════════════════════════════════════════════════════════════════════════════════════


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


docker_required = pytest.mark.skipif(not _docker_available(), reason="docker daemon unavailable")


def _dockerfile_api_copies_agent() -> bool:
    """True iff the frozen Dockerfile.api copies the ``veridex_agent`` package into the image.

    ``veridex.api.router`` -> ``veridex.api.demo_fixtures`` imports ``veridex_agent.config`` at import
    time, and ``pyproject.toml`` declares ``veridex_agent*`` a package. Dockerfile.api (I-5-owned,
    consumed UNCHANGED by D-1) currently only ``COPY veridex ./veridex`` — so the built image cannot
    import ``veridex.api.server`` (``ModuleNotFoundError: No module named 'veridex_agent'``) and the
    api container crash-loops. D-1's compose/readiness/WAL wiring was validated end-to-end against a
    probe image that adds the one missing line; these api-boot rows unblock the moment I-5 adds
    ``COPY veridex_agent ./veridex_agent`` to Dockerfile.api.
    """
    dockerfile = ROOT / "Dockerfile.api"
    return dockerfile.is_file() and "veridex_agent" in dockerfile.read_text(encoding="utf-8")


# Skips the LIVE api-boot/restart rows while the frozen Dockerfile.api cannot import the app. This is
# an EXTERNAL I-5 blocker (not a D-1 defect): D-1 owns compose/readiness/WAL, all proven against a
# probe image with the fix; the marker auto-enables once I-5 lands the COPY.
api_image_boots = pytest.mark.skipif(
    not _dockerfile_api_copies_agent(),
    reason=(
        "BLOCKED on I-5: Dockerfile.api does not COPY veridex_agent, so the built image cannot import "
        "veridex.api.server (router->demo_fixtures imports veridex_agent.config). D-1's compose + "
        "/readyz + WAL-restart wiring is proven against a probe image adding "
        "`COPY veridex_agent ./veridex_agent`; unblocks when I-5 adds that line."
    ),
)

PROJECT = "veridex_d1_local"


def _write_env_file(tmp: Path, overrides: dict[str, str] | None = None, drop: set[str] | None = None) -> Path:
    env = dict(LOCAL_TEST_ENV)
    if overrides:
        env.update(overrides)
    for key in drop or set():
        env.pop(key, None)
    path = tmp / "d1.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    return path


def _compose(
    *args: str,
    env_file: Path,
    check: bool = True,
    timeout: int = 900,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE), "--env-file", str(env_file), *args]
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=check, timeout=timeout, env=env)


class TestRequiredEnvGuardConfig:
    """RED 1 (guard half): `docker compose config` fails closed on a missing required secret."""

    @docker_required
    def test_config_succeeds_with_full_env(self, tmp_path: Path) -> None:
        env_file = _write_env_file(tmp_path)
        proc = _compose("config", env_file=env_file, check=False, timeout=120)
        assert proc.returncode == 0, f"full-env compose config must lint clean:\n{proc.stderr}"

    @docker_required
    def test_config_fails_closed_when_required_secret_missing(self, tmp_path: Path) -> None:
        # Drop a required secret: the `:?` guard must abort interpolation (never a silent default).
        dropped = {"DATABASE_URL"}
        env_file = _write_env_file(tmp_path, drop=dropped)
        # Isolate the subprocess env: `docker compose` interpolation prefers the SHELL env over
        # --env-file, so an ambient DATABASE_URL in the invoking shell would silently satisfy the
        # guard this test exists to prove fails closed. Start from a full os.environ copy (so PATH,
        # HOME, DOCKER_HOST, etc. still let `docker` run) and explicitly delete the dropped secret(s)
        # so the guarded var is deterministically absent regardless of the ambient shell.
        isolated_env = dict(os.environ)
        for key in dropped:
            isolated_env.pop(key, None)
        proc = _compose("config", env_file=env_file, check=False, timeout=120, env=isolated_env)
        assert proc.returncode != 0, "a missing required secret must fail compose closed (the :? guard fires)"
        assert "DATABASE_URL" in (proc.stderr + proc.stdout), "the guard must name the missing required var"


@pytest.fixture(scope="module")
def postgres_stack() -> Iterator[Path]:
    """Boot ONLY the pinned Postgres (no api image needed) for the restore drill. Teardown after.

    Kept separate from :func:`compose_stack` so the restore drill runs for real even while the api
    image is blocked by the I-5 ``veridex_agent`` gap — the drill exercises Postgres backup/restore,
    which is independent of the api container.
    """
    if not _docker_available():
        pytest.skip("docker daemon unavailable")
    tmp = Path(os.environ.get("PYTEST_D1_TMP", "/tmp")) / "veridex_d1_pg"
    tmp.mkdir(parents=True, exist_ok=True)
    env_file = _write_env_file(tmp)
    _compose("down", "-v", "--remove-orphans", env_file=env_file, check=False, timeout=180)
    _compose("up", "-d", "postgres", env_file=env_file, timeout=300)
    _wait_postgres_healthy(env_file)
    try:
        yield env_file
    finally:
        _compose("down", "-v", "--remove-orphans", env_file=env_file, check=False, timeout=180)


@pytest.fixture(scope="module")
def compose_stack() -> Iterator[Path]:
    """Build + boot api+postgres once for the Docker durability drills, tearing down volumes after.

    Only reached when Dockerfile.api can import the app (see ``api_image_boots``); otherwise the
    dependent tests skip before this fixture is set up, so no wasted image build.
    """
    if not _docker_available():
        pytest.skip("docker daemon unavailable")
    tmp = Path(os.environ.get("PYTEST_D1_TMP", "/tmp")) / "veridex_d1_env"
    tmp.mkdir(parents=True, exist_ok=True)
    env_file = _write_env_file(tmp)
    # Clean any prior run, then build + boot ONLY api+postgres (web is validated via `config`; its
    # Next.js build is II-11's deployed concern and too heavy for a local durability drill).
    _compose("down", "-v", "--remove-orphans", env_file=env_file, check=False, timeout=180)
    _compose("up", "-d", "--build", "postgres", "api-runtime", env_file=env_file, timeout=1800)
    try:
        _wait_ready(env_file)
        yield env_file
    finally:
        _compose("down", "-v", "--remove-orphans", env_file=env_file, check=False, timeout=180)


def _wait_postgres_healthy(env_file: Path, timeout_s: int = 120) -> None:
    """Poll ``pg_isready`` inside the postgres container until healthy."""
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        proc = _compose(
            "exec", "-T", "postgres", "pg_isready", "-U", LOCAL_TEST_ENV["POSTGRES_USER"],
            env_file=env_file, check=False, timeout=20,
        )
        if proc.returncode == 0:
            return
        last = proc.stderr or proc.stdout
        time.sleep(2)
    raise AssertionError(f"postgres never became healthy within {timeout_s}s; last: {last}")


def _wait_ready(env_file: Path, timeout_s: int = 180) -> None:
    """Poll /readyz inside the api container until 200 (api becomes ready) or fail."""
    probe = (
        "import urllib.request,sys\n"
        "try:\n"
        "    r=urllib.request.urlopen('http://localhost:8000/readyz',timeout=5)\n"
        "    sys.exit(0 if r.status==200 else 1)\n"
        "except Exception:\n"
        "    sys.exit(1)\n"
    )
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        proc = _compose("exec", "-T", "api-runtime", "python", "-c", probe, env_file=env_file, check=False, timeout=30)
        if proc.returncode == 0:
            return
        last = proc.stderr
        time.sleep(4)
    raise AssertionError(f"api /readyz never became ready within {timeout_s}s; last: {last}")


@api_image_boots
class TestLocalComposeBoot:
    """RED 1 (boot half): the LOCAL stack boots and /readyz reports ready with all checks true."""

    @docker_required
    def test_stack_boots_and_readyz_is_ready(self, compose_stack: Path) -> None:
        read = (
            "import urllib.request,json\n"
            "print(urllib.request.urlopen('http://localhost:8000/readyz',timeout=5).read().decode())\n"
        )
        proc = _compose("exec", "-T", "api-runtime", "python", "-c", read, env_file=compose_stack, check=False)
        assert proc.returncode == 0, f"/readyz must be 200 on the booted stack:\n{proc.stderr}"
        body = json.loads(proc.stdout.strip().splitlines()[-1])
        assert body["ready"] is True
        assert body["checks"] == {"postgres": True, "runtime_event_spool": True, "replay_pack_catalog": True}

    @docker_required
    def test_smoke_public_passes_against_local_stack(self, compose_stack: Path) -> None:
        if not SMOKE.is_file():
            pytest.fail("scripts/smoke_public.sh must exist")
        proc = subprocess.run(
            ["bash", str(SMOKE)],
            env={**os.environ, "BASE_URL": f"http://localhost:{LOCAL_TEST_ENV['API_HOST_PORT']}"},
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, f"smoke_public.sh failed:\n{proc.stdout}\n{proc.stderr}"
        assert "SMOKE_OK" in proc.stdout


# Script the api container runs to enqueue ONE Agent-Ops event to the WAL WITHOUT committing it to
# Postgres (no drain), so the surviving-and-replaying assertion is genuine WAL replay, not a row that
# was already in Postgres before the restart.
_ENQUEUE_WAL_ONLY = """
import asyncio, os
from veridex.store import PostgresStore
from veridex.runtime.runtime_store import DurableRuntimeEventStore
from veridex.runtime.runtime_events import runtime_event, RuntimeEventType

AGENT = "d1-restart-probe"

async def main():
    store = PostgresStore(dsn=os.environ["DATABASE_URL"])
    spool = DurableRuntimeEventStore(store=store, wal_dir=os.environ["WAL_DIR"])
    ev = runtime_event(RuntimeEventType.RUN_STARTED, agent_id=AGENT, note="pre-restart")
    spool.enqueue(ev)                 # WAL line + flush to OS; NOT yet committed to Postgres
    before = await store.list_runtime_events(AGENT)
    assert before == [], f"event must NOT be in Postgres pre-drain, got {before!r}"
    print("ENQUEUED_WAL_ONLY", ev.payload)

asyncio.run(main())
"""

# After the container is force-recreated, a fresh spool over the SAME mounted WAL_DIR recovers the WAL
# tail and drains it -> the event lands in Postgres. Proves the WAL survived container replacement.
_RECOVER_AND_ASSERT = """
import asyncio, os
from veridex.store import PostgresStore
from veridex.runtime.runtime_store import DurableRuntimeEventStore

AGENT = "d1-restart-probe"

async def main():
    store = PostgresStore(dsn=os.environ["DATABASE_URL"])
    spool = DurableRuntimeEventStore(store=store, wal_dir=os.environ["WAL_DIR"])
    spool.recover()          # replay WAL tail beyond the durable cursor
    await spool.drain()      # commit the replayed batch to Postgres
    rows = await store.list_runtime_events(AGENT)
    assert len(rows) >= 1, f"WAL event must survive container restart + replay, got {rows!r}"
    assert rows[0].agent_id == AGENT
    print("WAL_REPLAYED_ROWS", len(rows), rows[0].event_type)

asyncio.run(main())
"""


@api_image_boots
class TestContainerRestartPreservesWAL:
    """RED 3: a real container force-recreate against the same volumes preserves WAL Ops + pack."""

    @docker_required
    def test_wal_event_and_pack_survive_container_recreate(self, compose_stack: Path) -> None:
        env_file = compose_stack
        # 1) Enqueue an Agent-Ops event to the WAL (uncommitted) inside the running container.
        proc = _compose(
            "exec", "-T", "api-runtime", "python", "-c", _ENQUEUE_WAL_ONLY, env_file=env_file, check=False
        )
        assert proc.returncode == 0 and "ENQUEUED_WAL_ONLY" in proc.stdout, f"enqueue failed:\n{proc.stderr}"

        # 2) Load the pinned pack into the writable capture volume (survives via the named volume).
        capture = "/var/lib/veridex/replay-packs/capture"
        load = _compose(
            "exec", "-T", "api-runtime", "sh", "-c",
            f"mkdir -p {capture}/pinned && cp {CURATED_TARGET}/pack.json {capture}/pinned/pack.json && "
            f"test -f {capture}/pinned/pack.json && echo PACK_LOADED",
            env_file=env_file, check=False,
        )
        assert load.returncode == 0 and "PACK_LOADED" in load.stdout, f"pack load failed:\n{load.stderr}"

        # 3) Force-recreate the api container (destroy + recreate; named volumes persist).
        _compose("up", "-d", "--force-recreate", "--no-deps", "api-runtime", env_file=env_file, timeout=300)
        _wait_ready(env_file)

        # 4a) WAL Ops history replayed into Postgres after the restart.
        recovered = _compose(
            "exec", "-T", "api-runtime", "python", "-c", _RECOVER_AND_ASSERT, env_file=env_file, check=False
        )
        assert recovered.returncode == 0 and "WAL_REPLAYED_ROWS" in recovered.stdout, (
            f"WAL replay across restart failed:\n{recovered.stdout}\n{recovered.stderr}"
        )

        # 4b) The loaded pack is still on the capture volume.
        pack = _compose(
            "exec", "-T", "api-runtime", "sh", "-c",
            f"test -f {capture}/pinned/pack.json && echo PACK_SURVIVED",
            env_file=env_file, check=False,
        )
        assert pack.returncode == 0 and "PACK_SURVIVED" in pack.stdout, "the loaded pinned pack must survive restart"


class TestRestoreDrill:
    """RED 4: pg_dump -> pg_restore -l dry-run -> a sample row reads back from a fresh restore."""

    @docker_required
    def test_restore_drill_reads_sample_row_back(self, postgres_stack: Path) -> None:
        if not RESTORE_DRILL.is_file():
            pytest.fail("scripts/restore_drill.sh must exist")
        # Run the drill INSIDE the postgres container (it carries pg_dump/pg_restore/psql). `sh -s`
        # reads the script from stdin, so this shells out directly to pass `input=`.
        script = RESTORE_DRILL.read_text(encoding="utf-8")
        cmd = [
            "docker", "compose", "-p", PROJECT, "-f", str(COMPOSE), "--env-file", str(postgres_stack),
            "exec", "-T",
            "-e", f"PGUSER={LOCAL_TEST_ENV['POSTGRES_USER']}",
            "-e", f"PGPASSWORD={LOCAL_TEST_ENV['POSTGRES_PASSWORD']}",
            "-e", f"PGDATABASE={LOCAL_TEST_ENV['POSTGRES_DB']}",
            "postgres", "sh", "-s",
        ]
        proc = subprocess.run(cmd, cwd=str(ROOT), input=script, capture_output=True, text=True, timeout=180)
        assert proc.returncode == 0, f"restore drill failed:\n{proc.stdout}\n{proc.stderr}"
        assert "RESTORE_DRILL_OK" in proc.stdout, f"drill must confirm sample-row read-back:\n{proc.stdout}"


# ---------------------------------------------------------------------------
# P0-1 — the image's Ethereum dependency family.
#
# `origin/signal-trials` deployed and crashed in `create_server_app` with
# `ModuleNotFoundError: eth_abi`, then the vendored package's own
# `ImportError: EVM mechanism requires ethereum packages`. Root cause:
# `okxweb3-app-x402` gates eth-abi / eth-account / eth-keys / eth-utils / web3
# behind extras (`evm`, `mechanisms`, `all`) and the bare distribution pulls none.
#
# NO IMPORT TEST CAN GUARD THIS. `import eth_abi` passes on any host whose venv
# already carries web3 for unrelated reasons, and every `uv sync` environment does:
# uv.lock already resolved the family, so the lock-based dev path always worked
# while the image's `pip install ".[...]"` path got nothing. The two install paths
# diverge, and that divergence is exactly what stayed invisible.
#
# So these assert on the DECLARATION, which is host-independent by construction.
# ---------------------------------------------------------------------------

# The five distributions x402 needs for the EVM mechanism, per its own metadata.
_X402_EVM_DISTS = ("eth-abi", "eth-account", "eth-keys", "eth-utils", "web3")


def _pyproject_text() -> str:
    return (ROOT / "pyproject.toml").read_text()


def _signal_trials_extra() -> str:
    """The one line declaring the `signal-trials` optional-dependency group."""
    for line in _pyproject_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("signal-trials ="):
            return stripped
    raise AssertionError("pyproject.toml declares no 'signal-trials' extra")


def test_the_signal_trials_extra_requests_x402s_evm_extra() -> None:
    """ACCEPTANCE — the declaration must ASK for the Ethereum family.

    `okxweb3-app-x402==0.1.1` installs no eth/web3 distribution at all. The image
    runs `pip install ".[api,postgres,live,agent,signal-trials]"`, so whatever this
    line omits is simply absent at runtime — and absent only inside the container.
    """
    line = _signal_trials_extra()
    assert "okxweb3-app-x402" in line, line
    assert "okxweb3-app-x402[" in line, (
        "okxweb3-app-x402 is declared WITHOUT an extra, so no eth/web3 "
        f"distribution reaches the image: {line}"
    )
    # `evm`, `mechanisms` and `all` each carry the identical five dists; any is
    # sufficient, and pinning to one exact spelling would be over-tight.
    assert any(f"[{name}]" in line or f",{name}]" in line or f"[{name}," in line
               for name in ("evm", "mechanisms", "all")), (
        f"the declared extra does not supply x402's Ethereum family: {line}"
    )


def test_the_declared_extra_is_narrow_rather_than_everything() -> None:
    """`all` would also drag in the Solana mechanism this deployment does not use.

    Not a correctness requirement — an image-size and attack-surface one — so it is
    asserted separately from the acceptance test above and can be relaxed knowingly.
    """
    line = _signal_trials_extra()
    assert "[all]" not in line, (
        f"prefer the narrow [evm] extra over [all], which adds unused SVM machinery: {line}"
    )


def test_the_image_actually_installs_the_signal_trials_extra() -> None:
    """DISCRIMINATION — the declaration only matters if the image asks for it.

    Without this, the two tests above could both pass while `Dockerfile.api` had
    quietly dropped `signal-trials` from its extras list, leaving the runtime
    exactly as broken with the pyproject looking correct.
    """
    install_lines = [
        line for line in (ROOT / "Dockerfile.api").read_text().splitlines()
        if "pip install" in line and ".[" in line
    ]
    assert install_lines, "Dockerfile.api has no extras-based pip install line"
    assert any("signal-trials" in line for line in install_lines), (
        f"Dockerfile.api does not install the signal-trials extra: {install_lines}"
    )


def test_the_evm_distributions_are_resolvable_together() -> None:
    """The five names are asserted against x402's OWN metadata, not a hand-list.

    A hardcoded list would rot silently if upstream renamed or added a dependency.
    Skipped rather than failed when the distribution is absent, because this file's
    other tests must remain runnable in an environment without the extra installed.
    """
    import importlib.metadata as metadata

    try:
        dist = metadata.distribution("okxweb3-app-x402")
    except metadata.PackageNotFoundError:  # pragma: no cover - env-dependent
        pytest.skip("okxweb3-app-x402 is not installed in this environment")

    declared = {
        req.split(";")[0].split(">")[0].split("=")[0].strip().lower()
        for req in (dist.metadata.get_all("Requires-Dist") or [])
        if "extra == 'evm'" in req or 'extra == "evm"' in req
    }
    missing = [name for name in _X402_EVM_DISTS if name.replace("_", "-") not in declared]
    assert not missing, (
        f"x402's own 'evm' extra no longer declares {missing}; "
        f"the guard above is checking for the wrong thing. Declared: {sorted(declared)}"
    )
