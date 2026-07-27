"""H1.3 Step 3 — EXECUTABLE contract tests for demo_pay.sh.

These exist because the structural tests could not have caught the defects that a
review found. Those tests read the script's SOURCE TEXT; they asserted the string
``onchainos payment pay`` was present and passed happily while the real CLI rejected
the invented ``--wallet`` flag and while the script forwarded a whole JSON document
as a payment signature. A test that never invokes a contract cannot observe it.

So these tests RUN the script against a fake ``onchainos`` and a fake ``curl`` placed
on PATH, and assert on what the script actually DID:

* only ``.authorization_header`` is sent as ``PAYMENT-SIGNATURE`` — never the envelope
* a sentinel authorization never appears in output, even under ``bash -x``
* a 200 without ``PAYMENT-RESPONSE`` fails instead of claiming a settlement

Each predicate has a DISCRIMINATION control: the fake is perturbed so the script
SHOULD fail, and the test asserts it does. A harness that only ever sees the happy
path proves nothing about enforcement.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "signal_trials" / "demo_pay.sh"

SENTINEL_AUTH = "SENTINEL-SPENDABLE-AUTHORIZATION-9f3a"
RECEIPT_ID = "rcpt_test_0001"
TX_HASH = "0xfeedfacefeedfacefeedfacefeedfacefeedface"

FAKE_ONCHAINOS = """#!/usr/bin/env bash
set -euo pipefail
case "$1 ${2:-}" in
  "wallet switch") echo "switched" ;;
  "wallet current") printf '{"account_id":"%s"}\\n' "${ONCHAINOS_ACCOUNT_ID}" ;;
  "payment pay")
    # v2 output contract: the authorization is ONE FIELD of this document.
    printf '{"authorization_header":"%s","header_name":"%s","scheme":"exact","wallet":"w"}\\n' \\
      "@@AUTH@@" "@@HEADER_NAME@@" ;;
  *) echo "fake onchainos: unexpected $*" >&2; exit 2 ;;
esac
"""

# Records every -H argument so the test can assert what actually reached the wire.
FAKE_CURL = """#!/usr/bin/env bash
set -euo pipefail
HDR_OUT=""; BODY_OUT=""; SENT_HEADERS="$CURL_LOG"
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    -D) HDR_OUT="${args[$((i+1))]}" ;;
    -o) BODY_OUT="${args[$((i+1))]}" ;;
    -H) echo "${args[$((i+1))]}" >> "$SENT_HEADERS" ;;
  esac
done
if grep -qi '^PAYMENT-SIGNATURE:' "$SENT_HEADERS" 2>/dev/null; then
  { echo "HTTP/1.1 200 OK"; @@SETTLE@@; } > "$HDR_OUT"
  printf '{"receipt_id":"%s"}\\n' "@@RECEIPT_ID@@" > "$BODY_OUT"
  printf '200'
else
  { echo "HTTP/1.1 402 Payment Required"; echo "PAYMENT-REQUIRED: @@CHALLENGE@@"; } > "$HDR_OUT"
  printf '{"error":"payment_required"}\\n' > "$BODY_OUT"
  printf '402'
fi
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _settle_header(transaction: str | None) -> str:
    """Emit a base64 PAYMENT-RESPONSE, or nothing at all to model an idempotent replay."""
    if transaction is None:
        return ":"  # a no-op shell command: the header is simply absent
    import base64
    import json

    encoded = base64.b64encode(json.dumps({"transaction": transaction}).encode()).decode()
    return f'echo "PAYMENT-RESPONSE: {encoded}"'


@pytest.fixture
def harness(tmp_path: Path):
    """Build a fake CLI + curl on PATH and return a runner."""

    def run(
        *,
        auth: str = SENTINEL_AUTH,
        header_name: str = "PAYMENT-SIGNATURE",
        transaction: str | None = TX_HASH,
        trace: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        log = tmp_path / "sent_headers.txt"
        log.write_text("")

        _write_exec(
            bindir / "onchainos", FAKE_ONCHAINOS.replace("@@AUTH@@", auth).replace("@@HEADER_NAME@@", header_name)
        )
        _write_exec(
            bindir / "curl",
            FAKE_CURL.replace("@@CHALLENGE@@", "Y2hhbGxlbmdl")
            .replace("@@RECEIPT_ID@@", RECEIPT_ID)
            .replace("@@SETTLE@@", _settle_header(transaction)),
        )

        env = {
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
            "BASE_URL": "https://example.invalid",
            "TRIAL_ID": "trial_test",
            "ONCHAINOS_ACCOUNT_ID": "acct_second_0001",
            "CURL_LOG": str(log),
        }
        cmd = ["bash", "-x", str(SCRIPT)] if trace else ["bash", str(SCRIPT)]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
        sent = [ln for ln in log.read_text().splitlines() if ln.strip()]
        return proc, sent

    return run


def test_the_happy_path_completes(harness) -> None:
    proc, _ = harness()
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert RECEIPT_ID in proc.stdout
    assert TX_HASH in proc.stdout


def test_only_the_authorization_header_field_is_sent(harness) -> None:
    """ACCEPTANCE — the wire carries the FIELD, never the CLI's JSON envelope.

    This is the defect the source-reading tests could not see: the script forwarded
    the whole ``{authorization_header, header_name, scheme, wallet}`` document, which
    the server's decoder rejects.
    """
    _, sent = harness()
    sig = [h for h in sent if h.lower().startswith("payment-signature:")]
    assert len(sig) == 1, f"expected exactly one signature header, got {sent}"
    value = sig[0].split(":", 1)[1].strip()
    assert value == SENTINEL_AUTH, f"the wire value is not the authorization field: {value!r}"
    assert "authorization_header" not in value, "the JSON envelope reached the wire"
    assert "scheme" not in value and "wallet" not in value


def test_a_wrong_header_name_from_the_cli_is_refused(harness) -> None:
    """DISCRIMINATION — if the CLI's contract shifts, the script must stop, not guess."""
    proc, sent = harness(header_name="X-SOMETHING-ELSE")
    assert proc.returncode != 0, "a mismatched header_name must abort"
    assert "PAYMENT-SIGNATURE" in proc.stderr
    assert not [h for h in sent if h.lower().startswith("payment-signature:")]


def test_an_empty_authorization_is_refused(harness) -> None:
    """DISCRIMINATION — an empty field must not be sent as a signature."""
    proc, sent = harness(auth="")
    assert proc.returncode != 0
    assert not [h for h in sent if h.lower().startswith("payment-signature:")]


def test_a_200_without_settlement_evidence_is_not_reported_as_success(harness) -> None:
    """The route omits PAYMENT-RESPONSE on an idempotent replay — no new payment.

    Reporting that as a settled payment would claim exactly what the response
    disproves, in the demo whose entire purpose is proving a payment occurred.
    """
    proc, _ = harness(transaction=None)
    assert proc.returncode != 0, "a 200 with no PAYMENT-RESPONSE must not report success"
    assert "OK:" not in proc.stdout
    assert "settled no payment" in proc.stderr


def test_the_authorization_never_appears_under_bash_x(harness) -> None:
    """ACCEPTANCE — xtrace expands assignments and curl arguments.

    The previous script documented this protection and did not enforce it; a traced
    run printed the spendable value. Demo logs commonly use tracing.
    """
    proc, _ = harness(trace=True)
    combined = proc.stdout + proc.stderr
    assert SENTINEL_AUTH not in combined, "the spendable authorization leaked under bash -x"


def test_the_trace_harness_would_actually_catch_a_leak(harness) -> None:
    """DISCRIMINATION — prove the trace test can fail.

    Without this, the assertion above could be passing because the sentinel never
    reaches the script rather than because tracing is suppressed. The receipt id
    travels the same path and is deliberately NOT protected, so it must be visible.
    """
    proc, _ = harness(trace=True)
    combined = proc.stdout + proc.stderr
    assert RECEIPT_ID in combined, (
        "the harness sees no traced output at all, so the leak assertion proves nothing"
    )
