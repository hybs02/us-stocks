# -*- coding: utf-8 -*-
"""米国株：いまの採点（6ヶ月モメ50%・200日線乖離30%・12-1モメ20%）で
「何を・いつ売るか」の規則を 20 年分で比べる。

※対象は「いまの」S&P500 構成銘柄（＋最近外れた数銘柄）＝生存バイアスで全数値が過大。
  規則どうしの比較（同じ偏りを共有）と、同じ銘柄群の均等買い（基準）との差で見る。

売買は月末の終値で採点 → 翌営業日の終値で約定（同じ終値で売買する甘さを避ける）。
"""
import csv, io, json, os, sys, math, statistics, datetime as dt
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")


def load():
    meta = {}
    for f in ("../sp500.csv", "sp500_latest.csv"):
        for r in csv.DictReader(io.open(os.path.join(HERE, f), encoding="utf-8")):
            meta[r["Symbol"].replace(".", "-")] = (r["GICS Sector"], r.get("GICS Sub-Industry", ""))
    spy = json.load(open(os.path.join(CACHE, "SPY.json")))
    days = [dt.datetime.utcfromtimestamp(t).date() for t in spy["t"]]
    idx = {d: i for i, d in enumerate(days)}
    tick = sorted(t for t in meta if os.path.exists(os.path.join(CACHE, t + ".json")))
    P = np.full((len(days), len(tick)), np.nan)
    for k, t in enumerate(tick):
        d = json.load(open(os.path.join(CACHE, t + ".json")))
        for ts, c in zip(d["t"], d["c"]):
            i = idx.get(dt.datetime.utcfromtimestamp(ts).date())
            if i is not None and c and c > 0:
                P[i, k] = c
    # 飛び値（1日で ±80% 超）を欠損扱い
    r = P[1:] / P[:-1]
    bad = (r > 1.8) | (r < 0.2)
    P[1:][bad] = np.nan
    spyc = np.array([c for c in spy["c"]])
    return days, tick, P, meta, spyc


def ffill(P):
    out = P.copy()
    for i in range(1, len(out)):
        m = np.isnan(out[i])
        out[i, m] = out[i - 1, m]
    return out


def main():
    days, tick, P, meta, spy = load()
    F = ffill(P)
    n, m = F.shape
    print("銘柄", m, "日数", n, days[0], "〜", days[-1])
    # 月末の位置
    me = [i for i in range(n - 1) if days[i].month != days[i + 1].month]
    # 200日線
    cs = np.nancumsum(np.nan_to_num(F), axis=0)
    ma200 = np.full_like(F, np.nan)
    ma200[200:] = (cs[200:] - cs[:-200]) / 200
    valid = np.zeros_like(F, dtype=bool)
    cnt = np.cumsum(~np.isnan(P), axis=0)
    valid[260:] = (cnt[260:] - cnt[:-260]) >= 250            # 1年分そろっている

    def pct_rank(v):
        o = np.full(v.shape, np.nan)
        ok = ~np.isnan(v)
        if ok.sum() < 2:
            return o
        r = v[ok].argsort().argsort()
        o[ok] = r / (ok.sum() - 1) * 100
        return o

    def score_at(i):
        ok = valid[i]
        mom6 = np.where(ok, F[i] / F[i - 126] - 1, np.nan)
        mom12 = np.where(ok, F[i - 21] / F[i - 252] - 1, np.nan)
        trend = np.where(ok, F[i] / ma200[i] - 1, np.nan)
        s = 0.5 * pct_rank(mom6) + 0.3 * pct_rank(trend) + 0.2 * pct_rank(mom12)
        return s, trend

    scores = {i: score_at(i) for i in me if i >= 300}
    sec = [meta[t][0] for t in tick]
    sub = [meta[t][1] for t in tick]

    def pick(i, k=10, held=(), cap=(5, 2)):
        s, trend = scores[i]
        order = [j for j in np.argsort(-np.nan_to_num(s, nan=-1)) if not np.isnan(s[j])]
        chosen, us, ub = list(held), {}, {}
        for j in chosen:
            us[sec[j]] = us.get(sec[j], 0) + 1; ub[sub[j]] = ub.get(sub[j], 0) + 1
        for j in order:
            if len(chosen) >= k:
                break
            if j in chosen or s[j] < 70 or trend[j] <= 0:
                continue
            if us.get(sec[j], 0) >= cap[0] or ub.get(sub[j], 0) >= cap[1]:
                continue
            chosen.append(j); us[sec[j]] = us.get(sec[j], 0) + 1; ub[sub[j]] = ub.get(sub[j], 0) + 1
        return chosen

    def simulate(rule, k=10, start=dt.date(2006, 1, 1), daily_stop=None):
        """rule(i, j, info) -> True なら売る。月末 i で判定、翌日の終値で約定。
        daily_stop: 日々の終値で見る撤退（高値から -x%）"""
        eq, curve, pos, trades = 1.0, [], {}, 0
        mlist = [i for i in me if i >= 300 and days[i] >= start]
        for a, b in zip(mlist, mlist[1:] + [n - 2]):
            # 月末 a で売買を決める
            s, trend = scores[a]
            for j in list(pos):
                info = pos[j]
                if rule(a, j, info, s, trend):
                    del pos[j]
            new = pick(a, k, held=list(pos))
            for j in new:
                if j not in pos:
                    pos[j] = {"entry": F[a + 1, j], "peak": F[a + 1, j], "t0": a + 1}
                    trades += 1
            # a+1 → b+1 の保有（等金額・均等配分、空き枠は現金）
            if pos:
                w = 1.0 / k
                r = 0.0
                for j in list(pos):
                    seg = F[a + 1:b + 2, j]
                    if daily_stop:
                        pk = pos[j]["peak"]
                        exit_r = None
                        for x in range(1, len(seg)):
                            pk = max(pk, seg[x - 1])
                            if seg[x] <= pk * (1 - daily_stop):
                                exit_r = seg[x] / seg[0] - 1
                                break
                        if exit_r is not None:
                            r += w * exit_r
                            del pos[j]
                            continue
                        pos[j]["peak"] = max(pk, np.nanmax(seg))
                    r += w * (seg[-1] / seg[0] - 1)
                eq *= 1 + r - 0.0005 * 0            # 手数料は下で別計算
            curve.append((days[b + 1], eq))
        return curve, trades

    def ew_bench(start=dt.date(2006, 1, 1)):
        eq, curve = 1.0, []
        mlist = [i for i in me if i >= 300 and days[i] >= start]
        for a, b in zip(mlist, mlist[1:] + [n - 2]):
            ok = valid[a] & ~np.isnan(F[b + 1]) & ~np.isnan(F[a + 1])
            r = np.nanmean(F[b + 1, ok] / F[a + 1, ok] - 1)
            eq *= 1 + r
            curve.append((days[b + 1], eq))
        return curve

    def stats(curve, trades=None):
        eqs = [e for _, e in curve]
        yrs = (curve[-1][0] - curve[0][0]).days / 365.25
        cagr = eqs[-1] ** (1 / yrs) - 1
        pk, mdd = 0, 0
        for e in eqs:
            pk = max(pk, e); mdd = min(mdd, e / pk - 1)
        rets = [eqs[i] / eqs[i - 1] - 1 for i in range(1, len(eqs))]
        sh = statistics.mean(rets) / statistics.stdev(rets) * math.sqrt(12)
        # 年ごと
        yr = {}
        prev = 1.0
        for d, e in curve:
            yr.setdefault(d.year, [prev, e]); yr[d.year][1] = e
            prev_e = e
        return {"cagr": cagr * 100, "mdd": mdd * 100, "sharpe": sh, "trades": trades}

    bench = ew_bench()
    sb = stats(bench)
    print(f"\n基準（同じ銘柄群を均等買い・毎月）: 年率{sb['cagr']:.1f}% 最大下落{sb['mdd']:.1f}% シャープ{sb['sharpe']:.2f}")

    RULES = {
        "毎月入れ替え（上位10から外れたら売る）": lambda a, j, info, s, tr: j not in set(pick(a, 10)),
        "いまのアプリ（点数<40 か 200日線割れで売る）": lambda a, j, info, s, tr: (np.isnan(s[j]) or s[j] < 40 or tr[j] <= 0),
        "200日線割れだけで売る": lambda a, j, info, s, tr: (np.isnan(tr[j]) or tr[j] <= 0),
        "点数<50で売る": lambda a, j, info, s, tr: (np.isnan(s[j]) or s[j] < 50),
        "点数<60で売る": lambda a, j, info, s, tr: (np.isnan(s[j]) or s[j] < 60),
        "点数<70で売る": lambda a, j, info, s, tr: (np.isnan(s[j]) or s[j] < 70),
        "6ヶ月たったら売る": lambda a, j, info, s, tr: (a + 1 - info["t0"]) >= 120,
        "12ヶ月たったら売る": lambda a, j, info, s, tr: (a + 1 - info["t0"]) >= 245,
    }
    print("\n売り方の比較（上位10銘柄・業種の上限あり・2006年〜）")
    res = {}
    for name, rule in RULES.items():
        c, t = simulate(rule)
        st = stats(c, t)
        res[name] = (c, st)
        print(f"  {name:38s} 年率{st['cagr']:5.1f}% 最大下落{st['mdd']:6.1f}% シャープ{st['sharpe']:.2f} 買った回数{t}")
    for x in (0.15, 0.20, 0.25, 0.30):
        c, t = simulate(RULES["点数<50で売る"], daily_stop=x)
        st = stats(c, t)
        print(f"  点数<50＋高値から-{int(x*100)}%で即売り{'':14s} 年率{st['cagr']:5.1f}% 最大下落{st['mdd']:6.1f}% シャープ{st['sharpe']:.2f} 買った回数{t}")
    for x in (0.20, 0.25):
        c, t = simulate(RULES["いまのアプリ（点数<40 か 200日線割れで売る）"], daily_stop=x)
        st = stats(c, t)
        print(f"  いまのアプリ＋高値から-{int(x*100)}%{'':18s} 年率{st['cagr']:5.1f}% 最大下落{st['mdd']:6.1f}% シャープ{st['sharpe']:.2f} 買った回数{t}")

    # 期間を分けて安定性を見る
    print("\n期間別の年率（2006-12 / 2013-19 / 2020-26）")
    for name in RULES:
        out = []
        for a, b in ((dt.date(2006, 1, 1), dt.date(2013, 1, 1)), (dt.date(2013, 1, 1), dt.date(2020, 1, 1)),
                     (dt.date(2020, 1, 1), dt.date(2027, 1, 1))):
            c, _ = simulate(RULES[name], start=a)
            c = [x for x in c if x[0] < b]
            out.append(stats(c)["cagr"])
        bb = []
        for a, b in ((dt.date(2006, 1, 1), dt.date(2013, 1, 1)), (dt.date(2013, 1, 1), dt.date(2020, 1, 1)),
                     (dt.date(2020, 1, 1), dt.date(2027, 1, 1))):
            c = [x for x in ew_bench(a) if x[0] < b]
            bb.append(stats(c)["cagr"])
        print(f"  {name:38s} " + " / ".join(f"{o:5.1f}%(基準比{o-q:+.1f})" for o, q in zip(out, bb)))


if __name__ == "__main__":
    main()
