import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "f" / "screener"))
import run_screen as rs

URL = "https://finviz.com/screener.ashx?v=171&f=cap_mega"
ROWS = [
    {"No.": "1", "Ticker": "AAA", "Price": "10.00", "ATR": "1.50", "RSI": "25.0", "Volume": "1,234,567"},
    {"No.": "2", "Ticker": "BBB", "Price": "20.00", "ATR": "2.00", "RSI": "70.0", "Volume": "500"},
    {"No.": "3", "Ticker": "CCC", "Price": "5.00", "ATR": "-", "RSI": "20.0", "Volume": "10"},
]


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(rs, "send_email", lambda to, subject, html, csv_text: sent.append((to, subject, html, csv_text)))
    return sent


@pytest.mark.parametrize(
    "text, expected",
    [("12.5%", 12.5), ("1,234.5", 1234.5), ("-", None), ("1.2B", 1.2e9), ("-0.30%", -0.3), ("AAPL", None), ("", None),
     ("inf", None), ("nan", None), ("1e999", None), (" 12.5% ", 12.5)],
)
def test_parse_number(text, expected):
    assert rs.parse_number(text) == expected


def test_compile_rule_arithmetic():
    rule = rs.compile_rule("[Price] - 2 * [ATR]", ["Price", "ATR"])
    assert rule({"Price": 10.0, "ATR": 1.5}) == 7.0


def test_compile_rule_condition_and_functions():
    rule = rs.compile_rule("[RSI] < 30 and max([Price], 5) > 5", ["RSI", "Price"])
    assert rule({"RSI": 25.0, "Price": 10.0}) is True
    assert rule({"RSI": 35.0, "Price": 10.0}) is False


def test_round_accepts_float_digits():
    assert rs.compile_rule("round([Price], 1.0)", ["Price"])({"Price": 12.34}) == 12.3


def test_round_digits_are_capped():
    assert rs.compile_rule("round(123456789012, -11) + 0 * [Price]", ["Price"])({"Price": 1.0}) == 120000000000
    assert rs.compile_rule("round(5, -30000000) + 0 * [Price]", ["Price"])({"Price": 1.0}) == 0


@pytest.mark.parametrize(
    "rule, expected",
    [
        ("+[Price] - -1", 11.0),
        ("[Price] / 4 * 2", 5.0),
        ("not [Price] < 5", True),
        ("[Price] <= 10 and [Price] >= 10", True),
        ("[Price] == 10 or [Price] != 10", True),
        ("[Price] > 11 or [Price] < 9", False),
        ("abs(-[Price]) + min([Price], 3) + max([Price], 3) + round([Price])", 33.0),
    ],
)
def test_compile_rule_every_allowed_operator(rule, expected):
    assert rs.compile_rule(rule, ["Price"])({"Price": 10.0}) == expected


def test_compile_rule_column_with_space():
    assert rs.compile_rule("[52W High] * 1", ["52W High"])({"52W High": -3.5}) == -3.5


@pytest.mark.parametrize(
    "bad",
    [
        "__import__('os').system('id')",
        "[Price].__class__",
        "lambda: 1",
        "[Price] ** 99",
        "open('/etc/passwd')",
        "'text'",
        "min",
        "min([Price], 1) + min",
        "[Price] and max",
        "max([Price], default=1)",
        "(lambda: 1)()",
        "max([Price])",
        "min([Price])",
        "min()",
        "abs(1, 2)",
        "round([Price], 1, 2)",
        "[Price] * 1e999",
        "1 +" * 200 + " 1",
        "-" * 498 + "1",
        "[Price] +",
    ],
)
def test_compile_rule_rejects(bad):
    with pytest.raises(ValueError, match="rule"):
        rs.compile_rule(bad, ["Price"])


def test_compile_rule_unknown_column_names_rule_and_columns():
    with pytest.raises(ValueError, match=r"NoSuchColumn.*Price"):
        rs.compile_rule("[NoSuchColumn] * 2", ["Price"])


def test_screen_skips_missing_values_with_reason():
    matches, skipped = rs.screen(ROWS, {"entry": "[Price]", "stop": "[Price] - 2 * [ATR]"})
    assert [m["Ticker"] for m in matches] == ["AAA", "BBB"]
    assert skipped == [("CCC", "missing ATR")]


@pytest.mark.parametrize(
    "rules",
    [
        {"entry": "[Price] * 1" + "0" * 400},
        {"entry": "round([Price], [Price] * 1e308 * 10)"},
        {"entry": "min([Price] * 1e308 * 10, 5)"},
        {"entry": "[Price]", "condition": "[Price] * 1e308 * 10 > 0"},
        {"entry": "[Price]", "condition": "[Price] * 1e308 * 10 - [Price] * 1e308 * 10 < 1"},
        {"entry": "[Price] + 1 / ([ATR] * 1e308 * 10)"},
        {"entry": "[Price]", "condition": "1" + "0" * 400 + " * 10 > 0"},
    ],
)
def test_screen_oversized_numbers_are_skipped(rules):
    matches, skipped = rs.screen([{"Ticker": "BIG", "Price": "10", "ATR": "2"}], {"stop": "[Price]", **rules})
    assert matches == [] and skipped == [("BIG", "number too large")]


def test_main_blank_optional_rules_are_ignored(monkeypatch, outbox):
    monkeypatch.setattr(rs, "fetch_rows", lambda url: ROWS[:1])
    assert [m["entry"] for m in rs.main(URL, "user@example.com", " [Price] ", "[Price]", "  ", " ")] == [10.0]
    assert [m["entry"] for m in rs.main(URL, "user@example.com", "[Price]", "[Price]", None, None)] == [10.0]


def test_screen_infinite_level_is_skipped():
    rows = [{"Ticker": "BIG", "Price": "1e308"}]
    matches, skipped = rs.screen(rows, {"entry": "[Price] * 10", "stop": "[Price]"})
    assert matches == [] and skipped == [("BIG", "number too large")]


def test_fetch_rows_returns_finviz_rows(monkeypatch):
    monkeypatch.setattr(rs.Screener, "init_from_url", lambda url, rows=None: iter(ROWS[:2]))
    assert rs.fetch_rows(URL) == ROWS[:2]


def test_fetch_rows_empty_finviz_screen(monkeypatch):
    def no_results(url, rows=None):
        raise rs.NoResults("no results")

    monkeypatch.setattr(rs.Screener, "init_from_url", no_results)
    assert rs.fetch_rows(URL) == []


@pytest.mark.parametrize(
    "rules, field",
    [({"entry": "x" * 501, "stop": "[Price]"}, "entry"), ({"entry": "[Price]", "stop": "[Price]\x00"}, "stop"),
     ({"entry": "[Price]", "stop": "[Price]", "condition": "min()"}, "condition")],
)
def test_main_rule_errors_name_the_field(monkeypatch, outbox, rules, field):
    monkeypatch.setattr(rs, "fetch_rows", lambda url: pytest.fail("scraped despite a bad rule"))
    with pytest.raises(ValueError, match=f"^{field} rule: "):
        rs.main(URL, "user@example.com", **rules)
    assert f"{field} rule" in outbox[0][2]


@pytest.mark.parametrize("entry, stop", [(" ", "[Price]"), ("[Price]", "")])
def test_main_requires_entry_and_stop(monkeypatch, outbox, entry, stop):
    monkeypatch.setattr(rs, "fetch_rows", lambda url: pytest.fail("scraped without an entry or stop rule"))
    with pytest.raises(ValueError, match="required"):
        rs.main(URL, "user@example.com", entry, stop)
    assert "failed" in outbox[0][1]


def test_screen_rounds_levels_to_two_decimals():
    matches, _ = rs.screen([{"Ticker": "AAA", "Price": "10"}], {"entry": "[Price] / 3", "stop": "[Price] * 2 / 3"})
    assert (matches[0]["entry"], matches[0]["stop"]) == (3.33, 6.67)


def test_screen_division_by_zero_is_skipped():
    rows = [{"Ticker": "ZZZ", "Price": "10", "ATR": "0"}]
    matches, skipped = rs.screen(rows, {"entry": "[Price] / [ATR]", "stop": "[Price]"})
    assert matches == [] and skipped == [("ZZZ", "division by zero")]


def test_ci_pins_match_windmill_requirements_header():
    script = (Path(rs.__file__)).read_text().splitlines()
    header = {line.lstrip("#") for line in script[1 : script.index("")]}
    pins = set((Path(__file__).resolve().parents[1] / "requirements-test.txt").read_text().split())
    assert header <= pins


def test_main_condition_levels_and_email(monkeypatch, outbox):
    monkeypatch.setattr(rs, "fetch_rows", lambda url: ROWS[:2])
    result = rs.main(URL, "user@example.com", "[Price] * 1.01", "[Price] - 2 * [ATR]", "[Price] + 3 * [ATR]", "[RSI] < 30")
    assert result == [
        {"Ticker": "AAA", "Price": "10.00", "ATR": "1.50", "RSI": "25.0", "Volume": "1,234,567", "entry": 10.1, "stop": 7.0, "target": 14.5}
    ]
    (to, subject, html, csv_text), = outbox
    assert to == "user@example.com"
    assert "1 match" in subject and "1 matches" not in subject
    assert "AAA" in html and "BBB" not in html
    assert csv_text.splitlines()[0] == "Ticker,Price,ATR,RSI,Volume,entry,stop,target"


def test_main_zero_matches_still_emails(monkeypatch, outbox):
    monkeypatch.setattr(rs, "fetch_rows", lambda url: ROWS[:2])
    assert rs.main(URL, "user@example.com", "[Price]", "[Price]", condition="[RSI] < 0") == []
    assert "0 matches" in outbox[0][1]
    assert "No stock matched today." in outbox[0][2]


def test_main_email_lists_skipped_rows(monkeypatch, outbox):
    monkeypatch.setattr(rs, "fetch_rows", lambda url: ROWS)
    rs.main(URL, "user@example.com", "[Price]", "[Price] - [ATR]")
    assert "<li>CCC: missing ATR</li>" in outbox[0][2]


def test_main_no_rows_from_finviz_still_emails(monkeypatch, outbox):
    monkeypatch.setattr(rs, "fetch_rows", lambda url: [])
    assert rs.main(URL, "user@example.com", "[Price]", "[Price] - [ATR]") == []
    assert "0 matches" in outbox[0][1]


def test_main_bad_rule_fails_before_scraping_and_emails(monkeypatch, outbox):
    def no_fetch(url):
        raise AssertionError("scraped despite a bad rule")

    monkeypatch.setattr(rs, "fetch_rows", no_fetch)
    with pytest.raises(ValueError, match="__import__"):
        rs.main(URL, "user@example.com", "__import__('os').system('id')", "[Price]")
    assert "failed" in outbox[0][1]


def test_main_scrape_failure_emails_and_reraises(monkeypatch, outbox):
    def broken(url):
        raise RuntimeError("finviz changed <its> page")

    monkeypatch.setattr(rs, "fetch_rows", broken)
    with pytest.raises(RuntimeError):
        rs.main(URL, "user@example.com", "[Price]", "[Price]")
    assert "failed" in outbox[0][1]
    assert "&lt;its&gt;" in outbox[0][2]


@pytest.mark.parametrize("url", [URL, "https://www.finviz.com/screener.ashx?v=171&f=cap_mega"])
def test_main_accepts_finviz_urls(monkeypatch, outbox, url):
    monkeypatch.setattr(rs, "fetch_rows", lambda u: ROWS[:1])
    assert len(rs.main(url, "user@example.com", "[Price]", "[Price]")) == 1


@pytest.mark.parametrize("url", ["http://10.0.0.1/", "https://evil.example/screener.ashx", "file:///etc/passwd", "https://elite.finviz.com/screener.ashx?v=171", "http://finviz.com/screener.ashx?v=171"])
def test_main_rejects_non_finviz_url(monkeypatch, outbox, url):
    monkeypatch.setattr(rs, "fetch_rows", lambda u: pytest.fail("accepted a non-Finviz URL"))
    with pytest.raises(ValueError, match="Finviz"):
        rs.main(url, "user@example.com", "[Price]", "[Price]")
