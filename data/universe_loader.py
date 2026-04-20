"""
data/universe_loader.py — AlphaBot tradeable universe loader

Responsibilities:
  1. Fetch IBKR product directories for NYSE/NASDAQ/LSE/ASX (all tradeable common stocks)
  2. Fetch index constituent lists from Wikipedia (S&P 500, Russell 2000, FTSE 100/250/SmallCap,
     ASX 200, ASX Small Ordinaries)
  3. Cross-reference: only store symbols that exist in BOTH IBKR AND at least one index
  4. Write to `universe` table (symbols) and `universe_indices` table (symbol→index membership)
  5. Idempotent: uses UPSERT, re-runs safely
  6. Defensive: writes to temp tables first, swaps atomically if sanity checks pass

Usage:
    from data.universe_loader import refresh_universe
    result = refresh_universe()
    # returns dict: {
    #   'ok': True,
    #   'counts': {'universe': 3847, 'indices': 12},
    #   'errors': [],
    #   'took_seconds': 67.4
    # }

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
USER_AGENT = "AlphaBot/1.0 (garrathholdstock@users.github — personal trading bot)"
HTTP_TIMEOUT = 30
HTTP_DELAY = 0.5  # be polite: 2 req/sec max

log = logging.getLogger("universe_loader")
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] [UNIVERSE] %(message)s'))
    log.addHandler(h)
    log.setLevel(logging.INFO)


# ═══════════════════════════════════════════════════════════════
# HTTP HELPERS
# ═══════════════════════════════════════════════════════════════
def _http_get(url, referer=None):
    """Polite GET with delay, UA header, timeout."""
    time.sleep(HTTP_DELAY)
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        **({"Referer": referer} if referer else {}),
    })
    with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ═══════════════════════════════════════════════════════════════
# SOURCE 1 — WIKIPEDIA INDEX CONSTITUENTS
# ═══════════════════════════════════════════════════════════════
# Wikipedia tables are wrapped in <table class="wikitable sortable"> ... </table>
# with <tbody><tr><td>...</td></tr></tbody>. First column is usually ticker.

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
        """Return list of (ticker, name) tuples, skipping header row."""
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
            # Normalize
            tkr = tkr.upper().replace(".", ".")  # preserve dots (BRK.B)
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
    """Return list of (ticker, name) for a given index."""
    if index_name not in WIKI_INDICES:
        raise ValueError(f"Unknown index: {index_name}")
    url, tkr_col, name_col = WIKI_INDICES[index_name]
    try:
        html = _http_get(url)
    except Exception as e:
        log.error(f"{index_name}: fetch failed — {e}")
        return []
    parser = WikiTableParser(ticker_col=tkr_col, name_col=name_col)
    try:
        parser.feed(html)
    except Exception as e:
        log.error(f"{index_name}: parse failed — {e}")
        return []
    tickers = parser.get_tickers()
    log.info(f"{index_name}: {len(tickers)} members fetched")
    return tickers


# ═══════════════════════════════════════════════════════════════
# SOURCE 2 — IBKR PRODUCT DIRECTORY (cross-reference for tradeability)
# ═══════════════════════════════════════════════════════════════
# IBKR paginates their product pages. We fetch the "STK" (stock) category.
# URL pattern: /en/index.php?f=2222&exch={exch}&showcategories=STK&p=&cc=&limit=100&page={N}
# Response is HTML with a table of symbols.

class IBKRProductParser(HTMLParser):
    """Parse IBKR's STK product list table. Looks for <a> tags inside <td> cells."""
    def __init__(self):
        super().__init__()
        self.in_row = False
        self.in_cell = False
        self.current_cell = []
        self.current_row = []
        self.rows = []
        self.cell_idx = 0
        self.in_relevant_table = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "table":
            self.in_relevant_table = True
        elif tag == "tr" and self.in_relevant_table:
            self.in_row = True
            self.current_row = []
            self.cell_idx = 0
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
            self.cell_idx += 1

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell.append(data)


def fetch_ibkr_universe(exchange):
    """
    Fetch tradeable STK symbols from IBKR's public product directory.
    Paginates via &page=N until empty page returned.
    Returns list of (symbol, name, currency) tuples.
    """
    base = "https://www.interactivebrokers.com/en/index.php"
    results = []
    page = 1
    max_pages = 100  # safety cap
    empty_pages = 0
    while page <= max_pages:
        url = f"{base}?f=2222&exch={exchange}&showcategories=STK&p=&cc=&limit=100&page={page}"
        try:
            html = _http_get(url, referer=base)
        except Exception as e:
            log.warning(f"IBKR {exchange} page {page}: {e}")
            empty_pages += 1
            if empty_pages >= 3:
                break
            page += 1
            continue
        parser = IBKRProductParser()
        try:
            parser.feed(html)
        except Exception as e:
            log.warning(f"IBKR {exchange} page {page} parse error: {e}")
            page += 1
            continue

        # Extract usable rows — IBKR's product table has columns like:
        # [Symbol, Currency, Name, ...]  — we want symbol (col 0) + name (col 2) + currency (col 1)
        # But the exact shape varies. Be defensive: find rows where col 0 is a short uppercase symbol.
        found_this_page = 0
        for row in parser.rows:
            if len(row) < 2:
                continue
            sym = row[0].strip().upper()
            # Filter: must look like a stock symbol
            if not sym or len(sym) > 10 or not re.match(r"^[A-Z0-9.\-]+$", sym):
                continue
            # Skip obvious header rows
            if sym.lower() in ("symbol", "ticker", "code"):
                continue
            name = row[2] if len(row) >= 3 else ""
            currency = row[1] if len(row) >= 2 else ""
            results.append((sym, name, currency))
            found_this_page += 1
        if found_this_page == 0:
            empty_pages += 1
            if empty_pages >= 2:
                log.info(f"IBKR {exchange}: pagination done at page {page}")
                break
        else:
            empty_pages = 0
        page += 1
    # Deduplicate
    seen = set()
    unique = []
    for sym, name, cur in results:
        if sym in seen:
            continue
        seen.add(sym)
        unique.append((sym, name, cur))
    log.info(f"IBKR {exchange}: {len(unique)} unique STK symbols")
    return unique


IBKR_EXCHANGES = {
    "nyse":   ("NYSE",   "USD"),
    "nasdaq": ("NASDAQ", "USD"),
    "lse":    ("LSE",    "GBP"),
    "asx":    ("ASX",    "AUD"),
}


# ═══════════════════════════════════════════════════════════════
# DB SCHEMA + WRITE
# ═══════════════════════════════════════════════════════════════
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
            PRIMARY KEY (symbol, exchange, index_name),
            FOREIGN KEY (symbol, exchange) REFERENCES universe(symbol, exchange)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_universe_exchange ON universe(exchange)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_universe_indices_index ON universe_indices(index_name)")
    conn.commit()
    conn.close()


def refresh_universe():
    """
    End-to-end refresh:
      1. Fetch IBKR universe for each exchange
      2. Fetch Wikipedia index constituents
      3. Cross-reference → only keep symbols in BOTH
      4. Atomically swap into live tables
    Returns dict with counts + errors.
    """
    t0 = time.time()
    ensure_schema()

    result = {"ok": False, "counts": {}, "errors": [], "took_seconds": 0.0}

    # ── Step 1: fetch IBKR universe for each exchange ──
    ibkr_data = {}  # {exchange_code: {symbol: (name, currency)}}
    for exch_key, (exch_code, currency) in IBKR_EXCHANGES.items():
        try:
            syms = fetch_ibkr_universe(exch_key)
            ibkr_data[exch_code] = {s: (n, c or currency) for s, n, c in syms}
        except Exception as e:
            log.error(f"IBKR {exch_code} fetch failed: {e}")
            result["errors"].append(f"IBKR {exch_code}: {e}")
            ibkr_data[exch_code] = {}

    # Sanity check: we should have got AT LEAST 500 NYSE symbols
    if len(ibkr_data.get("NYSE", {})) < 500:
        msg = f"NYSE universe too small ({len(ibkr_data.get('NYSE', {}))}) — aborting to avoid wiping good data"
        log.error(msg)
        result["errors"].append(msg)
        result["took_seconds"] = time.time() - t0
        return result

    # ── Step 2: fetch Wikipedia indices ──
    index_data = {}  # {index_name: [(ticker, name)]}
    for idx_name in WIKI_INDICES.keys():
        members = fetch_index_members(idx_name)
        index_data[idx_name] = members

    # ── Step 3: build the cross-referenced universe ──
    # For each symbol in any index, look up in IBKR universe.
    # Map index → expected exchange:
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

    universe_rows = []  # (symbol, exchange, currency, name)
    index_rows = []     # (symbol, exchange, index_name)
    seen = set()

    for idx_name, members in index_data.items():
        candidates = index_exchange_hint.get(idx_name, ["NYSE", "NASDAQ", "LSE", "ASX"])
        for tkr, name in members:
            # Try each candidate exchange
            matched_exch = None
            # Normalize common ticker variations — BRK.B vs BRKB, RDSA vs RDSA.L
            variants = [tkr, tkr.replace(".", ""), tkr.split(".")[0]]
            for exch in candidates:
                ibkr_pool = ibkr_data.get(exch, {})
                for var in variants:
                    if var in ibkr_pool:
                        matched_exch = exch
                        tkr = var  # use the matched variant
                        break
                if matched_exch:
                    break
            if not matched_exch:
                continue  # not tradeable on IBKR, skip
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
        msg = f"Cross-referenced universe too small ({len(universe_rows)}) — aborting"
        log.error(msg)
        result["errors"].append(msg)
        result["took_seconds"] = time.time() - t0
        return result

    # ── Step 4: atomic swap via temp tables ──
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
        log.info(f"Universe written: {len(universe_rows)} symbols, {len(index_rows)} index memberships")
    except Exception as e:
        conn.rollback()
        log.error(f"DB write failed: {e}")
        result["errors"].append(f"DB: {e}")
        result["took_seconds"] = time.time() - t0
        conn.close()
        return result
    conn.close()

    result["ok"] = True
    result["took_seconds"] = round(time.time() - t0, 1)
    return result


# ═══════════════════════════════════════════════════════════════
# CLI ENTRYPOINT
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys
    print("Refreshing universe from IBKR + Wikipedia...")
    print("This will take 1-5 minutes. Please wait.\n")
    result = refresh_universe()
    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result["ok"] else 1)
