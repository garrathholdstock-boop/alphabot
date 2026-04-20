"""
data/universe_loader.py -- AlphaBot tradeable universe loader (Wikipedia-only edition)

IBKR's product directory moved to JavaScript rendering (no HTML tables),
so we rely solely on Wikipedia index constituents.

Indices fetched:
  US:   SP500, SP400, SP600, NASDAQ100, DJIA
  UK:   FTSE100, FTSE250
  AUS:  ASX200, ASX300

Every ticker in these indices is tradeable on IBKR (validated organically by
bot runtime; any Error 200 symbols get stripped in subsequent refresh).

Writes to `universe` and `universe_indices` tables.
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


# ==============================================================
# HTTP
# ==============================================================
def _http_get(url):
    time.sleep(HTTP_DELAY)
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    })
    with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ==============================================================
# WIKIPEDIA PARSER
# ==============================================================
class WikiTableParser(HTMLParser):
    """Extract rows from <table class="wikitable ...">, handling nested tables."""
    def __init__(self, ticker_col=0, name_col=1):
        super().__init__()
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.current_row = []
        self.current_cell = []
        self.rows = []
        self.ticker_col = ticker_col
        self.name_col = name_col
        self._table_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "table":
            classes = attrs_dict.get("class", "")
            if "wikitable" in classes and not self.in_table:
                self.in_table = True
                self._table_depth = 0
            elif self.in_table:
                self._table_depth += 1
        elif self.in_table:
            if tag == "tr" and self._table_depth == 0:
                self.in_row = True
                self.current_row = []
            elif tag in ("td", "th") and self.in_row:
                self.in_cell = True
                self.current_cell = []

    def handle_endtag(self, tag):
        if tag == "table" and self.in_table:
            if self._table_depth > 0:
                self._table_depth -= 1
            else:
                self.in_table = False
        elif tag == "tr" and self.in_row:
            if self.current_row:
                self.rows.append(self.current_row)
            self.in_row = False
        elif tag in ("td", "th") and self.in_cell:
            text = "".join(self.current_cell).strip()
            text = re.sub(r"\s+", " ", text)
            self.current_row.append(text)
            self.in_cell = False

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell.append(data)

    def get_tickers(self):
        """Return list of (ticker, name) tuples. Filters junk."""
        out = []
        for r in self.rows:
            if len(r) <= max(self.ticker_col, self.name_col):
                continue
            tkr_raw = r[self.ticker_col].strip()
            name = _ascii_safe(r[self.name_col].strip() if self.name_col < len(r) else "")
            # Normalize ticker: ASCII uppercase, strip exchange suffixes common on Wikipedia
            tkr = _ascii_safe(tkr_raw).upper().strip()
            # Remove trailing exchange suffix like "BHP.L" -> "BHP" (for LSE)
            # and "BHP.AX" -> "BHP" (for ASX)
            # But preserve genuine dots like "BRK.B" (class B shares)
            if tkr.endswith(".L") or tkr.endswith(".AX"):
                tkr = tkr.rsplit(".", 1)[0]
            # Filters
            if not tkr or len(tkr) > 6:
                continue
            if tkr.lower() in ("ticker", "symbol", "code", "epic", "asxcode", "ref"):
                continue
            # Must be uppercase letters with optional dot (for BRK.B, etc)
            if not re.match(r"^[A-Z][A-Z0-9.]*[A-Z0-9]$", tkr) and len(tkr) > 1:
                continue
            if not re.match(r"^[A-Z]$", tkr) and len(tkr) == 1:
                continue
            out.append((tkr, name))
        return out


# Index sources: (url, ticker_col, name_col, exchange)
WIKI_INDICES = {
    "SP500":     ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", 0, 1, "NYSE_NASDAQ"),
    "SP400":     ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", 0, 1, "NYSE_NASDAQ"),
    "SP600":     ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", 0, 1, "NYSE_NASDAQ"),
    "NASDAQ100": ("https://en.wikipedia.org/wiki/Nasdaq-100", 1, 0, "NASDAQ"),
    "DJIA":      ("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average", 2, 0, "NYSE_NASDAQ"),
    "FTSE100":   ("https://en.wikipedia.org/wiki/FTSE_100_Index", 0, 1, "LSE"),
    "FTSE250":   ("https://en.wikipedia.org/wiki/FTSE_250_Index", 0, 1, "LSE"),
    "ASX200":    ("https://en.wikipedia.org/wiki/S%26P/ASX_200", 0, 1, "ASX"),
    "ASX300":    ("https://en.wikipedia.org/wiki/S%26P/ASX_300", 0, 1, "ASX"),
}


def fetch_index_members(index_name):
    """Return list of (ticker, name) from Wikipedia. ASCII-safe errors."""
    if index_name not in WIKI_INDICES:
        raise ValueError("Unknown index: %s" % index_name)
    url, tkr_col, name_col, _ = WIKI_INDICES[index_name]
    try:
        html = _http_get(url)
    except Exception as e:
        log.error("%s: fetch failed: %s", index_name, _ascii_safe(e))
        return []
    parser = WikiTableParser(ticker_col=tkr_col, name_col=name_col)
    try:
        parser.feed(html)
    except Exception as e:
        log.error("%s: parse failed: %s", index_name, _ascii_safe(e))
        return []
    tickers = parser.get_tickers()
    log.info("%s: %d members fetched", index_name, len(tickers))
    return tickers


# ==============================================================
# DB
# ==============================================================
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
    """
    Fetch Wikipedia indices, write to universe + universe_indices tables.
    Each index_name maps to a specific exchange (US indices -> NYSE for now,
    since we pick watchlists by index not by exchange).
    """
    t0 = time.time()
    ensure_schema()

    result = {"ok": False, "counts": {}, "errors": [], "took_seconds": 0.0}

    # Exchange resolution: US indices use "US" as a virtual exchange
    # (main.py doesn't care which US exchange -- it routes via SMART anyway).
    # For LSE and ASX, tickers are ambiguous without suffix, we use the exchange
    # as the lookup for IBKR contract routing.
    def _exchange_for(index_name):
        _, _, _, exch = WIKI_INDICES[index_name]
        if exch == "NYSE_NASDAQ":
            return "US"  # IBKR SMART handles this
        if exch == "NASDAQ":
            return "US"
        return exch  # LSE, ASX

    universe_rows = []  # (symbol, exchange, currency, name)
    index_rows = []     # (symbol, exchange, index_name)
    seen_universe = set()  # (symbol, exchange) keys

    for idx_name in WIKI_INDICES.keys():
        exch = _exchange_for(idx_name)
        currency = {"US": "USD", "LSE": "GBP", "ASX": "AUD"}.get(exch, "USD")
        members = fetch_index_members(idx_name)
        if not members:
            log.warning("%s: returned 0 members, skipping", idx_name)
            continue
        for tkr, name in members:
            key = (tkr, exch)
            if key not in seen_universe:
                seen_universe.add(key)
                universe_rows.append((tkr, exch, currency, name))
            index_rows.append((tkr, exch, idx_name))

    result["counts"]["universe"] = len(universe_rows)
    result["counts"]["indices"] = len(index_rows)
    result["counts"]["per_index"] = {}
    # Count per-index for reporting
    for idx_name in WIKI_INDICES.keys():
        count = sum(1 for r in index_rows if r[2] == idx_name)
        result["counts"]["per_index"][idx_name] = count

    # Sanity check: SP500 alone should give us >400 tickers
    sp500_count = result["counts"]["per_index"].get("SP500", 0)
    if sp500_count < 400:
        msg = "SP500 returned only %d tickers, aborting to avoid wiping good data" % sp500_count
        log.error(msg)
        result["errors"].append(msg)
        result["took_seconds"] = round(time.time() - t0, 1)
        return result

    if len(universe_rows) < 500:
        msg = "Total universe only %d symbols, aborting" % len(universe_rows)
        log.error(msg)
        result["errors"].append(msg)
        result["took_seconds"] = round(time.time() - t0, 1)
        return result

    # Atomic DB swap
    conn = _get_conn()
    c = conn.cursor()
    try:
        c.execute("BEGIN")
        c.execute("DELETE FROM universe_indices")
        c.execute("DELETE FROM universe")
        c.executemany(
            "INSERT INTO universe (symbol, exchange, currency, name, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            universe_rows
        )
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
    print("This takes 30-60 seconds.")
    print()
    result = refresh_universe()
    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result["ok"] else 1)
