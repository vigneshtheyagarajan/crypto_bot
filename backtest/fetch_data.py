"""
Download Binance public 1-minute spot klines (no API key) into backtest_data/.

    python3 backtest/fetch_data.py --start 2025-11 --end 2026-10

Writes one gzip CSV per symbol: backtest_data/<SYMBOL>_1m.csv.gz with columns
open_time, open, high, low, close, volume (no header).
"""
import argparse
import csv
import gzip
import io
import os
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

from backtest import WATCHLIST

URL = "https://data.binance.vision/data/spot/monthly/klines/{s}/1m/{s}-1m-{m}.zip"


def months(start, end):
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def fetch(sym, month):
    for _ in range(3):
        try:
            with urllib.request.urlopen(URL.format(s=sym, m=month), timeout=60) as r:
                z = zipfile.ZipFile(io.BytesIO(r.read()))
                return z.read(z.namelist()[0]).decode()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:
            pass
    raise RuntimeError(f"failed {sym} {month}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-11")
    ap.add_argument("--end", default="2026-10")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "backtest_data"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    ms = list(months(a.start, a.end))
    for sym in WATCHLIST:
        with ThreadPoolExecutor(6) as ex:
            parts = list(ex.map(lambda m: fetch(sym, m), ms))
        rows = 0
        with gzip.open(os.path.join(a.out, f"{sym}_1m.csv.gz"), "wt", newline="") as f:
            w = csv.writer(f)
            for m, text in zip(ms, parts):
                if text is None:
                    print(f"  {sym} {m}: not available")
                    continue
                for row in csv.reader(io.StringIO(text)):
                    if row and row[0].isdigit():
                        w.writerow(row[:6]); rows += 1
        print(f"{sym}: {rows} rows")


if __name__ == "__main__":
    main()
