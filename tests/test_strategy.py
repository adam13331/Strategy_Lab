from __future__ import annotations

import random

from sqx.strategy import default_config, describe, sample_strategy, spec_hash


def test_defaults():
    cfg = default_config(["USDJPY"])
    assert cfg.timeframe == "H4"
    assert cfg.oos_pct == 40
    assert cfg.risk.sizing == "fixed_lot"
    assert cfg.risk.fixed_lot == 0.10
    assert cfg.autocorrelation.enabled is True
    assert cfg.autocorrelation.threshold == 0.95
    assert cfg.target_accepted == 200
    assert len(cfg.indicators) == 53
    assert all(v.enabled for v in cfg.indicators.values())


def test_oos_validation():
    cfg = default_config(["USDJPY"])
    cfg.oos_pct = 40
    assert cfg.oos_pct == 40


def test_sampling_is_seeded():
    cfg = default_config(["USDJPY"])
    cfg.direction = "both"
    a = sample_strategy(random.Random(11), cfg)
    b = sample_strategy(random.Random(11), cfg)
    c = sample_strategy(random.Random(12), cfg)
    assert a == b
    assert spec_hash(a) == spec_hash(b)
    assert spec_hash(a) != spec_hash(c)


def test_spec_shape_and_describe():
    cfg = default_config(["USDJPY"])
    spec = sample_strategy(random.Random(3), cfg, "USDJPY")
    assert spec["symbol"] == "USDJPY"
    assert spec["entry"]["long"] or spec["entry"]["short"]
    assert isinstance(spec["exit"]["conditions"], list)
    assert spec["exit"]["stop_loss"]["mode"] in ("none", "stop_loss")
    assert spec["risk"]["balance"] > 0

    rules = describe(spec)
    assert set(rules) >= {"long", "short", "exits", "stop_loss", "take_profit",
                          "trailing", "opposite_signal", "max_bars"}
    assert all(isinstance(x, str) for x in rules["long"])


def test_guaranteed_exit_path():
    cfg = default_config(["USDJPY"])
    cfg.max_exit_indicators = 0
    cfg.opposite_signal = "ignore"
    cfg.stop_loss.mode = "never"
    cfg.take_profit.mode = "never"
    for seed in range(30):
        spec = sample_strategy(random.Random(seed), cfg)
        ex = spec["exit"]
        assert ex["stop_loss"]["mode"] != "none" or ex["take_profit"]["mode"] != "none"


def test_default_orders_allow_market_limit_and_stop():
    cfg = default_config(["USDJPY"])
    assert cfg.orders.types == ["market", "limit", "stop"]
    seen = set()
    for seed in range(30):
        spec = sample_strategy(random.Random(seed), cfg, "USDJPY")
        orders = spec["execution"]["orders"]
        assert 1 <= len(orders) <= cfg.orders.max_orders
        seen.update(o["type"] for o in orders)
        assert isinstance(describe(spec)["orders"], str)
    assert seen == {"market", "limit", "stop"}


def test_pending_order_sampling_multiple_orders():
    cfg = default_config(["USDJPY"])
    cfg.orders.types = ["limit", "stop"]
    cfg.orders.max_orders = 3
    cfg.orders.distance_type = "fixed"
    for seed in range(20):
        spec = sample_strategy(random.Random(seed), cfg, "USDJPY")
        orders = spec["execution"]["orders"]
        assert 1 <= len(orders) <= 3
        assert all(o["type"] in ("limit", "stop") for o in orders)
        assert all(o["pips"] > 0 for o in orders)
        assert spec["execution"]["expiry_bars"] >= 1


def test_market_does_not_mix_with_pending_orders():
    cfg = default_config(["USDJPY"])
    cfg.orders.types = ["market", "limit", "stop"]
    cfg.orders.max_orders = 3
    for seed in range(40):
        spec = sample_strategy(random.Random(seed), cfg, "USDJPY")
        types = [o["type"] for o in spec["execution"]["orders"]]
        assert not ("market" in types and len(types) > 1)


def test_htf_indicator_timeframes_are_sampled_as_choices():
    cfg = default_config(["USDJPY"])
    for ind_id in cfg.indicators:
        cfg.indicators[ind_id].enabled = ind_id in ("iHigh", "iLow",
                                                    "iDailyHigh", "iMonthlyLow")
    cfg.max_entry_indicators = 2
    for seed in range(15):
        spec = sample_strategy(random.Random(seed), cfg, "USDJPY")
        for side in ("long", "short"):
            for c in spec["entry"][side]:
                assert isinstance(c["params"], dict)
                if c["indicator"] in ("iHigh", "iLow"):
                    assert c["params"]["tf"] in ("H1", "H4", "D1", "W1", "MN1")
                else:
                    assert isinstance(c["params"]["offset"], int)
        assert isinstance(describe(spec)["orders"], str)
