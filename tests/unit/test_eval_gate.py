import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "gate", Path(__file__).resolve().parents[2] / "evals" / "promptfoo" / "gate.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _summary(recall, precision, fn=0, errors=0, gap=0.0):
    return {"by_language": {"全体": {"recall": recall, "precision": precision, "fn": fn, "errors": errors}},
            "language_gap": {"recall": gap}}


def test_gate_passes_when_all_thresholds_are_met():
    assert gate.check(_summary(0.95, 0.8), {"min_recall": 0.9, "min_precision": 0.7, "max_errors": 0}) == []


def test_gate_fails_on_recall_drop_errors_and_language_gap():
    fails = gate.check(_summary(0.85, 0.8, errors=2, gap=0.3),
                       {"min_recall": 0.9, "max_errors": 0, "max_language_recall_gap": 0.2})
    assert len(fails) == 3
