"""
海外日报数据采集脚本 — daily_fetch.py v4
架构：
  美股  Yahoo v8 chart（11个SPDR ETF板块 + 预定义screener涨跌排名）
  港股  腾讯r_hk批量行情（首选，78字段） + 新浪rt_hk备选（25字段）
  指数  Yahoo ^HSI/^HSCE + 新浪hkHSTECH fallback
  大宗  Yahoo v8 chart（黄金/美元/原油/BTC/ETH）
  内部信号区  异动股清单，仅供Claude搜索催化剂用，不出现在日报正文

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


# HK股票池：{5位代码: 板块中文名}
# 新浪rt_hk接口直接返回中文名+涨跌幅，无需额外lookup
HK_STOCKS = {
    # 通信服务
    "00700": "通信服务",   # 腾讯控股
    "09999": "通信服务",   # 网易
    "09888": "通信服务",   # 百度
    "01024": "通信服务",   # 快手
    "09626": "通信服务",   # 哔哩哔哩
    "09961": "通信服务",   # 携程集团
    "00941": "通信服务",   # 中国移动
    "00762": "通信服务",   # 中国联通
    "00728": "通信服务",   # 中国电信
    # 非必需消费
    "09988": "非必需消费", # 阿里巴巴
    "03690": "非必需消费", # 美团
    "09618": "非必需消费", # 京东集团
    "02518": "非必需消费", # 汽车之家
    "09992": "非必需消费", # 泡泡玛特
    "00175": "非必需消费", # 吉利汽车
    "06862": "非必需消费", # 海底捞
    "02015": "非必需消费", # 理想汽车
    "09866": "非必需消费", # 蔚来
    "09868": "非必需消费", # 小鹏汽车
    # 科技/半导体
    "01810": "科技",       # 小米集团
    "00981": "科技",       # 中芯国际
    "02626": "科技",       # 华虹半导体
    "02382": "科技",       # 舜宇光学
    "00285": "科技",       # 比亚迪电子
    "02513": "科技",       # 智谱
    "03986": "科技",       # 兆易创新
    "09698": "科技",       # 金蝶国际
    "00020": "科技",       # 商汤科技
    "02026": "科技",       # 小马智行
    # 医疗保健
    "06160": "医疗保健",   # 百济神州
    "01177": "医疗保健",   # 中国生物制药
    "02359": "医疗保健",   # 药明康德
    "03692": "医疗保健",   # 翰森制药
    # 金融服务
    "00388": "金融服务",   # 香港交易所
    "00005": "金融服务",   # 汇丰控股
    "00011": "金融服务",   # 恒生银行
    "02318": "金融服务",   # 中国平安
    "01299": "金融服务",   # 友邦保险
    "01398": "金融服务",   # 工商银行
    "00939": "金融服务",   # 建设银行
    "03988": "金融服务",   # 中国银行
    "02628": "金融服务",   # 中国人寿
    "02388": "金融服务",   # 中银香港
    # 能源
    "00883": "能源",       # 中国海洋石油
    "00386": "能源",       # 中国石化
    "00857": "能源",       # 中国石油
    # 必需消费
    "09633": "必需消费",   # 农夫山泉
    "00291": "必需消费",   # 华润啤酒
    "02313": "必需消费",   # 申洲国际
    # 工业/新能源
    "03750": "工业",       # 宁德时代
    "01113": "工业",       # 长江基建
    "01038": "工业",       # 长和
    # 房地产
    "00016": "房地产",     # 新鸿基地产
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


def hk_batch_tencent() -> list:
    """
    腾讯港股批量行情 — r_hk格式，78字段（README推荐第一优先）
    qt.gtimg.cn 支持批量，境外IP可访问
    关键字段: f[1]=中文名, f[3]=现价, f[32]=涨跌幅%, f[39]=PE, f[44]=总市值
    fallback: 新浪 rt_hk（25字段，备选）
    """
    def _parse_tencent(text: str) -> list:
        results = []
        for line in text.split(";"):
            line = line.strip()
            if not line or '"' not in line:
                continue
            m_code = re.search(r"hq_str_r_hk(\d+)", line)
            if not m_code:
                continue
            code = m_code.group(1)
            m_data = re.search(r'"(.+)"', line)
            if not m_data:
                continue
            f = m_data.group(1).split("~")
            if len(f) < 50 or not f[3]:
                continue
            try:
                results.append({
                    "code":       code,
                    "name":       f[1],
                    "price":      round(float(f[3]), 2),
                    "change_pct": round(float(f[32]) if f[32] else 0, 2),
                    "sector":     HK_STOCKS.get(code, ""),
                    "pe":         float(f[39]) if f[39] else None,
                    "market_cap": float(f[44]) if f[44] else None,
                })
            except (ValueError, IndexError):
                continue
        return results

    def _parse_sina_fallback(text: str) -> list:
        """新浪rt_hk备选解析: f[0]=中文名, f[1]=现价, f[3]=涨跌幅%"""
        results = []
        for line in text.split(";"):
            line = line.strip()
            if not line or '"' not in line:
                continue
            m_code = re.search(r"hq_str_rt_hk(\d+)", line)
            if not m_code:
                continue
            code = m_code.group(1)
            m_data = re.search(r'"(.+)"', line)
            if not m_data:
                continue
            f = m_data.group(1).split(",")
            if len(f) < 4 or not f[1]:
                continue
            try:
                results.append({
                    "code":       code,
                    "name":       f[0],
                    "price":      round(float(f[1]), 2),
                    "change_pct": round(float(f[3]), 2),
                    "sector":     HK_STOCKS.get(code, ""),
                })
            except (ValueError, IndexError):
                continue
        return results

    codes     = list(HK_STOCKS.keys())
    batch_sz  = 40
    all_res   = []
    tencent_ok = True

    try:
        for i in range(0, len(codes), batch_sz):
            batch = codes[i:i+batch_sz]
            query = ",".join(f"r_hk{c}" for c in batch)
            r = requests.get(f"https://qt.gtimg.cn/q={query}", timeout=12)
            r.encoding = "gbk"
            all_res.extend(_parse_tencent(r.text))
        print(f"腾讯HK batch: {len(all_res)}只", file=sys.stderr)
    except Exception as e:
        print(f"腾讯HK失败({e})，切换新浪rt_hk备选", file=sys.stderr)
        tencent_ok = False

    # 腾讯失败或返回数据不足 → 新浪rt_hk备选
    if not tencent_ok or len(all_res) < len(codes) // 2:
        all_res = []
        try:
            for i in range(0, len(codes), 60):
                batch = codes[i:i+60]
                query = ",".join(f"rt_hk{c}" for c in batch)
                r = requests.get(
                    f"https://hq.sinajs.cn/list={query}",
                    headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": UA},
                    timeout=15,
                )
                r.encoding = "gbk"
                all_res.extend(_parse_sina_fallback(r.text))
            print(f"新浪HK backup: {len(all_res)}只", file=sys.stderr)
        except Exception as e2:
            print(f"新浪HK也失败: {e2}", file=sys.stderr)

    return all_res if all_res else [{"error": "腾讯+新浪HK均返回空"}]



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
            # Sina取到的是上一交易日收盘，日期用昨日（港股上一交易日）
            from datetime import date, timedelta
            prev_date = (datetime.now(BEIJING) - timedelta(days=1)).strftime("%Y-%m-%d")
            L.append(f"| 恒生科技指数 | {fmt_price(sina_q['price'])} | {fmt_chg(sina_q['change_pct'])} | {prev_date}(Sina) |")
        else:
            L.append(f"| 恒生科技指数 | ⚠️ Yahoo+Sina均不可用 | — | — |")
    L.append("")

    # ════════════════════════════════════════
    # 港股11大板块（批量拉取，本地分组统计）
    # ════════════════════════════════════════
    L.append("## 港股板块行情（全11板块，按平均涨跌幅排序）")
    L.append("| 板块 | 平均涨跌幅 | 覆盖股数 | 信号 |")
    L.append("|------|-----------|---------|------|")

    # HK批量：腾讯r_hk（78字段，README第一优先）+ 新浪rt_hk备选
    hk_stocks_raw = hk_batch_tencent()
    hk_stocks = [s for s in hk_stocks_raw if "error" not in s]
    # 腾讯/新浪接口直接返回中文名，_cn()直接返回fallback（即stock["name"]）
    def _cn(code, fallback):
        return fallback

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
    # 从已拉取的hk_stocks排序，用腾讯中文名缓存
    gainers_hk = sorted(hk_stocks, key=lambda x: x["change_pct"], reverse=True)[:20]
    for s in gainers_hk:
        cn = _cn(s["code"], s["name"])
        L.append(f"| {s['code']} | {cn} | {fmt_chg(s['change_pct'])} | {cn_sector(s.get('sector',''))} | {fmt_price(s['price'])} |")
    L.append("")

    L.append("## 港股今日跌幅 TOP 10")
    L.append("| 代码 | 名称 | 涨跌幅 | 板块 | 收盘(HKD) |")
    L.append("|------|------|--------|------|-----------|")
    losers_hk = sorted(hk_stocks, key=lambda x: x["change_pct"])[:10]
    for s in losers_hk:
        cn = _cn(s["code"], s["name"])
        L.append(f"| {s['code']} | {cn} | {fmt_chg(s['change_pct'])} | {cn_sector(s.get('sector',''))} | {fmt_price(s['price'])} |")
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

    # 美股异动：从已拉取的涨跌幅列表中筛选|涨跌|≥5%的个股
    # 注意：predefined screener不返回sector字段，板块列为—是已知限制
    us_movers = []
    for scr_id, desc in [("day_gainers", True), ("day_losers", False)]:
        try:
            s_sess = yahoo_session()
            r_m = s_sess.get(
                "https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved",
                params={"count": 100, "scrIds": scr_id, "crumb": s_sess._crumb},
                timeout=15,
            )
            quotes_m = r_m.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
            for q in quotes_m:
                if "." in q.get("symbol","") and not q["symbol"].endswith(".HK"):
                    continue
                chg = q.get("regularMarketChangePercent", 0) or 0
                mktcap = q.get("marketCap", 0) or 0
                if abs(chg) >= 5 and mktcap >= 5_000_000_000:
                    us_movers.append({
                        "code": q.get("symbol",""),
                        "name": (q.get("shortName") or "")[:15],
                        "change_pct": round(chg, 2),
                        "sector": q.get("sector",""),
                    })
        except Exception:
            pass

    if us_movers:
        L.append("### 美股异动个股（大市值，|涨跌|≥5%，需核查催化剂）")
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
            # 腾讯批量已返回中文名，直接用s["name"]
            L.append(f"| {s['code']} | {s['name']} | {fmt_chg(s['change_pct'])} | {s['sector']} |")
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
