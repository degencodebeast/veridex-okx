import contextlib
import json
import os
import subprocess
import sys
from importlib import util as importlib_util
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.signal_trials.test_challenge_spec import REST

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
