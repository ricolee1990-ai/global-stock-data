"""
海外日报数据采集脚本 — daily_fetch.py v3
架构：
  第一层 全板块扫描  US 11个板块(SPDR ETF) + HK 11个板块(Yahoo screener批量)
  第二层 波动排序    按|涨跌幅|排序，自动找当日焦点板块
  第三层 个股钻入    焦点板块 top 10 个股
  内部信号区        异动股清单，仅供Claude搜索催化剂用，不出现在日报正文

依赖: pip install requests
运行: python daily_fetch.py
"""

import requests, re, sys, time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

UA      = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
BEIJING = timezone(timedelta(hours=8))

# ── Yahoo Finance 11个板块（Yahoo命名，与GICS略有差异）──────────────
# 美股 SPDR ETF 代理（直接反映板块涨跌，零crumb）
US_SECTORS = {
    "Technology":             "XLK",
    "Communication Services": "XLC",
    "Consumer Cyclical":      "XLY",   # GICS: Consumer Discretionary
    "Healthcare":             "XLV",
    "Financial Services":     "XLF",   # GICS: Financials
    "Industrials":            "XLI",
    "Energy":                 "XLE",
    "Basic Materials":        "XLB",   # GICS: Materials
    "Real Estate":            "XLRE",
    "Consumer Defensive":     "XLP",   # GICS: Consumer Staples
    "Utilities":              "XLU",
}

# 板块英文→中文映射（美股和港股共用）
SECTOR_CN = {
    "Technology":             "科技",
    "Communication Services": "通信服务",
    "Consumer Cyclical":      "非必需消费",
    "Healthcare":             "医疗保健",
    "Financial Services":     "金融服务",
    "Industrials":            "工业",
    "Energy":                 "能源",
    "Basic Materials":        "原材料",
    "Real Estate":            "房地产",
    "Consumer Defensive":     "必需消费",
    "Utilities":              "公用事业",
}

def cn_sector(en: str) -> str:
    """把Yahoo英文板块名转成中文，未知板块保留英文"""
    return SECTOR_CN.get(en, en) if en else "—"

# ──────────────────────────────────────────────
# Yahoo crumb session（全局复用）
# ──────────────────────────────────────────────
_yahoo_session = None

def yahoo_session():
    global _yahoo_session
    if _yahoo_session and hasattr(_yahoo_session, "_crumb"):
        return _yahoo_session
    s = requests.Session()
    s.headers["User-Agent"] = UA
    s.get("https://fc.yahoo.com", timeout=10)
    r = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10)
    r.raise_for_status()
    s._crumb = r.text.strip()
    _yahoo_session = s
    return s


# ──────────────────────────────────────────────
# Yahoo v8 chart（零crumb，指数/ETF/大宗通用）
# ──────────────────────────────────────────────
def yahoo_close(symbol: str) -> dict:
    try:
        r = requests.get(
            f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"interval": "1d", "range": "5d"},
            headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        res = r.json().get("chart", {}).get("result", [])
        if not res:
            return {"symbol": symbol, "error": "no data"}
        meta   = res[0].get("meta", {})
        ts     = res[0].get("timestamp", [])
        closes = res[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
        valid  = [(t, c) for t, c in zip(ts, closes) if c is not None]
        if len(valid) < 2:
            return {"symbol": symbol, "error": "insufficient"}
        _, prev = valid[-2]
        last_ts, last = valid[-1]
        return {
            "symbol": symbol,
            "name": meta.get("shortName", symbol),
            "price": round(last, 2),
            "change_pct": round((last - prev) / prev * 100, 2),
            "currency": meta.get("currency", ""),
            "date": datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
        }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}


# ──────────────────────────────────────────────
# 新浪港股指数（真实点位）
# ──────────────────────────────────────────────
def hk_index_sina(sina_code: str, label: str) -> dict:
    """
    新浪港股指数行情
    实测字段: f[5]=收盘价, f[7]=涨跌额(点数,非%), 涨跌%需用点数推算
    公式: prev = price - chg_pts; chg_pct = chg_pts / prev * 100
    """
    try:
        r = requests.get(
            f"https://hq.sinajs.cn/list={sina_code}",
            headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": UA},
            timeout=10)
        r.encoding = "gbk"
        m = re.search(r'"(.+)"', r.text)
        if not m:
            return {"label": label, "error": "empty"}
        f = m.group(1).split(",")
        if len(f) < 8:
            return {"label": label, "error": "short"}
        price    = float(f[5]) if f[5] else 0   # 收盘/最新价（实测f[5]正确）
        chg_pts  = float(f[7]) if f[7] else 0   # 涨跌额（点数，非百分比）
        prev     = price - chg_pts               # 昨收 = 今收 - 涨跌额
        chg_pct  = round(chg_pts / prev * 100, 2) if prev != 0 else 0
        return {"label": label, "price": round(price, 2), "change_pct": chg_pct}
    except Exception as e:
        return {"label": label, "error": str(e)}


# ──────────────────────────────────────────────
# 腾讯港股个股（中文名+行情）
# ──────────────────────────────────────────────
def hk_quote_tencent(code: str) -> dict:
    try:
        r = requests.get(f"https://qt.gtimg.cn/q=r_hk{code}", timeout=8)
        r.encoding = "gbk"
        m = re.search(r'"(.+)"', r.text)
        if not m:
            return {"code": code, "error": "empty"}
        f = m.group(1).split("~")
        if len(f) < 50:
            return {"code": code, "error": "short"}
        return {
            "code": code,
            "name": f[1],
            "price": float(f[3]) if f[3] else 0,
            "change_pct": round(float(f[32]) if f[32] else 0, 2),
            "volume": int(float(f[6])) if f[6] else 0,
        }
    except Exception as e:
        return {"code": code, "error": str(e)}


# ──────────────────────────────────────────────
# Yahoo POST screener（通用，US和HK都用）
# ──────────────────────────────────────────────
def yahoo_screener(exchange_filter: list, n: int = 30,
                   sort_field: str = "percentchange", sort_desc: bool = True,
                   sector: str = None, min_mktcap: int = 1_000_000_000) -> list:
    """
    exchange_filter: ["HKG"] 或 ["NMS","NYQ"] 等
    sector: Yahoo sector名称，None=不过滤
    """
    try:
        s = yahoo_session()
        operands = [
            {"operator": "or", "operands": [
                {"operator": "eq", "operands": ["exchange", ex]}
                for ex in exchange_filter
            ]},
            {"operator": "gte", "operands": ["intradaymarketcap", min_mktcap]},
        ]
        if sector:
            operands.append({"operator": "eq", "operands": ["sector", sector]})

        payload = {
            "offset": 0, "size": n,
            "sortField": sort_field,
            "sortType": "DESC" if sort_desc else "ASC",
            "quoteType": "EQUITY",
            "topOperator": "AND",
            "query": {"operator": "AND", "operands": operands},
            "userId": "", "userIdType": "guid",
        }
        r = s.post(
            "https://query2.finance.yahoo.com/v1/finance/screener",
            params={"crumb": s._crumb, "lang": "en-US",
                    "region": "US", "formatted": "false"},
            json=payload, timeout=20,
        )
        r.raise_for_status()
        quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
        return quotes
    except Exception as e:
        return [{"error": str(e)}]


def parse_quote(q: dict, market: str = "us") -> dict:
    """统一解析Yahoo screener返回的quote"""
    sym = q.get("symbol", "")
    if market == "hk":
        code = sym.replace(".HK", "").zfill(5)
    else:
        code = sym
    return {
        "code":       code,
        "symbol":     sym,
        "name":       (q.get("shortName") or q.get("longName") or sym)[:16],
        "change_pct": round(q.get("regularMarketChangePercent", 0), 2),
        "price":      round(q.get("regularMarketPrice", 0), 2),
        "market_cap": q.get("marketCap", 0),
        "sector":     q.get("sector", ""),
        "volume":     q.get("regularMarketVolume", 0),
        "avg_volume": q.get("averageDailyVolume3Month", 1),
    }


# ──────────────────────────────────────────────
# 格式化工具
# ──────────────────────────────────────────────
def fmt_chg(pct):
    if pct is None: return "N/A"
    return f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%"

def fmt_price(p, cur=""):
    if not p: return "N/A"
    return f"{cur}{p:,.0f}" if p > 10000 else f"{cur}{p:,.2f}"

def signal_bar(pct):
    """把涨跌幅转成直觉性的文字信号"""
    if pct is None: return "—"
    if pct >= 3:   return "强势"
    if pct >= 1:   return "偏强"
    if pct >= -1:  return "平稳"
    if pct >= -3:  return "偏弱"
    return "弱势"


# ──────────────────────────────────────────────
# 主报告
# ──────────────────────────────────────────────

def us_movers_yahoo(n: int, descending: bool = True) -> list:
    """
    美股涨跌幅排名 — 用Yahoo预定义screener(day_gainers/day_losers)
    该接口返回sector字段，POST screener不返回
    """
    try:
        s = yahoo_session()
        scr_id = "day_gainers" if descending else "day_losers"
        r = s.get(
            "https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved",
            params={"count": n + 10, "scrIds": scr_id, "crumb": s._crumb},
            timeout=15,
        )
        r.raise_for_status()
        quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
        # 只保留美股（无.后缀，排除港股.HK、加股.TO等）
        us_only = [q for q in quotes
                   if q.get("exchange","") in ("NMS","NYQ","ASE","NGM","NCM")
                   or "." not in q.get("symbol","")]
        result = []
        for q in us_only[:n]:
            result.append({
                "code":       q.get("symbol",""),
                "name":       (q.get("shortName") or q.get("longName") or q.get("symbol",""))[:16],
                "change_pct": round(q.get("regularMarketChangePercent", 0), 2),
                "price":      round(q.get("regularMarketPrice", 0), 2),
                "sector":     q.get("sector", ""),
            })
        return result if result else [{"error": "day_gainers/losers返回空"}]
    except Exception as e:
        print(f"US movers异常: {e}", file=sys.stderr)
        return [{"error": str(e)}]


def hk_batch_yahoo(n: int = 150) -> list:
    """
    港股批量获取 — 用Yahoo预定义screener(most_actives, region=HK)
    Yahoo POST screener不支持exchange=HKG，改用此方案
    返回带sector字段的股票列表，供板块分组用
    """
    try:
        s = yahoo_session()
        # most_actives按成交量排名，覆盖面最广，含sector字段
        r = s.get(
            "https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved",
            params={
                "count": n,
                "scrIds": "most_actives",
                "region": "HK",
                "lang": "en-HK",
                "crumb": s._crumb,
            },
            timeout=20,
        )
        r.raise_for_status()
        quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
        # 严格过滤：只保留.HK symbol，排除混入的美股ADR
        hk_only = [q for q in quotes if q.get("symbol", "").endswith(".HK")]
        result = [parse_quote(q, "hk") for q in hk_only]
        print(f"HK batch: {len(result)}只股票", file=sys.stderr)
        return result if result else [{"error": "most_actives返回0只HK股票"}]
    except Exception as e:
        print(f"HK batch异常: {e}", file=sys.stderr)
        return [{"error": str(e)}]


def hk_movers_yahoo(n: int, descending: bool = True) -> list:
    """
    港股涨跌幅排名 — 优先东财push2，失败改用Yahoo day_gainers/day_losers
    """
    # 优先试东财
    try:
        r = requests.get("https://push2.eastmoney.com/api/qt/clist/get", params={
            "fs": "m:116", "fields": "f2,f3,f5,f6,f12,f14",
            "pn": 1, "pz": n, "fid": "f3", "po": 1 if descending else 0,
        }, headers={"User-Agent": UA, "Referer": "https://www.eastmoney.com/"}, timeout=8)
        diff = r.json().get("data", {}).get("diff")
        if diff:
            return [{"code": s["f12"], "name": s["f14"],
                     "change_pct": round(s["f3"] / 100, 2), "sector": ""}
                    for s in diff if s.get("f3") is not None]
    except Exception:
        pass

    # 东财失败 → Yahoo预定义screener
    try:
        s = yahoo_session()
        scr_id = "day_gainers" if descending else "day_losers"
        r = s.get(
            "https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved",
            params={"count": n, "scrIds": scr_id,
                    "region": "HK", "lang": "en-HK", "crumb": s._crumb},
            timeout=15,
        )
        r.raise_for_status()
        quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
        hk_only = [q for q in quotes if q.get("symbol", "").endswith(".HK")]
        return [{"code": q["symbol"].replace(".HK","").zfill(5),
                 "name": q.get("shortName",""),
                 "change_pct": round(q.get("regularMarketChangePercent", 0), 2),
                 "sector": q.get("sector", ""),
                 "price": round(q.get("regularMarketPrice", 0), 2)}
                for q in hk_only[:n]]
    except Exception as e:
        return [{"error": str(e)}]


def build_report() -> str:
    now = datetime.now(BEIJING)
    L   = []  # output lines

    L.append("# 海外市场数据快照")
    L.append(f"**采集时间（北京时间）：{now.strftime('%Y-%m-%d %H:%M')}**")
    L.append("> 数据说明：北京时间05:30后采集，港股/美股均已收盘，价格为前日收盘价。")
    L.append("")

    # ════════════════════════════════════════
    # 美股指数
    # ════════════════════════════════════════
    L.append("## 美股基准指数")
    L.append("| 指数 | 收盘 | 涨跌幅 | 日期 |")
    L.append("|------|------|--------|------|")
    for sym, label in [
        ("^GSPC","标普500"), ("^NDX","纳斯达克100"),
        ("^IXIC","纳斯达克综合"), ("^DJI","道琼斯"),
        ("^VIX","VIX恐慌指数"), ("^TNX","10Y美债(%)"),
    ]:
        q = yahoo_close(sym)
        if "error" in q:
            L.append(f"| {label} | ⚠️ | — | — |")
        else:
            L.append(f"| {label} | {fmt_price(q['price'])} | {fmt_chg(q['change_pct'])} | {q['date']} |")
    L.append("")

    # ════════════════════════════════════════
    # 美股11大板块（全覆盖，SPDR ETF代理）
    # ════════════════════════════════════════
    L.append("## 美股11大板块涨跌幅（以SPDR行业ETF为基准）")
    L.append("| 板块 | ETF | 涨跌幅 | 信号 |")
    L.append("|------|-----|--------|------|")

    us_sector_results = []
    for sector, etf in US_SECTORS.items():
        q = yahoo_close(etf)
        if "error" not in q:
            us_sector_results.append({
                "sector": sector, "etf": etf,
                "change_pct": q["change_pct"], "date": q["date"],
            })

    # 按|涨跌幅|降序排列
    us_sector_results.sort(key=lambda x: abs(x["change_pct"]), reverse=True)

    for r in us_sector_results:
        L.append(f"| {cn_sector(r['sector'])} | {r['etf']} | {fmt_chg(r['change_pct'])} | {signal_bar(r['change_pct'])} |")
    L.append("")

    # ════════════════════════════════════════
    # 美股涨跌幅排名 — 预定义screener，含sector字段，显示中文板块名
    # ════════════════════════════════════════
    L.append("## 美股今日涨幅 TOP 15")
    L.append("| Ticker | 名称 | 涨跌幅 | 板块 | 收盘(USD) |")
    L.append("|--------|------|--------|------|-----------|")
    for s in us_movers_yahoo(15, descending=True):
        if "error" in s: break
        L.append(f"| {s['code']} | {s['name']} | {fmt_chg(s['change_pct'])} | {cn_sector(s['sector'])} | {fmt_price(s['price'],'$')} |")
    L.append("")

    L.append("## 美股今日跌幅 TOP 10")
    L.append("| Ticker | 名称 | 涨跌幅 | 板块 | 收盘(USD) |")
    L.append("|--------|------|--------|------|-----------|")
    for s in us_movers_yahoo(10, descending=False):
        if "error" in s: break
        L.append(f"| {s['code']} | {s['name']} | {fmt_chg(s['change_pct'])} | {cn_sector(s['sector'])} | {fmt_price(s['price'],'$')} |")
    L.append("")
    time.sleep(0.5)

    # ════════════════════════════════════════
    # 港股指数（新浪真实点位）
    # ════════════════════════════════════════
    L.append("## 港股基准指数")
    L.append("| 指数 | 收盘 | 涨跌幅 | 日期 |")
    L.append("|------|------|--------|------|")

    # HSI / HSCEI — Yahoo Finance（境外IP可访问，稳定）
    for sym, label in [("^HSI", "恒生指数"), ("^HSCE", "国企指数(H股)")]:
        q = yahoo_close(sym)
        if "error" in q:
            L.append(f"| {label} | ⚠️ {q.get('error','')} | — | — |")
        else:
            L.append(f"| {label} | {fmt_price(q['price'])} | {fmt_chg(q['change_pct'])} | {q['date']} |")

    # HSTECH — 优先Yahoo ^HSTECH，失败回退新浪 hkHSTECH，均失败报错（不用ETF代理）
    hstech_q = yahoo_close("^HSTECH")
    if "error" not in hstech_q:
        L.append(f"| 恒生科技指数 | {fmt_price(hstech_q['price'])} | {fmt_chg(hstech_q['change_pct'])} | {hstech_q['date']} |")
    else:
        sina_q = hk_index_sina("hkHSTECH", "恒生科技指数")
        if "error" not in sina_q:
            now_date = datetime.now(BEIJING).strftime("%Y-%m-%d")
            L.append(f"| 恒生科技指数 | {fmt_price(sina_q['price'])} | {fmt_chg(sina_q['change_pct'])} | {now_date} |")
        else:
            L.append(f"| 恒生科技指数 | ⚠️ Yahoo+Sina均不可用 | — | — |")
    L.append("")

    # ════════════════════════════════════════
    # 港股11大板块（批量拉取，本地分组统计）
    # ════════════════════════════════════════
    L.append("## 港股板块行情（全11板块，按平均涨跌幅排序）")
    L.append("| 板块 | 平均涨跌幅 | 覆盖股数 | 信号 |")
    L.append("|------|-----------|---------|------|")

    # HK批量：用预定义screener(most_actives/region=HK)，POST screener不支持exchange=HKG
    hk_stocks_raw = hk_batch_yahoo(n=150)
    hk_stocks = [s for s in hk_stocks_raw if "error" not in s]

    # 按sector分组
    sector_map = defaultdict(list)
    for s in hk_stocks:
        sec = s["sector"]
        if sec:
            sector_map[sec].append(s)

    hk_sector_results = []
    for sector, stocks in sector_map.items():
        pcts = [s["change_pct"] for s in stocks]
        avg  = round(sum(pcts) / len(pcts), 2)
        hk_sector_results.append({
            "sector": sector, "avg_change": avg,
            "count": len(stocks), "stocks": stocks,
        })

    hk_sector_results.sort(key=lambda x: abs(x["avg_change"]), reverse=True)

    for sr in hk_sector_results:
        L.append(f"| {cn_sector(sr['sector'])} | {fmt_chg(sr['avg_change'])} | {sr['count']}只 | {signal_bar(sr['avg_change'])} |")
    L.append("")

    # ════════════════════════════════════════
    # 港股全市场涨跌幅排名（top 20/10，含板块列）
    # ════════════════════════════════════════
    L.append("## 港股今日涨幅 TOP 20")
    L.append("| 代码 | 名称 | 涨跌幅 | 板块 | 收盘(HKD) |")
    L.append("|------|------|--------|------|-----------|")
    gainers = hk_movers_yahoo(20, descending=True)
    for s in gainers:
        if "error" in s: break
        tq = hk_quote_tencent(s["code"])
        cn = tq.get("name", s.get("name","")) if "error" not in tq else s.get("name","")
        px = tq.get("price", s.get("price",0)) if "error" not in tq else s.get("price",0)
        L.append(f"| {s['code']} | {cn} | {fmt_chg(s['change_pct'])} | {cn_sector(s.get('sector',''))} | {fmt_price(px)} |")
    L.append("")

    L.append("## 港股今日跌幅 TOP 10")
    L.append("| 代码 | 名称 | 涨跌幅 | 板块 | 收盘(HKD) |")
    L.append("|------|------|--------|------|-----------|")
    losers = hk_movers_yahoo(10, descending=False)
    for s in losers:
        if "error" in s: break
        tq = hk_quote_tencent(s["code"])
        cn = tq.get("name", s.get("name","")) if "error" not in tq else s.get("name","")
        px = tq.get("price", s.get("price",0)) if "error" not in tq else s.get("price",0)
        L.append(f"| {s['code']} | {cn} | {fmt_chg(s['change_pct'])} | {cn_sector(s.get('sector',''))} | {fmt_price(px)} |")
    L.append("")


    # ════════════════════════════════════════
    # 大宗商品 & 加密
    # ════════════════════════════════════════
    L.append("## 黄金 / 美元 / 原油 / 加密货币")
    L.append("| 品种 | 最新价 | 涨跌幅 | 日期 |")
    L.append("|------|--------|--------|------|")
    for sym, label in [
        ("GC=F",     "COMEX黄金 (USD/oz)"),
        ("SI=F",     "白银 (USD/oz)"),
        ("DX-Y.NYB", "美元指数 DXY"),
        ("CL=F",     "WTI原油 (USD/bbl)"),
        ("BZ=F",     "布伦特原油 (USD/bbl)"),
        ("BTC-USD",  "比特币 BTC"),
        ("ETH-USD",  "以太坊 ETH"),
    ]:
        q = yahoo_close(sym)
        if "error" in q:
            L.append(f"| {label} | ⚠️ | — | — |")
        else:
            L.append(f"| {label} | {fmt_price(q['price'])} {q['currency']} | {fmt_chg(q['change_pct'])} | {q['date']} |")
    L.append("")

    # ════════════════════════════════════════
    # 内部分析信号（仅供Claude搜索催化剂用）
    # 此区域内容不得出现在日报正文中
    # ════════════════════════════════════════
    L.append("---")
    L.append("## [内部分析信号] 仅供Claude核查催化剂，不出现在日报正文")
    L.append("")

    # 美股异动（|涨跌|>3%，需核查原因）
    us_movers = []
    for sr in us_sector_results[:5]:     # 只看波动前5板块
        quotes = yahoo_screener(
            exchange_filter=["NMS", "NYQ"],
            n=50, sort_field="percentchange",
            sort_desc=True, sector=sr["sector"],
            min_mktcap=5_000_000_000,    # 大市值（>50亿美元）才标注
        )
        for q in quotes:
            if "error" in q: break
            p = parse_quote(q, "us")
            if abs(p["change_pct"]) >= 3:
                us_movers.append(p)
        time.sleep(0.3)

    if us_movers:
        L.append("### 美股异动个股（大市值，|涨跌|≥3%，需核查催化剂）")
        L.append("| Ticker | 名称 | 涨跌幅 | 板块 |")
        L.append("|--------|------|--------|------|")
        seen = set()
        for p in sorted(us_movers, key=lambda x: abs(x["change_pct"]), reverse=True):
            if p["code"] in seen: continue
            seen.add(p["code"])
            L.append(f"| {p['code']} | {p['name']} | {fmt_chg(p['change_pct'])} | {cn_sector(p['sector'])} |")
        L.append("")

    # 港股异动（|涨跌|>5%，需核查原因）
    hk_movers = [s for s in hk_stocks if abs(s["change_pct"]) >= 5]
    if hk_movers:
        L.append("### 港股异动个股（|涨跌|≥5%，需核查催化剂）")
        L.append("| 代码 | 名称 | 涨跌幅 | 板块 |")
        L.append("|------|------|--------|------|")
        for s in sorted(hk_movers, key=lambda x: abs(x["change_pct"]), reverse=True):
            tq = hk_quote_tencent(s["code"])
            cn = tq.get("name", s["name"]) if "error" not in tq else s["name"]
            L.append(f"| {s['code']} | {cn} | {fmt_chg(s['change_pct'])} | {s['sector']} |")
        L.append("")

    L.append(f"*由 GitHub Actions 自动生成 · {now.strftime('%Y-%m-%d %H:%M')} 北京时间*")
    return "\n".join(L)


# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import os
    os.makedirs("data", exist_ok=True)
    report       = build_report()
    beijing_date = datetime.now(BEIJING).strftime("%Y-%m-%d")
    for path in [f"data/{beijing_date}.md", "data/daily_latest.md"]:
        with open(path, "w", encoding="utf-8") as f:
            f.write(report)
    print(report)
    print(f"\n✅ 已写入 data/{beijing_date}.md + data/daily_latest.md", file=sys.stderr)
