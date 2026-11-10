#requirements:
#finviz==2.1.0
#wmill==1.866.0

import ast
import csv
import io
import math
import re
import smtplib
from datetime import date
from email.message import EmailMessage
from html import escape
from urllib.parse import urlparse

import wmill
from finviz.screener import NoResults, Screener

SMTP_RESOURCE = "f/screener/smtp"
FINVIZ_HOSTS = {"finviz.com", "www.finviz.com"}
COLUMN = re.compile(r"\[([^\[\]]+)\]")
MAX_RULE_LENGTH = 500
SUFFIXES = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
MAX_ROUND_DIGITS = 10
FUNCTIONS = {
    "min": min,
    "max": max,
    "abs": abs,
    # A huge negative ndigits keeps the worker busy for minutes.
    "round": lambda number, digits=0: round(number, max(-MAX_ROUND_DIGITS, int(digits))),
}
ARITY = {"min": range(2, MAX_RULE_LENGTH), "max": range(2, MAX_RULE_LENGTH), "abs": range(1, 2), "round": range(1, 3)}
ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Constant, ast.Name, ast.Call, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or,
    ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
)


def parse_number(text: str) -> float | None:
    value = text.strip().replace(",", "").removesuffix("%")
    multiplier = SUFFIXES.get(value[-1:], 1)
    if multiplier != 1:
        value = value[:-1]
    try:
        number = float(value) * multiplier
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def finite(number):
    if not math.isfinite(number):
        raise OverflowError("number too large")
    return number


class CheckArithmetic(ast.NodeTransformer):
    def visit_BinOp(self, node):
        self.generic_visit(node)
        return ast.Call(func=ast.Name(id="finite", ctx=ast.Load()), args=[node], keywords=[])


def compile_rule(rule: str, columns: list[str] | None = None):
    if len(rule) > MAX_RULE_LENGTH:
        raise ValueError(f"rule is longer than {MAX_RULE_LENGTH} characters")
    variables = {}

    def to_variable(match):
        column = match.group(1)
        if columns is not None and column not in columns:
            raise ValueError(f"rule {rule!r}: unknown column [{column}]. Columns: {', '.join(columns)}")
        return variables.setdefault(column, f"column_{len(variables)}")

    try:
        tree = ast.parse(COLUMN.sub(to_variable, rule), mode="eval")
    except SyntaxError as error:
        raise ValueError(f"rule {rule!r}: {error.msg}") from None
    function_names = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise ValueError(f"rule {rule!r}: {type(node).__name__} is not allowed")
        if isinstance(node, ast.Constant) and (type(node.value) not in (int, float) or (type(node.value) is float and not math.isfinite(node.value))):
            raise ValueError(f"rule {rule!r}: only numbers are allowed, not {node.value!r}")
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS):
            raise ValueError(f"rule {rule!r}: only {', '.join(FUNCTIONS)} can be called")
        if isinstance(node, ast.Call) and len(node.args) not in ARITY[node.func.id]:
            raise ValueError(f"rule {rule!r}: wrong number of values for {node.func.id}()")
        if isinstance(node, ast.Name) and node.id not in variables.values() and id(node) not in function_names:
            raise ValueError(f"rule {rule!r}: unknown name {node.id!r}, write columns as [Name]")
    try:
        code = compile(ast.fix_missing_locations(CheckArithmetic().visit(tree)), "<rule>", "eval")
    except RecursionError:
        raise ValueError(f"rule {rule!r}: nested too deeply") from None

    def evaluate(values):
        return eval(code, {"__builtins__": {}, "finite": finite, **FUNCTIONS}, {v: values[c] for c, v in variables.items()})

    evaluate.columns = list(variables)
    return evaluate


def screen(rows, rules):
    if not rows:
        return [], []
    compiled = {name: compile_rule(rule, list(rows[0])) for name, rule in rules.items() if rule}
    used = {column for rule in compiled.values() for column in rule.columns}
    matches, skipped = [], []
    for row in rows:
        ticker = row["Ticker"]
        values = {column: parse_number(row[column]) for column in used}
        missing = sorted(column for column in used if values[column] is None)
        if missing:
            skipped.append((ticker, "missing " + ", ".join(missing)))
            continue
        try:
            if "condition" in compiled and not compiled["condition"](values):
                continue
            levels = {name: round(rule(values), 2) for name, rule in compiled.items() if name != "condition"}
        except ZeroDivisionError:
            skipped.append((ticker, "division by zero"))
            continue
        except OverflowError:
            skipped.append((ticker, "number too large"))
            continue
        matches.append({**{k: v for k, v in row.items() if k != "No."}, **levels})
    return matches, skipped


def fetch_rows(finviz_url):
    try:
        return list(Screener.init_from_url(finviz_url))
    except NoResults:
        return []


def send_email(to, subject, html, csv_text):
    smtp = wmill.get_resource(SMTP_RESOURCE)
    message = EmailMessage()
    message["From"] = smtp.get("from") or smtp["user"]
    message["To"] = to
    message["Subject"] = subject
    message.set_content(subject)
    message.add_alternative(html, subtype="html")
    if csv_text is not None:
        message.add_attachment(csv_text.encode(), maintype="text", subtype="csv", filename="screen.csv")
    with smtplib.SMTP(smtp["host"], int(smtp["port"]), timeout=30) as server:
        server.starttls()
        server.login(smtp["user"], smtp["password"])
        server.send_message(message)


def to_html(matches, skipped):
    parts = []
    if matches:
        header = "".join(f"<th>{escape(k)}</th>" for k in matches[0])
        body = "".join("<tr>" + "".join(f"<td>{escape(str(v))}</td>" for v in m.values()) + "</tr>" for m in matches)
        parts.append(f"<table border='1' cellpadding='4' cellspacing='0'><tr>{header}</tr>{body}</table>")
    else:
        parts.append("<p>No stock matched today.</p>")
    if skipped:
        items = "".join(f"<li>{escape(t)}: {escape(r)}</li>" for t, r in skipped)
        parts.append(f"<p>Skipped:</p><ul>{items}</ul>")
    return "".join(parts)


def to_csv(matches):
    buffer = io.StringIO()
    if matches:
        writer = csv.DictWriter(buffer, fieldnames=list(matches[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(matches)
    return buffer.getvalue()


def main(finviz_url: str, email: str, entry: str, stop: str, target: str = "", condition: str = "") -> list[dict]:
    try:
        rules = {name: (rule or "").strip() for name, rule in
                 {"entry": entry, "stop": stop, "target": target, "condition": condition}.items()}
        url = urlparse(finviz_url)
        if url.scheme != "https" or url.hostname not in FINVIZ_HOSTS:
            raise ValueError(f"not a Finviz screener URL: {finviz_url}")
        if not rules["entry"] or not rules["stop"]:
            raise ValueError("entry and stop rules are required")
        for name, rule in rules.items():
            if rule:
                try:
                    compile_rule(rule)
                except ValueError as error:
                    raise ValueError(f"{name} rule: {error}") from None
        matches, skipped = screen(fetch_rows(finviz_url), rules)
    except Exception as error:
        send_email(email, f"Screener {date.today()}: failed", f"<p>The screen failed:</p><pre>{escape(str(error))}</pre>", None)
        raise
    count = len(matches)
    send_email(email, f"Screener {date.today()}: {count} match{'' if count == 1 else 'es'}", to_html(matches, skipped), to_csv(matches))
    return matches
