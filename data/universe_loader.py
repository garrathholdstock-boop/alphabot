"""
data/universe_loader.py -- AlphaBot tradeable universe loader

Responsibilities:
  1. Fetch IBKR product directories for NYSE/NASDAQ/LSE/ASX (all tradeable common stocks)
  2. Fetch index constituent lists from Wikipedia (SP 500, Russell 2000, FTSE 100/250,
     ASX 200, etc.)
  3. Cross-reference: only store symbols in BOTH IBKR AND at least one index
  4. Write to `universe` and `universe_indices` tables
  5. Idempotent: uses atomic transaction, re-runs safely
  6. ASCII-only output and ASCII-safe error handling (server locale may be latin-1)

Usage:
    from data.universe_loader import refresh_universe
    result = refresh_universe()

Run standalone:
    python3 -m data.universe_loader
"""
import logging
import sqlite3
import time
import json
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from html.parser import HTMLParser
import re

DB_PATH = "/home/alphabot/app/alphabot.db"
USER_AGENT = "AlphaBot/1.0 (personal trading bot)"
HTTP_TIMEOUT = 30
HTTP_DELAY = 0.5  # polite: 2 req/sec max


def _ascii_safe(s):
    """Convert any string to ASCII-safe form -- strips non-ASCII chars.
    Used before logging to avoid latin-1 codec crashes on non-UTF8 VPS."""
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
# HTTP HELPERS
# ==============================================================
def _http_get(url, referer=None):
    """Polite GET with delay, UA header, timeout. Returns str or raises."""
    time.sleep(HTTP_DELAY)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    req = Request(url, headers=headers)
    with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ==============================================================
# SOURCE 1 -- WIKIPEDIA INDEX CONSTITUENTS
# ==============================================================
class WikiTableParser(HTMLParser):
    """Extract wikitable sortable rows. First <td> in each <tr> is the ticker."""
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
            if tag == "table":
                self._table_depth += 1
            elif tag == "tr" and self._table_depth == 0:
                self.in_row = True
                self.current_row = []
            elif tag in ("td", "th") and self.in_row:
                self.in_cell = True
                self.current_cell = []

    def handle_endtag(self, tag):
        if tag == "table":
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
        """Return list of (ticker, name) tuples, skipping header rows."""
        out = []
        for r in self.rows:
            if len(r) <= max(self.ticker_col, self.name_col):
                continue
            tkr = r[self.ticker_col].strip()
            name = r[self.name_col].strip() if self.name_col < len(r) else ""
            # Skip headers
            if tkr.lower() in ("ticker", "symbol", "code", "epic", "asx code", "asx code[1]"):
                continue
            # Skip empties
            if not tkr or len(tkr) > 10:
                continue
            # Normalize to ASCII uppercase
            tkr = _ascii_safe(tkr).upper().strip()
            name = _ascii_safe(name)
            if not tkr:
                continue
            out.append((tkr, name))
        return out


# Index sources: (index_name, url, ticker_col, name_col)
WIKI_INDICES = {
    "SP500":     ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", 0, 1),
    "SP400":     ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", 0, 1),
    "SP600":     ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", 0, 1),
    "NASDAQ100": ("https://en.wikipedia.org/wiki/Nasdaq-100", 1, 0),
    "FTSE100":   ("https://en.wikipedia.org/wiki/FTSE_100_Index", 0, 1),
    "FTSE250":   ("https://en.wikipedia.org/wiki/FTSE_250_Index", 0, 1),
    "ASX200":    ("https://en.wikipedia.org/wiki/S%26P/ASX_200", 0, 1),
    "ASX300":    ("https://en.wikipedia.org/wiki/S%26P/ASX_300", 0, 1),
    "DJIA":      ("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average", 2, 0),
}


def fetch_index_members(index_name):
    """Return list of (ticker, name) for a given index. ASCII-safe errors."""
    if index_name not in WIKI_INDICES:
        raise ValueError("Unknown index: %s" % index_name)
    url, tkr_col, name_col = WIKI_INDICES[index_name]
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
# SOURCE 2 -- IBKR PRODUCT DIRECTORY
# ==============================================================
class IBKRProductParser(HTMLParser):
    """Parse IBKR's STK product list table. Extract ALL cells per row as strings."""
    def __init__(self):
        super().__init__()
        self.in_row = False
        self.in_cell = False
        self.current_cell = []
        self.current_row = []
        self.rows = []
        self.in_relevant_table = False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.in_relevant_table = True
        elif tag == "tr" and self.in_relevant_table:
            self.in_row = True
            self.current_row = []
        elif tag in ("td", "th") and self.in_row:
            self.in_cell = True
            self.current_cell = []

    def handle_endtag(self, tag):
        if tag == "table":
            self.in_relevant_table = False
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


def fetch_ibkr_universe(exchange):
    """
    Fetch tradeable STK symbols from IBKR's product directory.
    Returns list of (symbol, name, currency) tuples. ASCII-safe throughout.
    """
    base = "https://www.interactivebrokers.com/en/index.php"
    results = []
    page = 1
    max_pages = 100
    empty_pages = 0
    while page <= max_pages:
        url = "%s?f=2222&exch=%s&showcategories=STK&p=&cc=&limit=100&page=%d" % (base, exchange, page)
        try:
            html = _http_get(url, referer=base)
        except Exception as e:
            log.warning("IBKR %s page %d: %s", exchange, page, _ascii_safe(e))
            empty_pages += 1
            if empty_pages >= 3:
                break
            page += 1
            continue
        parser = IBKRProductParser()
        try:
            parser.feed(html)
        except Exception as e:
            log.warning("IBKR %s page %d parse error: %s", exchange, page, _ascii_safe(e))
            page += 1
            continue

        found_this_page = 0
        for row in parser.rows:
            if len(row) < 2:
                continue
            raw_sym = _ascii_safe(row[0]).strip().upper()
            # Must look like a valid stock ticker
            if not raw_sym or len(raw_sym) > 10:
                continue
            if not re.match(r"^[A-Z0-9.\-]+$", raw_sym):
                continue
            if raw_sym.lower() in ("symbol", "ticker", "code"):
                continue
            name = _ascii_safe(row[2]) if len(row) >= 3 else ""
            currency = _ascii_safe(row[1]) if len(row) >= 2 else ""
            results.append((raw_sym, name, currency))
            found_this_page += 1
        if found_this_page == 0:
            empty_pages += 1
            if empty_pages >= 2:
                log.info("IBKR %s: pagination done at page %d", exchange, page)
                break
        else:
            empty_pages = 0
        page += 1
    # Deduplicate by symbol
    seen = set()
    unique = []
    for sym, name, cur in results:
        if sym in seen:
            continue
        seen.add(sym)
        unique.append((sym, name, cur))
    log.info("IBKR %s: %d unique STK symbols", exchange, len(unique))
    return unique


IBKR_EXCHANGES = {
    "nyse":   ("NYSE",   "USD"),
    "nasdaq": ("NASDAQ", "USD"),
    "lse":    ("LSE",    "GBP"),
    "asx":    ("ASX",    "AUD"),
}


# ==============================================================
# DB SCHEMA + WRITE
# ==============================================================
def _get_conn():
    return sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)


def ensure_schema():
    """Create universe + universe_indices tables if not present."""
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
    End-to-end refresh:
      1. Fetch IBKR universe per exchange
      2. Fetch Wikipedia index constituents
      3. Cross-reference -- only keep symbols in BOTH
      4. Atomically swap into live tables
    Returns dict with counts + errors. ASCII-safe throughout.
    """
    t0 = time.time()
    ensure_schema()

    result = {"ok": False, "counts": {}, "errors": [], "took_seconds": 0.0}

    # Step 1: fetch IBKR universe per exchange
    ibkr_data = {}
    for exch_key, (exch_code, currency) in IBKR_EXCHANGES.items():
        try:
            syms = fetch_ibkr_universe(exch_key)
            ibkr_data[exch_code] = {s: (n, c or currency) for s, n, c in syms}
        except Exception as e:
            err = _ascii_safe(e)
            log.error("IBKR %s fetch failed: %s", exch_code, err)
            result["errors"].append("IBKR %s: %s" % (exch_code, err))
            ibkr_data[exch_code] = {}

    # Sanity check
    nyse_count = len(ibkr_data.get("NYSE", {}))
    if nyse_count < 500:
        msg = "NYSE universe too small (%d), aborting to avoid wiping good data" % nyse_count
        log.error(msg)
        result["errors"].append(msg)
        result["took_seconds"] = time.time() - t0
        return result

    # Step 2: fetch Wikipedia indices
    index_data = {}
    for idx_name in WIKI_INDICES.keys():
        members = fetch_index_members(idx_name)
        index_data[idx_name] = members

    # Step 3: cross-reference
    index_exchange_hint = {
        "SP500": ["NYSE", "NASDAQ"],
        "SP400": ["NYSE", "NASDAQ"],
        "SP600": ["NYSE", "NASDAQ"],
        "NASDAQ100": ["NASDAQ"],
        "DJIA": ["NYSE", "NASDAQ"],
        "FTSE100": ["LSE"],
        "FTSE250": ["LSE"],
        "ASX200": ["ASX"],
        "ASX300": ["ASX"],
    }

    universe_rows = []
    index_rows = []
    seen = set()

    for idx_name, members in index_data.items():
        candidates = index_exchange_hint.get(idx_name, ["NYSE", "NASDAQ", "LSE", "ASX"])
        for tkr, name in members:
            matched_exch = None
            variants = [tkr, tkr.replace(".", ""), tkr.split(".")[0]]
            for exch in candidates:
                ibkr_pool = ibkr_data.get(exch, {})
                for var in variants:
                    if var in ibkr_pool:
                        matched_exch = exch
                        tkr = var
                        break
                if matched_exch:
                    break
            if not matched_exch:
                continue
            key = (tkr, matched_exch)
            if key not in seen:
                seen.add(key)
                ibkr_name, ibkr_currency = ibkr_data[matched_exch][tkr]
                universe_rows.append((tkr, matched_exch, ibkr_currency, ibkr_name or name))
            index_rows.append((tkr, matched_exch, idx_name))

    result["counts"]["ibkr_raw"] = {e: len(d) for e, d in ibkr_data.items()}
    result["counts"]["index_raw"] = {k: len(v) for k, v in index_data.items()}
    result["counts"]["universe"] = len(universe_rows)
    result["counts"]["indices"] = len(index_rows)

    if len(universe_rows) < 500:
        msg = "Cross-referenced universe too small (%d), aborting" % len(universe_rows)
        log.error(msg)
        result["errors"].append(msg)
        result["took_seconds"] = time.time() - t0
        return result

    # Step 4: atomic swap
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
        result["took_seconds"] = time.time() - t0
        conn.close()
        return result
    conn.close()

    result["ok"] = True
    result["took_seconds"] = round(time.time() - t0, 1)
    return result


if __name__ == "__main__":
    import sys
    print("Refreshing universe from IBKR + Wikipedia...")
    print("This takes 1-5 minutes. Please wait.")
    print()
    result = refresh_universe()
    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result["ok"] else 1)
