from __future__ import annotations

from sqx.store import _json


def test_json_column_handles_null_and_nan():
    assert _json(None) == {}
    assert _json(float("nan")) == {}
    assert _json("") == {}
    assert _json('{"a": 1}') == {"a": 1}
    assert _json("not json") == {}
    assert _json(None, []) == []


def test_bank_roundtrip(tmp_path):
    from sqx.store import Store

    s = Store(tmp_path / "test.duckdb")
    s.create_run("run1", "source", {}, 1)
    s.create_bank("bank_x", "Survivors", "run1", "rob1", {"confidence": 95})
    row = {
        "strategy_id": "run1-0001", "source_run_id": "run1",
        "spec": {"symbol": "USDJPY"}, "symbol": "USDJPY",
        "timeframe": "H4", "direction": "both",
        "metrics_is": {"sharpe": 0.5}, "metrics_oos": {"sharpe": 0.4},
        "metrics_all": {"sharpe": {"median": 0.45, "p05": 0.1}},
        "score": 0.6,
    }
    s.add_bank_strategy("bank_x", row)
    banks = s.list_banks()
    assert len(banks) == 1
    assert banks[0]["bank_id"] == "bank_x"
    assert banks[0]["count"] == 1
    got = s.get_bank("bank_x")
    assert got and got["config"]["confidence"] == 95
    rows = s.list_bank_strategies("bank_x")
    assert len(rows) == 1
    assert rows[0]["metrics_all"]["sharpe"]["median"] == 0.45
    one = s.get_bank_strategy("bank_x", "run1-0001")
    assert one and one["spec"]["symbol"] == "USDJPY"


def test_stress_roundtrip(tmp_path):
    from sqx.store import Store

    s = Store(tmp_path / "stress.duckdb")
    s.create_run("run1", "source", {}, 1)
    s.create_stress("stress_x", "Pipeline", "run1", {"seed": 7})
    s.update_stress("stress_x", status="running")
    s.insert_stress_result({
        "stress_id": "stress_x",
        "strategy_id": "run1-0001",
        "survival": True,
        "stages": {"parameter_sensitivity": {"status": "passed"}},
        "tests_requested": 100,
        "tests_run": 100,
        "pass_rate": 1.0,
        "metrics": {"sharpe": 1.2},
    })
    got = s.get_stress("stress_x")
    assert got and got["config"]["seed"] == 7
    rows = s.list_stress_results("stress_x")
    assert len(rows) == 1
    assert rows[0]["survival"] is True
    assert rows[0]["stages"]["parameter_sensitivity"]["status"] == "passed"
    assert s.list_stress()[0]["stress_id"] == "stress_x"
