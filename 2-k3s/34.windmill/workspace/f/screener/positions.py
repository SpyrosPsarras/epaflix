#requirements:
#wmill==1.866.0

"""My positions: record the stocks you bought and sold, so the daily strategy screen follows them.

Run it with action buy when you open a trade and sell when you close it; list shows what is recorded, export emails
the full history of a book with its KPIs and a CSV for Excel or Power BI. The positions are stored in the Windmill
data table `main` (schema `screener`, table `positions`), one open position per ticker in each book. The strategy
screen with `persist` on reads the open positions of the swing book every day and reports for each one whether to hold
or sell, with the same exit rules as its backtest (stop, target, time exit, ADX momentum hold), the flags of the day,
how many of the max_open_trades slots are taken, and a swap when a stalled position could make room for a new setup.

A book keeps a strategy and its budget apart: swing is the screener's trades; pmcc and holding are kept for other
strategies and are never read by the screen. Closed positions are never deleted: a sell only fills sell_date and
sell_price.

stop and target are optional (0 = not given): the screen then takes them from its own signal on that ticker in the 14
days before the buy, else uses position_stop_atr ATR below the buy price and a position_target_r R target.
"""

import csv
import io
import re
import smtplib
from datetime import date, datetime, timezone
from email.message import EmailMessage
from html import escape

import wmill

DATA_TABLE = "main"
SMTP_RESOURCE = "f/screener/smtp"
TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
ACTIONS = ("buy", "sell", "list", "export")
BOOKS = ("swing", "pmcc", "holding")
# Kept identical to POSITIONS_SQL in strategy_screen.py (a test checks it): either script may create the table first.
# Rows written before books existed get book swing. The one-open-position index is per book and ticker.
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
ALTER TABLE screener.positions ADD COLUMN IF NOT EXISTS book text NOT NULL DEFAULT 'swing';
DROP INDEX IF EXISTS screener.positions_one_open;
CREATE UNIQUE INDEX IF NOT EXISTS positions_one_open_per_book ON screener.positions (book, ticker) WHERE closed_at IS NULL;
"""
# Every $n is cast to its column type. Windmill declares each argument's type from its Python value (wmill's
# infer_sql_type), and None, an empty stop, target or note, is declared text: PostgreSQL refuses text in a double
# precision column without a cast.
BUY_SQL = POSITIONS_SQL + """INSERT INTO screener.positions (ticker, buy_date, buy_price, size, stop, target, note, book)
VALUES ($1::text, $2::date, $3::double precision, $4::double precision, $5::double precision, $6::double precision,
        $7::text, $8::text)
ON CONFLICT (book, ticker) WHERE closed_at IS NULL DO NOTHING
RETURNING *;"""
SELL_SQL = POSITIONS_SQL + """UPDATE screener.positions SET sell_date = $2::date, sell_price = $3::double precision,
    closed_at = now()
WHERE ticker = $1::text AND book = $4::text AND closed_at IS NULL AND buy_date <= $2::date
RETURNING *;"""
LIST_SQL = POSITIONS_SQL + """SELECT * FROM screener.positions
WHERE book = $1::text AND (closed_at IS NULL OR sell_date >= current_date - 365)
ORDER BY closed_at IS NOT NULL, buy_date, id;"""
EXPORT_SQL = POSITIONS_SQL + """SELECT * FROM screener.positions WHERE book = $1::text ORDER BY buy_date, id;"""
CSV_COLUMNS = ("id", "book", "ticker", "status", "buy_date", "buy_price", "size", "invested", "stop", "target",
               "sell_date", "sell_price", "days_held", "return", "pnl", "r_multiple", "note")


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


def trade_rows(rows, budget, today):
    """One flat row per position, in budget money: invested = budget x size, pnl = invested x return (closed only)."""
    out = []
    for row in rows:
        buy_day = date.fromisoformat(str(row["buy_date"])[:10])
        sell_day = date.fromisoformat(str(row["sell_date"])[:10]) if row.get("sell_date") else None
        invested = budget * row["size"]
        ret = row["sell_price"] / row["buy_price"] - 1 if sell_day else None
        risk = row["buy_price"] - row["stop"] if row.get("stop") else None
        out.append({
            "id": row["id"], "book": row["book"], "ticker": row["ticker"], "status": "closed" if sell_day else "open",
            "buy_date": buy_day.isoformat(), "buy_price": row["buy_price"], "size": row["size"],
            "invested": round(invested, 2), "stop": row.get("stop"), "target": row.get("target"),
            "sell_date": sell_day.isoformat() if sell_day else None, "sell_price": row.get("sell_price"),
            "days_held": ((sell_day or today) - buy_day).days,
            "return": round(ret, 4) if ret is not None else None,
            "pnl": round(invested * ret, 2) if ret is not None else None,
            "r_multiple": round((row["sell_price"] - row["buy_price"]) / risk, 2) if ret is not None and risk else None,
            "note": row.get("note")})
    return out


def kpis(trades, budget):
    """Realized KPIs of the closed trades and the exposure of the open ones. Equity = budget + realized P&L in sell
    order; max drawdown is its largest fall from a peak. No fees, dividends, currency or open P&L."""
    closed = sorted((t for t in trades if t["status"] == "closed"), key=lambda t: (t["sell_date"], t["id"]))
    open_ = [t for t in trades if t["status"] == "open"]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    gross_win, gross_loss = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    equity = peak = budget
    drawdown = 0.0
    for t in closed:
        equity += t["pnl"]
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
    total = sum(t["pnl"] for t in closed)
    r_values = [t["r_multiple"] for t in closed if t["r_multiple"] is not None]

    def mean(values, digits):
        return round(sum(values) / len(values), digits) if values else None

    exposure = sum(t["size"] for t in open_)
    return {
        "budget": budget, "closed_trades": len(closed), "open_positions": len(open_),
        "winners": len(wins), "losers": len(losses),
        "win_rate": round(len(wins) / len(closed), 4) if closed else None,
        "realized_pnl": round(total, 2), "realized_return_on_budget": round(total / budget, 4),
        "equity": round(budget + total, 2),
        "average_return": mean([t["return"] for t in closed], 4),
        "average_win": mean([t["pnl"] for t in wins], 2), "average_loss": mean([t["pnl"] for t in losses], 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "expectancy_per_trade": mean([t["pnl"] for t in closed], 2),
        "average_r": mean(r_values, 2), "average_days_held": mean([t["days_held"] for t in closed], 1),
        "best_trade": max(closed, key=lambda t: t["pnl"])["ticker"] if closed else None,
        "worst_trade": min(closed, key=lambda t: t["pnl"])["ticker"] if closed else None,
        "max_drawdown": round(drawdown, 4),
        "open_exposure": round(exposure, 4), "invested_open": round(budget * exposure, 2),
        "cash_free": round(budget + total - budget * exposure, 2)}


def to_csv(trades):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(trades)
    return buffer.getvalue()


KPI_LABELS = (("closed_trades", "Closed trades", "{}"), ("open_positions", "Open positions", "{}"),
              ("win_rate", "Win rate", "{:.1%}"), ("realized_pnl", "Realized P&L", "{:+,.2f}"),
              ("realized_return_on_budget", "Return on budget", "{:+.2%}"), ("equity", "Equity", "{:,.2f}"),
              ("average_return", "Average return per trade", "{:+.2%}"), ("average_win", "Average win", "{:+,.2f}"),
              ("average_loss", "Average loss", "{:+,.2f}"), ("profit_factor", "Profit factor", "{:.2f}"),
              ("expectancy_per_trade", "Expectancy per trade", "{:+,.2f}"), ("average_r", "Average R", "{:+.2f}"),
              ("average_days_held", "Average days held", "{}"), ("best_trade", "Best trade", "{}"),
              ("worst_trade", "Worst trade", "{}"), ("max_drawdown", "Max drawdown (realized)", "{:.2%}"),
              ("invested_open", "Invested in open positions", "{:,.2f}"), ("cash_free", "Cash free", "{:,.2f}"))


def to_html(book, stats, today):
    cells = []
    for key, label, fmt in KPI_LABELS:
        value = stats[key]
        cells.append(f"<tr><td>{escape(label)}</td><td align='right'>{escape(fmt.format(value)) if value is not None else '-'}</td></tr>")
    return (f"<p><b>Book {escape(book)}</b>, budget {stats['budget']:,.2f}, as of {today}.</p>"
            f"<table border='1' cellpadding='4' cellspacing='0'>{''.join(cells)}</table>"
            "<p>P&amp;L = budget x size x (sell / buy - 1), realized trades only: no fees, dividends, currency or open P&amp;L. "
            "Every position of the book is in the attached CSV.</p>")


def send_email(to, subject, html, csv_text, filename):
    smtp = wmill.get_resource(SMTP_RESOURCE)
    message = EmailMessage()
    message["From"] = smtp.get("from") or smtp["user"]
    message["To"] = to
    message["Subject"] = subject
    message.set_content(subject)
    message.add_alternative(html, subtype="html")
    message.add_attachment(csv_text.encode(), maintype="text", subtype="csv", filename=filename)
    with smtplib.SMTP(smtp["host"], int(smtp["port"]), timeout=30) as server:
        server.starttls()
        server.login(smtp["user"], smtp["password"])
        server.send_message(message)


def main(action: str = "list", ticker: str = "", price: float = 0.0, day: str = "", size: float = 0.10, stop: float = 0.0,
         target: float = 0.0, note: str = "", book: str = "swing", email: str = "", budget: float = 10000.0) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    book = book.strip().lower()
    if book not in BOOKS:
        raise ValueError(f"book must be one of {', '.join(BOOKS)}")
    query = datatable_query()
    today = datetime.now(timezone.utc).date()
    if action == "export":
        email = email.strip()
        if not EMAIL.match(email):
            raise ValueError("export needs the email address the report goes to")
        if not budget > 0:
            raise ValueError("budget must be above 0")
        trades = trade_rows(query(EXPORT_SQL, book), float(budget), today)
        stats = kpis(trades, float(budget))
        subject = (f"Positions {book} {today}: {stats['closed_trades']} closed, {stats['open_positions']} open, "
                   f"P&L {stats['realized_pnl']:+,.2f}")
        send_email(email, subject, to_html(book, stats, today), to_csv(trades), f"positions_{book}_{today}.csv")
        return {"action": action, "book": book, "emailed_to": email, "kpis": stats, "positions": trades}
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
                     note.strip()[:500] or None, book)
        if not rows:
            raise ValueError(f"{ticker} is already open in {book}: sell it before you record a new buy")
        done = _view(rows[0])
    elif action == "sell":
        rows = query(SELL_SQL, ticker, when.isoformat(), float(price), book)
        if not rows:
            raise ValueError(f"no open position in {ticker} ({book}) bought on or before {when}")
        done = _view(rows[0])
    else:
        done = None
    listed = [_view(r) for r in query(LIST_SQL, book)]
    open_ = [r for r in listed if not r.get("sell_date")]
    return {"action": action, "book": book, "position": done, "open_count": len(open_), "open": open_,
            "closed": [r for r in listed if r.get("sell_date")],
            "note": "The daily strategy screen (persist on) reports hold or sell for each open position of the swing book."}
