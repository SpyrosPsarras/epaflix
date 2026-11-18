import csv
import json
import math
import random
import sys
from array import array
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "f" / "screener"))
sys.path.insert(0, str(ROOT / "research"))
import strategy_research as sr  # noqa: E402
import strategy_screen as ss  # noqa: E402


def sessions(n, start=date(2011, 1, 3)):
    days, day = [], start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def walk(ticker, n, seed, drift=0.0007, vol=0.016, start=date(2011, 1, 3)):
    """Seeded random walk with gaps, ranges and noisy volume."""
    rng = random.Random(seed)
    o, h, l, c, v = [], [], [], [], []
    close = 50.0
    for _ in range(n):
        opening = close * (1 + rng.gauss(0, vol / 3))
        close = max(1.0, opening * (1 + rng.gauss(drift, vol)))
        o.append(opening)
        c.append(close)
        h.append(max(opening, close) * (1 + abs(rng.gauss(0, vol / 2))))
        l.append(min(opening, close) * (1 - abs(rng.gauss(0, vol / 2))))
        v.append(1e6 * math.exp(rng.gauss(0, 0.45)))
    return ss.Bars(ticker, sessions(n, start), o, h, l, c, v)


def flat_bars(rows, start=date(2020, 1, 6)):
    o, h, l, c = (list(map(float, x)) for x in zip(*rows))
    return ss.Bars("T", sessions(len(rows), start), o, h, l, c, [1e6] * len(rows))


def market_bars(n):
    bars = {"QQQ": walk("QQQ", n, 1, drift=0.0005, vol=0.01)}
    for k, etf in enumerate(sr.SECTOR_ETFS):
        bars[etf] = walk(etf, n, 100 + k, drift=0.0004, vol=0.01)
    return bars


def make_study(stocks, n=1400):
    bars = {**market_bars(n), **stocks}
    return sr.Study(sr.MemoryStore(bars), {t: {"sector": "Technology"} for t in stocks})


def qqq_map(n=1400):
    q = sr.ordinal_bars(walk("QQQ", n, 1, drift=0.0005, vol=0.01))
    return {"QQQ": dict(zip(q.dates, q.close))}


# ---------------------------------------------------------------- indicators and look-ahead


def test_rolling_max_before_matches_brute_force():
    rng = random.Random(3)
    values = [rng.random() for _ in range(300)]
    for n in (1, 5, 20):
        out = sr.rolling_max_before(values, n)
        assert out[:n] == [None] * n
        assert all(out[i] == max(values[i - n:i]) for i in range(n, len(values)))


def test_momentum_matches_definition_and_ignores_later_bars():
    close = walk("X", 400, 5).close
    mom, vadj = sr.momentum(close)
    assert mom[125] is None and mom[126] == pytest.approx(close[105] / close[0] - 1)
    rets = [math.log(close[i] / close[i - 1]) for i in range(200 - 125, 201)]
    vol = math.sqrt(sum((r - sum(rets) / 126) ** 2 for r in rets) / 125 * 252)
    assert vadj[200] == pytest.approx(mom[200] / vol, rel=1e-6)
    changed = close[:300] + [x * 3 for x in close[300:]]
    mom2, vadj2 = sr.momentum(changed)
    assert mom2[:300] == mom[:300]
    assert all(a == pytest.approx(b, rel=1e-9) for a, b in zip(vadj2[126:300], vadj[126:300]))


def test_candidates_do_not_change_when_later_bars_change():
    stock = sr.ordinal_bars(walk("AAA", 1400, 11))
    full = sr.ticker_features(stock, qqq_map())
    cut = 900
    altered = ss.Bars("AAA", stock.dates, stock.open[:cut] + [x * 0.5 for x in stock.open[cut:]],
                      stock.high[:cut] + [x * 0.5 for x in stock.high[cut:]], stock.low[:cut] + [x * 0.5 for x in stock.low[cut:]],
                      stock.close[:cut] + [x * 0.5 for x in stock.close[cut:]], stock.volume)
    partial = sr.ticker_features(altered, qqq_map())
    for k in (0, 1):
        before = [c for c in full[k] if c["t"] < cut]
        assert before and before == [c for c in partial[k] if c["t"] < cut]
    assert [t for t in full[2] if t < cut] == [t for t in partial[2] if t < cut]
    assert [x for x in full[3] if x[0] < stock.dates[cut]] == [x for x in partial[3] if x[0] < stock.dates[cut]]


def test_ranks_use_only_the_same_date():
    d1, d2, d3 = 737000, 737001, 737002
    rows = {d1: (array("f", range(30)), array("f", range(30))), d2: (array("f", (100.0 + i for i in range(30))), array("f", [0.0] * 30))}
    cands = sr.Columns("pullback")
    blank = {k: 0 for k in sr.COLUMNS["pullback"]}
    for d, m in ((d1, 21.0), (d2, 21.0), (d3, 1.0)):
        cands.append({**blank, "day": d, "mom": m, "vadj": m})
    sr.rank_percentiles(rows, cands)
    assert cands.data["mom_pct"][0] == pytest.approx(0.7)
    assert cands.data["mom_pct"][1] == 0.0
    assert math.isnan(cands.data["mom_pct"][2])


# ---------------------------------------------------------------- equivalence with the production strategy rules


def test_baseline_candidates_equal_strategy_screen_signals():
    stocks = {f"S{k}": walk(f"S{k}", 1400, 40 + k, drift=0.0009) for k in range(4)}
    study = make_study(stocks)
    study.prepare(log=None)
    qqq = market_bars(1400)["QQQ"]
    cfg = ss.settings_with_defaults({"benchmarks": ["QQQ"]})
    found = 0
    for strategy, evaluator in ss.EVALUATORS.items():
        cands, baseline = study.candidates[strategy], sr.BASELINE[strategy]
        got = {(study.stocks[c["ticker"]], c["t"]) for c in map(cands.row, range(len(cands)))
               if sr.passes(strategy, c, baseline, "other") and sr.plan(strategy, c, baseline) is not None}
        expected = set()
        for ticker, bars in stocks.items():
            series = ss.compute(bars, {"QQQ": dict(zip(qqq.dates, qqq.close))}, cfg)
            for t in range(253, len(bars) - 1):
                if evaluator(bars, series, t, cfg)["qualified"]:
                    expected.add((ticker, t))
        assert got == expected, strategy
        found += len(expected)
    assert found >= 10


# ---------------------------------------------------------------- exits


def test_simulate_exit_matches_strategy_screen_for_fixed_targets():
    bars = walk("X", 600, 9)
    compared = 0
    for t in range(260, 560, 3):
        entry = bars.high[t] + 0.01
        for strategy in ("pullback", "breakout"):
            e = entry if strategy == "pullback" else bars.close[t]
            stop, target = e - 1.5, e + 3.0
            signal = {"strategy": strategy, "levels": {"entry": e, "stop": stop, "target": target}, "indicators": {"atr14": 1.0}}
            expected = ss.simulate(bars, t, signal, 20)
            got = sr.simulate_exit(bars, t, "stop_buy" if strategy == "pullback" else "next_open", e, stop, target, 20, None, 1.0)
            if expected["outcome"] == "not_filled":
                assert got == "not_filled"
                continue
            assert got["r"] == pytest.approx(expected["r"], abs=1e-3) and got["outcome"] == expected["outcome"]
            assert got["held"] == expected["bars_held"]
            compared += 1
    assert compared > 50


def test_open_above_target_exits_at_the_open():
    bars = flat_bars([(10, 10, 10, 10)] * 3 + [(10.2, 10.5, 10.1, 10.4), (12.5, 12.8, 12.0, 12.6)] + [(12, 12, 12, 12)] * 30)
    result = sr.simulate_exit(bars, 2, "next_open", 10, 9, 12, 20)
    assert result["outcome"] == "target" and result["gap"] and result["exit_price"] == 12.5 and result["r"] == pytest.approx((12.5 - 10.2) / 1.2)


def test_gap_below_stop_exits_at_the_open_and_not_filled_cases():
    rows = [(10, 10, 10, 10)] * 3 + [(10.2, 10.5, 10.1, 10.4), (8.0, 8.5, 7.5, 8.2)] + [(8, 8, 8, 8)] * 30
    result = sr.simulate_exit(flat_bars(rows), 2, "next_open", 10, 9, None, 20)
    assert result["outcome"] == "stop" and result["gap"] and result["exit_price"] == 8.0
    assert sr.simulate_exit(flat_bars(rows), 2, "stop_buy", 10.6, 9, None, 20) == "not_filled"
    assert sr.simulate_exit(flat_bars(rows), 2, "next_open", 10, 10.3, None, 20) == "not_filled"
    assert sr.simulate_exit(flat_bars(rows), 2, "next_open", 10, 9, None, 40) is None


def test_trailing_stop_moves_at_the_close_and_applies_from_the_next_bar():
    rows = [(10, 10, 10, 10)] * 3 + [
        (10.0, 10.2, 9.9, 10.0),   # t = 3, signal
        (10.0, 12.1, 9.95, 12.0),  # fill at 10, close 12 -> trail 12 - 1 = 11, from the next bar
        (11.9, 12.0, 11.2, 11.5),  # low 11.2 above 11: still open
        (11.4, 11.5, 10.8, 11.0),  # low 10.8 <= 11: trailing exit at 11
    ] + [(11, 11, 11, 11)] * 30
    result = sr.simulate_exit(flat_bars(rows), 3, "next_open", 10, 9, None, 20, trail_atr=1.0, atr=1.0)
    assert result["outcome"] == "trail" and result["exit_price"] == 11.0 and result["held"] == 3 and result["r"] == pytest.approx(1.0)
    # The same bar that sets a higher trail cannot also trigger it.
    rows[4] = (10.0, 12.1, 9.95, 12.0)
    same_bar = sr.simulate_exit(flat_bars(rows[:5] + [(12, 12.2, 11.9, 12.1)] * 30), 3, "next_open", 10, 9, None, 5, 1.0, 1.0)
    assert same_bar["outcome"] == "timeout"


def test_plan_applies_risk_and_structural_reward_checks():
    cand = {"atr": 1.0, "entry": 10.0, "low3": 9.0, "swing_high": 11.0}
    assert sr.plan("pullback", cand, {"stop_atr": 0.2, "exit": "struct"}) is None  # reward/risk 0.83 < 1.5
    assert sr.plan("pullback", cand, {"stop_atr": 0.2, "exit": "2R"})[3] == pytest.approx(12.4)
    assert sr.plan("pullback", {**cand, "low3": 7.0}, {"stop_atr": 0.2, "exit": "2R"}) is None  # risk 3.2 ATR > 2.5
    kind, entry, stop, target, trail = sr.plan("breakout", {"atr": 1.0, "entry": 20.0, "level": 19.5, "low_t": 19.0, "base_low": 15.0},
                                               {"stop_atr": 0.5, "exit": "trail3"})
    assert (kind, stop, target, trail) == ("next_open", 18.5, None, 3.0)


# ---------------------------------------------------------------- configurations and selection


def test_neighbours_step_one_numeric_dimension():
    cfg = sr.BASELINE["pullback"]
    found = list(sr.neighbours("pullback", cfg))
    assert len(found) == 7  # touch 2, depth 2, stop 1 (0.2 is the first value), horizon 2
    assert all(sum(cfg[k] != n[k] for k in cfg) == 1 for n in found)
    assert all(n["exit"] == cfg["exit"] and n["regime"] == cfg["regime"] for n in found)


def test_grid_contains_baselines_and_ids_are_unique():
    for strategy in sr.GRIDS:
        ids = [sr.config_id(strategy, c) for c in sr.configs(strategy)]
        assert len(ids) == len(set(ids))
        assert sr.config_id(strategy, sr.BASELINE[strategy]) in ids


def bare_study(rows, regime_context=None):
    study = object.__new__(sr.Study)
    cands = sr.Columns("pullback")
    blank = {k: 0 for k in sr.COLUMNS["pullback"]}
    for row in rows:
        cands.append({**blank, **row})
    cands.add_column("mom_pct", "d", [r.get("mom_pct", 0.0) for r in rows])
    cands.add_column("vadj_pct", "d", [r.get("vadj_pct", 0.0) for r in rows])
    study.candidates, study.masks = {"pullback": cands}, {"pullback": {}}
    study.context = regime_context or {"bull": {}, "breadth": {}}
    return study


def test_trades_keep_one_open_trade_per_ticker():
    study = bare_study([{"ticker": 0, "t": 10}, {"ticker": 0, "t": 12}, {"ticker": 0, "t": 16}, {"ticker": 1, "t": 11}, {"ticker": 1, "t": 13}])
    sims = sr.Sims()
    for r, held in ((1.0, 5), (2.0, 5), (-1.0, 5), (None, 0), (0.5, 5)):
        sims.add(None if r is None else {"r": r, "held": held, "fill": 10.0, "risk": 1.0, "outcome": "stop", "gap": False,
                                         "exit_index": 10 + held}, ss.Bars("T", list(range(40)), *[[1.0] * 40] * 5))
    trades = study.trades("pullback", sr.BASELINE["pullback"], sims)
    assert [t[7] for t in trades] == [0, 2, 4]  # 12 overlaps 10..15; ticker 1 at 11 was not filled
    assert trades[0][1] == 15 and trades[0][8] == 11  # exit day and fill day from the bars' dates


def test_masks_and_regime_filters():
    d = 737000
    study = bare_study([{"day": d, "touch": 0.2, "depth": 1.2, "rs_qqq": 0.1, "rs_sector": -0.1, "mom_pct": 0.8, "vadj_pct": 0.5}],
                       {"bull": {d: True}, "breadth": {d: 5 / 9}})
    masks = study.filter_masks("pullback")
    assert masks[("touch_atr", 0.1)] == 0 and masks[("touch_atr", 0.25)] == 1
    assert masks[("depth_atr", 1.5)] == 0 and masks[("depth_atr", 1.0)] == 1
    assert masks[("regime", "bull")] == 1 and masks[("regime", "bull_breadth")] == 0
    assert masks[("strength", "rs_qqq")] == 1 and masks[("strength", "rs_sector")] == 0
    assert masks[("strength", "mom_top30")] == 1 and masks[("strength", "vadj_top30")] == 0
    assert ("horizon", 20) not in masks


def test_market_context_bull_needs_a_rising_sma200_and_breadth():
    n = 400
    up = ss.Bars("QQQ", sessions(n), *([[100 + 0.1 * i for i in range(n)]] * 4), [1e6] * n)
    sectors = []
    for k, etf in enumerate(sr.SECTOR_ETFS):
        closes = [100 + (0.1 if k < 6 else -0.1) * i for i in range(n)]
        sectors.append(ss.Bars(etf, sessions(n), closes, closes, closes, closes, [1e6] * n))
    context = sr.market_context(up, sectors)
    last = up.dates[-1]
    assert context["bull"][last] and not context["bull"][up.dates[210]]
    assert sr.regime_of(context, last) == "bull_breadth"
    sectors[0] = ss.Bars("XLK", sessions(n), *([[100 - 0.1 * i for i in range(n)]] * 4), [1e6] * n)
    assert sr.regime_of(sr.market_context(up, sectors), last) == "bull"


def result(strategy, cfg, yearly):
    return {"strategy": strategy, "config": cfg, "id": sr.config_id(strategy, cfg), "yearly": yearly}


def test_robustness_counts_thin_neighbours_as_zero_and_selection_uses_only_its_years():
    strategy = "pullback"
    good = {y: [100, 10.0] for y in range(2011, 2019)}
    results = [result(strategy, c, {}) for c in sr.configs(strategy)]
    by_id = {r["id"]: r for r in results}
    center = sr.BASELINE[strategy]
    by_id[sr.config_id(strategy, center)]["yearly"] = good
    assert sr.robustness(strategy, by_id[sr.config_id(strategy, center)], by_id, range(2011, 2019)) == 0.0
    for n in sr.neighbours(strategy, center):
        by_id[sr.config_id(strategy, n)]["yearly"] = {y: [100, 5.0] for y in range(2011, 2019)}
    assert sr.robustness(strategy, by_id[sr.config_id(strategy, center)], by_id, range(2011, 2019)) == pytest.approx(0.05)
    future_only = {**center, "regime": "bull"}
    by_id[sr.config_id(strategy, future_only)]["yearly"] = {2019: [1000, 900.0], **{y: [100, -1.0] for y in range(2011, 2019)}}
    picked = sr.select(strategy, results, by_id, range(2011, 2019), 3)
    assert picked and picked[0][1]["id"] == sr.config_id(strategy, center)
    assert sr.config_id(strategy, future_only) not in [p[1]["id"] for p in picked]


def test_select_requires_trades_and_positive_years():
    strategy = "pullback"
    results = [result(strategy, c, {}) for c in sr.configs(strategy)]
    by_id = {r["id"]: r for r in results}
    center = sr.config_id(strategy, sr.BASELINE[strategy])
    by_id[center]["yearly"] = {y: [10, 1.0] for y in range(2011, 2019)}  # 80 trades < 200
    assert sr.select(strategy, results, by_id, range(2011, 2019), 3) == []
    by_id[center]["yearly"] = {y: [100, 50.0 if y < 2014 else -5.0] for y in range(2011, 2019)}  # 3 of 8 years positive
    assert sr.select(strategy, results, by_id, range(2011, 2019), 3) == []


def test_stats_costs_drawdown_and_concentration():
    d = date(2015, 1, 5)
    rows = [(d, d + timedelta(days=k), r, 20.0, 0, 0, k % 7) for k, r in enumerate([2.0, -1.0, -1.0, 3.0, -1.0, -1.0, 1.0, -1.0, 0.5, -1.0])]
    st = sr.stats(rows)
    assert st["trades"] == 10 and st["e0"] == pytest.approx(0.05)
    assert st["e25"] == pytest.approx(0.05 - 0.0025 * 20)
    assert st["max_dd"] == pytest.approx(2.8)  # peak +2.80 after trade 4, back to 0.00 at the end
    assert st["e25_without_top5"] is not None and st["e25_without_top5"] < st["e25"]
    assert sr.stats([]) == {"trades": 0}


def test_set_bits():
    assert sr.set_bits(0, 10) == []
    assert sr.set_bits((1 << 0) | (1 << 9) | (1 << 17), 20) == [0, 9, 17]


def test_spearman():
    assert sr.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert sr.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert sr.spearman([1, 2], [1, 2]) is None


def test_store_round_trip(tmp_path):
    bars = walk("ABC", 50, 3)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "ABC.json").write_text(json.dumps({"ticker": "ABC", "dates": [d.isoformat() for d in bars.dates], "open": bars.open,
                                                "high": bars.high, "low": bars.low, "close": bars.close, "volume": bars.volume}))
    assert sr.convert_cache(cache, tmp_path / "store") == {"ABC": 50}
    loaded = sr.Store(tmp_path / "store").bars("ABC")
    assert list(loaded.dates) == [d.toordinal() for d in bars.dates]
    assert list(loaded.close) == bars.close and list(loaded.volume) == bars.volume


def test_random_benchmark_is_matched_and_reproducible():
    stocks = {f"R{k}": walk(f"R{k}", 1400, 70 + k, drift=0.0009) for k in range(4)}
    study = make_study(stocks)
    study.prepare(log=None)
    cfg = {**sr.BASELINE["pullback"], "touch_atr": 0.5, "depth_atr": 0.5, "exit": "2R"}
    first = study.benchmark("pullback", cfg, "dev", draws=4, seed=7, chunk=2)
    assert first["trades"] > 0 and first["draws"] == 4
    assert first == study.benchmark("pullback", cfg, "dev", draws=4, seed=7, chunk=3)  # chunking does not change draws
    assert "mean_qqq_same_window_pct" in first
    study.pool = {}
    assert study.benchmark("pullback", cfg, "dev", draws=2, seed=7)["draws"] == 0


# ---------------------------------------------------------------- end to end


def test_main_runs_on_synthetic_data_and_logs_every_configuration(tmp_path, monkeypatch):
    monkeypatch.setitem(sr.GRIDS, "pullback", {"touch_atr": (0.25, 0.5), "depth_atr": (0.5, 1.0), "stop_atr": (0.2, 0.5),
                                               "exit": ("struct", "trail3"), "horizon": (10, 20), "regime": ("all", "bull"),
                                               "strength": ("none", "mom_top30")})
    monkeypatch.setitem(sr.GRIDS, "breakout", {"lookback": (20, 50), "base": ("none", "base"), "rel_volume": (1.0, 1.5),
                                               "stop_atr": (0.5,), "exit": ("struct", "2R"), "horizon": (10, 20),
                                               "regime": ("all",), "strength": ("none", "rs_qqq")})
    monkeypatch.setitem(sr.RULES, "min_trades", {"pullback": 1, "breakout": 1})
    monkeypatch.setitem(sr.RULES, "min_positive_years", 0.0)
    cache = tmp_path / "cache"
    cache.mkdir()
    n = 3300
    allbars = {"QQQ": walk("QQQ", n, 1, drift=0.0005, vol=0.01)}
    for k, etf in enumerate(sr.SECTOR_ETFS):
        allbars[etf] = walk(etf, n, 100 + k, drift=0.0004, vol=0.01)
    for k in range(6):
        allbars[f"S{k}"] = walk(f"S{k}", n, 200 + k, drift=0.0008)
    for t, b in allbars.items():
        (cache / f"{t}.json").write_text(json.dumps({"ticker": t, "dates": [d.isoformat() for d in b.dates], "open": b.open,
                                                     "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}))
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({f"S{k}": {"sector": "Technology"} for k in range(6)}))
    out = tmp_path / "out"
    report = sr.main(["--cache", str(cache), "--meta", str(meta), "--out", str(out), "--draws", "3"])
    with open(out / "configs.csv") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2 ** 7 + 2 ** 6
    assert {"dev_trades", "validation_e25"} <= set(rows[0])
    assert not any(k.startswith("holdout") for k in rows[0])  # the holdout is only reported for the shortlist
    for strategy in ("pullback", "breakout"):
        body = report["strategies"][strategy]
        assert "baseline" in body["selected"] and body["walk_forward"]["picks"]
        assert set(body["selected"]["baseline"]["periods"]) == {"dev", "validation", "holdout"}
    assert (out / "report.md").read_text().startswith("# Strategy research run")
