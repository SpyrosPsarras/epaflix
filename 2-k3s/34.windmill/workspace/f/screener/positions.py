#requirements:
#wmill==1.866.0

"""My positions: record the stocks you bought and sold, so the daily strategy screen follows them.

Run it with action buy when you open a trade and sell when you close it; list shows what is recorded. The positions are
stored in the Windmill data table `main` (schema `screener`, table `positions`), one open position per ticker. The
strategy screen with `persist` on reads them every day and reports for each one whether to hold or sell, with the same
exit rules as its backtest (stop, target, time exit, ADX momentum hold), the flags of the day, how many of the
max_open_trades slots are taken, and a swap when a stalled position could make room for a new setup.

stop and target are optional (0 = not given): the screen then takes them from its own signal on that ticker in the 14
days before the buy, else uses position_stop_atr ATR below the buy price and a position_target_r R target.
"""

import re
from datetime import date, datetime, timezone

import wmill

DATA_TABLE = "main"
TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
ACTIONS = ("buy", "sell", "list")
# Kept identical to POSITIONS_SQL in strategy_screen.py (a test checks it): either script may create the table first.
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
BUY_SQL = POSITIONS_SQL + """INSERT INTO screener.positions (ticker, buy_date, buy_price, size, stop, target, note)
VALUES ($1, $2::date, $3, $4, $5, $6, $7)
ON CONFLICT (ticker) WHERE closed_at IS NULL DO NOTHING
RETURNING *;"""
SELL_SQL = POSITIONS_SQL + """UPDATE screener.positions SET sell_date = $2::date, sell_price = $3, closed_at = now()
WHERE ticker = $1 AND closed_at IS NULL AND buy_date <= $2::date
RETURNING *;"""
LIST_SQL = POSITIONS_SQL + """SELECT * FROM screener.positions
WHERE closed_at IS NULL OR sell_date >= current_date - 365
ORDER BY closed_at IS NOT NULL, buy_date, id;"""


def datatable_query(name=DATA_TABLE):
    """Runs one SQL text with positional $n arguments on a Windmill data table; returns the rows of the last statement."""
    def run(sql, *args):
        rows = wmill.datatable(name).query(sql, *args).fetch()
        return rows if isinstance(rows, list) else []
    return run


def _view(row):
    out = {k: (str(v)[:10] if k in ("buy_date", "sell_date") and v else v) for k, v in row.items()
           if k not in ("created_at", "closed_at")}
    if row.get("sell_price"):
        out["return"] = round(row["sell_price"] / row["buy_price"] - 1, 4)
    return out


def _day(text, today):
    if not text.strip():
        return today
    try:
        day = date.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"day must be YYYY-MM-DD, got {text!r}") from None
    if day > today:
        raise ValueError(f"day {day} is in the future")
    return day


def main(action: str = "list", ticker: str = "", price: float = 0.0, day: str = "", size: float = 0.10, stop: float = 0.0,
         target: float = 0.0, note: str = "") -> dict:
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    query = datatable_query()
    today = datetime.now(timezone.utc).date()
    if action != "list":
        ticker = ticker.strip().upper()
        if not TICKER.match(ticker):
            raise ValueError(f"not a ticker: {ticker!r}")
        if not price > 0:
            raise ValueError("price must be above 0")
        when = _day(day, today)
    if action == "buy":
        if not 0 < size <= 1:
            raise ValueError("size is the share of the budget, above 0 and at most 1 (0.10 = 10%)")
        if stop and not 0 < stop < price:
            raise ValueError(f"stop {stop} must be above 0 and below the price {price}")
        if target and not target > price:
            raise ValueError(f"target {target} must be above the price {price}")
        rows = query(BUY_SQL, ticker, when.isoformat(), float(price), float(size), float(stop) or None, float(target) or None,
                     note.strip()[:500] or None)
        if not rows:
            raise ValueError(f"{ticker} is already open: sell it before you record a new buy")
        done = _view(rows[0])
    elif action == "sell":
        rows = query(SELL_SQL, ticker, when.isoformat(), float(price))
        if not rows:
            raise ValueError(f"no open position in {ticker} bought on or before {when}")
        done = _view(rows[0])
    else:
        done = None
    listed = [_view(r) for r in query(LIST_SQL)]
    open_ = [r for r in listed if not r.get("sell_date")]
    return {"action": action, "position": done, "open_count": len(open_), "open": open_,
            "closed": [r for r in listed if r.get("sell_date")],
            "note": "The daily strategy screen (persist on) reports hold or sell for each open position."}
