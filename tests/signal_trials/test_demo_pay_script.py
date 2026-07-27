"""H1.3 Step 3 — structural tests for scripts/signal_trials/demo_pay.sh.

The script cannot be EXECUTED here: it needs the deployed API, the onchainos CLI and
the second wallet, all of which are operator-only. That does not make it untestable.
What is testable without running it is its structure, its failure modes, and the
property that matters most — that it can never print the payment signature.

Every predicate below carries an ACCEPTANCE assertion and, where the predicate is
load-bearing, a DISCRIMINATION control proving the test would FAIL on a script that
violated it (C52). A test that passes on both the correct and the broken script pins
nothing.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "signal_trials" / "demo_pay.sh"


@pytest.fixture(scope="module")
def source() -> str:
    return SCRIPT.read_text()


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), f"{SCRIPT} is missing"
    assert os.access(SCRIPT, os.X_OK), "demo_pay.sh must be executable to be a demo path"


def test_script_is_syntactically_valid() -> None:
    """`bash -n` parses without executing — the strongest check available offline."""
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"syntax error: {result.stderr}"


def test_it_fails_closed_without_required_inputs() -> None:
    """No BASE_URL means no request. The script must refuse, not guess a default.

    Run with an emptied environment so a value leaking in from the developer's shell
    cannot make this pass for the wrong reason.
    """
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert result.returncode != 0, "a missing BASE_URL must abort"
    assert "BASE_URL" in result.stderr, "the error must name the variable it needs"


def _printing_lines(text: str) -> list[str]:
    """Lines that write to stdout/stderr — where a leak could actually occur."""
    return [ln for ln in text.splitlines() if re.search(r"\b(echo|printf)\b", ln)]


def _printed_arguments(text: str) -> list[str]:
    """What `echo`/`printf` actually WRITE, not merely the lines they appear on.

    A line-granular check is the wrong instrument for this property and produced a
    false positive on a safe line::

        [ -n "$SIGNATURE" ] || { echo "FATAL: ..." >&2; exit 1; }

    The signature is in the TEST before the ``||``; the echo prints a fixed string.
    Only text following the print keyword can reach the output.
    """
    return [
        ln[match.end() :]
        for ln in text.splitlines()
        for match in re.finditer(r"\b(?:echo|printf)\b", ln)
    ]


def test_the_payment_signature_is_never_printed(source: str) -> None:
    """ACCEPTANCE — the signature is sent in a header and never echoed.

    A shell trace of a leaking script would publish a spendable authorization into
    whatever captures the demo's output.
    """
    leaks = [arg for arg in _printed_arguments(source) if "SIGNATURE}" in arg or "$SIGNATURE" in arg]
    assert leaks == [], f"the signature reaches a printing statement: {leaks}"


def test_the_leak_check_would_actually_catch_a_leak(tmp_path: Path, source: str) -> None:
    """DISCRIMINATION — mutate the script to echo the signature; the check must FAIL.

    Without this, `test_the_payment_signature_is_never_printed` could be passing
    because the pattern never matches anything, not because the script is safe.
    """
    mutated = source.replace(
        '[ -n "$SIGNATURE" ]',
        'echo "signature is ${SIGNATURE}"\n[ -n "$SIGNATURE" ]',
        1,
    )
    assert mutated != source, "the mutation did not apply — the control proves nothing"

    leaks = [
        arg for arg in _printed_arguments(mutated) if "SIGNATURE}" in arg or "$SIGNATURE" in arg
    ]
    assert leaks, "the leak check failed to detect a deliberately leaking script"


def test_it_asserts_the_402_then_the_200(source: str) -> None:
    """The whole point is that the endpoint CHARGES and then ACCEPTS payment.

    A demo that only checked the 200 would pass against a free endpoint, proving
    nothing about x402.
    """
    assert '!= "402"' in source, "the unpaid request must be asserted to return 402"
    assert '!= "200"' in source, "the paid replay must be asserted to return 200"


def test_it_pays_from_the_second_wallet(source: str) -> None:
    """Plan L243: the payer is the SECOND wallet, not the operator's funding wallet."""
    assert "onchainos payment pay" in source
    assert "ONCHAINOS_WALLET" in source, "the wallet must come from the environment"


def test_it_prints_the_transaction_hash_and_receipt_id(source: str) -> None:
    """Both are public and both are the evidence the demo exists to produce."""
    printed = "\n".join(_printing_lines(source))
    assert "TX_HASH" in printed, "the transaction hash must be printed"
    assert "RECEIPT_ID" in printed, "the receipt id must be printed"


def test_no_credential_is_embedded(source: str) -> None:
    """Gate A — no key, address or funding value may live in the file."""
    forbidden = re.findall(r"0x[a-fA-F0-9]{20,}", source)
    assert forbidden == [], f"an address or key literal is embedded: {forbidden}"
