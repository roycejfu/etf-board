#!/usr/bin/env python3
"""重点ETF资金看板 · 数据构建脚本

数据源
- 份额：东方财富妙想 MCP（mx_fund_finance_data「场内流通份额」，经 WorkBuddy 本地连接器代理调用）
- 日线成交额/收盘价：腾讯行情 newfqkline（不复权）
- 分钟成交额：东方财富 push2 trends2（ndays=5，逐日归档，滚动积累近20日）；东财失败时改用腾讯分钟（仅当日）
- 宽基ETF名单与跟踪指数：东方财富 ETF 选择器（ETF_TYPE_CODE=002）
- 宽基汇总校验：东方财富 RPT_ETF_NETINFLOW（宽基汇总）

净流入（申赎口径）= (当日场内份额 − 前一日场内份额) × 当日收盘价；上市首日、份额折算日剔除。
用法：python3 build_data.py            # 增量更新（默认回补最近 15 个交易日份额）
      python3 build_data.py --full     # 全量回补（2023-10 起）
"""
import json, os, sys, time, datetime as dt, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor

import mx_client

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
MIN_DIR = os.path.join(DATA, "minute")
os.makedirs(MIN_DIR, exist_ok=True)

KEY20 = [  # 代码, 名称, 指数分类
    ("588000", "科创50ETF华夏", "科创50"), ("159915", "创业板ETF易方达", "创业板指"),
    ("510300", "沪深300ETF华泰柏瑞", "沪深300"), ("510050", "上证50ETF华夏", "上证50"),
    ("512100", "中证1000ETF南方", "中证1000"), ("510500", "中证500ETF南方", "中证500"),
    ("510310", "沪深300ETF易方达", "沪深300"), ("159919", "沪深300ETF嘉实", "沪深300"),
    ("563360", "A500ETF华泰柏瑞", "中证A500"), ("159338", "中证A500ETF国泰", "中证A500"),
    ("159352", "A500ETF南方", "中证A500"), ("159361", "A500ETF易方达", "中证A500"),
    ("159949", "创业板50ETF华安", "创业板50"), ("588080", "科创50ETF易方达", "科创50"),
    ("159845", "中证1000ETF华夏", "中证1000"), ("512050", "A500ETF华夏", "中证A500"),
    ("159952", "创业板ETF广发", "创业板指"), ("560010", "中证1000ETF广发", "中证1000"),
    ("510210", "上证指数ETF富国", "上证指数"), ("159633", "中证1000ETF易方达", "中证1000"),
]
KEY_CODES = [k[0] for k in KEY20]
HIST_START = "2023-10-09"
BROAD_START = "2026-01-05"
BROAD_MIN_SCALE = 5e8  # 宽基全市场口径：规模≥5亿的宽基ETF（覆盖>99%规模）
CAT_ORDER = ["沪深300", "中证A500", "中证1000", "中证500", "科创50", "创业板指", "上证50",
             "创业板50", "科创创业50", "上证指数", "中证2000", "科创100", "上证180", "中证A50", "其他宽基"]

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def get_json(url, headers=None, timeout=20, retry=3):
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            if i == retry - 1:
                raise
            time.sleep(1.5)


def mkt(code):
    return "sh" if code[0] in "56" else "sz"


def load(name, default):
    p = os.path.join(DATA, name)
    return json.load(open(p)) if os.path.exists(p) else default


def save(name, obj):
    json.dump(obj, open(os.path.join(DATA, name), "w"), ensure_ascii=False, separators=(",", ":"))


# ---------------- 名单 ----------------
def broad_universe():
    p = dict(type="RPTA_APP_ETFSELECT", source="SECURITIES", client="APP", p=1, ps=3000,
             sty="ETF_TYPE_CODE,SECURITY_CODE,SECURITY_NAME_ABBR,ETF_SCALE,INDEXNAME,DERIVE_INDEX_CODE")
    rows = get_json("https://datacenter.eastmoney.com/stock/etfselector/api/data/get?" + urllib.parse.urlencode(p))["result"]["data"]
    out = {}
    for r in rows:
        if r["ETF_TYPE_CODE"] != "002" or (r["ETF_SCALE"] or 0) < BROAD_MIN_SCALE:
            continue
        cat = r["INDEXNAME"] if r["INDEXNAME"] in CAT_ORDER else "其他宽基"
        out[r["SECURITY_CODE"]] = {"name": r["SECURITY_NAME_ABBR"], "cat": cat, "scale": r["ETF_SCALE"]}
    for c, n, cat in KEY20:
        out.setdefault(c, {"name": n, "cat": cat, "scale": 0})
    return out


# ---------------- 日线 ----------------
def kline(sym):
    url = f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param={sym},day,,,2000,"
    d = get_json(url)["data"][sym]
    arr = d.get("day") or d.get("qfqday") or []
    res = {}
    for x in arr:
        if x[0] < "2023-09-01":
            continue
        amt = float(x[8]) * 1e4 if len(x) > 8 and x[8] not in ("", None) else None  # 万元→元
        vol = float(x[5]) * 100  # 手→份
        turn = float(x[7]) if len(x) > 7 and x[7] not in ("", None) else 0.0  # %，腾讯以「前一日份额」为分母
        res[x[0]] = [float(x[2]), amt, vol, turn]
    return res


# ---------------- 份额（上交所官方，沪市） ----------------
SSE_DIR = os.path.join(DATA, "sse")
os.makedirs(SSE_DIR, exist_ok=True)


def sse_day(d):
    p = os.path.join(SSE_DIR, d + ".json")
    if os.path.exists(p):
        return d, json.load(open(p))
    url = ("http://query.sse.com.cn/commonQuery.do?isPagination=true&pageHelp.pageSize=1500&pageHelp.pageNo=1"
           "&pageHelp.beginPage=1&pageHelp.cacheSize=1&sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L&STAT_DATE=" + d)
    try:
        rows = get_json(url, headers={"Referer": "http://www.sse.com.cn/"}, timeout=40)["pageHelp"]["data"]
    except Exception as e:
        print("   SSE fail", d, e); return d, None
    m = {r["SEC_CODE"]: float(r["TOT_VOL"]) * 1e4 for r in rows if r.get("TOT_VOL") and r.get("STAT_DATE") == d}
    if m:
        json.dump(m, open(p, "w"), separators=(",", ":"))
    return d, m or None


def selector_shares():
    """东财 ETF 选择器最新份额（T-1，全精度）"""
    p = dict(type="RPTA_APP_ETFSELECT", source="SECURITIES", client="APP", p=1, ps=3000,
             sty="SECURITY_CODE,DEC_TOTALSHARE")
    rows = get_json("https://datacenter.eastmoney.com/stock/etfselector/api/data/get?" + urllib.parse.urlencode(p))["result"]["data"]
    return {r["SECURITY_CODE"]: float(r["DEC_TOTALSHARE"]) for r in rows if r.get("DEC_TOTALSHARE")}


def index_kline():
    d = get_json("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,2023-09-01,2030-12-31,2000,")["data"]["sh000001"]["day"]
    return {x[0]: float(x[2]) for x in d}


# ---------------- 份额（妙想） ----------------
def parse_share(s):
    if s is None:
        return None
    s = str(s).replace(",", "").replace("份", "").strip()
    try:
        if s.endswith("亿"): return float(s[:-1]) * 1e8
        if s.endswith("万"): return float(s[:-1]) * 1e4
        return float(s)
    except ValueError:
        return None


def mx_shares(codes, start, end):
    """分批查询场内流通份额 -> {code: {date: shares}}"""
    out = {}
    for i in range(0, len(codes), 150):
        batch = codes[i:i + 150]
        q = ",".join(f"{c}.{'SH' if mkt(c) == 'sh' else 'SZ'}" for c in batch) + \
            f" {start[:4]}年{int(start[5:7])}月{int(start[8:])}日至{end[:4]}年{int(end[5:7])}月{int(end[8:])}日每日场内流通份额"
        for attempt in range(3):
            try:
                r = mx_client.call("mx_fund_finance_data", q); break
            except Exception as e:
                print("  mx retry", attempt, e); time.sleep(3)
        else:
            continue
        for sheet in r.get("data", []):
            cols = sheet["columns"]
            for item in sheet["items"]:
                # 两种布局：行=基金(列=日期) 或 行=指标(sheet 名含基金)
                label = item[0]
                code = next((c for c in batch if f"{c}." in label), None)
                if code is None:
                    code = next((c for c in batch if f"{c}." in cols[0]), None)
                if code is None:
                    continue
                for dte, v in zip(cols[1:], item[1:]):
                    val = parse_share(v)
                    if val:
                        out.setdefault(code, {})[dte] = val
    return out


def windows(start, end, months):
    s = dt.date.fromisoformat(start); e = dt.date.fromisoformat(end)
    while s <= e:
        m = s.month - 1 + months
        nxt = dt.date(s.year + m // 12, m % 12 + 1, 1)
        yield s.isoformat(), min(e, nxt - dt.timedelta(days=1)).isoformat()
        s = nxt


# ---------------- 分钟 ----------------
def em_trends(secid, ndays=5):
    url = ("https://push2his.eastmoney.com/api/qt/stock/trends2/get?fields1=f1,f2,f3,f4,f5,f6,f7,f8"
           f"&fields2=f51,f53,f57&iscr=0&ndays={ndays}&secid={secid}")
    d = get_json(url)["data"]
    return d.get("trends") or [], d.get("preClose")


def tx_minute(sym):
    """腾讯分钟（仅当日）：返回 (date, {hm: 分钟成交额元}, {hm: 价格}, 昨收)。数据为累计量，逐分钟差分。"""
    d = get_json(f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}")["data"][sym]
    raw = d["data"]["data"]; ds = d["data"]["date"]
    date = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
    amt, px, prev = {}, {}, 0.0
    for row in raw:
        hhmm, p, _v, a = row.split(" ")
        hm = hhmm[:2] + ":" + hhmm[2:]
        a = float(a)
        if hm != "13:00":  # 腾讯 13:00 为午间占位，与 11:30 累计相同
            amt[hm] = max(a - prev, 0.0); px[hm] = float(p)
        prev = a
    return date, amt, px, float(d["qt"][sym][4])


def _minutes_em():
    secids = [("1." if mkt(c) == "sh" else "0.") + c for c in KEY_CODES]
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(em_trends, secids))
    idx, _ = em_trends("1.000001")
    days = {}
    for trends, _pc in res:
        for t in trends:
            ts, close, amt = t.split(",")
            d, hm = ts.split(" ")
            days.setdefault(d, {}).setdefault(hm, 0.0)
            days[d][hm] += float(amt)
    idx_map = {}
    for t in idx:
        ts, close, _ = t.split(",")
        d, hm = ts.split(" ")
        idx_map.setdefault(d, {})[hm] = float(close)
    if not days or not idx_map:
        raise RuntimeError("东财分时返回为空")
    return days, idx_map


def _minutes_tx():
    syms = [mkt(c) + c for c in KEY_CODES]
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(tx_minute, syms))
    d0, _, ipx, _ = tx_minute("sh000001")
    days = {}
    for d, amt, _px, _pc in res:
        if d != d0:
            continue  # 停牌/日期不一致的不并入
        for hm, a in amt.items():
            days.setdefault(d, {}).setdefault(hm, 0.0)
            days[d][hm] += a
    return days, {d0: ipx}


def update_minutes():
    """归档分钟数据；返回当天盘中记录（盘中不落盘，仅放入 board.json 的 mtoday 供页面兜底）。"""
    try:
        days, idx_map = _minutes_em()
        src = "东财"
    except Exception as e:
        print("   东财分时失败", e, "→ 改用腾讯分钟")
        days, idx_map = _minutes_tx()
        src = "腾讯"
    today = dt.date.today().isoformat()
    now_hm = dt.datetime.now().strftime("%H:%M")
    mtoday = None
    for d, mm in days.items():
        times = sorted(mm)
        rec = {"date": d, "t": times, "amt": [round(mm[t] / 1e8, 4) for t in times],
               "idx": [idx_map.get(d, {}).get(t) for t in times]}
        if d == today and now_hm < "15:01":
            mtoday = {**rec, "asof": now_hm, "src": src}
            continue  # 盘中不归档当天
        json.dump(rec, open(os.path.join(MIN_DIR, f"{d}.json"), "w"), separators=(",", ":"))
    print(f"   分钟来源 {src}，日期 {sorted(days)}" + (f"，当日盘中截至 {now_hm}" if mtoday else ""))
    return mtoday


# ---------------- 主流程 ----------------
def main(full=False):
    today = dt.date.today().isoformat()
    print("1) 名单")
    uni = broad_universe()
    save("universe.json", uni)
    all_codes = sorted(uni)
    print(f"   宽基(规模≥5亿) {len(all_codes)} 只")

    print("2) 日线")
    with ThreadPoolExecutor(10) as ex:
        kl = dict(zip(all_codes, ex.map(lambda c: kline(mkt(c) + c), all_codes)))
    sh = index_kline()
    # 腾讯指数日线收盘后更新较晚：用东财分时补齐 ETF 已有、指数缺失的当天
    extra = sorted({d for c in KEY_CODES for d in kl.get(c, {}) if d >= HIST_START} - set(sh))
    if extra:
        try:
            tr, _ = em_trends("1.000001", 1)
            if tr:
                ts, close, _a = tr[-1].split(",")
                if ts[:10] in extra:
                    sh[ts[:10]] = float(close)
        except Exception as e:
            print("   指数补齐失败(东财)", e, "→ 改用腾讯实时行情")
        if any(d not in sh for d in extra):
            try:  # 备用源：腾讯实时行情，字段 3=现价，30=时间戳 YYYYMMDDhhmmss
                req = urllib.request.Request("https://qt.gtimg.cn/q=sh000001", headers=UA)
                with urllib.request.urlopen(req, timeout=15) as r:
                    q = r.read().decode("gbk", "ignore").split('"')[1].split("~")
                qd = f"{q[30][:4]}-{q[30][4:6]}-{q[30][6:8]}"
                if qd in extra and float(q[3]) > 0:
                    sh[qd] = float(q[3])
                    print(f"   指数补齐(腾讯) {qd} {q[3]}")
            except Exception as e:
                print("   指数补齐失败(腾讯)", e)

    print("3) 份额")
    trade_days = sorted(d for d in sh if d >= HIST_START)
    shares = load("shares.json", {})          # 精确份额：妙想(历史) + 上交所 + 东财快照
    if full and "--mx" in sys.argv:           # 妙想有积分时可全量重刷（深市精确值）
        for s, e in windows(HIST_START, today, 4):
            for c, m in mx_shares(KEY_CODES, s, e).items():
                shares.setdefault(c, {}).update(m)
    # 3a 上交所：沪市全部 ETF，逐日
    need = [d for d in trade_days if d >= "2025-09-01" and d <= today]
    with ThreadPoolExecutor(8) as ex:
        for d, m in ex.map(sse_day, need):
            if not m:
                continue
            for c in all_codes:
                if mkt(c) == "sh" and c in m:
                    shares.setdefault(c, {})[d] = m[c]
    sse_last = max((d for d in need if os.path.exists(os.path.join(SSE_DIR, d + ".json"))), default=None)
    # 3b 东财选择器快照（T-1），作为深市锚点
    try:
        snap = selector_shares()
        if sse_last:
            for c in all_codes:
                if mkt(c) != "sz" or c not in snap:
                    continue
                # 快照日期未知：用「次日成交量/换手率」反推份额匹配，选最接近的交易日（偏差需 <3%）
                k = kl.get(c, {}); ks = [d for d in trade_days if d in k]
                best = None
                for d in [x for x in ks if x <= sse_last][-2:]:
                    i = ks.index(d)
                    if i + 1 < len(ks) and k[ks[i + 1]][3] > 0:
                        imp = k[ks[i + 1]][2] / (k[ks[i + 1]][3] / 100)
                        dev = abs(snap[c] / imp - 1)
                        if dev < 0.03 and (best is None or dev < best[1]):
                            best = (d, dev)
                if best:
                    shares.setdefault(c, {})[best[0]] = snap[c]
    except Exception as e:
        print("   selector 失败", e)
    save("shares.json", shares)

    # 3c 深市缺口：份额(d) ≈ 成交量(d+1) / 换手率(d+1)，两端用精确值做比例锚定
    est_flag = {}
    full_sh = {}
    for c in all_codes:
        exact = dict(shares.get(c, {}))
        k = kl.get(c, {})
        if mkt(c) == "sz":
            ks = [d for d in trade_days if d in k]
            implied = {}
            for a, b in zip(ks, ks[1:]):
                if k[b][3] > 0:
                    implied[a] = k[b][2] / (k[b][3] / 100)
            anchors = sorted(d for d in exact if d in implied)
            filled = dict(exact)
            gaps = [d for d in ks if d not in exact and d in implied and (not anchors or d > anchors[0] or d >= "2025-12-01")]
            for d in gaps:
                lo = max((a for a in anchors if a < d), default=None)
                hi = min((a for a in anchors if a > d), default=None)
                rlo = exact[lo] / implied[lo] if lo else None
                rhi = exact[hi] / implied[hi] if hi else None
                if rlo and rhi:
                    w = (ks.index(d) - ks.index(lo)) / max(1, ks.index(hi) - ks.index(lo))
                    r = rlo + (rhi - rlo) * w
                else:
                    r = rlo or rhi or 1.0
                filled[d] = implied[d] * r
                est_flag.setdefault(c, set()).add(d)
            full_sh[c] = filled
        else:
            full_sh[c] = exact

    print("4) 净流入计算")

    def flows(code):
        sm, k = full_sh.get(code, {}), kl.get(code, {})
        res, prev = {}, None
        for d in trade_days:
            s = sm.get(d)
            if s is None or d not in k:
                continue
            if prev is not None:
                ps, pp = prev
                p = k[d][0]
                ratio, pr = s / ps, p / pp if pp else 1
                if (ratio > 1.6 and pr < 0.75) or (ratio < 0.6 and pr > 1.4):
                    res[d] = 0.0  # 份额折算
                else:
                    res[d] = (s - ps) * p
            prev = (s, k[d][0])
        return res

    fl = {c: flows(c) for c in all_codes}
    last_share_day = sse_last or max((max(v) for v in shares.values() if v), default=None)
    est_from = min((min(v) for c, v in est_flag.items() if c in KEY_CODES and v), default=None)

    # 20只合计
    amt20, in20 = [], []
    for d in trade_days:
        a = sum(kl[c][d][1] or 0 for c in KEY_CODES if d in kl.get(c, {}))
        amt20.append(round(a / 1e8, 2) if a else None)
        if last_share_day and d <= last_share_day:
            in20.append(round(sum(fl[c].get(d, 0) for c in KEY_CODES) / 1e8, 2))
        else:
            in20.append(None)

    # 全市场宽基分类
    bdays = [d for d in trade_days if d >= BROAD_START and last_share_day and d <= last_share_day]
    cats = {c: [0.0] * len(bdays) for c in CAT_ORDER}
    for code in all_codes:
        cat = uni[code]["cat"]
        for i, d in enumerate(bdays):
            cats[cat][i] += fl[code].get(d, 0)
    cats = {k: [round(x / 1e8, 2) for x in v] for k, v in cats.items()}
    cats = {k: v for k, v in cats.items() if any(abs(x) > 0.01 for x in v)}

    # EM 宽基汇总校验
    try:
        p = dict(reportName="RPT_ETF_NETINFLOW", columns="SECURITY_TYPE,TRADE_DATE,PERIODESTIMATENETINFLOW1",
                 pageSize=500, filter=f"(TRADE_DATE>='{BROAD_START}')(SECURITY_TYPE in (\"宽基\",\"汇金\"))",
                 sortColumns="TRADE_DATE", sortTypes=-1)
        rows = get_json("https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(p))["result"]["data"]
        em = {}
        for r in rows:
            em.setdefault(r["SECURITY_TYPE"], {})[r["TRADE_DATE"][:10]] = round(r["PERIODESTIMATENETINFLOW1"] / 1e8, 1)
    except Exception as e:
        print("   EM 汇总失败", e); em = {}

    # 单只 ETF 指标
    win = [d for d in trade_days if last_share_day and d <= last_share_day]
    etfs = []
    for c, n, cat in KEY20:
        k = kl.get(c, {})
        ds = [d for d in trade_days if d in k]
        last20 = [k[d][1] for d in ds[-20:] if k[d][1]]
        sm = {d: v for d, v in full_sh.get(c, {}).items() if d <= (last_share_day or "9")}
        sd = max(sm) if sm else None
        f = fl[c]
        etfs.append({
            "code": c, "name": n, "cat": cat, "est": c in est_flag, "secid": ("1." if mkt(c) == "sh" else "0.") + c,
            "avg20": round(sum(last20) / len(last20) / 1e8, 2) if last20 else None,
            "shares": round(sm[sd] / 1e8, 2) if sd else None, "share_date": sd,
            "in1": round(f.get(last_share_day, 0) / 1e8, 2) if last_share_day else None,
            "in5": round(sum(f.get(d, 0) for d in win[-5:]) / 1e8, 2),
            "in20": round(sum(f.get(d, 0) for d in win[-20:]) / 1e8, 2),
            "q": {"date": ds[-1], "amt": round((k[ds[-1]][1] or 0) / 1e8, 2),
                  "pct": round((k[ds[-1]][0] / k[ds[-2]][0] - 1) * 100, 2)} if len(ds) > 1 else None,
        })

    # 分钟
    print("5) 分钟归档")
    mtoday = None
    try:
        mtoday = update_minutes()
    except Exception as e:
        print("   分钟失败", e)
    mfiles = sorted(f for f in os.listdir(MIN_DIR) if f.endswith(".json"))
    minutes = [json.load(open(os.path.join(MIN_DIR, f))) for f in mfiles]
    recent = [m for m in minutes if m["date"] < today or dt.datetime.now().strftime("%H:%M") >= "15:01"][-20:]
    # 近N日累计均值曲线
    grid = minutes[-1]["t"] if minutes else []
    avg_cum = []
    if recent:
        cums = []
        for m in recent:
            acc, mp = 0, {}
            for t, a in zip(m["t"], m["amt"]):
                acc += a; mp[t] = acc
            cums.append(mp)
        for t in grid:
            vals = [c[t] for c in cums if t in c]
            avg_cum.append(round(sum(vals) / len(vals), 2) if vals else None)
    # 放量案例：所有归档日的 15 分钟聚合
    cases = []
    for m in minutes:
        b_t, b_a, b_i = [], [], []
        for i in range(0, len(m["t"]), 15):
            seg = m["amt"][i:i + 15]
            b_t.append(m["t"][i]); b_a.append(round(sum(seg) / len(seg), 3))
            b_i.append(m["idx"][min(i + 14, len(m["idx"]) - 1)])
        cases.append({"date": m["date"], "total": round(sum(m["amt"]), 1), "t": b_t, "amt": b_a, "idx": b_i})

    # ---------------- 结论所需的事实摘要（供撰写「观察与结论」） ----------------
    facts = {}
    try:
        ld = last_share_day; li = trade_days.index(ld)
        hist = [(d, v) for d, v in zip(trade_days, in20) if v is not None]
        vals = sorted((v for _, v in hist), reverse=True)
        streak, acc = 0, 0.0
        for d, v in reversed(hist):
            if v > 0: streak += 1; acc += v
            else: break
        roll20 = [sum(v for _, v in hist[i - 19:i + 1]) for i in range(19, len(hist))]
        top5 = sorted(hist, key=lambda x: -x[1])[:5]
        bi = bdays.index(ld) if ld in bdays else None
        cat_ld = sorted(((k, v[bi]) for k, v in cats.items()), key=lambda x: -x[1]) if bi is not None else []
        tdx = len(trade_days) - 1
        amt_hist = sorted((a for a in amt20 if a), reverse=True)
        avg20_amt = sum(e["avg20"] or 0 for e in etfs)
        def prof(m):
            a = m["amt"]; t = m["t"]; tot = sum(a) or 1
            first = sum(x for x, tt in zip(a, t) if tt <= "10:00"); last = sum(x for x, tt in zip(a, t) if tt >= "14:30")
            pm = sum(x for x, tt in zip(a, t) if tt >= "13:00")
            return {"total": round(tot, 1), "first30_pct": round(first / tot * 100, 1),
                    "last30_pct": round(last / tot * 100, 1), "pm_pct": round(pm / tot * 100, 1)}
        mprof = {m["date"]: prof(m) for m in minutes[-6:]}
        facts = {
            "share_day": ld,
            "in20_ld": in20[li], "in20_rank": vals.index(in20[li]) + 1, "in20_n": len(vals),
            "in20_top5": top5, "inflow_streak_days": streak, "inflow_streak_sum": round(acc, 1),
            "in20_5d": round(sum(v for _, v in hist[-5:]), 1), "in20_20d": round(sum(v for _, v in hist[-20:]), 1),
            "roll20_max": round(max(roll20), 1) if roll20 else None,
            "roll20_max_date": hist[19 + roll20.index(max(roll20))][0] if roll20 else None,
            "tuodi_last20": sum(1 for _, v in hist[-20:] if v > 100),
            "broad_ld_by_cat": cat_ld, "broad_ld_total": round(sum(v for _, v in cat_ld), 1),
            "em_broad_ld": em.get("宽基", {}).get(ld), "em_huijin_ld": em.get("汇金", {}).get(ld),
            "last_trade_day": trade_days[tdx], "amt20_last": amt20[tdx], "amt20_avg20": round(avg20_amt, 1),
            "amt20_rank": amt_hist.index(amt20[tdx]) + 1 if amt20[tdx] in amt_hist else None,
            "sh_last": sh[trade_days[tdx]], "sh_chg_pct": round((sh[trade_days[tdx]] / sh[trade_days[tdx - 1]] - 1) * 100, 2),
            "sh_5d_chg_pct": round((sh[trade_days[tdx]] / sh[trade_days[tdx - 5]] - 1) * 100, 2),
            "etf_today": [(e["name"], e["q"]["pct"], e["q"]["amt"], round(e["q"]["amt"] / e["avg20"], 2) if e["avg20"] else None, e["in1"])
                          for e in sorted(etfs, key=lambda x: -(x["q"] or {}).get("amt", 0)) if e["q"]],
            "minute_profile": mprof,
        }
    except Exception as e:
        print("   facts 失败", e)

    out = {
        "facts": facts,
        "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "last_share_day": last_share_day, "est_from": est_from,
        "etfs": etfs,
        "daily": {"dates": trade_days, "amt20": amt20, "in20": in20, "sh": [sh[d] for d in trade_days]},
        "broad": {"dates": bdays, "cats": cats, "em_broad": em.get("宽基", {}), "em_huijin": em.get("汇金", {}),
                  "universe_n": len(all_codes)},
        "minute": {"grid": grid, "avg_cum": avg_cum, "avg_n": len(recent)},
        "cases": cases,
        "mraw": minutes[-20:],
        "mtoday": mtoday,
        "names": {c: n for c, n, _ in KEY20},
    }
    save("board.json", out)
    with open(os.path.join(ROOT, "board_data.js"), "w") as f:
        f.write("window.BOARD=" + json.dumps(out, ensure_ascii=False, separators=(",", ":")) + ";")
    json.dump(facts, open(os.path.join(DATA, "facts.json"), "w"), ensure_ascii=False, indent=1)
    print("完成：", out["updated"], "份额最新日", last_share_day,
          "| 20只最新净流入", in20[trade_days.index(last_share_day)] if last_share_day in trade_days else None)


if __name__ == "__main__":
    main(full="--full" in sys.argv)
