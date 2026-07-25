import os
import json
import logging
import urllib.request
import re
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("UpdateScreenerPool")

DEFAULT_TICKERS = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "GOOG", "META", "TSLA", "UNH", "JNJ",
    "JPM", "XOM", "V", "PG", "AVGO", "HD", "MA", "LLY", "MRK", "ABBV",
    "PEP", "COST", "KO", "ADBE", "WMT", "MCD", "CSCO", "CRM", "BAC", "ACN",
    "TMO", "NFLX", "PFE", "ORCL", "AMD", "ABT", "NKE", "CMCSA", "DIS", "INTC",
    "CVX", "WFC", "QCOM", "TXN", "MS", "HON", "COP", "AMAT", "VZ", "RTX",
    "VRTX", "NEE", "AMGN", "IBM", "PM", "GE", "UNP", "SPY", "QQQ", "SOL/USD"
]

def scrape_sp500_wikipedia() -> list[str]:
    """Scrapes S&P 500 tickers from Wikipedia."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    logger.info(f"Fetching S&P 500 constituents from {url}...")
    
    req = urllib.request.Request(
        url, 
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode('utf-8')
    except Exception as e:
        logger.warning(f"urllib failed to fetch: {e}. Returning empty list.")
        return []
        
    table_match = re.search(r'<table[^>]*id="constituents"[^>]*>(.*?)</table>', html, re.DOTALL)
    if not table_match:
        table_match = re.search(r'<table[^>]*class="wikitable sortable"[^>]*>(.*?)</table>', html, re.DOTALL)
        
    tickers = []
    if table_match:
        table_html = table_match.group(1)
        rows = re.findall(r'<tr>(.*?)</tr>', table_html, re.DOTALL)
        for row in rows:
            tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            if tds:
                sym_match = re.search(r'<a[^>]*>(.*?)</a>', tds[0])
                if sym_match:
                    sym = sym_match.group(1).strip()
                    sym = sym.replace('\n', '').replace(' ', '')
                    if re.match(r'^[A-Z\.-]+$', sym):
                        tickers.append(sym)
                else:
                    sym = re.sub(r'<[^>]*>', '', tds[0]).strip()
                    if re.match(r'^[A-Z\.-]+$', sym):
                        tickers.append(sym)
                        
    return [t.upper().replace('.', '-') for t in tickers if t]

def main():
    pool_path = Path(__file__).resolve().parent.parent / "screener_pool.json"
    try:
        scraped = scrape_sp500_wikipedia()
        if scraped and len(scraped) > 100:
            logger.info(f"Successfully scraped {len(scraped)} tickers from Wikipedia.")
            scraped_set = set(scraped)
            
            final_pool = ["SPY", "QQQ", "SOL/USD"]
            
            # Add other defaults first (highest liquidity)
            for t in DEFAULT_TICKERS:
                if t not in final_pool and t in scraped_set:
                    final_pool.append(t)
                    
            # Fill up with scraped set up to 65 tickers
            for t in scraped:
                if len(final_pool) >= 65:
                    break
                if t not in final_pool and t not in ("BRK-B", "BRK.B", "BF.B"):
                    final_pool.append(t)
                    
            # Write to file
            with open(pool_path, "w") as f:
                json.dump(final_pool, f, indent=4)
            logger.info(f"Updated {pool_path} with {len(final_pool)} liquid tickers.")
        else:
            raise ValueError(f"Scraped ticker list is empty or too small: {len(scraped) if scraped else 0}")
    except Exception as e:
        logger.error(f"Failed to scrape constituents: {e}. Falling back to default list.")
        if not pool_path.exists() or pool_path.stat().st_size == 0:
            with open(pool_path, "w") as f:
                json.dump(DEFAULT_TICKERS, f, indent=4)
            logger.info(f"Wrote default tickers to {pool_path}")

if __name__ == "__main__":
    main()
