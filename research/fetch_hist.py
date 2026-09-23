# -*- coding: utf-8 -*-
"""検証用：S&P500 構成銘柄の日足（調整後終値）を 2004 年から取って cache/ に置く。

使い方:  python research/fetch_hist.py [銘柄CSV ...]
  既に取ってある銘柄は飛ばす（途中で止まっても続きから取れる）。
"""
import csv, io, json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
P1 = 1072915200          # 2004-01-01


def fetch(sym):
    path = os.path.join(CACHE, sym + ".json")
    if os.path.exists(path):
        return sym, "skip"
    u = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
         f"?period1={P1}&period2={int(time.time())}&interval=1d&events=div,split")
    for k in range(4):
        try:
            req = urllib.request.Request(u, headers=UA)
            d = json.load(urllib.request.urlopen(req, timeout=40))
            res = d["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
            out = {"t": [], "c": [], "raw": []}
            for t, a, c in zip(res["timestamp"], adj, q["close"]):
                if a is not None and c is not None:
                    out["t"].append(t); out["c"].append(round(a, 4)); out["raw"].append(round(c, 4))
            io.open(path, "w").write(json.dumps(out, separators=(",", ":")))
            return sym, len(out["t"])
        except Exception as e:
            err = e
            time.sleep(2 * (k + 1))
    return sym, f"ERR {err}"


def main():
    os.makedirs(CACHE, exist_ok=True)
    files = sys.argv[1:] or [os.path.join(HERE, "..", "sp500.csv")]
    syms = []
    for f in files:
        for r in csv.DictReader(io.open(f, encoding="utf-8")):
            s = r["Symbol"].replace(".", "-")
            if s not in syms:
                syms.append(s)
    syms.append("SPY")
    print(len(syms), "銘柄", flush=True)
    done = 0
    with ThreadPoolExecutor(4) as ex:
        for sym, st in ex.map(fetch, syms):
            done += 1
            if isinstance(st, str) and st.startswith("ERR"):
                print(sym, st, flush=True)
            if done % 50 == 0:
                print(f"  {done}/{len(syms)}", flush=True)
    print("完了", flush=True)


if __name__ == "__main__":
    main()
