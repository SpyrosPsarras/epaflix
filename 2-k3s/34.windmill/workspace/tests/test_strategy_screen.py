import inspect
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "f" / "screener"))
import strategy_screen as ss

CFG = ss.settings_with_defaults(None)


def sessions(n, start=date(2025, 1, 6)):
    days, day = [], start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def make_bars(rows, ticker="TEST", start=date(2025, 1, 6)):
    """rows: (open, high, low, close, volume)."""
    o, h, l, c, v = (list(map(float, x)) for x in zip(*rows))
    return ss.Bars(ticker, sessions(len(rows), start), o, h, l, c, v)


def uptrend(n, start=100.0, slope=0.5, volume=1_000_000):
    rows = []
    for i in range(n):
        close = start + slope * i
        rows.append((close - 0.2, close + 1, close - 1, close, volume))
    return rows


def pullback_rows():
    rows = uptrend(290)
    close = rows[-1][3]
    for _ in range(6):  # quiet pullback toward EMA20
        close -= 1.6
        rows.append((close + 0.8, close + 1, close - 1, close, 700_000))
    rows.append((close, close + 2.9, close - 0.6, close + 2.8, 1_200_000))  # bullish reversal bar
    return rows


def breakout_rows():
    rows = uptrend(240, slope=0.35)
    for j in range(60):  # rising base under resistance at 190
        close = min(183 + 0.08 * j + 3.5 * math.sin(j * 0.7), 188.5)
        high = 190.0 if j in (15, 35, 52) else min(close + 1.5, 189.0)
        rows.append((close - 0.3, high, close - 1.5, close, 1_000_000))
    rows.append((188.5, 192.0, 188.3, 191.6, 3_000_000))  # breakout on volume
    return rows


def run(evaluator, rows, cfg=CFG, benchmarks=None):
    bars = make_bars(rows)
    assert ss.quality_problems(bars, bars.dates, cfg) == []
    series = ss.compute(bars, benchmarks or {}, cfg)
    return evaluator(bars, series, len(bars) - 1, cfg)


def failed(result):
    return [c["name"] for c in result["checks"] if not c["passed"]]


# ---------------------------------------------------------------- indicators


def test_sma():
    assert ss.sma([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]


def test_ema_is_seeded_with_sma_then_recursive():
    out = ss.ema([1, 2, 3, 4], 3)
    assert out[:2] == [None, None] and out[2] == 2
    assert out[3] == pytest.approx(4 * 0.5 + 2 * 0.5)


def test_atr_wilder_by_hand():
    high, low, close = [10, 11, 12, 11], [9, 9, 10, 8], [9.5, 10.5, 11, 9]
    # true ranges from bar 1: max(2, 1.5, 0.5)=2, max(2, 1.5, 0.5)=2, max(3, 0, 3)=3
    out = ss.atr(high, low, close, 2)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(2.0) and out[3] == pytest.approx((2.0 * 1 + 3) / 2)


def test_rsi_by_hand():
    assert ss.rsi([1, 2, 3, 4], 2)[2:] == [100.0, 100.0]
    out = ss.rsi([10, 11, 10, 11, 10], 2)
    # gains 1,0,1,0 losses 0,1,0,1 -> first averages 0.5/0.5, then 0.75/0.25 (gain), then 0.375/0.625
    assert out[2] == pytest.approx(50) and out[3] == pytest.approx(75) and out[4] == pytest.approx(37.5)


def test_prior_mean_excludes_current_bar():
    assert ss.prior_mean([1, 2, 3, 100], 2) == [None, None, 1.5, 2.5]


def test_relative_strength_aligns_by_date_and_handles_gaps():
    bars = make_bars([(1, 1, 1, c, 1) for c in (10, 11, 12, 13)])
    bench = {bars.dates[0]: 100, bars.dates[1]: 100, bars.dates[3]: 110}
    line, change = ss.relative_strength(bars, bench, 2)
    assert line == [0.1, 0.11, None, pytest.approx(13 / 110)]
    assert change[2] is None and change[3] == pytest.approx((13 / 110) / 0.11 - 1)


# ---------------------------------------------------------------- no look-ahead


def test_indicators_are_causal():
    rows = pullback_rows()
    full = make_bars(rows)
    bench = {d: 100 + i for i, d in enumerate(full.dates)}
    whole = ss.compute(full, {"QQQ": bench}, CFG)
    for m in (60, 200, 280, len(rows) - 1):
        part = ss.compute(full.head(m), {"QQQ": bench}, CFG)
        for key, values in part.items():
            assert values == whole[key][:m], key


@pytest.mark.parametrize("evaluator, rows", [(ss.evaluate_pullback, pullback_rows), (ss.evaluate_breakout, breakout_rows)])
def test_signal_ignores_future_bars(evaluator, rows):
    rows = rows()
    t = len(rows) - 1
    future = [(1, 500, 0.5, 400, 9e9), (400, 401, 1, 2, 1)]
    for extra in ([], future):
        bars = make_bars(rows + extra)
        result = evaluator(bars, ss.compute(bars, {}, CFG), t, CFG)
        if not extra:
            expected = result
    assert result == expected and expected["qualified"]


def test_completed_bars_drops_todays_partial_bar():
    bars = make_bars(uptrend(5), start=date(2026, 11, 12))
    assert bars.dates[-1] == date(2026, 11, 18)
    morning = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)  # 10:00 New York
    after_close = datetime(2026, 11, 18, 21, 25, tzinfo=timezone.utc)  # 16:25 New York
    just_before = datetime(2026, 11, 18, 21, 15, tzinfo=timezone.utc)  # 16:15 New York
    assert ss.completed_bars(bars, morning).dates[-1] == date(2026, 11, 17)
    assert ss.completed_bars(bars, just_before).dates[-1] == date(2026, 11, 17)
    assert ss.completed_bars(bars, after_close) is bars
    assert ss.completed_bars(bars, datetime(2026, 11, 21, 12, tzinfo=timezone.utc)) is bars


@pytest.mark.parametrize("utc, hour", [
    (datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc), 1),  # before DST starts
    (datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc), 3),  # DST starts 02:00 -> 03:00
    (datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc), 16),
    (datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc), 1),
    (datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc), 1),  # DST ends 02:00 -> 01:00
    (datetime(2026, 12, 1, 21, 0, tzinfo=timezone.utc), 16),
])
def test_new_york_time(utc, hour):
    assert ss.new_york_time(utc).hour == hour


# ---------------------------------------------------------------- data quality


def test_quality_accepts_clean_data():
    bars = make_bars(uptrend(300))
    assert ss.quality_problems(bars, bars.dates, CFG) == []


def test_quality_rejects_short_history():
    assert ss.quality_problems(make_bars(uptrend(100)), [], CFG) == ["only 100 daily bars, need 260"]


@pytest.mark.parametrize("index, row, message", [
    (10, (1, 2, 0.5, math.nan, 5), "missing or invalid"),
    (150, (100, 99, 98, 100, 5), "inconsistent high/low"),
    (200, (0, 1, 0, 1, 5), "missing or invalid"),
])
def test_quality_rejects_bad_bars_anywhere(index, row, message):
    rows = uptrend(300)
    rows[index] = row
    bars = make_bars(rows)
    assert message in ss.quality_problems(bars, bars.dates, CFG)[0]


def test_quality_rejects_stale_and_gappy_data():
    bars = make_bars(uptrend(300))
    calendar = bars.dates + sessions(2, bars.dates[-1] + timedelta(days=1))
    assert "older than the last session" in ss.quality_problems(bars, calendar, CFG)[0]
    gappy = make_bars(uptrend(300))
    for i in (-10, -20, -30):
        del gappy.dates[i], gappy.open[i], gappy.high[i], gappy.low[i], gappy.close[i], gappy.volume[i]
    assert "3 sessions missing" in ss.quality_problems(gappy, bars.dates, CFG)[0]


def test_quality_rejects_zero_volume_days():
    rows = uptrend(300)
    for i in (-1, -5, -9):
        rows[i] = rows[i][:4] + (0,)
    bars = make_bars(rows)
    assert ss.quality_problems(bars, bars.dates, CFG) == ["3 days without volume in the last 50"]


# ---------------------------------------------------------------- fetching


def chart(ticker="AAPL", n=3):
    stamps = [int(datetime(2026, 11, 16 + i, 14, tzinfo=timezone.utc).timestamp()) for i in range(n)]
    return {"ticker": ticker, "date": stamps, "open": [1.0] * n, "high": [2.0] * n, "low": [0.5] * n, "close": [1.5] * n, "volume": [10] * n}


def test_parse_chart():
    bars = ss.parse_chart(chart(), "AAPL")
    assert bars.dates == [date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)] and bars.close == [1.5] * 3


@pytest.mark.parametrize("change, message", [
    ({"ticker": "MSFT"}, "another ticker"),
    ({"close": [1.0]}, "uneven"),
    ({"date": [3, 2, 1]}, "strictly increasing"),
])
def test_parse_chart_rejects_bad_payloads(change, message):
    with pytest.raises(ValueError, match=message):
        ss.parse_chart({**chart(), **change}, "AAPL")


def test_fetch_bars_retries_rate_limits(monkeypatch):
    calls, sleeps = [], []

    class Response:
        def __init__(self, status):
            self.status_code = status

        def raise_for_status(self):
            assert self.status_code == 200

        def json(self):
            return chart()

    def get(url, params, headers, timeout):
        calls.append(params)
        return Response(429 if len(calls) < 3 else 200)

    monkeypatch.setattr(ss.requests, "get", get)
    monkeypatch.setattr(ss.time, "sleep", sleeps.append)
    assert len(ss.fetch_bars("AAPL", 300)) == 3
    assert sleeps == [1, 2] and calls[0] == {"instrument": "stock", "ticker": "AAPL", "timeframe": "d", "barsCount": 300}


# ---------------------------------------------------------------- strategies


def test_pullback_qualifies_with_levels_and_reasons():
    result = run(ss.evaluate_pullback, pullback_rows())
    assert failed(result) == [] and result["qualified"]
    levels = result["levels"]
    assert levels["entry"] > levels["stop"] and levels["target"] > levels["entry"]
    assert levels["reward_risk"] >= CFG["pullback_min_rr"]
    assert {c["name"] for c in result["checks"]} >= {"uptrend", "ema50_rising", "pullback_depth", "ema_touch", "holds_ema50", "reversal_bar"}
    assert all(c["detail"] for c in result["checks"])
    assert 0 <= result["score"] <= 100 and sum(s["points"] for s in result["scoring"]) == 100


def test_pullback_without_reversal_bar_is_rejected():
    rows = pullback_rows()
    o, h, l, c, v = rows[-1]
    rows[-1] = (c, c + 0.2, c - 3, c - 2.5, v)  # closes down
    result = run(ss.evaluate_pullback, rows)
    assert not result["qualified"] and "reversal_bar" in failed(result) and result["score"] is None


def test_pullback_needs_an_uptrend():
    rows = [(200 - 0.4 * i + 0.2, 201 - 0.4 * i, 199 - 0.4 * i, 200 - 0.4 * i, 1e6) for i in range(300)]
    assert {"uptrend", "ema50_rising"} <= set(failed(run(ss.evaluate_pullback, rows)))


def test_breakout_qualifies_with_levels_and_reasons():
    result = run(ss.evaluate_breakout, breakout_rows())
    assert failed(result) == [] and result["qualified"]
    levels = result["levels"]
    assert levels["resistance"] == 190 and levels["entry"] == 191.6 and levels["stop"] < 188.3
    assert levels["reward_risk"] >= CFG["breakout_min_rr"]
    assert result["indicators"]["rel_volume"] >= 1.5
    assert sum(s["points"] for s in result["scoring"]) == 100


def test_breakout_without_volume_is_rejected():
    rows = breakout_rows()
    rows[-1] = rows[-1][:4] + (1_000_000,)
    assert failed(run(ss.evaluate_breakout, rows)) == ["volume"]


def test_breakout_must_be_fresh():
    rows = breakout_rows()
    rows.append((191.6, 193, 191, 192.5, 3_000_000))  # second day above resistance
    assert "fresh_breakout" in failed(run(ss.evaluate_breakout, rows))


def test_breakout_below_resistance_is_rejected():
    rows = breakout_rows()
    rows[-1] = (188.5, 189.5, 188.3, 189.2, 3_000_000)
    assert "fresh_breakout" in failed(run(ss.evaluate_breakout, rows))


def test_resistance_counts_separate_tests():
    rows = [(1, h, 0.5, 1, 1) for h in (5, 10, 9.8, 5, 5, 9.9, 5, 10, 5, 5)]
    level, age, touches, low = ss.resistance(make_bars(rows), 10, 10, 0.5)
    assert (level, age, touches, low) == (10, 3, 3, 0.5)


def test_relative_strength_scoring_uses_benchmarks():
    rows = pullback_rows()
    dates = sessions(len(rows))
    flat = {d: 100.0 for d in dates}
    soaring = {d: 100.0 * 1.02 ** i for i, d in enumerate(dates)}
    scores = {s["name"]: s["earned"] for s in run(ss.evaluate_pullback, rows, benchmarks={"QQQ": flat, "SMH": soaring})["scoring"]}
    assert scores["outperforms_QQQ"] == 20 and scores["outperforms_SMH"] == 0


# ---------------------------------------------------------------- validation


def signal(strategy, entry, stop, target):
    return {"strategy": strategy, "levels": {"entry": entry, "stop": stop, "target": target}}


def future(rows):
    return make_bars([(10, 10.5, 9.5, 10, 1)] + rows)


def test_simulate_pullback_not_filled():
    assert ss.simulate(future([(10, 10.4, 9.8, 10, 1)] * 3), 0, signal("pullback", 10.6, 9, 12), 2)["outcome"] == "not_filled"


def test_simulate_stop_wins_a_tie():
    out = ss.simulate(future([(10, 13, 8, 10, 1)] * 3), 0, signal("breakout", 0, 9, 12), 2)
    assert out["outcome"] == "stop" and out["r"] == -1.0


def test_simulate_target_and_gap_through():
    out = ss.simulate(future([(10, 10.5, 9.6, 10.2, 1), (12.5, 13, 12.4, 12.8, 1), (1, 1, 1, 1, 1)]), 0, signal("breakout", 0, 9, 12), 3)
    assert out["outcome"] == "target" and out["exit"] == 12.5 and out["r"] == 2.5 and out["bars_held"] == 2


def test_simulate_timeout_and_open():
    rows = [(10, 10.5, 9.6, 10.3, 1)] * 3
    assert ss.simulate(future(rows), 0, signal("breakout", 0, 9, 12), 3)["outcome"] == "timeout"
    assert ss.simulate(future(rows), 0, signal("breakout", 0, 9, 12), 4)["outcome"] == "open"


def test_simulate_breakout_gap_below_stop_is_skipped():
    assert ss.simulate(future([(8, 8.5, 7, 8, 1)] * 2), 0, signal("breakout", 0, 9, 12), 2)["outcome"] == "not_filled"


def test_validate_walks_forward_without_overlap():
    rows = breakout_rows() + [(192 + i, 193.5 + i, 191.5 + i, 193 + i, 1_000_000) for i in range(25)]
    bars = make_bars(rows)
    trades = ss.validate(bars, ss.compute(bars, {}, CFG), ["breakout"], 40, CFG)
    assert [t["signal_date"] for t in trades] == [bars.dates[300].isoformat()]
    assert trades[0]["outcome"] == "target" and trades[0]["fill"] == 192
    summary = ss.summarize(trades)
    assert summary["closed"] == 1 and summary["win_rate"] == 1 and summary["average_r"] > 0


def test_summarize_empty():
    assert ss.summarize([])["win_rate"] is None


# ---------------------------------------------------------------- inputs and main


def test_settings_overrides_and_validation():
    assert ss.settings_with_defaults({"min_rel_volume": 2, "benchmarks": ["QQQ"]})["min_rel_volume"] == 2
    assert ss.settings_with_defaults({"min_bars": 10})["min_bars"] == 253
    for bad, message in [({"nope": 1}, "unknown setting"), ({"benchmarks": "QQQ"}, "benchmarks"), ({"max_risk_atr": -1}, "0 or more"),
                         ({"min_rel_volume": True}, "number"), ({"breakout_lookback": 0}, "1 or more")]:
        with pytest.raises(ValueError, match=message):
            ss.settings_with_defaults(bad)
    assert ss.DEFAULTS["benchmarks"] == ["QQQ", "SMH"]


def test_discover_tickers_and_finviz(monkeypatch):
    assert ss.discover("", "aapl, msft,AAPL", 5) == (["AAPL", "MSFT"], "tickers")
    with pytest.raises(ValueError, match="not tickers"):
        ss.discover("", "AAPL,rm -rf", 5)
    with pytest.raises(ValueError, match="Finviz"):
        ss.discover("https://evil.example/screener.ashx", "", 5)
    seen = {}

    def screen(url, rows=None):
        seen["rows"] = rows
        return iter([{"Ticker": "AAA"}, {"Ticker": "BBB"}])

    monkeypatch.setattr(ss.Screener, "init_from_url", screen)
    assert ss.discover("https://finviz.com/screener.ashx?v=171", "", 2) == (["AAA", "BBB"], "finviz") and seen["rows"] == 2


@pytest.fixture
def offline(monkeypatch):
    rows = {"PULL": pullback_rows(), "BRK": breakout_rows(), "QQQ": uptrend(301, 300, 0.1), "SMH": uptrend(301, 200, 0.1)}
    rows["PULL"] = uptrend(4) + rows["PULL"]  # same length as the others
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)

    def fetch(ticker, count):
        if ticker not in rows:
            raise ValueError("chart data is for another ticker")
        return make_bars(rows[ticker], ticker)

    monkeypatch.setattr(ss, "fetch_bars", fetch)
    return rows


def test_main_returns_structured_results(offline, capsys):
    result = ss.main(tickers="PULL,BRK,NOPE")
    assert [q["ticker"] for q in result["results"]["pullback"]["qualified"]] == ["PULL"]
    assert [q["ticker"] for q in result["results"]["breakout"]["qualified"]] == ["BRK"]
    assert {r["ticker"] for r in result["results"]["pullback"]["rejected"]} == {"BRK"}
    assert all(r["failed"] for r in result["results"]["breakout"]["rejected"])
    assert result["data_errors"] == [{"ticker": "NOPE", "reason": "chart data is for another ticker"}]
    assert set(result["benchmarks"]) == {"QQQ", "SMH"} and result["validation"] is None
    assert result["as_of"] == sessions(301)[-1].isoformat()
    assert "Summary: 3 tickers, 1 data errors, qualified: pullback 1, breakout 1" in capsys.readouterr().out


def test_main_single_strategy_and_validation(offline):
    result = ss.main(tickers="BRK", strategy="breakout", validation_days=20)
    assert list(result["results"]) == ["breakout"] and set(result["validation"]["summary"]) == {"breakout"}


def test_main_without_benchmarks_fails(offline, monkeypatch):
    with pytest.raises(RuntimeError, match="benchmark"):
        ss.main(tickers="PULL", settings={"benchmarks": ["NOPE"]})


@pytest.mark.parametrize("kwargs, message", [
    ({}, "Finviz screener URL or"),
    ({"tickers": "AAPL", "strategy": "scalp"}, "strategy"),
    ({"tickers": "AAPL", "max_tickers": 0}, "max_tickers"),
    ({"tickers": "AAPL", "validation_days": 5000}, "validation_days"),
])
def test_main_rejects_bad_inputs(offline, kwargs, message):
    with pytest.raises(ValueError, match=message):
        ss.main(**kwargs)


def test_never_sends_email_or_touches_resources():
    source = Path(ss.__file__).read_text()
    assert "smtplib" not in source and "wmill" not in source


def test_requirements_header_is_pinned_for_ci():
    script = Path(ss.__file__).read_text().splitlines()
    header = {line.lstrip("#") for line in script[1:script.index("")]}
    pins = set((Path(__file__).resolve().parents[1] / "requirements-test.txt").read_text().split())
    assert header and header <= pins


def test_script_yaml_matches_main_signature():
    text = Path(ss.__file__).with_name("strategy_screen.script.yaml").read_text()
    order = text.split("order:\n")[1].split("  properties:")[0].split()
    assert [x for x in order if x != "-"] == list(inspect.signature(ss.main).parameters)
