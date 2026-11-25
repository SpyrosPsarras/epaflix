#requirements:
#finviz==2.1.0
#requests==2.34.2
#wmill==1.866.0

"""Strategy screener: LONG Pullback / EMA bounce and Breakout / momentum, SHORT Rally rejection, Breakdown and Run break,
plus a Potential short squeeze watchlist (LONG).

Finviz finds the candidates. Daily OHLCV comes from the Finviz chart endpoint
(split adjusted, same host as the screener, no API key). Every indicator is
computed here from completed daily bars only, so a signal on day t uses no data
after day t. It returns JSON and, only if `email` is given, sends the candidates by email.

Every setup carries its direction, entry, stop, target, reward/risk, score, relative strength vs QQQ and
its sector ETF, the market regime (bullish / neutral / bearish from QQQ's trend and sector-ETF breadth)
and its sector ETF's trend. They turn it into an action: long or short when the regime and sector agree,
watch, countertrend_watch or no_trade otherwise. The regime never changes which setups qualify.
Each candidate carries a `recommendation` line: proposed or watch only and why, the strategy and its rank in a bull
market (breakout, then pullback, then squeeze), the score items it earned and the plan. LONG pullback and breakout
stops sit high_beta_stop_atr x beta ATR lower for stocks with beta >= high_beta_min. The LONG score weights come from a
bull-market backtest and include the market (QQQ above its EMA50, not overheated) and the sector ETF trend.
The market phase of the benchmark (uptrend, pullback; in a decline, i.e. a bear regime or a correction after a run or a
big extension: falling, bounce, above_ema50) comes with a recommendation and flags the day it switches, an overextended
bull market (benchmark overextended_bull or more above its SMA200), and a dead cat bounce of the benchmark rejected at
its 200-day line (an alert with a debit spread on the benchmark). SHORT setups (rally rejection, run break, breakdown,
dead cat) are not traded short: in a decline or an overextended bull market they are proposed as bear put debit spreads
with a defined risk, short-dated for a fast move (strikes, expiry and exit in `put_spread`). In the 2016-2026 backtest both the shorts and these
debit spreads lost on average.
`track_days` adds the status of the signals of the previous sessions (open, target, stopped, time exit,
not triggered, skipped). Every setup is a 30-45 session swing: the time exit depends on the stock's beta vs SPY
(higher beta, shorter hold). With momentum_hold_adx above 0, a LONG trade that reaches its target or time exit while
the ADX trend is up stays open until that trend ends (momentum_hold_adx, 0 turns it off). Proposed LONG candidates get a
position of position_size (10%) of the budget, best first, while fewer than max_open_trades (10) of your positions are
held. Your positions come from the positions script (screener.positions, needs persist): every day each one gets hold or sell
by the backtested exit rules, its flags, and a swap when a stalled position could make room for a new setup. Breakdown runs only when
asked for by name (strategy breakdown): it lost in every backtest window. The squeeze rules are an untested hypothesis: validate before trading them.
`finviz_url` takes one or more screener URLs (one per line), e.g. a large/mid-cap list and a high-short-float list.
With `backdrop` (on by default) the email opens with the market backdrop, information only: the next Fed decision and the
cut or hike odds of the fed funds futures, VIX futures and Nasdaq volatility, oil and metals, theme rotation ETFs ranked against
SPY, and how each proposal and position moves with them.
With `persist`, every signal is stored in the Windmill data table `main` (schema `screener`) and followed until it
closes, even after its ticker leaves the screen; closed signals cannot change (enforced by the database).
"""

import bisect
import json
from collections import deque
import math
import re
import smtplib
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from urllib.parse import urlparse

import requests
import wmill
from finviz.screener import NoResults, Screener

FINVIZ_HOSTS = {"finviz.com", "www.finviz.com"}
CHART_URL = "https://finviz.com/api/quote"
QUOTE_URL = "https://finviz.com/quote.ashx"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
STRATEGIES = ("pullback", "breakout", "squeeze", "rally_rejection", "breakdown", "run_break", "dead_cat")
DIRECTIONS = {"pullback": "long", "breakout": "long", "squeeze": "long", "rally_rejection": "short", "breakdown": "short",
              "run_break": "short", "dead_cat": "short"}
ENTRY_TYPES = {"pullback": "stop", "breakout": "open", "squeeze": "open", "rally_rejection": "stop", "breakdown": "stop",
               "run_break": "open", "dead_cat": "open"}
# Breakdown runs only when asked for by name: it lost in every 750-session window of the backtest (about 105 large caps,
# 2017-2020 -18.5R, 2020-2023 -63.5R, 2023-2026 -8.7R), also in a bearish regime (-59.9R in 83 trades), and no filter on
# the regime, trend, RSI, volume, relative strength, stop size or score, nor a closer target, made it positive in all three.
OFF_BY_DEFAULT = ("breakdown",)
STRATEGY_GROUPS = {"all": tuple(s for s in STRATEGIES if s not in OFF_BY_DEFAULT), "long": ("pullback", "breakout", "squeeze"),
                   "both": ("pullback", "breakout"), "short": ("rally_rejection", "run_break", "dead_cat")}
MAX_URLS = 3
BETA_BENCHMARK = "SPY"  # beta is measured against the S&P 500, like Finviz; QQQ if SPY is not loaded
SMTP_RESOURCE = "f/screener/smtp"
# A daily bar counts as final this long after the 16:00 New York close (Finviz free data is delayed).
SESSION_FINAL_AFTER = (16, 20)
MAX_TICKERS = 200
MAX_VALIDATION_DAYS = 750
MAX_TRACK_DAYS = 60
# Finviz sector -> SPDR sector ETF, used for relative strength vs sector and for market breadth.
SECTOR_ETFS = {"Technology": "XLK", "Financial": "XLF", "Healthcare": "XLV", "Industrials": "XLI", "Consumer Cyclical": "XLY",
               "Consumer Defensive": "XLP", "Energy": "XLE", "Utilities": "XLU", "Basic Materials": "XLB",
               "Real Estate": "XLRE", "Communication Services": "XLC"}
# What a LONG setup means in each market regime.
ACTIONS = {"bullish": "long", "neutral": "watch", "bearish": "no_trade", "unknown": "watch"}
# SHORT setups are not traded short: in the bear backtest (600 stocks and 55 ETFs, 2016-2026) shorting them lost in bear
# markets and corrections, while the stocks rose 4-9% on average over the next 30-45 sessions. In a decline (bear regime
# or correction) or an overextended bull market (see market_phase, spread_context) they time a bear put debit spread
# instead, so the loss is capped at the debit (the chosen strategy for now; it lost too, see STRATEGY_PRIORITY).
SHORT_ACTIONS = {"bullish": "countertrend_watch", "neutral": "watch", "bearish": "bear_put_spread", "unknown": "watch"}
# Squeezes also happen in bear-market rallies, so a bearish regime makes them countertrend, not forbidden.
SQUEEZE_ACTIONS = {"bullish": "long", "neutral": "watch", "bearish": "countertrend_watch", "unknown": "watch"}
ACTIONS_BY_DIRECTION = {"long": ACTIONS, "short": SHORT_ACTIONS}
ACTIONS_BY_STRATEGY = {"squeeze": SQUEEZE_ACTIONS}
ACTION_ORDER = {"long": 0, "short": 0, "bear_put_spread": 0, "watch": 1, "countertrend_watch": 2, "no_trade": 3}
TRADE_ACTIONS = ("long", "short", "bear_put_spread")
# Bear put debit spread on a SHORT setup: (buy, sell) put strikes as shares of the last close, nearest listed strikes.
# Short moves are fast, so the spread is short-dated and narrow: at the money / 5% lower, held put_spread_days, take
# profit at put_spread_take_profit of the width. In the backtest (5 sessions) this lost about 0.12 of the debit per trade
# (46% won), against about 0.4 (29% won) for ATM / -10% held for the stock's 30-45 sessions.
PUT_SPREAD_STRIKES = (1.00, 0.95)

DEFAULTS = {
    "benchmarks": ["QQQ"],
    "min_bars": 260,
    "min_price": 5.0,
    "min_dollar_volume": 10_000_000.0,
    "max_missing_sessions": 2,
    "max_zero_volume_days": 2,
    "rs_lookback": 63,
    "max_risk_atr": 2.5,
    # Wider stop for high-beta stocks, LONG pullback and breakout: at beta >= high_beta_min the stop moves
    # high_beta_stop_atr x beta ATR lower. Entry, target and the checks use the stop before this buffer. In the bull
    # backtest (600 stocks, 2016-2026) this raised R per trade for beta >= 1.3 and did not help squeezes.
    "high_beta_min": 1.3,
    "high_beta_stop_atr": 0.5,
    # Score thresholds of the LONG setups, set on the bull backtest (thresholds from signals before 2022-07, checked after)
    "score_beta_min": 1.2,  # pullback: points for beta above this
    "score_min_atr_pct": 0.0215,  # pullback, breakout: points for ATR14 / close above this (the median of the signals)
    "score_strong_rs": 0.05,  # breakout, squeeze: points for relative strength vs the market above this
    "market_hot_change": 0.03,  # breakout, squeeze: points when the market rose less than this in 21 sessions
    "breakout_heavy_volume": 3.0,  # breakout: points for relative volume at or above this
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
    # SHORT, both strategies (hypotheses, not validated)
    "short_min_price": 10.0,
    "short_min_dollar_volume": 20_000_000.0,
    "earnings_block_days": 7,
    "short_float_warning": 0.20,
    # Short rally rejection
    "rally_min_bars": 3,
    "rally_min_atr": 1.0,
    "rally_max_spike_share": 0.6,
    "rally_touch_atr": 0.25,
    "rally_stop_atr": 0.2,
    "rally_min_rr": 1.5,
    # Short breakdown
    "breakdown_lookback": 50,
    "support_touch_atr": 0.5,
    "breakdown_max_extension_atr": 1.0,
    "breakdown_min_rsi": 25.0,
    "breakdown_stop_atr": 0.5,
    "breakdown_min_rr": 2.0,
    # Run break (correction after a run): first close below EMA50 after a run_break_min_run rise in the 126 sessions
    # before a closing high of the last run_break_high_bars, having closed above EMA50 on run_break_above_bars of the last 20
    "run_break_min_run": 0.30,
    "run_break_high_bars": 40,
    "run_break_above_bars": 15,
    "run_break_stop_atr": 0.2,
    "run_break_target_r": 2.0,
    # Volume confirmation of the run break bar (0 = off): relative volume vs the 50-day average at least this. In the
    # backtest (about 105 large caps, three 750-session windows 2017-2026) 1.25 cut the run break loss in every window
    # (-30.3R to -13.4R, -24.2R to -5.0R, -11.9R to +12.8R) by keeping about a fifth of the signals; still negative before 2023.
    "run_break_min_rel_volume": 1.25,
    # Dead cat bounce rejected at the 200-day line (EMA200 or SMA200): closed below it on dead_cat_below_bars of the last
    # 20, bounced dead_cat_bounce_atr ATR off the 20-day low, a high of the last 3 bars within dead_cat_touch_atr ATR of
    # the line, and a bearish close below it. Also an alert when the benchmark does it (market_phase).
    "dead_cat_below_bars": 15,
    "dead_cat_bounce_atr": 2.0,
    "dead_cat_touch_atr": 0.25,
    "dead_cat_stop_atr": 0.2,
    "dead_cat_target_r": 2.0,
    "dead_cat_alert_gap": 10,  # benchmark alert on the first rejection only: none in this many sessions before
    # Potential short squeeze (LONG, hypothesis): price trigger here, short interest live only
    "squeeze_lookback": 20,
    "squeeze_min_rel_volume": 2.0,
    "squeeze_close_location": 0.6,
    "squeeze_max_extension_atr": 3.0,
    "squeeze_max_rsi": 80.0,
    "squeeze_stop_atr": 0.2,
    "squeeze_target_r": 2.0,
    "squeeze_min_short_float": 0.15,
    # Market regime: QQQ vs its SMA200 and the SMA200 slope, plus breadth = share of sector ETFs above their SMA200
    "regime_slope_bars": 20,
    "breadth_bullish": 0.6,
    "breadth_bearish": 0.4,
    # Market phase of the benchmark (see market_phase), from the bear/correction backtest 2016-2026. A decline is a bearish
    # regime or a correction: the close at least correction_drawdown below the highest close of the last phase_high_bars,
    # after a correction_run rise in the 126 sessions before that high or with that high correction_extension above the SMA200.
    "phase_high_bars": 63,
    "correction_drawdown": 0.05,
    "correction_run": 0.10,
    "correction_extension": 0.10,
    "bounce_low_bars": 10,  # bounce: the close bounce_atr ATR above the low of the last bounce_low_bars, or above EMA20
    "bounce_atr": 2.0,
    "deep_drawdown": 0.15,  # debit spreads on SHORT setups lost the most with the benchmark this far below its high
    "overextended_bull": 0.15,  # no decline and the benchmark this far above its SMA200 (QQQ: 13% of days 2016-2026)
    # a pullback within this many sessions of an overextended day: LONG setups are watch only (they lost -0.2R per trade)
    "after_overextended_bars": 20,
    # Bear put debit spread plan: close after put_spread_days sessions at the latest, or earlier when the spread is worth
    # put_spread_take_profit of the strike width; expiry about put_spread_expiry_days calendar days after that exit
    "put_spread_days": 5,
    "index_put_spread_days": 10,  # the benchmark spread on a dead cat alert: 5 sessions lost more there (11 cases)
    # Tight long on a bounce inside a decline (phase bounce): pullback setups become long for bounce_long_days sessions;
    # on the day the benchmark's bounce starts, a long on the benchmark up to its 200-day line, stop at the bounce low,
    # for index_long_days sessions at most
    "bounce_long_days": 10,
    "index_long_days": 10,
    "put_spread_take_profit": 0.5,
    "put_spread_expiry_days": 7,
    # Market overview only: trend of these ETFs next to QQQ
    # SPY/IWM: broad and small-cap market, SMH: semis, MDY: mid caps, TLT: long Treasuries, HYG: high-yield credit
    "context_etfs": ["SPY", "IWM", "MDY", "SMH", "TLT", "HYG"],
    # Validation and tracking: bars from the fill, LONG and SHORT
    # Swing holding period in sessions: swing_max_days for beta <= beta_low, swing_min_days for beta >= beta_high,
    # linear in between; the midpoint when beta is unknown. Trades not stopped or at target exit at that close.
    "swing_min_days": 30,
    "swing_max_days": 45,
    "beta_low": 1.0,
    "beta_high": 2.0,
    "beta_lookback": 252,
    # Momentum hold of LONG trades (0 = off, else the ADX14 level). When the target or the time exit comes with
    # ADX14 >= this and +DI > -DI (at the previous close for the target, at that close for the time exit), the trade stays
    # open; the stop does not apply while that trend held at the previous close; the trade exits at the next open after the
    # first close with the trend off. In the backtest (about 105 large caps, $10,000, 5% risk per trade, at most 25% per
    # position) level 20 lifted the LONG portfolio in all three 750-session windows: +10% to +16% (2017-2020), +59% to +68%
    # (2020-2023), +57% to +88% (2023-2026); R per trade rose only in the last two.
    "momentum_hold_adx": 20.0,
    # Position sizing of LONG trades: position_size of the budget per trade, at most max_open_trades open at once (one per
    # ticker), the best candidates first. Open trades are your positions recorded with the positions script (needs
    # persist), less those the screen says to sell today.
    # In the backtest (about 105 large caps, longs, ADX hold) 10% x 10 made +36% / +30% / +88% in 2017-2020 / 2020-2023 /
    # 2023-2026 with a maximum drawdown of 15-18%; 2.5% x 10 left about 80% of the budget in cash and made +9% / +8% / +18%.
    "position_size": 0.10,
    "max_open_trades": 10,
    # Your positions: stop and target when neither the position nor a screener LONG signal on that ticker in the 14 days
    # before the buy gives them: position_stop_atr ATR14 (at the buy) below the buy price, a position_target_r R target.
    "position_stop_atr": 2.0,
    "position_target_r": 2.0,
    # Swap: with every slot taken, a new LONG setup scoring swap_min_score or more may replace a stalled position, held
    # swap_stalled_days sessions or more and within swap_band_r R of its buy price (0 days = off). In the backtest (10% x
    # 10, ADX hold, two sets of about 105 large caps, three 750-session windows each) this beat no swap in 4 of 6 windows
    # (+1023% against +876% over the six together); swapping only for a score 20 or 30 points higher lost in 5 and 6 of 6.
    "swap_stalled_days": 10,
    "swap_min_score": 60.0,
    "swap_band_r": 0.5,
    # Diversification: at most max_per_industry LONG trades per Finviz industry (0 = no limit), counting your positions
    # (less those to sell today) and the proposals above; a setup past that is listed as an alternative to the ones
    # taken. In the backtest (10% x 10, ADX hold, the six windows of the swap study) 2 per industry made +942% against
    # +876% without a limit, better in 4 of 6 windows; 1 per industry made +665%, as the gains come in clusters
    # (semiconductors 2023-2026). A proposal whose daily returns over the last 63 sessions correlate overlap_correlation
    # or more with a position or an earlier proposal gets a note only (0 = off): skipping them made +825%.
    "max_per_industry": 2,
    "overlap_correlation": 0.8,
    # ETF breakouts (1 on, 0 off): the breakout rules on the sector, industry and theme ETFs of ETF_SETUPS, proposed
    # when the ETF's rotation accelerates: its 10-session return less SPY's above its 63-session one scaled to 10
    # sessions by more than etf_min_acceleration. Backtest (47 ETFs, 2016-2026, ADX hold, net of 10 bp, bullish market):
    # +1.01R / +0.64R per trade before / after mid-2022 (39 / 55 trades) with the acceleration above 0, -0.50R / +0.03R
    # (8 / 22) without it; about 35 more requests.
    "etf_setups": 1,
    "etf_min_acceleration": 0.0,
    # Market backdrop in the result and the email (1 on, 0 off): Fed, VIX, oil, metals, theme rotation, correlations.
    # Information only; about 45 more requests.
    "backdrop": 1,
    # Email: 1 the short report (proposals, positions, the 3 fastest rotating ETFs, extreme move, alerts only when a level
    # in ALERT_LEVELS is crossed), 0 the long one with every setup and the full backdrop.
    "compact_report": 1,
    # Extreme move block when the benchmark closes this much up or down on the day (0 = off); odds from EXTREME_STATS.
    "extreme_move": 0.03,
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


def parse_industry(html):
    """Finviz industry name from a quote page, or None."""
    match = re.search(r'f=ind(?:ustry)?_[a-z0-9]+"[^>]*>(?:<span[^>]*>)?([^<]+)<', html)
    return match.group(1).strip() if match and match.group(1).strip() else None


def fetch_sector(ticker):
    return parse_sector(_get(QUOTE_URL, {"t": ticker, "p": "d"}).text)


def fetch_profile(ticker):
    """Sector and industry from one quote page."""
    html = _get(QUOTE_URL, {"t": ticker, "p": "d"}).text
    return parse_sector(html), parse_industry(html)


SNAPSHOT_FIELD = r'>{}</a></div></td><td[^>]*><div[^>]*><a[^>]*><b>(?:<small[^>]*>)?([^<]+)<'
MONTHS = {m: i + 1 for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))}


def parse_snapshot(html, today):
    """Earnings date (the next one, or the last one if none is scheduled), short float and short ratio from a Finviz quote page.

    Finviz shows the earnings date without a year; the year that puts it closest to today is used."""
    out = {"earnings": None, "earnings_text": None, "short_float": None, "short_ratio": None, "industry": parse_industry(html)}
    match = re.search(SNAPSHOT_FIELD.format("Earnings"), html)
    if match:
        text = match.group(1).strip()
        out["earnings_text"] = text
        parts = text.split()
        if len(parts) >= 2 and parts[0] in MONTHS and parts[1].isdigit():
            options = []
            for year in (today.year - 1, today.year, today.year + 1):
                try:
                    options.append(date(year, MONTHS[parts[0]], int(parts[1])))
                except ValueError:
                    pass
            out["earnings"] = min(options, key=lambda d: abs((d - today).days)) if options else None
    for key, label in (("short_float", "Short Float"), ("short_ratio", "Short Ratio")):
        match = re.search(SNAPSHOT_FIELD.format(label), html)
        if match:
            try:
                value = float(match.group(1).strip().rstrip("%"))
            except ValueError:
                continue
            out[key] = value / 100 if key == "short_float" else value
    return out


def fetch_snapshot(ticker, today):
    return parse_snapshot(_get(QUOTE_URL, {"t": ticker, "p": "d"}).text, today)


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


def adx(high, low, close, n=14):
    """Wilder's ADX with +DI and -DI: (adx, plus_di, minus_di), None until there is enough history."""
    m = len(close)
    plus, minus = [None] * m, [None] * m
    for i in range(1, m):
        up, down = high[i] - high[i - 1], low[i - 1] - low[i]
        plus[i] = up if up > down and up > 0 else 0.0
        minus[i] = down if down > up and down > 0 else 0.0
    tr, p, q = wilder(true_range(high, low, close), n), wilder(plus, n), wilder(minus, n)
    pdi, ndi, dx = [None] * m, [None] * m, [None] * m
    for i in range(m):
        if tr[i]:
            pdi[i], ndi[i] = 100 * p[i] / tr[i], 100 * q[i] / tr[i]
            total = pdi[i] + ndi[i]
            dx[i] = 100 * abs(pdi[i] - ndi[i]) / total if total else 0.0
    out = [None] * m
    first = next((i for i, x in enumerate(dx) if x is not None), None)
    if first is not None and first + 2 * n - 1 <= m:
        current = sum(dx[first:first + n]) / n
        out[first + n - 1] = current
        for i in range(first + n, m):
            current = (current * (n - 1) + dx[i]) / n
            out[i] = current
    return out, pdi, ndi


def trend_up(bars, level):
    """Per bar: ADX14 >= level and +DI > -DI at that close (the momentum hold condition)."""
    cached = getattr(bars, "_adx", None)
    if cached is None or len(cached[0]) != len(bars):
        cached = adx(bars.high, bars.low, bars.close)
        bars._adx = cached
    return [a is not None and a >= level and p > q for a, p, q in zip(*cached)]


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


SWING_BARS = 5


def swing_lows(low, k):
    """True at p if low[p] is the lowest of the k bars on each side. Known only at p + k, so use p <= t - k at day t."""
    n = len(low)
    return [k <= p < n - k and low[p] == min(low[p - k:p + k + 1]) for p in range(n)]


def rolling_beta(bars, benchmark, lookback):
    """Beta of daily returns vs the benchmark closes (a date -> close dict) over the last `lookback` return pairs up to
    each bar; None before 60 pairs. Returns are taken between consecutive dates that both have prices."""
    out, pairs = [None] * len(bars), deque()
    sx = sy = sxx = sxy = 0.0
    previous = None
    for i, (d, c) in enumerate(zip(bars.dates, bars.close)):
        b = benchmark.get(d)
        if b and c and c > 0:
            if previous:
                x, y = b / previous[1] - 1, c / previous[0] - 1
                pairs.append((x, y))
                sx, sy, sxx, sxy = sx + x, sy + y, sxx + x * x, sxy + x * y
                if len(pairs) > lookback:
                    x, y = pairs.popleft()
                    sx, sy, sxx, sxy = sx - x, sy - y, sxx - x * x, sxy - x * y
            previous = (c, b)
        n = len(pairs)
        if n >= 60:
            variance = sxx / n - (sx / n) ** 2
            if variance > 1e-12:
                out[i] = (sxy / n - sx * sy / n / n) / variance
    return out


def holding_days(beta, cfg):
    """Sessions to hold a swing: swing_max_days at beta <= beta_low, swing_min_days at beta >= beta_high, linear between."""
    low, high = cfg["swing_min_days"], cfg["swing_max_days"]
    if beta is None:
        return round((low + high) / 2)
    span = cfg["beta_high"] - cfg["beta_low"]
    share = min(1.0, max(0.0, (beta - cfg["beta_low"]) / span)) if span > 0 else float(beta >= cfg["beta_low"])
    return int(round(high - share * (high - low)))


def _beta_at(series, t):
    values = series.get("beta")
    return values[t] if values and t < len(values) else None


def compute(bars, benchmarks, cfg, beta_closes=None):
    series = {
        "ema20": ema(bars.close, 20),
        "ema50": ema(bars.close, 50),
        "sma200": sma(bars.close, 200),
        "ema200": ema(bars.close, 200),
        "atr14": atr(bars.high, bars.low, bars.close, 14),
        "rsi14": rsi(bars.close, 14),
        "volume_avg50": prior_mean(bars.volume, 50),
        "dollar_volume_avg50": prior_mean([c * v for c, v in zip(bars.close, bars.volume)], 50),
        "swing_low": swing_lows(bars.low, SWING_BARS),
    }
    for name, closes in benchmarks.items():
        series[f"rs_line_{name}"], series[f"rs_{name}"] = relative_strength(bars, closes, cfg["rs_lookback"])
    series["beta"] = rolling_beta(bars, beta_closes, cfg["beta_lookback"]) if beta_closes else [None] * len(bars)
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


def market_context(dates, market_bars, sector_bars=None, slope=20):
    """Per stock date, from bars up to that date only: the market benchmark above its EMA50, its 21-session change, the
    sector ETF in an uptrend (see trend_labels) and the sector ETF's 63-session change minus the market's. None if unknown."""
    keys = ("market_above_ema50", "market_change_21", "sector_uptrend", "sector_vs_market_63")
    out = {k: [None] * len(dates) for k in keys}
    if market_bars is None:
        return out
    m_index, mc = {d: i for i, d in enumerate(market_bars.dates)}, market_bars.close
    m50 = ema(mc, 50)
    if sector_bars is not None:
        s_index, sc = {d: i for i, d in enumerate(sector_bars.dates)}, sector_bars.close
        trend = trend_labels(sector_bars.dates, sc, slope)
    for k, d in enumerate(dates):
        i = m_index.get(d)
        if i is None:
            continue
        if m50[i] is not None:
            out["market_above_ema50"][k] = mc[i] > m50[i]
        if i >= 21:
            out["market_change_21"][k] = mc[i] / mc[i - 21] - 1
        j = s_index.get(d) if sector_bars is not None else None
        if j is not None:
            out["sector_uptrend"][k] = None if trend.get(d) is None else trend[d] == "up"
            if i >= 63 and j >= 63:
                out["sector_vs_market_63"][k] = (sc[j] / sc[j - 63]) - (mc[i] / mc[i - 63])
    return out


def _at(series, key, t):
    values = series.get(key)
    return values[t] if values and t < len(values) else None


def _market_score(scores, series, t, cfg, points):
    """Points from the market and the sector ETF: (market above EMA50, market not hot, sector ETF uptrend, sector ETF beats
    the market over 63 sessions); 0 skips an item."""
    market = cfg["benchmarks"][0] if cfg["benchmarks"] else "market"
    above, change = _at(series, "market_above_ema50", t), _at(series, "market_change_21", t)
    up, vs = _at(series, "sector_uptrend", t), _at(series, "sector_vs_market_63", t)
    etf = series.get("sector_etf") or "sector ETF"
    items = [
        ("market_above_ema50", above is True, f"{market} {'above' if above else 'below'} its EMA50" if above is not None else "no market data"),
        ("market_not_hot", change is not None and change < cfg["market_hot_change"],
         f"{market} {change * 100:+.1f}% in 21 sessions, best under {cfg['market_hot_change'] * 100:+.0f}%" if change is not None else "no market data"),
        ("sector_uptrend", up is True, f"{etf} {'in an uptrend' if up else 'not in an uptrend'}" if up is not None else "no sector ETF data"),
        ("sector_beats_market", vs is not None and vs > 0, f"{etf} {vs * 100:+.1f}% vs {market} over 63 sessions" if vs is not None else "no sector ETF data"),
    ]
    for (name, earned, detail), value in zip(items, points):
        if value:
            _points(scores, name, value, earned, detail)


def _rs_score(scores, series, t, cfg, points, weaker=False):
    rs = relative_strength_at(series, t, cfg)
    for key, value in zip(("market", "sector"), points):
        if not value:
            continue
        item = rs.get(key, {"benchmark": None, "change": None})
        change = item["change"]
        if change is not None:
            detail = f"{change * 100:+.1f}% vs {item['benchmark']} over {cfg['rs_lookback']} bars"
        elif key == "sector" and not item["benchmark"]:
            detail = "sector unknown or no sector ETF data"
        else:
            detail = "no aligned benchmark data"
        if weaker:
            _points(scores, f"underperforms_{key}", value, change is not None and change < 0, detail)
        else:
            _points(scores, f"outperforms_{key}", value, change is not None and change > 0, detail)


def _context(bars, series, t, cfg):
    """Indicator values at t, or None if history is too short."""
    needed = ("ema20", "ema50", "sma200", "atr14", "rsi14", "volume_avg50", "dollar_volume_avg50")
    if t < max(252, cfg["breakout_lookback"] + 1, cfg["breakdown_lookback"] + 1) or any(series[k][t] is None for k in needed) or series["ema50"][t - 10] is None:
        return None
    values = {k: series[k][t] for k in needed}
    values["close"] = bars.close[t]
    return values


def _liquidity(checks, ctx, cfg, short=False):
    price, volume = (cfg["short_min_price"], cfg["short_min_dollar_volume"]) if short else (cfg["min_price"], cfg["min_dollar_volume"])
    _check(checks, "price", ctx["close"] >= price, f"close {ctx['close']:.2f}, minimum {price:g}")
    _check(checks, "liquidity", ctx["dollar_volume_avg50"] >= volume,
           f"50-day average dollar volume {ctx['dollar_volume_avg50'] / 1e6:.1f}M, minimum {volume / 1e6:g}M")


def _result(strategy, bars, t, checks, scores, levels, ctx):
    passed = all(c["passed"] for c in checks)
    return {
        "strategy": strategy,
        "direction": DIRECTIONS[strategy],
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


def _levels(entry, stop, target, direction="long"):
    sign = 1 if direction == "long" else -1
    risk = sign * (entry - stop)
    return {"entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2),
            "risk_per_share": round(risk, 2), "reward_risk": round(sign * (target - entry) / risk, 2) if risk > 0 else None}


def high_beta_stop(levels, series, t, atr_value, cfg):
    """LONG levels with the stop moved high_beta_stop_atr x beta ATR lower when beta >= high_beta_min; others unchanged.
    `base_stop` keeps the stop of the setup rule and `stop_buffer_atr` the ATRs added."""
    beta = _beta_at(series, t)
    if not levels or beta is None or beta < cfg["high_beta_min"] or cfg["high_beta_stop_atr"] <= 0 or not atr_value:
        return levels
    buffer = cfg["high_beta_stop_atr"] * beta
    out = _levels(levels["entry"], levels["stop"] - buffer * atr_value, levels["target"])
    return {**levels, **out, "base_stop": levels["stop"], "stop_buffer_atr": round(buffer, 2)}


def _strong_rs(scores, series, t, cfg, points):
    change = (relative_strength_at(series, t, cfg).get("market") or {}).get("change")
    _points(scores, "strong_rs", points, change is not None and change > cfg["score_strong_rs"],
            f"{change * 100:+.1f}% vs the market over {cfg['rs_lookback']} bars, best above {cfg['score_strong_rs'] * 100:+.0f}%"
            if change is not None else "no aligned benchmark data")


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
    # Weights from the bull backtest: only items that ranked in both periods (before and after mid-2022).
    _rs_score(scores, series, t, cfg, (0, 38))
    beta = _beta_at(series, t)
    _points(scores, "high_beta", 30, beta is not None and beta > cfg["score_beta_min"],
            f"beta {beta:.2f}, best above {cfg['score_beta_min']:g}" if beta is not None else "beta unknown")
    _points(scores, "wide_range", 17, a / c[t] > cfg["score_min_atr_pct"],
            f"ATR14 {a / c[t]:.1%} of the price, best above {cfg['score_min_atr_pct']:.2%}")
    _points(scores, "closes_above_prior_high", 15, c[t] > h[t - 1], f"close {c[t]:.2f} vs previous high {h[t - 1]:.2f}")
    levels = high_beta_stop(levels, series, t, a, cfg)
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
    # Weights from the bull backtest: only items that ranked in both periods (before and after mid-2022).
    benchmarks = cfg["benchmarks"]
    _rs_score(scores, series, t, cfg, (18, 0))
    span = h[t] - l[t]
    location = (c[t] - l[t]) / span if span > 0 else 0.0
    _points(scores, "strong_close", 15, location >= 0.75, f"close at {location:.0%} of the day's range, best 75% or more")
    _market_score(scores, series, t, cfg, (13, 9, 8, 0))
    _points(scores, "wide_range", 12, a / c[t] > cfg["score_min_atr_pct"],
            f"ATR14 {a / c[t]:.1%} of the price, best above {cfg['score_min_atr_pct']:.2%}")
    _points(scores, "strong_volume", 12, rel_volume >= cfg["breakout_heavy_volume"],
            f"relative volume {rel_volume:.2f}, best {cfg['breakout_heavy_volume']:g} or more")
    _strong_rs(scores, series, t, cfg, 7)
    line = series.get(f"rs_line_{benchmarks[0]}", [None] * (t + 1)) if benchmarks else [None] * (t + 1)
    window = [x for x in line[t - 49:t + 1] if x is not None]
    _points(scores, "rs_line_high", 6, line[t] is not None and line[t] >= max(window),
            f"relative strength line vs {benchmarks[0] if benchmarks else '-'} at a 50-day high" if line[t] is not None else "no aligned benchmark data")
    levels = high_beta_stop(levels, series, t, a, cfg)
    return _with_rs(_result("breakout", bars, t, checks, scores, levels, ctx), series, t, cfg)


# ---------------------------------------------------------------- SHORT strategies (hypotheses, not validated)


def next_support(bars, series, t, entry, risk, atr_value):
    """Target for a short: the highest confirmed swing low of the last 252 bars at least 1R below the entry, + 0.1 ATR.
    Falls back to entry - 2R when there is none within 3R."""
    flags, low = series["swing_low"], bars.low
    best = None
    if risk > 0:
        for p in range(max(SWING_BARS, t - 251), t - SWING_BARS + 1):
            if flags[p] and low[p] <= entry - risk and (best is None or low[p] > best):
                best = low[p]
    if best is None or best < entry - 3 * risk:
        return entry - 2 * risk, "2R (no swing-low support between 1R and 3R below the entry)"
    return best + 0.1 * atr_value, f"swing-low support {best:.2f} + 0.1 ATR"


def evaluate_rally_rejection(bars, series, t, cfg):
    checks, scores = [], []
    ctx = _context(bars, series, t, cfg)
    if ctx is None:
        _check(checks, "history", False, "not enough history for EMA50, SMA200 and ATR14")
        return _result("rally_rejection", bars, t, checks, scores, None, None)
    h, l, o, c, v = bars.high, bars.low, bars.open, bars.close, bars.volume
    a, e20, e50, m200 = ctx["atr14"], ctx["ema20"], ctx["ema50"], ctx["sma200"]
    _liquidity(checks, ctx, cfg, short=True)
    _check(checks, "downtrend", c[t] < e50 < series["ema50"][t - 10],
           f"close {c[t]:.2f}, EMA50 {e50:.2f}, EMA50 ten bars ago {series['ema50'][t - 10]:.2f}")
    # The rally starts at the lowest low of t-15..t-1 (the latest one if tied); rally_duration needs it at t-3 or earlier.
    b = max(range(t - 15, t), key=lambda j: (-l[j], j))
    n = t - b
    top = max(c[b + 1:t + 1])
    gain = top - c[b]
    up_closes = sum(c[j] > c[j - 1] for j in range(b + 1, t + 1))
    biggest = max(c[j] - c[j - 1] for j in range(b + 1, t + 1))
    _check(checks, "rally_duration", n >= cfg["rally_min_bars"], f"rally of {n} bars from the low {l[b]:.2f} on {bars.dates[b]}, minimum {cfg['rally_min_bars']}")
    _check(checks, "rally_size", top - l[b] >= cfg["rally_min_atr"] * a,
           f"highest close {top:.2f} is {(top - l[b]) / a:.2f} ATR above the low, minimum {cfg['rally_min_atr']:g}")
    _check(checks, "rally_participation", up_closes * 2 >= n, f"{up_closes} of {n} rally bars closed higher, minimum half")
    share = biggest / gain if gain > 0 else 1.0
    _check(checks, "rally_not_a_spike", gain > 0 and share <= cfg["rally_max_spike_share"],
           f"largest one-day gain is {share:.0%} of the rally, maximum {cfg['rally_max_spike_share']:.0%}")
    tolerance = cfg["rally_touch_atr"] * a
    rally_high = max(h[t - 2:t + 1])
    levels = [("EMA20", e20), ("EMA50", e50)]
    flags = series["swing_low"]
    for p in range(t - 60, t - 9):
        if flags[p] and any(c[j] <= l[p] - 0.5 * a for j in range(p + 1, t)):
            levels.append((f"broken support {l[p]:.2f}", l[p]))
    hit = [(name, x) for name, x in levels if rally_high >= x - tolerance and c[t] < x]
    _check(checks, "resistance_rejection", hit,
           f"high of last 3 bars {rally_high:.2f} reached {hit[0][0]} ({hit[0][1]:.2f}) and closed below it" if hit
           else f"high of last 3 bars {rally_high:.2f} did not reach EMA20 {e20:.2f}, EMA50 {e50:.2f} or a broken support "
                f"within {cfg['rally_touch_atr']:g} ATR with a close below it")
    span = h[t] - l[t]
    location = (c[t] - l[t]) / span if span > 0 else 1.0
    _check(checks, "bearish_bar", c[t] < o[t] and c[t] < c[t - 1] and location <= 0.5,
           f"open {o[t]:.2f}, close {c[t]:.2f}, previous close {c[t - 1]:.2f}, close at {location:.0%} of the day's range")
    entry = l[t] - 0.01
    stop = rally_high + cfg["rally_stop_atr"] * a
    risk = stop - entry
    target, source = next_support(bars, series, t, entry, risk, a)
    levels_out = _levels(entry, stop, target, "short")
    levels_out["target_source"] = source
    _check(checks, "risk_size", 0 < risk <= cfg["max_risk_atr"] * a, f"risk {risk:.2f} = {risk / a:.2f} ATR, maximum {cfg['max_risk_atr']:g}")
    _check(checks, "reward_risk", levels_out["reward_risk"] is not None and levels_out["reward_risk"] >= cfg["rally_min_rr"],
           f"reward/risk {levels_out['reward_risk']} to the {source}, minimum {cfg['rally_min_rr']:g}")
    _rs_score(scores, series, t, cfg, (20, 10), weaker=True)
    rsi_now, rsi_before = ctx["rsi14"], series["rsi14"][t - 1]
    _points(scores, "rsi_turning_down", 15, 40 <= rsi_now <= 60 and rsi_before is not None and rsi_now < rsi_before,
            f"RSI14 {rsi_now:.1f} vs {rsi_before if rsi_before is None else round(rsi_before, 1)} the day before, best falling within 40-60")
    rally_volume = sum(v[t - 4:t]) / 4
    _points(scores, "rally_volume_dry_up", 15, rally_volume < ctx["volume_avg50"], f"rally volume {rally_volume / ctx['volume_avg50']:.2f}x the 50-day average")
    _points(scores, "rejection_volume", 10, v[t] > v[t - 1], f"signal volume {v[t]:,.0f} vs {v[t - 1]:,.0f} the day before")
    wick = (h[t] - max(o[t], c[t])) / span if span > 0 else 0.0
    _points(scores, "upper_wick", 10, wick >= 0.4, f"upper wick {wick:.0%} of the day's range, best 40% or more")
    _points(scores, "below_sma200", 10, c[t] < m200, f"close {c[t]:.2f} vs SMA200 {m200:.2f}")
    _points(scores, "failed_reclaim", 10, any(h[t] > x > c[t] for x in (e20, e50)), "high above EMA20 or EMA50, close below it")
    result = _with_rs(_result("rally_rejection", bars, t, checks, scores, levels_out, ctx), series, t, cfg)
    result["max_risk_atr"] = cfg["max_risk_atr"]
    result["notes"] = [f"gap up of {(o[j] - h[j - 1]) / a:.1f} ATR on {bars.dates[j]} inside the rally: news or squeeze driven"
                       for j in range(b + 1, t + 1) if o[j] - h[j - 1] >= 1.5 * a]
    return result


def support(bars, end, lookback, tolerance):
    """Lowest low of the lookback bars before end, its age, how many separate times price came within tolerance, and the highest high."""
    start = end - lookback
    lows = bars.low[start:end]
    level = min(lows)
    last = start + max(i for i, x in enumerate(lows) if x == level)
    touches, inside = 0, False
    for x in lows:
        near = x <= level + tolerance
        touches += near and not inside
        inside = near
    return level, end - last, touches, max(bars.high[start:end])


def evaluate_breakdown(bars, series, t, cfg):
    checks, scores = [], []
    ctx = _context(bars, series, t, cfg)
    if ctx is None:
        _check(checks, "history", False, "not enough history for EMA50, SMA200 and ATR14")
        return _result("breakdown", bars, t, checks, scores, None, None)
    h, l, o, c, v = bars.high, bars.low, bars.open, bars.close, bars.volume
    a, e20, e50, m200 = ctx["atr14"], ctx["ema20"], ctx["ema50"], ctx["sma200"]
    lookback = cfg["breakdown_lookback"]
    _liquidity(checks, ctx, cfg, short=True)
    _check(checks, "below_ema50", c[t] < e50, f"close {c[t]:.2f} vs EMA50 {e50:.2f}")
    level, age, touches, base_high = support(bars, t, lookback, cfg["support_touch_atr"] * a)
    _check(checks, "base_age", age >= cfg["min_base_bars"], f"{lookback}-day low {level:.2f} set {age} bars ago, minimum {cfg['min_base_bars']}")
    _check(checks, "support_touches", touches >= cfg["min_touches"],
           f"{touches} separate tests of {level:.2f} within {cfg['support_touch_atr']:g} ATR, minimum {cfg['min_touches']}")
    depth = (base_high - level) / base_high
    _check(checks, "base_depth", depth <= cfg["max_base_depth"], f"base {depth:.0%} deep ({level:.2f}-{base_high:.2f}), maximum {cfg['max_base_depth']:.0%}")
    previous_level = min(l[t - 1 - lookback:t - 1])
    _check(checks, "fresh_breakdown", c[t] < level and c[t - 1] >= previous_level,
           f"close {c[t]:.2f} vs support {level:.2f}; previous close {c[t - 1]:.2f} vs {previous_level:.2f}")
    span = h[t] - l[t]
    location = (c[t] - l[t]) / span if span > 0 else 1.0
    _check(checks, "bearish_bar", c[t] < o[t] and location <= 1 / 3, f"open {o[t]:.2f}, close {c[t]:.2f}, close at {location:.0%} of the day's range, maximum 33%")
    extension = (level - c[t]) / a
    _check(checks, "not_extended", extension <= cfg["breakdown_max_extension_atr"] and ctx["rsi14"] >= cfg["breakdown_min_rsi"],
           f"{extension:.2f} ATR below support (maximum {cfg['breakdown_max_extension_atr']:g}), RSI14 {ctx['rsi14']:.1f} (minimum {cfg['breakdown_min_rsi']:g})")
    entry = l[t] - 0.01
    stop = max(h[t], level) + cfg["breakdown_stop_atr"] * a
    risk = stop - entry
    target, source = next_support(bars, series, t, entry, risk, a)
    levels_out = _levels(entry, stop, target, "short")
    levels_out["support"] = round(level, 2)
    levels_out["target_source"] = source
    _check(checks, "risk_size", 0 < risk <= cfg["max_risk_atr"] * a, f"risk {risk:.2f} = {risk / a:.2f} ATR, maximum {cfg['max_risk_atr']:g}")
    _check(checks, "reward_risk", levels_out["reward_risk"] is not None and levels_out["reward_risk"] >= cfg["breakdown_min_rr"],
           f"reward/risk {levels_out['reward_risk']} to the {source}, minimum {cfg['breakdown_min_rr']:g}")
    rel_volume = v[t] / ctx["volume_avg50"] if ctx["volume_avg50"] else 0.0
    ctx["rel_volume"] = rel_volume
    ctx["ema20_distance_atr"] = (e20 - c[t]) / a  # reported only; a cap is a hypothesis to test
    _points(scores, "breakdown_volume", 20, rel_volume >= 1.5, f"relative volume {rel_volume:.2f}, best 1.5 or more")
    _rs_score(scores, series, t, cfg, (15, 10), weaker=True)
    _points(scores, "weak_rsi", 10, ctx["rsi14"] < 45, f"RSI14 {ctx['rsi14']:.1f}, best below 45")
    _points(scores, "ema20_below_ema50", 10, e20 < e50, f"EMA20 {e20:.2f} vs EMA50 {e50:.2f}")
    _points(scores, "ema50_falling", 10, e50 < series["ema50"][t - 10], f"EMA50 {e50:.2f} vs {series['ema50'][t - 10]:.2f} ten bars ago")
    tight = (max(h[t - 10:t]) - min(l[t - 10:t])) / a
    _points(scores, "tight_base", 15, tight <= 3, f"last 10 bars span {tight:.1f} ATR, best 3 or less")
    _points(scores, "below_sma200", 10, c[t] < m200, f"close {c[t]:.2f} vs SMA200 {m200:.2f}")
    result = _with_rs(_result("breakdown", bars, t, checks, scores, levels_out, ctx), series, t, cfg)
    result["max_risk_atr"] = cfg["max_risk_atr"]
    return result


def evaluate_run_break(bars, series, t, cfg):
    """Correction after a run (SHORT setup, a bear put debit spread trigger in a decline): the stock rose run_break_min_run or more
    in the 126 sessions before its highest close of the last run_break_high_bars, closed above EMA50 on at least
    run_break_above_bars of the previous 20 sessions, and now makes its first close below EMA50 with a bearish bar
    (close below the open, in the lower half of the range), on run_break_min_rel_volume relative volume (0 turns that off).
    Entry at the next open, stop run_break_stop_atr ATR above the
    5-day high, target run_break_target_r R. Not scored: the backtest found no item that ranks these signals."""
    checks, scores = [], []
    ctx = _context(bars, series, t, cfg)
    if ctx is None:
        _check(checks, "history", False, "not enough history for EMA50, SMA200 and ATR14")
        return _result("run_break", bars, t, checks, scores, None, None)
    h, l, o, c = bars.high, bars.low, bars.open, bars.close
    a, e50 = ctx["atr14"], series["ema50"]
    _liquidity(checks, ctx, cfg, short=True)
    _check(checks, "first_close_below_ema50", c[t] < e50[t] and e50[t - 1] is not None and c[t - 1] >= e50[t - 1],
           f"close {c[t]:.2f} vs EMA50 {e50[t]:.2f}; previous close {c[t - 1]:.2f} vs {e50[t - 1] or 0:.2f}")
    above = sum(e50[j] is not None and c[j] > e50[j] for j in range(t - 20, t))
    _check(checks, "was_above_ema50", above >= cfg["run_break_above_bars"],
           f"{above} of the previous 20 closes above EMA50, minimum {cfg['run_break_above_bars']}")
    k = max(range(t - cfg["run_break_high_bars"], t + 1), key=lambda j: c[j])
    run = c[k] / c[k - 126] - 1 if k >= 126 and c[k - 126] > 0 else None
    _check(checks, "prior_run", run is not None and run >= cfg["run_break_min_run"],
           f"high close {c[k]:.2f} on {bars.dates[k]} is {'-' if run is None else f'{run:+.0%}'} vs 126 sessions earlier, "
           f"minimum {cfg['run_break_min_run']:+.0%}")
    span = h[t] - l[t]
    location = (c[t] - l[t]) / span if span > 0 else 1.0
    _check(checks, "bearish_bar", c[t] < o[t] and span > 0 and location <= 0.5,
           f"open {o[t]:.2f}, close {c[t]:.2f}, close at {location:.0%} of the day's range, maximum 50%")
    if cfg["run_break_min_rel_volume"] > 0:
        rel_volume = bars.volume[t] / ctx["volume_avg50"] if ctx["volume_avg50"] else 0.0
        _check(checks, "volume", rel_volume >= cfg["run_break_min_rel_volume"],
               f"relative volume {rel_volume:.2f}, minimum {cfg['run_break_min_rel_volume']:g}")
    entry = c[t]
    stop = max(h[t - 4:t + 1]) + cfg["run_break_stop_atr"] * a
    risk = stop - entry
    levels = _levels(entry, stop, entry - cfg["run_break_target_r"] * risk, "short")
    levels["target_source"] = f"{cfg['run_break_target_r']:g}R"
    _check(checks, "risk_size", 0 < risk <= cfg["max_risk_atr"] * a, f"risk {risk:.2f} = {risk / a:.2f} ATR, maximum {cfg['max_risk_atr']:g}")
    if run is not None:
        ctx["prior_run"] = run
    result = _with_rs(_result("run_break", bars, t, checks, scores, levels, ctx), series, t, cfg)
    result["max_risk_atr"] = cfg["max_risk_atr"]
    return result


def dead_cat_level(bars, lines, a, t, cfg):
    """The 200-day line (name, value) a dead cat bounce was rejected at on bar t, else None. lines: name -> values."""
    h, l, o, c = bars.high, bars.low, bars.open, bars.close
    if t < 21 or a[t] is None or not a[t] or not (c[t] < o[t] and c[t] < c[t - 1]):
        return None
    if max(c[t - 5:t + 1]) - min(l[t - 20:t + 1]) < cfg["dead_cat_bounce_atr"] * a[t]:
        return None
    for name, values in lines.items():
        line = values[t]
        if line is None or c[t] >= line or max(h[t - 2:t + 1]) < line - cfg["dead_cat_touch_atr"] * a[t]:
            continue
        if sum(values[j] is not None and c[j] < values[j] for j in range(t - 20, t)) >= cfg["dead_cat_below_bars"]:
            return name, line
    return None


def evaluate_dead_cat(bars, series, t, cfg):
    """Dead cat bounce rejected at the 200-day line (SHORT setup, a bear put debit spread trigger in a decline or an
    overextended bull market): the stock closed below its EMA200 or SMA200 on dead_cat_below_bars of the previous 20
    sessions, bounced dead_cat_bounce_atr ATR or more off its 20-day low, reached the line (a high of the last 3 bars
    within dead_cat_touch_atr ATR of it) and closed below it on a bearish bar (below the open and the previous close).
    Entry at the next open, stop dead_cat_stop_atr ATR above the 3-day high, target dead_cat_target_r R. Not scored."""
    checks, scores = [], []
    ctx = _context(bars, series, t, cfg)
    if ctx is None or series["ema200"][t] is None:
        _check(checks, "history", False, "not enough history for EMA200, SMA200 and ATR14")
        return _result("dead_cat", bars, t, checks, scores, None, None)
    h, l, o, c = bars.high, bars.low, bars.open, bars.close
    a = ctx["atr14"]
    _liquidity(checks, ctx, cfg, short=True)
    lines = {"EMA200": series["ema200"], "SMA200": series["sma200"]}
    below = {k: sum(v[j] is not None and c[j] < v[j] for j in range(t - 20, t)) for k, v in lines.items()}
    _check(checks, "below_200", max(below.values()) >= cfg["dead_cat_below_bars"],
           f"{below['EMA200']} of the previous 20 closes below EMA200, {below['SMA200']} below SMA200, minimum {cfg['dead_cat_below_bars']}")
    low = min(l[t - 20:t + 1])
    bounce = (max(c[t - 5:t + 1]) - low) / a
    _check(checks, "bounce", bounce >= cfg["dead_cat_bounce_atr"],
           f"bounce of {bounce:.1f} ATR from the 20-day low {low:.2f}, minimum {cfg['dead_cat_bounce_atr']:g}")
    hit = dead_cat_level(bars, lines, series["atr14"], t, cfg)
    near = min(lines.items(), key=lambda kv: abs(kv[1][t] - max(h[t - 2:t + 1])))
    name, line = hit or (near[0], near[1][t])
    _check(checks, "rejected_at_200", hit is not None,
           f"3-day high {max(h[t - 2:t + 1]):.2f}, close {c[t]:.2f} vs {name} {line:.2f} (open {o[t]:.2f}, previous close {c[t - 1]:.2f})")
    entry = c[t]
    stop = max(h[t - 2:t + 1]) + cfg["dead_cat_stop_atr"] * a
    risk = stop - entry
    levels = _levels(entry, stop, entry - cfg["dead_cat_target_r"] * risk, "short")
    levels["target_source"] = f"{cfg['dead_cat_target_r']:g}R"
    levels["line"], levels["line_name"] = round(line, 2), name
    _check(checks, "risk_size", 0 < risk <= cfg["max_risk_atr"] * a, f"risk {risk:.2f} = {risk / a:.2f} ATR, maximum {cfg['max_risk_atr']:g}")
    ctx["bounce_atr"] = bounce
    result = _with_rs(_result("dead_cat", bars, t, checks, scores, levels, ctx), series, t, cfg)
    result["max_risk_atr"] = cfg["max_risk_atr"]
    return result


# ---------------------------------------------------------------- Potential short squeeze (LONG, hypothesis, not validated)


def evaluate_squeeze(bars, series, t, cfg):
    """Price part of a potential short squeeze: a breakout above the 20-day high on heavy volume with a strong close,
    above EMA20 and EMA50, not yet extended. No uptrend is required: squeezes often start in beaten-down stocks.

    Short interest (short float, days to cover) is only known for today, so squeeze_checks adds it live; validation and
    backtests can only test this price trigger. Entry at the next open, stop under the signal bar, target squeeze_target_r."""
    checks, scores = [], []
    ctx = _context(bars, series, t, cfg)
    if ctx is None:
        _check(checks, "history", False, "not enough history for EMA50, SMA200 and ATR14")
        return _result("squeeze", bars, t, checks, scores, None, None)
    o, h, l, c, v = bars.open, bars.high, bars.low, bars.close, bars.volume
    a, e20, e50 = ctx["atr14"], ctx["ema20"], ctx["ema50"]
    lookback = cfg["squeeze_lookback"]
    _liquidity(checks, ctx, cfg)
    level, previous_level = max(h[t - lookback:t]), max(h[t - 1 - lookback:t - 1])
    _check(checks, "range_breakout", c[t] > level and c[t - 1] <= previous_level,
           f"close {c[t]:.2f} vs {lookback}-day high {level:.2f}; previous close {c[t - 1]:.2f} vs {previous_level:.2f}")
    _check(checks, "above_emas", c[t] > e20 and c[t] > e50, f"close {c[t]:.2f}, EMA20 {e20:.2f}, EMA50 {e50:.2f}")
    rel_volume = v[t] / ctx["volume_avg50"] if ctx["volume_avg50"] else 0.0
    ctx["rel_volume"] = rel_volume
    _check(checks, "volume", rel_volume >= cfg["squeeze_min_rel_volume"],
           f"relative volume {rel_volume:.2f}, minimum {cfg['squeeze_min_rel_volume']:g}")
    span = h[t] - l[t]
    location = (c[t] - l[t]) / span if span > 0 else 0.0
    _check(checks, "strong_close", location >= cfg["squeeze_close_location"],
           f"close at {location:.0%} of the day's range, minimum {cfg['squeeze_close_location']:.0%}")
    extension = (c[t] - e20) / a
    _check(checks, "not_extended", extension <= cfg["squeeze_max_extension_atr"] and ctx["rsi14"] <= cfg["squeeze_max_rsi"],
           f"{extension:.2f} ATR above EMA20 (maximum {cfg['squeeze_max_extension_atr']:g}), RSI14 {ctx['rsi14']:.1f} (maximum {cfg['squeeze_max_rsi']:g})")
    entry = c[t]
    stop = l[t] - cfg["squeeze_stop_atr"] * a
    risk = entry - stop
    levels = _levels(entry, stop, entry + cfg["squeeze_target_r"] * risk)
    levels["breakout_level"] = round(level, 2)
    _check(checks, "risk_size", 0 < risk <= cfg["max_risk_atr"] * a, f"risk {risk:.2f} = {risk / a:.2f} ATR, maximum {cfg['max_risk_atr']:g}")
    year_high = max(h[t - 251:t + 1])
    drawdown = min(l[t - 62:t + 1]) / year_high - 1
    ctx["low_vs_52w_high"] = drawdown
    # 60 points from price and market here, weights from the bull backtest; squeeze_checks adds 40 from short interest.
    _market_score(scores, series, t, cfg, (0, 22, 0, 0))
    _rs_score(scores, series, t, cfg, (0, 13))
    _strong_rs(scores, series, t, cfg, 12)
    _rs_score(scores, series, t, cfg, (5, 0))
    _market_score(scores, series, t, cfg, (0, 0, 5, 3))
    return _with_rs(_result("squeeze", bars, t, checks, scores, levels, ctx), series, t, cfg)


EVALUATORS = {"pullback": evaluate_pullback, "breakout": evaluate_breakout, "squeeze": evaluate_squeeze,
              "rally_rejection": evaluate_rally_rejection, "breakdown": evaluate_breakdown, "run_break": evaluate_run_break,
              "dead_cat": evaluate_dead_cat}


# ---------------------------------------------------------------- validation
#
# Execution model, also returned as validation.assumptions:
# - Signals use completed bars up to and including the signal day t only.
# - Pullback: buy stop at the entry, valid on bar t+1 only; a gap above it fills at the open.
#   Breakout: buy at the open of bar t+1. A fill at or below the stop is skipped.
# - Each bar is handled in time order: first its open, then its range. An open at or beyond
#   the stop or target exits at that open. Inside a bar the order of high and low is unknown,
#   so when both the stop and the target are inside the range, the stop counts.
# - Trades still open after their holding period (30-45 sessions by beta, see holding_days) exit at that bar's close.
# - One trade per ticker and strategy at a time. Trades of different tickers overlap freely and
#   each risks 1R, so drawdown is a sum over concurrent trades, not a portfolio simulation.
# - Costs are a round trip in basis points of the fill price, converted to R per trade.

COST_BPS = (0, 10, 25, 50)
FORWARD_DAYS = (5, 10, 20)
ASSUMPTIONS = [
    "Signals on day t use completed daily bars up to t only; the partial bar of the current session is dropped.",
    "Pullback fills with a buy stop on the next bar only, at the open if it gaps above the entry. Breakout fills at the next open. Fills at or below the stop are skipped.",
    "SHORT setups are pending sell stops valid on the next bar only: an open at or above the stop or at or below the target cancels the order (skipped, no R); a gap below the entry fills at the open unless the risk from that fill exceeds max_risk_atr or less than 1R of reward is left (skipped). Short exits mirror the long ones, stop first. Borrow fees, dividends and short availability are not modelled.",
    "Bars are processed in time order: an open at or beyond the stop or target exits at the open. Daily bars do not show the order of prices inside a bar, so two cases use the conservative assumption and are flagged in `ambiguous`: a bar that touches both the stop and the target counts as stopped (`stop_and_target_same_bar`), and on the bar where a pullback buy stop triggers inside the bar, a low at or below the stop counts as stopped even though it may have come before the fill (`fill_and_stop_same_bar`).",
    "MAE uses the full low of each bar held, including the part of the fill bar before a buy stop triggered, so it is conservative.",
    "Trades not stopped or at target exit at the close of the last bar of the horizon.",
    "With momentum_hold_adx above 0, LONG trades follow the momentum hold (see DEFAULTS): outcome `momentum` when the trend ended, "
    "`held_open` when the trend still holds at the last bar (valued at the last close and counted with the closed trades).",
    "Costs are round trip in basis points of the fill price; no market impact or borrow modelling.",
    "One trade per ticker and strategy at a time; trades of different tickers overlap and each risks 1R. Drawdown sums them in exit order and is not a portfolio simulation.",
    "Forward returns run from the next open (the first possible fill) to the close k sessions after the signal, and are computed only for signals with k bars of future data. They include overlapping signals; a non-overlapping subset is reported too.",
    "Selection bias: the tickers come from today's Finviz screen, so every one of them exists and passed today's filters. Survivorship bias: delisted and acquired stocks are missing. Both make past results look better than a real-time run would have been.",
    "Earnings dates come from the Finviz quote page and are the actual report times as Finviz shows them today, not the dates that were scheduled at the time.",
    "Squeeze: there is no history of short interest, so validation tests only its price trigger (on every ticker, whatever its short float). Live, a squeeze also needs today's short float; tracked squeeze signals are kept only for tickers whose short float today meets the minimum, a proxy.",
]


def _fill(bars, i, signal):
    """Fill price on bar i, or (None, reason). Short signals arrive mirrored (prices negated, see _oriented)."""
    levels = signal["levels"]
    short = DIRECTIONS[signal["strategy"]] == "short"
    if short:
        if bars.open[i] <= levels["stop"]:
            return None, "opened at or above the stop"
        if bars.open[i] >= levels["target"]:
            return None, "opened at or below the target"
    if ENTRY_TYPES[signal["strategy"]] == "stop":
        if bars.high[i] < levels["entry"]:
            return None, "not_filled"
        fill = max(bars.open[i], levels["entry"])
    else:
        fill = bars.open[i]
    if fill <= levels["stop"]:
        return None, "opened below the stop"
    if short and fill > levels["entry"]:  # gapped through the sell stop
        atr_value = (signal.get("indicators") or {}).get("atr14")
        risk, reward = fill - levels["stop"], levels["target"] - fill
        if atr_value and risk > (signal.get("max_risk_atr") or DEFAULTS["max_risk_atr"]) * atr_value:
            return None, f"gap fill leaves a risk of {risk / atr_value:.2f} ATR"
        if reward < risk:
            return None, f"gap fill leaves {reward / risk:.2f}R of reward"
    return fill, None


def _mirror(bars):
    """The same bars with prices negated, so a short trade runs through the long execution rules."""
    cached = getattr(bars, "_mirrored", None)
    if cached is None or len(cached) != len(bars):
        cached = Bars(bars.ticker, bars.dates, [-x for x in bars.open], [-x for x in bars.low], [-x for x in bars.high],
                      [-x for x in bars.close], bars.volume)
        bars._mirrored = cached
    return cached


def _oriented(bars, signal):
    """(bars, signal, sign) in long form: short signals get mirrored bars and negated levels."""
    if DIRECTIONS[signal["strategy"]] == "long":
        return bars, signal, 1
    levels = {k: -signal["levels"][k] for k in ("entry", "stop", "target")}
    return _mirror(bars), {**signal, "levels": levels}, -1


PRICE_KEYS = ("fill", "exit", "last_close")


def _unmirror(result, sign):
    return result if sign == 1 else {k: (-v if k in PRICE_KEYS and v is not None else v) for k, v in result.items()}


def horizon_for(cfg, series, t):
    """Holding period of a signal on bar t, from the stock's beta at t (no later data)."""
    return holding_days(_beta_at(series, t), cfg)


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


def _walk_hold(bars, i, fill, stop, target, end, on):
    """_walk for a LONG trade with the momentum hold; on[j] is the trend at the close of bar j. A target, or the time exit
    at the close of bar end - 1, with the trend on (at the previous close for the target, at that close for the time exit)
    starts the hold; the stop does not apply while the trend was on at the previous close; in the hold the trade exits at
    the next open after the first close with the trend off (outcome momentum). Returns the _walk tuple plus whether the hold
    started; j is None while the trade is still open at the last bar."""
    low = high = fill
    hold = False
    for j in range(i, len(bars)):
        on_prev = on[j - 1]
        opening = fill if j == i else bars.open[j]
        if not hold and opening >= target:
            if not on_prev:
                return j, opening, "target", True, low, high, None, hold
            hold = True
        if not on_prev:
            if j > i and opening <= stop:
                return j, opening, "stop", True, low, high, None, hold
            if bars.low[j] <= stop:
                ambiguous = ("stop_and_target_same_bar" if not hold and bars.high[j] >= target
                             else "fill_and_stop_same_bar" if j == i and fill > bars.open[j] else None)
                return j, stop, "stop", False, low, high, ambiguous, hold
        low = min(low, bars.low[j])
        if not hold:
            if bars.high[j] >= target:
                if not on_prev:
                    return j, target, "target", False, low, high, None, hold
                hold = True
            elif j >= end - 1:
                if not on[j]:
                    return j, bars.close[j], "timeout", False, low, high, None, hold
                hold = True
        high = max(high, bars.high[j])
        if hold and not on[j] and j + 1 < len(bars):
            return j + 1, bars.open[j + 1], "momentum", False, min(low, bars.open[j + 1]), max(high, bars.open[j + 1]), None, hold
    return None, None, None, False, low, high, None, hold


def _hold_trend(bars, signal, hold_adx):
    return trend_up(bars, hold_adx) if hold_adx and DIRECTIONS[signal["strategy"]] == "long" else None


def simulate(bars, t, signal, horizon, hold_adx=0):
    """Outcome of a signal on bar t, using only bars after t. hold_adx > 0: momentum hold of LONG trades."""
    on = _hold_trend(bars, signal, hold_adx)
    bars, signal, sign = _oriented(bars, signal)
    return _unmirror(_simulate(bars, t, signal, horizon, on), sign)


def _simulate(bars, t, signal, horizon, on=None):
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
    if on is None:
        j, price, outcome, gap, low, high, ambiguous = _walk(bars, i, fill, stop, target, i + horizon)
        if j is None:
            j, price, outcome = i + horizon - 1, bars.close[i + horizon - 1], "timeout"
    else:
        j, price, outcome, gap, low, high, ambiguous, _ = _walk_hold(bars, i, fill, stop, target, i + horizon, on)
        if j is None:
            j, price, outcome = len(bars) - 1, bars.close[-1], "held_open"
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


def track(bars, t, signal, horizon, hold_adx=0):
    """Status today of a past signal on bar t < len(bars) - 1, with the same execution rules as simulate.

    not_triggered: the pullback buy stop was not reached on the next bar; skipped: opened below the stop;
    stopped / target: exit hit; time_exit: still open after `horizon` bars, closed at that bar's close;
    open: still running, valued at the last close. hold_adx > 0: momentum hold of LONG trades, whose end is a time_exit."""
    on = _hold_trend(bars, signal, hold_adx)
    bars, signal, sign = _oriented(bars, signal)
    return _unmirror(_track(bars, t, signal, horizon, on), sign)


def _track(bars, t, signal, horizon, on=None):
    levels = signal["levels"]
    i = t + 1
    fill, reason = _fill(bars, i, signal)
    if fill is None:
        return {"status": "not_triggered" if reason == "not_filled" else "skipped", "detail": reason}
    risk = fill - levels["stop"]
    base = {"fill_date": bars.dates[i].isoformat(), "fill": round(fill, 4)}
    held = False
    if on is None:
        end = min(i + horizon, len(bars))
        j, price, outcome, gap, _, _, ambiguous = _walk(bars, i, fill, levels["stop"], levels["target"], end)
        if j is None and i + horizon <= len(bars):
            j, price, outcome = i + horizon - 1, bars.close[i + horizon - 1], "time_exit"
    else:
        j, price, outcome, gap, _, _, ambiguous, held = _walk_hold(bars, i, fill, levels["stop"], levels["target"], i + horizon, on)
    if j is not None:
        out = {"status": {"stop": "stopped", "timeout": "time_exit", "momentum": "time_exit"}.get(outcome, outcome), **base, "gap": gap,
               "ambiguous": ambiguous, "exit_date": bars.dates[j].isoformat(), "exit": round(price, 4), "r": round((price - fill) / risk, 3),
               "bars_held": j - t}
        if outcome == "momentum":
            out["detail"] = "momentum hold ended: the ADX trend turned off"
        return out
    last = len(bars) - 1
    out = {"status": "open", **base, "last_close": round(bars.close[last], 4), "r_now": round((bars.close[last] - fill) / risk, 3),
           "bars_held": last - t, "bars_left": max(horizon - (last - t), 0) if on is not None else horizon - (last - t)}
    if held:
        out["detail"] = "momentum hold: past the target or time exit, open while the ADX trend holds"
    return out


def track_signals(bars, series, strategies, days, cfg, regime=None, sector_trend=None, phases=None):
    """Signals of the last `days` completed sessions before the latest, one active signal per ticker and strategy."""
    last = len(bars) - 1
    regime, sector_trend, phases = regime or {}, sector_trend or {}, phases or {}
    out = []
    for strategy in strategies:
        direction = DIRECTIONS[strategy]
        busy_until = -1
        for t in range(max(cfg["min_bars"] - 1, last - days), last):
            if t <= busy_until:
                continue
            signal = EVALUATORS[strategy](bars, series, t, cfg)
            if not signal["qualified"]:
                continue
            horizon = horizon_for(cfg, series, t)
            status = track(bars, t, signal, horizon, cfg["momentum_hold_adx"])
            label = regime.get(bars.dates[t], "unknown")
            trend = sector_trend.get(bars.dates[t])
            beta = _beta_at(series, t)
            out.append({"strategy": strategy, "direction": direction, "ticker": bars.ticker, "signal_date": signal["signal_date"],
                        "score": signal["score"], "regime": label, "sector_trend": trend,
                        "action": action_for(direction, label, trend, strategy, spread_context(phases.get(bars.dates[t])),
                                             bounce_long(phases.get(bars.dates[t])), after_overextended(phases.get(bars.dates[t]))),
                        "beta": None if beta is None else round(beta, 2), "holding_days": horizon, "atr14": signal["indicators"]["atr14"],
                        "signal_close": bars.close[t], **signal["levels"], **status})
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


def actions_table(strategy):
    return ACTIONS_BY_STRATEGY.get(strategy) or ACTIONS_BY_DIRECTION[DIRECTIONS[strategy]]


# ---------------------------------------------------------------- persistent tracking (Windmill data table)
#
# One row per signal, keyed by (strategy, ticker, signal_date, direction). The original fields are written once; the
# trade state (status, fill, exit, R, data status) changes only while the signal is pending or open. Once closed, a row
# cannot change or be deleted: a trigger in the database refuses it. Every run is idempotent: inserts skip existing
# keys, and the state of an active signal is recomputed from the full price history with the same execution rules.

DATA_TABLE = "main"
ACTIVE_STATUSES = ("pending", "open")
CLOSED_STATUSES = ("target", "stopped", "time_exit", "not_triggered", "skipped")
ORIGINAL_FIELDS = ("strategy", "ticker", "signal_date", "direction", "live", "score", "regime", "sector", "sector_trend", "action",
                   "entry", "stop", "target", "reward_risk", "atr14", "signal_close", "beta", "holding_days", "original")
STATE_FIELDS = ("status", "detail", "fill_date", "fill", "exit_date", "exit", "r", "r_now", "last_close", "bars_held", "bars_left",
                "gap", "ambiguous", "price_factor", "data_status", "data_note", "last_data_date", "stale_days")
COLUMN_TYPES = {"strategy": "text", "ticker": "text", "signal_date": "date", "direction": "text", "live": "boolean",
                "score": "double precision", "regime": "text", "sector": "text", "sector_trend": "text", "action": "text",
                "entry": "double precision", "stop": "double precision", "target": "double precision", "reward_risk": "double precision",
                "atr14": "double precision", "signal_close": "double precision", "beta": "double precision", "holding_days": "integer",
                "original": "jsonb", "status": "text", "detail": "text", "fill_date": "date", "fill": "double precision",
                "exit_date": "date", "exit": "double precision", "r": "double precision", "r_now": "double precision",
                "last_close": "double precision", "bars_held": "integer", "bars_left": "integer", "gap": "boolean", "ambiguous": "text",
                "price_factor": "double precision", "data_status": "text", "data_note": "text", "last_data_date": "date",
                "stale_days": "integer"}
_ACTIVE_SQL = ", ".join(f"'{x}'" for x in ACTIVE_STATUSES)
_CLOSED_SQL = ", ".join(f"'{x}'" for x in CLOSED_STATUSES)

SCHEMA_SQL = f"""
CREATE SCHEMA IF NOT EXISTS screener;
CREATE TABLE IF NOT EXISTS screener.signals (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    strategy text NOT NULL,
    ticker text NOT NULL,
    signal_date date NOT NULL,
    direction text NOT NULL CHECK (direction IN ('long', 'short')),
    live boolean NOT NULL,
    score double precision,
    regime text,
    sector text,
    sector_trend text,
    action text,
    entry double precision NOT NULL,
    stop double precision NOT NULL,
    target double precision NOT NULL,
    reward_risk double precision,
    atr14 double precision,
    signal_close double precision NOT NULL CHECK (signal_close > 0),
    beta double precision,
    holding_days integer NOT NULL CHECK (holding_days > 0),
    original jsonb NOT NULL DEFAULT '{{}}',
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL CHECK (status IN ({_ACTIVE_SQL}, {_CLOSED_SQL})),
    detail text,
    fill_date date,
    fill double precision,
    exit_date date,
    exit double precision,
    r double precision,
    r_now double precision,
    last_close double precision,
    bars_held integer,
    bars_left integer,
    gap boolean,
    ambiguous text,
    price_factor double precision NOT NULL DEFAULT 1,
    data_status text NOT NULL DEFAULT 'ok' CHECK (data_status IN ('ok', 'unavailable')),
    data_note text,
    last_data_date date,
    stale_days integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz,
    UNIQUE (strategy, ticker, signal_date, direction),
    CHECK ((closed_at IS NULL) = (status IN ({_ACTIVE_SQL})))
);
CREATE INDEX IF NOT EXISTS signals_active ON screener.signals (ticker) WHERE closed_at IS NULL;
CREATE TABLE IF NOT EXISTS screener.runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_at timestamptz NOT NULL DEFAULT now(),
    as_of date NOT NULL,
    regime text,
    universe integer,
    data_errors integer,
    inserted integer,
    updated integer,
    market jsonb,
    candidates jsonb
);
CREATE OR REPLACE FUNCTION screener.guard_signal() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'screener.signals is a history: it cannot be truncated';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'screener.signals is a history: signal % cannot be deleted', OLD.id;
    END IF;
    IF OLD.closed_at IS NOT NULL THEN
        RAISE EXCEPTION 'signal % is closed and cannot change', OLD.id;
    END IF;
    IF (NEW.id, NEW.first_seen_at, {", ".join("NEW." + f for f in ORIGINAL_FIELDS)})
       IS DISTINCT FROM (OLD.id, OLD.first_seen_at, {", ".join("OLD." + f for f in ORIGINAL_FIELDS)}) THEN
        RAISE EXCEPTION 'the original fields of signal % cannot change', OLD.id;
    END IF;
    RETURN NEW;
END
$$;
CREATE OR REPLACE TRIGGER signals_guard BEFORE UPDATE OR DELETE ON screener.signals
    FOR EACH ROW EXECUTE FUNCTION screener.guard_signal();
CREATE OR REPLACE TRIGGER signals_no_truncate BEFORE TRUNCATE ON screener.signals
    FOR EACH STATEMENT EXECUTE FUNCTION screener.guard_signal();
"""

# Active signals, plus the recent closed ones: they are known keys and their trades block overlapping new signals.
LOAD_SQL = SCHEMA_SQL + """SELECT * FROM screener.signals
WHERE closed_at IS NULL OR signal_date >= current_date - 120 OR exit_date >= current_date - 120
ORDER BY id;"""

_NEW_COLUMNS = ORIGINAL_FIELDS + STATE_FIELDS
_UPDATE_COLUMNS = ("id",) + STATE_FIELDS
WRITE_SQL = f"""
WITH new_rows AS (
    SELECT * FROM jsonb_to_recordset($1::jsonb) AS x({", ".join(f'"{c}" {COLUMN_TYPES[c]}' for c in _NEW_COLUMNS)})
), ins AS (
    INSERT INTO screener.signals ({", ".join(f'"{c}"' for c in _NEW_COLUMNS)}, closed_at)
    SELECT {", ".join(f'"{c}"' for c in _NEW_COLUMNS)}, CASE WHEN status IN ({_CLOSED_SQL}) THEN now() END FROM new_rows
    ON CONFLICT (strategy, ticker, signal_date, direction) DO NOTHING
    RETURNING id
), changes AS (
    SELECT * FROM jsonb_to_recordset($2::jsonb) AS x(id bigint, {", ".join(f'"{c}" {COLUMN_TYPES[c]}' for c in STATE_FIELDS)})
), upd AS (
    UPDATE screener.signals s SET {", ".join(f'"{c}" = c."{c}"' for c in STATE_FIELDS)}, updated_at = now(),
        closed_at = CASE WHEN c.status IN ({_CLOSED_SQL}) THEN now() END
    FROM changes c
    WHERE s.id = c.id AND s.closed_at IS NULL
    RETURNING s.id
), run AS (
    INSERT INTO screener.runs (as_of, regime, universe, data_errors, inserted, updated, market, candidates)
    SELECT $3::date, $4, $5, $6, (SELECT count(*) FROM ins), (SELECT count(*) FROM upd), $7::jsonb, $8::jsonb
    RETURNING id
)
SELECT (SELECT count(*) FROM ins) AS inserted, (SELECT count(*) FROM upd) AS updated, (SELECT id FROM run) AS run_id;
"""

STATS_SQL = """
SELECT strategy, live,
       count(*) AS signals,
       count(*) FILTER (WHERE closed_at IS NULL) AS active,
       count(*) FILTER (WHERE status IN ('not_triggered', 'skipped')) AS not_filled,
       count(r) FILTER (WHERE closed_at IS NOT NULL) AS closed_trades,
       count(*) FILTER (WHERE closed_at IS NOT NULL AND r > 0) AS winners,
       avg(r) FILTER (WHERE closed_at IS NOT NULL) AS average_r,
       sum(r) FILTER (WHERE closed_at IS NOT NULL) AS total_r,
       count(*) FILTER (WHERE data_status = 'unavailable' AND closed_at IS NULL) AS data_unavailable
FROM screener.signals
GROUP BY strategy, live
ORDER BY strategy, live DESC;
"""

# Your positions, written by the positions script. Kept identical to POSITIONS_SQL there (a test checks it): either script
# may create the table first.
POSITIONS_SQL = """
CREATE SCHEMA IF NOT EXISTS screener;
CREATE TABLE IF NOT EXISTS screener.positions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticker text NOT NULL,
    buy_date date NOT NULL,
    buy_price double precision NOT NULL CHECK (buy_price > 0),
    size double precision NOT NULL CHECK (size > 0 AND size <= 1),
    stop double precision CHECK (stop IS NULL OR (stop > 0 AND stop < buy_price)),
    target double precision CHECK (target IS NULL OR target > buy_price),
    note text,
    sell_date date,
    sell_price double precision CHECK (sell_price IS NULL OR sell_price > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz,
    CHECK ((sell_date IS NULL) = (closed_at IS NULL)),
    CHECK (sell_date IS NULL OR sell_date >= buy_date)
);
CREATE UNIQUE INDEX IF NOT EXISTS positions_one_open ON screener.positions (ticker) WHERE closed_at IS NULL;
"""
LOAD_POSITIONS_SQL = POSITIONS_SQL + """SELECT * FROM screener.positions WHERE closed_at IS NULL ORDER BY buy_date, id;"""


def datatable_query(name=DATA_TABLE):
    """A function running one SQL text with positional $n arguments on a Windmill data table; returns the rows of the
    last statement. Lists and dicts are sent as JSON."""
    def run(sql, *args):
        rows = wmill.datatable(name).query(sql, *args).fetch()
        return rows if isinstance(rows, list) else []
    return run


def _day(value):
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def sessions_after(calendar, start, end):
    """Number of sessions in the calendar after `start` up to and including `end`."""
    return max(0, bisect.bisect_right(calendar, end) - bisect.bisect_right(calendar, start))


def _json_safe(value):
    return json.loads(json.dumps(value, default=str))


def stored_update(row, bars, calendar, as_of, note=None, hold_adx=0):
    """New state of a stored active signal, recomputed from its ticker's bars. bars None (or a history without the
    signal date) keeps the last known trade state and marks the data unavailable, with the sessions it is behind."""
    signal_date = _day(row["signal_date"])
    last_known = _day(row["last_data_date"]) if row.get("last_data_date") else signal_date
    t = None
    if bars is not None:
        i = bisect.bisect_left(bars.dates, signal_date)
        t = i if i < len(bars) and bars.dates[i] == signal_date else None
        if t is None:
            note = f"signal date {signal_date} not in the price history"
    if t is None or row["strategy"] not in DIRECTIONS:
        if t is not None:
            note = f"unknown strategy {row['strategy']}"
        keep = {k: row.get(k) for k in STATE_FIELDS}
        return {"id": row["id"], **keep, "data_status": "unavailable", "data_note": note or "no price data",
                "last_data_date": last_known.isoformat(), "stale_days": sessions_after(calendar, last_known, as_of)}
    factor = bars.close[t] / row["signal_close"]
    if abs(factor - 1) < 0.005:  # rounding; a real change means a split or another adjustment of the history
        factor = 1.0
    levels = {k: row[k] * factor for k in ("entry", "stop", "target")}
    signal = {"strategy": row["strategy"], "levels": levels, "indicators": {"atr14": (row.get("atr14") or 0) * factor or None}}
    state = {"status": "pending"} if t == len(bars) - 1 else track(bars, t, signal, int(row["holding_days"]), hold_adx)
    out = {"id": row["id"], **{k: None for k in STATE_FIELDS}, **{k: v for k, v in state.items() if k in STATE_FIELDS}}
    for k in ("fill", "exit", "last_close"):  # back to the prices of the signal day, like entry, stop and target
        if out[k] is not None:
            out[k] = round(out[k] / factor, 4)
    out.update({"price_factor": round(factor, 6), "data_status": "ok", "last_data_date": bars.dates[-1].isoformat(),
                "stale_days": sessions_after(calendar, bars.dates[-1], as_of),
                "data_note": None if factor == 1.0 else f"prices adjusted by {factor:.4f} since the signal (split?)"})
    return out


def new_row(item, live, last_data_date):
    """A row for screener.signals from a qualified signal of today (live) or a recomputed past signal (not live)."""
    levels = item["levels"] if "levels" in item else item
    original = {"warnings": item.get("warnings", []), "sector_etf": item.get("sector_etf"),
                "scoring": [{"name": x["name"], "earned": x["earned"]} for x in item.get("scoring", [])],
                "levels": {k: v for k, v in levels.items() if k not in ("entry", "stop", "target", "reward_risk")} if "levels" in item else {}}
    if not live:
        original["note"] = "recomputed later from the price history; live-only checks (earnings, short interest) not applied"
    row = {"strategy": item["strategy"], "ticker": item["ticker"], "signal_date": item["signal_date"], "direction": item["direction"],
           "live": live, "score": item.get("score"), "regime": item.get("regime"), "sector": item.get("sector"),
           "sector_trend": item.get("sector_trend"), "action": item.get("action"), "entry": levels["entry"], "stop": levels["stop"],
           "target": levels["target"], "reward_risk": levels.get("reward_risk"),
           "atr14": item["indicators"]["atr14"] if "indicators" in item else item.get("atr14"),
           "signal_close": item["indicators"]["close"] if "indicators" in item else item["signal_close"],
           "beta": item.get("beta"), "holding_days": item["holding_days"], "original": _json_safe(original),
           **{k: None for k in STATE_FIELDS}, "price_factor": 1.0, "data_status": "ok", "last_data_date": last_data_date, "stale_days": 0}
    row.update({k: item[k] for k in STATE_FIELDS if k in item and item[k] is not None})
    row["status"] = item.get("status", "pending")
    return row


def _busy_until(state):
    if state["status"] in ACTIVE_STATUSES:
        return date.max
    return _day(state["exit_date"]) if state.get("exit_date") else None


def select_new(stored, updates, candidates):
    """Candidates that may be inserted: one trade per strategy, ticker and direction at a time, like the validation.
    A signal is skipped if it comes after a stored one of the same strategy, ticker and direction that is still pending
    or open, or not later than that one's exit date. Keys already stored are left out silently."""
    by_id = {u["id"]: u for u in updates}
    spans = {}
    for row in stored:
        until = _busy_until({**row, **by_id.get(row["id"], {})})
        if until is not None:
            spans.setdefault((row["strategy"], row["ticker"], row["direction"]), []).append((_day(row["signal_date"]), until))
    known = {(r["strategy"], r["ticker"], str(r["signal_date"])[:10], r["direction"]) for r in stored}
    keep, skipped = [], []
    for row in sorted(candidates, key=lambda r: (r["signal_date"], r["strategy"], r["ticker"])):
        key = (row["strategy"], row["ticker"], row["direction"])
        if (*key[:2], row["signal_date"], key[2]) in known:
            continue
        day = _day(row["signal_date"])
        if any(start < day <= until for start, until in spans.get(key, [])):
            skipped.append({k: row[k] for k in ("strategy", "ticker", "signal_date", "direction")})
            continue
        keep.append(row)
        known.add((*key[:2], row["signal_date"], key[2]))
        until = _busy_until(row)
        if until is not None:
            spans.setdefault(key, []).append((day, until))
    return keep, skipped


def stored_view(row):
    keys = ("strategy", "ticker", "direction", "signal_date", "live", "status", "entry", "stop", "target", "fill", "exit",
            "exit_date", "r", "r_now", "holding_days", "bars_left", "data_status", "data_note", "stale_days")
    return {k: (str(row[k])[:10] if k in ("signal_date", "exit_date") and row.get(k) else row.get(k)) for k in keys}


def position_signal(stored, ticker, buy_day):
    """The latest live LONG screener signal on the ticker in the 14 days up to the buy, or None."""
    rows = [r for r in stored if r["ticker"] == ticker and r["direction"] == "long" and r.get("live")
            and buy_day - timedelta(days=14) <= _day(r["signal_date"]) <= buy_day]
    return max(rows, key=lambda r: (_day(r["signal_date"]), r.get("id") or 0)) if rows else None


def position_levels(pos, series, i, cfg, signal=None):
    """Stop, target, holding period and where they come from: the position, else the screener signal, else the ATR rule."""
    buy = float(pos["buy_price"])
    hold = int(signal["holding_days"]) if signal else horizon_for(cfg, series, i)
    stop, source = pos.get("stop"), "yours"
    if not stop and signal and 0 < signal["stop"] < buy:
        stop, source = float(signal["stop"]), f"the {signal['strategy']} signal of {str(signal['signal_date'])[:10]}"
    if not stop:
        atr = next((a for a in (series["atr14"][i], series["atr14"][-1]) if a), buy * 0.02)
        stop, source = buy - cfg["position_stop_atr"] * atr, f"{cfg['position_stop_atr']:g} ATR below the buy"
    target = pos.get("target")
    if not target:
        target = float(signal["target"]) if signal and signal["target"] > buy and source != "yours" else None
        target = target or buy + cfg["position_target_r"] * (buy - stop)
    return {"stop": round(float(stop), 4), "target": round(float(target), 4), "holding_days": hold, "levels_from": source}


def position_view(pos):
    return {"id": pos.get("id"), "ticker": pos["ticker"], "buy_date": str(pos["buy_date"])[:10], "buy_price": pos["buy_price"],
            "size": pos["size"], "note": pos.get("note")}


# Fields of a position status that need prices; None when there are none yet (unavailable data, bought today).
POSITION_PRICE_FIELDS = ("close", "return", "r_now", "stop", "target", "holding_days", "levels_from", "sessions_held",
                         "exit_date", "exit_price")


def _no_prices(pos, **known):
    return {**{k: None for k in POSITION_PRICE_FIELDS}, "stop": pos.get("stop") or None, "target": pos.get("target") or None,
            "levels_from": "yours" if pos.get("stop") or pos.get("target") else None, "momentum_hold": False, **known}


def position_unknown(pos, note):
    return {**position_view(pos), **_no_prices(pos), "advice": "check", "reason": f"no price data today ({note})", "flags": [],
            "stalled": False, "data_status": "unavailable"}


def position_status(pos, bars, series, cfg, signal=None, short_today=(), earnings_days=None):
    """Hold or sell for one of your positions, by the backtested exit rules from the buy day on: stop, target, time exit
    after holding_days, and with momentum_hold_adx the ADX momentum hold (the stop does not apply while the trend is up;
    past the target or the time exit it holds until the first close with the trend off). Adds the flags of the day."""
    buy, buy_day = float(pos["buy_price"]), _day(pos["buy_date"])
    last = len(bars) - 1
    i = bisect.bisect_left(bars.dates, buy_day)
    view = position_view(pos)
    if i > last:  # e.g. recorded today and the screen ran before today's session closed
        return {**view, **_no_prices(pos), "advice": "hold",
                "reason": f"bought after the last completed session ({bars.dates[last].isoformat()}): first check after it closes",
                "flags": [], "stalled": False, "data_status": "pending"}
    i = max(i, 1)
    levels = position_levels(pos, series, i, cfg, signal)
    stop, target, hold = levels["stop"], levels["target"], levels["holding_days"]
    on = trend_up(bars, cfg["momentum_hold_adx"]) if cfg["momentum_hold_adx"] else None
    end = i + hold
    if on is None:
        j, price, outcome, *_ = _walk(bars, i, buy, stop, target, min(end, len(bars)))
        held = False
        if j is None and end - 1 <= last:
            j, price, outcome = end - 1, bars.close[end - 1], "timeout"
    else:
        j, price, outcome, _, _, _, _, held = _walk_hold(bars, i, buy, stop, target, end, on)
    close, atr = bars.close[last], series["atr14"][last]
    risk = buy - stop
    r_now = (close - buy) / risk
    sessions = last - i
    trend = "" if on is None else " with the ADX trend off"
    if j is not None:
        when = bars.dates[j].isoformat()
        advice, reason = "sell", {
            "stop": f"the stop {stop:g} was hit on {when}{trend}",
            "target": f"the target {target:g} was reached on {when}" + ("" if on is None else " without an ADX uptrend"),
            "timeout": f"time exit: the {hold} sessions since the buy ended on {when}{trend}",
            "momentum": f"the momentum hold ended: the ADX trend turned off at the close of {bars.dates[j - 1].isoformat()}",
        }[outcome]
        reason += "; sell at the next open if you still hold it"
    elif held and not on[last]:
        advice, reason = "sell", "the momentum hold ended today (the ADX trend turned off): sell at the next open"
    elif held:
        advice, reason = "hold", ("momentum hold: past the target or the time exit while the ADX trend is up; sell at the next "
                                  "open after the first close with the trend off")
    else:
        advice = "hold"
        reason = (f"{max(end - 1 - last, 0)} of {hold} sessions left; sell at the stop {stop:g} or the target {target:g}"
                  + ("" if on is None else "; while the ADX trend is up the stop is paused and the target or the time exit "
                                             "starts a momentum hold instead"))
    flags = []
    e50 = series["ema50"][last]
    if e50 and close < e50:
        flags.append(f"closed below its EMA50 ({e50:.2f})")
    if advice == "hold" and on is not None and not on[last] and atr and close - stop < 0.5 * atr:
        flags.append("ADX trend off and the close within 0.5 ATR of the stop")
    if earnings_days is not None and 0 <= earnings_days <= cfg["earnings_block_days"]:
        flags.append(f"earnings in {earnings_days} days")
    if short_today:
        flags.append(f"SHORT setup today ({', '.join(short_today)}): the price action turned weak")
    stalled = (advice == "hold" and not held and cfg["swap_stalled_days"] > 0 and sessions >= cfg["swap_stalled_days"]
               and abs(r_now) <= cfg["swap_band_r"])
    if stalled:
        flags.append(f"stalled: {sessions} sessions, {r_now:+.2f}R")
    return {**view, "advice": advice, "reason": reason, "flags": flags, "close": round(close, 4),
            "return": round(close / buy - 1, 4), "r_now": round(r_now, 3), **levels, "sessions_held": sessions,
            "momentum_hold": bool(held), "stalled": stalled, "exit_date": bars.dates[j].isoformat() if j is not None else None,
            "exit_price": round(price, 4) if j is not None else None, "data_status": "ok"}


def plan_swaps(candidates, statuses, cfg):
    """With every slot taken: pair the best LONG setups left without a slot (score >= swap_min_score) with your stalled
    positions, the weakest first. Marks both and returns the pairs."""
    if not cfg["swap_stalled_days"]:
        return []
    seen, waiting = set(), []
    for c in sorted(candidates, key=lambda x: -(x.get("score") or 0)):
        if ((c.get("position") or {}).get("reason") == "no free slot" and (c.get("score") or 0) >= cfg["swap_min_score"]
                and c["ticker"] not in seen):
            seen.add(c["ticker"])
            waiting.append(c)
    stalled = sorted((p for p in statuses if p.get("stalled")), key=lambda p: p["r_now"])
    pairs = []
    for c, p in zip(waiting, stalled):
        size = cfg["position_size"]
        c["position"] = {"size": size, "slot": None, "reason": "swap", "replaces": p["ticker"]}
        c["recommendation"] = (c["recommendation"].rsplit(" Size: ", 1)[0]
                               + f" Swap: sell {p['ticker']} (stalled: {p['sessions_held']} sessions, {p['r_now']:+.2f}R) and buy"
                               f" this at {size:.0%} of the budget.")
        p["swap_for"] = c["ticker"]
        p["flags"].append(f"swap: sell it to buy {c['ticker']} (score {c['score']:g})")
        pairs.append({"sell": p["ticker"], "buy": c["ticker"], "score": c["score"], "r_now": p["r_now"],
                      "sessions_held": p["sessions_held"]})
    return pairs


def spread_context(phase):
    """Where SHORT setups become bear put debit spreads: the decline (bear, correction) or "overextended", else None."""
    if not phase:
        return None
    return phase.get("decline") or ("overextended" if phase.get("overextended") else None)


def bounce_long(phase):
    """A bounce inside a decline: pullback setups become tight longs (bounce_long_days)."""
    return bool(phase and phase.get("decline") and phase.get("phase") == "bounce")


def index_long_day(phase):
    """The first day of a bounce inside a decline: the tight long on the benchmark."""
    return bounce_long(phase) and bool(phase.get("switched"))


def after_overextended(phase):
    """A pullback right after an overextended bull market: LONG setups are watch only."""
    return bool(phase and phase.get("after_overextended"))


def action_for(direction, regime_label, sector_trend=None, strategy=None, decline=None, bounce=False, cooling=False):
    """What to do with a qualified setup. The market regime decides; a sector ETF trending against the trade turns
    long/short into watch. A SHORT setup in a decline (bear regime or correction) or an overextended bull market
    (`decline` from spread_context) becomes a bear put debit spread whatever the sector does. A pullback in a bounce
    inside a decline (`bounce`, see bounce_long) is a tight long whatever the regime and sector. A LONG setup in a
    pullback right after an overextended market (`cooling`, see after_overextended) is watch only. Nothing here changes
    qualification."""
    if direction == "short" and (decline or regime_label == "bearish"):
        return "bear_put_spread"
    if bounce and direction == "long" and strategy == "pullback":
        return "long"
    if cooling and direction == "long":
        return "watch"
    table = actions_table(strategy) if strategy else ACTIONS_BY_DIRECTION[direction]
    action = table.get(regime_label, "watch")
    if (action == "long" and sector_trend == "down") or (action == "short" and sector_trend == "up"):
        return "watch"
    return action


def trend_labels(dates, closes, slope):
    """Per date: up (above a rising SMA200), down (below a falling SMA200), mixed, or None without enough history."""
    m200, out = sma(closes, 200), {}
    for i, d in enumerate(dates):
        if m200[i] is None or i < slope or m200[i - slope] is None:
            out[d] = None
            continue
        above, rising = closes[i] > m200[i], m200[i] > m200[i - slope]
        out[d] = "up" if above and rising else "down" if not above and not rising else "mixed"
    return out


PHASE_ADVICE = {
    "uptrend": "Uptrend: long setups as ranked (breakout, then pullback, then squeeze). No shorts and no put spreads: "
               "outside a decline SHORT setups lost in the backtest (debit spreads about -0.1 to -0.2 of the debit).",
    "overextended": "Overextended bull market (benchmark {ext:+.0%} vs its SMA200, minimum {overextended:.0%}): long setups "
                    "as ranked, and SHORT setups (rally rejection, run break, breakdown, dead cat) as small bear put debit "
                    "spreads, never a short. In the backtest the short-dated spreads on SHORT setups lost about 0.11-0.18 of the "
                    "debit per trade here (43% won), while long setups did well (+0.2R per trade): longs stay the main "
                    "trade, the spreads are small hedges.",
    "pullback": "Pullback in a rising market (benchmark below its EMA20, no correction): the weakest stretch for the "
                "benchmark in the backtest (QQQ +1.3% over the next 30 sessions on average, +1.8% in an uptrend). Long again "
                "at support, but small (long setups +0.17R per trade before 2022-07, -0.07R after). No shorts and no put "
                "spreads (debit spreads about -0.1 to -0.3 of the debit here).",
    "pullback_after_overextended": "Pullback right after an overextended bull market (an overextended day in the last "
                "{after_overextended_bars} sessions): LONG setups are watch only. In the backtest long setups here lost "
                "-0.21 / -0.12R per trade before / after 2022-07 (about 1 in 3 won). Long again at support once this "
                "window has passed; no shorts and no put spreads.",
    "falling": "Decline, still falling: SHORT setups (rally rejection, run break, breakdown) as bear put debit spreads only, "
               "never a short. In the backtest the short-dated spreads lost about 0.06-0.13 of the debit per trade here (48% "
               "won), more with the benchmark {deep:.0%} or more below its high: keep each spread small.",
    "bounce": "Bounce inside a decline (benchmark {bounce_atr:g} ATR above its {bounce_low_bars}-day low or above its EMA20, "
              "still below its EMA50). In 2016-2026 most such bounces led on to a recovery rather than failing as a dead cat "
              "(QQQ +3.8% over the next 30 sessions, up 62% of the time). Tight long the bounce: pullback setups become long "
              "for {bounce_long_days} sessions (+0.09 / +0.55R per trade before / after 2022-07, 60% won), and on the day "
              "the bounce starts a long on the benchmark up to its 200-day line (see index_long). Do not short the bounce; "
              "back to a put debit spread at the rejection (a rally rejection or a dead cat rejection at the 200-day line).",
    "above_ema50": "Decline with the benchmark above its EMA50 (early in a correction, or recovering): shorts lost the most "
                   "here (-0.4 to -0.5R) and short-dated debit spreads about 0.13-0.2 of the debit; breakouts did well on few trades. "
                   "Favour longs; debit spreads only small.",
}


def market_phase(bars, regime, cfg=None):
    """Phase of the benchmark per date, from data up to that date (None without enough history).

    decline: "bear" in a bearish regime; "correction" when the close is correction_drawdown or more below the highest close
    of the last phase_high_bars, after a correction_run rise in the 126 sessions before that high or with that high at
    least correction_extension above the SMA200 (a big extension); otherwise None.
    phase in a decline: above_ema50 (close at or above EMA50: early in a correction or recovering), bounce (close
    bounce_atr ATR above the low of the last bounce_low_bars or above EMA20), else falling. Without a decline: pullback
    (close below EMA20), else uptrend; `overextended` when the close is overextended_bull or more above the SMA200.
    `switched` marks the first day of a phase, `since` its first day and `previous` the phase before it.
    `after_overextended` marks a pullback within after_overextended_bars sessions of an overextended day.
    `dead_cat` names the 200-day line (EMA200 or SMA200) on the day the benchmark's bounce is rejected there: the first
    rejection only, none in the previous dead_cat_alert_gap sessions."""
    cfg = cfg or DEFAULTS
    c, h, l = bars.close, bars.high, bars.low
    e20, e50, m200, a = ema(c, 20), ema(c, 50), sma(c, 200), atr(h, l, c, 14)
    lines = {"EMA200": ema(c, 200), "SMA200": m200}
    n_hi, n_lo = cfg["phase_high_bars"], cfg["bounce_low_bars"]
    out, last, last_alert, last_ext = {}, None, None, None
    for i, d in enumerate(bars.dates):
        if i < 126 + n_hi or None in (e20[i], e50[i], m200[i], a[i]) or not a[i]:
            out[d] = None
            continue
        k = max(range(i - n_hi + 1, i + 1), key=lambda j: c[j])
        drawdown = c[i] / c[k] - 1
        run = c[k] / c[k - 126] - 1
        extension = c[k] / m200[k] - 1 if m200[k] else None
        bounce = (c[i] - min(l[i - n_lo + 1:i + 1])) / a[i]
        if regime.get(d) == "bearish":
            decline = "bear"
        elif drawdown <= -cfg["correction_drawdown"] and (run >= cfg["correction_run"] or
                                                         (extension is not None and extension >= cfg["correction_extension"])):
            decline = "correction"
        else:
            decline = None
        if decline:
            phase = "above_ema50" if c[i] >= e50[i] else "bounce" if bounce >= cfg["bounce_atr"] or c[i] > e20[i] else "falling"
        else:
            phase = "pullback" if c[i] < e20[i] else "uptrend"
        switched = last is not None and last["phase"] != phase
        vs_sma200 = c[i] / m200[i] - 1
        if decline is None and vs_sma200 >= cfg["overextended_bull"]:
            last_ext = i
        hit = dead_cat_level(bars, lines, a, i, cfg)
        if hit:
            hit, last_alert = (hit if last_alert is None or i - last_alert > cfg["dead_cat_alert_gap"] else None), i
        out[d] = {"phase": phase, "decline": decline, "vs_sma200": round(vs_sma200, 4),
                  "overextended": decline is None and vs_sma200 >= cfg["overextended_bull"],
                  "after_overextended": phase == "pullback" and last_ext is not None and i - last_ext <= cfg["after_overextended_bars"],
                  "dead_cat": hit[0] if hit else None, "drawdown": round(drawdown, 4), "high": round(c[k], 2), "high_date": bars.dates[k].isoformat(),
                  "run_before_high": round(run, 4), "extension_at_high": None if extension is None else round(extension, 4),
                  "bounce_atr": round(bounce, 2), "deep": drawdown <= -cfg["deep_drawdown"], "switched": switched,
                  "previous": (last["phase"] if switched else last["previous"]) if last else None,
                  "since": d.isoformat() if last is None or switched else last["since"]}
        last = out[d]
    return out


def phase_advice(info, cfg=None):
    """The recommendation for a market_phase entry, with the switch first when the phase started today."""
    if not info:
        return "Market phase unknown: not enough benchmark history."
    cfg = cfg or DEFAULTS
    key = ("overextended" if info["phase"] == "uptrend" and info.get("overextended")
           else "pullback_after_overextended" if info.get("after_overextended") else info["phase"])
    text = PHASE_ADVICE[key].format(deep=cfg["deep_drawdown"], bounce_atr=cfg["bounce_atr"], bounce_low_bars=cfg["bounce_low_bars"],
                                    ext=info.get("vs_sma200") or 0, overextended=cfg["overextended_bull"],
                                    bounce_long_days=cfg["bounce_long_days"],
                                    after_overextended_bars=cfg["after_overextended_bars"])
    where = f"{info['drawdown']:+.1%} from the {info['high_date']} high" + (f", {info['decline']}" if info["decline"] else "")
    head = (f"SWITCH today from {info['previous']} to {info['phase']} ({where}). " if info["switched"]
            else f"Phase {info['phase']} since {info['since']}" + (f" (after {info['previous']})" if info["previous"] else "") + f", {where}. ")
    alert = ""
    if info.get("dead_cat"):
        alert = (f"ALERT: dead cat bounce rejected at the benchmark's {info['dead_cat']} today. Proposal: a bear put debit spread "
                 "on the benchmark (see index_put_spread), small. Backtest 2016-2026, few cases: 11 such alerts on QQQ, the "
                 "10-session spread lost 0.15 of the debit before 2022-07 and was about flat after (+0.03); on SPY it lost "
                 "(-0.07 / -0.14). ")
    elif index_long_day(info):
        alert = ("ALERT: bounce inside a decline started today. Proposal: a tight long on the benchmark (see index_long) up "
                 "to its 200-day line, where the next put debit spread comes on a rejection. Backtest 2016-2026, 10 sessions at most: "
                 "on QQQ +0.8% / +1.6% per trade before / after 2022-07, 64% won (50 cases); on SPY +0.4% / +1.5%, 61% won; "
                 "worst trade about -11%. ")
    return alert + head + text


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


def trend_snapshot(bars, cfg):
    """Where an index or ETF stands on its last bar: trend label, distance from its averages, recent changes, RSI14."""
    i, c, slope = len(bars) - 1, bars.close, cfg["regime_slope_bars"]
    m200, e20, e50, r = sma(c, 200), ema(c, 20), ema(c, 50), rsi(c, 14)

    def change(k):
        return round(c[i] / c[i - k] - 1, 4) if i >= k and c[i - k] else None

    return {"close": round(c[i], 2), "trend": trend_labels(bars.dates, c, slope)[bars.dates[i]],
            "sma200": None if m200[i] is None else round(m200[i], 2),
            "vs_sma200": None if m200[i] is None else round(c[i] / m200[i] - 1, 4),
            "sma200_rising": None if m200[i] is None or i < slope or m200[i - slope] is None else m200[i] > m200[i - slope],
            "ema20": None if e20[i] is None else round(e20[i], 2), "ema50": None if e50[i] is None else round(e50[i], 2),
            "change_20d": change(20), "change_63d": change(63), "rsi14": None if r[i] is None else round(r[i], 1)}


def market_summary(name, bars, breadth_by_date, regime, sector_bars, cfg, context_bars=None, phases=None):
    """The latest regime and market phase, the inputs behind them, and what they mean for each strategy."""
    d = bars.dates[-1]
    count = breadth_by_date.get(d)
    etfs = {}
    for etf, b in sorted(sector_bars.items()):
        if b.dates[-1] != d or len(b) < 200:
            continue
        snap = trend_snapshot(b, cfg)
        etfs[etf] = {"close": snap["close"], "sma200": snap["sma200"], "above": snap["vs_sma200"] is not None and snap["vs_sma200"] > 0,
                     "trend": snap["trend"], "change_63d": snap["change_63d"]}
    ranked = sorted((x for x in etfs.items() if x[1]["change_63d"] is not None), key=lambda x: -x[1]["change_63d"])
    label = regime.get(d, "unknown")
    phase = (phases or {}).get(d)
    decline = spread_context(phase)
    actions = {s: action_for(DIRECTIONS[s], label, None, s, decline, bounce_long(phase), after_overextended(phase))
               for s in STRATEGIES}
    index_spread = index_long = None
    if phase and phase.get("dead_cat"):
        index_spread = {"ticker": name, **put_spread_plan(bars.close[-1], d, decline or "rejected at the 200-day line",
                                                          phase["deep"], cfg, cfg["index_put_spread_days"])}
    elif index_long_day(phase):
        index_long = index_long_plan(bars, d, cfg)
    return {"as_of": d.isoformat(), "regime": label, "long_setups": ACTIONS[label],
            "short_setups": action_for("short", label, None, None, decline),
            "phase": None if phase is None else {**phase, "advice": phase_advice(phase, cfg)},
            "index_put_spread": index_spread, "index_long": index_long,
            "actions": actions,
            "favoured": [s for s in STRATEGY_GROUPS["all"] if actions[s] in TRADE_ACTIONS],
            "benchmark": {"ticker": name, **trend_snapshot(bars, cfg)},
            "context": {k: trend_snapshot(b, cfg) for k, b in sorted((context_bars or {}).items())},
            "breadth": {"above_sma200": count[0] if count else None, "sector_etfs": count[1] if count else 0, "etfs": etfs},
            "sector_leaders": [k for k, _ in ranked[:3]], "sector_laggards": [k for k, _ in ranked[::-1][:3]],
            "rules": "bullish: benchmark above a rising SMA200 and breadth >= {:g}; bearish: below the SMA200 and the SMA200 falling or "
                     "breadth <= {:g}; otherwise neutral. LONG setups: bullish -> long, neutral -> watch, bearish -> no_trade. "
                     "SHORT setups: in a decline (bearish regime or correction) or an overextended bull market (see phase) -> bear_put_spread, otherwise neutral -> watch, "
                     "bullish -> countertrend_watch. Pullback in a bounce inside a decline -> long for bounce_long_days. LONG setups in a "
                     "pullback right after an overextended market -> watch. Squeeze: bullish -> long, neutral -> watch, "
                     "bearish -> countertrend_watch. Bonds (TLT, HYG) are context only, not part of the rules. A sector ETF trending against "
                     "the trade turns long/short into watch; it does not change bear_put_spread.".format(cfg["breadth_bullish"], cfg["breadth_bearish"])}


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
    last = len(bars) - 1 - max(cfg["swing_max_days"], max(FORWARD_DAYS))
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
            signals.append({"strategy": strategy, "direction": DIRECTIONS[strategy], "ticker": bars.ticker, "signal_date": signal["signal_date"],
                            "overlaps": previous is not None and t - previous < max(FORWARD_DAYS), **forward_returns(bars, t, market)})
            previous = t
            if t <= busy_until:
                continue
            horizon = horizon_for(cfg, series, t)
            outcome = simulate(bars, t, signal, horizon, cfg["momentum_hold_adx"])
            beta = _beta_at(series, t)
            trade = {"ticker": bars.ticker, "strategy": strategy, "direction": DIRECTIONS[strategy], "signal_date": signal["signal_date"], "score": signal["score"],
                     "regime": regime.get(bars.dates[t], "unknown"), "beta": None if beta is None else round(beta, 2), "holding_days": horizon,
                     **signal["levels"], **outcome}
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
        "exits": {k: sum(x["outcome"] == k for x in closed) for k in ("stop", "target", "timeout")}
                 | {k: n for k in ("momentum", "held_open") if (n := sum(x["outcome"] == k for x in closed))},
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


# ---------------------------------------------------------------- market backdrop

# Information only: none of it changes a score, a filter or a proposal. All from Finviz (futures chart, economic calendar).
CALENDAR_URL = "https://finviz.com/api/calendar/economic"
FED_DECISION = "Fed Interest Rate Decision"
# Futures: front month, rolled by Finviz. VX stands in for the VIX index, the 20-day volatility of NQ for the Nasdaq VIX.
BACKDROP_ASSETS = {"CL": "WTI crude", "QA": "Brent crude", "GC": "Gold", "SI": "Silver", "PL": "Platinum", "HG": "Copper",
                   "GDX": "Gold miners", "SIL": "Silver miners"}
BACKDROP_FUTURES = ("VX", "NQ", "ZQ", "DX", "ZN", "CL", "QA", "GC", "SI", "PL", "HG")
DRIVERS = {"NQ": "Nasdaq", "CL": "oil", "GC": "gold", "DX": "dollar", "ZN": "10y note", "VX": "VIX"}
# Theme rotation: each theme is an equal-weight basket of these ETFs or stocks (Optical has no ETF).
ROTATION_THEMES = {
    "Semiconductors": ("SMH",), "Memory": ("DRAM",), "Optical": ("COHR", "LITE", "CIEN", "FN", "AAOI", "CRDO", "ANET"),
    "Software": ("IGV",), "Cybersecurity": ("CIBR",), "Robotics & AI": ("BOTZ",), "Quantum": ("QTUM",), "Space": ("UFO", "ARKX"),
    "Uranium & nuclear": ("URA", "NLR"), "Solar": ("TAN",), "Lithium & batteries": ("LIT",), "Copper miners": ("COPX",),
    "Gold miners": ("GDX",), "Silver miners": ("SIL",), "Metals & mining": ("XME",), "Commodities": ("DBC",), "Energy": ("XLE",),
}
ROTATION_LOOKBACKS = (21, 63)  # 1 and 3 months
# ETFs screened for breakouts when their rotation against SPY accelerates (the 47 of the backtest).
ETF_SETUPS = {
    "XLK": "Technology", "XLF": "Financials", "XLV": "Health care", "XLI": "Industrials", "XLY": "Consumer cyclical",
    "XLP": "Consumer staples", "XLE": "Energy", "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real estate",
    "XLC": "Communication", "SMH": "Semiconductors", "SOXX": "Semiconductors", "IGV": "Software", "SKYY": "Cloud",
    "CIBR": "Cybersecurity", "XBI": "Biotech", "IBB": "Biotech", "KRE": "Regional banks", "KBE": "Banks",
    "ITB": "Homebuilders", "XHB": "Homebuilders", "XRT": "Retail", "XOP": "Oil & gas producers", "OIH": "Oil services",
    "XME": "Metals & mining", "GDX": "Gold miners", "GDXJ": "Junior gold miners", "SIL": "Silver miners",
    "URA": "Uranium", "TAN": "Solar", "ICLN": "Clean energy", "LIT": "Lithium & batteries", "COPX": "Copper miners",
    "ARKK": "Innovation", "KWEB": "China internet", "IYT": "Transports", "JETS": "Airlines", "PAVE": "Infrastructure",
    "ITA": "Aerospace & defense", "XAR": "Aerospace & defense", "IHI": "Medical devices", "BOTZ": "Robotics & AI",
    "FDN": "Internet", "GLD": "Gold", "SLV": "Silver", "USO": "Oil",
}
ACCELERATION_WINDOWS = (5, 10, 20, 63)
ROTATION_BACK = 20  # rank 4 weeks earlier
CORRELATION_DAYS = 63
VIX_SPIKE = 0.25  # VX up 25% or more in 5 sessions
VIX_NOTES = {  # forward Nasdaq (NQ) returns 2020-2026, one count per episode; any day: 63 sessions +3.8%, up 70%
    "spike": "in 2020-2026 a 25% VIX jump in 5 sessions was followed by a Nasdaq rise over the next 63 sessions 76% of the time "
             "(+7.4% on average, 19 cases) against 70% (+3.8%) on any day; the first week often fell further",
    "high": "a VIX of 25 or more without a jump was no rally signal in 2020-2026 (median Nasdaq return over the next 63 sessions "
            "-2.6%, 25 cases)",
    "low": "VIX in the lowest tenth of its year: complacent, but the Nasdaq still rose over the next 63 sessions 76% of the time",
}


def fetch_futures(ticker, count):
    response = _get(CHART_URL, {"instrument": "futures", "ticker": ticker, "timeframe": "d", "barsCount": count})
    return parse_chart(response.json(), ticker)


def fetch_calendar(start, end):
    """Economic calendar events from start to end (dates). Finviz cuts a response at about 90 events, so ask a week at once."""
    payload = _get(CALENDAR_URL, {"dateFrom": start.isoformat(), "dateTo": end.isoformat()}).json()
    if not isinstance(payload, list):
        raise ValueError("the economic calendar is not a list")
    return payload


def _percent(text):
    match = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*%?\s*$", str(text or ""))
    return float(match.group(1)) if match else None


def rate_decisions(events):
    out = []
    for e in events:
        if isinstance(e, dict) and e.get("event") == FED_DECISION and isinstance(e.get("date"), str):
            out.append({"date": date.fromisoformat(e["date"][:10]), "actual": _percent(e.get("actual")),
                        "previous": _percent(e.get("previous")),
                        "forecast": _percent(e.get("forecast")) or _percent(e.get("teforecast"))})
    return sorted(out, key=lambda x: x["date"])


def _days_in_month(day):
    following = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (following - timedelta(days=1)).day


def implied_fed_move(month_close, previous_close, meeting, previous_decision=None):
    """Rate change in percentage points that the 30-day fed funds futures price for a meeting in the current month.
    The contract settles to the month's average rate (100 - price); a decision applies from the next day. The rate today is
    the previous month's average, corrected for a decision in that month."""
    n, d = _days_in_month(meeting), meeting.day
    if n == d:
        return None
    now = 100 - previous_close
    if previous_decision and previous_decision["actual"] is not None and previous_decision["previous"] is not None:
        n0 = _days_in_month(previous_decision["date"])
        now += (previous_decision["actual"] - previous_decision["previous"]) * previous_decision["date"].day / n0
    return ((100 - month_close) - now) * n / (n - d)


def fed_outlook(as_of, zq=None, fetch=None, weeks=10):
    """The next Fed rate decision from the economic calendar (date, current rate, forecast) and, once its month has started,
    the cut or hike probability priced by the 30-day fed funds futures (ZQ, front month)."""
    fetch = fetch or fetch_calendar
    found, start = [], as_of + timedelta(days=1)
    for _ in range(weeks):
        found = [x for x in rate_decisions(fetch(start, start + timedelta(days=6))) if x["date"] > as_of]
        if found:
            break
        start += timedelta(days=7)
    if not found:
        return {"date": None, "note": f"no Fed rate decision in the calendar for the next {weeks} weeks"}
    nxt = found[0]
    out = {"date": nxt["date"].isoformat(), "days": (nxt["date"] - as_of).days, "rate": nxt["previous"], "forecast": nxt["forecast"],
           "implied_move_bp": None, "probability": None, "outcome": None}
    same_month = zq is not None and len(zq) and (zq.dates[-1].year, zq.dates[-1].month) == (nxt["date"].year, nxt["date"].month)
    previous = [i for i, d in enumerate(zq.dates) if (d.year, d.month) < (nxt["date"].year, nxt["date"].month)] if same_month else []
    if not previous:
        out["note"] = "the futures price this meeting from the first day of its month on"
        return out
    first = nxt["date"].replace(day=1)
    last_month = (first - timedelta(days=1)).replace(day=1)
    decisions, start = [], last_month
    while start < first:
        decisions += [x for x in rate_decisions(fetch(start, min(start + timedelta(days=6), first - timedelta(days=1))))
                      if last_month <= x["date"] < first]
        start += timedelta(days=7)
    move = implied_fed_move(zq.close[-1], zq.close[previous[-1]], nxt["date"], decisions[-1] if decisions else None)
    if move is None:
        out["note"] = "a decision on the last day of the month is not priced by that month's futures"
        return out
    out["implied_move_bp"] = round(move * 100)
    out["outcome"] = "cut" if move < 0 else "hike"
    out["probability"] = round(min(abs(move) / 0.25, 1.0), 2)
    out["note"] = (f"the 30-day fed funds futures price {move * 100:+.0f} bp: about {out['probability']:.0%} chance of a 25 bp "
                   f"{out['outcome']}" + (" (or a bigger move)" if abs(move) > 0.25 else ""))
    return out


def _finite(bars):
    return [(d, c) for d, c in zip(bars.dates, bars.close) if math.isfinite(c) and c > 0]


def _rank_in_year(values, i, n=252):
    window = values[max(0, i - n + 1):i + 1]
    return sum(v <= values[i] for v in window) / len(window)


def _realized_vol(closes, i, n=20):
    if i < n:
        return None
    returns = [math.log(closes[k] / closes[k - 1]) for k in range(i - n + 1, i + 1)]
    return statistics.stdev(returns) * math.sqrt(252)


def vix_view(vx, nq=None):
    """VX level, its place in the last year, its 5-session change, and the 20-day volatility of the Nasdaq futures."""
    closes = [c for _, c in _finite(vx)]
    if len(closes) < 6:
        return None
    t = len(closes) - 1
    change = closes[t] / closes[t - 5] - 1
    rank = _rank_in_year(closes, t)
    out = {"close": round(closes[t], 2), "year_rank": round(rank, 2), "change_5d": round(change, 4),
           "spike": change >= VIX_SPIKE, "nasdaq_vol20": None, "nasdaq_vol_rank": None}
    out["note"] = (VIX_NOTES["spike"] if out["spike"] else VIX_NOTES["high"] if closes[t] >= 25
                   else VIX_NOTES["low"] if rank <= 0.1 else None)
    if nq is not None:
        q = [c for _, c in _finite(nq)]
        vols = [_realized_vol(q, i) for i in range(len(q))]
        known = [v for v in vols if v is not None]
        if known:
            out["nasdaq_vol20"] = round(known[-1], 3)
            out["nasdaq_vol_rank"] = round(_rank_in_year(known, len(known) - 1), 2)
    return out


def asset_view(name, bars):
    """Trend (close vs EMA50 vs EMA200), 1- and 3-month change, distance from the 1-year high and low, new 1-year high."""
    closes = [c for _, c in _finite(bars)]
    t = len(closes) - 1
    if t < 63:
        return None
    e50, e200 = ema(closes, 50)[t], ema(closes, 200)[t]
    year = closes[max(0, t - 251):t + 1]
    trend = ("uptrend" if e200 and closes[t] > e50 > e200 else "downtrend" if e200 and closes[t] < e50 < e200 else "mixed")
    return {"name": name, "ticker": bars.ticker, "close": round(closes[t], 4), "trend": trend,
            "change_1m": round(closes[t] / closes[t - 21] - 1, 4), "change_3m": round(closes[t] / closes[t - 63] - 1, 4),
            "from_high": round(closes[t] / max(year) - 1, 4), "from_low": round(closes[t] / min(year) - 1, 4),
            "new_high": t >= 252 and closes[t] >= max(closes[t - 252:t])}


def theme_rotation(theme_bars, spy, themes=ROTATION_THEMES, lookbacks=ROTATION_LOOKBACKS, back=ROTATION_BACK):
    """Themes ranked by their average 1- and 3-month return less SPY's, with the rank 4 weeks earlier."""
    days = [d for d, _ in _finite(spy)]
    spy_close = dict(_finite(spy))
    maps = {k: dict(_finite(b)) for k, b in theme_bars.items() if b is not None}

    def relative(members, t):
        out = []
        for n in lookbacks:
            if t - n < 0:
                return None
            a, b = days[t - n], days[t]
            rets = [m[b] / m[a] - 1 for m in members if a in m and b in m]
            if not rets:
                return None
            out.append(sum(rets) / len(rets) - (spy_close[b] / spy_close[a] - 1))
        return out

    rows, t = [], len(days) - 1
    for theme, tickers in themes.items():
        members = [maps[k] for k in tickers if k in maps]
        now = relative(members, t) if members else None
        if now is None:
            continue
        before = relative(members, t - back)
        rows.append({"theme": theme, "tickers": [k for k in tickers if k in maps], "rel_1m": round(now[0], 4),
                     "rel_3m": round(now[1], 4), "score": sum(now) / 2, "score_4w": None if before is None else sum(before) / 2})
    for key, rank in (("score", "rank"), ("score_4w", "rank_4w")):
        ranked = sorted((r for r in rows if r[key] is not None), key=lambda r: -r[key])
        for k, r in enumerate(ranked, 1):
            r[rank] = k
    for r in rows:
        r.setdefault("rank_4w", None)
        r["score"] = round(r["score"], 4)
        r["score_4w"] = None if r["score_4w"] is None else round(r["score_4w"], 4)
    return sorted(rows, key=lambda r: r["rank"])


def rotation_acceleration(closes, spy_closes, windows=ACCELERATION_WINDOWS):
    """Return less SPY's over each window (sessions) to the last common day, and the acceleration: the 10-session
    relative return less the 63-session one scaled to 10 sessions. None without enough common history."""
    days = sorted(d for d in closes if d in spy_closes)
    if len(days) <= max(windows):
        return None
    end = days[-1]
    out = {"as_of": end.isoformat()}
    for n in windows:
        start = days[-1 - n]
        out[f"rs{n}"] = round((closes[end] / closes[start] - 1) - (spy_closes[end] / spy_closes[start] - 1), 4)
    out["acceleration"] = round(out["rs10"] - out["rs63"] * 10 / 63, 4)
    return out


def etf_breakout(bars, closes, reference, spy_closes, cfg, run_cfg, beta_closes=None):
    """The breakout rules on an ETF plus one check: its rotation against SPY accelerates. Returns signal, series, rotation."""
    series = compute(bars, closes, cfg, beta_closes)
    series.update(market_context(bars.dates, reference, None, cfg["regime_slope_bars"]))
    series["sector_etf"] = None
    signal = evaluate_breakout(bars, series, len(bars) - 1, run_cfg)
    rotation = rotation_acceleration(dict(_finite(bars)), spy_closes)
    if rotation is None:
        _check(signal["checks"], "rotation_acceleration", False, "not enough SPY history")
    else:
        _check(signal["checks"], "rotation_acceleration", rotation["acceleration"] > cfg["etf_min_acceleration"],
               f"10 sessions {rotation['rs10']:+.1%} vs SPY against the 63-session pace {rotation['rs63'] * 10 / 63:+.1%}: "
               f"acceleration {rotation['acceleration']:+.1%}, minimum above {cfg['etf_min_acceleration']:+.1%}")
    signal["qualified"] = all(c["passed"] for c in signal["checks"])
    return signal, series, rotation


def _returns(closes):
    """Daily returns by date from a date -> close map."""
    days = sorted(closes)
    return {b: closes[b] / closes[a] - 1 for a, b in zip(days, days[1:])}


def _correlation(a, b, days):
    common = [d for d in days if d in a and d in b]
    if len(common) < 0.75 * len(days):
        return None
    try:
        return statistics.correlation([a[d] for d in common], [b[d] for d in common])
    except statistics.StatisticsError:
        return None


def correlation_map(closes, drivers, themes, n=CORRELATION_DAYS):
    """Correlation of the stock's daily returns over the last n sessions with each driver and theme basket."""
    own = _returns(closes)
    days = sorted(own)[-n:]
    if len(days) < n:
        return None
    by_driver = {k: _correlation(own, v, days) for k, v in drivers.items()}
    by_theme = {k: _correlation(own, v, days) for k, v in themes.items()}
    by_driver = {k: round(v, 2) for k, v in by_driver.items() if v is not None}
    by_theme = {k: round(v, 2) for k, v in by_theme.items() if v is not None}
    best = max(by_theme, key=by_theme.get) if by_theme else None
    return {"drivers": by_driver, "theme": best, "theme_correlation": by_theme.get(best)}


def market_backdrop(as_of, now, count, cfg, context_bars, closes_by_ticker, mapping_tickers):
    """Fed, VIX, oil and metals, theme rotation, and how today's proposals and your positions move with them."""
    missing, futures, stocks = [], {}, {}

    def load(ticker, fetch, label):
        try:
            bars = completed_bars(fetch(ticker, count), now)
            if not _finite(bars):
                raise ValueError("no prices")
            return bars
        except (requests.RequestException, ValueError) as error:
            missing.append({"ticker": ticker, "data": label, "reason": str(error)})
            return None
        finally:
            time.sleep(cfg["request_pause"])

    for t in BACKDROP_FUTURES:
        futures[t] = load(t, fetch_futures, "futures")
    wanted = sorted({k for tickers in ROTATION_THEMES.values() for k in tickers} | {"SPY", "GDX", "SIL"})
    for t in wanted:
        stocks[t] = context_bars.get(t) or load(t, fetch_bars, "ETF or stock")
    try:
        fed = fed_outlook(as_of, futures.get("ZQ"), lambda a, b: _paused(fetch_calendar, cfg, a, b))
    except (requests.RequestException, ValueError) as error:
        missing.append({"ticker": "FOMC", "data": "economic calendar", "reason": str(error)})
        fed = None
    vix = vix_view(futures["VX"], futures.get("NQ")) if futures.get("VX") else None
    assets = [v for v in (asset_view(name, futures.get(t) or stocks.get(t)) for t, name in BACKDROP_ASSETS.items()
                          if futures.get(t) or stocks.get(t)) if v]
    rotation = theme_rotation(stocks, stocks["SPY"]) if stocks.get("SPY") else []
    drivers = {name: _returns(dict(_finite(futures[k]))) for k, name in DRIVERS.items() if futures.get(k) is not None}
    themes = {}
    for theme, tickers in ROTATION_THEMES.items():
        series = [_returns(dict(_finite(stocks[k]))) for k in tickers if stocks.get(k) is not None]
        if series:
            days = set().union(*series)
            themes[theme] = {d: sum(s[d] for s in series if d in s) / sum(d in s for s in series) for d in days}
    mapping = []
    for ticker in mapping_tickers:
        if ticker in closes_by_ticker:
            found = correlation_map(closes_by_ticker[ticker], drivers, themes)
            if found:
                mapping.append({"ticker": ticker, **found})
    alerts = macro_alerts({**{k: v for k, v in stocks.items() if v is not None},
                           **{k: v for k, v in futures.items() if v is not None}}, fed, rotation)
    return {"fed": fed, "vix": vix, "assets": assets, "rotation": rotation, "mapping": mapping, "missing": missing, "alerts": alerts,
            "note": "Information only: it changes no score, filter or proposal. Futures are front month (VX for the VIX)."}


def _paused(fetch, cfg, *args):
    try:
        return fetch(*args)
    finally:
        time.sleep(cfg["request_pause"])


def backdrop_html(b):
    pct = lambda x: "-" if x is None else f"{x:+.1%}"
    items = []
    fed = b.get("fed")
    if fed and fed.get("date"):
        rate = "" if fed["rate"] is None else f", rate now {fed['rate']:g}%"
        forecast = "" if fed["forecast"] is None else f", forecast {fed['forecast']:g}%"
        items.append(f"Fed: next decision {fed['date']} (in {fed['days']} days){rate}{forecast}; {fed['note']}.")
    elif fed:
        items.append(f"Fed: {fed['note']}.")
    v = b.get("vix")
    if v:
        text = (f"VIX futures {v['close']:g} (higher than {v['year_rank']:.0%} of the last year), 5 days {pct(v['change_5d'])}"
                + (" JUMP" if v["spike"] else ""))
        if v["nasdaq_vol20"] is not None:
            text += f"; Nasdaq 20-day volatility {v['nasdaq_vol20']:.0%} (higher than {v['nasdaq_vol_rank']:.0%} of the last year)"
        items.append(text + (f". Note: {v['note']}." if v["note"] else "."))
    for a in b.get("assets", []):
        items.append(f"{a['name']} ({a['ticker']}) {a['close']:g}, {a['trend']}, 1 month {pct(a['change_1m'])}, 3 months "
                     f"{pct(a['change_3m'])}, {pct(a['from_high'])} from its 1-year high, {pct(a['from_low'])} from its low"
                     + (", NEW 1-YEAR HIGH" if a["new_high"] else "") + ".")
    parts = ["<p><b>Market backdrop</b> (information only, no effect on scores)</p>"]
    if items:
        parts.append("<ul>" + "".join(f"<li>{escape(x)}</li>" for x in items) + "</ul>")
    rot = b.get("rotation") or []
    if rot:
        def move(r):
            if r["rank_4w"] is None:
                return "-"
            d = r["rank_4w"] - r["rank"]
            return f"{r['rank_4w']} ({'up ' + str(d) if d > 0 else 'down ' + str(-d) if d < 0 else 'same'})"
        body = "".join(f"<tr><td>{r['rank']}</td><td>{escape(r['theme'])}</td><td>{escape(', '.join(r['tickers']))}</td>"
                       f"<td>{pct(r['rel_1m'])}</td><td>{pct(r['rel_3m'])}</td><td>{move(r)}</td></tr>" for r in rot)
        parts.append("<p><b>Theme rotation</b>: return less SPY's, ranked by the average of 1 and 3 months.</p>"
                     "<table border='1' cellpadding='4' cellspacing='0'><tr><th>rank</th><th>theme</th><th>tickers</th>"
                     f"<th>1 month vs SPY</th><th>3 months vs SPY</th><th>rank 4 weeks ago</th></tr>{body}</table>")
    mapping = b.get("mapping") or []
    if mapping:
        def line(m):
            drivers = ", ".join(f"{k} {v:+.2f}" for k, v in m["drivers"].items())
            theme = f"; moves most with {m['theme']} ({m['theme_correlation']:+.2f})" if m["theme"] else ""
            return f"{m['ticker']}: {drivers}{theme}"
        parts.append(f"<p><b>Correlation of daily returns, last {CORRELATION_DAYS} sessions</b> (proposals and your positions; "
                     "+1 moves together, -1 opposite, 10y note is the bond price, so + means it gains when yields fall):</p><ul>"
                     + "".join(f"<li>{escape(line(m))}</li>" for m in mapping) + "</ul>")
    if b.get("missing"):
        parts.append(f"<p>Backdrop data unavailable: {escape(', '.join(m['ticker'] for m in b['missing']))}.</p>")
    return "".join(parts)


# ---------------------------------------------------------------- main


def settings_with_defaults(settings):
    cfg = {**DEFAULTS, "benchmarks": list(DEFAULTS["benchmarks"]), "context_etfs": list(DEFAULTS["context_etfs"])}
    for key, value in (settings or {}).items():
        if key not in DEFAULTS:
            raise ValueError(f"unknown setting {key!r}. Settings: {', '.join(DEFAULTS)}")
        if key == "benchmarks":
            if not isinstance(value, list) or not value or not all(isinstance(x, str) and TICKER.match(x) for x in value):
                raise ValueError("benchmarks must be a list of tickers, e.g. [\"QQQ\"]; the first one is the market benchmark")
        elif key == "context_etfs":
            if not isinstance(value, list) or not all(isinstance(x, str) and TICKER.match(x) for x in value):
                raise ValueError("context_etfs must be a list of tickers, e.g. [\"SPY\", \"IWM\"]")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"setting {key} must be a number of 0 or more")
        cfg[key] = value
    for key in ("min_bars", "breakout_lookback", "min_base_bars", "min_touches", "rs_lookback", "regime_slope_bars",
                "rally_min_bars", "breakdown_lookback", "squeeze_lookback", "swing_min_days", "swing_max_days", "beta_lookback",
                "run_break_high_bars", "run_break_above_bars", "phase_high_bars", "bounce_low_bars", "dead_cat_below_bars", "dead_cat_alert_gap", "put_spread_days", "after_overextended_bars",
                "index_put_spread_days", "bounce_long_days", "index_long_days"):
        cfg[key] = int(cfg[key])
        if cfg[key] < 1:
            raise ValueError(f"setting {key} must be 1 or more")
    cfg["earnings_block_days"] = int(cfg["earnings_block_days"])
    cfg["put_spread_expiry_days"] = int(cfg["put_spread_expiry_days"])
    if (cfg["run_break_high_bars"] > 120 or cfg["run_break_above_bars"] > 20 or cfg["phase_high_bars"] > 120
            or cfg["dead_cat_below_bars"] > 20):
        raise ValueError("run_break_high_bars and phase_high_bars must be 120 or less, run_break_above_bars and "
                         "dead_cat_below_bars 20 or less")
    if cfg["momentum_hold_adx"] > 100:
        raise ValueError("momentum_hold_adx is an ADX level: 0 (off) to 100")
    if not 0 < cfg["put_spread_take_profit"] <= 1:
        raise ValueError("put_spread_take_profit is a share of the strike width: more than 0 and at most 1")
    if cfg["swing_min_days"] > cfg["swing_max_days"]:
        raise ValueError("swing_min_days must not be above swing_max_days")
    cfg["min_bars"] = max(cfg["min_bars"], 253, cfg["breakout_lookback"] + 2, cfg["breakdown_lookback"] + 2)
    return cfg


def discover(finviz_url, tickers, max_tickers):
    if tickers.strip():
        names = [x.strip().upper() for x in tickers.split(",") if x.strip()]
        bad = [x for x in names if not TICKER.match(x)]
        if bad:
            raise ValueError(f"not tickers: {', '.join(bad)}")
        return list(dict.fromkeys(names))[:max_tickers], "tickers", {}, {}
    urls = finviz_url.split()
    if len(urls) > MAX_URLS:
        raise ValueError(f"at most {MAX_URLS} Finviz URLs, one per line")
    for address in urls:
        url = urlparse(address)
        if url.scheme != "https" or url.hostname not in FINVIZ_HOSTS:
            raise ValueError(f"not a Finviz screener URL: {address}")
    names, sectors, industries = [], {}, {}
    for address in urls:  # up to max_tickers from each URL, in order, without duplicates
        try:
            rows = list(Screener.init_from_url(address, rows=max_tickers))
        except NoResults:
            rows = []
        rows = [r for r in rows if TICKER.match(r.get("Ticker", ""))][:max_tickers]
        sectors.update({r["Ticker"]: r["Sector"] for r in rows if r.get("Sector") in SECTOR_ETFS})
        industries.update({r["Ticker"]: r["Industry"].strip() for r in rows if (r.get("Industry") or "").strip()})
        names += [r["Ticker"] for r in rows]
    return list(dict.fromkeys(names)), "finviz", sectors, industries


EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def short_checks(signal, snapshot, as_of, cfg):
    """Live-only SHORT checks from today's quote page: no earnings within earnings_block_days (required) and squeeze warnings."""
    warnings = []
    if snapshot is None:
        warnings.append("earnings date and short float unknown (quote page not loaded)")
        return warnings
    earnings = snapshot.get("earnings")
    if earnings is not None:
        days = (earnings - as_of).days
        _check(signal["checks"], "no_earnings_soon", not 0 <= days <= cfg["earnings_block_days"],
               f"earnings {snapshot['earnings_text']} ({earnings.isoformat()}), {days} days away, blocked within {cfg['earnings_block_days']}")
    else:
        warnings.append("next earnings date unknown")
    short_float, ratio = snapshot.get("short_float"), snapshot.get("short_ratio")
    if short_float is not None and short_float >= cfg["short_float_warning"] / 2:
        warnings.append(f"squeeze risk: short float {short_float:.1%}" + (" (high)" if short_float > cfg["short_float_warning"] else ""))
    if ratio is not None and ratio > 5:
        warnings.append(f"squeeze risk: {ratio:.1f} days to cover")
    return warnings


def squeeze_checks(signal, snapshot, as_of, cfg):
    """Live-only squeeze checks from today's quote page: short float at least squeeze_min_short_float (required), points for
    a high short float and many days to cover, and an earnings warning. Recomputes the score."""
    warnings = []
    short_float = (snapshot or {}).get("short_float")
    ratio = (snapshot or {}).get("short_ratio")
    if snapshot is None:
        detail = "short float unknown (quote page not loaded)"
    elif short_float is None:
        detail = "short float not shown on the quote page"
    else:
        detail = f"short float {short_float:.1%}, minimum {cfg['squeeze_min_short_float']:.0%}"
    _check(signal["checks"], "short_interest", short_float is not None and short_float >= cfg["squeeze_min_short_float"], detail)
    _points(signal["scoring"], "short_float_25", 20, short_float is not None and short_float >= 0.25,
            f"short float {'-' if short_float is None else f'{short_float:.1%}'}, best 25% or more")
    _points(signal["scoring"], "days_to_cover_5", 20, ratio is not None and ratio >= 5,
            f"{'-' if ratio is None else f'{ratio:.1f}'} days to cover, best 5 or more")
    earnings = (snapshot or {}).get("earnings")
    if earnings is not None and 0 <= (earnings - as_of).days <= cfg["earnings_block_days"]:
        warnings.append(f"earnings {snapshot['earnings_text']} in {(earnings - as_of).days} days: gap risk both ways")
    elif snapshot is not None and earnings is None:
        warnings.append("next earnings date unknown")
    if short_float is not None:
        warnings.append(f"short float {short_float:.1%}" + ("" if ratio is None else f", {ratio:.1f} days to cover"))
    signal["qualified"] = all(c["passed"] for c in signal["checks"])
    signal["score"] = sum(x["earned"] for x in signal["scoring"]) if signal["qualified"] else None
    return warnings


def add_sessions(day, n):
    """day + n weekdays: an estimate of a future session date (market holidays are not known here)."""
    while n > 0:
        day += timedelta(days=1)
        n -= day.weekday() < 5
    return day


def holding_rule(cfg, beta_vs=BETA_BENCHMARK):
    return {"min": cfg["swing_min_days"], "max": cfg["swing_max_days"], "beta_low": cfg["beta_low"], "beta_high": cfg["beta_high"],
            "beta_vs": beta_vs,
            "rule": f"sessions to hold: {cfg['swing_max_days']} at beta <= {cfg['beta_low']:g}, {cfg['swing_min_days']} at beta >= "
                    f"{cfg['beta_high']:g}, linear in between, {holding_days(None, cfg)} if beta is unknown; then exit at the close"
                    + (f"; LONG trades stay open past the target or time exit while ADX14 >= {cfg['momentum_hold_adx']:g} with "
                       "+DI > -DI, and exit at the next open after that trend ends" if cfg["momentum_hold_adx"] else "")}


# Order of the strategies within an action, and why, from the bull backtest (600 stocks, 2016-2026, net of 10 bp).
STRATEGY_PRIORITY = {
    "breakout": (1, "first choice in a bull market: best R per trade of the long setups in the bull backtest"),
    "pullback": (2, "second choice in a bull market: small but steady edge in the bull backtest"),
    "squeeze": (3, "weakest long setup in the bull backtest: no edge after 2022, keep it small"),
    # SHORT setups as bear put debit spread triggers in a decline (bear backtest: ATM / -10% spread, take profit at 80% of
    # the width, else the time exit; return on the debit, before / after 2022-07). All three lost on average.
    # short-dated spread (ATM / -5%, 5 sessions, take profit at 50% of the width), stocks, before / after 2022-07
    "rally_rejection": (1, "bear put debit spread trigger: -0.08 / -0.08 of the debit per trade in the bear backtest, half won"),
    "breakdown": (2, "bear put debit spread trigger: +0.01 / -0.10 of the debit per trade in the bear backtest, 53% won"),
    "dead_cat": (3, "bear put debit spread trigger: -0.13 / -0.18 of the debit per trade in the bear backtest, 43% won"),
    "run_break": (4, "bear put debit spread trigger: -0.23 / -0.13 of the debit per trade in the bear backtest, 44% won"),
}
REASON_LABELS = {
    "outperforms_market": "stronger than the market", "outperforms_sector": "stronger than its sector",
    "underperforms_market": "weaker than the market", "underperforms_sector": "weaker than its sector",
    "rs_line_high": "relative strength at a high", "strong_volume": "strong volume", "strong_close": "strong close",
    "tight_base": "tight base", "near_52w_high": "near the 52-week high", "reward_risk_3": "high reward/risk",
    "healthy_rsi": "healthy RSI", "volume_dry_up": "volume dried up on the pullback", "reversal_volume": "volume rose on the reversal",
    "closes_above_prior_high": "closed above the previous high", "ema_stack": "EMAs stacked up", "heavy_volume": "heavy volume",
    "gap_up": "gap up", "beaten_down": "beaten down before", "room_to_52w_high": "room to the 52-week high",
    "high_beta": "high beta", "wide_range": "wide daily range", "strong_rs": "strong relative strength",
    "market_above_ema50": "market above its EMA50", "market_not_hot": "market not overheated",
    "sector_uptrend": "sector ETF in an uptrend", "sector_beats_market": "sector ETF stronger than the market",
}


def _verdict(q):
    action, regime, direction = q["action"], q.get("regime"), q["direction"]
    if action == "bear_put_spread":
        return "PROPOSED BEAR PUT DEBIT SPREAD"
    if action in ("long", "short"):
        return f"PROPOSED {action.upper()}"
    if action == "no_trade":
        return f"NO TRADE, market {regime}"
    if action == "watch" and q.get("after_overextended"):
        return "WATCH ONLY, pullback right after an overextended market"
    if action == "countertrend_watch":
        return f"WATCH ONLY, against the {regime} market"
    table = actions_table(q["strategy"])
    if table.get(regime) in ("long", "short") and q.get("sector_trend") in ("down", "up"):
        return f"WATCH ONLY, sector ETF {q.get('sector_etf') or ''} trending {q['sector_trend']}".replace("  ", " ")
    return f"WATCH ONLY, market {regime}" if direction == "long" or regime != "bullish" else "WATCH ONLY"


def _setup_text(q):
    s, levels, ind = q["strategy"], q["levels"], q.get("indicators") or {}
    vol = ind.get("rel_volume")
    vol_text = f" on {vol:.1f}x average volume" if vol else ""
    if s == "pullback":
        touch = next((c["detail"] for c in q.get("checks", []) if c["name"] == "ema_touch"), "")
        return f"pullback in an uptrend ({touch}) and a reversal day" if touch else "pullback in an uptrend and a reversal day"
    if s == "breakout":
        return f"fresh close above the {levels.get('resistance')} resistance{vol_text}"
    if s == "squeeze":
        return f"close above the 20-day high {levels.get('breakout_level')}{vol_text}"
    if s == "rally_rejection":
        return "rally rejected at resistance in a downtrend"
    if s == "dead_cat":
        bounce = ind.get("bounce_atr")
        return (f"dead cat bounce rejected at the {levels.get('line_name', '200-day line')} {levels.get('line')}"
                + (f" after a {bounce:.1f} ATR bounce" if bounce is not None else ""))
    if s == "run_break":
        run = ind.get("prior_run")
        return "first close below EMA50 after a run" + (f" of {run:+.0%}" if run is not None else "")
    return f"close below support{vol_text}"


def recommendation(q):
    """One line for the reader: proposed or not and why, the strategy and its rank, the reasons the score found, the plan."""
    priority, note = STRATEGY_PRIORITY.get(q["strategy"], (9, ""))
    earned = sorted((x for x in q.get("scoring", []) if x["earned"]), key=lambda x: -x["points"])[:3]
    reasons = "; ".join(f"{REASON_LABELS.get(x['name'], x['name'].replace('_', ' '))} ({x['detail']})" for x in earned)
    levels = q["levels"]
    spread = q.get("put_spread")
    if q["action"] == "bear_put_spread" and spread:
        legs = spread["legs"]
        plan = (f"buy the {legs[0]['buy']} put and sell the {legs[0]['sell']} put, expiry after {spread['expiry_after']}, "
                f"take profit when the spread is worth {spread['take_profit']:.0%} of the strike width, else close by "
                f"{spread['exit_by']}; the most you can lose is the debit")
        deep = "; market deep in the decline, the worst case in the backtest" if spread.get("deep") else ""
        return (f"{q['ticker']}: {_verdict(q)} ({note}). Why: {_setup_text(q)} in a {spread['decline']}; a short-dated debit "
                f"spread caps the loss and fits a fast move, as such stocks rose on average over the next 30-45 sessions{deep}. "
                f"Plan: {plan}.")
    stop = f"stop {levels['stop']}"
    if levels.get("stop_buffer_atr"):
        stop += f" ({levels['stop_buffer_atr']:g} ATR below the rule's {levels['base_stop']} because beta is {q.get('beta')})"
    plan = (f"{stop}, target {levels['target']}, reward/risk {levels['reward_risk']}, "
            f"exit by {q['time_exit']} ({q['holding_days']} sessions) if neither is hit")
    if q.get("etf"):
        note = ("breakout of a sector or theme ETF whose rotation accelerates: +1.01 / +0.64R per trade before / after "
                "mid-2022 in the bull backtest of 47 ETFs")
    if q.get("tight_long"):
        note = ("tight long on a bounce inside a decline: +0.09 / +0.55R per trade over 10 sessions in the backtest, 60% "
                "won; close before the 200-day line rejection")
    rotation = q.get("rotation")
    if rotation:
        reasons = (f"{q['etf']} ETF, return less SPY's: 5 sessions {rotation['rs5']:+.1%}, 10 {rotation['rs10']:+.1%}, "
                   f"20 {rotation['rs20']:+.1%}, 63 {rotation['rs63']:+.1%} (acceleration {rotation['acceleration']:+.1%})"
                   + (f"; {reasons}" if reasons else ""))
    return (f"{q['ticker']}: {_verdict(q)} {q['strategy']} ({note}). Why: {_setup_text(q)}"
            + (f"; {reasons}" if reasons else "") + f". Plan: {plan}.")


def _overlaps(ticker, others, closes, threshold, n=CORRELATION_DAYS):
    """Those of `others` whose daily returns over the last n sessions correlate `threshold` or more with the ticker's."""
    if not threshold or ticker not in closes:
        return []
    own = _returns(closes[ticker])
    days = sorted(own)[-n:]
    if len(days) < n:
        return []
    found = []
    for other in others:
        if other in closes and other != ticker:
            value = _correlation(own, _returns(closes[other]), days)
            if value is not None and value >= threshold:
                found.append({"ticker": other, "correlation": round(value, 2)})
    return found


def assign_positions(candidates, positions, cfg, known=True, closes=None):
    """Give the proposed LONG candidates, best first, a position of position_size of the budget while fewer than
    max_open_trades slots are taken; one trade per ticker, at most max_per_industry per industry. The slots taken are your
    open positions less those to sell today. A setup past the industry limit becomes an alternative to the trades of
    that industry; a proposal moving with a position or an earlier proposal (overlap_correlation) gets a note. Adds
    `position` to each candidate and a sentence to its recommendation; returns the sizing summary."""
    size, limit, cap = cfg["position_size"], int(cfg["max_open_trades"]), int(cfg["max_per_industry"])
    held, proposed = {p["ticker"] for p in positions}, {}
    to_sell = sum(p["advice"] == "sell" for p in positions)
    used = before = len(positions) - to_sell
    kept = [p["ticker"] for p in positions if p["advice"] != "sell"]
    by_industry = {}
    for p in positions:
        if p["advice"] != "sell" and p.get("industry"):
            by_industry.setdefault(p["industry"], []).append(p["ticker"])
    for c in candidates:
        if c["direction"] != "long" or c["action"] != "long":
            c["position"] = None
            continue
        industry = c.get("industry")
        same = by_industry.get(industry, []) if industry else []
        if c["ticker"] in held:
            c["position"] = {"size": 0.0, "slot": None, "reason": "already open"}
            c["recommendation"] += " Size: none, you already hold this ticker."
        elif c["ticker"] in proposed:
            c["position"] = {"size": 0.0, "slot": None, "reason": "already proposed"}
            c["recommendation"] += " Size: none, this ticker is already proposed above as another setup."
        elif cap and len(same) >= cap:
            c["position"] = {"size": 0.0, "slot": None, "reason": "same industry", "alternative_to": list(same)}
            names = ", ".join(f"your {t}" if t in held else t for t in same)
            c["recommendation"] += (f" Size: none, alternative to {names}: at most {cap} trade{'s' if cap > 1 else ''} "
                                    f"per industry ({industry}).")
            for t in same:
                if t in proposed:
                    proposed[t].setdefault("alternatives", []).append(c)
        elif used >= limit:
            c["position"] = {"size": 0.0, "slot": None, "reason": "no free slot"}
            c["recommendation"] += f" Size: none, {limit} of {limit} slots are taken; skip."
        else:
            used += 1
            overlap = _overlaps(c["ticker"], kept + list(proposed), closes or {}, cfg["overlap_correlation"])
            proposed[c["ticker"]] = {"row": c}
            if industry:
                by_industry.setdefault(industry, []).append(c["ticker"])
            c["position"] = {"size": size, "slot": used, "reason": None}
            c["recommendation"] += f" Size: {size:.0%} of the budget (slot {used} of {limit})."
            if overlap:
                c["position"]["overlap"] = overlap
                c["recommendation"] += (" Same exposure: it moves with " + ", ".join(
                    f"{'your ' if o['ticker'] in held else ''}{o['ticker']} (correlation {o['correlation']:.2f})" for o in overlap)
                    + f" over {CORRELATION_DAYS} sessions; together they act as a larger position.")
    for item in proposed.values():
        if item.get("alternatives"):
            row = item["row"]
            row["alternatives"] = [a["ticker"] for a in item["alternatives"]]
            row["recommendation"] += " Alternatives in the same industry: " + ", ".join(
                f"{a['ticker']} ({a['strategy']}, score {a['score']:g})" if a.get("strategy") else a["ticker"]
                for a in item["alternatives"]) + "."
    return {"position_size": size, "max_open_trades": limit, "open": len(positions) if known else None,
            "to_sell": to_sell if known else None, "held": before if known else None, "proposed": used - before,
            "free": max(limit - used, 0) if known else None,
            "note": None if known else "your positions are unknown without persist: every slot counted as free"}


def candidate_row(strategy, q):
    levels = q["levels"]
    return {"ticker": q["ticker"], "strategy": strategy, "direction": q["direction"], "action": q["action"], "score": q["score"],
            "priority": STRATEGY_PRIORITY.get(strategy, (9, ""))[0], "recommendation": recommendation({**q, "strategy": strategy}),
            "beta": q["beta"], "holding_days": q["holding_days"], "time_exit": q["time_exit"],
            "entry": levels["entry"], "stop": levels["stop"], "target": levels["target"], "reward_risk": levels["reward_risk"],
            "sector": q["sector"], "industry": q.get("industry"), "sector_trend": q["sector_trend"], "warnings": q["warnings"],
            "put_spread": q.get("put_spread"), "etf": q.get("etf"), "rotation": q.get("rotation")}


def index_long_plan(bars, as_of, cfg):
    """Tight long on the benchmark on the first day of a bounce inside a decline: buy at the next open, target the nearest
    200-day line (EMA200 or SMA200) above the close, stop at the low of the last bounce_low_bars, exit after
    index_long_days sessions at the latest."""
    close = bars.close[-1]
    lines = {"EMA200": ema(bars.close, 200)[-1], "SMA200": sma(bars.close, 200)[-1]}
    above = sorted((v, k) for k, v in lines.items() if v is not None and v > close)
    stop = min(bars.low[-cfg["bounce_low_bars"]:])
    return {"ticker": bars.ticker, "type": "tight long", "entry": "next open", "close": round(close, 2),
            "stop": round(stop, 2), "target": round(above[0][0], 2) if above else None,
            "target_line": above[0][1] if above else None,
            "exit_by": add_sessions(as_of, cfg["index_long_days"]).isoformat(), "sessions": cfg["index_long_days"],
            "note": "at the 200-day line watch for the rejection: that is the next put debit spread"}


def put_spread_plan(close, as_of, context, deep, cfg, days=None):
    """Short-dated bear put debit spread for a SHORT setup: strikes from PUT_SPREAD_STRIKES on the last close, closed after
    put_spread_days sessions from as_of at the latest (take profit earlier at put_spread_take_profit of the strike width),
    the first expiry after that exit plus put_spread_expiry_days."""
    days = days or cfg["put_spread_days"]
    exit_by = add_sessions(as_of, days)
    expiry = exit_by + timedelta(days=cfg["put_spread_expiry_days"])
    buy, sell = PUT_SPREAD_STRIKES
    legs = [{"buy": round(close * buy, 2), "sell": round(close * sell, 2)}]
    return {"type": "bear put debit spread", "decline": context, "deep": bool(deep), "legs": legs,
            "expiry_after": expiry.isoformat(), "take_profit": cfg["put_spread_take_profit"], "exit_by": exit_by.isoformat(),
            "sessions": days,
            "max_loss": "the debit paid",
            "note": "nearest listed strikes; the stock's stop does not apply to the spread"}


def _fmt(value, spec, suffix=""):
    """A number in the given format, or N/A when it is missing or not a finite number (never a misleading 0)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "N/A"
    try:
        return format(value, spec) + suffix
    except (TypeError, ValueError):
        return "N/A"


def position_line(p):
    """One line of the email for a position status; any missing price field shows N/A."""
    advice = str(p.get("advice") or "check").upper()
    head = (f"{p.get('ticker', '?')} bought {p.get('buy_date') or 'N/A'} at {_fmt(p.get('buy_price'), 'g')} "
            f"({_fmt(p.get('size'), '.0%')}): {advice}")
    reason = str(p.get("reason") or "no reason given")
    flags = p.get("flags") or []
    tail = f" Flags: {'; '.join(map(str, flags))}." if flags else ""
    if p.get("data_status") == "unavailable":
        return f"{head}, {reason}.{tail}"
    levels = f" (from {p['levels_from']})" if p.get("levels_from") else ""
    return (f"{head}, {_fmt(p.get('return'), '+.1%')} ({_fmt(p.get('r_now'), '+.2f', 'R')}), close {_fmt(p.get('close'), 'g')}, "
            f"stop {_fmt(p.get('stop'), 'g')}, target {_fmt(p.get('target'), 'g')}{levels}. {reason[:1].upper()}{reason[1:]}.{tail}")


def fallback_email_html(result, error):
    """A plain report when email_html fails: the error, the proposals and the positions, every field read defensively."""
    parts = [f"<p><b>The full report could not be built</b> ({escape(f'{type(error).__name__}: {error}')}). The screening "
             "run itself completed; this is a short version.</p>"]
    market = result.get("market") or {}
    parts.append(f"<p>As of {escape(str(result.get('as_of')))}, market {escape(str(market.get('regime', 'N/A')))}.</p>")
    rows = result.get("candidates") or []
    parts.append("<ol>" + "".join(f"<li>{escape(str(r.get('recommendation') or r.get('ticker')))}</li>" for r in rows) + "</ol>"
                 if rows else "<p>No setup qualified today.</p>")
    items = (result.get("positions") or {}).get("items") or []
    lines = []
    for p in items:
        try:
            lines.append(position_line(p))
        except Exception:  # noqa: BLE001 - the fallback must not fail
            lines.append(f"{p.get('ticker', '?')}: status unavailable")
    if items:
        parts.append("<p><b>My positions</b></p><ul>" + "".join(f"<li>{escape(x)}</li>" for x in lines) + "</ul>")
    parts.append("<p>Screening only, not advice.</p>")
    return "".join(parts)


def build_email(result):
    """The email body and the formatting error, if any (then the body is the fallback report)."""
    try:
        return email_html(result), None
    except Exception as error:  # noqa: BLE001 - a formatting bug must not cost the run or the email
        return fallback_email_html(result, error), f"{type(error).__name__}: {error}"


# Extreme move days of QQQ, 2000-2026 (study on the daily bars since 1999): what the next 10 sessions did and how the
# benchmark tight long (index_long_plan) and the 10-session put debit spread (ATM / -5%, take profit at 50% of the width,
# priced with Black-Scholes on realized volatility, 3% cost) did. Any day: higher 10 sessions later 59% of the time; a
# spread bought every third day lost 0.22 of the debit on average and won 41%. Neither trade won on average in any bucket
# but one (an up day outside a decline, not confirmed on SPY), so the email shows the odds and the levels, no proposal.
EXTREME_STATS = {  # key: (direction, size, market); n cases, shares of cases, f10 = median close-to-close return
    ("up", "3-5%", "decline"): dict(n=122, up5=0.50, up10=0.49, f10=-0.001, lost_low=0.63, new_high=0.93, long_mean=-0.007,
                                    long_win=0.50, spread_mean=-0.09, spread_win=0.61),
    ("up", "3-5%", "no decline"): dict(n=27, up5=0.30, up10=0.15, f10=-0.027, lost_low=0.74, new_high=0.85, long_mean=-0.025,
                                       long_win=0.19, spread_mean=0.11, spread_win=0.70),
    ("up", "5%+", "any"): dict(n=60, up5=0.45, up10=0.50, f10=-0.004, lost_low=0.65, new_high=0.90, long_mean=-0.015,
                               long_win=0.42, spread_mean=-0.07, spread_win=0.65),
    ("down", "3-5%", "decline"): dict(n=181, up5=0.52, up10=0.51, f10=0.003, lost_low=0.88, new_high=0.69, long_mean=-0.005,
                                      long_win=0.24, spread_mean=-0.08, spread_win=0.61),
    ("down", "3-5%", "no decline"): dict(n=20, up5=0.45, up10=0.50, f10=-0.001, lost_low=0.95, new_high=0.55, long_mean=-0.007,
                                         long_win=0.20, spread_mean=-0.07, spread_win=0.55),
    ("down", "5%+", "any"): dict(n=54, up5=0.65, up10=0.46, f10=-0.004, lost_low=0.98, new_high=0.67, long_mean=-0.009,
                                 long_win=0.15, spread_mean=-0.00, spread_win=0.67),
}
EXTREME_BASE = {"up10": 0.59, "spread_mean": -0.22, "spread_win": 0.41}


def extreme_move(bars, as_of, phase, cfg):
    """On a benchmark day of extreme_move or more (close to close): the historical odds of the next 10 sessions, the key
    levels, and both index trades (tight long, put debit spread) with their backtest. None on an ordinary day."""
    if not cfg["extreme_move"] or len(bars) < 201:
        return None
    c, h, l = bars.close, bars.high, bars.low
    day = c[-1] / c[-2] - 1
    if abs(day) < cfg["extreme_move"]:
        return None
    direction = "up" if day > 0 else "down"
    decline = (phase or {}).get("decline")
    size = "5%+" if abs(day) >= 0.05 else "3-5%"
    market = "any" if size == "5%+" else "decline" if decline else "no decline"
    stats = EXTREME_STATS[(direction, size, market)]
    e20, e50, e200, m200 = ema(c, 20)[-1], ema(c, 50)[-1], ema(c, 200)[-1], sma(c, 200)[-1]
    levels = {"high": h[-1], "middle": (h[-1] + l[-1]) / 2, "low": l[-1], "previous close": c[-2],
              f"{cfg['bounce_low_bars']}-day low": min(l[-cfg["bounce_low_bars"]:]), "EMA20": e20, "EMA50": e50, "EMA200": e200,
              "SMA200": m200}
    levels = {k: round(v, 2) for k, v in sorted(levels.items(), key=lambda x: -x[1]) if v}
    spread = put_spread_plan(c[-1], as_of, decline or "extreme move", (phase or {}).get("deep"), cfg, cfg["index_put_spread_days"])
    long_plan = index_long_plan(bars, as_of, cfg)
    lean = "put spread" if stats["spread_mean"] > 0 else None
    return {"ticker": bars.ticker, "change": round(day, 4), "direction": direction, "size": size, "market": market,
            "close": round(c[-1], 2), "stats": stats, "base": EXTREME_BASE, "levels": levels, "tight_long": long_plan,
            "put_spread": spread, "proposal": lean, "long_stop": f"low of the last {cfg['bounce_low_bars']} sessions",
            "note": ("the put spread won on average only here (27 cases, mostly 1999-2015) and not on SPY: small size" if lean
                     else "neither trade won on average after such days: no proposal, the odds and levels are for orientation")}


# Market alerts: shown only when a move is about as large as the top 5% of daily or 5-session moves in 2020-2026.
ALERT_LEVELS = {  # ticker: (name, 1-session move, 5-session move), both as absolute changes
    "CL": ("WTI crude", 0.05, 0.10), "QA": ("Brent crude", 0.05, 0.10), "GC": ("Gold", 0.025, 0.05), "SI": ("Silver", 0.05, 0.10),
    "PL": ("Platinum", 0.045, 0.09), "HG": ("Copper", 0.03, 0.07), "DX": ("Dollar index", 0.01, 0.02),
    "ZN": ("10y note", 0.01, 0.02), "GDX": ("Gold miners", 0.05, 0.10), "SIL": ("Silver miners", 0.06, 0.12),
}
VIX_ALERT = (0.15, VIX_SPIKE, 30.0)  # VX up 15% in a session, up 25% in 5 sessions, or at 30 or more
FED_ALERT_DAYS = 7
THEME_ALERT_RANKS = 3  # a theme new in the top 3 of the rotation table, 3 or more places higher than 4 weeks earlier, beating SPY


def _changes(bars):
    closes = [c for _, c in _finite(bars)] if bars is not None else []
    if len(closes) < 6:
        return None
    return closes[-1], closes[-1] / closes[-2] - 1, closes[-1] / closes[-6] - 1


def macro_alerts(series, fed=None, rotation=()):
    """The market moves worth a look today, as sentences; `series` maps tickers (futures and ETFs) to bars."""
    out = []
    vx = _changes(series.get("VX"))
    if vx:
        close, one, five = vx
        why = [x for x, hit in ((f"{one:+.0%} today", one >= VIX_ALERT[0]), (f"{five:+.0%} in 5 sessions", five >= VIX_ALERT[1]),
                                (f"at {close:.1f}", close >= VIX_ALERT[2])) if hit]
        if why:
            note = VIX_NOTES["spike"] if five >= VIX_ALERT[1] else VIX_NOTES["high"] if close >= 25 else ""
            out.append(f"VIX futures {close:.1f}: " + ", ".join(why) + (f". {note[0].upper()}{note[1:]}." if note else "."))
    for ticker, (name, day, week) in ALERT_LEVELS.items():
        found = _changes(series.get(ticker))
        if not found:
            continue
        close, one, five = found
        why = [x for x, hit in ((f"{one:+.1%} today", abs(one) >= day), (f"{five:+.1%} in 5 sessions", abs(five) >= week)) if hit]
        if why:
            out.append(f"{name} ({ticker}) {close:g}: " + ", ".join(why) + ".")
    if fed and fed.get("date") and fed.get("days") is not None and fed["days"] <= FED_ALERT_DAYS:
        rate = "" if fed.get("rate") is None else f", rate now {fed['rate']:g}%"
        forecast = "" if fed.get("forecast") is None else f", forecast {fed['forecast']:g}%"
        out.append(f"Fed decision {fed['date']} (in {fed['days']} days){rate}{forecast}; {fed.get('note') or ''}.".replace("; .", "."))
    for r in rotation:
        if (r.get("rank") and r["rank"] <= THEME_ALERT_RANKS and r.get("rank_4w") and r["rank_4w"] - r["rank"] >= THEME_ALERT_RANKS
                and (r.get("rel_1m") or 0) > 0):
            out.append(f"Theme rotation: {r['theme']} ({', '.join(r['tickers'])}) rose to rank {r['rank']} of the themes from "
                       f"{r['rank_4w']} four weeks ago ({r['rel_1m']:+.1%} vs SPY in 1 month).")
    return out


def extreme_html(x):
    s, base, lv = x["stats"], x["base"], x["levels"]
    where = {"decline": "inside a decline", "no decline": "outside a decline", "any": "in any market"}[x["market"]]
    long_plan, spread = x["tight_long"], x["put_spread"]
    leg = spread["legs"][0]
    target = (f"target the {long_plan['target_line']} {long_plan['target']}" if long_plan.get("target") else "no 200-day line above")
    lines = [
        f"History (QQQ since 2000, {s['n']} days {x['direction']} {x['size']} {where}): higher 5 sessions later {s['up5']:.0%}, "
        f"10 sessions later {s['up10']:.0%} (any day {base['up10']:.0%}), median 10-session change {s['f10']:+.1%}; today's low "
        f"was broken within 10 sessions {s['lost_low']:.0%} of the time, today's high exceeded {s['new_high']:.0%}.",
        "Key levels: " + ", ".join(f"{k} {v:g}" for k, v in lv.items()) + ".",
        f"Tight long: buy at the next open, stop {long_plan['stop']} (the {x['long_stop']}), {target}, out by "
        f"{long_plan['exit_by']}. Backtest: {s['long_mean']:+.1%} per trade on average, won {s['long_win']:.0%}.",
        f"Put debit spread: buy the {leg['buy']} put, sell the {leg['sell']} put, expiry after {spread['expiry_after']}, take "
        f"profit at {spread['take_profit']:.0%} of the width, else close by {spread['exit_by']}. Backtest: {s['spread_mean']:+.2f} "
        f"of the debit on average, won {s['spread_win']:.0%} (a spread on any day: {base['spread_mean']:+.2f}, won "
        f"{base['spread_win']:.0%}).",
        (f"PROPOSED: {x['proposal']}; {x['note']}." if x["proposal"] else f"No proposal: {x['note']}."),
    ]
    return (f"<p><b>Extreme move: {escape(x['ticker'])} {x['change']:+.1%} today</b> (close {x['close']:g})</p><ul>"
            + "".join(f"<li>{escape(t)}</li>" for t in lines) + "</ul>")


def compact_email_html(result):
    """The short daily email: market line, extreme move, proposals as they are, your positions, the 3 fastest rotating
    ETFs, and the alerts of the day; everything else stays in the run result."""
    m = result["market"]
    b = m["benchmark"]
    pct = lambda x: "N/A" if x is None else f"{x:+.1%}"
    phase = m.get("phase") or {}
    head = (f"<p><b>Market {escape(m['regime'])}</b>, phase {escape(str(phase.get('phase', 'unknown')))}"
            f"{' (' + escape(phase['decline']) + ')' if phase.get('decline') else ''}: {escape(b['ticker'])} {b['close']}, "
            f"{pct(b.get('vs_sma200'))} vs SMA200, 20 days {pct(b.get('change_20d'))}. Long setups: {escape(str(m.get('long_setups')))}.</p>")
    parts = [head]
    if phase.get("switched") or phase.get("dead_cat") or m.get("index_long"):
        parts.append(f"<p>{escape(phase.get('advice', ''))}</p>")
    spread = m.get("index_put_spread")
    if spread:
        leg = spread["legs"][0]
        parts.append(f"<p><b>Index debit spread</b>: {escape(spread['ticker'])} buy the {leg['buy']} put, sell the {leg['sell']} put, "
                     f"expiry after {spread['expiry_after']}, take profit at {spread['take_profit']:.0%} of the width, "
                     f"else close by {spread['exit_by']}; the most you can lose is the debit.</p>")
    long_plan = m.get("index_long")
    if long_plan:
        target = (f"target the {long_plan['target_line']} at {long_plan['target']}" if long_plan["target"] else "no 200-day line above")
        parts.append(f"<p><b>Index tight long</b>: {escape(long_plan['ticker'])} buy at the next open, {target}, stop "
                     f"{long_plan['stop']} (the bounce low), else close by {long_plan['exit_by']}.</p>")
    if result.get("extreme_move"):
        parts.append(extreme_html(result["extreme_move"]))

    rows = result.get("candidates") or []
    proposed = [r for r in rows if r["action"] in TRADE_ACTIONS]
    if proposed:
        parts.append(f"<p><b>Proposed</b>: {len(proposed)} of {len(rows)} setups, best first.</p><ol>"
                     + "".join(f"<li>{escape(r['recommendation'])}</li>" for r in proposed) + "</ol>")
    else:
        parts.append(f"<p><b>Proposed</b>: none today ({len(rows)} setups, all watch only).</p>" if rows
                     else "<p>No setup qualified today.</p>")
    watch = [r for r in rows if r["action"] not in TRADE_ACTIONS]
    if watch:
        parts.append("<p>Watch only: " + escape(", ".join(f"{r['ticker']} {r['strategy']}" for r in watch)) + ".</p>")
    sizing = result.get("sizing")
    if sizing and any(r["action"] == "long" for r in proposed):
        state = (f"{sizing['open']} open, {sizing['to_sell']} to sell, {sizing['proposed']} proposed today, {sizing['free']} free"
                 if sizing["open"] is not None else sizing["note"])
        parts.append(f"<p>Sizing: {sizing['position_size']:.0%} per long trade, at most {sizing['max_open_trades']} open "
                     f"({escape(state)}).</p>")

    mine = result.get("positions")
    if mine is not None:
        items = mine.get("items") or []
        act = [p for p in items if p.get("advice") != "hold"]
        parts.append(f"<p><b>My positions: {_fmt(mine.get('open'), 'd')} of {_fmt(mine.get('max_open_trades'), 'd')} open</b>"
                     + (f", {len(act)} to sell or check" if act else "") + ".</p>")
        if items:
            ordered = act + [p for p in items if p.get("advice") == "hold"]
            parts.append("<ul>" + "".join(f"<li>{escape(position_line(p))}</li>" for p in ordered) + "</ul>")

    etf = result.get("etf_setups") or {}
    top = [r for r in etf.get("rotation") or [] if r.get("acceleration", 0) > 0][:3]
    if top:
        parts.append("<p><b>Rotation, fastest accelerating ETFs vs SPY</b> (5 / 10 / 20 / 63 sessions; information, ETF trades "
                     "come only from a breakout above):</p><ul>" + "".join(
                         f"<li>{escape(r['ticker'])} {escape(r['name'])}: {pct(r['rs5'])} / {pct(r['rs10'])} / {pct(r['rs20'])} / "
                         f"{pct(r['rs63'])}, acceleration {pct(r['acceleration'])}</li>" for r in top) + "</ul>")

    backdrop = result.get("backdrop")
    if backdrop is not None:
        alerts = backdrop.get("alerts") or []
        parts.append("<p><b>Alerts</b></p><ul>" + "".join(f"<li>{escape(a)}</li>" for a in alerts) + "</ul>" if alerts else
                     "<p>Alerts: none (VIX, oil, metals, dollar, 10y note, Fed and theme rotation within their usual range).</p>")

    stored = result.get("persistence")
    if stored and (stored.get("closed_today") or stored.get("data_unavailable")):
        def line(r):
            return f"{r['ticker']} {r['strategy']} {r['status']}" + (f" {r['r']:+.2f}R" if r.get("r") is not None else "")
        parts.append(f"<p>Tracked signals closed today: {escape(', '.join(line(r) for r in stored['closed_today']) or 'none')}; "
                     f"data unavailable: {escape(', '.join(r['ticker'] for r in stored['data_unavailable']) or 'none')}.</p>")
    parts.append("<p>Screening only, not advice. Full details (all setups, checks, backdrop, correlations) are in the Windmill run "
                 "result; settings compact_report 0 restores the long email.</p>")
    return "".join(parts)


def email_html(result):
    if (result.get("settings") or {}).get("compact_report"):
        return compact_email_html(result)
    m = result["market"]
    b = m["benchmark"]
    pct = lambda x: "-" if x is None else f"{x:+.1%}"
    parts = [f"<p><b>Market {escape(m['regime'])}</b> ({escape(b['ticker'])} {b['close']}, {pct(b['vs_sma200'])} vs SMA200, "
             f"20 days {pct(b['change_20d'])}; {m['breadth']['above_sma200']} of {m['breadth']['sector_etfs']} sector ETFs above their SMA200). "
             f"Favoured: {escape(', '.join(m['favoured']) or 'none')}.</p>"]
    if m.get("phase"):
        parts.append(f"<p><b>Market phase {escape(m['phase']['phase'])}</b>: {escape(m['phase']['advice'])}</p>")
    spread = m.get("index_put_spread")
    if spread:
        leg = spread["legs"][0]
        parts.append(f"<p><b>Index debit spread</b>: {escape(spread['ticker'])} buy the {leg['buy']} put, sell the {leg['sell']} put, "
                     f"expiry after {spread['expiry_after']}, take profit at {spread['take_profit']:.0%} of the width, "
                     f"else close by {spread['exit_by']}; the most you can lose is the debit.</p>")
    long_plan = m.get("index_long")
    if long_plan:
        target = (f"target the {long_plan['target_line']} at {long_plan['target']}" if long_plan["target"]
                  else "no 200-day line above")
        parts.append(f"<p><b>Index tight long</b>: {escape(long_plan['ticker'])} buy at the next open, {target}, stop "
                     f"{long_plan['stop']} (the bounce low), else close by {long_plan['exit_by']}; "
                     f"{escape(long_plan['note'])}.</p>")
    context = m.get("context") or {}
    if context:
        parts.append("<p>" + "; ".join(f"{escape(k)} {v['close']} ({escape(str(v['trend']))}, {pct(v['vs_sma200'])} vs SMA200, "
                                       f"20 days {pct(v['change_20d'])})" for k, v in context.items()) + "</p>")
    if result.get("backdrop"):
        parts.append(backdrop_html(result["backdrop"]))
    mine = result.get("positions")
    if mine is not None:
        items = mine.get("items") or []
        sell = [p for p in items if p.get("advice") != "hold"]
        parts.append(f"<p><b>My positions: {_fmt(mine.get('open'), 'd')} of {_fmt(mine.get('max_open_trades'), 'd')} open</b>"
                     + (f", {len(sell)} to sell or check" if sell else "") + ".</p>")
        if items:
            parts.append("<ul>" + "".join(f"<li>{escape(position_line(p))}</li>" for p in items) + "</ul>")
        else:
            parts.append("<p>None recorded: run f/screener/positions with action buy when you open a trade.</p>")
    rows = result["candidates"]
    if rows:
        proposed = [r for r in rows if r["action"] in TRADE_ACTIONS]
        parts.append(f"<p><b>Proposed</b>: {len(proposed)} of {len(rows)} setups. Best first; the rest are watch only.</p><ol>"
                     + "".join(f"<li>{escape(r['recommendation'])}</li>" for r in rows) + "</ol>")
        sizing = result.get("sizing")
        if sizing:
            state = (f"{sizing['open']} open, {sizing['to_sell']} to sell, {sizing['proposed']} proposed today, {sizing['free']} free"
                     if sizing["open"] is not None else sizing["note"])
            parts.append(f"<p><b>Sizing</b>: {sizing['position_size']:.0%} of the budget per long trade, at most "
                         f"{sizing['max_open_trades']} open ({escape(state)}).</p>")
        keys = ["ticker", "strategy", "action", "size", "score", "entry", "stop", "target", "reward_risk", "beta", "holding_days",
                "time_exit", "sector", "industry", "warnings"]
        size = lambda r: ("-" if not r.get("position") else f"{r['position']['size']:.0%}" if r["position"]["slot"]
                          else f"{r['position']['size']:.0%} if you sell {r['position']['replaces']}" if r["position"]["reason"] == "swap"
                          else r["position"]["reason"])
        cell = lambda r, k: size(r) if k == "size" else (r.get("etf") and f"ETF: {r['etf']}" or "-") if k == "industry" and not r.get(k) else "; ".join(r[k]) if isinstance(r[k], list) else str(r[k])
        head = "".join(f"<th>{k}</th>" for k in keys)
        body = "".join("<tr>" + "".join(f"<td>{escape(cell(r, k))}</td>" for k in keys) + "</tr>" for r in rows)
        parts.append(f"<table border='1' cellpadding='4' cellspacing='0'><tr>{head}</tr>{body}</table>")
    else:
        parts.append("<p>No setup qualified today.</p>")
    stored = result.get("persistence")
    if stored:
        def line(r):
            text = f"{r['ticker']} {r['strategy']} {r['status']}"
            return escape(text + (f" {r['r']:+.2f}R" if r.get("r") is not None else ""))
        parts.append(f"<p><b>Tracked signals</b>: {len(stored['active'])} active, {stored['inserted']} new. "
                     f"Closed today: {', '.join(line(r) for r in stored['closed_today']) or 'none'}. "
                     f"Data unavailable: {escape(', '.join(r['ticker'] for r in stored['data_unavailable'])) or 'none'}.</p>")
    parts.append("<p>Screening only, not advice. SHORT setups are not traded short; in a decline or an overextended bull market they time bear put debit spreads "
                 "(they lost on average in the backtest), whose results come from a backtest with modelled option prices. Squeeze rules are untested hypotheses.</p>")
    return "".join(parts)


def send_email(to, subject, html):
    smtp = wmill.get_resource(SMTP_RESOURCE)
    message = EmailMessage()
    message["From"] = smtp.get("from") or smtp["user"]
    message["To"] = to
    message["Subject"] = subject
    message.set_content(subject)
    message.add_alternative(html, subtype="html")
    with smtplib.SMTP(smtp["host"], int(smtp["port"]), timeout=30) as server:
        server.starttls()
        server.login(smtp["user"], smtp["password"])
        server.send_message(message)


def _load(name, count, now, data_errors, label, pause):
    try:
        return completed_bars(fetch_bars(name, count), now)
    except (requests.RequestException, ValueError) as error:
        data_errors.append({"ticker": name, "reason": f"{label}: {error}"})
        return None
    finally:
        time.sleep(pause)


def main(finviz_url: str = "", tickers: str = "", strategy: str = "all", max_tickers: int = 50, track_days: int = 10,
         validation_days: int = 0, settings: dict | None = None, email: str = "", persist: bool = False) -> dict:
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    if strategy in STRATEGY_GROUPS:
        strategies = list(STRATEGY_GROUPS[strategy])
    elif strategy in STRATEGIES:
        strategies = [strategy]
    else:
        raise ValueError(f"strategy must be {', '.join(STRATEGY_GROUPS)} or one of {', '.join(STRATEGIES)}")
    if not finviz_url.strip() and not tickers.strip():
        raise ValueError("give a Finviz screener URL or a comma-separated list of tickers")
    if not 1 <= max_tickers <= MAX_TICKERS:
        raise ValueError(f"max_tickers must be 1 to {MAX_TICKERS}")
    if not 0 <= validation_days <= MAX_VALIDATION_DAYS:
        raise ValueError(f"validation_days must be 0 to {MAX_VALIDATION_DAYS}")
    if not 0 <= track_days <= MAX_TRACK_DAYS:
        raise ValueError(f"track_days must be 0 to {MAX_TRACK_DAYS}")
    if email.strip() and not EMAIL.match(email.strip()):
        raise ValueError(f"not an email address: {email}")
    cfg = settings_with_defaults(settings)
    longest = cfg["swing_max_days"]
    count = cfg["min_bars"] + max(validation_days + longest, track_days) + 60

    names, source, sectors, industries = discover(finviz_url, tickers, max_tickers)
    query = datatable_query() if persist else None
    stored = query(LOAD_SQL) if persist else []
    active = [row for row in stored if row.get("closed_at") is None]
    positions = query(LOAD_POSITIONS_SQL) if persist else []
    position_tickers, position_data = {p["ticker"] for p in positions}, {}
    pending_by_ticker = {}
    for row in active:
        pending_by_ticker.setdefault(row["ticker"], []).append(row)
    updates, new_rows = [], []
    benchmarks, benchmark_bars, benchmark_info, data_errors = {}, {}, {}, []
    for name in cfg["benchmarks"]:
        bars = _load(name, count, now, data_errors, "benchmark", cfg["request_pause"])
        if bars is not None:
            benchmarks[name] = {d: c for d, c in zip(bars.dates, bars.close) if math.isfinite(c) and c > 0}
            benchmark_bars[name] = bars
            benchmark_info[name] = {"as_of": bars.dates[-1].isoformat(), "close": round(bars.close[-1], 2)}
    if not benchmarks:
        raise RuntimeError("no benchmark data, so the market calendar and relative strength cannot be checked")
    calendar = sorted(set().union(*benchmarks.values()))
    rs_benchmarks = [b for b in cfg["benchmarks"] if b in benchmarks]
    reference = benchmark_bars[rs_benchmarks[0]]
    sector_bars, sector_closes, sector_trends = {}, {}, {}
    for etf in sorted(set(SECTOR_ETFS.values())):
        bars = _load(etf, count, now, data_errors, "sector ETF", cfg["request_pause"])
        if bars is not None:
            sector_bars[etf] = bars
            sector_closes[etf] = {d: c for d, c in zip(bars.dates, bars.close) if math.isfinite(c) and c > 0}
            sector_trends[etf] = trend_labels(bars.dates, bars.close, cfg["regime_slope_bars"])
    context_bars = {}
    for name in cfg["context_etfs"]:
        bars = _load(name, count, now, data_errors, "context ETF", cfg["request_pause"])
        if bars is not None:
            context_bars[name] = bars
    breadth_by_date = breadth(sector_bars)
    regime = market_regime(reference.dates, reference.close, breadth_by_date, cfg)
    phases = market_phase(reference, regime, cfg)
    market = {"name": rs_benchmarks[0], "open": dict(zip(reference.dates, reference.open)),
              "close": dict(zip(reference.dates, reference.close)), "regime": regime}
    beta_source = context_bars.get(BETA_BENCHMARK) or reference
    holding = holding_rule(cfg, beta_source.ticker)
    beta_closes = {d: c for d, c in zip(beta_source.dates, beta_source.close) if math.isfinite(c) and c > 0}
    market_info = market_summary(rs_benchmarks[0], reference, breadth_by_date, regime, sector_bars, cfg, context_bars, phases)
    phase_now = phases.get(reference.dates[-1]) or {}
    extreme = extreme_move(reference, reference.dates[-1], phase_now, cfg)
    run_cfg = {**cfg, "benchmarks": rs_benchmarks}
    as_of = calendar[-1]

    results = {s: {"qualified": [], "rejected": []} for s in strategies}
    trades, signals, base, without_earnings, screened, tracked = [], [], [], [], 0, []
    closes_by_ticker = {}
    for name in names:
        try:
            bars = completed_bars(fetch_bars(name, count), now)
            problems = quality_problems(bars, calendar, cfg)
        except (requests.RequestException, ValueError) as error:
            problems = [str(error)]
        time.sleep(cfg["request_pause"])
        if problems:
            data_errors.append({"ticker": name, "reason": "; ".join(problems)})
            updates += [stored_update(row, None, calendar, as_of, "; ".join(problems)) for row in pending_by_ticker.pop(name, [])]
            if name in position_tickers:
                position_data[name] = "; ".join(problems)
            continue
        updates += [stored_update(row, bars, calendar, as_of, hold_adx=cfg["momentum_hold_adx"]) for row in pending_by_ticker.pop(name, [])]
        screened += 1
        closes_by_ticker[name] = dict(_finite(bars))
        sector, industry = sectors.get(name), industries.get(name)
        if source == "tickers":
            try:
                sector, industry = fetch_profile(name)
            except (requests.RequestException, ValueError):
                sector = industry = None
            time.sleep(cfg["request_pause"])
            if industry:
                industries[name] = industry
        etf = SECTOR_ETFS.get(sector)
        closes = {b: benchmarks[b] for b in rs_benchmarks}
        if etf in sector_closes:
            closes["sector"] = sector_closes[etf]
        series = compute(bars, closes, cfg, beta_closes)
        series.update(market_context(bars.dates, reference, sector_bars.get(etf), cfg["regime_slope_bars"]))
        beta = _beta_at(series, len(bars) - 1)
        hold = horizon_for(cfg, series, len(bars) - 1)
        series["sector_etf"] = etf if etf in sector_closes else None
        if name in position_tickers:
            position_data[name] = (bars, series)
        trends = sector_trends.get(etf, {})
        label, trend = regime.get(bars.dates[-1], "unknown"), trends.get(bars.dates[-1])
        loaded = {}

        def snapshot(name=name):
            if "value" not in loaded:
                try:
                    loaded["value"] = fetch_snapshot(name, as_of)
                except (requests.RequestException, ValueError):
                    loaded["value"] = None
                time.sleep(cfg["request_pause"])
            return loaded["value"]

        for s in strategies:
            signal = EVALUATORS[s](bars, series, len(bars) - 1, run_cfg)
            warnings = signal_warnings(signal) if signal["qualified"] else []
            if signal["qualified"] and signal["direction"] == "short":
                warnings += short_checks(signal, snapshot(), as_of, cfg) + signal.get("notes", [])
                signal["qualified"] = all(c["passed"] for c in signal["checks"])
            elif signal["qualified"] and s == "squeeze":
                warnings += squeeze_checks(signal, snapshot(), as_of, cfg)
            if signal["qualified"]:
                action = action_for(signal["direction"], label, trend, s, spread_context(phase_now), bounce_long(phase_now),
                                    after_overextended(phase_now))
                tight = action == "long" and s == "pullback" and bounce_long(phase_now)
                hold_s = cfg["bounce_long_days"] if tight else hold
                time_exit = add_sessions(as_of, hold_s).isoformat()
                results[s]["qualified"].append({
                    **{k: signal[k] for k in ("ticker", "signal_date", "score", "direction")}, "strategy": s, "regime": label,
                    "action": action, "sector": sector, "industry": industry, "sector_etf": series["sector_etf"],
                    "sector_trend": trend, "beta": None if beta is None else round(beta, 2), "holding_days": hold_s,
                    "time_exit": time_exit, "warnings": warnings, "tight_long": tight,
                    "after_overextended": signal["direction"] == "long" and after_overextended(phase_now),
                    "put_spread": put_spread_plan(bars.close[-1], as_of, spread_context(phase_now) or "bear",
                                                  phase_now.get("deep"), cfg)
                    if action == "bear_put_spread" else None,
                    **{k: signal[k] for k in ("levels", "relative_strength", "checks", "scoring", "indicators")}})
            else:
                results[s]["rejected"].append({"ticker": name, "failed": [c for c in signal["checks"] if not c["passed"]]})
        if track_days:
            rows = track_signals(bars, series, strategies, track_days, run_cfg, regime, trends, phases)
            if any(x["strategy"] == "squeeze" for x in rows):
                short_float = (snapshot() or {}).get("short_float")
                keep = short_float is not None and short_float >= cfg["squeeze_min_short_float"]
                rows = [x if x["strategy"] != "squeeze" else {**x, "short_float_today": short_float} for x in rows
                        if x["strategy"] != "squeeze" or keep]
            tracked += [{**x, "sector": sector} for x in rows]
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
    for name, rows in sorted(pending_by_ticker.items()):  # stored signals whose ticker is not in today's universe
        try:
            bars = completed_bars(fetch_bars(name, count), now)
            problems = quality_problems(bars, calendar, cfg)
        except (requests.RequestException, ValueError) as error:
            bars, problems = None, [str(error)]
        time.sleep(cfg["request_pause"])
        updates += [stored_update(row, None if problems else bars, calendar, as_of, "; ".join(problems) or None, cfg["momentum_hold_adx"])
                    for row in rows]
    for name in sorted(position_tickers - set(position_data)):  # your positions whose ticker is not in today's universe
        try:
            bars = completed_bars(fetch_bars(name, count), now)
            problems = quality_problems(bars, calendar, cfg)
        except (requests.RequestException, ValueError) as error:
            bars, problems = None, [str(error)]
        time.sleep(cfg["request_pause"])
        position_data[name] = ("; ".join(problems) if problems else
                               (bars, compute(bars, {b: benchmarks[b] for b in rs_benchmarks}, cfg, beta_closes)))
    etf_bars, etf_rotation, etf_missing = {}, [], []
    spy_bars = context_bars.get("SPY") or benchmark_bars.get("SPY")
    if cfg["etf_setups"] and "breakout" in strategies:
        for etf in (["SPY"] if spy_bars is None else []) + list(ETF_SETUPS):
            bars = sector_bars.get(etf) or context_bars.get(etf) or benchmark_bars.get(etf)
            if bars is None:
                try:
                    bars = completed_bars(fetch_bars(etf, count), now)
                    problems = quality_problems(bars, calendar, cfg)
                except (requests.RequestException, ValueError) as error:
                    problems = [str(error)]
                time.sleep(cfg["request_pause"])
                if problems:
                    etf_missing.append({"ticker": etf, "reason": "; ".join(problems)})
                    continue
            if etf == "SPY":
                spy_bars = bars
            else:
                etf_bars[etf] = bars
    if etf_bars and spy_bars is not None:
        spy_closes = dict(_finite(spy_bars))
        label_now = regime.get(reference.dates[-1], "unknown")
        for etf, bars in etf_bars.items():
            signal, series, rotation = etf_breakout(bars, {b: benchmarks[b] for b in rs_benchmarks}, reference, spy_closes, cfg,
                                                    run_cfg, beta_closes)
            if rotation:
                etf_rotation.append({"ticker": etf, "name": ETF_SETUPS[etf], **rotation})
            if not signal["qualified"] or etf in names:
                continue
            beta = _beta_at(series, len(bars) - 1)
            hold = horizon_for(cfg, series, len(bars) - 1)
            action = action_for("long", label_now, None, "breakout", spread_context(phase_now), bounce_long(phase_now),
                                after_overextended(phase_now))
            results["breakout"]["qualified"].append({
                **{k: signal[k] for k in ("ticker", "signal_date", "score", "direction")}, "strategy": "breakout",
                "regime": label_now, "action": action, "sector": None, "industry": None, "sector_etf": None,
                "sector_trend": None, "beta": None if beta is None else round(beta, 2), "holding_days": hold,
                "time_exit": add_sessions(as_of, hold).isoformat(), "warnings": signal_warnings(signal), "tight_long": False,
                "after_overextended": after_overextended(phase_now), "put_spread": None, "etf": ETF_SETUPS[etf],
                "rotation": rotation, **{k: signal[k] for k in ("levels", "relative_strength", "checks", "scoring", "indicators")}})
        etf_rotation.sort(key=lambda r: -r["acceleration"])
    shorts_today = {}
    for s in strategies:
        if DIRECTIONS[s] == "short":
            for q in results[s]["qualified"]:
                shorts_today.setdefault(q["ticker"], []).append(s)
    statuses = []
    for pos in positions:
        data = position_data.get(pos["ticker"], "not loaded")
        if isinstance(data, str):
            statuses.append(position_unknown(pos, data))
            continue
        try:
            snap = fetch_snapshot(pos["ticker"], as_of)
        except (requests.RequestException, ValueError):
            snap = None
        time.sleep(cfg["request_pause"])
        days = (snap["earnings"] - as_of).days if snap and snap.get("earnings") else None
        statuses.append(position_status(pos, *data, cfg, position_signal(stored, pos["ticker"], _day(pos["buy_date"])),
                                        shorts_today.get(pos["ticker"], ()), days))
        statuses[-1]["industry"] = industries.get(pos["ticker"]) or (snap or {}).get("industry")
    for status in statuses:
        status.setdefault("industry", industries.get(status["ticker"]))
    for s in strategies:
        results[s]["qualified"].sort(key=lambda x: (-x["score"], x["ticker"]))
    candidates = sorted((candidate_row(s, q) for s in strategies for q in results[s]["qualified"]),
                        key=lambda x: (ACTION_ORDER.get(x["action"], 9), x["priority"], -x["score"], x["ticker"], x["strategy"]))
    for name, data in position_data.items():
        if not isinstance(data, str):
            closes_by_ticker.setdefault(name, dict(_finite(data[0])))
    for name, bars in etf_bars.items():
        closes_by_ticker.setdefault(name, dict(_finite(bars)))
    sizing = assign_positions(candidates, statuses, cfg, persist, closes_by_ticker)
    swaps = plan_swaps(candidates, statuses, cfg) if persist else []
    backdrop = None
    if cfg["backdrop"]:
        mapped = list(dict.fromkeys([c["ticker"] for c in candidates if c["action"] in TRADE_ACTIONS]
                                    + [p["ticker"] for p in statuses if p["data_status"] != "unavailable"]))
        backdrop = market_backdrop(as_of, now, count, cfg, {**etf_bars, **context_bars}, closes_by_ticker, mapped)

    duration = time.monotonic() - started
    counts = ", ".join(f"{s} {len(results[s]['qualified'])}" for s in strategies)
    print(f"Summary: {len(names)} tickers, {len(data_errors)} data errors, regime {market_info['regime']}, qualified: {counts}, {duration:.1f}s")
    tracked.sort(key=lambda x: (x["signal_date"], x["strategy"], x["ticker"]), reverse=True)
    errors = {}  # stages after the screening: persistence, email_format, email_delivery
    result = {
        "generated_at": now.isoformat(timespec="seconds"),
        "as_of": as_of.isoformat(),
        "universe": {"source": source, "tickers": len(names), "data_errors": len(data_errors)},
        "market": market_info,
        "benchmarks": benchmark_info,
        "holding": holding,
        "candidates": candidates,
        "sizing": sizing,
        "backdrop": backdrop,
        "extreme_move": extreme,
        "etf_setups": None if not cfg["etf_setups"] or "breakout" not in strategies else {
            "checked": len(etf_bars), "missing": etf_missing, "rotation": etf_rotation,
            "note": "Breakout rules on sector and theme ETFs, proposed only when the rotation against SPY accelerates. The "
                    "acceleration alone did not predict the next 21 or 63 sessions in the backtest; it only filters breakouts."},
        "positions": None if not persist else {
            "open": len(statuses), "max_open_trades": sizing["max_open_trades"], "to_sell": sizing["to_sell"],
            "items": statuses, "swaps": swaps,
            "note": "Record buys and sells with f/screener/positions. Advice by the backtested exit rules from your buy price.",
        },
        "results": results,
        "tracking": None if not track_days else {
            "days": track_days,
            "holding_days": holding,
            "note": "Signals of the previous sessions, recomputed from today's data with the execution rules of the validation. "
                    "Only tickers in today's universe are tracked; one active signal per ticker and strategy. Squeeze signals are "
                    "kept only for tickers whose short float today meets squeeze_min_short_float.",
            "summary": {s: tracking_summary([x for x in tracked if x["strategy"] == s]) for s in strategies},
            "signals": tracked,
        },
        "data_errors": data_errors,
        "validation": None if not validation_days else {
            "days": validation_days,
            "holding_days": holding,
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
    if persist:
        candidates_new = [new_row(q, True, as_of.isoformat()) for s in strategies for q in results[s]["qualified"]]
        candidates_new += [new_row(x, False, as_of.isoformat()) for x in tracked]
        new_rows, skipped = select_new(stored, updates, candidates_new)
        try:  # one statement, one transaction: all rows are written or none
            written = query(WRITE_SQL, new_rows, updates, as_of.isoformat(), market_info["regime"], len(names), len(data_errors),
                            _json_safe(market_info), _json_safe(candidates))
            stats = query(STATS_SQL)
        except Exception as error:  # noqa: BLE001 - the screening result and the email still go out
            errors["persistence"] = f"{type(error).__name__}: {error}"
            print(f"Persistence failed: {errors['persistence']}")
            written, stats = [], []
        by_id = {u["id"]: u for u in updates}
        merged = [{**row, **by_id.get(row["id"], {})} for row in active]
        result["persistence"] = {
            "data_table": DATA_TABLE, "schema": "screener",
            "inserted": (written[0] if written else {}).get("inserted"), "updated": (written[0] if written else {}).get("updated"),
            "run_id": (written[0] if written else {}).get("run_id"),
            "new": [stored_view(r) for r in new_rows],
            "active": [stored_view(r) for r in merged if r["status"] in ACTIVE_STATUSES]
                      + [stored_view(r) for r in new_rows if r["status"] in ACTIVE_STATUSES],
            "closed_today": [stored_view(r) for r in merged if r["status"] in CLOSED_STATUSES],
            "data_unavailable": [stored_view(r) for r in merged if r["data_status"] == "unavailable"],
            "skipped_busy": skipped,
            "stats": stats,
            "status": "failed" if "persistence" in errors else "ok",
            "note": "live = recorded on its signal day with the live checks; not live = recomputed later from the price history.",
        }
    if email.strip():
        subject = f"Strategy screen {as_of.isoformat()}: market {market_info['regime']}, {len(candidates)} setups"
        if persist:
            subject += f", positions {len(statuses)}/{sizing['max_open_trades']}" + (f", {sizing['to_sell']} to sell" if sizing["to_sell"] else "")
        html, problem = build_email(result)
        if problem:
            errors["email_format"] = problem
            subject += " (short report)"
        if "persistence" in errors:
            html = (f"<p><b>Not saved</b>: the database write failed ({escape(errors['persistence'])}); tracked signals were not "
                    "updated. The next run retries them without duplicates.</p>") + html
        try:
            send_email(email.strip(), subject, html)
            status = "sent"
        except Exception as error:  # noqa: BLE001 - the screening result stays valid without the email
            errors["email_delivery"] = f"{type(error).__name__}: {error}"
            status = "failed"
        result["email"] = {"to": email.strip(), "subject": subject, "status": status, "short_report": bool(problem)}
    result["errors"] = errors
    for stage, text in errors.items():
        print(f"{stage} failed: {text}")
    return result
