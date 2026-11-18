#requirements:
#finviz==2.1.0
#requests==2.34.2

"""Two-strategy screener: Pullback / EMA bounce and Breakout / momentum.

Finviz finds the candidates. Daily OHLCV comes from the Finviz chart endpoint
(split adjusted, same host as the screener, no API key). Every indicator is
computed here from completed daily bars only, so a signal on day t uses no data
after day t. The script sends no email and changes nothing; it returns JSON.

Every LONG setup carries its entry, stop, target, reward/risk, score, relative strength vs QQQ and
its sector ETF, and the market regime (bullish / neutral / bearish from QQQ's trend and sector-ETF
breadth), which turns it into an action: long, watch or no_trade. `track_days` adds the status of the
signals of the previous sessions (open, target, stopped, time exit, not triggered).
"""

import bisect
import json
import math
import re
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from finviz.screener import NoResults, Screener

FINVIZ_HOSTS = {"finviz.com", "www.finviz.com"}
CHART_URL = "https://finviz.com/api/quote"
QUOTE_URL = "https://finviz.com/quote.ashx"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
STRATEGIES = ("pullback", "breakout")
# A daily bar counts as final this long after the 16:00 New York close (Finviz free data is delayed).
SESSION_FINAL_AFTER = (16, 20)
MAX_TICKERS = 200
MAX_VALIDATION_DAYS = 750
MAX_TRACK_DAYS = 60
# Finviz sector -> SPDR sector ETF, used for relative strength vs sector and for market breadth.
SECTOR_ETFS = {"Technology": "XLK", "Financial": "XLF", "Healthcare": "XLV", "Industrials": "XLI", "Consumer Cyclical": "XLY",
               "Consumer Defensive": "XLP", "Energy": "XLE", "Utilities": "XLU", "Basic Materials": "XLB",
               "Real Estate": "XLRE", "Communication Services": "XLC"}
# What a LONG setup means in each market regime. Short setups are not screened yet.
ACTIONS = {"bullish": "long", "neutral": "watch", "bearish": "no_trade", "unknown": "watch"}

DEFAULTS = {
    "benchmarks": ["QQQ"],
    "min_bars": 260,
    "min_price": 5.0,
    "min_dollar_volume": 10_000_000.0,
    "max_missing_sessions": 2,
    "max_zero_volume_days": 2,
    "rs_lookback": 63,
    "max_risk_atr": 2.5,
    # Pullback / EMA bounce
    "pullback_min_atr": 1.0,
    "touch_atr": 0.25,
    "reversal_close_location": 0.5,
    "pullback_stop_atr": 0.2,
    "pullback_min_rr": 1.5,
    # Breakout / momentum
    "breakout_lookback": 50,
    "min_base_bars": 5,
    "min_touches": 2,
    "resistance_touch_atr": 0.5,
    "max_base_depth": 0.30,
    "max_extension_atr": 1.0,
    "min_rel_volume": 1.5,
    "breakout_stop_atr": 0.5,
    "breakout_min_rr": 2.0,
    # Market regime: QQQ vs its SMA200 and the SMA200 slope, plus breadth = share of sector ETFs above their SMA200
    "regime_slope_bars": 20,
    "breadth_bullish": 0.6,
    "breadth_bearish": 0.4,
    # Validation and tracking
    "validation_horizon": 20,
    "request_pause": 0.4,
}


# ---------------------------------------------------------------- data


class Bars:
    """Daily bars, oldest first."""

    def __init__(self, ticker, dates, open_, high, low, close, volume):
        self.ticker = ticker
        self.dates, self.open, self.high, self.low, self.close, self.volume = dates, open_, high, low, close, volume

    def __len__(self):
        return len(self.dates)

    def head(self, n):
        return Bars(self.ticker, self.dates[:n], self.open[:n], self.high[:n], self.low[:n], self.close[:n], self.volume[:n])


def parse_chart(payload, ticker):
    if not isinstance(payload, dict) or payload.get("ticker", "").upper() != ticker:
        raise ValueError("chart data is for another ticker")
    columns = [payload.get(k) for k in ("date", "open", "high", "low", "close", "volume")]
    if not all(isinstance(column, list) for column in columns) or len({len(column) for column in columns}) != 1:
        raise ValueError("chart data has missing or uneven columns")
    stamps, open_, high, low, close, volume = columns
    # Finviz stamps each daily bar during that session (13:00 or 14:00 UTC).
    dates = [datetime.fromtimestamp(stamp, timezone.utc).date() for stamp in stamps]
    if any(later <= earlier for earlier, later in zip(dates, dates[1:])):
        raise ValueError("chart dates are not strictly increasing")
    numbers = [[float(x) if x is not None else math.nan for x in column] for column in (open_, high, low, close, volume)]
    return Bars(ticker, dates, *numbers)


def _get(url, params, retries=3):
    for attempt in range(retries + 1):
        response = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=20)
        if response.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(2 ** attempt)
            continue
        response.raise_for_status()
        return response


def fetch_bars(ticker, count):
    response = _get(CHART_URL, {"instrument": "stock", "ticker": ticker, "timeframe": "d", "barsCount": count})
    return parse_chart(response.json(), ticker)


def parse_earnings(html):
    """Earnings report times (UTC) from the chart events embedded in a Finviz quote page."""
    marker = '"chartEvents":'
    start = html.find(marker + "[")
    if start < 0:
        raise ValueError("the quote page has no chart events")
    events, _ = json.JSONDecoder().raw_decode(html[start + len(marker):])
    return sorted(datetime.fromtimestamp(e["dateTimestamp"], timezone.utc) for e in events
                  if isinstance(e, dict) and e.get("eventType") == "chartEvent/earnings" and isinstance(e.get("dateTimestamp"), (int, float)))


def parse_sector(html):
    """Finviz sector name from a quote page, or None."""
    match = re.search(r'f=sec_[a-z]+"[^>]*>([^<]+)<', html)
    return match.group(1).strip() if match and match.group(1).strip() in SECTOR_ETFS else None


def fetch_sector(ticker):
    return parse_sector(_get(QUOTE_URL, {"t": ticker, "p": "d"}).text)


def fetch_earnings(ticker):
    # The weekly chart view carries about ten years of report dates; the daily view only two.
    return parse_earnings(_get(QUOTE_URL, {"t": ticker, "p": "w"}).text)


def new_york_time(now):
    """UTC to US Eastern wall time, with US daylight saving rules (no tz database needed)."""
    year = now.year
    march_8, november_1 = date(year, 3, 8), date(year, 11, 1)
    dst_start = march_8 + timedelta(days=(6 - march_8.weekday()) % 7)
    dst_end = november_1 + timedelta(days=(6 - november_1.weekday()) % 7)
    start = datetime(year, 3, dst_start.day, 7, tzinfo=timezone.utc)
    end = datetime(year, 11, dst_end.day, 6, tzinfo=timezone.utc)
    return now + timedelta(hours=-4 if start <= now < end else -5)


def completed_bars(bars, now):
    """Drop today's bar until the session is over, so a signal never uses a partial day."""
    local = new_york_time(now)
    keep = len(bars)
    while keep and (bars.dates[keep - 1] > local.date() or (
            bars.dates[keep - 1] == local.date() and (local.hour, local.minute) < SESSION_FINAL_AFTER)):
        keep -= 1
    return bars.head(keep) if keep != len(bars) else bars


def quality_problems(bars, calendar, cfg):
    """Reasons the data cannot be trusted. calendar: sorted session dates from the benchmark."""
    n = cfg["min_bars"]
    if len(bars) < n:
        return [f"only {len(bars)} daily bars, need {n}"]
    problems = []
    # Every bar, not only recent ones: one bad value would poison the running averages after it.
    for i in range(len(bars)):
        o, h, l, c, v = bars.open[i], bars.high[i], bars.low[i], bars.close[i], bars.volume[i]
        if not all(math.isfinite(x) for x in (o, h, l, c, v)) or min(o, h, l, c) <= 0 or v < 0:
            problems.append(f"missing or invalid values on {bars.dates[i]}")
            break
        slack = 1e-6 * h
        if h + slack < max(o, c, l) or l - slack > min(o, c, h):
            problems.append(f"inconsistent high/low on {bars.dates[i]}")
            break
    zero = sum(1 for i in range(len(bars) - 50, len(bars)) if bars.volume[i] == 0)
    if zero > cfg["max_zero_volume_days"]:
        problems.append(f"{zero} days without volume in the last 50")
    if calendar and bars.dates[-1] < calendar[-1]:
        problems.append(f"last bar {bars.dates[-1]} is older than the last session {calendar[-1]}")
    if calendar:
        start = bars.dates[-60]
        expected = [d for d in calendar if start <= d <= bars.dates[-1]]
        missing = len(set(expected) - set(bars.dates))
        if missing > cfg["max_missing_sessions"]:
            problems.append(f"{missing} sessions missing in the last 60")
    return problems


# ---------------------------------------------------------------- indicators
# Each returns one value per bar; the value at i uses only bars 0..i.


def sma(values, n):
    out, total = [None] * len(values), 0.0
    for i, value in enumerate(values):
        total += value
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema(values, n):
    """Seeded with the simple average of the first n values."""
    out = [None] * len(values)
    if len(values) < n:
        return out
    k, current = 2 / (n + 1), sum(values[:n]) / n
    out[n - 1] = current
    for i in range(n, len(values)):
        current = values[i] * k + current * (1 - k)
        out[i] = current
    return out


def true_range(high, low, close):
    return [None] + [max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])) for i in range(1, len(close))]


def wilder(values, n):
    """Wilder smoothing of values[1:], first value at index n."""
    out = [None] * len(values)
    if len(values) <= n:
        return out
    current = sum(values[1:n + 1]) / n
    out[n] = current
    for i in range(n + 1, len(values)):
        current = (current * (n - 1) + values[i]) / n
        out[i] = current
    return out


def atr(high, low, close, n=14):
    return wilder(true_range(high, low, close), n)


def rsi(close, n=14):
    changes = [None] + [close[i] - close[i - 1] for i in range(1, len(close))]
    gains = wilder([None] + [max(c, 0.0) for c in changes[1:]], n)
    losses = wilder([None] + [max(-c, 0.0) for c in changes[1:]], n)
    return [None if g is None else 100.0 if l == 0 else 100 - 100 / (1 + g / l) for g, l in zip(gains, losses)]


def prior_mean(values, n):
    """Mean of the n values before i, excluding i."""
    out, total = [None] * len(values), 0.0
    for i, value in enumerate(values):
        if i >= n:
            out[i] = total / n
            total -= values[i - n]
        total += value
    return out


def relative_strength(bars, benchmark, lookback):
    """Ratio of the stock to the benchmark close on the same date, and its change over lookback bars."""
    line = [bars.close[i] / benchmark[d] if benchmark.get(d) else None for i, d in enumerate(bars.dates)]
    change = [None] * len(line)
    for i in range(lookback, len(line)):
        if line[i] is not None and line[i - lookback] is not None:
            change[i] = line[i] / line[i - lookback] - 1
    return line, change


def compute(bars, benchmarks, cfg):
    series = {
        "ema20": ema(bars.close, 20),
        "ema50": ema(bars.close, 50),
        "sma200": sma(bars.close, 200),
        "atr14": atr(bars.high, bars.low, bars.close, 14),
        "rsi14": rsi(bars.close, 14),
        "volume_avg50": prior_mean(bars.volume, 50),
        "dollar_volume_avg50": prior_mean([c * v for c, v in zip(bars.close, bars.volume)], 50),
    }
    for name, closes in benchmarks.items():
        series[f"rs_line_{name}"], series[f"rs_{name}"] = relative_strength(bars, closes, cfg["rs_lookback"])
    return series


# ---------------------------------------------------------------- strategies


def _check(checks, name, passed, detail):
    checks.append({"name": name, "passed": bool(passed), "detail": detail})


def _points(scores, name, points, earned, detail):
    scores.append({"name": name, "points": points, "earned": points if earned else 0, "detail": detail})


def relative_strength_at(series, t, cfg):
    """Change of the price ratio vs the market benchmark and vs the sector ETF over rs_lookback bars."""
    market = cfg["benchmarks"][0] if cfg["benchmarks"] else None
    out = {}
    for key, label in ((market, market), ("sector", series.get("sector_etf"))):
        if key is None:
            continue
        value = series.get(f"rs_{key}", [None] * (t + 1))[t] if label else None
        out["market" if key == market else "sector"] = {"benchmark": label, "change": None if value is None else round(value, 4)}
    return out


def _rs_score(scores, series, t, cfg, points):
    rs = relative_strength_at(series, t, cfg)
    for key, value in zip(("market", "sector"), points):
        item = rs.get(key, {"benchmark": None, "change": None})
        change = item["change"]
        if change is not None:
            detail = f"{change * 100:+.1f}% vs {item['benchmark']} over {cfg['rs_lookback']} bars"
        elif key == "sector" and not item["benchmark"]:
            detail = "sector unknown or no sector ETF data"
        else:
            detail = "no aligned benchmark data"
        _points(scores, f"outperforms_{key}", value, change is not None and change > 0, detail)


def _context(bars, series, t, cfg):
    """Indicator values at t, or None if history is too short."""
    needed = ("ema20", "ema50", "sma200", "atr14", "rsi14", "volume_avg50", "dollar_volume_avg50")
    if t < max(252, cfg["breakout_lookback"] + 1) or any(series[k][t] is None for k in needed) or series["ema50"][t - 10] is None:
        return None
    values = {k: series[k][t] for k in needed}
    values["close"] = bars.close[t]
    return values


def _liquidity(checks, ctx, cfg):
    _check(checks, "price", ctx["close"] >= cfg["min_price"], f"close {ctx['close']:.2f}, minimum {cfg['min_price']:g}")
    _check(checks, "liquidity", ctx["dollar_volume_avg50"] >= cfg["min_dollar_volume"],
           f"50-day average dollar volume {ctx['dollar_volume_avg50'] / 1e6:.1f}M, minimum {cfg['min_dollar_volume'] / 1e6:g}M")


def _result(strategy, bars, t, checks, scores, levels, ctx):
    passed = all(c["passed"] for c in checks)
    return {
        "strategy": strategy,
        "ticker": bars.ticker,
        "signal_date": bars.dates[t].isoformat(),
        "qualified": passed,
        "checks": checks,
        "score": sum(s["earned"] for s in scores) if passed else None,
        "scoring": scores if passed else [],
        "levels": levels,
        "indicators": {k: round(v, 4) for k, v in (ctx or {}).items()},
    }


def _with_rs(result, series, t, cfg):
    result["relative_strength"] = relative_strength_at(series, t, cfg) if result["indicators"] else {}
    return result


def _levels(entry, stop, target):
    risk = entry - stop
    return {"entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2),
            "risk_per_share": round(risk, 2), "reward_risk": round((target - entry) / risk, 2) if risk > 0 else None}


def evaluate_pullback(bars, series, t, cfg):
    checks, scores = [], []
    ctx = _context(bars, series, t, cfg)
    if ctx is None:
        _check(checks, "history", False, "not enough history for EMA50, SMA200 and ATR14")
        return _result("pullback", bars, t, checks, scores, None, None)
    h, l, o, c, v = bars.high, bars.low, bars.open, bars.close, bars.volume
    a, e20, e50, m200 = ctx["atr14"], ctx["ema20"], ctx["ema50"], ctx["sma200"]
    _liquidity(checks, ctx, cfg)
    _check(checks, "uptrend", c[t] > m200 and e20 > e50 > m200,
           f"close {c[t]:.2f}, EMA20 {e20:.2f}, EMA50 {e50:.2f}, SMA200 {m200:.2f}")
    _check(checks, "ema50_rising", e50 > series["ema50"][t - 10], f"EMA50 {e50:.2f} vs {series['ema50'][t - 10]:.2f} ten bars ago")
    swing_high, pull_low = max(h[t - 19:t + 1]), min(l[t - 2:t + 1])
    depth = (swing_high - pull_low) / a
    _check(checks, "pullback_depth", depth >= cfg["pullback_min_atr"],
           f"low of last 3 bars {pull_low:.2f} is {depth:.2f} ATR below the 20-day high {swing_high:.2f}, minimum {cfg['pullback_min_atr']:g}")
    tolerance = cfg["touch_atr"] * a
    touched = "EMA50" if pull_low <= e50 + tolerance else "EMA20" if pull_low <= e20 + tolerance else None
    _check(checks, "ema_touch", touched, f"low of last 3 bars {pull_low:.2f} reached {touched}" if touched
           else f"low of last 3 bars {pull_low:.2f} stayed above EMA20 {e20:.2f} + {cfg['touch_atr']:g} ATR")
    _check(checks, "holds_ema50", c[t] > e50, f"close {c[t]:.2f} vs EMA50 {e50:.2f}")
    span = h[t] - l[t]
    location = (c[t] - l[t]) / span if span > 0 else 0.0
    _check(checks, "reversal_bar", c[t] > o[t] and c[t] > c[t - 1] and location >= cfg["reversal_close_location"],
           f"open {o[t]:.2f}, close {c[t]:.2f}, previous close {c[t - 1]:.2f}, close at {location:.0%} of the day's range")
    entry = h[t] + 0.01
    stop = pull_low - cfg["pullback_stop_atr"] * a
    levels = _levels(entry, stop, swing_high)
    risk = entry - stop
    _check(checks, "risk_size", 0 < risk <= cfg["max_risk_atr"] * a, f"risk {risk:.2f} = {risk / a:.2f} ATR, maximum {cfg['max_risk_atr']:g}")
    _check(checks, "reward_risk", levels["reward_risk"] is not None and levels["reward_risk"] >= cfg["pullback_min_rr"],
           f"reward/risk {levels['reward_risk']} to the 20-day high, minimum {cfg['pullback_min_rr']:g}")
    _rs_score(scores, series, t, cfg, (20, 10))
    _points(scores, "healthy_rsi", 15, 40 <= ctx["rsi14"] <= 60, f"RSI14 {ctx['rsi14']:.1f}, best 40-60")
    quiet = sum(v[t - 5:t]) / 5
    _points(scores, "volume_dry_up", 15, quiet < ctx["volume_avg50"], f"pullback volume {quiet / ctx['volume_avg50']:.2f}x the 50-day average")
    _points(scores, "reversal_volume", 10, v[t] > v[t - 1], f"signal volume {v[t]:,.0f} vs {v[t - 1]:,.0f} the day before")
    _points(scores, "closes_above_prior_high", 10, c[t] > h[t - 1], f"close {c[t]:.2f} vs previous high {h[t - 1]:.2f}")
    _points(scores, "ema_stack", 10, c[t] > e20 > e50 > m200, "close > EMA20 > EMA50 > SMA200")
    _points(scores, "reward_risk_3", 10, (levels["reward_risk"] or 0) >= 2.5, f"reward/risk {levels['reward_risk']}, best 2.5 or more")
    return _with_rs(_result("pullback", bars, t, checks, scores, levels, ctx), series, t, cfg)


def resistance(bars, end, lookback, tolerance):
    """Highest high of the lookback bars before end, its age, and how many separate times price came within tolerance."""
    start = end - lookback
    highs = bars.high[start:end]
    level = max(highs)
    last = start + max(i for i, x in enumerate(highs) if x == level)
    touches, inside = 0, False
    for x in highs:
        near = x >= level - tolerance
        touches += near and not inside
        inside = near
    return level, end - last, touches, min(bars.low[start:end])


def evaluate_breakout(bars, series, t, cfg):
    checks, scores = [], []
    ctx = _context(bars, series, t, cfg)
    if ctx is None:
        _check(checks, "history", False, "not enough history for EMA50, SMA200 and ATR14")
        return _result("breakout", bars, t, checks, scores, None, None)
    h, l, c, v = bars.high, bars.low, bars.close, bars.volume
    a, e20, e50, m200 = ctx["atr14"], ctx["ema20"], ctx["ema50"], ctx["sma200"]
    lookback = cfg["breakout_lookback"]
    _liquidity(checks, ctx, cfg)
    _check(checks, "uptrend", c[t] > e20 > e50 > m200, f"close {c[t]:.2f}, EMA20 {e20:.2f}, EMA50 {e50:.2f}, SMA200 {m200:.2f}")
    level, age, touches, base_low = resistance(bars, t, lookback, cfg["resistance_touch_atr"] * a)
    _check(checks, "base_age", age >= cfg["min_base_bars"], f"{lookback}-day high {level:.2f} set {age} bars ago, minimum {cfg['min_base_bars']}")
    _check(checks, "resistance_touches", touches >= cfg["min_touches"],
           f"{touches} separate tests of {level:.2f} within {cfg['resistance_touch_atr']:g} ATR, minimum {cfg['min_touches']}")
    depth = (level - base_low) / level
    _check(checks, "base_depth", depth <= cfg["max_base_depth"], f"base {depth:.0%} deep ({base_low:.2f}-{level:.2f}), maximum {cfg['max_base_depth']:.0%}")
    previous_level = max(h[t - 1 - lookback:t - 1])
    _check(checks, "fresh_breakout", c[t] > level and c[t - 1] <= previous_level,
           f"close {c[t]:.2f} vs resistance {level:.2f}; previous close {c[t - 1]:.2f} vs {previous_level:.2f}")
    extension = (c[t] - level) / a
    _check(checks, "not_extended", extension <= cfg["max_extension_atr"], f"{extension:.2f} ATR above resistance, maximum {cfg['max_extension_atr']:g}")
    rel_volume = v[t] / ctx["volume_avg50"] if ctx["volume_avg50"] else 0.0
    ctx["rel_volume"] = rel_volume
    _check(checks, "volume", rel_volume >= cfg["min_rel_volume"], f"relative volume {rel_volume:.2f}, minimum {cfg['min_rel_volume']:g}")
    entry = c[t]
    stop = min(l[t], level) - cfg["breakout_stop_atr"] * a
    levels = _levels(entry, stop, entry + (level - base_low))
    levels["resistance"] = round(level, 2)
    risk = entry - stop
    _check(checks, "risk_size", 0 < risk <= cfg["max_risk_atr"] * a, f"risk {risk:.2f} = {risk / a:.2f} ATR, maximum {cfg['max_risk_atr']:g}")
    _check(checks, "reward_risk", levels["reward_risk"] is not None and levels["reward_risk"] >= cfg["breakout_min_rr"],
           f"reward/risk {levels['reward_risk']} to the measured move, minimum {cfg['breakout_min_rr']:g}")
    benchmarks = cfg["benchmarks"]
    _points(scores, "strong_volume", 20, rel_volume >= 2, f"relative volume {rel_volume:.2f}, best 2 or more")
    _rs_score(scores, series, t, cfg, (15, 10))
    line = series.get(f"rs_line_{benchmarks[0]}", [None] * (t + 1)) if benchmarks else [None] * (t + 1)
    window = [x for x in line[t - 49:t + 1] if x is not None]
    _points(scores, "rs_line_high", 15, line[t] is not None and line[t] >= max(window),
            f"relative strength line vs {benchmarks[0] if benchmarks else '-'} at a 50-day high" if line[t] is not None else "no aligned benchmark data")
    span = h[t] - l[t]
    location = (c[t] - l[t]) / span if span > 0 else 0.0
    _points(scores, "strong_close", 10, location >= 0.75, f"close at {location:.0%} of the day's range")
    tight = (level - min(l[t - 10:t])) / a
    _points(scores, "tight_base", 10, tight <= 3, f"last 10 bars span {tight:.1f} ATR below resistance, best 3 or less")
    year_high = max(h[t - 251:t + 1])
    _points(scores, "near_52w_high", 10, c[t] >= 0.9 * year_high, f"close {c[t] / year_high:.0%} of the 52-week high {year_high:.2f}")
    _points(scores, "reward_risk_3", 10, (levels["reward_risk"] or 0) >= 3, f"reward/risk {levels['reward_risk']}, best 3 or more")
    return _with_rs(_result("breakout", bars, t, checks, scores, levels, ctx), series, t, cfg)


EVALUATORS = {"pullback": evaluate_pullback, "breakout": evaluate_breakout}


# ---------------------------------------------------------------- validation
#
# Execution model, also returned as validation.assumptions:
# - Signals use completed bars up to and including the signal day t only.
# - Pullback: buy stop at the entry, valid on bar t+1 only; a gap above it fills at the open.
#   Breakout: buy at the open of bar t+1. A fill at or below the stop is skipped.
# - Each bar is handled in time order: first its open, then its range. An open at or beyond
#   the stop or target exits at that open. Inside a bar the order of high and low is unknown,
#   so when both the stop and the target are inside the range, the stop counts.
# - Trades still open after `validation_horizon` bars exit at that bar's close.
# - One trade per ticker and strategy at a time. Trades of different tickers overlap freely and
#   each risks 1R, so drawdown is a sum over concurrent trades, not a portfolio simulation.
# - Costs are a round trip in basis points of the fill price, converted to R per trade.

COST_BPS = (0, 10, 25, 50)
FORWARD_DAYS = (5, 10, 20)
ASSUMPTIONS = [
    "Signals on day t use completed daily bars up to t only; the partial bar of the current session is dropped.",
    "Pullback fills with a buy stop on the next bar only, at the open if it gaps above the entry. Breakout fills at the next open. Fills at or below the stop are skipped.",
    "Bars are processed in time order: an open at or beyond the stop or target exits at the open. Daily bars do not show the order of prices inside a bar, so two cases use the conservative assumption and are flagged in `ambiguous`: a bar that touches both the stop and the target counts as stopped (`stop_and_target_same_bar`), and on the bar where a pullback buy stop triggers inside the bar, a low at or below the stop counts as stopped even though it may have come before the fill (`fill_and_stop_same_bar`).",
    "MAE uses the full low of each bar held, including the part of the fill bar before a buy stop triggered, so it is conservative.",
    "Trades not stopped or at target exit at the close of the last bar of the horizon.",
    "Costs are round trip in basis points of the fill price; no market impact or borrow modelling.",
    "One trade per ticker and strategy at a time; trades of different tickers overlap and each risks 1R. Drawdown sums them in exit order and is not a portfolio simulation.",
    "Forward returns run from the next open (the first possible fill) to the close k sessions after the signal, and are computed only for signals with k bars of future data. They include overlapping signals; a non-overlapping subset is reported too.",
    "Selection bias: the tickers come from today's Finviz screen, so every one of them exists and passed today's filters. Survivorship bias: delisted and acquired stocks are missing. Both make past results look better than a real-time run would have been.",
    "Earnings dates come from the Finviz quote page and are the actual report times as Finviz shows them today, not the dates that were scheduled at the time.",
]


def _fill(bars, i, signal):
    """Fill price on bar i, or (None, reason)."""
    levels = signal["levels"]
    if signal["strategy"] == "pullback":
        if bars.high[i] < levels["entry"]:
            return None, "not_filled"
        fill = max(bars.open[i], levels["entry"])
    else:
        fill = bars.open[i]
    if fill <= levels["stop"]:
        return None, "opened below the stop"
    return fill, None


def _walk(bars, i, fill, stop, target, end):
    """Bars i..end-1 in time order. Returns (j, price, outcome, gap, low, high, ambiguous); j is None if nothing was hit.

    Inside a bar the stop counts before the target (conservative); `ambiguous` names the case when the bar alone cannot
    tell the order: both levels touched, or the stop touched on the bar where a buy stop filled above the open."""
    low = high = fill
    for j in range(i, end):
        opening = fill if j == i else bars.open[j]
        if opening >= target:
            return j, opening, "target", True, low, high, None
        if opening <= stop:  # only on later bars, the fill is above the stop
            return j, opening, "stop", True, low, high, None
        if bars.low[j] <= stop:
            ambiguous = ("stop_and_target_same_bar" if bars.high[j] >= target
                         else "fill_and_stop_same_bar" if j == i and fill > bars.open[j] else None)
            return j, stop, "stop", False, low, high, ambiguous
        low = min(low, bars.low[j])
        if bars.high[j] >= target:
            return j, target, "target", False, low, high, None
        high = max(high, bars.high[j])
    return None, None, None, False, low, high, None


def simulate(bars, t, signal, horizon):
    """Outcome of a signal on bar t, using only bars after t."""
    levels = signal["levels"]
    stop, target = levels["stop"], levels["target"]
    i = t + 1
    if i + horizon > len(bars):
        return {"outcome": "open"}
    fill, reason = _fill(bars, i, signal)
    if fill is None:
        return {"outcome": "not_filled"} if reason == "not_filled" else {"outcome": "not_filled", "detail": reason}
    risk = fill - stop
    atr = (signal.get("indicators") or {}).get("atr14") or risk
    j, price, outcome, gap, low, high, ambiguous = _walk(bars, i, fill, stop, target, i + horizon)
    if j is None:
        j, price, outcome = i + horizon - 1, bars.close[i + horizon - 1], "timeout"
    return {"outcome": outcome, "gap": gap, "ambiguous": ambiguous, "fill": round(fill, 4), "fill_date": bars.dates[i].isoformat(),
            "exit": round(price, 4), "exit_date": bars.dates[j].isoformat(), "risk": round(risk, 4),
            "r": round((price - fill) / risk, 4), "bars_held": j - t,
            "mae_r": round((fill - min(low, price)) / risk, 3), "mfe_r": round((max(high, price) - fill) / risk, 3),
            "mae_atr": round((fill - min(low, price)) / atr, 3), "mfe_atr": round((max(high, price) - fill) / atr, 3)}


def signal_warnings(signal):
    """Practical cautions that do not change qualification."""
    levels, atr = signal["levels"], (signal.get("indicators") or {}).get("atr14")
    risk, out = levels["risk_per_share"], []
    if atr and risk < 0.5 * atr:
        out.append(f"tight stop: risk {risk:.2f} is {risk / atr:.2f} ATR, inside normal daily noise")
    if risk > 0 and 10 / 1e4 * levels["entry"] / risk > 0.1:
        out.append(f"costs: a 10 bp round trip costs {10 / 1e4 * levels['entry'] / risk:.2f} R")
    return out


def track(bars, t, signal, horizon):
    """Status today of a past signal on bar t < len(bars) - 1, with the same execution rules as simulate.

    not_triggered: the pullback buy stop was not reached on the next bar; skipped: opened below the stop;
    stopped / target: exit hit; time_exit: still open after `horizon` bars, closed at that bar's close;
    open: still running, valued at the last close."""
    levels = signal["levels"]
    i = t + 1
    fill, reason = _fill(bars, i, signal)
    if fill is None:
        return {"status": "not_triggered" if reason == "not_filled" else "skipped", "detail": reason}
    risk = fill - levels["stop"]
    base = {"fill_date": bars.dates[i].isoformat(), "fill": round(fill, 4)}
    end = min(i + horizon, len(bars))
    j, price, outcome, gap, _, _, ambiguous = _walk(bars, i, fill, levels["stop"], levels["target"], end)
    if j is None and i + horizon <= len(bars):
        j, price, outcome = i + horizon - 1, bars.close[i + horizon - 1], "time_exit"
    if j is not None:
        return {"status": {"stop": "stopped"}.get(outcome, outcome), **base, "gap": gap, "ambiguous": ambiguous, "exit_date": bars.dates[j].isoformat(),
                "exit": round(price, 4), "r": round((price - fill) / risk, 3), "bars_held": j - t}
    last = len(bars) - 1
    return {"status": "open", **base, "last_close": round(bars.close[last], 4), "r_now": round((bars.close[last] - fill) / risk, 3),
            "bars_held": last - t, "bars_left": horizon - (last - t)}


def track_signals(bars, series, strategies, days, cfg, regime=None):
    """Signals of the last `days` completed sessions before the latest, one active signal per ticker and strategy."""
    horizon, last = cfg["validation_horizon"], len(bars) - 1
    regime = regime or {}
    out = []
    for strategy in strategies:
        busy_until = -1
        for t in range(max(cfg["min_bars"] - 1, last - days), last):
            if t <= busy_until:
                continue
            signal = EVALUATORS[strategy](bars, series, t, cfg)
            if not signal["qualified"]:
                continue
            status = track(bars, t, signal, horizon)
            label = regime.get(bars.dates[t], "unknown")
            out.append({"strategy": strategy, "ticker": bars.ticker, "signal_date": signal["signal_date"], "score": signal["score"],
                        "regime": label, "action": ACTIONS[label], **signal["levels"], **status})
            if status["status"] not in ("not_triggered", "skipped"):
                busy_until = t + status["bars_held"]
    return out


def tracking_summary(rows):
    closed = [x["r"] for x in rows if "r" in x]
    return {"signals": len(rows), "status": {k: sum(x["status"] == k for x in rows)
                                             for k in ("open", "target", "stopped", "time_exit", "not_triggered", "skipped")},
            "closed_trades": len(closed), "closed_average_r": round(statistics.fmean(closed), 3) if closed else None,
            "open_average_r_now": round(statistics.fmean(x["r_now"] for x in rows if x["status"] == "open"), 3)
            if any(x["status"] == "open" for x in rows) else None}


def breadth(sector_bars):
    """Per date: (number of sector ETFs above their SMA200, number with an SMA200)."""
    above = {}
    for bars in sector_bars.values():
        for d, c, m in zip(bars.dates, bars.close, sma(bars.close, 200)):
            if m is not None:
                count = above.setdefault(d, [0, 0])
                count[0] += c > m
                count[1] += 1
    return {d: tuple(v) for d, v in above.items()}


def market_regime(dates, closes, breadth_by_date=None, cfg=None):
    """Regime per date from data up to that date.

    bullish: benchmark above its SMA200, the SMA200 rising over regime_slope_bars, and breadth (share of sector ETFs above
    their SMA200) at least breadth_bullish. bearish: below the SMA200 and either the SMA200 falling or breadth at most
    breadth_bearish. neutral: anything else. Breadth needs at least 6 sector ETFs; without it only the trend decides."""
    cfg = cfg or DEFAULTS
    slope, m200 = cfg["regime_slope_bars"], sma(closes, 200)
    breadth_by_date = breadth_by_date or {}
    regime = {}
    for i, (d, c) in enumerate(zip(dates, closes)):
        if m200[i] is None or i < 199 + slope:
            regime[d] = "unknown"
            continue
        above, rising = c > m200[i], m200[i] > m200[i - slope]
        count = breadth_by_date.get(d)
        share = count[0] / count[1] if count and count[1] >= 6 else None
        if above and rising and (share is None or share >= cfg["breadth_bullish"]):
            regime[d] = "bullish"
        elif not above and (not rising or (share is not None and share <= cfg["breadth_bearish"])):
            regime[d] = "bearish"
        else:
            regime[d] = "neutral"
    return regime


def market_summary(name, bars, breadth_by_date, regime, sector_bars, cfg):
    """The latest regime and the inputs behind it."""
    d, i, slope = bars.dates[-1], len(bars) - 1, cfg["regime_slope_bars"]
    m200 = sma(bars.close, 200)
    count = breadth_by_date.get(d)
    etfs = {}
    for etf, b in sorted(sector_bars.items()):
        m = sma(b.close, 200)[-1]
        if m is not None and b.dates[-1] == d:
            etfs[etf] = {"close": round(b.close[-1], 2), "sma200": round(m, 2), "above": b.close[-1] > m}
    label = regime.get(d, "unknown")
    return {"as_of": d.isoformat(), "regime": label, "long_setups": ACTIONS[label],
            "benchmark": {"ticker": name, "close": round(bars.close[i], 2),
                          "sma200": None if m200[i] is None else round(m200[i], 2),
                          "sma200_rising": None if m200[i] is None or i < slope or m200[i - slope] is None else m200[i] > m200[i - slope]},
            "breadth": {"above_sma200": count[0] if count else None, "sector_etfs": count[1] if count else 0, "etfs": etfs},
            "rules": "bullish: benchmark above a rising SMA200 and breadth >= {:g}; bearish: below the SMA200 and the SMA200 falling or "
                     "breadth <= {:g}; otherwise neutral. LONG setups: bullish -> long, neutral -> watch, bearish -> no_trade."
                     .format(cfg["breadth_bullish"], cfg["breadth_bearish"])}


def forward_returns(bars, t, market=None):
    """Returns from the next open to the close k sessions after t, and the same for the benchmark."""
    out = {}
    if t + 1 >= len(bars):
        return out
    start = bars.open[t + 1]
    for k in FORWARD_DAYS:
        if t + k >= len(bars):
            continue
        value = bars.close[t + k] / start - 1
        out[f"fwd_{k}"] = value
        if market:
            m_start, m_end = market["open"].get(bars.dates[t + 1]), market["close"].get(bars.dates[t + k])
            if m_start and m_end:
                out[f"excess_{k}"] = value - (m_end / m_start - 1)
    return out


def reaction_sessions(events, calendar):
    """Session whose open first reflects each report: the same day before 09:30 New York, else the next session."""
    sessions = set()
    for event in events:
        local = new_york_time(event)
        position = bisect.bisect_left(calendar, local.date())
        if (local.hour, local.minute) >= (9, 30) and position < len(calendar) and calendar[position] == local.date():
            position += 1
        if position < len(calendar):
            sessions.add(calendar[position])
    return {"since": min(new_york_time(e).date() for e in events) if events else None, "sessions": sessions}


def earnings_flags(trade, info):
    if not info or not info["since"] or date.fromisoformat(trade["signal_date"]) < info["since"]:
        return {"known": False}
    fill, exit_ = date.fromisoformat(trade["fill_date"]), date.fromisoformat(trade["exit_date"])
    signal = date.fromisoformat(trade["signal_date"])
    return {"known": True,
            "held_through": any(fill < s <= exit_ for s in info["sessions"]),
            "reaction_at_entry": signal in info["sessions"] or fill in info["sessions"]}


def validate(bars, series, strategies, days, cfg, market=None, earnings=None):
    """Walk forward over `days` signal days that ended at least `horizon` bars ago, so every trade can finish.

    Returns the trades (one open trade per ticker and strategy), every qualified signal with its
    forward returns, and the forward returns of every day in the window (the base rate)."""
    horizon = cfg["validation_horizon"]
    last = len(bars) - 1 - max(horizon, max(FORWARD_DAYS))
    window = range(max(cfg["min_bars"] - 1, last - days), last)
    base = [forward_returns(bars, t, market) for t in window]
    trades, signals = [], []
    regime = (market or {}).get("regime", {})
    for strategy in strategies:
        busy_until, previous = -1, None
        for t in window:
            signal = EVALUATORS[strategy](bars, series, t, cfg)
            if not signal["qualified"]:
                continue
            signals.append({"strategy": strategy, "ticker": bars.ticker, "signal_date": signal["signal_date"],
                            "overlaps": previous is not None and t - previous < max(FORWARD_DAYS), **forward_returns(bars, t, market)})
            previous = t
            if t <= busy_until:
                continue
            outcome = simulate(bars, t, signal, horizon)
            trade = {"ticker": bars.ticker, "strategy": strategy, "signal_date": signal["signal_date"], "score": signal["score"],
                     "regime": regime.get(bars.dates[t], "unknown"), **signal["levels"], **outcome}
            if "r" in outcome:
                trade["earnings"] = earnings_flags(trade, earnings)
                busy_until = t + outcome["bars_held"]
            trades.append(trade)
    return trades, signals, base


def net_r(trade, bps):
    return trade["r"] - bps / 1e4 * trade["fill"] / trade["risk"]


def metrics(values):
    """values: R per trade in exit order."""
    if not values:
        return {"trades": 0}
    gains, losses = sum(v for v in values if v > 0), -sum(v for v in values if v <= 0)
    total = peak = drawdown = 0.0
    for v in values:
        total += v
        peak = max(peak, total)
        drawdown = max(drawdown, peak - total)
    return {"trades": len(values), "expectancy_r": round(statistics.fmean(values), 3), "median_r": round(statistics.median(values), 3),
            "win_rate": round(sum(v > 0 for v in values) / len(values), 3), "profit_factor": round(gains / losses, 2) if losses else None,
            "total_r": round(total, 2), "max_drawdown_r": round(drawdown, 2), "largest_loss_r": round(min(values), 2)}


def _medians(trades):
    if not trades:
        return {"trades": 0}
    return {"trades": len(trades), **{f"median_{k}": round(statistics.median(x[k] for x in trades), 2) for k in ("mae_r", "mfe_r", "mae_atr", "mfe_atr")}}


def _forward(rows):
    out = {}
    for k in FORWARD_DAYS:
        values = [x[f"fwd_{k}"] for x in rows if f"fwd_{k}" in x]
        excess = [x[f"excess_{k}"] for x in rows if f"excess_{k}" in x]
        out[f"{k}d"] = {"n": len(values)} if not values else {
            "n": len(values), "mean_pct": round(100 * statistics.fmean(values), 2), "median_pct": round(100 * statistics.median(values), 2),
            "positive": round(sum(v > 0 for v in values) / len(values), 3),
            "mean_excess_pct": round(100 * statistics.fmean(excess), 2) if excess else None}
    return out


def summarize(trades, signals=()):
    closed = sorted((x for x in trades if "r" in x), key=lambda x: (x["exit_date"], x["signal_date"], x["ticker"]))
    gap_stops = [x for x in closed if x["outcome"] == "stop" and x["gap"]]
    known = [x for x in closed if x.get("earnings", {}).get("known")]
    summary = {
        "trades": len(trades),
        "not_filled": sum(x["outcome"] == "not_filled" for x in trades),
        "still_open": sum(x["outcome"] == "open" for x in trades),
        "closed": len(closed),
        "exits": {k: sum(x["outcome"] == k for x in closed) for k in ("stop", "target", "timeout")},
        "costs": {f"{bps}bp": metrics([net_r(x, bps) for x in closed]) for bps in COST_BPS},
        "gaps": {
            "stop_exits_through_gap": len(gap_stops),
            "extra_loss_beyond_stop_r": round(sum(-1 - x["r"] for x in gap_stops), 2),
            "worst_gap_stop_r": round(min((x["r"] for x in gap_stops), default=0), 2),
            "target_exits_at_open": sum(x["outcome"] == "target" and x["gap"] for x in closed),
        },
        "ambiguous_bars": {k: sum(x.get("ambiguous") == k for x in closed) for k in ("stop_and_target_same_bar", "fill_and_stop_same_bar")},
        "mae_mfe": {"all": _medians(closed), **{k: _medians([x for x in closed if x["outcome"] == k]) for k in ("stop", "target", "timeout")}},
        "by_regime": {name: {"gross": metrics([x["r"] for x in closed if x["regime"] == name]),
                             "expectancy_r_10bp": metrics([net_r(x, 10) for x in closed if x["regime"] == name])["expectancy_r"]}
                      for name in sorted({x["regime"] for x in closed})},
        "earnings": {
            "trades_with_known_dates": len(known),
            "trades_without_dates": len(closed) - len(known),
            "held_through_report": metrics([x["r"] for x in known if x["earnings"]["held_through"]]),
            "not_held_through_report": metrics([x["r"] for x in known if not x["earnings"]["held_through"]]),
            "report_at_signal_or_entry": metrics([x["r"] for x in known if x["earnings"]["reaction_at_entry"]]),
        },
        "forward_returns": {
            "all_signals": _forward(signals),
            "non_overlapping_signals": _forward([x for x in signals if not x["overlaps"]]),
            "overlapping_signals": sum(x["overlaps"] for x in signals),
        },
    }
    summary["gross"] = summary["costs"]["0bp"]
    return summary


# ---------------------------------------------------------------- main


def settings_with_defaults(settings):
    cfg = {**DEFAULTS, "benchmarks": list(DEFAULTS["benchmarks"])}
    for key, value in (settings or {}).items():
        if key not in DEFAULTS:
            raise ValueError(f"unknown setting {key!r}. Settings: {', '.join(DEFAULTS)}")
        if key == "benchmarks":
            if not isinstance(value, list) or not value or not all(isinstance(x, str) and TICKER.match(x) for x in value):
                raise ValueError("benchmarks must be a list of tickers, e.g. [\"QQQ\"]; the first one is the market benchmark")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"setting {key} must be a number of 0 or more")
        cfg[key] = value
    for key in ("min_bars", "breakout_lookback", "min_base_bars", "min_touches", "validation_horizon", "rs_lookback", "regime_slope_bars"):
        cfg[key] = int(cfg[key])
        if cfg[key] < 1:
            raise ValueError(f"setting {key} must be 1 or more")
    cfg["min_bars"] = max(cfg["min_bars"], 253, cfg["breakout_lookback"] + 2)
    return cfg


def discover(finviz_url, tickers, max_tickers):
    if tickers.strip():
        names = [x.strip().upper() for x in tickers.split(",") if x.strip()]
        bad = [x for x in names if not TICKER.match(x)]
        if bad:
            raise ValueError(f"not tickers: {', '.join(bad)}")
        return list(dict.fromkeys(names))[:max_tickers], "tickers", {}
    url = urlparse(finviz_url)
    if url.scheme != "https" or url.hostname not in FINVIZ_HOSTS:
        raise ValueError(f"not a Finviz screener URL: {finviz_url}")
    try:
        rows = list(Screener.init_from_url(finviz_url, rows=max_tickers))
    except NoResults:
        rows = []
    rows = [r for r in rows if TICKER.match(r.get("Ticker", ""))]
    sectors = {r["Ticker"]: r["Sector"] for r in rows if r.get("Sector") in SECTOR_ETFS}
    return list(dict.fromkeys(r["Ticker"] for r in rows))[:max_tickers], "finviz", sectors


def main(finviz_url: str = "", tickers: str = "", strategy: str = "both", max_tickers: int = 50, track_days: int = 10,
         validation_days: int = 0, settings: dict | None = None) -> dict:
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    strategies = list(STRATEGIES) if strategy == "both" else [strategy]
    if strategy != "both" and strategy not in STRATEGIES:
        raise ValueError(f"strategy must be both, {' or '.join(STRATEGIES)}")
    if not finviz_url.strip() and not tickers.strip():
        raise ValueError("give a Finviz screener URL or a comma-separated list of tickers")
    if not 1 <= max_tickers <= MAX_TICKERS:
        raise ValueError(f"max_tickers must be 1 to {MAX_TICKERS}")
    if not 0 <= validation_days <= MAX_VALIDATION_DAYS:
        raise ValueError(f"validation_days must be 0 to {MAX_VALIDATION_DAYS}")
    if not 0 <= track_days <= MAX_TRACK_DAYS:
        raise ValueError(f"track_days must be 0 to {MAX_TRACK_DAYS}")
    cfg = settings_with_defaults(settings)
    count = cfg["min_bars"] + max(validation_days + cfg["validation_horizon"], track_days) + 60

    names, source, sectors = discover(finviz_url, tickers, max_tickers)
    benchmarks, benchmark_bars, benchmark_info, data_errors = {}, {}, {}, []
    for name in cfg["benchmarks"]:
        try:
            bars = completed_bars(fetch_bars(name, count), now)
            benchmarks[name] = {d: c for d, c in zip(bars.dates, bars.close) if math.isfinite(c) and c > 0}
            benchmark_bars[name] = bars
            benchmark_info[name] = {"as_of": bars.dates[-1].isoformat(), "close": round(bars.close[-1], 2)}
        except (requests.RequestException, ValueError) as error:
            data_errors.append({"ticker": name, "reason": f"benchmark: {error}"})
        time.sleep(cfg["request_pause"])
    if not benchmarks:
        raise RuntimeError("no benchmark data, so the market calendar and relative strength cannot be checked")
    calendar = sorted(set().union(*benchmarks.values()))
    rs_benchmarks = [b for b in cfg["benchmarks"] if b in benchmarks]
    reference = benchmark_bars[rs_benchmarks[0]]
    sector_bars, sector_closes = {}, {}
    for etf in sorted(set(SECTOR_ETFS.values())):
        try:
            bars = completed_bars(fetch_bars(etf, count), now)
            sector_bars[etf] = bars
            sector_closes[etf] = {d: c for d, c in zip(bars.dates, bars.close) if math.isfinite(c) and c > 0}
        except (requests.RequestException, ValueError) as error:
            data_errors.append({"ticker": etf, "reason": f"sector ETF: {error}"})
        time.sleep(cfg["request_pause"])
    breadth_by_date = breadth(sector_bars)
    regime = market_regime(reference.dates, reference.close, breadth_by_date, cfg)
    market = {"name": rs_benchmarks[0], "open": dict(zip(reference.dates, reference.open)),
              "close": dict(zip(reference.dates, reference.close)), "regime": regime}
    market_info = market_summary(rs_benchmarks[0], reference, breadth_by_date, regime, sector_bars, cfg)
    run_cfg = {**cfg, "benchmarks": rs_benchmarks}

    results = {s: {"qualified": [], "rejected": []} for s in strategies}
    trades, signals, base, without_earnings, screened, tracked = [], [], [], [], 0, []
    for name in names:
        try:
            bars = completed_bars(fetch_bars(name, count), now)
            problems = quality_problems(bars, calendar, cfg)
        except (requests.RequestException, ValueError) as error:
            problems = [str(error)]
        time.sleep(cfg["request_pause"])
        if problems:
            data_errors.append({"ticker": name, "reason": "; ".join(problems)})
            continue
        screened += 1
        sector = sectors.get(name)
        if sector is None and source == "tickers":
            try:
                sector = fetch_sector(name)
            except (requests.RequestException, ValueError):
                sector = None
            time.sleep(cfg["request_pause"])
        etf = SECTOR_ETFS.get(sector)
        closes = {b: benchmarks[b] for b in rs_benchmarks}
        if etf in sector_closes:
            closes["sector"] = sector_closes[etf]
        series = compute(bars, closes, cfg)
        series["sector_etf"] = etf if etf in sector_closes else None
        label = regime.get(bars.dates[-1], "unknown")
        for s in strategies:
            signal = EVALUATORS[s](bars, series, len(bars) - 1, run_cfg)
            if signal["qualified"]:
                results[s]["qualified"].append({
                    **{k: signal[k] for k in ("ticker", "signal_date", "score")}, "direction": "long", "regime": label,
                    "action": ACTIONS[label], "sector": sector, "warnings": signal_warnings(signal),
                    **{k: signal[k] for k in ("levels", "relative_strength", "checks", "scoring", "indicators")}})
            else:
                results[s]["rejected"].append({"ticker": name, "failed": [c for c in signal["checks"] if not c["passed"]]})
        if track_days:
            tracked += track_signals(bars, series, strategies, track_days, run_cfg, regime)
        if validation_days:
            info = None
            try:
                info = reaction_sessions(fetch_earnings(name), calendar)
            except (requests.RequestException, ValueError):
                pass
            if not info or not info["since"]:
                without_earnings.append(name)
            time.sleep(cfg["request_pause"])
            more = validate(bars, series, strategies, validation_days, run_cfg, market, info)
            trades += more[0]
            signals += more[1]
            base += more[2]
    for s in strategies:
        results[s]["qualified"].sort(key=lambda x: (-x["score"], x["ticker"]))

    duration = time.monotonic() - started
    counts = ", ".join(f"{s} {len(results[s]['qualified'])}" for s in strategies)
    print(f"Summary: {len(names)} tickers, {len(data_errors)} data errors, regime {market_info['regime']}, qualified: {counts}, {duration:.1f}s")
    tracked.sort(key=lambda x: (x["signal_date"], x["strategy"], x["ticker"]), reverse=True)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "as_of": calendar[-1].isoformat(),
        "universe": {"source": source, "tickers": len(names), "data_errors": len(data_errors)},
        "market": market_info,
        "benchmarks": benchmark_info,
        "results": results,
        "tracking": None if not track_days else {
            "days": track_days,
            "horizon_bars": cfg["validation_horizon"],
            "note": "Signals of the previous sessions, recomputed from today's data with the execution rules of the validation. "
                    "Only tickers in today's universe are tracked; one active signal per ticker and strategy.",
            "summary": {s: tracking_summary([x for x in tracked if x["strategy"] == s]) for s in strategies},
            "signals": tracked,
        },
        "data_errors": data_errors,
        "validation": None if not validation_days else {
            "days": validation_days,
            "horizon_bars": cfg["validation_horizon"],
            "cost_scenarios_bps": list(COST_BPS),
            "forward_days": list(FORWARD_DAYS),
            "market_regime_benchmark": market["name"],
            "assumptions": ASSUMPTIONS,
            "earnings_data": {
                "source": "Finviz quote page chart events (weekly view), report time decides the reaction session",
                "tickers_with_dates": screened - len(without_earnings),
                "tickers_without_dates": without_earnings,
            },
            "universe_base_rate": _forward(base),
            "summary": {s: summarize([x for x in trades if x["strategy"] == s], [x for x in signals if x["strategy"] == s]) for s in strategies},
            "trades": trades,
        },
        "settings": cfg,
        "duration_seconds": round(duration, 2),
    }
