import glob
import inspect
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "f" / "screener"))
import strategy_screen as ss

CFG = ss.settings_with_defaults(None)
CFG20 = ss.settings_with_defaults({"swing_min_days": 20, "swing_max_days": 20})  # short fixtures: a fixed 20-session hold


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


def downtrend(n, start=300.0, slope=-0.5, volume=1_000_000):
    rows = []
    for i in range(n):
        close = start + slope * i
        rows.append((close + 0.2, close + 1, close - 1, close, volume))
    return rows


def rally_rows(steps=(1.1, 0.9, 1.0, 1.2)):
    rows = downtrend(290)
    close = rows[-1][3]
    for _ in range(3):  # leg down to the rally low
        close -= 1.5
        rows.append((close + 1.2, close + 1.4, close - 0.6, close, 1_200_000))
    for step in steps:  # quiet rally back toward EMA20
        close += step
        rows.append((close - 0.7, close + 0.6, close - 0.9, close, 700_000))
    rows.append((close + 0.5, close + 2.6, close - 1.8, close - 1.6, 1_300_000))  # rejection bar
    return rows


def breakdown_rows(last=(201.5, 201.7, 198.0, 198.4, 3_000_000)):
    rows = downtrend(240, 330, -0.5)
    for j in range(60):  # base above support at 200
        close = max(207 - 0.05 * j - 3.5 * math.sin(j * 0.7), 201.5)
        low = 200.0 if j in (15, 35, 52) else max(close - 1.5, 201.0)
        rows.append((close + 0.3, close + 1.5, low, close, 1_000_000))
    rows.append(last)  # breakdown on volume
    return rows


def squeeze_rows(last=(183.0, 187.0, 182.6, 186.4, 3_500_000)):
    rows = downtrend(240, 300, -0.5)
    for j in range(60):  # base around 181 after a long fall
        close = 181 + 1.5 * math.sin(j * 0.6)
        rows.append((close - 0.2, close + 1.0, close - 1.0, close, 1_000_000))
    rows.append(last)  # gap up, breakout above the 20-day high on 3.5x volume
    return rows


def run(evaluator, rows, cfg=CFG, benchmarks=None, sector_etf=None):
    bars = make_bars(rows)
    assert ss.quality_problems(bars, bars.dates, cfg) == []
    series = ss.compute(bars, benchmarks or {}, cfg)
    series["sector_etf"] = sector_etf
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


def _with_beta(evaluator, rows, beta, cfg=CFG):
    bars = make_bars(rows)
    series = ss.compute(bars, {}, cfg)
    series["sector_etf"] = None
    series["beta"] = [beta] * len(bars)
    return evaluator(bars, series, len(bars) - 1, cfg)


@pytest.mark.parametrize("evaluator,rows", [(ss.evaluate_pullback, pullback_rows), (ss.evaluate_breakout, breakout_rows)])
def test_high_beta_widens_the_long_stop(evaluator, rows):
    base = _with_beta(evaluator, rows(), 1.0)
    wide = _with_beta(evaluator, rows(), 2.0)
    atr14 = wide["indicators"]["atr14"]
    assert base["qualified"] and wide["qualified"] and "base_stop" not in base["levels"]
    b, w = base["levels"], wide["levels"]
    assert w["entry"] == b["entry"] and w["target"] == b["target"] and w["base_stop"] == b["stop"]
    assert w["stop_buffer_atr"] == 1.0 and w["stop"] == round(b["stop"] - 1.0 * atr14, 2)
    assert w["risk_per_share"] > b["risk_per_share"] and w["reward_risk"] < b["reward_risk"]
    assert [c["name"] for c in wide["checks"]] == [c["name"] for c in base["checks"]]  # qualification uses the rule's stop
    off = _with_beta(evaluator, rows(), 2.0, ss.settings_with_defaults({"high_beta_stop_atr": 0}))
    assert off["levels"] == b
    assert _with_beta(evaluator, rows(), 1.29)["levels"] == b and _with_beta(evaluator, rows(), None)["levels"] == b


def test_market_context_uses_bars_up_to_each_date():
    market = make_bars(uptrend(120, 100, 1.0), "QQQ")
    sector = make_bars(uptrend(400, 50, 0.5), "XLK", start=date(2024, 3, 25))
    dates = market.dates
    ctx = ss.market_context(dates, market, sector)
    assert ctx["market_change_21"][20] is None and ctx["market_change_21"][119] == pytest.approx(219 / 198 - 1)
    assert ctx["market_above_ema50"][119] is True and ctx["sector_uptrend"][119] is True
    j = sector.dates.index(dates[119])
    assert ctx["sector_vs_market_63"][119] == pytest.approx(sector.close[j] / sector.close[j - 63] - 219 / 156)
    early = ss.market_context(dates[:60], market.head(60), sector)
    assert all(early[k] == ctx[k][:60] for k in ctx)  # no look-ahead
    assert ss.market_context(dates, None) == {k: [None] * len(dates) for k in ctx}


def test_breakout_scores_market_and_volume():
    rows = breakout_rows()
    bars = make_bars(rows)
    series = ss.compute(bars, {}, CFG)
    series["sector_etf"] = "XLK"
    n = len(bars)
    series.update({"market_above_ema50": [True] * n, "market_change_21": [0.01] * n, "sector_uptrend": [True] * n,
                   "sector_vs_market_63": [0.02] * n})
    result = ss.evaluate_breakout(bars, series, n - 1, CFG)
    earned = {x["name"]: x["earned"] for x in result["scoring"]}
    assert sum(x["points"] for x in result["scoring"]) == 100
    assert earned["market_above_ema50"] == 13 and earned["market_not_hot"] == 9 and earned["sector_uptrend"] == 8
    assert earned["strong_volume"] == 12 and earned["strong_close"] == 15  # 3.0x volume, close at the top of the range
    hot = {**series, "market_change_21": [0.05] * n, "market_above_ema50": [False] * n}
    earned = {x["name"]: x["earned"] for x in ss.evaluate_breakout(bars, hot, n - 1, CFG)["scoring"]}
    assert earned["market_not_hot"] == 0 and earned["market_above_ema50"] == 0


def test_pullback_scores_beta_and_range():
    high = _with_beta(ss.evaluate_pullback, pullback_rows(), 1.25)
    low = _with_beta(ss.evaluate_pullback, pullback_rows(), 1.0)
    assert {x["name"]: x["earned"] for x in high["scoring"]}["high_beta"] == 30
    assert {x["name"]: x["earned"] for x in low["scoring"]}["high_beta"] == 0 and high["score"] == low["score"] + 30
    assert {x["name"] for x in high["scoring"]} == {"outperforms_sector", "high_beta", "wide_range", "closes_above_prior_high"}


def test_recommendation_says_what_why_and_the_plan():
    q = {"ticker": "ABC", "strategy": "breakout", "direction": "long", "action": "long", "regime": "bullish", "beta": 1.6,
         "holding_days": 37, "time_exit": "2026-01-30", "sector_trend": "up", "sector_etf": "XLK",
         "levels": {"entry": 100.0, "stop": 92.0, "base_stop": 96.0, "stop_buffer_atr": 0.8, "target": 120.0, "reward_risk": 2.5,
                    "resistance": 99.0},
         "indicators": {"rel_volume": 2.4},
         "scoring": [{"name": "strong_volume", "points": 20, "earned": 20, "detail": "relative volume 2.40"},
                     {"name": "tight_base", "points": 10, "earned": 0, "detail": "x"},
                     {"name": "outperforms_market", "points": 15, "earned": 15, "detail": "+8.0% vs QQQ"}]}
    text = ss.recommendation(q)
    assert text.startswith("ABC: PROPOSED LONG breakout (first choice in a bull market")
    assert "fresh close above the 99.0 resistance on 2.4x average volume" in text
    assert "strong volume (relative volume 2.40); stronger than the market (+8.0% vs QQQ)" in text and "tight base" not in text
    assert "stop 92.0 (0.8 ATR below the rule's 96.0 because beta is 1.6), target 120.0, reward/risk 2.5" in text
    assert "exit by 2026-01-30 (37 sessions)" in text
    watch = ss.recommendation({**q, "action": "watch", "sector_trend": "down"})
    assert "WATCH ONLY, sector ETF XLK trending down" in watch
    assert "WATCH ONLY, market neutral" in ss.recommendation({**q, "action": "watch", "regime": "neutral", "sector_trend": None})
    assert "NO TRADE, market bearish" in ss.recommendation({**q, "action": "no_trade", "regime": "bearish"})


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


# ---------------------------------------------------------------- squeeze


def test_squeeze_price_trigger_qualifies():
    rows = squeeze_rows()
    result = run(ss.evaluate_squeeze, rows)
    assert result["qualified"], failed(result)
    assert result["direction"] == "long" and ss.ENTRY_TYPES["squeeze"] == "open"
    levels, atr14 = result["levels"], result["indicators"]["atr14"]
    assert levels["entry"] == 186.4 and levels["stop"] == round(182.6 - 0.2 * atr14, 2) and levels["reward_risk"] == 2.0
    assert levels["breakout_level"] == round(max(r[1] for r in rows[-21:-1]), 2)
    assert sum(x["points"] for x in result["scoring"]) == 60  # the other 40 come from short interest, live only
    earned = {x["name"]: x["earned"] for x in result["scoring"]}
    assert set(earned) == {"market_not_hot", "outperforms_sector", "strong_rs", "outperforms_market", "sector_uptrend", "sector_beats_market"}
    assert result["score"] == 0  # no market or sector data in this fixture
    assert result["indicators"]["low_vs_52w_high"] < -0.2


@pytest.mark.parametrize("last, check", [
    ((183.0, 187.0, 182.6, 186.4, 1_500_000), "volume"),
    ((181.0, 183.2, 180.0, 182.5, 3_500_000), "range_breakout"),
    ((183.0, 187.0, 182.0, 183.8, 3_500_000), "strong_close"),
    ((183.0, 194.0, 182.6, 193.5, 3_500_000), "not_extended"),
])
def test_squeeze_rejections(last, check):
    assert check in failed(run(ss.evaluate_squeeze, squeeze_rows(last)))


def test_squeeze_needs_price_above_the_emas():
    rows = downtrend(301, 300, -0.5)
    o, h, l, c, v = rows[-1]
    rows[-1] = (o, max(r[1] for r in rows[-21:-1]) + 1, l, max(r[1] for r in rows[-21:-1]) + 0.9, 5_000_000)
    assert "above_emas" in failed(run(ss.evaluate_squeeze, rows))


def squeeze_signal():
    return run(ss.evaluate_squeeze, squeeze_rows())


def test_squeeze_checks_add_short_interest():
    as_of = date(2026, 11, 17)
    signal = squeeze_signal()
    warnings = ss.squeeze_checks(signal, {"short_float": 0.31, "short_ratio": 6.5, "earnings": as_of + timedelta(days=4),
                                          "earnings_text": "Nov 21 AMC"}, as_of, CFG)
    assert signal["qualified"] and signal["score"] == 40 and sum(x["points"] for x in signal["scoring"]) == 100
    assert warnings == ["earnings Nov 21 AMC in 4 days: gap risk both ways", "short float 31.0%, 6.5 days to cover"]
    low = squeeze_signal()
    ss.squeeze_checks(low, {"short_float": 0.08, "short_ratio": 9.0, "earnings": None, "earnings_text": None}, as_of, CFG)
    assert not low["qualified"] and low["score"] is None and failed(low) == ["short_interest"]
    unknown = squeeze_signal()
    ss.squeeze_checks(unknown, None, as_of, CFG)
    assert not unknown["qualified"] and "quote page not loaded" in unknown["checks"][-1]["detail"]


def test_squeeze_actions_and_horizon():
    assert ss.action_for("long", "bearish", None, "squeeze") == "countertrend_watch"
    assert ss.action_for("long", "bearish", None, "breakout") == "no_trade"
    assert ss.action_for("long", "bullish", "down", "squeeze") == "watch" and ss.action_for("long", "bullish", "up", "squeeze") == "long"
    assert ss.STRATEGY_GROUPS["long"] == ("pullback", "breakout", "squeeze") and ss.STRATEGY_GROUPS["both"] == ("pullback", "breakout")


# ---------------------------------------------------------------- SHORT strategies


def test_rally_rejection_qualifies_with_short_levels():
    rows = rally_rows()
    result = run(ss.evaluate_rally_rejection, rows)
    assert result["qualified"], failed(result)
    assert result["direction"] == "short" and result["strategy"] == "rally_rejection"
    levels, atr14 = result["levels"], result["indicators"]["atr14"]
    assert levels["entry"] == round(rows[-1][2] - 0.01, 2)
    assert levels["stop"] == round(max(r[1] for r in rows[-3:]) + 0.2 * atr14, 2)
    assert levels["stop"] > levels["entry"] > levels["target"] and levels["reward_risk"] >= 1.5
    assert levels["target_source"].startswith("2R")
    scores = {s["name"]: s["earned"] for s in result["scoring"]}
    assert set(scores) == {"underperforms_market", "underperforms_sector", "rsi_turning_down", "rally_volume_dry_up",
                           "rejection_volume", "upper_wick", "below_sma200", "failed_reclaim"}
    assert sum(s["points"] for s in result["scoring"]) == 100 and result["score"] == 55


@pytest.mark.parametrize("steps, check", [
    ((0.2, 0.2, 3.8, 0.2), "rally_not_a_spike"),
    ((0.1, 0.1, 0.1, 0.1), "rally_size"),
    ((1.6, -0.4, -0.4, 1.4), "rally_participation"),
])
def test_rally_rejection_needs_a_real_rally(steps, check):
    assert check in failed(run(ss.evaluate_rally_rejection, rally_rows(steps)))


def test_rally_rejection_rejects_a_two_bar_pop():
    rows = rally_rows()[:-5]
    close = rows[-1][3]
    rows += [(close + 0.1, close + 0.3, close - 3.0, close - 2.8, 1_000_000)]  # new low one bar before the signal...
    close -= 2.8
    rows += [(close, close + 3.6, close - 0.2, close + 3.4, 900_000), (close + 3.5, close + 5.0, close + 1.0, close + 1.2, 1_300_000)]
    assert "rally_duration" in failed(run(ss.evaluate_rally_rejection, rows))


def test_rally_rejection_needs_downtrend_resistance_and_bearish_bar():
    up = run(ss.evaluate_rally_rejection, pullback_rows())
    assert "downtrend" in failed(up)
    rows = rally_rows()
    o, h, l, c, v = rows[-1]
    green = rows[:-1] + [(l + 0.2, h, l, h - 0.2, v)]
    assert "bearish_bar" in failed(run(ss.evaluate_rally_rejection, green))
    reclaimed = rally_rows((2.5, 2.5, 2.5, 2.5))  # closes back above EMA20 and never reaches EMA50
    assert failed(run(ss.evaluate_rally_rejection, reclaimed)) == ["resistance_rejection"]


def test_rally_rejection_flags_gaps_inside_the_rally():
    rows = rally_rows()
    o, h, l, c, v = rows[-3]
    gap_open = rows[-4][1] + 4
    rows[-3] = (gap_open, gap_open + 0.5, min(l, c - 0.5), c, v)  # opens far above the previous high, closes back down
    assert any("gap up" in n for n in run(ss.evaluate_rally_rejection, rows)["notes"])


def test_breakdown_qualifies_with_short_levels():
    rows = breakdown_rows()
    result = run(ss.evaluate_breakdown, rows)
    assert result["qualified"], failed(result)
    levels, atr14 = result["levels"], result["indicators"]["atr14"]
    assert levels["support"] == 200.0 and levels["entry"] == 197.99
    assert levels["stop"] == round(max(rows[-1][1], 200.0) + 0.5 * atr14, 2)
    assert levels["reward_risk"] >= 2 and result["score"] == 75
    assert sum(s["points"] for s in result["scoring"]) == 100
    assert result["indicators"]["ema20_distance_atr"] > 0 and result["indicators"]["rel_volume"] == 3.0


@pytest.mark.parametrize("last, check", [
    ((201.5, 201.7, 192.0, 192.2, 3_000_000), "not_extended"),
    ((201.5, 202.5, 199.0, 202.0, 3_000_000), "fresh_breakdown"),
    ((199.0, 201.0, 198.5, 199.8, 3_000_000), "bearish_bar"),
])
def test_breakdown_rejections(last, check):
    assert check in failed(run(ss.evaluate_breakdown, breakdown_rows(last)))


def test_breakdown_needs_price_below_ema50_and_short_liquidity():
    assert "below_ema50" in failed(run(ss.evaluate_breakdown, breakout_rows()))
    thin = [(o, h, l, c, 50_000) for o, h, l, c, _ in breakdown_rows()]
    assert {"liquidity"} <= set(failed(run(ss.evaluate_breakdown, thin)))
    assert "liquidity" not in failed(run(ss.evaluate_pullback, [(o, h, l, c, 60_000) for o, h, l, c, _ in pullback_rows()]))


def test_support_mirrors_resistance():
    rows = [(10, 11, 9, 10, 1), (10, 11, 9.1, 10, 1), (10, 12, 8, 10, 1), (10, 11, 9.5, 10, 1), (10, 11, 8.2, 10, 1), (10, 11, 9.5, 10, 1)]
    level, age, touches, high = ss.support(make_bars(rows), 6, 6, 0.5)
    assert (level, age, touches, high) == (8, 4, 2, 12)


def test_next_support_uses_confirmed_swing_lows_only():
    rows = downtrend(260, 200, 0.0)
    rows[240] = (200, 201, 190, 200, 1)  # swing low at 190, confirmed 5 bars later
    bars = make_bars(rows)
    series = ss.compute(bars, {}, CFG)
    assert ss.next_support(bars, series, 259, 196.0, 2.0, 1.0) == (190.1, "swing-low support 190.00 + 0.1 ATR")
    target, source = ss.next_support(bars, series, 244, 196.0, 2.0, 1.0)  # not confirmed yet at t = 244
    assert target == 192.0 and source.startswith("2R")
    assert ss.next_support(bars, series, 259, 196.0, 1.0, 1.0)[1].startswith("2R")  # 6R away: beyond 3R
    assert ss.next_support(bars, series, 259, 191.0, 2.0, 1.0)[1].startswith("2R")  # less than 1R below the entry


def short_signal(entry=10.0, stop=11.0, target=8.0, strategy="rally_rejection", atr14=1.0):
    return {"strategy": strategy, "levels": {"entry": entry, "stop": stop, "target": target}, "indicators": {"atr14": atr14}}


@pytest.mark.parametrize("bar, detail", [
    ((11.2, 11.5, 9.5, 10.0, 1), "opened at or above the stop"),
    ((7.9, 8.2, 7.5, 8.0, 1), "opened at or below the target"),
    ((8.9, 9.0, 8.5, 8.8, 1), "gap fill leaves 0.43R of reward"),
])
def test_short_pending_order_is_cancelled(bar, detail):
    out = ss.simulate(future([bar] * 3), 0, short_signal(), 2)
    assert out == {"outcome": "not_filled", "detail": detail}
    assert ss.track(future([bar] * 3), 0, short_signal(), 2)["status"] == "skipped"


def test_short_gap_fill_risk_cap():
    out = ss.simulate(future([(9.5, 9.6, 9.0, 9.2, 1)] * 2), 0, short_signal(stop=12.5, target=4.0, atr14=1.0), 2)
    assert out == {"outcome": "not_filled", "detail": "gap fill leaves a risk of 3.00 ATR"}


def test_short_not_triggered_and_fills():
    assert ss.simulate(future([(10.5, 10.8, 10.1, 10.6, 1)] * 2), 0, short_signal(), 2) == {"outcome": "not_filled"}
    win = ss.simulate(future([(10.5, 10.6, 9.8, 9.9, 1), (9.5, 9.6, 7.9, 8.0, 1)]), 0, short_signal(), 2)
    assert (win["outcome"], win["fill"], win["exit"], win["r"], win["gap"]) == ("target", 10.0, 8.0, 2.0, False)
    gap = ss.simulate(future([(9.8, 9.9, 9.5, 9.6, 1), (11.5, 11.8, 11.0, 11.2, 1)]), 0, short_signal(), 2)
    assert (gap["outcome"], gap["fill"], gap["exit"], gap["gap"]) == ("stop", 9.8, 11.5, True) and gap["r"] == round((9.8 - 11.5) / 1.2, 4)
    target_gap = ss.simulate(future([(10.5, 10.6, 9.8, 9.9, 1), (7.5, 7.8, 7.2, 7.4, 1)]), 0, short_signal(), 2)
    assert (target_gap["outcome"], target_gap["exit"], target_gap["r"]) == ("target", 7.5, 2.5)
    timeout = ss.simulate(future([(10.5, 10.6, 9.8, 9.9, 1), (9.9, 10.4, 9.5, 9.6, 1), (9.6, 9.9, 9.2, 9.4, 1)]), 0, short_signal(), 3)
    assert (timeout["outcome"], timeout["exit"], timeout["r"]) == ("timeout", 9.4, 0.6)
    assert (timeout["mae_r"], timeout["mfe_r"]) == (0.6, 0.8)  # adverse = highs, favourable = lows


def test_short_ambiguous_bars_assume_the_stop():
    entry_bar = ss.simulate(future([(10.5, 11.2, 9.9, 10.0, 1)] * 2), 0, short_signal(), 2)
    assert (entry_bar["outcome"], entry_bar["r"], entry_bar["ambiguous"]) == ("stop", -1.0, "fill_and_stop_same_bar")
    both = ss.simulate(future([(10.5, 10.6, 9.9, 10.1, 1), (10.0, 11.1, 7.9, 9.0, 1)]), 0, short_signal(), 2)
    assert (both["outcome"], both["exit"], both["ambiguous"]) == ("stop", 11.0, "stop_and_target_same_bar")
    gap_fill = ss.simulate(future([(9.8, 11.2, 9.5, 10.0, 1)] * 2), 0, short_signal(), 2)
    assert gap_fill["outcome"] == "stop" and gap_fill["ambiguous"] is None  # filled at the open, so the high came after


def test_short_track_open_and_breakout_fill_unchanged():
    running = ss.track(future([(10.5, 10.6, 9.8, 9.9, 1), (9.9, 10.0, 9.4, 9.5, 1)]), 0, short_signal(), 10)
    assert running["status"] == "open" and running["fill"] == 10.0 and running["last_close"] == 9.5 and running["r_now"] == 0.5
    long_out = ss.simulate(future([(10, 10.5, 9.6, 10.2, 1), (10.5, 12.3, 10.4, 12.1, 1)]), 0, signal("breakout", 0, 9, 12), 2)
    assert (long_out["outcome"], long_out["fill"], long_out["r"]) == ("target", 10, 2.0)


def test_short_validation_and_tracking_hold_for_the_swing_period():
    rows = rally_rows() + [(153.6, 153.9, 153.2, 153.5, 1_000_000)] * 50  # fills the sell stop, then goes nowhere
    t = len(rows) - 51
    bars = make_bars(rows)
    series = ss.compute(bars, {}, CFG)  # no beta benchmark: the midpoint of 30-45
    trades, _, _ = ss.validate(bars, series, ["rally_rejection"], 60, CFG)
    trade = [x for x in trades if x["signal_date"] == bars.dates[t].isoformat()][0]
    assert trade["direction"] == "short" and trade["outcome"] == "timeout" and trade["bars_held"] == 38
    assert trade["holding_days"] == 38 and trade["beta"] is None
    tracked = ss.track_signals(bars, series, ["rally_rejection"], 60, CFG, {bars.dates[t]: "bearish"})
    first = [x for x in tracked if x["signal_date"] == bars.dates[t].isoformat()][0]
    assert first["action"] == "bull_put_spread" and first["status"] == "time_exit" and first["direction"] == "short" and first["fill"] == 153.39
    assert first["holding_days"] == 38 and first["bars_held"] == 38


def beta_pair(beta, n=300):
    """Benchmark closes by date and stock bars whose daily returns are exactly beta times the benchmark's."""
    days = sessions(n)
    bench, stock = [100.0], [50.0]
    for i in range(1, n):
        r = 0.01 * math.sin(i * 1.7) + 0.004 * math.cos(i * 0.3)
        bench.append(bench[-1] * (1 + r))
        stock.append(stock[-1] * (1 + beta * r))
    return dict(zip(days, bench)), make_bars([(c, c, c, c, 1) for c in stock])


@pytest.mark.parametrize("beta, days", [(0.6, 45), (1.0, 45), (1.4, 39), (1.6, 36), (2.0, 30), (3.1, 30)])
def test_beta_sets_the_holding_period(beta, days):
    closes, bars = beta_pair(beta)
    series = ss.compute(bars, {}, CFG, closes)
    assert series["beta"][58] is None and series["beta"][-1] == pytest.approx(beta)
    assert ss.holding_days(series["beta"][-1], CFG) == days == ss.horizon_for(CFG, series, len(bars) - 1)


def test_holding_days_settings_and_dates():
    assert ss.holding_days(None, CFG) == 38
    same = ss.settings_with_defaults({"swing_min_days": 21, "swing_max_days": 32, "beta_low": 0.8, "beta_high": 1.6})
    assert ss.holding_days(0.5, same) == 32 and ss.holding_days(1.2, same) == 26 and ss.holding_days(2, same) == 21
    with pytest.raises(ValueError, match="swing_min_days"):
        ss.settings_with_defaults({"swing_min_days": 50})
    assert ss.add_sessions(date(2026, 11, 13), 1) == date(2026, 11, 16)  # Friday + 1 session = Monday
    assert ss.add_sessions(date(2026, 11, 17), 30) == date(2026, 12, 29)
    rule = ss.holding_rule(CFG)
    assert rule["beta_vs"] == "SPY" and "45 at beta <= 1, 30 at beta >= 2" in rule["rule"]


def test_rolling_beta_skips_missing_benchmark_dates():
    closes, bars = beta_pair(1.5)
    del closes[bars.dates[150]]
    beta = ss.rolling_beta(bars, closes, 252)
    assert beta[-1] == pytest.approx(1.5, rel=0.05) and ss.rolling_beta(bars, {}, 252)[-1] is None


def test_action_for_regime_and_sector():
    assert ss.action_for("long", "bullish") == "long" and ss.action_for("long", "bullish", "down") == "watch"
    assert ss.action_for("long", "bearish", "up") == "no_trade" and ss.action_for("long", "neutral") == "watch"
    assert ss.action_for("short", "bearish") == "bull_put_spread" and ss.action_for("short", "bearish", "up") == "bull_put_spread"
    assert ss.action_for("short", "bullish", "down") == "countertrend_watch" and ss.action_for("short", "unknown") == "watch"
    # a correction makes SHORT setups bull put spreads in any regime; LONG setups keep the regime's action
    assert ss.action_for("short", "bullish", "up", "breakdown", "correction") == "bull_put_spread"
    assert ss.action_for("short", "neutral", None, "run_break", "correction") == "bull_put_spread"
    assert ss.action_for("long", "neutral", None, "pullback", "correction") == "watch"


def test_trend_labels():
    closes = [100 + i for i in range(230)] + [329 - 3 * i for i in range(100)]
    labels = ss.trend_labels(sessions(len(closes)), closes, 20)
    values = list(labels.values())
    assert values[150] is None and values[229] == "up" and values[-1] == "down" and "mixed" in values


AAPL_SNAPSHOT = (
    '<td>Earnings</td>'
    '<a href="x">Earnings</a></div></td><td class="snapshot-td2" align="left"><div class="snapshot-td-content">'
    '<a href="stock?t=AAPL&ty=ea" class="hover:underline"><b><small class="text-2xs">Jan 29 AMC</small></b></a></div></td>'
    '<a href="x">Short Float</a></div></td><td class="snapshot-td2" align="left"><div class="snapshot-td-content">'
    '<a href="stock?t=AAPL&ty=si" class="hover:underline"><b>24.50%</b></a></div></td>'
    '<a href="x">Short Ratio</a></div></td><td class="snapshot-td2" align="left"><div class="snapshot-td-content">'
    '<a href="stock?t=AAPL&ty=si" class="hover:underline"><b>6.20</b></a></div></td>')


def test_parse_snapshot():
    out = ss.parse_snapshot(AAPL_SNAPSHOT, date(2026, 12, 20))
    assert out == {"earnings": date(2027, 1, 29), "earnings_text": "Jan 29 AMC", "short_float": 0.245, "short_ratio": 6.2}
    assert ss.parse_snapshot(AAPL_SNAPSHOT, date(2026, 2, 10))["earnings"] == date(2026, 1, 29)
    assert ss.parse_snapshot("<html></html>", date(2026, 1, 1)) == {"earnings": None, "earnings_text": None, "short_float": None, "short_ratio": None}


def test_short_checks():
    as_of = date(2027, 1, 25)
    snap = ss.parse_snapshot(AAPL_SNAPSHOT, as_of)
    signal = {"checks": []}
    warnings = ss.short_checks(signal, snap, as_of, CFG)
    assert signal["checks"][0]["name"] == "no_earnings_soon" and not signal["checks"][0]["passed"]
    assert warnings == ["squeeze risk: short float 24.5% (high)", "squeeze risk: 6.2 days to cover"]
    signal = {"checks": []}
    ss.short_checks(signal, snap, date(2027, 1, 10), CFG)
    assert signal["checks"][0]["passed"]
    assert ss.short_checks({"checks": []}, None, as_of, CFG) == ["earnings date and short float unknown (quote page not loaded)"]


def test_relative_strength_scoring_uses_benchmarks():
    rows = pullback_rows()
    dates = sessions(len(rows))
    flat = {d: 100.0 for d in dates}
    soaring = {d: 100.0 * 1.02 ** i for i, d in enumerate(dates)}
    result = run(ss.evaluate_pullback, rows, benchmarks={"QQQ": flat, "sector": soaring}, sector_etf="XLK")
    scores = {s["name"]: s for s in result["scoring"]}
    assert scores["outperforms_sector"]["earned"] == 0 and "outperforms_market" not in scores  # pullback scores the sector only
    beats = run(ss.evaluate_pullback, rows, benchmarks={"QQQ": soaring, "sector": flat}, sector_etf="XLK")
    assert {s["name"]: s["earned"] for s in beats["scoring"]}["outperforms_sector"] == 38
    assert "vs XLK over 63 bars" in scores["outperforms_sector"]["detail"]
    rs = result["relative_strength"]
    assert rs["market"]["benchmark"] == "QQQ" and rs["market"]["change"] > 0
    assert rs["sector"]["benchmark"] == "XLK" and rs["sector"]["change"] < 0
    unknown = run(ss.evaluate_pullback, rows, benchmarks={"QQQ": flat})
    sector = {s["name"]: s for s in unknown["scoring"]}["outperforms_sector"]
    assert sector["earned"] == 0 and sector["detail"] == "sector unknown or no sector ETF data"
    assert unknown["relative_strength"]["sector"] == {"benchmark": None, "change": None}


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


def test_simulate_fill_above_target_exits_at_the_open():
    # Regression: a breakout opening above its target used to exit at the lower target price.
    out = ss.simulate(future([(12.6, 13, 12.2, 12.8, 1)] * 3), 0, signal("breakout", 0, 9, 12), 2)
    assert out["outcome"] == "target" and out["gap"] and out["fill"] == out["exit"] == 12.6 and out["r"] == 0 and out["bars_held"] == 1


def test_simulate_pullback_gap_above_target_exits_at_the_open():
    out = ss.simulate(future([(12.4, 12.9, 11.9, 12.5, 1)] * 3), 0, signal("pullback", 10.6, 9, 12), 2)
    assert (out["outcome"], out["fill"], out["exit"], out["r"]) == ("target", 12.4, 12.4, 0)


def test_simulate_open_comes_before_the_range():
    # Opens above the target, then trades down through the stop: the open happens first.
    up = ss.simulate(future([(10, 10.5, 9.6, 10.2, 1), (12.5, 12.6, 8.5, 9, 1)]), 0, signal("breakout", 0, 9, 12), 2)
    assert (up["outcome"], up["exit"], up["gap"]) == ("target", 12.5, True)
    # Opens below the stop: exits at the open, worse than the stop.
    down = ss.simulate(future([(10, 10.5, 9.6, 10.2, 1), (8, 12.5, 7.5, 12, 1)]), 0, signal("breakout", 0, 9, 12), 2)
    assert (down["outcome"], down["exit"], down["r"], down["gap"]) == ("stop", 8, -2.0, True)


def test_simulate_intrabar_stop_and_target_exits():
    stop = ss.simulate(future([(10, 10.5, 9.6, 10.2, 1), (10, 10.2, 8.9, 9.5, 1)]), 0, signal("breakout", 0, 9, 12), 2)
    assert (stop["outcome"], stop["exit"], stop["gap"]) == ("stop", 9, False)
    target = ss.simulate(future([(10, 10.5, 9.6, 10.2, 1), (10.5, 12.3, 10.4, 12.1, 1)]), 0, signal("breakout", 0, 9, 12), 2)
    assert (target["outcome"], target["exit"], target["r"], target["gap"]) == ("target", 12, 2.0, False)


def test_simulate_flags_ambiguous_bars_and_assumes_the_stop():
    both = ss.simulate(future([(10, 10.5, 9.6, 10.2, 1), (10.5, 12.5, 8.8, 11, 1)]), 0, signal("breakout", 0, 9, 12), 2)
    assert (both["outcome"], both["exit"], both["r"], both["ambiguous"]) == ("stop", 9, -1.0, "stop_and_target_same_bar")
    entry_bar = ss.simulate(future([(10, 11, 8.9, 10.5, 1)] * 2), 0, signal("pullback", 10.6, 9, 12), 2)
    assert (entry_bar["outcome"], entry_bar["fill"], entry_bar["ambiguous"]) == ("stop", 10.6, "fill_and_stop_same_bar")
    clean = ss.simulate(future([(10, 10.5, 9.6, 10.2, 1), (10, 10.2, 8.9, 9.5, 1)]), 0, signal("breakout", 0, 9, 12), 2)
    assert clean["outcome"] == "stop" and clean["ambiguous"] is None
    gap_open = ss.simulate(future([(10.7, 11, 8.9, 10.5, 1)] * 2), 0, signal("pullback", 10.6, 9, 12), 2)
    assert gap_open["outcome"] == "stop" and gap_open["ambiguous"] is None  # filled at the open, so the low came after
    assert ss.summarize([{**both, "signal_date": "2025-01-01", "ticker": "A", "regime": "bullish"}])["ambiguous_bars"] == {
        "stop_and_target_same_bar": 1, "fill_and_stop_same_bar": 0}


def test_track_flags_ambiguous_bars():
    flat = [(10, 10, 10, 10, 1)] * 3
    both = make_bars(flat + [(10.0, 10.5, 9.9, 10.2, 1), (10.2, 12.3, 8.9, 10, 1)])
    result = ss.track(both, 2, pullback_signal(), 20)
    assert (result["status"], result["r"], result["ambiguous"]) == ("stopped", -1.0, "stop_and_target_same_bar")


def test_simulate_mae_and_mfe():
    sig = {**signal("breakout", 0, 8, 20), "indicators": {"atr14": 0.5}}
    out = ss.simulate(future([(10, 11, 9.5, 10.5, 1), (10.5, 12, 9, 11, 1), (11, 11.5, 10, 10.8, 1)]), 0, sig, 3)
    assert out["outcome"] == "timeout" and out["exit"] == 10.8
    assert (out["mae_r"], out["mfe_r"], out["mae_atr"], out["mfe_atr"]) == (0.5, 1.0, 2.0, 4.0)
    stopped = ss.simulate(future([(10, 11, 9.5, 10.5, 1), (10.5, 12, 7, 11, 1)]), 0, sig, 2)
    assert stopped["mae_r"] == 1.0 and stopped["mfe_r"] == 0.5  # the stop bar's high is not counted


def test_validate_walks_forward_without_overlap():
    rows = breakout_rows() + [(192 + i, 193.5 + i, 191.5 + i, 193 + i, 1_000_000) for i in range(25)]
    bars = make_bars(rows)
    trades, signals, base = ss.validate(bars, ss.compute(bars, {}, CFG20), ["breakout"], 40, CFG20)
    assert [t["signal_date"] for t in trades] == [bars.dates[300].isoformat()]
    assert trades[0]["outcome"] == "target" and trades[0]["fill"] == 192 and trades[0]["regime"] == "unknown"
    assert trades[0]["earnings"] == {"known": False}
    assert [s["signal_date"] for s in signals] == [bars.dates[300].isoformat()] and not signals[0]["overlaps"]
    assert len(base) == 40 and all("fwd_20" in x for x in base)
    summary = ss.summarize(trades, signals)
    assert summary["closed"] == 1 and summary["gross"]["win_rate"] == 1 and summary["gross"]["expectancy_r"] > 0
    assert summary["forward_returns"]["all_signals"]["5d"]["n"] == 1


def test_forward_returns_start_at_the_next_open_and_never_run_past_the_data():
    bars = make_bars([(10, 10, 10, 10, 1)] + [(20, 22, 19, 21 + k, 1) for k in range(20)])
    out = ss.forward_returns(bars, 0)
    assert out["fwd_5"] == pytest.approx((21 + 4) / 20 - 1) and out["fwd_20"] == pytest.approx((21 + 19) / 20 - 1)
    assert set(ss.forward_returns(bars, 10)) == {"fwd_5", "fwd_10"}
    assert ss.forward_returns(bars, 20) == {}
    market = {"open": {bars.dates[1]: 100}, "close": {bars.dates[5]: 110}}
    assert ss.forward_returns(bars, 0, market)["excess_5"] == pytest.approx(0.25 - 0.10)


def test_validation_window_leaves_room_for_every_forward_return():
    rows = uptrend(330)
    bars = make_bars(rows)
    _, _, base = ss.validate(bars, ss.compute(bars, {}, CFG), ["pullback"], 500, CFG)
    assert base and all(set(x) == {"fwd_5", "fwd_10", "fwd_20"} for x in base)


def test_overlapping_signals_are_flagged():
    rows = breakout_rows()
    bars = make_bars(rows + [(192, 193, 191, 192.5, 1_000_000)] * 30)
    def fake(b, s, t, cfg):
        return {"qualified": t in (265, 280, 305), "signal_date": b.dates[t].isoformat(), "score": 1,
                "levels": {"entry": 0, "stop": 1, "target": 2}, "strategy": "breakout"}

    original = ss.EVALUATORS["breakout"]
    ss.EVALUATORS["breakout"] = fake
    try:
        _, signals, _ = ss.validate(bars, ss.compute(bars, {}, CFG20), ["breakout"], 60, CFG20)
    finally:
        ss.EVALUATORS["breakout"] = original
    assert [s["overlaps"] for s in signals] == [False, True, False]


def test_metrics_drawdown_and_costs():
    m = ss.metrics([1.0, -1.0, -1.0, 2.0, -0.5])
    assert m == {"trades": 5, "expectancy_r": 0.1, "median_r": -0.5, "win_rate": 0.4, "profit_factor": 1.2,
                 "total_r": 0.5, "max_drawdown_r": 2.0, "largest_loss_r": -1.0}
    trade = {"r": 1.0, "fill": 100.0, "risk": 2.0}
    assert ss.net_r(trade, 0) == 1.0 and ss.net_r(trade, 10) == pytest.approx(0.95) and ss.net_r(trade, 50) == pytest.approx(0.75)
    assert ss.metrics([]) == {"trades": 0}


def closed_trade(r, outcome="stop", regime="uptrend", gap=False, exit_date="2026-01-05", earnings=None):
    return {"ticker": "X", "signal_date": "2026-01-01", "exit_date": exit_date, "outcome": outcome, "gap": gap, "r": r,
            "fill": 100.0, "risk": 2.0, "regime": regime, "mae_r": 1, "mfe_r": 0.5, "mae_atr": 1, "mfe_atr": 0.5,
            "earnings": earnings or {"known": False}}


def test_summarize_reports_every_diagnostic():
    trades = [closed_trade(-1.0), closed_trade(-2.5, gap=True, exit_date="2026-01-06"),
              closed_trade(2.0, "target", regime="bear", exit_date="2026-01-07", earnings={"known": True, "held_through": True, "reaction_at_entry": False}),
              closed_trade(0.4, "timeout", exit_date="2026-01-08", earnings={"known": True, "held_through": False, "reaction_at_entry": True}),
              {"ticker": "X", "signal_date": "2026-01-02", "outcome": "not_filled", "regime": "uptrend"}]
    s = ss.summarize(trades)
    assert (s["trades"], s["not_filled"], s["closed"], s["exits"]) == (5, 1, 4, {"stop": 2, "target": 1, "timeout": 1})
    assert s["gross"] == s["costs"]["0bp"] and s["gross"]["max_drawdown_r"] == 3.5 and s["gross"]["largest_loss_r"] == -2.5
    assert s["costs"]["50bp"]["expectancy_r"] == pytest.approx(s["gross"]["expectancy_r"] - 0.25, abs=1e-3)
    assert s["gaps"] == {"stop_exits_through_gap": 1, "extra_loss_beyond_stop_r": 1.5, "worst_gap_stop_r": -2.5, "target_exits_at_open": 0}
    assert set(s["by_regime"]) == {"bear", "uptrend"} and s["by_regime"]["bear"]["gross"]["trades"] == 1
    assert s["earnings"]["trades_with_known_dates"] == 2 and s["earnings"]["held_through_report"]["expectancy_r"] == 2.0
    assert s["earnings"]["report_at_signal_or_entry"]["trades"] == 1
    assert s["mae_mfe"]["stop"]["trades"] == 2


def test_summarize_empty():
    s = ss.summarize([])
    assert s["closed"] == 0 and s["gross"] == {"trades": 0}


def test_market_regime_from_trend():
    closes = [100 + i for i in range(260)] + [359 - 2 * i for i in range(160)]
    dates = sessions(len(closes))
    regime = ss.market_regime(dates, closes)
    assert regime[dates[100]] == "unknown" and regime[dates[218]] == "unknown" and regime[dates[219]] == "bullish"
    labels = [regime[d] for d in dates[219:]]
    order = [label for i, label in enumerate(labels) if i == 0 or label != labels[i - 1]]
    assert order == ["bullish", "neutral", "bearish"]  # below a still-rising SMA200 is only neutral


def test_market_regime_uses_breadth():
    closes = [100 + i for i in range(300)]
    dates = sessions(len(closes))
    weak = {d: (3, 9) for d in dates}
    assert ss.market_regime(dates, closes, weak)[dates[-1]] == "neutral"
    assert ss.market_regime(dates, closes, {d: (6, 9) for d in dates})[dates[-1]] == "bullish"
    assert ss.market_regime(dates, closes, {d: (2, 5) for d in dates})[dates[-1]] == "bullish"  # too few ETFs: trend only
    dip = closes[:280] + [closes[279] - 150] * 20  # below a still-rising SMA200
    assert ss.market_regime(dates, dip)[dates[-1]] == "neutral"
    assert ss.market_regime(dates, dip, weak)[dates[-1]] == "bearish"


def test_breadth_counts_sector_etfs_above_sma200():
    up = make_bars(uptrend(210), "XLK")
    down = make_bars(uptrend(210, 300, -0.5), "XLE")
    counts = ss.breadth({"XLK": up, "XLE": down})
    assert counts[up.dates[-1]] == (1, 2) and up.dates[100] not in counts


def test_parse_sector():
    page = '<a href="screener.ashx?v=111&f=sec_technology" class="tab-link">Technology</a>'
    assert ss.parse_sector(page) == "Technology"
    assert ss.parse_sector('<a href="screener.ashx?f=sec_x">Nonsense</a>') is None
    assert ss.parse_sector("<html></html>") is None


# ---------------------------------------------------------------- tracking


def pullback_signal(entry=10.0, stop=9.0, target=12.0, strategy="pullback"):
    return {"strategy": strategy, "levels": {"entry": entry, "stop": stop, "target": target}}


def test_track_statuses():
    flat = [(10, 10, 10, 10, 1)] * 3
    not_triggered = make_bars(flat + [(9.8, 9.9, 9.6, 9.7, 1)])
    assert ss.track(not_triggered, 2, pullback_signal(), 20)["status"] == "not_triggered"
    gap_down = make_bars(flat + [(8.5, 8.6, 8.0, 8.2, 1)])
    assert ss.track(gap_down, 2, pullback_signal(strategy="breakout"), 20)["status"] == "skipped"
    stopped = make_bars(flat + [(10.0, 10.5, 9.9, 10.2, 1), (10.1, 10.2, 8.8, 9.0, 1)])
    result = ss.track(stopped, 2, pullback_signal(), 20)
    assert result["status"] == "stopped" and result["r"] == -1.0 and result["exit_date"] == stopped.dates[4].isoformat()
    target = make_bars(flat + [(10.0, 10.5, 9.9, 10.2, 1), (12.4, 12.6, 12.2, 12.5, 1)])
    result = ss.track(target, 2, pullback_signal(), 20)
    assert result["status"] == "target" and result["gap"] and result["exit"] == 12.4
    running = make_bars(flat + [(10.0, 10.5, 9.9, 10.2, 1), (10.2, 11.0, 10.1, 10.8, 1)])
    result = ss.track(running, 2, pullback_signal(), 20)
    assert result["status"] == "open" and result["r_now"] == 0.8 and result["bars_held"] == 2 and result["bars_left"] == 18
    timed = make_bars(flat + [(10.0, 10.5, 9.9, 10.2, 1), (10.2, 11.0, 10.1, 10.8, 1), (10.8, 11.0, 10.5, 10.9, 1)])
    result = ss.track(timed, 2, pullback_signal(), 2)
    assert result["status"] == "time_exit" and result["exit"] == 10.8 and result["bars_held"] == 2


def test_signal_warnings():
    def signal(entry, stop, atr):
        return {"levels": {"entry": entry, "risk_per_share": round(entry - stop, 2)}, "indicators": {"atr14": atr}}
    assert ss.signal_warnings(signal(100.0, 97.0, 3.0)) == []
    tight = ss.signal_warnings(signal(15.0, 14.9, 0.3))
    assert len(tight) == 2 and tight[0].startswith("tight stop") and "0.15 R" in tight[1]


def test_track_signals_reports_past_signals_with_regime_and_action():
    rows = pullback_rows()
    signal_bar = rows[-1]
    o, h, l, c, v = signal_bar
    rows = rows + [(c, h + 1.0, c - 0.1, h + 0.5, 1_200_000)] * 2  # triggers the buy stop above h, stays above the stop
    bars = make_bars(rows)
    series = ss.compute(bars, {}, CFG)
    t = len(rows) - 3
    regime = {bars.dates[t]: "neutral"}
    tracked = ss.track_signals(bars, series, ["pullback"], 5, CFG, regime)
    assert [x["signal_date"] for x in tracked] == [bars.dates[t].isoformat()]
    row = tracked[0]
    assert row["regime"] == "neutral" and row["action"] == "watch" and row["status"] == "open" and row["bars_held"] == 2
    summary = ss.tracking_summary(tracked)
    assert summary["signals"] == 1 and summary["status"]["open"] == 1 and summary["open_average_r_now"] == row["r_now"]
    assert ss.track_signals(bars, series, ["pullback"], 1, CFG) == []  # t = len - 2 only


# ---------------------------------------------------------------- earnings


def quote_page(events):
    import json
    return '<html><script>var data = {"ticker":"AAPL","chartEvents":' + json.dumps(events) + ',"other":[1]}</script></html>'


def test_parse_earnings_keeps_only_earnings_events():
    events = [{"eventType": "chartEvent/earnings", "dateTimestamp": 1761856200},
              {"eventType": "chartEvent/dividends", "dateTimestamp": 1762732800},
              {"eventType": "chartEvent/earnings", "dateTimestamp": 1753993800}]
    assert ss.parse_earnings(quote_page(events)) == [datetime(2025, 7, 31, 20, 30, tzinfo=timezone.utc),
                                                     datetime(2025, 10, 30, 20, 30, tzinfo=timezone.utc)]
    with pytest.raises(ValueError, match="no chart events"):
        ss.parse_earnings("<html>nothing</html>")


def test_reaction_session_depends_on_report_time():
    calendar = sessions(10, date(2026, 1, 5))  # Mon 5 Jan to Fri 16 Jan
    before_open = datetime(2026, 1, 6, 12, 0, tzinfo=timezone.utc)  # 07:00 New York, Tue
    after_close = datetime(2026, 1, 8, 21, 30, tzinfo=timezone.utc)  # 16:30 New York, Thu
    friday_close = datetime(2026, 1, 9, 21, 30, tzinfo=timezone.utc)  # reacts on Monday
    info = ss.reaction_sessions([before_open, after_close, friday_close], calendar)
    assert info["sessions"] == {date(2026, 1, 6), date(2026, 1, 9), date(2026, 1, 12)} and info["since"] == date(2026, 1, 6)
    assert ss.reaction_sessions([], calendar) == {"since": None, "sessions": set()}


def test_earnings_flags():
    info = {"since": date(2026, 1, 1), "sessions": {date(2026, 2, 10)}}
    trade = {"signal_date": "2026-02-05", "fill_date": "2026-02-06", "exit_date": "2026-02-12"}
    assert ss.earnings_flags(trade, info) == {"known": True, "held_through": True, "reaction_at_entry": False}
    entry = {"signal_date": "2026-02-10", "fill_date": "2026-02-11", "exit_date": "2026-02-20"}
    assert ss.earnings_flags(entry, info) == {"known": True, "held_through": False, "reaction_at_entry": True}
    assert ss.earnings_flags({**trade, "signal_date": "2025-12-01"}, info) == {"known": False}
    assert ss.earnings_flags(trade, None) == {"known": False}


# ---------------------------------------------------------------- inputs and main


def test_settings_overrides_and_validation():
    assert ss.settings_with_defaults({"min_rel_volume": 2, "benchmarks": ["QQQ"]})["min_rel_volume"] == 2
    assert ss.settings_with_defaults({"min_bars": 10})["min_bars"] == 253
    for bad, message in [({"nope": 1}, "unknown setting"), ({"benchmarks": "QQQ"}, "benchmarks"), ({"max_risk_atr": -1}, "0 or more"),
                         ({"min_rel_volume": True}, "number"), ({"breakout_lookback": 0}, "1 or more")]:
        with pytest.raises(ValueError, match=message):
            ss.settings_with_defaults(bad)
    assert ss.DEFAULTS["benchmarks"] == ["QQQ"]


def test_discover_tickers_and_finviz(monkeypatch):
    assert ss.discover("", "aapl, msft,AAPL", 5) == (["AAPL", "MSFT"], "tickers", {})
    with pytest.raises(ValueError, match="not tickers"):
        ss.discover("", "AAPL,rm -rf", 5)
    with pytest.raises(ValueError, match="Finviz"):
        ss.discover("https://evil.example/screener.ashx", "", 5)
    seen = {}

    def screen(url, rows=None):
        seen["rows"] = rows
        return iter([{"Ticker": "AAA", "Sector": "Technology"}, {"Ticker": "BBB", "Sector": "Unknown"}])

    monkeypatch.setattr(ss.Screener, "init_from_url", screen)
    assert ss.discover("https://finviz.com/screener.ashx?v=171", "", 2) == (["AAA", "BBB"], "finviz", {"AAA": "Technology"})
    assert seen["rows"] == 2


def test_discover_merges_several_finviz_urls(monkeypatch):
    lists = {"a": [{"Ticker": "AAA", "Sector": "Technology"}, {"Ticker": "BBB", "Sector": "Energy"}, {"Ticker": "XXX"}],
             "b": [{"Ticker": "BBB", "Sector": "Energy"}, {"Ticker": "CCC", "Sector": "Healthcare"}]}
    monkeypatch.setattr(ss.Screener, "init_from_url", lambda url, rows=None: iter(lists[url[-1]]))
    urls = "https://finviz.com/screener.ashx?v=111&f=cap_midover,sh_price_o5&o=a\n  https://finviz.com/screener.ashx?f=sh_short_o15&o=b\n"
    names, source, sectors = ss.discover(urls, "", 2)
    assert names == ["AAA", "BBB", "CCC"] and source == "finviz"  # 2 from each, duplicates dropped, commas inside URLs kept
    assert sectors == {"AAA": "Technology", "BBB": "Energy", "CCC": "Healthcare"}
    with pytest.raises(ValueError, match="at most 3"):
        ss.discover(" ".join(["https://finviz.com/screener.ashx"] * 4), "", 2)
    with pytest.raises(ValueError, match="evil"):
        ss.discover("https://finviz.com/screener.ashx https://evil.example/x", "", 2)


@pytest.fixture
def offline(monkeypatch):
    rows = {"PULL": pullback_rows(), "BRK": breakout_rows(), "QQQ": uptrend(301, 300, 0.1),
            "RALY": downtrend(3, 301.5) + rally_rows(), "BRKD": breakdown_rows(), "SQZ": squeeze_rows(), "SQLO": squeeze_rows()}
    rows["PULL"] = uptrend(4) + rows["PULL"]  # same length as the others
    for k, etf in enumerate(ss.DEFAULTS["context_etfs"]):
        rows[etf] = uptrend(301, 400 + k, 0.2 if etf != "IWM" else -0.05)
    for k, etf in enumerate(sorted(set(ss.SECTOR_ETFS.values()))):
        rows[etf] = uptrend(301, 50 + k, 0.05 if etf != "XLE" else -0.02)
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)

    def fetch(ticker, count):
        if ticker not in rows:
            raise ValueError("chart data is for another ticker")
        return make_bars(rows[ticker], ticker)

    monkeypatch.setattr(ss, "fetch_bars", fetch)

    def earnings(ticker):
        if ticker != "BRK":
            raise ValueError("the quote page has no chart events")
        return [datetime(2025, 12, 1, 21, 30, tzinfo=timezone.utc)]

    monkeypatch.setattr(ss, "fetch_earnings", earnings)

    def sector(ticker):
        if ticker == "BRK":
            raise ValueError("no sector")
        return "Technology"

    monkeypatch.setattr(ss, "fetch_sector", sector)

    def snapshot(ticker, today):
        if ticker in ("BRKD", "SQLO"):
            return {"earnings": today + timedelta(days=3), "earnings_text": "soon", "short_float": 0.05, "short_ratio": 1.0}
        return {"earnings": today - timedelta(days=20), "earnings_text": "past", "short_float": 0.25, "short_ratio": 2.0}

    monkeypatch.setattr(ss, "fetch_snapshot", snapshot)
    sent = []
    monkeypatch.setattr(ss, "send_email", lambda to, subject, html: sent.append((to, subject, html)))
    rows["_sent"] = sent
    return rows


def test_main_returns_structured_results(offline, capsys):
    result = ss.main(tickers="PULL,BRK,NOPE")
    assert [q["ticker"] for q in result["results"]["pullback"]["qualified"]] == ["PULL"]
    assert [q["ticker"] for q in result["results"]["breakout"]["qualified"]] == ["BRK"]
    assert [q["ticker"] for q in result["results"]["squeeze"]["qualified"]] == ["BRK"]  # the breakout bar, short float 25%
    assert {r["ticker"] for r in result["results"]["pullback"]["rejected"]} == {"BRK"}
    assert all(r["failed"] for r in result["results"]["breakout"]["rejected"])
    assert result["data_errors"] == [{"ticker": "NOPE", "reason": "chart data is for another ticker"}]
    assert set(result["benchmarks"]) == {"QQQ"} and result["validation"] is None
    assert result["as_of"] == sessions(301)[-1].isoformat()
    market = result["market"]
    assert market["regime"] == "bullish" and market["long_setups"] == "long"
    assert market["breadth"]["above_sma200"] == 10 and market["breadth"]["sector_etfs"] == 11 and not market["breadth"]["etfs"]["XLE"]["above"]
    pull = result["results"]["pullback"]["qualified"][0]
    assert pull["direction"] == "long" and pull["action"] == "long" and pull["regime"] == "bullish" and pull["sector"] == "Technology"
    assert pull["relative_strength"]["sector"]["benchmark"] == "XLK" and pull["relative_strength"]["market"]["benchmark"] == "QQQ"
    assert set(pull["levels"]) >= {"entry", "stop", "target", "reward_risk"} and pull["score"] > 0
    assert isinstance(pull["warnings"], list)
    brk = result["results"]["breakout"]["qualified"][0]
    assert brk["sector"] is None and brk["relative_strength"]["sector"] == {"benchmark": None, "change": None}
    tracking = result["tracking"]
    assert tracking["days"] == 10 and set(tracking["summary"]) == set(ss.STRATEGIES) and isinstance(tracking["signals"], list)
    assert tracking["holding_days"]["min"] == 30 and tracking["holding_days"]["max"] == 45 and result["holding"]["beta_vs"] == "SPY"
    assert all(q["holding_days"] == ss.holding_days(q["beta"], result["settings"]) for s in ss.STRATEGIES for q in result["results"][s]["qualified"])
    assert result["candidates"][0]["time_exit"] == ss.add_sessions(date.fromisoformat(result["as_of"]), result["candidates"][0]["holding_days"]).isoformat()
    assert "Summary: 3 tickers, 1 data errors, regime bullish, qualified: pullback 1, breakout 1" in capsys.readouterr().out
    assert market["actions"] == {"pullback": "long", "breakout": "long", "squeeze": "long",
                                 "rally_rejection": "countertrend_watch", "breakdown": "countertrend_watch", "run_break": "countertrend_watch"}
    assert market["favoured"] == ["pullback", "breakout", "squeeze"] and market["short_setups"] == "countertrend_watch"
    assert market["phase"]["phase"] == "uptrend" and market["phase"]["decline"] is None and "Uptrend: long setups" in market["phase"]["advice"]
    assert set(market["context"]) == {"SPY", "IWM", "MDY", "SMH", "TLT", "HYG"} and market["context"]["TLT"]["trend"] == "up"
    assert market["benchmark"]["trend"] == "up" and market["benchmark"]["vs_sma200"] > 0 and market["benchmark"]["rsi14"] is not None
    assert market["sector_laggards"][0] == "XLE" and len(market["sector_leaders"]) == 3
    assert [(c["ticker"], c["strategy"]) for c in result["candidates"]] == [("BRK", "breakout"), ("PULL", "pullback"), ("BRK", "squeeze")]
    assert result["candidates"][0]["action"] == "long"
    assert [c["priority"] for c in result["candidates"]] == [1, 2, 3]
    assert result["candidates"][0]["recommendation"].startswith("BRK: PROPOSED LONG breakout")
    assert "email" not in offline["_sent"] and offline["_sent"] == []


def test_main_screens_squeeze_candidates(offline):
    result = ss.main(tickers="SQZ,SQLO", strategy="squeeze", track_days=0)
    (q,) = result["results"]["squeeze"]["qualified"]
    assert {x["name"] for x in q["scoring"] if x["earned"]} == {"market_not_hot", "outperforms_market", "sector_uptrend",
                                                                "sector_beats_market", "short_float_25"}
    assert q["ticker"] == "SQZ" and q["score"] == 55 and q["action"] == "long" and "short float 25.0%, 2.0 days to cover" in q["warnings"]
    (r,) = result["results"]["squeeze"]["rejected"]
    assert r["ticker"] == "SQLO" and [c["name"] for c in r["failed"]] == ["short_interest"]
    assert [(c["ticker"], c["strategy"]) for c in result["candidates"]] == [("SQZ", "squeeze")]


def test_tracked_squeeze_signals_need_short_float_today(offline):
    for ticker in ("SQZ", "SQLO"):
        offline[ticker] = offline[ticker] + [(186.5, 187.0, 186.0, 186.6, 1_000_000)] * 2
    for name in [k for k in offline if k not in ("SQZ", "SQLO", "_sent")]:
        offline[name] = offline[name] + [offline[name][-1]] * 2
    tracked = ss.main(tickers="SQZ,SQLO", strategy="squeeze", track_days=5)["tracking"]["signals"]
    assert [(x["ticker"], x["short_float_today"], x["status"]) for x in tracked] == [("SQZ", 0.25, "open")]


def test_main_screens_short_setups(offline):
    result = ss.main(tickers="RALY,BRKD,PULL", strategy="short")
    assert list(result["results"]) == ["rally_rejection", "breakdown", "run_break"]
    rally = result["results"]["rally_rejection"]["qualified"]
    assert [q["ticker"] for q in rally] == ["RALY"]
    q = rally[0]
    assert q["direction"] == "short" and q["action"] == "countertrend_watch" and q["sector_trend"] == "up"  # bullish market
    assert "squeeze risk: short float 25.0% (high)" in q["warnings"]
    brkd = {r["ticker"]: r["failed"] for r in result["results"]["breakdown"]["rejected"]}
    assert [c["name"] for c in brkd["BRKD"]] == ["no_earnings_soon"]  # qualified on price, blocked by earnings in 3 days
    assert [(c["ticker"], c["strategy"], c["action"]) for c in result["candidates"]] == [("RALY", "rally_rejection", "countertrend_watch")]


def test_main_emails_candidates_only_when_asked(offline):
    result = ss.main(tickers="PULL,RALY", email="trader@example.com")
    (to, subject, html), = offline["_sent"]
    assert to == "trader@example.com" and result["email"] == {"to": "trader@example.com", "subject": subject}
    assert subject.startswith(f"Strategy screen {result['as_of']}: market bullish, 2 setups")
    assert "PULL" in html and "RALY" in html and "Favoured: pullback, breakout, squeeze" in html and "TLT " in html
    assert "<b>Proposed</b>: 1 of 2 setups" in html and "PULL: PROPOSED LONG pullback" in html and "RALY: WATCH ONLY" in html
    with pytest.raises(ValueError, match="email"):
        ss.main(tickers="PULL", email="not an address")


def test_email_html_without_candidates():
    market = {"regime": "neutral", "favoured": [], "breadth": {"above_sma200": 5, "sector_etfs": 11},
              "benchmark": {"ticker": "QQQ", "close": 1.0, "vs_sma200": -0.02, "change_20d": None}}
    html = ss.email_html({"market": market, "candidates": []})
    assert "No setup qualified today" in html and "Favoured: none" in html and "-2.0%" in html


def test_main_single_strategy_and_validation(offline):
    result = ss.main(tickers="BRK", strategy="breakout", validation_days=20, settings={"swing_min_days": 20, "swing_max_days": 20})
    assert list(result["results"]) == ["breakout"] and set(result["validation"]["summary"]) == {"breakout"}
    validation = result["validation"]
    assert validation["cost_scenarios_bps"] == [0, 10, 25, 50] and validation["forward_days"] == [5, 10, 20]
    assert validation["earnings_data"]["tickers_with_dates"] == 1 and validation["earnings_data"]["tickers_without_dates"] == []
    assert validation["universe_base_rate"]["20d"]["n"] == 20 and validation["market_regime_benchmark"] == "QQQ"
    assert any("Survivorship" in a for a in validation["assumptions"])


def test_main_reports_missing_earnings_data(offline):
    result = ss.main(tickers="PULL", strategy="pullback", validation_days=10)
    assert result["validation"]["earnings_data"]["tickers_without_dates"] == ["PULL"]


def test_main_without_benchmarks_fails(offline, monkeypatch):
    with pytest.raises(RuntimeError, match="benchmark"):
        ss.main(tickers="PULL", settings={"benchmarks": ["NOPE"]})


@pytest.mark.parametrize("kwargs, message", [
    ({}, "Finviz screener URL or"),
    ({"tickers": "AAPL", "strategy": "scalp"}, "strategy"),
    ({"tickers": "AAPL", "max_tickers": 0}, "max_tickers"),
    ({"tickers": "AAPL", "validation_days": 5000}, "validation_days"),
    ({"tickers": "AAPL", "track_days": 61}, "track_days"),
    ({"tickers": "AAPL", "settings": {"context_etfs": "SPY"}}, "context_etfs"),
])
def test_main_rejects_bad_inputs(offline, kwargs, message):
    with pytest.raises(ValueError, match=message):
        ss.main(**kwargs)


def test_windmill_is_used_only_for_email_and_the_data_table():
    source = Path(ss.__file__).read_text()
    assert source.count("wmill.") == 2 and "wmill.get_resource(SMTP_RESOURCE)" in source and "wmill.datatable(name)" in source
    assert source.count("send_email(") == 2 and "if email.strip():" in source
    assert source.count("datatable_query()") == 1 and "datatable_query() if persist else None" in source


def test_requirements_header_is_pinned_for_ci():
    script = Path(ss.__file__).read_text().splitlines()
    header = {line.lstrip("#") for line in script[1:script.index("")]}
    pins = set((Path(__file__).resolve().parents[1] / "requirements-test.txt").read_text().split())
    assert header and header <= pins


def test_script_yaml_matches_main_signature():
    text = Path(ss.__file__).with_name("strategy_screen.script.yaml").read_text()
    order = text.split("order:\n")[1].split("  properties:")[0].split()
    assert [x for x in order if x != "-"] == list(inspect.signature(ss.main).parameters)


# ---------------------------------------------------------------- persistent tracking
#
# The SQL runs against a real PostgreSQL (the runner's, or pgserver's binaries locally) through an adapter that executes like Windmill's postgresql
# executor: statements split on ";" outside quotes and dollar quotes, run one by one in autocommit, each binding only the
# $n it uses (renumbered), lists and dicts sent as JSON, and the rows of the last statement returned as JSON.


def split_sql(sql):
    out, start, i, quote = [], 0, 0, None
    while i < len(sql):
        if quote:
            if sql.startswith(quote, i):
                i += len(quote)
                quote = None
            else:
                i += 1
            continue
        tag = re.match(r"\$[A-Za-z_]*\$", sql[i:])
        if sql[i] == "'" or tag:
            quote = "'" if sql[i] == "'" else tag.group(0)
            i += len(quote)
            continue
        if sql[i] == ";":
            out.append(sql[start:i + 1])
            start = i + 1
        i += 1
    if sql[start:].strip():
        out.append(sql[start:])
    return [x for x in out if x.strip(" \n;")]


class WindmillLike:
    def __init__(self, conn):
        self.conn, self.calls = conn, []

    def __call__(self, sql, *args):
        from psycopg.types.json import Json
        self.calls.append(sql)
        rows = []
        for statement in split_sql(sql):
            used = sorted({int(x) for x in re.findall(r"\$(\d+)", statement)})
            number = {old: k + 1 for k, old in enumerate(used)}
            text = re.sub(r"\$(\d+)", lambda m: f"${number[int(m.group(1))]}", statement)
            params = [Json(args[old - 1]) if isinstance(args[old - 1], (list, dict)) else args[old - 1] for old in used]
            cursor = self.conn.execute(text, params or None)
            rows = cursor.fetchall() if cursor.description else []
        return json.loads(json.dumps(rows, default=str))

    def one(self, sql, *params):
        return self.conn.execute(sql, params or None).fetchone()


def postgres_bin():
    """A PostgreSQL bin directory: PG_BIN, the newest /usr/lib/postgresql/*/bin (GitHub runners ship one), or pgserver."""
    candidates = [os.environ.get("PG_BIN", "")]
    candidates += sorted(glob.glob("/usr/lib/postgresql/*/bin"), key=lambda p: int(re.sub(r"\D", "", p) or 0), reverse=True)
    try:
        import pgserver
        candidates.append(str(Path(pgserver.__file__).parent / "pginstall" / "bin"))
    except ImportError:
        pass
    for path in candidates:
        if path and Path(path, "initdb").exists() and Path(path, "pg_ctl").exists():
            return Path(path)
    return None


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory):
    pytest.importorskip("psycopg")
    bin_dir = postgres_bin()
    if bin_dir is None:
        pytest.skip("no PostgreSQL binaries (set PG_BIN)")
    root = tmp_path_factory.mktemp("pg")
    data, sockets = root / "data", Path(tempfile.mkdtemp(prefix="pg"))  # a short path: socket paths are limited to 107 bytes
    subprocess.run([bin_dir / "initdb", "-D", data, "-U", "postgres", "--auth=trust", "--no-sync", "-E", "UTF8"],
                   check=True, capture_output=True)
    subprocess.run([bin_dir / "pg_ctl", "-D", data, "-l", root / "log", "-w", "-o",
                    f"-k {sockets} -c listen_addresses='' -c fsync=off", "start"], check=True, capture_output=True)
    yield f"host={sockets} user=postgres"
    subprocess.run([bin_dir / "pg_ctl", "-D", data, "-m", "immediate", "stop"], capture_output=True)


@pytest.fixture
def db(pg_server):
    import psycopg
    from psycopg.rows import dict_row
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(f"{pg_server} dbname=postgres", autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    conn = psycopg.connect(f"{pg_server} dbname={name}", autocommit=True, cursor_factory=psycopg.RawCursor, row_factory=dict_row)
    yield WindmillLike(conn)
    conn.close()


def test_split_sql_keeps_function_bodies_whole():
    parts = split_sql(ss.LOAD_SQL)
    body = [x for x in parts if "guard_signal() RETURNS" in x]
    assert len(body) == 1 and "RETURN NEW;" in body[0] and body[0].rstrip().endswith("$$;")
    assert parts[-1].strip().startswith("SELECT * FROM screener.signals")


def stored_row(**over):
    row = {"strategy": "pullback", "ticker": "AAA", "signal_date": "2026-11-02", "direction": "long", "live": True, "score": 60,
           "regime": "bullish", "sector": "Technology", "sector_trend": "up", "action": "long", "entry": 10.0, "stop": 9.0,
           "target": 12.0, "reward_risk": 2.0, "atr14": 0.5, "signal_close": 9.9, "beta": 1.2, "holding_days": 3,
           "original": {"warnings": []}, **{k: None for k in ss.STATE_FIELDS}, "status": "pending", "price_factor": 1.0,
           "data_status": "ok", "last_data_date": "2026-11-02", "stale_days": 0}
    row.update(over)
    return row


def write(db, new=(), updates=(), as_of="2026-11-17"):
    return db(ss.WRITE_SQL, list(new), list(updates), as_of, "neutral", 10, 1, {"regime": "neutral"}, [{"ticker": "AAA"}])[0]


def test_schema_is_idempotent_and_loads_active_signals(db):
    assert db(ss.LOAD_SQL) == [] and db(ss.LOAD_SQL) == []
    assert write(db, [stored_row(), stored_row(ticker="BBB", status="not_triggered")]) == {"inserted": 2, "updated": 0, "run_id": 1}
    loaded = db(ss.LOAD_SQL)
    assert [(r["ticker"], r["closed_at"] is None) for r in loaded] == [("AAA", True), ("BBB", False)]
    active = loaded[0]
    assert active["ticker"] == "AAA" and active["signal_date"] == "2026-11-02" and active["original"] == {"warnings": []}
    assert active["closed_at"] is None and active["id"] == 1
    assert db.one("SELECT closed_at IS NOT NULL AS closed FROM screener.signals WHERE ticker = 'BBB'")["closed"]


def test_reruns_insert_nothing_twice(db):
    db(ss.LOAD_SQL)
    assert write(db, [stored_row()])["inserted"] == 1
    changed = stored_row(score=99, entry=11.0)  # same key: the original row wins
    assert write(db, [changed])["inserted"] == 0
    row = db.one("SELECT score, entry FROM screener.signals")
    assert row == {"score": 60, "entry": 10.0}
    assert db.one("SELECT count(*) AS n, max(inserted) AS i FROM screener.runs") == {"n": 2, "i": 1}


def update_for(row_id, **state):
    return {"id": row_id, **{k: None for k in ss.STATE_FIELDS}, "price_factor": 1.0, "data_status": "ok", "stale_days": 0,
            "last_data_date": "2026-11-17", **state}


def test_active_signals_update_until_closed_then_freeze(db):
    db(ss.LOAD_SQL)
    write(db, [stored_row()])
    assert write(db, updates=[update_for(1, status="open", fill=10.0, fill_date="2026-11-03", r_now=0.4, bars_held=2)])["updated"] == 1
    assert db.one("SELECT status, r_now, closed_at FROM screener.signals") == {"status": "open", "r_now": 0.4, "closed_at": None}
    closing = update_for(1, status="target", fill=10.0, exit=12.0, exit_date="2026-11-05", r=2.0, bars_held=3)
    assert write(db, updates=[closing])["updated"] == 1
    assert db.one("SELECT closed_at IS NOT NULL AS closed, r FROM screener.signals") == {"closed": True, "r": 2.0}
    assert write(db, updates=[update_for(1, status="stopped", r=-1.0)])["updated"] == 0  # a closed signal is skipped
    assert [r["closed_at"] is not None for r in db(ss.LOAD_SQL)] == [True]
    write(db, [stored_row(ticker="OLD", signal_date="2020-01-02", status="stopped", exit_date="2020-01-10", r=-1.0)])
    assert [r["ticker"] for r in db(ss.LOAD_SQL)] == ["AAA"]
    assert db.one("SELECT status, r FROM screener.signals") == {"status": "target", "r": 2.0}


@pytest.mark.parametrize("statement, message", [
    ("UPDATE screener.signals SET r = 5 WHERE ticker = 'BBB'", "is closed and cannot change"),
    ("UPDATE screener.signals SET entry = 5 WHERE ticker = 'AAA'", "original fields"),
    ("UPDATE screener.signals SET original = '{}' WHERE ticker = 'AAA'", "original fields"),
    ("UPDATE screener.signals SET signal_date = '2026-11-03' WHERE ticker = 'AAA'", "original fields"),
    ("DELETE FROM screener.signals WHERE ticker = 'AAA'", "cannot be deleted"),
    ("TRUNCATE screener.signals", "cannot be truncated"),
    ("UPDATE screener.signals SET status = 'won' WHERE ticker = 'AAA'", "check constraint"),
    ("UPDATE screener.signals SET status = 'target' WHERE ticker = 'AAA'", "check constraint"),  # closed needs closed_at
])
def test_database_refuses_changes_to_history(db, statement, message):
    import psycopg
    db(ss.LOAD_SQL)
    write(db, [stored_row(original={"warnings": ["x"]}), stored_row(ticker="BBB", status="stopped", r=-1.0, exit_date="2026-11-04")])
    with pytest.raises(psycopg.Error, match=message):
        db.conn.execute(statement)
    assert db.one("SELECT count(*) AS n FROM screener.signals")["n"] == 2
    db.conn.execute("UPDATE screener.signals SET r_now = 0.5 WHERE ticker = 'AAA'")  # the trade state of an active signal may change


def test_stats_per_strategy_and_live_flag(db):
    db(ss.LOAD_SQL)
    write(db, [stored_row(ticker="A1", status="target", r=2.0, exit_date="2026-11-05"),
               stored_row(ticker="A2", status="stopped", r=-1.0, exit_date="2026-11-05"),
               stored_row(ticker="A3", status="not_triggered"),
               stored_row(ticker="A4"),
               stored_row(ticker="B1", live=False, status="stopped", r=-1.0, exit_date="2026-11-04")])
    live, recomputed = db(ss.STATS_SQL)
    assert live == {"strategy": "pullback", "live": True, "signals": 4, "active": 1, "not_filled": 1, "closed_trades": 2, "winners": 1,
                    "average_r": 0.5, "total_r": 1.0, "data_unavailable": 0}
    assert recomputed["live"] is False and recomputed["closed_trades"] == 1 and recomputed["average_r"] == -1.0


# ---------------------------------------------------------------- stored signal state (no database)

CAL = sessions(10, date(2026, 11, 2))


def stored_bars(rows, start=date(2026, 11, 2)):
    return make_bars(rows, "AAA", start)


def test_stored_update_follows_the_trade_from_pending_to_target():
    row = {**stored_row(), "id": 7}
    bars = stored_bars([(9.8, 10, 9.6, 9.9, 1)])
    assert ss.stored_update(row, bars, CAL, CAL[0])["status"] == "pending"
    bars = stored_bars([(9.8, 10, 9.6, 9.9, 1), (10.1, 10.5, 9.9, 10.4, 1)])
    state = ss.stored_update(row, bars, CAL, CAL[1])
    assert (state["status"], state["fill"], state["r_now"], state["bars_left"], state["data_status"]) == ("open", 10.1, 0.273, 2, "ok")
    assert set(state) == {"id", *ss.STATE_FIELDS} and state["last_data_date"] == CAL[1].isoformat() and state["stale_days"] == 0
    bars = stored_bars([(9.8, 10, 9.6, 9.9, 1), (10.1, 10.5, 9.9, 10.4, 1), (10.5, 12.5, 10.4, 12.2, 1)])
    done = ss.stored_update(row, bars, CAL, CAL[2])
    assert (done["status"], done["exit"], done["r"], done["r_now"], done["bars_left"]) == ("target", 12.0, round(1.9 / 1.1, 3), None, None)


def test_stored_update_time_exit_uses_the_stored_holding_days():
    row = {**stored_row(holding_days=2), "id": 1}
    bars = stored_bars([(9.8, 10, 9.6, 9.9, 1), (10.1, 10.5, 9.9, 10.4, 1), (10.4, 10.8, 10.2, 10.6, 1)])
    state = ss.stored_update(row, bars, CAL, CAL[2])
    assert state["status"] == "time_exit" and state["exit"] == 10.6 and state["bars_held"] == 2


def test_stored_update_keeps_original_prices_after_a_split():
    row = {**stored_row(), "id": 1}
    rows = [(9.8, 10, 9.6, 9.9, 1), (10.1, 10.5, 9.9, 10.4, 1)]
    split = stored_bars([tuple(x / 2 for x in r[:4]) + (r[4] * 2,) for r in rows])  # 2:1 split adjusts the whole history
    state = ss.stored_update(row, split, CAL, CAL[1])
    assert state["price_factor"] == 0.5 and "adjusted by 0.5000" in state["data_note"]
    assert (state["status"], state["fill"], state["last_close"], state["r_now"]) == ("open", 10.1, 10.4, 0.273)


def test_stored_update_marks_missing_data_without_closing():
    row = {**stored_row(status="open", fill=10.1, fill_date="2026-11-03", r_now=0.3, last_data_date="2026-11-04"), "id": 3}
    state = ss.stored_update(row, None, CAL, CAL[6], "chart data is for another ticker")
    assert (state["status"], state["fill"], state["r_now"]) == ("open", 10.1, 0.3)
    assert (state["data_status"], state["data_note"], state["stale_days"], state["last_data_date"]) == (
        "unavailable", "chart data is for another ticker", 4, "2026-11-04")
    late = ss.stored_update(row, stored_bars([(1, 1, 1, 1, 1)], date(2026, 11, 9)), CAL, CAL[6])
    assert late["data_status"] == "unavailable" and "not in the price history" in late["data_note"] and late["status"] == "open"


def test_stored_update_short_signal():
    row = {**stored_row(strategy="breakdown", direction="short", entry=10.0, stop=11.0, target=8.0, signal_close=10.2), "id": 2}
    bars = stored_bars([(10.3, 10.4, 10.1, 10.2, 1), (9.9, 10.0, 7.9, 8.1, 1)])
    state = ss.stored_update(row, bars, CAL, CAL[1])
    assert (state["status"], state["fill"], state["exit"], state["r"]) == ("target", 9.9, 8.0, round(1.9 / 1.1, 3))


def test_select_new_one_trade_per_key_at_a_time():
    stored = [{**stored_row(status="open"), "id": 1}, {**stored_row(ticker="BBB", status="open"), "id": 2},
              {**stored_row(ticker="DDD", signal_date="2026-10-20", status="target", exit_date="2026-10-28"), "id": 3}]
    updates = [{"id": 2, "status": "stopped", "exit_date": "2026-11-06"}]
    candidates = [
        stored_row(signal_date="2026-11-02"),                    # the stored signal itself: already known
        stored_row(ticker="DDD", signal_date="2026-10-20"),      # a stored closed signal: known
        stored_row(ticker="DDD", signal_date="2026-10-27"),      # inside its trade: busy
        stored_row(signal_date="2026-10-30", status="not_triggered"),  # before the open AAA signal: allowed
        stored_row(signal_date="2026-11-05"),                    # AAA still open: busy
        stored_row(ticker="BBB", signal_date="2026-11-06"),      # BBB closed on its exit date: busy that day
        stored_row(ticker="BBB", signal_date="2026-11-09"),      # after the exit: new
        stored_row(ticker="BBB", signal_date="2026-11-10"),      # the new BBB signal is pending: busy
        stored_row(ticker="CCC", signal_date="2026-11-03", status="not_triggered"),
        stored_row(ticker="CCC", signal_date="2026-11-04"),      # the previous one never filled: new
        stored_row(strategy="breakout", signal_date="2026-11-05"),  # another strategy: new
    ]
    keep, skipped = ss.select_new(stored, updates, candidates)
    assert [(r["strategy"], r["ticker"], r["signal_date"]) for r in keep] == [
        ("pullback", "AAA", "2026-10-30"), ("pullback", "CCC", "2026-11-03"), ("pullback", "CCC", "2026-11-04"),
        ("breakout", "AAA", "2026-11-05"), ("pullback", "BBB", "2026-11-09")]
    assert [(r["ticker"], r["signal_date"]) for r in skipped] == [
        ("DDD", "2026-10-27"), ("AAA", "2026-11-05"), ("BBB", "2026-11-06"), ("BBB", "2026-11-10")]


def test_new_row_from_a_live_signal_and_a_recomputed_one():
    live = run(ss.evaluate_pullback, pullback_rows())
    item = {**live, "action": "watch", "regime": "neutral", "sector": "Technology", "sector_trend": "up", "sector_etf": "XLK",
            "warnings": ["w"], "beta": 1.3, "holding_days": 40}
    row = ss.new_row(item, True, "2026-11-17")
    assert set(row) == set(ss.ORIGINAL_FIELDS) | set(ss.STATE_FIELDS)
    assert row["live"] and row["status"] == "pending" and row["signal_close"] == pytest.approx(pullback_rows()[-1][3])
    assert row["entry"] == live["levels"]["entry"] and row["atr14"] == live["indicators"]["atr14"]
    assert row["original"]["warnings"] == ["w"] and row["original"]["sector_etf"] == "XLK" and len(row["original"]["scoring"]) == len(live["scoring"])
    json.dumps(row)
    tracked = {"strategy": "breakout", "direction": "long", "ticker": "BBB", "signal_date": "2026-11-10", "score": 70, "regime": "bullish",
               "sector_trend": "up", "action": "long", "beta": None, "holding_days": 38, "atr14": 1.5, "signal_close": 50.0, "entry": 50.0,
               "stop": 48.0, "target": 56.0, "risk_per_share": 2.0, "reward_risk": 3.0, "status": "stopped", "fill": 50.2,
               "fill_date": "2026-11-11", "exit": 48.0, "exit_date": "2026-11-12", "r": -1.1, "bars_held": 2, "gap": False,
               "ambiguous": None, "sector": "Energy"}
    old = ss.new_row(tracked, False, "2026-11-17")
    assert not old["live"] and old["status"] == "stopped" and old["r"] == -1.1 and old["sector"] == "Energy"
    assert "not applied" in old["original"]["note"]


# ---------------------------------------------------------------- main with persist


def test_main_persists_and_follows_signals(offline, db, monkeypatch):
    monkeypatch.setattr(ss, "datatable_query", lambda name=ss.DATA_TABLE: db)
    first = ss.main(tickers="PULL,BRK", track_days=0, persist=True)["persistence"]
    assert first["inserted"] == len(first["new"]) > 0 and first["updated"] == 0 and first["run_id"] == 1
    assert {(r["ticker"], r["status"]) for r in first["new"]} >= {("PULL", "pending"), ("BRK", "pending")}
    again = ss.main(tickers="PULL,BRK", track_days=0, persist=True)["persistence"]
    assert again["inserted"] == 0 and again["updated"] == first["inserted"] and again["new"] == []
    stored = db(ss.LOAD_SQL)
    assert len(stored) == first["inserted"] and all(r["live"] and r["status"] == "pending" for r in stored)
    assert again["skipped_busy"] == first["skipped_busy"] == []

    # A new session: PULL left the screen and its chart cannot be loaded, BRK is still there.
    for name in [k for k in offline if k != "_sent"]:
        offline[name] = offline[name] + [offline[name][-1]]
    pull_rows = offline.pop("PULL")
    later = ss.main(tickers="BRK", track_days=0, persist=True)
    stored = {(r["ticker"], r["strategy"]): r for r in db(ss.LOAD_SQL) if r["closed_at"] is None}
    assert stored[("PULL", "pullback")]["data_status"] == "unavailable" and stored[("PULL", "pullback")]["status"] == "pending"
    assert stored[("PULL", "pullback")]["stale_days"] == 1
    assert [r["ticker"] for r in later["persistence"]["data_unavailable"]] == ["PULL"]

    # The data comes back: PULL is followed again even though it is not in the screen.
    offline["PULL"] = pull_rows + [pull_rows[-1]]
    ss.main(tickers="BRK", track_days=0, persist=True)
    pull = db.one("SELECT status, data_status, stale_days FROM screener.signals WHERE ticker = 'PULL' AND strategy = 'pullback'")
    assert pull["data_status"] == "ok" and pull["stale_days"] == 0 and pull["status"] != "pending"
    assert db.one("SELECT count(*) AS n FROM screener.runs")["n"] == 4


def test_main_backfills_recomputed_signals_as_not_live(offline, db, monkeypatch):
    monkeypatch.setattr(ss, "datatable_query", lambda name=ss.DATA_TABLE: db)
    for name in [k for k in offline if k != "_sent"]:
        offline[name] = offline[name] + [offline[name][-1]] * 2
    out = ss.main(tickers="PULL,BRK", track_days=5, persist=True)["persistence"]
    rows = db.conn.execute("SELECT ticker, strategy, live, status FROM screener.signals ORDER BY ticker, strategy").fetchall()
    assert rows and all(not r["live"] for r in rows) and out["inserted"] == len(rows)
    assert {r["ticker"] for r in rows} == {"PULL", "BRK"}
    again = ss.main(tickers="PULL,BRK", track_days=5, persist=True)["persistence"]
    assert again["inserted"] == 0 and again["skipped_busy"] == []


def test_main_without_persist_never_touches_the_data_table(offline, monkeypatch):
    monkeypatch.setattr(ss, "datatable_query", lambda name=ss.DATA_TABLE: pytest.fail("data table used"))
    assert "persistence" not in ss.main(tickers="PULL", track_days=0)


def test_email_lists_tracked_signals():
    market = {"regime": "neutral", "favoured": [], "breadth": {"above_sma200": 5, "sector_etfs": 11},
              "benchmark": {"ticker": "QQQ", "close": 1.0, "vs_sma200": -0.02, "change_20d": None}}
    persistence = {"active": [{}], "inserted": 2, "closed_today": [{"ticker": "AAA", "strategy": "pullback", "status": "target", "r": 2.0}],
                   "data_unavailable": [{"ticker": "ZZZ"}]}
    html = ss.email_html({"market": market, "candidates": [], "persistence": persistence})
    assert "1 active, 2 new" in html and "AAA pullback target +2.00R" in html and "Data unavailable: ZZZ" in html


def run_break_rows():
    """A 74% run to 250, a flat top that lets EMA50 catch up, then the first close below EMA50 on a bearish bar."""
    return uptrend(200, 50, 1.0) + [(249.8, 251, 249, 250, 1_000_000)] * 60 + [(250, 250.5, 247, 247.5, 1_000_000)]


def test_run_break_first_close_below_ema50_after_a_run():
    bars = make_bars(run_break_rows())
    series = ss.compute(bars, {}, CFG)
    t = len(bars) - 1
    signal = ss.evaluate_run_break(bars, series, t, CFG)
    assert signal["qualified"] and signal["direction"] == "short" and signal["score"] == 0
    assert signal["levels"]["entry"] == 247.5 and signal["levels"]["reward_risk"] == 2.0
    assert signal["levels"]["stop"] == round(251 + 0.2 * series["atr14"][t], 2)
    assert signal["indicators"]["prior_run"] > 0.7 and "+74%" in ss._setup_text(signal)
    failed = [c["name"] for c in ss.evaluate_run_break(bars, series, t - 1, CFG)["checks"] if not c["passed"]]
    assert failed == ["first_close_below_ema50", "bearish_bar"]
    small = make_bars(uptrend(200, 200, 0.2) + [(249.8, 251, 249, 250, 1_000_000)] * 60 + [(250, 250.5, 247, 247.5, 1_000_000)])
    weak = ss.evaluate_run_break(small, ss.compute(small, {}, CFG), t, CFG)
    assert [c["name"] for c in weak["checks"] if not c["passed"]] == ["prior_run"]


def phase_rows():
    """Steady rise, a 10-day drop of 3 a day (correction), a bounce, then a new rise."""
    rows, close = uptrend(260, 100, 1.0), 359.0
    for _ in range(10):
        close -= 3
        rows.append((close + 2.5, close + 3, close - 0.5, close, 1_000_000))
    for _ in range(4):
        close += 3
        rows.append((close - 2.5, close + 0.5, close - 3, close, 1_000_000))
    for _ in range(12):
        close += 2
        rows.append((close - 1.5, close + 0.5, close - 2, close, 1_000_000))
    return rows


def test_market_phase_switches_and_advice():
    bars = make_bars(phase_rows(), "QQQ")
    phases = ss.market_phase(bars, {}, CFG)
    at = lambda i: phases[bars.dates[i]]
    assert phases[bars.dates[100]] is None  # not enough history
    assert at(259)["phase"] == "uptrend" and at(259)["decline"] is None and at(259)["drawdown"] == 0
    assert at(262)["phase"] == "pullback" and at(262)["switched"] and at(262)["previous"] == "uptrend"
    assert at(263)["phase"] == "pullback" and not at(263)["switched"] and at(263)["since"] == bars.dates[262].isoformat()
    assert at(265)["decline"] == "correction" and at(265)["phase"] == "above_ema50"  # 5% off the high, still above EMA50
    assert at(267)["phase"] == "falling" and at(269)["drawdown"] == round(329 / 359 - 1, 4) and not at(269)["deep"]
    assert at(271)["phase"] == "bounce" and at(271)["previous"] == "falling" and at(271)["bounce_atr"] >= 2
    assert at(len(bars) - 1)["phase"] == "uptrend" and at(len(bars) - 1)["decline"] is None
    text = ss.phase_advice(at(271), CFG)
    assert text.startswith("SWITCH today from falling to bounce (-6.7% from the ") and ", correction)" in text
    assert "dead cat" in text and "do not short the bounce" in text
    assert ss.phase_advice(at(263), CFG).startswith(f"Phase pullback since {bars.dates[262].isoformat()} (after uptrend)")
    assert ss.phase_advice(None) .startswith("Market phase unknown")
    # a bearish regime is a decline whatever the drawdown; no run and no extension means no correction
    assert ss.market_phase(bars, {bars.dates[259]: "bearish"}, CFG)[bars.dates[259]]["decline"] == "bear"
    flat = ss.market_phase(bars, {}, {**CFG, "correction_run": 5.0, "correction_extension": 5.0})
    assert flat[bars.dates[269]]["decline"] is None and flat[bars.dates[269]]["phase"] == "pullback"


def test_bull_put_spread_plan_and_recommendation():
    phase = {"decline": "correction", "deep": True}
    plan = ss.put_spread_plan(100.0, "2026-03-02", phase, CFG)
    assert plan["legs"] == [{"sell": 100.0, "buy": 95.0}, {"sell": 95.0, "buy": 90.0}]
    assert plan["expiry_after"] == "2026-03-09" and plan["exit_by"] == "2026-03-02" and plan["take_profit"] == 0.5
    q = {"ticker": "RALY", "strategy": "rally_rejection", "direction": "short", "action": "bull_put_spread", "regime": "neutral",
         "levels": {"entry": 99.0, "stop": 104.0, "target": 90.0, "reward_risk": 1.8}, "scoring": [], "indicators": {},
         "holding_days": 38, "time_exit": "2026-03-02", "put_spread": plan}
    text = ss.recommendation(q)
    assert text.startswith("RALY: PROPOSED BULL PUT SPREAD (first bull put spread trigger in a decline")
    assert "sell the 100.0 put and buy the 95.0 put (or 95.0 / 90.0 for more room), expiry after 2026-03-09" in text
    assert "close at 50% of the credit or by 2026-03-02" in text and "deep in the decline" in text and "stop 104.0" not in text


def test_main_proposes_bull_put_spreads_in_a_correction(offline):
    offline["QQQ"] = uptrend(289, 200, 0.5) + [(343 - 1.8 * k, 344 - 1.8 * k, 341 - 1.8 * k, 342 - 1.8 * k, 1_000_000) for k in range(12)]
    result = ss.main(tickers="RALY,PULL", strategy="all", email="trader@example.com")
    market = result["market"]
    assert market["phase"]["decline"] == "correction" and market["phase"]["drawdown"] <= -0.05
    assert market["short_setups"] == "bull_put_spread" and market["actions"]["breakdown"] == "bull_put_spread"
    assert market["actions"]["pullback"] == "long" and "rally_rejection" in market["favoured"]
    (q,) = result["results"]["rally_rejection"]["qualified"]
    assert q["action"] == "bull_put_spread" and q["sector_trend"] == "up"  # the sector does not veto the spread
    assert q["put_spread"]["decline"] == "correction" and len(q["put_spread"]["legs"]) == 2
    row = [c for c in result["candidates"] if c["ticker"] == "RALY"][0]
    assert row["put_spread"] == q["put_spread"] and "PROPOSED BULL PUT SPREAD" in row["recommendation"]
    assert result["results"]["pullback"]["qualified"][0]["put_spread"] is None
    html = offline["_sent"][0][2]
    assert "<b>Market phase " in html and "RALY: PROPOSED BULL PUT SPREAD" in html and "<b>Proposed</b>: 2 of 2 setups" in html


def test_settings_check_run_break_and_phase_windows():
    with pytest.raises(ValueError, match="120 or less"):
        ss.settings_with_defaults({"phase_high_bars": 200})
    cfg = ss.settings_with_defaults({"bounce_low_bars": 5.0, "put_spread_expiry_days": 3.0})
    assert cfg["bounce_low_bars"] == 5 and isinstance(cfg["put_spread_expiry_days"], int)
