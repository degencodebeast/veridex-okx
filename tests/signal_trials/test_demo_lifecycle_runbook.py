"""Executable contracts for the exact-pack and exact-trial demo workflow."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

RUNBOOK = Path(__file__).resolve().parents[2] / "scripts" / "signal_trials" / "DEMO_RUNBOOK.md"
TRIAL_ID = "trial_runbook_contract"


def _section(number: int) -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    match = re.search(rf"^## {number}\..*?\n(.*?)(?=^## {number + 1}\.|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"runbook section {number} is missing"
    return match.group(1)


def _settlement_block() -> str:
    match = re.search(r"```sh\n(.*?)\n```", _section(7), re.DOTALL)
    assert match is not None, "section 7 has no executable shell block"
    return match.group(1)


def _row(
    status: str,
    *,
    recorded: bool,
    settlements_recorded: int = 0,
    trial_id: str = TRIAL_ID,
) -> dict[str, Any]:
    return {
        "trial_id": trial_id,
        "status": status,
        "recorded": recorded,
        "settlements_recorded": settlements_recorded,
    }


def _summary(*, dry_run: bool, eligible: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "now_ms": 1_700_004_000_000,
        "bar": "1m",
        "season_selection": {"chain_index": "196", "bar": "1m"},
        "dry_run": dry_run,
        "eligible": eligible,
        "trials": rows,
        "agent_records": [],
    }


def _execute_settlement_block(
    tmp_path: Path,
    *,
    dry_payload: dict[str, Any],
    live_payload: dict[str, Any],
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the literal runbook block while replacing only the settlement process."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls_path = tmp_path / "calls.log"
    fake_python = fake_bin / "python"
    fake_python.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$DEMO_CALL_LOG"
case " $* " in
  *" --dry-run "*) printf '%s\\n' "$DEMO_DRY_JSON" ;;
  *) printf '%s\\n' "$DEMO_LIVE_JSON" ;;
esac
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    preflight = tmp_path / "preflight_result.json"
    preflight.write_text(json.dumps({"bar": "1m"}), encoding="utf-8")
    block = _settlement_block().replace(
        "/var/lib/veridex/signal-trials/preflight_result.json",
        str(preflight),
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DEMO_CALL_LOG": str(calls_path),
        "DEMO_DRY_JSON": json.dumps(dry_payload),
        "DEMO_LIVE_JSON": json.dumps(live_payload),
        "TRIAL_ID": TRIAL_ID,
    }
    completed = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", block],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    calls = calls_path.read_text(encoding="utf-8").splitlines() if calls_path.exists() else []
    return completed, calls


def test_live_coverage_gap_is_refused_independently_of_call_count(tmp_path: Path) -> None:
    """A valid JSON live result that is nonterminal must make the workflow refuse."""
    dry = _summary(dry_run=True, eligible=1, rows=[_row("settled", recorded=False)])
    live = _summary(dry_run=False, eligible=1, rows=[_row("coverage_gap", recorded=True)])

    result, _calls = _execute_settlement_block(tmp_path, dry_payload=dry, live_payload=live)

    assert result.returncode != 0, "the workflow unexpectedly accepted a live coverage_gap result"


def test_live_recorded_false_is_refused_independently_of_call_count(tmp_path: Path) -> None:
    """A terminal-looking result that was not recorded is not durable demo evidence."""
    dry = _summary(dry_run=True, eligible=1, rows=[_row("settled", recorded=False)])
    live = _summary(dry_run=False, eligible=1, rows=[_row("settled", recorded=False)])

    result, _calls = _execute_settlement_block(tmp_path, dry_payload=dry, live_payload=live)

    assert result.returncode != 0, "the workflow unexpectedly accepted recorded=false as terminal evidence"


def test_live_terminal_zero_participant_settlements_is_refused(tmp_path: Path) -> None:
    """An outcome without the paid participant settlement is incomplete demo evidence."""
    dry = _summary(dry_run=True, eligible=1, rows=[_row("settled", recorded=False)])
    live = _summary(dry_run=False, eligible=1, rows=[_row("settled", recorded=True)])

    result, _calls = _execute_settlement_block(tmp_path, dry_payload=dry, live_payload=live)

    assert result.returncode != 0, (
        "the workflow unexpectedly accepted a terminal outcome with zero participant settlements"
    )


def test_settlement_runs_exact_trial_dry_run_then_exact_trial_live_once(tmp_path: Path) -> None:
    """Call sequencing is its own contract, separate from output acceptance."""
    dry = _summary(dry_run=True, eligible=1, rows=[_row("settled", recorded=False)])
    live = _summary(
        dry_run=False,
        eligible=1,
        rows=[_row("settled", recorded=True, settlements_recorded=1)],
    )

    result, calls = _execute_settlement_block(tmp_path, dry_payload=dry, live_payload=live)

    assert result.returncode == 0
    assert len(calls) == 2
    assert "--dry-run" in calls[0]
    assert "--dry-run" not in calls[1]
    assert all(f"--trial-id {TRIAL_ID}" in call for call in calls)


def test_both_settlement_commands_are_pinned_to_the_exact_trial() -> None:
    block = _settlement_block()
    assert block.count('--trial-id "$TRIAL_ID"') == 2


def test_ambiguous_dry_run_refuses_before_a_live_call(tmp_path: Path) -> None:
    dry = _summary(
        dry_run=True,
        eligible=2,
        rows=[
            _row("settled", recorded=False),
            _row("settled", recorded=False, trial_id="trial_other"),
        ],
    )
    live = _summary(
        dry_run=False,
        eligible=1,
        rows=[_row("settled", recorded=True, settlements_recorded=1)],
    )

    result, calls = _execute_settlement_block(tmp_path, dry_payload=dry, live_payload=live)

    assert result.returncode != 0
    assert len(calls) == 1
    assert "--dry-run" in calls[0]


def test_publish_workflow_uses_local_hash_provenance_and_public_identity() -> None:
    section = _section(3)
    assert 'APPROVED_PACK_CONTENT_HASH="<reviewed-sealed-pack-content-hash>"' in section
    assert '--expected-content-hash "$APPROVED_PACK_CONTENT_HASH"' in section
    assert ".detail.approved_pack_content_hash" in section
    assert "published/state.json" in section
    assert "public season ID, chain, and bar" in section
    assert "same chain, bar, and pack hash" not in section


def test_terminal_timing_names_bar_dependent_no_candle_floors() -> None:
    section = _section(7)
    normalized = " ".join(section.split())
    assert "71 minutes" in section
    assert "130 minutes" in section
    assert "1m" in section
    assert "1H" in section
    assert "confirmed eligible candle" in section
    assert "process exit 0" in normalized
