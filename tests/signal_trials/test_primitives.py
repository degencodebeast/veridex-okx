import pytest
from veridex.signal_trials.primitives import brier_score

def test_brier_neutral_is_quarter(): assert brier_score([0.5, 0.5], [1, 0]) == pytest.approx(0.25)
def test_brier_perfect_is_zero(): assert brier_score([1.0, 0.0], [1, 0]) == 0.0
def test_brier_raises_on_mismatch():
    with pytest.raises(ValueError): brier_score([0.5], [1, 0])
def test_brier_matches_sports_module_math():
    from veridex.scoring import _brier   # CLV-coupled sports Brier; NEVER modified
    rows = [
        {"clv_bps": 50,  "raw_prescore": {"raw_action": {"params": {"confidence": 0.7}}}},
        {"clv_bps": -10, "raw_prescore": {"raw_action": {"params": {"confidence": 0.2}}}},
    ]
    assert brier_score([0.7, 0.2], [1, 0]) == pytest.approx(_brier(rows))
