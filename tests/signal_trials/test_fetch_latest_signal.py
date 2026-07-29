import contextlib
import json
import os
import shutil
import subprocess
import sys
from importlib import util as importlib_util
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.signal_trials.test_challenge_spec import REST
from veridex.signal_trials.challenge_spec import evidence_hash, normalize_signal, visible_at_decision

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "signal_trials" / "fetch_latest_signal.py"
OPEN_LIVE_TRIAL = SCRIPT.with_name("open_live_trial.py")
RUNBOOK = SCRIPT.with_name("DEMO_RUNBOOK.md")


def _load_producer():
    spec = importlib_util.spec_from_file_location("fetch_latest_signal_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fetch_latest_signal_producer_exists() -> None:
    assert SCRIPT.is_file(), "the one-signal REST producer must be shipped"


def test_unique_maximum_timestamp_wins_independent_of_response_order() -> None:
    producer = _load_producer()
    assert hasattr(producer, "select_unique_latest"), "producer must expose fail-closed selection"
    older = {"timestamp": "1000", "marker": "older"}
    newest = {"timestamp": "3000", "marker": "newest"}
    middle = {"timestamp": "2000", "marker": "middle"}

    assert producer.select_unique_latest([older, newest, middle]) is newest
    assert producer.select_unique_latest([middle, older, newest]) is newest
    assert producer.select_unique_latest([newest, middle, older]) is newest


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([], id="zero-rows"),
        pytest.param(["not-an-object"], id="malformed-row"),
        pytest.param([{"marker": "missing"}], id="missing-timestamp"),
        pytest.param([{"timestamp": "not-an-integer"}], id="malformed-timestamp"),
        pytest.param(
            [{"timestamp": "3000", "marker": "a"}, {"timestamp": "3000", "marker": "b"}],
            id="newest-tie",
        ),
    ],
)
def test_zero_malformed_or_tied_latest_rows_refuse_without_stdout(
    rows: list[object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    producer = _load_producer()
    assert hasattr(producer, "select_unique_latest"), "producer must expose fail-closed selection"

    with pytest.raises(producer.SignalSelectionError, match="signal|timestamp|latest|row"):
        producer.select_unique_latest(rows)

    assert capsys.readouterr().out == ""


def test_missing_credentials_stop_before_client_construction(
    capsys: pytest.CaptureFixture[str],
) -> None:
    producer = _load_producer()
    assert hasattr(producer, "main"), "producer must expose its fail-closed entry point"
    constructed = False

    @contextlib.asynccontextmanager
    async def client_factory(_credentials):
        nonlocal constructed
        constructed = True
        yield None

    assert producer.main(env={}, client_factory=client_factory) != 0
    captured = capsys.readouterr()
    assert constructed is False
    assert captured.out == ""
    assert "OKX_API_KEY" in captured.err
    assert "OKX_SECRET_KEY" in captured.err
    assert "OKX_PASSPHRASE" in captured.err


def test_exception_diagnostics_redact_every_credential_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    producer = _load_producer()
    assert hasattr(producer, "main"), "producer must expose its fail-closed entry point"
    sentinels = {
        "OKX_API_KEY": "NOT-A-REAL-API-KEY",
        "OKX_SECRET_KEY": "NOT-A-REAL-SECRET",
        "OKX_PASSPHRASE": "NOT-A-REAL-PASSPHRASE",
    }

    class ExplodingClient:
        async def list_signals(self, _filters):
            raise RuntimeError(" / ".join(sentinels.values()))

    @contextlib.asynccontextmanager
    async def client_factory(_credentials):
        yield ExplodingClient()

    assert producer.main(env=sentinels, client_factory=client_factory) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "redacted" in captured.err.lower()
    for sentinel in sentinels.values():
        assert sentinel not in captured.err


def test_producer_reuses_run_preflight_credential_loader_without_a_local_duplicate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    producer = _load_producer()
    duplicate_names = [
        name
        for name in ("_CREDENTIAL_VARS", "MissingCredentialError", "credentials_from_env")
        if hasattr(producer, name)
    ]
    assert duplicate_names == [], f"producer-local credential policy duplicates exist: {duplicate_names}"
    assert hasattr(producer, "run_preflight_seam")

    real_seam = producer.run_preflight_seam()
    expected_seam_path = SCRIPT.with_name("run_preflight.py").resolve()
    assert Path(real_seam.credentials_from_env.__code__.co_filename).resolve() == expected_seam_path

    called = False

    class CredentialTripwireSeam:
        @staticmethod
        def credentials_from_env(env):
            nonlocal called
            called = True
            assert env["OKX_API_KEY"] == "NOT-A-REAL-API-KEY"
            return producer.OKXCredentials(
                env["OKX_API_KEY"],
                env["OKX_SECRET_KEY"],
                env["OKX_PASSPHRASE"],
            )

    class FakeClient:
        async def list_signals(self, _filters):
            return SimpleNamespace(signals=(REST,))

    @contextlib.asynccontextmanager
    async def client_factory(_credentials):
        yield FakeClient()

    env = {
        "OKX_API_KEY": "NOT-A-REAL-API-KEY",
        "OKX_SECRET_KEY": "NOT-A-REAL-SECRET",
        "OKX_PASSPHRASE": "NOT-A-REAL-PASSPHRASE",
    }
    assert (
        producer.main(
            env=env,
            client_factory=client_factory,
            seam_loader=lambda: CredentialTripwireSeam,
        )
        == 0
    )
    assert called is True, "main bypassed the existing credentials_from_env seam"
    assert json.loads(capsys.readouterr().out) == REST


def test_success_writes_exactly_one_raw_json_object_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    producer = _load_producer()
    assert hasattr(producer, "main"), "producer must expose its fail-closed entry point"
    older = dict(REST, timestamp="1753399999999")

    class FakeClient:
        async def list_signals(self, filters):
            assert filters.chain_index == "196"
            return SimpleNamespace(signals=(older, REST))

    @contextlib.asynccontextmanager
    async def client_factory(_credentials):
        yield FakeClient()

    env = {
        "OKX_API_KEY": "NOT-A-REAL-API-KEY",
        "OKX_SECRET_KEY": "NOT-A-REAL-SECRET",
        "OKX_PASSPHRASE": "NOT-A-REAL-PASSPHRASE",
    }
    assert producer.main(env=env, client_factory=client_factory) == 0
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out) == REST
    assert captured.out.startswith("{") and captured.out.endswith("}\n")


def test_output_composes_with_real_open_live_trial_dry_run_without_durable_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    producer = _load_producer()
    assert hasattr(producer, "main"), "producer must expose its fail-closed entry point"

    class FakeClient:
        async def list_signals(self, _filters):
            return SimpleNamespace(signals=(REST,))

    @contextlib.asynccontextmanager
    async def client_factory(_credentials):
        yield FakeClient()

    env = {
        "OKX_API_KEY": "NOT-A-REAL-API-KEY",
        "OKX_SECRET_KEY": "NOT-A-REAL-SECRET",
        "OKX_PASSPHRASE": "NOT-A-REAL-PASSPHRASE",
    }
    assert producer.main(env=env, client_factory=client_factory) == 0
    raw_signal = capsys.readouterr().out

    result = subprocess.run(
        [
            sys.executable,
            str(OPEN_LIVE_TRIAL),
            "--source",
            "rest",
            "--dry-run",
            "--data-dir",
            str(tmp_path),
            "--now-ms",
            REST["timestamp"],
        ],
        input=raw_signal,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["published"] is False
    assert not (tmp_path / "live").exists()


def test_direct_importlib_exec_module_isolated_from_repo_cwd_needs_no_credentials_or_network(
    tmp_path: Path,
) -> None:
    """Mirror the image smoke test without adding the repository to sys.path in test code."""

    code = f"""
import importlib.util
import socket

def forbidden_network(*args, **kwargs):
    raise AssertionError("module import attempted network access")

socket.create_connection = forbidden_network
spec = importlib.util.spec_from_file_location("image_fetch_latest_signal", {str(SCRIPT)!r})
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert callable(module.main)
print("direct-load-ok")
"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"}
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "direct-load-ok\n"


def test_isolated_module_resolves_the_existing_httpx_transport_without_repo_on_sys_path(
    tmp_path: Path,
) -> None:
    """The by-path container command must reach the reviewed sibling seam without importing a package."""

    code = f"""
import importlib.util
import socket

def forbidden_network(*args, **kwargs):
    raise AssertionError("transport resolution attempted network access")

socket.create_connection = forbidden_network
spec = importlib.util.spec_from_file_location("image_fetch_latest_signal", {str(SCRIPT)!r})
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert hasattr(module, "httpx_transport_type"), "producer must resolve its existing HTTP seam by file"
transport_type = module.httpx_transport_type()
assert transport_type.__name__ == "HttpxTransport"
print("transport-load-ok")
"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"}
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "transport-load-ok\n"


def test_runbook_preopen_check_accepts_only_honest_404_and_final_qa_allows_closed_trial(
    tmp_path: Path,
) -> None:
    assert shutil.which("jq") is not None, "this executable runbook contract requires real jq"
    text = RUNBOOK.read_text(encoding="utf-8")
    start_marker = "<!-- PREOPEN_CHECK_START -->"
    end_marker = "<!-- PREOPEN_CHECK_END -->"
    assert start_marker in text and end_marker in text, "runbook must ship an executable pre-open gate"
    marked = text.split(start_marker, 1)[1].split(end_marker, 1)[0]
    assert "```sh" in marked
    shell = marked.split("```sh", 1)[1].split("```", 1)[0].strip()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

args = sys.argv[1:]
out = args[args.index("-o") + 1]
Path(out).write_text(os.environ["FAKE_BODY"], encoding="utf-8")
sys.stdout.write(os.environ["FAKE_STATUS"])
raise SystemExit(int(os.environ.get("FAKE_CURL_EXIT", "0")))
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    cases = [
        ("404", '{"error":"no_open_trial"}', 0, 0, "expected safe"),
        ("404", '{"error":"no_open_trial"}', 7, 1, "unexpected"),
        ("404", '{"error":"no_open_trial","unexpected":true}', 0, 1, "unexpected"),
        ("404", "{}", 0, 1, "unexpected"),
        ("200", '{"trial_id":"already-open"}', 0, 1, "already open"),
        ("404", '{"error":"different"}', 0, 1, "unexpected"),
        ("404", "[]", 0, 1, "unexpected"),
        ("404", "{invalid-json", 0, 1, "unexpected"),
        ("500", '{"error":"no_open_trial"}', 0, 1, "unexpected"),
    ]
    for status, body, curl_exit, expected_exit, expected_diagnostic in cases:
        env = dict(os.environ)
        env.update(
            {
                "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                "FAKE_STATUS": status,
                "FAKE_BODY": body,
                "FAKE_CURL_EXIT": str(curl_exit),
            }
        )
        result = subprocess.run(
            ["/bin/sh", "-c", shell],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == expected_exit, (status, body, result.stderr)
        assert expected_diagnostic in (result.stdout + result.stderr).lower()

    qa = text.split("## 8. QA", 1)[1]
    assert "curl -fsS https://api.proofarena.xyz/signal-trials/open-trial" not in qa
    assert "QA_OPEN_STATUS" in qa
    assert "closed trial" in qa.lower()


_LOCAL_TRIAL_ID = f"trial_{'a' * 24}_1753400000000"
_LOCAL_EVIDENCE_HASH = "b" * 64
_LOCAL_OPEN_SUMMARY = {
    "trial_id": _LOCAL_TRIAL_ID,
    "trial_mode": "live",
    "t0_ms": 1_753_400_000_000,
    "commit_deadline_ms": 1_753_400_300_000,
    "decision_window_ms": 300_000,
    "evidence_hash": _LOCAL_EVIDENCE_HASH,
    "evidence_fields": ["symbol"],
    "published": True,
}
_PUBLIC_LOCAL_TRIAL = {
    "trial_id": _LOCAL_TRIAL_ID,
    "trial_mode": "live",
    "t0_ms": 1_753_400_000_000,
    "commit_deadline_ms": 1_753_400_300_000,
    "evidence": {"symbol": "TKN"},
    "evidence_hash": _LOCAL_EVIDENCE_HASH,
    "outcome": None,
}
_REVIEWED_REST_SIGNAL = normalize_signal(REST, source="rest")
_REVIEWED_REST_EVIDENCE_HASH = evidence_hash(_REVIEWED_REST_SIGNAL)
_REVIEWED_REST_EVIDENCE = visible_at_decision(_REVIEWED_REST_SIGNAL)


def _extract_live_open_shell() -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    section = text.split("## 4. Dry-run and open", 1)[1].split(
        "## 5. Explicit single payment",
        1,
    )[0]
    shell_blocks = section.split("```sh")[1:]
    assert len(shell_blocks) >= 2, "Step 4 must contain separate dry-run and live-open commands"
    return shell_blocks[-1].split("```", 1)[0].strip()


def _mutate_live_open_command(shell: str, mutation: str) -> str:
    replacements = {
        "wrong-script-path": (
            "/app/scripts/signal_trials/open_live_trial.py",
            "/app/scripts/signal_trials/does_not_exist.py",
        ),
        "missing-signal-file": (
            "  --signal-file /tmp/proofarena-rest-signal.json \\\n",
            "",
        ),
        "missing-data-dir": (
            '  --data-dir "$SIGNAL_TRIALS_DATA_DIR" > "$OPEN_SUMMARY"',
            '  > "$OPEN_SUMMARY"',
        ),
    }
    before, after = replacements[mutation]
    mutated = shell.replace(before, after, 1)
    assert mutated != shell, f"{mutation} did not reach the extracted production live command"
    return mutated


def _run_live_open_shell(
    tmp_path: Path,
    *,
    open_exit: int = 0,
    local_summary: dict[str, object] | None = None,
    preopen_exit: int = 0,
    preopen_status: str = "404",
    preopen_body: str = '{"error":"no_open_trial"}',
    public_exit: int = 0,
    public_status: str = "200",
    public_body: dict[str, object] | None = None,
    prehold_lock: bool = False,
    command_mutation: str | None = None,
) -> SimpleNamespace:
    assert shutil.which("jq") is not None, "the executable open workflow requires real jq"
    data_dir = tmp_path / "signal-trials-data"
    data_dir.mkdir()
    temp_dir = tmp_path / "temporary-files"
    temp_dir.mkdir()
    lock_dir = data_dir / ".rest-signal-open.lock"
    if prehold_lock:
        lock_dir.mkdir()

    open_marker = tmp_path / "open-command-called"
    curl_log = tmp_path / "curl-urls"
    signal_fixture = tmp_path / "reviewed-rest-signal.json"
    signal_fixture.write_text(json.dumps(REST), encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text(
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

args = sys.argv[1:]
expected = [
    "/app/scripts/signal_trials/open_live_trial.py",
    "--source",
    "rest",
    "--signal-file",
    "/tmp/proofarena-rest-signal.json",
    "--data-dir",
    os.environ["EXPECTED_DATA_DIR"],
]
if args != expected:
    sys.stderr.write(f"strict open command mismatch: {args!r}\\n")
    raise SystemExit(64)

Path(os.environ["FAKE_OPEN_MARKER"]).write_text("called", encoding="utf-8")
summary = os.environ.get("FAKE_LOCAL_SUMMARY", "")
if summary:
    sys.stdout.write(summary)
    raise SystemExit(int(os.environ["FAKE_OPEN_EXIT"]))
open_exit = int(os.environ["FAKE_OPEN_EXIT"])
if open_exit:
    sys.stderr.write(os.environ.get("FAKE_OPEN_STDERR", ""))
    raise SystemExit(open_exit)

mapped = [
    os.environ["REAL_OPEN_SCRIPT"],
    "--source",
    "rest",
    "--signal-file",
    os.environ["REAL_SIGNAL_FILE"],
    "--data-dir",
    os.environ["EXPECTED_DATA_DIR"],
]
os.execv(sys.executable, [sys.executable, *mapped])
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
url = args[-1]
with Path(os.environ["FAKE_CURL_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(url + "\\n")
out = args[args.index("-o") + 1]
if url.endswith("/open-trial"):
    body = os.environ["FAKE_PREOPEN_BODY"]
    status = os.environ["FAKE_PREOPEN_STATUS"]
    exit_code = int(os.environ["FAKE_PREOPEN_EXIT"])
else:
    body = os.environ["FAKE_PUBLIC_BODY"]
    if not body:
        trial_id = url.rsplit("/", 1)[-1]
        trial_path = (
            Path(os.environ["EXPECTED_DATA_DIR"])
            / "live"
            / "trials"
            / f"{trial_id}.json"
        )
        document = json.loads(trial_path.read_text(encoding="utf-8"))
        body = json.dumps(
            {
                "trial_id": document["trial_id"],
                "trial_mode": document["trial_mode"],
                "t0_ms": document["t0_ms"],
                "commit_deadline_ms": document["commit_deadline_ms"],
                "evidence": json.loads(os.environ["REAL_PUBLIC_EVIDENCE"]),
                "evidence_hash": os.environ["REAL_EVIDENCE_HASH"],
                "outcome": None,
            }
        )
    status = os.environ["FAKE_PUBLIC_STATUS"]
    exit_code = int(os.environ["FAKE_PUBLIC_EXIT"])
Path(out).write_text(body, encoding="utf-8")
sys.stdout.write(status)
raise SystemExit(exit_code)
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    shell = _extract_live_open_shell().replace(
        "/var/lib/veridex/signal-trials",
        str(data_dir),
    )
    if command_mutation is not None:
        shell = _mutate_live_open_command(shell, command_mutation)
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "SIGNAL_TRIALS_DATA_DIR": str(data_dir),
            "TMPDIR": f"{temp_dir}{os.sep}",
            "FAKE_OPEN_MARKER": str(open_marker),
            "FAKE_OPEN_EXIT": str(open_exit),
            "FAKE_OPEN_STDERR": "simulated open refusal\n" if open_exit else "",
            "FAKE_LOCAL_SUMMARY": "" if local_summary is None else json.dumps(local_summary),
            "EXPECTED_DATA_DIR": str(data_dir),
            "REAL_OPEN_SCRIPT": str(OPEN_LIVE_TRIAL),
            "REAL_SIGNAL_FILE": str(signal_fixture),
            "REAL_EVIDENCE_HASH": _REVIEWED_REST_EVIDENCE_HASH,
            "REAL_PUBLIC_EVIDENCE": json.dumps(_REVIEWED_REST_EVIDENCE),
            "FAKE_CURL_LOG": str(curl_log),
            "FAKE_PREOPEN_EXIT": str(preopen_exit),
            "FAKE_PREOPEN_STATUS": preopen_status,
            "FAKE_PREOPEN_BODY": preopen_body,
            "FAKE_PUBLIC_EXIT": str(public_exit),
            "FAKE_PUBLIC_STATUS": public_status,
            "FAKE_PUBLIC_BODY": "" if public_body is None else json.dumps(public_body),
        }
    )
    result = subprocess.run(
        ["/bin/sh", "-c", shell],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    urls = curl_log.read_text(encoding="utf-8").splitlines() if curl_log.is_file() else []
    persisted_paths = sorted((data_dir / "live" / "trials").glob("*.json"))
    persisted_trials = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in persisted_paths
    ]
    return SimpleNamespace(
        result=result,
        open_marker=open_marker,
        lock_dir=lock_dir,
        temp_dir=temp_dir,
        urls=urls,
        persisted_trials=persisted_trials,
    )


def test_live_open_refuses_failed_open_even_if_foreign_public_read_succeeds(
    tmp_path: Path,
) -> None:
    foreign = {
        **_PUBLIC_LOCAL_TRIAL,
        "trial_id": f"trial_{'f' * 24}_1753400000001",
        "evidence_hash": "f" * 64,
    }
    execution = _run_live_open_shell(
        tmp_path,
        open_exit=17,
        local_summary=None,
        public_body=foreign,
    )

    assert execution.result.returncode != 0
    assert foreign["trial_id"] not in execution.result.stdout
    assert "TRIAL_ID=" not in execution.result.stdout


def test_live_open_atomic_lock_excludes_a_second_reviewed_opener_before_mutation(
    tmp_path: Path,
) -> None:
    execution = _run_live_open_shell(tmp_path, prehold_lock=True)

    assert execution.result.returncode != 0
    assert not execution.open_marker.exists(), "second opener reached the mutation command"
    assert execution.lock_dir.is_dir(), "refusing contender removed the first opener's lock"


@pytest.mark.parametrize(
    ("preopen_exit", "preopen_status", "preopen_body"),
    [
        pytest.param(7, "404", '{"error":"no_open_trial"}', id="curl-failure"),
        pytest.param(0, "200", '{"trial_id":"already-open"}', id="already-open"),
        pytest.param(
            0,
            "404",
            '{"error":"no_open_trial","unexpected":true}',
            id="schema-mismatch",
        ),
    ],
)
def test_live_open_immediate_precheck_refusal_prevents_mutation(
    tmp_path: Path,
    preopen_exit: int,
    preopen_status: str,
    preopen_body: str,
) -> None:
    execution = _run_live_open_shell(
        tmp_path,
        preopen_exit=preopen_exit,
        preopen_status=preopen_status,
        preopen_body=preopen_body,
    )

    assert execution.result.returncode != 0
    assert not execution.open_marker.exists()


@pytest.mark.parametrize(
    ("field", "mismatched_value"),
    [
        pytest.param(
            "trial_id",
            f"trial_{'c' * 24}_1753400000002",
            id="trial-id",
        ),
        pytest.param("evidence_hash", "c" * 64, id="evidence-hash"),
        pytest.param("commit_deadline_ms", 1_753_400_300_001, id="deadline"),
    ],
)
def test_live_open_refuses_public_trial_mismatch_before_payment_handoff(
    tmp_path: Path,
    field: str,
    mismatched_value: object,
) -> None:
    public = {**_PUBLIC_LOCAL_TRIAL, field: mismatched_value}
    execution = _run_live_open_shell(
        tmp_path,
        local_summary=_LOCAL_OPEN_SUMMARY,
        public_body=public,
    )

    assert execution.result.returncode != 0
    assert "TRIAL_ID=" not in execution.result.stdout
    assert not execution.lock_dir.exists()
    assert list(execution.temp_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        pytest.param("published", False, id="not-published"),
        pytest.param("trial_id", "unsafe/trial", id="unsafe-trial-id"),
        pytest.param("decision_window_ms", 299_999, id="wrong-window"),
        pytest.param("evidence_hash", "not-a-hash", id="bad-evidence-hash"),
        pytest.param("commit_deadline_ms", "not-an-integer", id="bad-deadline"),
    ],
)
def test_live_open_refuses_invalid_local_summary_before_payment_handoff(
    tmp_path: Path,
    field: str,
    invalid_value: object,
) -> None:
    local = {**_LOCAL_OPEN_SUMMARY, field: invalid_value}
    execution = _run_live_open_shell(tmp_path, local_summary=local)

    assert execution.result.returncode != 0
    assert "TRIAL_ID=" not in execution.result.stdout


def test_live_open_honest_single_writer_binds_direct_read_and_emits_only_local_id(
    tmp_path: Path,
) -> None:
    execution = _run_live_open_shell(tmp_path)

    assert execution.result.returncode == 0, execution.result.stderr
    assert len(execution.persisted_trials) == 1
    persisted = execution.persisted_trials[0]
    trial_id = persisted["trial_id"]
    assert execution.result.stdout == f"TRIAL_ID={trial_id}\n"
    assert execution.open_marker.is_file()
    assert execution.urls == [
        "https://api.proofarena.xyz/signal-trials/open-trial",
        f"https://api.proofarena.xyz/signal-trials/trials/{trial_id}",
    ]
    assert persisted["trial_mode"] == "live"
    assert persisted["commit_deadline_ms"] - persisted["t0_ms"] == 300_000
    assert persisted["sig"] == _REVIEWED_REST_SIGNAL.model_dump(mode="json")
    assert not execution.lock_dir.exists()
    assert list(execution.temp_dir.iterdir()) == []


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param("wrong-script-path", id="wrong-script-path"),
        pytest.param("missing-signal-file", id="missing-signal-file"),
        pytest.param("missing-data-dir", id="missing-data-dir"),
    ],
)
def test_live_open_strict_real_command_boundary_kills_mutations(
    tmp_path: Path,
    mutation: str,
) -> None:
    execution = _run_live_open_shell(tmp_path, command_mutation=mutation)

    assert execution.result.returncode != 0
    assert execution.result.stdout == ""
    assert not execution.open_marker.exists()
    assert execution.persisted_trials == []


def test_demo_runbook_pins_order_hosts_boundaries_windows_and_authority() -> None:
    assert RUNBOOK.is_file(), "the safe live-demo runbook must be shipped"
    text = RUNBOOK.read_text(encoding="utf-8")

    ordered_steps = [
        "## 1. Preflight",
        "## 2. Seal",
        "## 3. Publish",
        "## 4. Dry-run and open",
        "## 5. Explicit single payment",
        "## 6. Receipt",
        "## 7. One-hour settlement",
        "## 8. QA",
    ]
    positions = [text.index(step) for step in ordered_steps]
    assert positions == sorted(positions)

    assert "https://proofarena.xyz" in text
    assert "https://api.proofarena.xyz" in text
    assert "curl -fsS https://api.proofarena.xyz/healthz" in text
    assert "curl -fsS https://api.proofarena.xyz/readyz" in text
    assert "container operator" in text.lower()
    assert "buyer workstation" in text.lower()
    assert "published=false" in text
    assert "300000" in text and "five-minute" in text.lower()
    assert "3600000" in text and "one-hour" in text.lower()
    assert "exactly one payment" in text.lower()
    assert "no deployment, signing, or payment authority" in text.lower()
    assert "OKX_API_KEY=" not in text
    assert "OKX_SECRET_KEY=" not in text
    assert "OKX_PASSPHRASE=" not in text
    assert "PAYMENT-SIGNATURE:" not in text
