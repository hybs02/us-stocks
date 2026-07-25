# -*- coding: utf-8 -*-
"""毎晩 GitHub Actions で走り、S&P500 の全銘柄を評価して data.json を作る。

スマホ側(index.html)は data.json を読むだけなので、APIキーも待ち時間も不要。

方針(中長期・数ヶ月〜年):
  ・価格から作る要因は自前で検証済み(research)のものだけを使う。
  ・ファンダは「地雷を避けるフィルタ」として使う。過去の一時点データが
    取れないため厳密な検証ができない。順位付けの主役にはしない。
"""
import json, urllib.request, urllib.parse, http.cookiejar, time, statistics, io, os, sys, csv
from datetime import datetime, timezone, timedelta

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
OUT = "data.json"
SP500_CSV = "sp500.csv"
SP500_URL = ("https://raw.githubusercontent.com/datasets/"
             "s-and-p-500-companies/main/data/constituents.csv")


# ---------------- 取得まわり ----------------
def _open(url, opener=None, tries=3):
    """opener を渡した時は OpenerDirector.open() を使う。
    （OpenerDirector に urlopen() は無い。ここを間違えると
      cookie付きのファンダ取得が毎回失敗して黙って空になる）"""
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            if opener is not None:
                return opener.open(req, timeout=30)
            return urllib.request.urlopen(req, timeout=30)
        except Exception as e:
            last = e
            time.sleep(1.5 * (k + 1))
    raise last


def load_universe():
    """対象銘柄。リポジトリ内のCSVを優先し、無ければ取得する。"""
    if not os.path.exists(SP500_CSV):
        with _open(SP500_URL) as r:
            io.open(SP500_CSV, "wb").write(r.read())
    rows = list(csv.DictReader(io.open(SP500_CSV, encoding="utf-8")))
    out = []
    for r in rows:
        t = r["Symbol"].replace(".", "-")
        out.append({"t": t, "n": r["Security"], "sec": r["GICS Sector"],
                    "sub": r.get("GICS Sub-Industry", "")})
    return out


def fetch_daily(sym, rng="3y"):
    """日足の調整後終値。分割・配当の影響を除いた比較可能な系列。"""
    u = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
         f"?range={rng}&interval=1d")
    d = json.load(_open(u))
    res = d["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
    ts = res["timestamp"]
    out = []
    for t, c, h, l, v in zip(ts, adj, q["high"], q["low"], q.get("volume") or [0]*len(ts)):
        if c is not None:
            out.append({"t": t, "c": c, "h": h if h is not None else c,
                        "l": l if l is not None else c, "v": v or 0})
    return out


def yahoo_session(tries=3):
    """quoteSummary(ファンダ)は cookie + crumb が要る。取れなければ None。
    大量取得の直後は弾かれやすいので、間を空けて数回試す。"""
    for k in range(tries):
        try:
            cj = http.cookiejar.CookieJar()
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            op.addheaders = list(UA.items())
            try:
                op.open("https://fc.yahoo.com", timeout=20)
            except Exception:
                pass
            crumb = _open("https://query1.finance.yahoo.com/v1/test/getcrumb",
                          opener=op).read().decode().strip()
            if crumb and "<" not in crumb:
                return (op, crumb)
        except Exception:
            pass
        time.sleep(5 * (k + 1))
    return None


def fetch_fundamentals(sym, sess):
    """ROE・利益率・負債・PER など。取れなければ空(=判定は価格要因のみ)。"""
    if not sess:
        return {}
    op, crumb = sess
    mods = "financialData,defaultKeyStatistics,summaryDetail"
    u = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{sym}"
         f"?modules={mods}&crumb={urllib.parse.quote(crumb)}")
    try:
        d = json.load(_open(u, opener=op, tries=2))
        r = d["quoteSummary"]["result"][0]
    except Exception:
        return {}

    def g(block, key):
        x = r.get(block, {}).get(key)
        if isinstance(x, dict):
            return x.get("raw")
        return x if isinstance(x, (int, float)) else None

    return {
        "roe":   g("financialData", "returnOnEquity"),
        "opm":   g("financialData", "operatingMargins"),
        "de":    g("financialData", "debtToEquity"),
        "rev":   g("financialData", "revenueGrowth"),
        "fcf":   g("financialData", "freeCashflow"),
        "per":   g("summaryDetail", "trailingPE"),
        "pbr":   g("defaultKeyStatistics", "priceToBook"),
        "mcap":  g("summaryDetail", "marketCap"),
    }


# ---------------- 指標の計算 ----------------
# 重み・制限は 20年・503銘柄の検証で決めた値（詳細は README）
W_MOM6, W_TREND, W_MOM12 = 0.5, 0.3, 0.2
SECTOR_CAP, SUB_CAP = 5, 2      # 業種の偏りを防ぐ上限
BUY_MIN = 70                    # 買い候補とみなす最低スコア


def sma(a, n):
    return sum(a[-n:]) / n if len(a) >= n else None


def factors(px):
    """px = 日足の調整後終値リスト（古い順）。その時点で分かる情報だけで作る。"""
    n = len(px)
    if n < 260:
        return None
    f = {}
    f["mom6"]  = px[-1] / px[-127] - 1                      # 約6ヶ月
    f["mom12"] = px[-22] / px[-253] - 1                     # 12ヶ月前→1ヶ月前
    ma200 = sma(px, 200)
    f["trend"] = px[-1] / ma200 - 1 if ma200 else None
    f["ma200"] = ma200
    # 参考表示用（順位付けには使わない：検証で有効性を確認できなかったため）
    rets = [px[i] / px[i - 1] - 1 for i in range(n - 126, n)]
    m = sum(rets) / len(rets)
    f["vol"] = (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5 * (252 ** 0.5)
    return f if f["trend"] is not None else None


def pct_ranks(pairs):
    """[(sym, value)] → {sym: 0〜100の順位}"""
    ok = [(s, v) for s, v in pairs if v is not None]
    ok.sort(key=lambda x: x[1])
    n = len(ok)
    return {s: (i / (n - 1) * 100 if n > 1 else 50.0) for i, (s, v) in enumerate(ok)}


def quality_flags(fn):
    """ファンダは『地雷を避ける』用途に限定。順位付けには使わない。
    （過去の一時点データが取れず、厳密な検証ができないため）"""
    bad, good = [], []
    roe, opm, de, per = fn.get("roe"), fn.get("opm"), fn.get("de"), fn.get("per")
    if opm is not None and opm < 0:      bad.append("営業赤字")
    if roe is not None and roe < 0:      bad.append("自己資本利益率がマイナス")
    if de is not None and de > 300:      bad.append("借金が多い(D/E>300%)")
    if per is not None and per > 100:    bad.append("株価が利益の100倍超")
    if roe is not None and roe > 0.20:   good.append("稼ぐ力が強い(ROE20%超)")
    if opm is not None and opm > 0.20:   good.append("利益率が高い(20%超)")
    if de is not None and de < 50:       good.append("借金が少ない")
    return good, bad


# ---------------- 本体 ----------------
def main():
    uni = load_universe()
    print(f"対象 {len(uni)}銘柄", flush=True)

    # 1) 全銘柄の日足を取得して要因を計算
    px_last, fac, ok = {}, {}, []
    for i, u in enumerate(uni):
        try:
            bars = fetch_daily(u["t"])
            px = [b["c"] for b in bars]
            f = factors(px)
            if f:
                fac[u["t"]] = f
                px_last[u["t"]] = px[-1]
                ok.append(u)
        except Exception:
            pass
        if i % 100 == 0:
            print(f"  {i}/{len(uni)}", flush=True)
        time.sleep(0.08)
    print(f"計算できた銘柄: {len(ok)}", flush=True)
    if len(ok) < 100:
        raise SystemExit("取得できた銘柄が少なすぎます。中止します。")

    # 2) 順位付け → 合成スコア
    r6  = pct_ranks([(u["t"], fac[u["t"]]["mom6"])  for u in ok])
    rtr = pct_ranks([(u["t"], fac[u["t"]]["trend"]) for u in ok])
    r12 = pct_ranks([(u["t"], fac[u["t"]]["mom12"]) for u in ok])
    rvol = pct_ranks([(u["t"], -fac[u["t"]]["vol"]) for u in ok])   # 表示用
    for u in ok:
        t = u["t"]
        u["score"] = round(W_MOM6*r6[t] + W_TREND*rtr[t] + W_MOM12*r12[t])
        u["f"] = {"mom": round(r6[t]), "trend": round(rtr[t]),
                  "mom12": round(r12[t]), "vol": round(rvol[t])}
    ok.sort(key=lambda x: -x["score"])

    # 3) 相場全体の向き（全銘柄の平均が200日線の上か）
    #    検証では「弱い時に買わない」と利益は減るが下落幅は圧縮された。
    #    よって自動では止めず、情報として出すだけにする。
    above = sum(1 for u in ok if fac[u["t"]]["trend"] > 0)
    breadth = above / len(ok) * 100
    risk_on = breadth >= 50

    # 4) 上位だけファンダを取って「地雷」を弾く（全銘柄取ると時間が掛かるため）
    sess = yahoo_session()
    print("ファンダ取得:", "OK" if sess else "スキップ(価格要因のみで判定)", flush=True)
    for u in ok[:140]:
        fn = fetch_fundamentals(u["t"], sess) if sess else {}
        u["fund"] = fn
        g, b = quality_flags(fn)
        u["good"], u["bad"] = g, b
        if sess:
            time.sleep(0.08)

    # 5) 売買の区分と、業種の偏りを抑えた「買い候補」の確定
    used_sec, used_sub = {}, {}
    for u in ok:
        t = u["t"]
        f = fac[t]
        u["px"] = round(px_last[t], 2)
        u["stop"] = round(f["ma200"], 2)          # 200日線割れ＝上昇トレンド終了の目安
        u["stop_pct"] = round((f["ma200"] / px_last[t] - 1) * 100, 1)
        u["hold"] = "3〜12ヶ月（3ヶ月ごとに見直し）"
        bad = u.get("bad", [])

        if u["score"] < 40 or f["trend"] < 0:
            u["sig"] = "avoid"
        elif u["score"] >= BUY_MIN and not bad:
            # 同じ業種・同じ細分類に偏らせない（検証で分散させても成績は落ちなかった）
            s, sb = u["sec"], u["sub"]
            if used_sec.get(s, 0) < SECTOR_CAP and used_sub.get(sb, 0) < SUB_CAP:
                u["sig"] = "buy"
                used_sec[s] = used_sec.get(s, 0) + 1
                used_sub[sb] = used_sub.get(sb, 0) + 1
            else:
                u["sig"] = "hold"
                u["why_skip"] = f"「{sb or s}」に既に候補があるため分散の観点で見送り"
        else:
            u["sig"] = "hold"

        # 説明文（なぜこの評価か）
        parts = []
        mom = u["f"]["mom"]
        if mom >= 50:
            parts.append(f"6ヶ月の値上がりが全体の上位{max(1, 100-mom):.0f}%以内")
        else:
            parts.append(f"6ヶ月の値動きは弱め（下から{max(1, mom):.0f}%の位置）")
        parts.append("200日線の上で上昇トレンド継続中" if f["trend"] > 0
                     else "200日線を割っており下降トレンド")
        if u.get("good"): parts.append("／".join(u["good"]))
        if bad:           parts.append("⚠ " + "／".join(bad))
        if u.get("why_skip"): parts.append(u["why_skip"])
        u["why"] = "。".join(parts) + "。"

    # 円換算(株数の目安)に使うドル円レート。取れなければアプリ側が150で代用する。
    try:
        fx = fetch_daily("JPY=X", rng="5d")
        usdjpy = round(fx[-1]["c"], 2) if fx else None
    except Exception:
        usdjpy = None

    # GitHubのサーバーはUTCなので、明示的に日本時間へ直して表示する
    jst = timezone(timedelta(hours=9))
    out = {
        "updated": datetime.now(timezone.utc).astimezone(jst).strftime("%Y-%m-%d %H:%M") + " (日本時間)",
        "universe": len(ok),
        "usdjpy": usdjpy,
        "market": {
            "risk_on": risk_on,
            "label": "上向き" if risk_on else "下向き",
            "note": (f"200日線より上の銘柄が{breadth:.0f}%。"
                     + ("買い候補を検討できる地合いです。"
                        if risk_on else
                        "全体が弱いので、無理に買わず現金比率を上げるのも有効です。")),
            "breadth": round(breadth),
        },
        "note_bias": ("成績の検証値は現在のS&P500構成銘柄で測ったため、"
                      "実際より良く出ています。順位付けの有効性は確認済みですが、"
                      "示された利益率をそのまま期待しないでください。"),
        "stocks": [{k: u[k] for k in
                    ("t", "n", "sec", "sub", "px", "score", "f", "sig", "stop", "stop_pct", "hold", "why")}
                   for u in ok],
    }
    io.open(OUT, "w", encoding="utf-8").write(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    n_buy = sum(1 for u in ok if u["sig"] == "buy")
    print(f"完了: {OUT} 買い候補{n_buy}件 / 相場{out['market']['label']}", flush=True)


if __name__ == "__main__":
    main()
