# -*- coding: utf-8 -*-
"""売り方の詰め：売るしきい値 × 見直しの頻度 × 銘柄数 × 相場フィルタ（手数料込み）。

手数料: 日本のネット証券の米国株は約定代金の 0.495%（上限 22 ドル）＋為替の片道 0.25 円/ドル前後。
       往復でおよそ 1.3% とみて、売買のたびに差し引く。
"""
import sys, os, math, statistics, datetime as dt
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import study_rules as R   # noqa: E402

COST_RT = 0.013


def main():
    days, tick, P, meta, spy = R.load()
    F = R.ffill(P)
    n, m = F.shape
    cs = np.nancumsum(np.nan_to_num(F), axis=0)
    ma200 = np.full_like(F, np.nan); ma200[200:] = (cs[200:] - cs[:-200]) / 200
    cnt = np.cumsum(~np.isnan(P), axis=0)
    valid = np.zeros_like(F, dtype=bool); valid[260:] = (cnt[260:] - cnt[:-260]) >= 250
    spy = np.array(spy, dtype=float)
    spy_ma = np.full(n, np.nan)
    c2 = np.cumsum(spy); spy_ma[200:] = (c2[200:] - c2[:-200]) / 200
    sec = [meta[t][0] for t in tick]; sub = [meta[t][1] for t in tick]

    def pr(v):
        o = np.full(v.shape, np.nan); ok = ~np.isnan(v)
        if ok.sum() > 1:
            o[ok] = v[ok].argsort().argsort() / (ok.sum() - 1) * 100
        return o

    cache = {}

    def score(i):
        if i not in cache:
            ok = valid[i]
            s = (0.5 * pr(np.where(ok, F[i] / F[i - 126] - 1, np.nan))
                 + 0.3 * pr(np.where(ok, F[i] / ma200[i] - 1, np.nan))
                 + 0.2 * pr(np.where(ok, F[i - 21] / F[i - 252] - 1, np.nan)))
            tr = np.where(ok, F[i] / ma200[i] - 1, np.nan)
            breadth = np.nanmean(tr[ok] > 0) * 100
            cache[i] = (s, tr, breadth)
        return cache[i]

    def pick(i, k, held):
        s, tr, _ = score(i)
        order = [j for j in np.argsort(-np.nan_to_num(s, nan=-1)) if not np.isnan(s[j])]
        chosen = list(held); us, ub = {}, {}
        for j in chosen:
            us[sec[j]] = us.get(sec[j], 0) + 1; ub[sub[j]] = ub.get(sub[j], 0) + 1
        for j in order:
            if len(chosen) >= k: break
            if j in chosen or s[j] < 70 or tr[j] <= 0: continue
            if us.get(sec[j], 0) >= 5 or ub.get(sub[j], 0) >= 2: continue
            chosen.append(j); us[sec[j]] = us.get(sec[j], 0) + 1; ub[sub[j]] = ub.get(sub[j], 0) + 1
        return chosen

    def checkpoints(freq, start):
        pts = []
        for i in range(300, n - 1):
            if days[i] < start: continue
            if freq == "m" and days[i].month != days[i + 1].month: pts.append(i)
            if freq == "w" and days[i].weekday() >= days[i + 1].weekday(): pts.append(i)
            if freq == "q" and days[i].month != days[i + 1].month and days[i].month % 3 == 0: pts.append(i)
        return pts

    def sim(sell_below=70, k=10, freq="m", regime=None, start=dt.date(2006, 1, 1), end=None):
        pts = checkpoints(freq, start)
        if end: pts = [i for i in pts if days[i] < end]
        eq, curve, pos, buys = 1.0, [], set(), 0
        for a, b in zip(pts, pts[1:] + [n - 2]):
            s, tr, br = score(a)
            for j in list(pos):
                if np.isnan(s[j]) or s[j] < sell_below:
                    pos.discard(j); eq *= 1 - COST_RT / 2 / k
            risk_off = False
            if regime == "spy" and spy[a] < spy_ma[a]: risk_off = True
            if regime == "breadth" and br < 50: risk_off = True
            if regime == "spy_exit" and spy[a] < spy_ma[a]:
                for j in list(pos):
                    pos.discard(j); eq *= 1 - COST_RT / 2 / k
                risk_off = True
            if not risk_off:
                for j in pick(a, k, list(pos)):
                    if j not in pos:
                        pos.add(j); buys += 1; eq *= 1 - COST_RT / 2 / k
            if pos:
                r = sum(F[b + 1, j] / F[a + 1, j] - 1 for j in pos) / k
                eq *= 1 + r
            curve.append((days[b + 1], eq))
        return curve, buys

    def st(curve):
        e = [x for _, x in curve]
        yrs = (curve[-1][0] - curve[0][0]).days / 365.25
        pk, mdd = 0, 0
        for x in e:
            pk = max(pk, x); mdd = min(mdd, x / pk - 1)
        return (e[-1] ** (1 / yrs) - 1) * 100, mdd * 100

    def ew(start, end=None):
        pts = checkpoints("m", start)
        if end: pts = [i for i in pts if days[i] < end]
        eq, curve = 1.0, []
        for a, b in zip(pts, pts[1:] + [n - 2]):
            ok = valid[a]
            eq *= 1 + np.nanmean(F[b + 1, ok] / F[a + 1, ok] - 1)
            curve.append((days[b + 1], eq))
        return curve

    PER = [(dt.date(2006, 1, 1), dt.date(2013, 1, 1)), (dt.date(2013, 1, 1), dt.date(2020, 1, 1)),
           (dt.date(2020, 1, 1), None)]
    bench = [st(ew(a, b))[0] for a, b in PER]
    cb, mb = st(ew(dt.date(2006, 1, 1)))
    print(f"基準（均等買い）年率{cb:.1f}% 最大下落{mb:.1f}%  期間別 " + " / ".join(f"{x:.1f}%" for x in bench))

    def line(label, **kw):
        c, buys = sim(**kw)
        cg, md = st(c)
        per = [st(sim(**{**kw, "start": a, "end": b})[0])[0] for a, b in PER]
        yrs = (c[-1][0] - c[0][0]).days / 365.25
        print(f"  {label:34s} 年率{cg:5.1f}% 最大下落{md:6.1f}% 年{buys/yrs:4.1f}回買う | 基準比 " +
              " / ".join(f"{p-q:+5.1f}" for p, q in zip(per, bench)))

    print("\n== 売るしきい値（月1回見直し・10銘柄・手数料込み）==")
    for th in (40, 50, 60, 65, 70, 75, 80):
        line(f"{th}点を割ったら売る", sell_below=th)
    print("\n== 見直しの頻度（70点）==")
    for fq, nm in (("w", "毎週"), ("m", "毎月"), ("q", "3ヶ月ごと")):
        line(f"{nm}見直す", freq=fq)
    print("\n== 持つ銘柄数（70点・毎月）==")
    for k in (5, 10, 15, 20):
        line(f"{k}銘柄", k=k)
    print("\n== 相場全体が弱い時（70点・毎月・10銘柄）==")
    line("何もしない")
    line("S&P500が200日線割れ→新規買いを止める", regime="spy")
    line("上昇銘柄が5割未満→新規買いを止める", regime="breadth")
    line("S&P500が200日線割れ→全部売る", regime="spy_exit")


if __name__ == "__main__":
    main()
