"""
data/universe_loader.py -- AlphaBot tradeable universe loader (Wikipedia-only)

Smart parser: tries all wikitables on each page, tries multiple ticker columns,
and picks whichever combination yields the most valid tickers. Handles the fact
that FTSE/ASX Wikipedia pages use different table layouts than US pages.

Dedupes before DB insert to avoid UNIQUE constraint failures when the same
ticker appears in multiple indices (AAPL in SP500 + NASDAQ100).
"""
import logging
import sqlite3
import time
import json
from urllib.request import Request, urlopen
from html.parser import HTMLParser
import re

DB_PATH = "/home/alphabot/app/alphabot.db"
USER_AGENT = "Mozilla/5.0 AlphaBot/1.0 (personal trading bot)"
HTTP_TIMEOUT = 30
HTTP_DELAY = 0.5


def _ascii_safe(s):
    try:
        return str(s).encode("ascii", errors="replace").decode("ascii")
    except Exception:
        return "<unencodable>"


log = logging.getLogger("universe_loader")
if not log.handlers:
    import sys
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] [UNIVERSE] %(message)s'))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def _http_get(url):
    time.sleep(HTTP_DELAY)
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    })
    with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ==============================================================
# ALL-WIKITABLES PARSER
# ==============================================================
class AllWikitablesParser(HTMLParser):
    """Parse every <table class='wikitable'> on the page. Handles nested tables.
    Each result is a list of rows; each row is a list of cell strings."""
    def __init__(self):
        super().__init__()
        self.in_wikitable = False
        self.nest_depth = 0  # nested tables inside a wikitable
        self.in_row = False
        self.in_cell = False
        self.current_row = []
        self.current_cell = []
        self.current_table = []
        self.tables = []  # list of tables

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "table":
            classes = attrs_dict.get("class", "")
            if "wikitable" in classes and not self.in_wikitable:
                self.in_wikitable = True
                self.nest_depth = 0
                self.current_table = []
            elif self.in_wikitable:
                self.nest_depth += 1
        elif self.in_wikitable and self.nest_depth == 0:
            if tag == "tr":
                self.in_row = True
                self.current_row = []
            elif tag in ("td", "th") and self.in_row:
                self.in_cell = True
                self.current_cell = []

    def handle_endtag(self, tag):
        if tag == "table" and self.in_wikitable:
            if self.nest_depth > 0:
                self.nest_depth -= 1
            else:
                self.in_wikitable = False
                if self.current_table:
                    self.tables.append(self.current_table)
        elif self.in_wikitable and self.nest_depth == 0:
            if tag == "tr" and self.in_row:
                if self.current_row:
                    self.current_table.append(self.current_row)
                self.in_row = False
            elif tag in ("td", "th") and self.in_cell:
                text = "".join(self.current_cell).strip()
                text = re.sub(r"\s+", " ", text)
                self.current_row.append(text)
                self.in_cell = False

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell.append(data)


def _normalize_ticker(raw):
    """Normalize a raw cell value to a clean ticker or None."""
    if not raw:
        return None
    t = _ascii_safe(raw).strip().upper()
    # Strip exchange suffixes common on LSE/ASX Wikipedia pages
    # e.g. "BHP.L" -> "BHP", "BHP.AX" -> "BHP"
    if t.endswith(".L") or t.endswith(".AX"):
        t = t.rsplit(".", 1)[0]
    # Remove any trailing bracket notes like "ABC[1]"
    t = re.sub(r"\[.*?\]", "", t).strip()
    # Remove trailing whitespace/non-alphanumeric
    t = t.strip(" .-")
    if not t or len(t) > 6:
        return None
    if t.lower() in ("ticker", "symbol", "code", "epic", "asxcode", "ref", "company", "name"):
        return None
    # Must be at least one uppercase letter, allow digits and internal dots (BRK.B)
    if not re.match(r"^[A-Z][A-Z0-9.]*$", t):
        return None
    return t


def _score_table_column(table, col_idx):
    """Count how many rows have a valid-looking ticker in this column."""
    if not table or len(table) < 5:  # too small to be the right table
        return 0
    valid = 0
    for row in table:
        if col_idx < len(row):
            if _normalize_ticker(row[col_idx]):
                valid += 1
    return valid


def _extract_tickers_from_page(html, name_col_offset=1):
    """Find the best table + column combo, return (ticker, name) list.
    name_col_offset says 'name is usually N columns to the right of ticker'."""
    parser = AllWikitablesParser()
    parser.feed(html)
    if not parser.tables:
        return []

    best_score = 0
    best_combo = None  # (table_idx, ticker_col)
    for t_idx, table in enumerate(parser.tables):
        for col in range(min(4, max(len(r) for r in table) if table else 0)):
            score = _score_table_column(table, col)
            if score > best_score:
                best_score = score
                best_combo = (t_idx, col)

    if not best_combo or best_score < 5:
        return []

    t_idx, tkr_col = best_combo
    table = parser.tables[t_idx]
    name_col = tkr_col + name_col_offset
    # If name_col out of range, try tkr_col - 1
    if name_col >= max(len(r) for r in table):
        name_col = max(0, tkr_col - 1)

    results = []
    for row in table:
        if tkr_col >= len(row):
            continue
        tkr = _normalize_ticker(row[tkr_col])
        if not tkr:
            continue
        name = ""
        if name_col < len(row) and name_col != tkr_col:
            name = _ascii_safe(row[name_col])[:80]
        results.append((tkr, name))
    return results


# Index sources: (url, name_col_offset, exchange)
WIKI_INDICES = {
    "SP500":     ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", 1, "US"),
    "SP400":     ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", 1, "US"),
    "SP600":     ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", 1, "US"),
    "NASDAQ100": ("https://en.wikipedia.org/wiki/Nasdaq-100", -1, "US"),
    "DJIA":      ("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average", -2, "US"),
    "FTSE100":   ("https://en.wikipedia.org/wiki/FTSE_100_Index", 1, "LSE"),
    "FTSE250":   ("https://en.wikipedia.org/wiki/FTSE_250_Index", 1, "LSE"),
    "ASX200":    ("https://en.wikipedia.org/wiki/S%26P/ASX_200", 1, "ASX"),
    "ASX300":    ("https://en.wikipedia.org/wiki/S%26P/ASX_300", 1, "ASX"),
}


def fetch_index_members(index_name):
    if index_name not in WIKI_INDICES:
        raise ValueError("Unknown index: %s" % index_name)
    url, name_offset, _ = WIKI_INDICES[index_name]
    try:
        html = _http_get(url)
    except Exception as e:
        log.error("%s: fetch failed: %s", index_name, _ascii_safe(e))
        return []
    try:
        tickers = _extract_tickers_from_page(html, name_col_offset=name_offset)
    except Exception as e:
        log.error("%s: parse failed: %s", index_name, _ascii_safe(e))
        return []
    log.info("%s: %d members fetched", index_name, len(tickers))
    return tickers


def _get_conn():
    return sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)


def ensure_schema():
    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS universe (
            symbol       TEXT NOT NULL,
            exchange     TEXT NOT NULL,
            currency     TEXT,
            name         TEXT,
            updated_at   TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (symbol, exchange)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS universe_indices (
            symbol       TEXT NOT NULL,
            exchange     TEXT NOT NULL,
            index_name   TEXT NOT NULL,
            PRIMARY KEY (symbol, exchange, index_name)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_universe_exchange ON universe(exchange)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_universe_indices_index ON universe_indices(index_name)")
    conn.commit()
    conn.close()


def refresh_universe():
    t0 = time.time()
    ensure_schema()

    result = {"ok": False, "counts": {}, "errors": [], "took_seconds": 0.0}

    universe_map = {}       # (symbol, exchange) -> (currency, name)
    index_memberships = set()  # set of (symbol, exchange, index_name) -- dedup built-in

    per_index_counts = {}
    for idx_name in WIKI_INDICES.keys():
        _, _, exch = WIKI_INDICES[idx_name]
        currency = {"US": "USD", "LSE": "GBP", "ASX": "AUD"}.get(exch, "USD")
        members = fetch_index_members(idx_name)
        per_index_counts[idx_name] = len(members)
        if not members:
            log.warning("%s: returned 0 members, skipping", idx_name)
            continue
        for tkr, name in members:
            key = (tkr, exch)
            if key not in universe_map:
                universe_map[key] = (currency, name)
            index_memberships.add((tkr, exch, idx_name))

    result["counts"]["per_index"] = per_index_counts
    result["counts"]["universe"] = len(universe_map)
    result["counts"]["indices"] = len(index_memberships)

    # Sanity check: SP500 should be ~500
    sp500_count = per_index_counts.get("SP500", 0)
    if sp500_count < 400:
        msg = "SP500 returned only %d, aborting to avoid wiping good data" % sp500_count
        log.error(msg)
        result["errors"].append(msg)
        result["took_seconds"] = round(time.time() - t0, 1)
        return result

    if len(universe_map) < 500:
        msg = "Total universe only %d symbols, aborting" % len(universe_map)
        log.error(msg)
        result["errors"].append(msg)
        result["took_seconds"] = round(time.time() - t0, 1)
        return result

    # Atomic write
    conn = _get_conn()
    c = conn.cursor()
    try:
        c.execute("BEGIN")
        c.execute("DELETE FROM universe_indices")
        c.execute("DELETE FROM universe")
        universe_rows = [(sym, exch, cur, name) for (sym, exch), (cur, name) in universe_map.items()]
        c.executemany(
            "INSERT INTO universe (symbol, exchange, currency, name, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            universe_rows
        )
        index_rows = list(index_memberships)
        c.executemany(
            "INSERT INTO universe_indices (symbol, exchange, index_name) VALUES (?, ?, ?)",
            index_rows
        )
        conn.commit()
        log.info("Universe written: %d symbols, %d index memberships",
                 len(universe_rows), len(index_rows))
    except Exception as e:
        conn.rollback()
        err = _ascii_safe(e)
        log.error("DB write failed: %s", err)
        result["errors"].append("DB: %s" % err)
        result["took_seconds"] = round(time.time() - t0, 1)
        conn.close()
        return result
    conn.close()

    result["ok"] = True
    result["took_seconds"] = round(time.time() - t0, 1)
    return result


if __name__ == "__main__":
    import sys
    print("Refreshing universe from Wikipedia index constituents...")
    print()
    r = refresh_universe()
    print(json.dumps(r, indent=2, default=str))
    sys.exit(0 if r["ok"] else 1)
