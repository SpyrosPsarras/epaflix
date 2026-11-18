"""Walk-forward parameter study for the pullback and breakout strategies.

Research code, not a Windmill script: wmill.yaml syncs only f/**, so nothing in this folder is
deployed, and the production screeners (f/screener/run_screen.py, strategy_screen.py) are not
changed. It reuses the indicators and the execution model of strategy_screen.

How it works
1. Every ticker is scanned once with the loosest value of each varied entry threshold. Each
   candidate keeps the feature values it had on its signal day t (bars up to t only).
2. Cross-sectional ranks (6-1 month momentum, volatility-adjusted momentum) are computed per date
   across all liquid tickers on that date, so a rank never uses a later date.
3. For one exit configuration (stop, exit rule, holding period) at a time, every candidate is
   simulated with the chronology of strategy_screen.simulate, then every configuration that uses
   that exit is evaluated: entry thresholds and filters are masks over the candidates, one trade
   per ticker at a time. Every configuration is logged.
4. Selection uses the development period only, by a pre-declared robustness rule: the median net
   expectancy at 25 bp over the configuration and its numeric neighbours, with minimum trade
   counts and a share of positive years. Validation and holdout are reported, never used to pick.
5. An anchored walk-forward repeats the same selection each year on earlier years only and trades
   the pick in that year. Its concatenated result is the honest estimate for the procedure.
6. Shortlisted configurations are compared with matched random entries (same signal dates, random
   ticker from the liquid uptrend pool, same stop distance in ATR, same exit rule) and with QQQ
   over the same holding windows.

Memory: prices live in a compact binary store and are loaded one ticker at a time; candidates are
typed columns. A full run over ~1,000 tickers needs a few hundred MB in one process.

Run: python strategy_research.py --cache JSON_DIR --meta META.json --out DIR
"""

import argparse
import bisect
import csv
import hashlib
import itertools
import json
import math
import random
import statistics
import sys
import time
from array import array
from collections import deque
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "f" / "screener"))
import strategy_screen as ss  # noqa: E402

MARKET = "QQQ"
SECTOR_ETFS = ("XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB")
SECTOR_OF = {"Technology": "XLK", "Financial": "XLF", "Healthcare": "XLV", "Industrials": "XLI", "Consumer Cyclical": "XLY",
             "Consumer Defensive": "XLP", "Energy": "XLE", "Utilities": "XLU", "Basic Materials": "XLB",
             "Real Estate": "XLRE", "Communication Services": "XLC"}
NOT_STOCKS = set(SECTOR_ETFS) | {"QQQ", "SPY", "IWM", "RSP", "SMH", "XLRE", "XLC"}
COSTS = (0, 10, 25, 50)
SELECT_BPS = 25
PERIODS = {"dev": (date(2000, 1, 1), date(2018, 12, 31)), "validation": (date(2019, 1, 1), date(2022, 12, 31)),
           "holdout": (date(2023, 1, 1), date(2100, 1, 1))}
RULES = {"min_trades": {"pullback": 200, "breakout": 100}, "min_positive_years": 0.6, "min_year_trades": 10,
         "neighbour_min_trades": 30, "shortlist": 3}
MIN_PRICE, MIN_DOLLAR_VOLUME, MAX_RISK_ATR, EXTENSION_ATR = 5.0, 10e6, 2.5, 1.0
NAN = float("nan")

# Economically motivated, small, ordered grids. Numeric dimensions define the neighbourhood.
GRIDS = {
    "pullback": {
        "touch_atr": (0.1, 0.25, 0.5),        # how close the 3-day low must come to EMA20/50
        "depth_atr": (0.5, 1.0, 1.5),         # how far below the 20-day high the pullback went
        "stop_atr": (0.2, 0.5, 1.0),          # stop buffer below the 3-day low
        "exit": ("struct", "2R", "3R", "trail3"),
        "horizon": (10, 20, 40),
        "regime": ("all", "bull", "bull_breadth"),
        "strength": ("none", "rs_qqq", "rs_sector", "mom_top30", "vadj_top30"),
    },
    "breakout": {
        "lookback": (20, 50, 100),            # length of the base / resistance window
        "base": ("none", "base"),             # baseline base rules (age, touches, depth) or none
        "rel_volume": (1.0, 1.5, 2.0),        # breakout-day volume vs the 50-day average
        "stop_atr": (0.5, 1.0),               # stop buffer below min(breakout low, resistance)
        "exit": ("struct", "2R", "3R", "trail3"),
        "horizon": (10, 20, 40),
        "regime": ("all", "bull", "bull_breadth"),
        "strength": ("none", "rs_qqq", "rs_sector", "mom_top30", "vadj_top30"),
    },
}
NUMERIC = {"pullback": ("touch_atr", "depth_atr", "stop_atr", "horizon"),
           "breakout": ("lookback", "rel_volume", "stop_atr", "horizon")}
EXIT_DIMS = {"pullback": ("stop_atr", "exit", "horizon"), "breakout": ("lookback", "stop_atr", "exit", "horizon")}
BASELINE = {
    "pullback": {"touch_atr": 0.25, "depth_atr": 1.0, "stop_atr": 0.2, "exit": "struct", "horizon": 20, "regime": "all", "strength": "none"},
    "breakout": {"lookback": 50, "base": "base", "rel_volume": 1.5, "stop_atr": 0.5, "exit": "struct", "horizon": 20, "regime": "all", "strength": "none"},
}
OUTCOMES = ("stop", "target", "timeout", "trail")
COLUMNS = {
    "pullback": {"ticker": "H", "t": "H", "day": "i", "atr": "d", "rs_qqq": "d", "rs_sector": "d", "mom": "d", "vadj": "d",
                 "touch": "d", "depth": "d", "entry": "d", "low3": "d", "swing_high": "d"},
    "breakout": {"ticker": "H", "t": "H", "day": "i", "atr": "d", "rs_qqq": "d", "rs_sector": "d", "mom": "d", "vadj": "d",
                 "lookback": "H", "rel_volume": "d", "base": "b", "level": "d", "base_low": "d", "entry": "d", "low_t": "d"},
}


# ---------------------------------------------------------------- data


def ordinal_bars(bars):
    """Same bars with dates as day ordinals (ints), the form used throughout this module."""
    return ss.Bars(bars.ticker, [d.toordinal() if isinstance(d, date) else d for d in bars.dates], bars.open, bars.high,
                   bars.low, bars.close, bars.volume)


def convert_cache(json_dir, store_dir):
    """JSON cache (one file per ticker) -> compact binary store: int32 day ordinals, then 5 float64 columns."""
    store = Path(store_dir)
    store.mkdir(parents=True, exist_ok=True)
    index = {}
    for path in sorted(Path(json_dir).glob("*.json")):
        raw = json.loads(path.read_text())
        days = array("i", (date.fromisoformat(d).toordinal() for d in raw["dates"]))
        with open(store / f"{raw['ticker']}.bin", "wb") as handle:
            days.tofile(handle)
            for key in ("open", "high", "low", "close", "volume"):
                array("d", raw[key]).tofile(handle)
        index[raw["ticker"]] = len(days)
    (store / "index.json").write_text(json.dumps(index))
    return index


class Store:
    """Bars loaded on demand from the binary store."""

    def __init__(self, store_dir):
        self.dir = Path(store_dir)
        self.lengths = json.loads((self.dir / "index.json").read_text())

    def tickers(self):
        return sorted(self.lengths)

    def bars(self, ticker):
        n = self.lengths[ticker]
        with open(self.dir / f"{ticker}.bin", "rb") as handle:
            days = array("i")
            days.fromfile(handle, n)
            columns = []
            for _ in range(5):
                column = array("d")
                column.fromfile(handle, n)
                columns.append(column)
        return ss.Bars(ticker, days, *columns)


class MemoryStore:
    """Same interface over bars in memory (tests)."""

    def __init__(self, bars):
        self.data = {t: ordinal_bars(b) for t, b in bars.items()}
        self.lengths = {t: len(b) for t, b in self.data.items()}

    def tickers(self):
        return sorted(self.data)

    def bars(self, ticker):
        return self.data[ticker]


def snapshot_id(store):
    digest = hashlib.sha256()
    for ticker in store.tickers():
        b = store.bars(ticker)
        digest.update(f"{ticker}:{len(b)}:{b.dates[0]}:{b.dates[-1]}:{b.close[-1]}".encode())
    return digest.hexdigest()[:12]


def rolling_max_before(values, n):
    """out[i] = max(values[i-n:i]), from i = n on."""
    out, window = [None] * len(values), deque()
    for i, value in enumerate(values):
        while window and window[0] < i - n:
            window.popleft()
        if i >= n:
            out[i] = values[window[0]]
        while window and values[window[-1]] <= value:
            window.pop()
        window.append(i)
    return out


def momentum(close):
    """6-1 month momentum (t-126 to t-21) and the same divided by annualised volatility of the last 126 days."""
    n = len(close)
    mom, vadj = [None] * n, [None] * n
    rets = [0.0] + [math.log(close[i] / close[i - 1]) for i in range(1, n)]
    s = s2 = 0.0
    for i in range(1, n):
        s += rets[i]
        s2 += rets[i] * rets[i]
        if i > 126:
            s -= rets[i - 126]
            s2 -= rets[i - 126] * rets[i - 126]
        if i >= 126:
            mom[i] = close[i - 21] / close[i - 126] - 1
            var = max(s2 / 126 - (s / 126) ** 2, 0.0) * 126 / 125
            vadj[i] = mom[i] / math.sqrt(var * 252) if var > 0 else None
    return mom, vadj


def market_context(market, sectors):
    """Per day: QQQ bull (close above a rising SMA200) and breadth (share of sector ETFs above their SMA200)."""
    m200 = ss.sma(market.close, 200)
    bull = {}
    for i, d in enumerate(market.dates):
        bull[d] = m200[i] is not None and i >= 220 and market.close[i] > m200[i] and m200[i] > m200[i - 20]
    above = {}
    for b in sectors:
        m = ss.sma(b.close, 200)
        for i, d in enumerate(b.dates):
            if m[i] is not None:
                above.setdefault(d, []).append(b.close[i] > m[i])
    breadth = {d: sum(x) / len(x) for d, x in above.items() if len(x) >= 6}
    return {"bull": bull, "breadth": breadth}


def regime_of(context, d):
    if not context["bull"].get(d, False):
        return "other"
    return "bull_breadth" if context["breadth"].get(d, 0) >= 6 / 9 else "bull"


def period_of(day):
    d = date.fromordinal(day) if isinstance(day, int) else day
    for name, (start, end) in PERIODS.items():
        if start <= d <= end:
            return name
    return None


# ---------------------------------------------------------------- candidates


def ticker_features(bars, benchmarks):
    """Candidates of both strategies for one ticker, its liquid uptrend days, and its momentum values.

    benchmarks: {"QQQ": {day: close}, optionally "SECTOR": {day: close}}. Uses bars up to t only."""
    cfg = {**ss.DEFAULTS, "rs_lookback": 63, "breakout_lookback": max(GRIDS["breakout"]["lookback"])}
    series = ss.compute(bars, benchmarks, cfg)
    mom, vadj = momentum(bars.close)
    o, h, l, c, v = bars.open, bars.high, bars.low, bars.close, bars.volume
    rmax = {n: rolling_max_before(h, n) for n in GRIDS["breakout"]["lookback"]}
    rs_sector = series.get("rs_SECTOR")
    pullbacks, breakouts, pool, ranks = [], [], [], []
    for t in range(253, len(bars) - 1):
        ctx = ss._context(bars, series, t, cfg)
        if ctx is None:
            continue
        a, e20, e50, m200 = ctx["atr14"], ctx["ema20"], ctx["ema50"], ctx["sma200"]
        if c[t] < MIN_PRICE or ctx["dollar_volume_avg50"] < MIN_DOLLAR_VOLUME or a <= 0:
            continue
        d = bars.dates[t]
        if mom[t] is not None and vadj[t] is not None:
            ranks.append((d, mom[t], vadj[t]))
        if c[t] <= m200:
            continue
        pool.append(t)
        common = {"t": t, "day": d, "atr": a, "rs_qqq": _num(series["rs_QQQ"][t]), "rs_sector": _num(rs_sector[t] if rs_sector else None),
                  "mom": _num(mom[t]), "vadj": _num(vadj[t])}
        # Pullback: the baseline structure, with the loosest touch and depth of the grid.
        if e20 > e50 > m200 and e50 > series["ema50"][t - 10] and c[t] > e50:
            span = h[t] - l[t]
            location = (c[t] - l[t]) / span if span > 0 else 0.0
            if c[t] > o[t] and c[t] > c[t - 1] and location >= ss.DEFAULTS["reversal_close_location"]:
                swing_high, pull_low = max(h[t - 19:t + 1]), min(l[t - 2:t + 1])
                touch = min((pull_low - e20) / a, (pull_low - e50) / a)
                depth = (swing_high - pull_low) / a
                if touch <= max(GRIDS["pullback"]["touch_atr"]) and depth >= min(GRIDS["pullback"]["depth_atr"]):
                    pullbacks.append({**common, "touch": touch, "depth": depth, "entry": h[t] + 0.01, "low3": pull_low,
                                      "swing_high": swing_high})
        # Breakout: baseline trend, fresh close above each lookback's resistance, not extended.
        if c[t] > e20 > e50 > m200 and ctx["volume_avg50"]:
            rel_volume = v[t] / ctx["volume_avg50"]
            if rel_volume >= min(GRIDS["breakout"]["rel_volume"]):
                for n in GRIDS["breakout"]["lookback"]:
                    level, previous = rmax[n][t], rmax[n][t - 1]
                    if level is None or previous is None or not (c[t] > level and c[t - 1] <= previous):
                        continue
                    if (c[t] - level) / a > EXTENSION_ATR:
                        continue
                    level, age, touches, base_low = ss.resistance(bars, t, n, ss.DEFAULTS["resistance_touch_atr"] * a)
                    base_ok = age >= ss.DEFAULTS["min_base_bars"] and touches >= ss.DEFAULTS["min_touches"] and (
                        (level - base_low) / level <= ss.DEFAULTS["max_base_depth"])
                    breakouts.append({**common, "lookback": n, "rel_volume": rel_volume, "base": base_ok, "level": level,
                                      "base_low": base_low, "entry": c[t], "low_t": l[t]})
    return pullbacks, breakouts, pool, ranks


def _num(value):
    return NAN if value is None else value


class Columns:
    """Typed columns of one strategy's candidates, in (ticker, t) order."""

    def __init__(self, strategy):
        self.spec = dict(COLUMNS[strategy])
        self.data = {k: array(code) for k, code in self.spec.items()}

    def append(self, row):
        for key, column in self.data.items():
            column.append(row[key])

    def add_column(self, name, code, values):
        self.spec[name] = code
        self.data[name] = array(code, values)

    def __len__(self):
        return len(self.data["t"])

    def row(self, i):
        return {k: column[i] for k, column in self.data.items()}


def rank_percentiles(rank_rows, candidates):
    """Percentile of each candidate's momentum and vol-adjusted momentum among all liquid tickers on its day.
    rank_rows: {day: (array of mom, array of vadj)} over every liquid ticker-day."""
    mom_pct, vadj_pct = [], []
    sorted_rows = {}
    for i in range(len(candidates)):
        day, m, va = candidates.data["day"][i], candidates.data["mom"][i], candidates.data["vadj"][i]
        rows = rank_rows.get(day)
        if rows is None or len(rows[0]) < 20 or math.isnan(m) or math.isnan(va):
            mom_pct.append(NAN)
            vadj_pct.append(NAN)
            continue
        if day not in sorted_rows:
            sorted_rows[day] = (sorted(rows[0]), sorted(rows[1]))
        ms, vs = sorted_rows[day]
        m32, va32 = array("f", (m, va))  # the cross-section is stored as float32
        mom_pct.append(bisect.bisect_left(ms, m32) / len(ms))
        vadj_pct.append(bisect.bisect_left(vs, va32) / len(vs))
    candidates.add_column("mom_pct", "d", mom_pct)
    candidates.add_column("vadj_pct", "d", vadj_pct)


# ---------------------------------------------------------------- exits


def simulate_exit(bars, t, kind, entry, stop, target, horizon, trail_atr=None, atr=None):
    """Long trade from a signal on bar t, same chronology as strategy_screen.simulate.

    kind: "stop_buy" (buy stop at entry, valid on bar t+1 only) or "next_open".
    A trailing stop (trail_atr x ATR below the highest close since the fill) is raised at each close
    and applies from the next bar, so no bar uses its own later prices.
    Returns None if the trade cannot finish inside the data, "not_filled", or a result dict."""
    i = t + 1
    if i + horizon > len(bars):
        return None
    if kind == "stop_buy":
        if bars.high[i] < entry:
            return "not_filled"
        fill = max(bars.open[i], entry)
    else:
        fill = bars.open[i]
    if fill <= stop:
        return "not_filled"
    risk, current, best = fill - stop, stop, fill

    def done(j, price, outcome, gap):
        if outcome == "stop" and current > stop:
            outcome = "trail"
        return {"r": (price - fill) / risk, "held": j - t, "fill": fill, "risk": risk, "outcome": outcome, "gap": gap,
                "exit_index": j, "exit_price": price}

    for j in range(i, i + horizon):
        opening = fill if j == i else bars.open[j]
        if target is not None and opening >= target:
            return done(j, opening, "target", True)
        if opening <= current:
            return done(j, opening, "stop", True)
        if bars.low[j] <= current:
            return done(j, current, "stop", False)
        if target is not None and bars.high[j] >= target:
            return done(j, target, "target", False)
        if trail_atr:
            best = max(best, bars.close[j])
            current = max(current, best - trail_atr * atr)
    j = i + horizon - 1
    return done(j, bars.close[j], "timeout", False)


def plan(strategy, cand, exit_cfg):
    """Entry kind, entry, stop, target and trail of a candidate under an exit configuration, or None if
    the signal-day risk checks fail (risk size; for structural targets also the baseline reward/risk)."""
    a = cand["atr"]
    if strategy == "pullback":
        kind, entry, stop = "stop_buy", cand["entry"], cand["low3"] - exit_cfg["stop_atr"] * a
        structural, min_rr = cand["swing_high"], ss.DEFAULTS["pullback_min_rr"]
    else:
        kind, entry, stop = "next_open", cand["entry"], min(cand["low_t"], cand["level"]) - exit_cfg["stop_atr"] * a
        structural, min_rr = cand["entry"] + (cand["level"] - cand["base_low"]), ss.DEFAULTS["breakout_min_rr"]
    risk = entry - stop
    if not 0 < risk <= MAX_RISK_ATR * a:
        return None
    mode = exit_cfg["exit"]
    if mode == "struct":
        if round((structural - entry) / risk, 2) < min_rr:  # rounded like strategy_screen._levels
            return None
        target = structural
    elif mode.endswith("R"):
        target = entry + float(mode[:-1]) * risk
    else:
        target = None
    trail = float(mode[len("trail"):]) if mode.startswith("trail") else None
    return kind, entry, stop, target, trail


class Sims:
    """Outcome of every candidate under one exit configuration (NaN r = no trade)."""

    def __init__(self):
        self.r, self.cost = array("d"), array("d")
        self.held, self.fill_day, self.exit_day = array("h"), array("i"), array("i")
        self.outcome, self.gap = array("b"), array("b")

    def add(self, result, bars):
        if isinstance(result, dict):
            self.r.append(result["r"])
            self.cost.append(result["fill"] / result["risk"])
            self.held.append(result["held"])
            self.fill_day.append(bars.dates[result["exit_index"] - result["held"] + 1])
            self.exit_day.append(bars.dates[result["exit_index"]])
            self.outcome.append(OUTCOMES.index(result["outcome"]))
            self.gap.append(result["gap"] and result["outcome"] == "stop")
        else:
            self.r.append(NAN)
            self.cost.append(NAN)
            self.held.append(-1)
            self.fill_day.append(0)
            self.exit_day.append(0)
            self.outcome.append(-1)
            self.gap.append(0)


# ---------------------------------------------------------------- configurations


def configs(strategy):
    grid = GRIDS[strategy]
    names = list(grid)
    for values in itertools.product(*(grid[n] for n in names)):
        yield dict(zip(names, values))


def config_id(strategy, cfg):
    return strategy[0] + "-" + "-".join(str(cfg[k]) for k in GRIDS[strategy])


def exit_key(strategy, cfg):
    return tuple(cfg[k] for k in EXIT_DIMS[strategy])


def neighbours(strategy, cfg):
    """Configurations that differ by one step in one numeric dimension."""
    grid = GRIDS[strategy]
    for name in NUMERIC[strategy]:
        values = grid[name]
        index = values.index(cfg[name])
        for step in (-1, 1):
            if 0 <= index + step < len(values):
                yield {**cfg, name: values[index + step]}


def criterion(cand, name, value, regime):
    """One entry filter on signal-day values. Exit dimensions always pass."""
    if name == "touch_atr":
        return cand["touch"] <= value
    if name == "depth_atr":
        return cand["depth"] >= value
    if name == "rel_volume":
        return cand["rel_volume"] >= value
    if name == "base":
        return value == "none" or bool(cand["base"])
    if name == "regime":
        return value == "all" or (value == "bull" and regime != "other") or (value == "bull_breadth" and regime == "bull_breadth")
    if name == "strength":
        if value == "none":
            return True
        key, threshold = {"rs_qqq": ("rs_qqq", None), "rs_sector": ("rs_sector", None), "mom_top30": ("mom_pct", 0.7),
                          "vadj_top30": ("vadj_pct", 0.7)}[value]
        x = cand.get(key, NAN)
        if x is None or math.isnan(x):
            return False
        return x > 0 if threshold is None else x >= threshold
    return True


def passes(strategy, cand, cfg, regime):
    if strategy == "breakout" and cand["lookback"] != cfg["lookback"]:
        return False
    return all(criterion(cand, name, cfg[name], regime) for name in GRIDS[strategy])


BIT_POSITIONS = [tuple(bit for bit in range(8) if byte >> bit & 1) for byte in range(256)]


def set_bits(bits, size):
    """Indices of the set bits, ascending."""
    out = []
    for position, byte in enumerate(bits.to_bytes(size // 8 + 1, "little")):
        if byte:
            base = position << 3
            out.extend(base + bit for bit in BIT_POSITIONS[byte])
    return out


def net(r, cost, bps):
    return r - bps / 1e4 * cost


def stats(rows, bps_list=COSTS):
    """rows: (signal day, exit day, r, cost, outcome, gap, ticker). Drawdown runs in exit order."""
    if not rows:
        return {"trades": 0}
    rows = sorted(rows, key=lambda x: (x[1], x[0], x[6]))
    out = {"trades": len(rows)}
    for bps in bps_list:
        out[f"e{bps}"] = statistics.fmean(net(x[2], x[3], bps) for x in rows)
    values = [net(x[2], x[3], SELECT_BPS) for x in rows]
    gains, losses = sum(v for v in values if v > 0), -sum(v for v in values if v <= 0)
    total = peak = drawdown = 0.0
    for v in values:
        total += v
        peak = max(peak, total)
        drawdown = max(drawdown, peak - total)
    years, by_ticker = {}, {}
    for x, v in zip(rows, values):
        years.setdefault(_year(x[0]), []).append(v)
        by_ticker[x[6]] = by_ticker.get(x[6], 0.0) + v
    counted = [sum(y) for y in years.values() if len(y) >= RULES["min_year_trades"]]
    top = sorted(by_ticker.values(), reverse=True)[:5]
    out.update({
        "median": statistics.median(values), "win": sum(v > 0 for v in values) / len(values),
        "pf": gains / losses if losses else None, "total": total, "max_dd": drawdown, "worst": min(values),
        "pos_years": sum(y > 0 for y in counted) / len(counted) if counted else None, "years": len(counted),
        "e25_without_top5": (total - sum(top)) / len(values) if len(values) > 5 else None,
        "stops": sum(x[4] == 0 for x in rows) / len(rows), "targets": sum(x[4] == 1 for x in rows) / len(rows),
        "gap_stops": sum(x[5] for x in rows), "tickers": len(by_ticker),
    })
    return out


_YEARS = {}


def _year(day):
    if day not in _YEARS:
        _YEARS[day] = date.fromordinal(day).year if isinstance(day, int) else day.year
    return _YEARS[day]


# ---------------------------------------------------------------- the study


class Study:
    def __init__(self, store, meta):
        self.store, self.meta = store, meta
        self.stocks = [t for t in store.tickers() if t not in NOT_STOCKS]
        market = store.bars(MARKET)
        self.market = market
        self.market_index = {d: i for i, d in enumerate(market.dates)}
        self.qqq = dict(zip(market.dates, market.close))
        self.sectors = {}
        for etf in set(SECTOR_OF.values()):
            if etf in store.lengths:
                b = store.bars(etf)
                self.sectors[etf] = dict(zip(b.dates, b.close))
        self.context = market_context(market, [store.bars(e) for e in SECTOR_ETFS if e in store.lengths])
        self.candidates, self.offsets, self.masks, self.pool = {}, {}, {}, {}
        self._sims = (None, None)

    # -- pass 1: features
    def prepare(self, log=print):
        cands = {s: Columns(s) for s in GRIDS}
        offsets = {s: [] for s in GRIDS}
        rank_rows, pool = {}, {}
        for ticker_id, ticker in enumerate(self.stocks):
            bars = self.store.bars(ticker)
            benchmarks = {"QQQ": self.qqq}
            sector = SECTOR_OF.get(self.meta.get(ticker, {}).get("sector"))
            if sector in self.sectors:
                benchmarks["SECTOR"] = self.sectors[sector]
            pullbacks, breakouts, days, ranks = ticker_features(bars, benchmarks)
            for strategy, rows in (("pullback", pullbacks), ("breakout", breakouts)):
                offsets[strategy].append(len(cands[strategy]))
                for row in sorted(rows, key=lambda x: (x["t"], x.get("lookback", 0))):
                    cands[strategy].append({**row, "ticker": ticker_id})
            for t in days:
                entry = pool.setdefault(bars.dates[t], (array("H"), array("H")))
                entry[0].append(ticker_id)
                entry[1].append(t)
            for d, m, va in ranks:
                entry = rank_rows.setdefault(d, (array("f"), array("f")))
                entry[0].append(m)
                entry[1].append(va)
            if log and ticker_id % 100 == 0:
                log(f"features {ticker_id}/{len(self.stocks)}")
        for strategy in GRIDS:
            offsets[strategy].append(len(cands[strategy]))
            rank_percentiles(rank_rows, cands[strategy])
        del rank_rows
        self.candidates, self.offsets, self.pool = cands, offsets, pool
        self.masks = {s: self.filter_masks(s) for s in GRIDS}

    def filter_masks(self, strategy):
        """One bitset per (dimension, value) of the entry filters; a configuration ANDs its values.
        The breakout lookback is handled by the simulation, which only fills candidates of its lookback."""
        cands = self.candidates[strategy]
        regimes = [regime_of(self.context, d) for d in cands.data["day"]]
        masks = {}
        for name, values in GRIDS[strategy].items():
            if name in EXIT_DIMS[strategy]:
                continue
            for value in values:
                flags = bytearray(len(cands) // 8 + 1)
                for index in range(len(cands)):
                    if criterion(cands.row(index), name, value, regimes[index]):
                        flags[index >> 3] |= 1 << (index & 7)
                masks[(name, value)] = int.from_bytes(flags, "little")
        return masks

    # -- pass 2: one exit configuration at a time
    def sims(self, strategy, key):
        if self._sims[0] == (strategy, key):
            return self._sims[1]
        exit_cfg = dict(zip(EXIT_DIMS[strategy], key))
        cands, offsets = self.candidates[strategy], self.offsets[strategy]
        out = Sims()
        for ticker_id, ticker in enumerate(self.stocks):
            start, end = offsets[ticker_id], offsets[ticker_id + 1]
            if start == end:
                continue
            bars = None
            for index in range(start, end):
                cand = cands.row(index)
                result = None
                if strategy != "breakout" or cand["lookback"] == exit_cfg["lookback"]:
                    p = plan(strategy, cand, exit_cfg)
                    if p is not None:
                        bars = bars or self.store.bars(ticker)
                        kind, entry, stop, target, trail = p
                        result = simulate_exit(bars, cand["t"], kind, entry, stop, target, exit_cfg["horizon"], trail, cand["atr"])
                out.add(result, bars)
        self._sims = ((strategy, key), out)
        return out

    def trades(self, strategy, cfg, sims=None):
        """Trades of a configuration in signal order, one open trade per ticker at a time.
        Rows: (signal day, exit day, r, cost, outcome, gap, ticker id, candidate index, fill day)."""
        cands = self.candidates[strategy]
        sims = sims or self.sims(strategy, exit_key(strategy, cfg))
        bits = (1 << len(cands)) - 1
        for name in GRIDS[strategy]:
            if (name, cfg[name]) in self.masks[strategy]:
                bits &= self.masks[strategy][(name, cfg[name])]
        tickers, ts, days = cands.data["ticker"], cands.data["t"], cands.data["day"]
        out, busy_ticker, busy_until = [], None, -1
        for index in set_bits(bits, len(cands)):
            r = sims.r[index]
            if r != r:  # NaN: no trade
                continue
            if tickers[index] == busy_ticker and ts[index] <= busy_until:
                continue
            busy_ticker, busy_until = tickers[index], ts[index] + sims.held[index]
            out.append((days[index], sims.exit_day[index], r, sims.cost[index], sims.outcome[index], sims.gap[index], tickers[index], index,
                        sims.fill_day[index]))
        return out

    def evaluate_all(self, log=print):
        results = {}
        for strategy in GRIDS:
            by_key = {}
            for cfg in configs(strategy):
                by_key.setdefault(exit_key(strategy, cfg), []).append(cfg)
            results[strategy] = []
            for number, (key, cfgs) in enumerate(sorted(by_key.items(), key=lambda kv: str(kv[0]))):
                sims = self.sims(strategy, key)
                for cfg in cfgs:
                    results[strategy].append(self.evaluate(strategy, cfg, sims))
                if log:
                    log(f"{strategy} exit {number + 1}/{len(by_key)} {key}")
        return results

    def evaluate(self, strategy, cfg, sims=None):
        rows = {name: [] for name in PERIODS}
        yearly = {}
        for row in self.trades(strategy, cfg, sims):
            name = period_of(row[0])
            if name:
                rows[name].append(row)
            y = yearly.setdefault(_year(row[0]), [0, 0.0])
            y[0] += 1
            y[1] += net(row[2], row[3], SELECT_BPS)
        return {"strategy": strategy, "config": cfg, "id": config_id(strategy, cfg), "yearly": yearly,
                "dev": stats(rows["dev"]), "validation": stats(rows["validation"])}

    def full_stats(self, strategy, cfg):
        rows = {name: [] for name in PERIODS}
        for row in self.trades(strategy, cfg):
            name = period_of(row[0])
            if name:
                rows[name].append(row)
        return {name: stats(x) for name, x in rows.items()}

    # -- benchmarks
    def benchmark(self, strategy, cfg, period, draws, seed, chunk=20):
        """Matched random entries: for each trade, a random ticker from the liquid uptrend pool on the
        same signal day, next-open entry, the trade's stop distance in ATR, its reward in R, trail and
        horizon. Returns the strategy's net E at 25 bp and the distribution of the random means."""
        cands = self.candidates[strategy]
        trades = []
        for row in self.trades(strategy, cfg):
            if period_of(row[0]) != period:
                continue
            cand = cands.row(row[7])
            kind, entry, stop, target, trail = plan(strategy, cand, cfg)
            trades.append((row, (entry - stop) / cand["atr"], None if target is None else (target - entry) / (entry - stop), trail))
        if not trades:
            return {}
        actual = statistics.fmean(net(t[0][2], t[0][3], SELECT_BPS) for t in trades)
        rng = random.Random(seed)
        horizon = cfg["horizon"]
        sums, counts = [0.0] * draws, [0] * draws
        for first in range(0, draws, chunk):
            requests = {}
            for draw in range(first, min(first + chunk, draws)):
                for row, stop_atr, reward, trail in trades:
                    ids, ts = self.pool.get(row[0], ((), ()))
                    for _ in range(20):
                        if not ids:
                            break
                        k = rng.randrange(len(ids))
                        ticker = self.stocks[ids[k]]
                        if ts[k] + 1 + horizon <= self.store.lengths[ticker]:
                            requests.setdefault(ticker, []).append((ts[k], stop_atr, reward, trail, draw))
                            break
            for ticker, items in requests.items():
                bars = self.store.bars(ticker)
                atr = ss.atr(bars.high, bars.low, bars.close, 14)
                for t, stop_atr, reward, trail, draw in items:
                    base, a = bars.close[t], atr[t]
                    stop = base - stop_atr * a
                    result = simulate_exit(bars, t, "next_open", base, stop, None if reward is None else base + reward * (base - stop),
                                           horizon, trail, a)
                    if isinstance(result, dict):
                        sums[draw] += net(result["r"], result["fill"] / result["risk"], SELECT_BPS)
                        counts[draw] += 1
        means = sorted(s / c for s, c in zip(sums, counts) if c)
        return {"period": period, "trades": len(trades), "e25": round(actual, 3),
                "random_mean_e25": round(statistics.fmean(means), 3) if means else None,
                "random_p95_e25": round(means[int(0.95 * (len(means) - 1))], 3) if means else None,
                "percentile_vs_random": round(sum(m < actual for m in means) / len(means), 3) if means else None,
                "draws": len(means), **self.qqq_comparison([t[0] for t in trades])}

    def qqq_comparison(self, rows):
        """Each trade's return on its fill price (net of 25 bp) vs QQQ from the same open to the same close."""
        stock, market = [], []
        for row in rows:
            start, end = self.market_index.get(row[8]), self.market_index.get(row[1])
            if start is None or end is None:
                continue
            stock.append(row[2] / row[3] - SELECT_BPS / 1e4)
            market.append(self.market.close[end] / self.market.open[start] - 1)
        if not stock:
            return {}
        return {"mean_trade_return_pct": round(100 * statistics.fmean(stock), 3),
                "mean_qqq_same_window_pct": round(100 * statistics.fmean(market), 3),
                "mean_excess_pct": round(100 * (statistics.fmean(stock) - statistics.fmean(market)), 3),
                "beats_qqq_share": round(sum(s > m for s, m in zip(stock, market)) / len(stock), 3)}


# ---------------------------------------------------------------- selection


def window_stats(result, years):
    n = sum(result["yearly"].get(y, (0, 0.0))[0] for y in years)
    total = sum(result["yearly"].get(y, (0, 0.0))[1] for y in years)
    counted = [result["yearly"][y][1] for y in years if result["yearly"].get(y, (0, 0))[0] >= RULES["min_year_trades"]]
    return n, (total / n if n else None), (sum(v > 0 for v in counted) / len(counted) if counted else None)


def robustness(strategy, result, by_id, years):
    """Median net expectancy at 25 bp over the configuration and its numeric neighbours in the window.
    Neighbours with too few trades count as 0 R, so a lone spike cannot win."""
    values = []
    for cfg in [result["config"], *neighbours(strategy, result["config"])]:
        n, e, _ = window_stats(by_id[config_id(strategy, cfg)], years)
        values.append(e if n >= RULES["neighbour_min_trades"] and e is not None else 0.0)
    return statistics.median(values)


def select(strategy, results, by_id, years, count):
    """Pre-declared rule, using only the given years."""
    years = list(years)
    scale = max(len(years), 1) / 8  # minimum trades are stated for an 8-year window
    scored = []
    for result in results:
        n, e, positive = window_stats(result, years)
        if n < RULES["min_trades"][strategy] * scale or e is None or e <= 0:
            continue
        if positive is None or positive < RULES["min_positive_years"]:
            continue
        scored.append((robustness(strategy, result, by_id, years), e, result["id"]))
    scored.sort(reverse=True)
    return [(score, by_id[i]) for score, _, i in scored[:count]]


def walk_forward(study, strategy, results, by_id, first_year, last_year, start_year):
    """Each year: select with the same rule on all earlier years, then trade that pick for the year."""
    picks, rows = [], []
    for year in range(first_year, last_year + 1):
        chosen = select(strategy, results, by_id, range(start_year, year), 1)
        if not chosen:
            picks.append({"year": year, "config": None})
            continue
        score, result = chosen[0]
        trades = [x for x in study.trades(strategy, result["config"]) if _year(x[0]) == year]
        values = [net(x[2], x[3], SELECT_BPS) for x in trades]
        picks.append({"year": year, "config": result["id"], "train_score": round(score, 3), "trades": len(values),
                      "e25": round(statistics.fmean(values), 3) if values else None})
        rows.extend(trades)
    return picks, stats(rows)


def spearman(xs, ys):
    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2
            i = j + 1
        return out
    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


def marginals(strategy, results, period):
    """Median net expectancy (25 bp) of all configurations sharing each parameter value."""
    out = {}
    for name, values in GRIDS[strategy].items():
        out[name] = {}
        for value in values:
            es = [r[period]["e25"] for r in results if r["config"][name] == value and r[period]["trades"] >= RULES["neighbour_min_trades"]]
            out[name][str(value)] = {"configs": len(es), "median_e25": round(statistics.median(es), 3) if es else None,
                                     "share_positive": round(sum(e > 0 for e in es) / len(es), 3) if es else None}
    return out


# ---------------------------------------------------------------- report


CSV_STATS = ("trades", "e0", "e10", "e25", "e50", "win", "pf", "max_dd", "pos_years", "e25_without_top5")


def flat(result):
    row = {"strategy": result["strategy"], "id": result["id"], **result["config"]}
    for name in ("dev", "validation"):
        for key in CSV_STATS:
            value = result[name].get(key)
            row[f"{name}_{key}"] = round(value, 4) if isinstance(value, float) else value
    return row


def _fmt(value, digits=3):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:+.{digits}f}" if digits == 3 else f"{value:.{digits}f}"
    return str(value)


def render(report):
    """Readable summary of report.json."""
    run = report["run"]
    lines = [f"# Strategy research run {run['snapshot']}", "",
             f"- Tickers: {run['tickers']} (today's Finviz list, survivors only). First signal {run['first_signal']}, last session {run['last_session']}.",
             "- Periods: " + ", ".join(f"{k} {v[0]} to {v[1]}" for k, v in run["periods"].items()) + ".",
             f"- Selection: development period only, net expectancy at {run['select_bps']} bp, rules {json.dumps(run['rules'])}.",
             f"- Random benchmark: {run['draws']} draws, seed {run['seed']}. Runtime {run.get('seconds', '-')} s.", ""]
    for strategy, body in report["strategies"].items():
        lines += [f"## {strategy}", "",
                  f"- Configurations tested: {body['configs']}. Share with positive net expectancy at 25 bp (and 30+ trades): "
                  f"dev {body['share_positive_e25']['dev']:.1%}, validation {body['share_positive_e25']['validation']:.1%}.",
                  f"- Rank correlation of dev vs validation expectancy across configurations: {_fmt(body['dev_vs_validation_rank_correlation'], 2)}.", ""]
        lines.append("| | config | period | trades | E 0bp | E 10bp | E 25bp | E 50bp | win | PF | max DD R | pos. years | E25 w/o top 5 | random E25 | pct vs random | excess vs QQQ % |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for label, item in body["selected"].items():
            for period, st in item["periods"].items():
                bench = item["benchmark"].get(period) or {}
                if not st.get("trades"):
                    lines.append(f"| {label} | {item['id']} | {period} | 0 | | | | | | | | | | | | |")
                    continue
                lines.append(f"| {label} | {item['id']} | {period} | {st['trades']} | {_fmt(st['e0'])} | {_fmt(st['e10'])} | {_fmt(st['e25'])} | "
                             f"{_fmt(st['e50'])} | {st['win']:.0%} | {_fmt(st['pf'], 2)} | {st['max_dd']:.1f} | "
                             f"{'-' if st['pos_years'] is None else format(st['pos_years'], '.0%')} | {_fmt(st['e25_without_top5'])} | "
                             f"{_fmt(bench.get('random_mean_e25'))} | {_fmt(bench.get('percentile_vs_random'), 2)} | {_fmt(bench.get('mean_excess_pct'), 2)} |")
        wf = body["walk_forward"]
        lines += ["", "Walk-forward (select on all earlier years, trade the next year):", ""]
        for pick in wf["picks"]:
            lines.append(f"- {pick['year']}: {pick.get('config') or 'no configuration passed the rules'}"
                         + (f", {pick['trades']} trades, E25 {_fmt(pick['e25'])}" if pick.get("config") else ""))
        o = wf["out_of_sample"]
        if o.get("trades"):
            lines.append(f"- Out of sample total: {o['trades']} trades, E0 {_fmt(o['e0'])}, E10 {_fmt(o['e10'])}, E25 {_fmt(o['e25'])}, "
                         f"max DD {o['max_dd']:.1f} R, positive years {'-' if o['pos_years'] is None else format(o['pos_years'], '.0%')}.")
        lines += ["", "Median E25 of all configurations sharing a value (dev / validation):", ""]
        for name in body["marginals"]["dev"]:
            parts = []
            for value, dev in body["marginals"]["dev"][name].items():
                val = body["marginals"]["validation"][name][value]
                parts.append(f"{value}: {_fmt(dev['median_e25'])} / {_fmt(val['median_e25'])}")
            lines.append(f"- {name}: " + "; ".join(parts))
        lines.append("")
    return "\n".join(lines) + "\n"


def run(store, meta, out, draws=200, seed=20261118, log=print):
    started = time.time()
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    study = Study(store, meta)
    study.prepare(log)
    log("candidates: " + ", ".join(f"{s} {len(c)}" for s, c in study.candidates.items()))
    results = study.evaluate_all(log)
    log(f"evaluated {sum(len(r) for r in results.values())} configurations in {time.time() - started:.0f}s")
    days = [d for c in study.candidates.values() for d in c.data["day"]]
    first_signal, last_session = date.fromordinal(min(days)), date.fromordinal(study.market.dates[-1])
    start_year = first_signal.year
    dev_years = range(start_year, PERIODS["dev"][1].year + 1)
    report = {"run": {"tickers": len(study.stocks), "snapshot": snapshot_id(store), "first_signal": first_signal.isoformat(),
                      "last_session": last_session.isoformat(),
                      "periods": {k: [max(v[0], first_signal).isoformat(), min(v[1], last_session).isoformat()] for k, v in PERIODS.items()},
                      "grids": GRIDS, "baseline": BASELINE, "rules": RULES, "select_bps": SELECT_BPS, "seed": seed, "draws": draws},
              "strategies": {}}
    with open(out / "configs.csv", "w", newline="") as handle:
        rows = [flat(r) for s in results for r in results[s]]
        writer = csv.DictWriter(handle, fieldnames=list(dict.fromkeys(k for row in rows for k in row)))
        writer.writeheader()
        writer.writerows(rows)
    for strategy, res in results.items():
        by_id = {r["id"]: r for r in res}
        pairs = [(r["dev"]["e25"], r["validation"]["e25"]) for r in res
                 if r["dev"]["trades"] >= RULES["neighbour_min_trades"] and r["validation"]["trades"] >= RULES["neighbour_min_trades"]]
        shortlist = select(strategy, res, by_id, dev_years, RULES["shortlist"])
        chosen = [("baseline", config_id(strategy, BASELINE[strategy]))] + [(f"pick_{k + 1}", r["id"]) for k, (_, r) in enumerate(shortlist)]
        detail = {}
        for label, cid in chosen:
            cfg = by_id[cid]["config"]
            detail[label] = {"id": cid, "config": cfg, "dev_robustness": round(robustness(strategy, by_id[cid], by_id, dev_years), 3),
                             "periods": study.full_stats(strategy, cfg),
                             "benchmark": {p: study.benchmark(strategy, cfg, p, draws, seed) for p in PERIODS}}
            log(f"{strategy} {label} {cid} done")
        picks, oos = walk_forward(study, strategy, res, by_id, start_year + 4, last_session.year, start_year)
        report["strategies"][strategy] = {
            "configs": len(res),
            "share_positive_e25": {p: round(sum(r[p]["trades"] >= RULES["neighbour_min_trades"] and r[p]["e25"] > 0 for r in res) / len(res), 3)
                                   for p in ("dev", "validation")},
            "dev_vs_validation_rank_correlation": spearman([x for x, _ in pairs], [y for _, y in pairs]),
            "marginals": {p: marginals(strategy, res, p) for p in ("dev", "validation")},
            "selected": detail, "walk_forward": {"picks": picks, "out_of_sample": oos},
        }
    report["run"]["seconds"] = round(time.time() - started)
    report = json.loads(json.dumps(report, default=_json))
    (out / "report.json").write_text(json.dumps(report, indent=1))
    (out / "report.md").write_text(render(report))
    log(f"done in {time.time() - started:.0f}s")
    return report


def _json(value):
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", required=True, help="directory of per-ticker JSON files")
    parser.add_argument("--store", help="binary store directory (default: CACHE/../store)")
    parser.add_argument("--meta", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--draws", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20261118)
    args = parser.parse_args(argv)
    store_dir = Path(args.store or Path(args.cache).parent / "store")
    if not (store_dir / "index.json").exists():
        convert_cache(args.cache, store_dir)
    return run(Store(store_dir), json.loads(Path(args.meta).read_text()), args.out, args.draws, args.seed,
               log=lambda message: print(message, flush=True))


if __name__ == "__main__":
    main()
