# -*- coding: utf-8 -*-
"""毎晩 GitHub Actions で走り、S&P500 の全銘柄を評価して data.json と chart.json を作る。

スマホ側(index.html)は読むだけなので、APIキーも待ち時間も不要。

■ 規則（20年・約500銘柄・手数料込みで検証。research/study_rules2.py）
  点数 = 6ヶ月の値上がり順位×50% ＋ 200日線からの離れ具合の順位×30% ＋ 12-1ヶ月の値上がり順位×20%
  見直しは『月に1回・月末の点数』で行う（毎週見直すより良かった）。
    売る : 月末の点数が 70 未満になった持ち株
    買う : 月末の点数が 70 以上・200日線より上・財務に地雷なしの銘柄を点数順に。
           同じ業種は 5 つまで・同じ細かい業種は 2 つまで。合計 10 銘柄。
    止める: S&P500(SPY) が 200日線の下にある月末は、新しく買わない（持ち株は上の規則のまま）
           → 年率はほぼ同じ（32.7%→31.6%）で、最大下落が -57.8% → -32.6% に縮んだ。
  以前の「点数40未満か200日線割れで売る」は年率 27.9%・最大下落 -59.0% と、比べた中で最も悪かった。
  ※数字は「いまの S&P500 の銘柄」で測ったので生存バイアスで大きく出ている（規則どうしの比較には使える）。

■ ファンダは『地雷を避ける』用途だけ（過去の一時点データが無く検証できないため順位には使わない）
"""
import json, urllib.request, urllib.parse, http.cookiejar, time, io, os, sys, csv, bisect
from datetime import datetime, timezone, timedelta, date

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
OUT, CHART = "data.json", "chart.json"
SP500_CSV = "sp500.csv"
SP500_URL = ("https://raw.githubusercontent.com/datasets/"
             "s-and-p-500-companies/main/data/constituents.csv")

W_MOM6, W_TREND, W_MOM12 = 0.5, 0.3, 0.2
SECTOR_CAP, SUB_CAP = 5, 2
BUY_MIN = 70              # 買える点数・持ち続ける点数（月末で見る）
PORTFOLIO = 10            # 持つ銘柄数（既定）
JST = timezone(timedelta(hours=9))
# NYSE の休場日（月末の判定に使う。2026〜2027）
NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24"}
SECTOR_JA = {
    "Information Technology": "情報技術", "Health Care": "ヘルスケア", "Financials": "金融",
    "Consumer Discretionary": "一般消費財", "Communication Services": "通信・メディア",
    "Industrials": "資本財・産業", "Consumer Staples": "生活必需品", "Energy": "エネルギー",
    "Utilities": "公益", "Real Estate": "不動産", "Materials": "素材"}


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
    """対象銘柄。毎回最新の構成銘柄を取りに行き、取れたら保存して使う（取れなければ保存分）。
    以前は一度保存した CSV を使い続けていたため、7月以降の入れ替え（6銘柄）が反映されていなかった。"""
    try:
        with _open(SP500_URL) as r:
            raw = r.read()
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
        if len(rows) >= 480:
            io.open(SP500_CSV, "wb").write(raw)
            print("構成銘柄: 最新を取得", len(rows), flush=True)
    except Exception as e:
        print("構成銘柄: 取得できず保存分を使う", e, flush=True)
    rows = list(csv.DictReader(io.open(SP500_CSV, encoding="utf-8")))
    return [{"t": r["Symbol"].replace(".", "-"), "n": r["Security"], "sec": r["GICS Sector"],
             "sub": r.get("GICS Sub-Industry", "")} for r in rows]


def fetch_daily(sym, rng="3y"):
    """日足の調整後終値（分割・配当の影響を除いた比較可能な系列）と日付"""
    u = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"
    d = json.load(_open(u))
    res = d["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
    out = []
    for t, c in zip(res["timestamp"], adj):
        if c is not None and c > 0:
            day = datetime.fromtimestamp(t, timezone(timedelta(hours=-5))).date().isoformat()
            if out and out[-1][0] == day:
                out[-1] = (day, c)
            else:
                out.append((day, c))
    return out


def yahoo_session(tries=3):
    """quoteSummary(ファンダ)は cookie + crumb が要る。取れなければ None。"""
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
    """ROE・利益率・負債・PER・時価総額・次の決算日。取れなければ空。"""
    if not sess:
        return {}
    op, crumb = sess
    mods = "financialData,defaultKeyStatistics,summaryDetail,calendarEvents"
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

    earn = None
    try:
        ed = r.get("calendarEvents", {}).get("earnings", {}).get("earningsDate") or []
        if ed:
            earn = datetime.fromtimestamp(ed[0]["raw"], timezone.utc).date().isoformat()
    except Exception:
        pass
    return {"roe": g("financialData", "returnOnEquity"), "opm": g("financialData", "operatingMargins"),
            "de": g("financialData", "debtToEquity"), "rev": g("financialData", "revenueGrowth"),
            "per": g("summaryDetail", "trailingPE"), "mcap": g("summaryDetail", "marketCap"),
            "div": g("summaryDetail", "dividendYield"), "earn": earn}


# ---------------- 指標の計算 ----------------
def factors_at(px, k):
    """px の k 本目（古い順）の時点で分かる情報だけで作る要因。足りなければ None"""
    if k < 260:
        return None
    ma200 = sum(px[k - 199:k + 1]) / 200
    return {"mom6": px[k] / px[k - 126] - 1, "mom12": px[k - 21] / px[k - 252] - 1,
            "trend": px[k] / ma200 - 1, "ma200": ma200}


def pct_ranks(pairs):
    ok = sorted([(s, v) for s, v in pairs if v is not None], key=lambda x: x[1])
    n = len(ok)
    return {s: (i / (n - 1) * 100 if n > 1 else 50.0) for i, (s, v) in enumerate(ok)}


def score_all(fac):
    """{sym: 要因} → {sym: 点数(0〜100)}"""
    syms = [s for s in fac if fac[s]]
    r6 = pct_ranks([(s, fac[s]["mom6"]) for s in syms])
    rt = pct_ranks([(s, fac[s]["trend"]) for s in syms])
    r12 = pct_ranks([(s, fac[s]["mom12"]) for s in syms])
    return {s: (W_MOM6 * r6[s] + W_TREND * rt[s] + W_MOM12 * r12[s], r6[s], rt[s], r12[s]) for s in syms}


def quality_flags(fn):
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


def next_trading_day(d):
    x = date.fromisoformat(d)
    while True:
        x += timedelta(days=1)
        if x.weekday() < 5 and x.isoformat() not in NYSE_HOLIDAYS:
            return x.isoformat()


def month_ends(days):
    """取引日の並びから、各月の最後の取引日。最後の月は『翌営業日が翌月』の時だけ確定扱い"""
    out = [a for a, b in zip(days, days[1:]) if a[:7] != b[:7]]
    if days and next_trading_day(days[-1])[:7] != days[-1][:7]:
        out.append(days[-1])
    return out


def last_trading_day(y, m):
    x = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    while x.weekday() >= 5 or x.isoformat() in NYSE_HOLIDAYS:
        x -= timedelta(days=1)
    return x.isoformat()


def next_month_end(me_date):
    """確定した月末の次の月末（＝次の見直しに使う日）"""
    y, m = int(me_date[:4]), int(me_date[5:7])
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return last_trading_day(y, m)


def pick(cands, held_sec, held_sub, k):
    """点数順の候補から、業種の上限を守って k 銘柄まで選ぶ"""
    us, ub, out = dict(held_sec), dict(held_sub), []
    for u in cands:
        if len(out) >= k:
            break
        if us.get(u["sec"], 0) >= SECTOR_CAP or ub.get(u["sub"], 0) >= SUB_CAP:
            continue
        out.append(u)
        us[u["sec"]] = us.get(u["sec"], 0) + 1
        ub[u["sub"]] = ub.get(u["sub"], 0) + 1
    return out


# ---------------- 本体 ----------------
def main():
    uni = load_universe()
    print(f"対象 {len(uni)}銘柄", flush=True)
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = {s["t"]: s for s in json.load(io.open(OUT, encoding="utf-8")).get("stocks", [])}
        except Exception:
            prev = {}

    # 1) 全銘柄の日足
    series, ok = {}, []
    for i, u in enumerate(uni):
        try:
            s = fetch_daily(u["t"])
            if len(s) >= 300:
                series[u["t"]] = s
                ok.append(u)
        except Exception:
            pass
        if i % 100 == 0:
            print(f"  {i}/{len(uni)}", flush=True)
        time.sleep(0.08)
    print(f"計算できた銘柄: {len(ok)}", flush=True)
    if len(ok) < 400:
        raise SystemExit("取得できた銘柄が少なすぎます。中止します（前回の結果を残す）。")
    spy = fetch_daily("SPY")

    # 2) 取引日の暦（SPY）と月末
    days = [d for d, _ in spy]
    mes = month_ends(days)[-13:]                 # 直近 13 回の確定した月末
    last_day = days[-1]

    px = {u["t"]: [c for _, c in series[u["t"]]] for u in ok}
    dates = {u["t"]: [d for d, _ in series[u["t"]]] for u in ok}

    def scores_on(d):
        fac = {}
        for u in ok:
            t = u["t"]
            k = bisect.bisect_right(dates[t], d) - 1
            # その日から 5 営業日以上古い値しかない銘柄（上場廃止・取引停止）は外す
            if k < 0 or (date.fromisoformat(d) - date.fromisoformat(dates[t][k])).days > 7:
                continue
            fac[t] = factors_at(px[t], k)
        return score_all(fac), fac

    now_sc, now_fac = scores_on(last_day)
    hist = {u["t"]: [] for u in ok}
    me_sc, me_fac = None, None
    for d in mes:
        sc, fac = scores_on(d)
        for u in ok:
            v = sc.get(u["t"])
            hist[u["t"]].append(round(v[0]) if v else None)
        me_sc, me_fac = sc, fac
    me_date = mes[-1]

    # 3) 相場全体：S&P500(SPY) と 200日線（月末と今）、200日線より上の銘柄の割合
    spx = [c for _, c in spy]
    spy_me_k = days.index(me_date)

    def spy_state(k):
        ma = sum(spx[k - 199:k + 1]) / 200
        return spx[k] > ma, round((spx[k] / ma - 1) * 100, 1)
    on_now, dev_now = spy_state(len(spx) - 1)
    on_me, dev_me = spy_state(spy_me_k)
    breadth = round(sum(1 for t in now_fac if now_fac[t] and now_fac[t]["trend"] > 0) / max(1, len(now_fac)) * 100)

    # 4) ファンダ（点数 60 以上＝買い候補の周辺だけ。全部取ると時間が掛かる）
    need = [u for u in ok if (now_sc.get(u["t"], (0,))[0] >= 60 or (me_sc.get(u["t"], (0,))[0] >= 60))]
    sess = yahoo_session()
    print("ファンダ取得:", f"{len(need)}銘柄" if sess else "スキップ(価格要因のみで判定)", flush=True)
    fund = {}
    for u in need:
        fund[u["t"]] = fetch_fundamentals(u["t"], sess) if sess else {}
        if sess:
            time.sleep(0.08)

    # 5) 銘柄ごとの結果
    stocks = []
    for u in ok:
        t = u["t"]
        if t not in now_sc or not now_fac.get(t):
            continue
        s_now, r6, rt, r12 = now_sc[t]
        f = now_fac[t]
        fn = fund.get(t, {})
        good, bad = quality_flags(fn)
        me = me_sc.get(t)
        mef = me_fac.get(t) if me_fac else None
        elig = bool(me and me[0] >= BUY_MIN and mef and mef["trend"] > 0 and not bad)
        last = px[t][-1]
        rec = {
            "t": t, "n": u["n"], "sec": u["sec"], "secJa": SECTOR_JA.get(u["sec"], u["sec"]), "sub": u["sub"],
            "px": round(last, 2), "chg1d": round((last / px[t][-2] - 1) * 100, 2),
            "chg6m": round(f["mom6"] * 100, 1), "trend": round(f["trend"] * 100, 1),
            "score": round(s_now), "f": {"mom": round(r6), "trend": round(rt), "mom12": round(r12)},
            "me": round(me[0]) if me else None, "hist": hist[t], "elig": elig,
            "prev": prev.get(t, {}).get("score"),
            "good": good, "bad": bad,
            "fund": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in fn.items() if v is not None},
        }
        parts = []
        mom = rec["f"]["mom"]
        parts.append(f"6ヶ月の値上がりが全体の上位{max(1, 100 - mom):.0f}%以内" if mom >= 50
                     else f"6ヶ月の値動きは弱め（下から{max(1, mom):.0f}%の位置）")
        parts.append("200日線の上で上昇トレンド継続中" if f["trend"] > 0 else "200日線を割っており下降トレンド")
        if good: parts.append("／".join(good))
        if bad:  parts.append("⚠ " + "／".join(bad) + "（買い候補から外す）")
        rec["why"] = "。".join(parts) + "。"
        stocks.append(rec)
    stocks.sort(key=lambda x: -x["score"])

    # 6) 月末の点数で「新しく10銘柄持つなら」の組み合わせ（業種の上限つき）
    cands = sorted([s for s in stocks if s["elig"]], key=lambda x: -x["me"])
    model = [s["t"] for s in pick(cands, {}, {}, PORTFOLIO)] if on_me else []

    out = {
        "updated": datetime.now(timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M"),
        "asof": last_day, "monthEnd": me_date, "monthEnds": mes,
        "nextMonthEnd": next_month_end(me_date),
        "universe": len(stocks),
        "usdjpy": None,
        "rule": {"buyMin": BUY_MIN, "sectorCap": SECTOR_CAP, "subCap": SUB_CAP, "k": PORTFOLIO},
        "market": {"onNow": on_now, "devNow": dev_now, "onMe": on_me, "devMe": dev_me, "breadth": breadth,
                   "spy": round(spx[-1], 2)},
        "model": model,
        "note_bias": ("成績の検証値は現在のS&P500構成銘柄で測ったため、実際より良く出ています。"
                      "規則どうしの比較には使えますが、示された利益率をそのまま期待しないでください。"),
        "stocks": stocks,
    }
    try:
        fx = fetch_daily("JPY=X", rng="5d")
        out["usdjpy"] = round(fx[-1][1], 2) if fx else None
    except Exception:
        pass
    io.open(OUT, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    # チャート用（1年ぶんの週ごとの値）。詳細画面を開いた時だけ読む
    ch = {"d": [d for d in days[-253::5]], "px": {}}
    for s in stocks:
        p = px[s["t"]]
        ds = dates[s["t"]]
        ch["px"][s["t"]] = [round(p[bisect.bisect_right(ds, d) - 1], 2) if bisect.bisect_right(ds, d) else None for d in ch["d"]]
    io.open(CHART, "w", encoding="utf-8").write(json.dumps(ch, separators=(",", ":")))
    n_el = sum(1 for s in stocks if s["elig"])
    print(f"完了: {OUT} 候補{n_el}件 / 月末{me_date} S&P500 {'200日線の上' if on_me else '200日線の下'}", flush=True)


if __name__ == "__main__":
    main()
