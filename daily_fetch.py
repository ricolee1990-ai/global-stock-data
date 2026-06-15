"""
海外日报数据采集脚本 — daily_fetch.py
每天由 GitHub Actions 自动运行，结果写入 data/daily_latest.md
Claude 通过 web_fetch 读取该文件，直接生成日报（无需截图）

依赖: pip install requests
运行: python daily_fetch.py
"""

import requests
import re
import sys
from datetime import datetime, timezone, timedelta

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
BEIJING = timezone(timedelta(hours=8))


# ──────────────────────────────────────────────
# 数据获取函数
# ──────────────────────────────────────────────

def yahoo_chart_close(symbol: str) -> dict:
    """Yahoo Finance v8 — 零crumb，取最近收盘价"""
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
        r = requests.get(url, params={"interval": "1d", "range": "5d"},
                         headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        result = r.json().get("chart", {}).get("result", [])
        if not result:
            return {"symbol": symbol, "error": "no data"}

        meta = result[0].get("meta", {})
        timestamps = result[0].get("timestamp", [])
        quote = result[0].get("indicators", {}).get("quote", [{}])[0]
        closes = quote.get("close", [])

        valid = [(ts, c) for ts, c in zip(timestamps, closes) if c is not None]
        if len(valid) < 2:
            return {"symbol": symbol, "error": "insufficient data"}

        _, prev_close = valid[-2]
        last_ts, last_close = valid[-1]

        change_pct = (last_close - prev_close) / prev_close * 100
        date_str = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d")

        return {
            "symbol": symbol,
            "name": meta.get("shortName", symbol),
            "price": round(last_close, 2),
            "prev_close": round(prev_close, 2),
            "change_pct": round(change_pct, 2),
            "currency": meta.get("currency", ""),
            "date": date_str,
        }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}


def hk_quote(code: str) -> dict:
    """腾讯港股行情 — 78字段，中文名最可靠"""
    try:
        r = requests.get(f"https://qt.gtimg.cn/q=r_hk{code}", timeout=10)
        r.encoding = "gbk"
        m = re.search(r'"(.+)"', r.text)
        if not m:
            return {"code": code, "error": "empty"}
        f = m.group(1).split("~")
        if len(f) < 50:
            return {"code": code, "error": "short"}
        price = float(f[3]) if f[3] else 0
        prev  = float(f[4]) if f[4] else 0
        chg   = float(f[32]) if f[32] else 0
        vol   = int(float(f[6])) if f[6] else 0   # 腾讯返回浮点字符串，需先转float再转int
        amt   = float(f[37]) if f[37] else 0
        return {
            "code": code, "name": f[1],
            "price": price, "prev_close": prev,
            "change_pct": round(chg, 2),
            "volume": vol, "amount": amt,
            "pe": float(f[39]) if f[39] else None,
        }
    except Exception as e:
        return {"code": code, "error": str(e)}


def us_quote(ticker: str) -> dict:
    """新浪美股行情 — 中文名+EPS+PE"""
    try:
        r = requests.get(
            f"https://hq.sinajs.cn/list=gb_{ticker.lower()}",
            headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": UA},
            timeout=10)
        r.encoding = "gbk"
        m = re.search(r'"(.+)"', r.text)
        if not m:
            return {"ticker": ticker, "error": "empty"}
        f = m.group(1).split(",")
        if len(f) < 30 or not f[1]:
            return {"ticker": ticker, "error": "short"}
        return {
            "ticker": ticker, "name": f[0],
            "price": float(f[1]),
            "change_pct": float(f[2]),
            "prev_close": float(f[26]) if f[26] else 0,
            "pe": float(f[14]) if f[14] else None,
        }
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}


def hk_market_rank_yahoo(n: int = 20, descending: bool = True) -> list:
    """Yahoo Finance港股涨跌幅排名 — 境外IP可用，作为东财的fallback"""
    try:
        # Step1: 获取cookie+crumb
        s = requests.Session()
        s.headers["User-Agent"] = UA
        s.get("https://fc.yahoo.com", timeout=10)
        r = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10)
        r.raise_for_status()
        crumb = r.text

        # Step2: 调screener
        scr_id = "day_gainers" if descending else "day_losers"
        r2 = s.get(
            "https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved",
            params={"count": n, "scrIds": scr_id, "region": "HK",
                    "lang": "zh-Hant-HK", "crumb": crumb},
            timeout=15,
        )
        r2.raise_for_status()
        quotes = r2.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
        result = []
        for q in quotes[:n]:
            result.append({
                "code": q.get("symbol", "").replace(".HK", "").zfill(5),
                "name": q.get("shortName") or q.get("longName") or q.get("symbol"),
                "change_pct": round(q.get("regularMarketChangePercent", 0), 2),
                "amount": q.get("regularMarketVolume", 0),
            })
        return result if result else [{"error": "Yahoo screener返回空"}]
    except Exception as e:
        return [{"error": f"Yahoo fallback失败: {e}"}]


def hk_market_rank(n: int = 20, descending: bool = True) -> list:
    """港股全市场涨跌幅排名 — 优先东财，境外IP自动切Yahoo"""
    try:
        headers = {
            "User-Agent": UA,
            "Referer": "https://www.eastmoney.com/",
        }
        r = requests.get("https://push2.eastmoney.com/api/qt/clist/get", params={
            "fs": "m:116", "fields": "f2,f3,f5,f6,f12,f14",
            "pn": 1, "pz": n, "fid": "f3", "po": 1 if descending else 0,
        }, headers=headers, timeout=10)

        if r.status_code != 200 or not r.text:
            raise ValueError(f"东财HTTP {r.status_code}")

        diff = r.json().get("data", {}).get("diff")
        if not diff:
            raise ValueError("东财返回空，切Yahoo")

        return [{"code": s["f12"], "name": s["f14"],
                 "change_pct": round(s["f3"] / 100, 2),
                 "amount": s.get("f6", 0)}
                for s in diff if s.get("f3") is not None]

    except Exception:
        # 东财失败（境外IP被拒）→ 自动切Yahoo Finance
        return hk_market_rank_yahoo(n, descending)


# ──────────────────────────────────────────────
# 格式化输出（Markdown，供Claude直接读取）
# ──────────────────────────────────────────────

def fmt_chg(pct):
    if pct is None:
        return "N/A"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"

def fmt_price(p, currency=""):
    if p is None or p == 0:
        return "N/A"
    if p > 10000:
        return f"{currency}{p:,.0f}"
    return f"{currency}{p:,.2f}"


def build_report() -> str:
    now = datetime.now(BEIJING)
    lines = []

    lines.append(f"# 海外市场数据快照")
    lines.append(f"**采集时间（北京时间）：{now.strftime('%Y-%m-%d %H:%M')}**")
    lines.append(f"> 数据说明：北京时间05:30后采集，港股/美股均已收盘，价格为前日收盘价。")
    lines.append("")

    # ── 港股指数 ──────────────────────────────
    lines.append("## 港股指数")
    lines.append("| 指数 | 收盘 | 涨跌 | 数据日期 |")
    lines.append("|------|------|------|----------|")
    for sym, label in [
        ("^HSI",    "恒生指数"),
        ("3033.HK", "恒生科技(ETF代理)"),  # Yahoo无^HSTECH，用CSOP ETF代替
    ]:
        q = yahoo_chart_close(sym)
        if "error" in q:
            lines.append(f"| {label} | ⚠️ {q['error']} | — | — |")
        else:
            lines.append(f"| {label} | {fmt_price(q['price'])} | {fmt_chg(q['change_pct'])} | {q['date']} |")
    lines.append("")

    # ── 港股关键个股 ──────────────────────────
    lines.append("## 港股关键个股")
    lines.append("| 代码 | 中文名 | 收盘(HKD) | 涨跌 |")
    lines.append("|------|--------|-----------|------|")
    hk_watch = [
        "00700","09988","01810","03690","09999",
        "00981","02626","03750","02382",
        "09868","02015","09866",
        "02513","02026","03986",
    ]
    for code in hk_watch:
        q = hk_quote(code)
        if "error" in q:
            lines.append(f"| {code} | ⚠️ {q['error']} | — | — |")
        else:
            lines.append(f"| {code} | {q['name']} | {fmt_price(q['price'])} | {fmt_chg(q['change_pct'])} |")
    lines.append("")

    # ── 港股全市场涨跌排名 ────────────────────
    lines.append("## 港股今日涨幅 TOP 20")
    lines.append("| 代码 | 名称 | 涨跌幅 |")
    lines.append("|------|------|--------|")
    for s in hk_market_rank(20, True):
        if "error" in s:
            lines.append(f"| ⚠️ | {s['error']} | — |")
            break
        lines.append(f"| {s['code']} | {s['name']} | {fmt_chg(s['change_pct'])} |")
    lines.append("")

    lines.append("## 港股今日跌幅 TOP 10")
    lines.append("| 代码 | 名称 | 涨跌幅 |")
    lines.append("|------|------|--------|")
    for s in hk_market_rank(10, False):
        if "error" in s:
            lines.append(f"| ⚠️ | {s['error']} | — |")
            break
        lines.append(f"| {s['code']} | {s['name']} | {fmt_chg(s['change_pct'])} |")
    lines.append("")

    # ── 美股指数 ──────────────────────────────
    lines.append("## 美股指数")
    lines.append("| 指数 | 收盘 | 涨跌 | 数据日期 |")
    lines.append("|------|------|------|----------|")
    us_indices = [
        ("^GSPC", "标普500"),
        ("^NDX",  "纳斯达克100"),
        ("^IXIC", "纳斯达克综合"),
        ("^DJI",  "道琼斯"),
        ("^VIX",  "VIX"),
        ("^TNX",  "10Y美债收益率(%)"),
    ]
    for sym, label in us_indices:
        q = yahoo_chart_close(sym)
        if "error" in q:
            lines.append(f"| {label} | ⚠️ {q['error']} | — | — |")
        else:
            lines.append(f"| {label} | {fmt_price(q['price'])} | {fmt_chg(q['change_pct'])} | {q['date']} |")
    lines.append("")

    # ── 美股关键个股 ──────────────────────────
    lines.append("## 美股关键个股")
    lines.append("| Ticker | 中文名 | 收盘(USD) | 涨跌 |")
    lines.append("|--------|--------|-----------|------|")

    # 分批：新浪源（主）
    us_sina = ["AAPL","NVDA","MSFT","GOOGL","AMZN","META",  # 科技七巨头
               "SNDK","MU","INTC","AMD","AVGO",              # 存储/芯片
               "CRWD","PANW",                                # 网络安全
               "NVO","LLY",                                  # GLP-1
               "COIN","MSTR",                                # 加密
               "DKNG",                                       # 博彩
               ]
    for ticker in us_sina:
        q = us_quote(ticker)
        if "error" in q or q.get("price", 0) == 0:
            # 回退 Yahoo
            yq = yahoo_chart_close(ticker)
            if "error" not in yq:
                lines.append(f"| {ticker} | {yq.get('name','')[:12]} | {fmt_price(yq['price'],'$')} | {fmt_chg(yq['change_pct'])} |")
            else:
                lines.append(f"| {ticker} | ⚠️ | — | — |")
        else:
            lines.append(f"| {ticker} | {q.get('name','')[:12]} | {fmt_price(q['price'],'$')} | {fmt_chg(q['change_pct'])} |")

    # TSM 单独用 Yahoo（NYSE，新浪代码不同）
    tsm = yahoo_chart_close("TSM")
    if "error" not in tsm:
        lines.append(f"| TSM | 台积电 | {fmt_price(tsm['price'],'$')} | {fmt_chg(tsm['change_pct'])} |")
    lines.append("")

    # ── 大宗商品 & 加密 ───────────────────────
    lines.append("## 黄金 / 美元 / 原油 / 加密货币")
    lines.append("| 品种 | 最新价 | 涨跌 | 数据日期 |")
    lines.append("|------|--------|------|----------|")
    commodities = [
        ("GC=F",     "COMEX黄金期货 (USD/oz)"),
        ("SI=F",     "白银期货 (USD/oz)"),
        ("DX-Y.NYB", "美元指数 DXY"),
        ("CL=F",     "WTI原油期货 (USD/bbl)"),
        ("BZ=F",     "布伦特原油 (USD/bbl)"),
        ("BTC-USD",  "比特币 BTC (USD)"),
        ("ETH-USD",  "以太坊 ETH (USD)"),
    ]
    for sym, label in commodities:
        q = yahoo_chart_close(sym)
        if "error" in q:
            lines.append(f"| {label} | ⚠️ {q['error']} | — | — |")
        else:
            lines.append(f"| {label} | {fmt_price(q['price'])} {q['currency']} | {fmt_chg(q['change_pct'])} | {q['date']} |")
    lines.append("")

    lines.append("---")
    lines.append(f"*由 GitHub Actions 自动生成 · {now.strftime('%Y-%m-%d %H:%M')} 北京时间*")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import os
    os.makedirs("data", exist_ok=True)

    report = build_report()

    # 北京时间日期，用于文件命名
    beijing_date = datetime.now(BEIJING).strftime("%Y-%m-%d")

    # 1. 带日期的归档文件：data/2026-06-15.md（永久保留，不覆盖）
    dated_file = f"data/{beijing_date}.md"
    with open(dated_file, "w", encoding="utf-8") as f:
        f.write(report)

    # 2. latest 指针：data/daily_latest.md（始终指向最新）
    latest_file = "data/daily_latest.md"
    with open(latest_file, "w", encoding="utf-8") as f:
        f.write(report)

    # 同时打印到 stdout（本地调试用）
    print(report)
    print(f"\n✅ 已写入 {dated_file} 和 {latest_file}", file=sys.stderr)
