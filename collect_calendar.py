# -*- coding: utf-8 -*-
"""
日历搜集脚本 - 每周三/周五晚8点由计划任务触发
功能:
1. 抓取待确认(pending)股票的财报日期(stockanalysis.com)
2. 抓取美联储2026年FOMC会议日程(如有变更更新)
3. 增量更新 earnings_calendar.json: 只更新pending项和日期变化的项, 不动已完成项
用法: python collect_calendar.py
"""
import json
import os
import re
import sys
import time
from datetime import datetime, date
from pathlib import Path

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SCRIPT_DIR = Path(__file__).parent

sys.path.insert(0, str(SCRIPT_DIR))
from utils import _auto_proxy, WORKSPACE_ROOT

# 代理分流由 utils._auto_proxy 统一处理: 本地走 127.0.0.1:7890, NO_PROXY=1 直连
# 云端用 WORKSPACE_ROOT 环境变量指向仓库根, 保证 watchlist_us 可找到
# 注意: WATCHLIST_US/CAL_JSON 必须在 WORKSPACE 覆盖之后再计算, 否则云端会写到仓库外
WORKSPACE = WORKSPACE_ROOT
WATCHLIST_US = WORKSPACE / "watchlist_us"
CAL_JSON = WATCHLIST_US / "earnings_calendar.json"
LOG_F = SCRIPT_DIR / "collect_calendar.log"

# 美联储2026年FOMC会议日程(提前一年公布, 数据源: 美联储官网)
# 2026年8次FOMC会议(含经济预测摘要SEP的会议标记)
FOMC_2026 = [
    {"date": "2026-01-27", "name": "FOMC会议", "note": "含SEP经济预测"},
    {"date": "2026-01-28", "name": "FOMC会议第2日", "note": "利率决议+鲍威尔发布会"},
    {"date": "2026-03-17", "name": "FOMC会议", "note": "含SEP经济预测"},
    {"date": "2026-03-18", "name": "FOMC会议第2日", "note": "利率决议+鲍威尔发布会"},
    {"date": "2026-04-28", "name": "FOMC会议", "note": ""},
    {"date": "2026-04-29", "name": "FOMC会议第2日", "note": "利率决议+鲍威尔发布会"},
    {"date": "2026-06-16", "name": "FOMC会议", "note": "含SEP经济预测"},
    {"date": "2026-06-17", "name": "FOMC会议第2日", "note": "利率决议+鲍威尔发布会"},
    {"date": "2026-07-28", "name": "FOMC会议", "note": ""},
    {"date": "2026-07-29", "name": "FOMC会议第2日", "note": "利率决议+鲍威尔发布会"},
    {"date": "2026-09-15", "name": "FOMC会议", "note": "含SEP经济预测"},
    {"date": "2026-09-16", "name": "FOMC会议第2日", "note": "利率决议+鲍威尔发布会"},
    {"date": "2026-10-27", "name": "FOMC会议", "note": ""},
    {"date": "2026-10-28", "name": "FOMC会议第2日", "note": "利率决议+鲍威尔发布会"},
    {"date": "2026-12-08", "name": "FOMC会议", "note": "含SEP经济预测"},
    {"date": "2026-12-09", "name": "FOMC会议第2日", "note": "利率决议+鲍威尔发布会"},
]

# 2026年关键经济数据发布日(非农/CPI, 提前数月公布日程, 均为美东时间8:30)
# 格式: (日期, 名称, 备注) - 每月非农(首个周五) + CPI(月中)
ECON_2026 = [
    # 非农就业报告 (每月第一个周五, 美东8:30)
    {"date": "2026-08-07", "name": "非农就业报告", "note": "7月数据"},
    {"date": "2026-09-04", "name": "非农就业报告", "note": "8月数据"},
    {"date": "2026-10-02", "name": "非农就业报告", "note": "9月数据"},
    {"date": "2026-11-06", "name": "非农就业报告", "note": "10月数据"},
    {"date": "2026-12-04", "name": "非农就业报告", "note": "11月数据"},
    # 2027年1月非农(12月数据, 提前加入以便预告)
    {"date": "2027-01-08", "name": "非农就业报告", "note": "12月数据"},
    # CPI (每月中旬, 美东8:30; 日期为美东当地时间)
    {"date": "2026-08-12", "name": "CPI通胀数据", "note": "7月数据"},
    {"date": "2026-09-11", "name": "CPI通胀数据", "note": "8月数据"},
    {"date": "2026-10-14", "name": "CPI通胀数据", "note": "9月数据"},
    {"date": "2026-11-10", "name": "CPI通胀数据", "note": "10月数据"},
    {"date": "2026-12-10", "name": "CPI通胀数据", "note": "11月数据"},
]

# 经济数据发布可能有节假日偏移, 实际日期以官方日历为准, 本脚本会尽量从stockanalysis/其他源校正


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        # PowerShell/GBK终端下部分字符无法打印，只写日志
        pass
    with open(LOG_F, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch_earnings_date(symbol, session):
    """从stockanalysis.com抓取财报日期（已废弃，改用fetch_nasdaq_calendar）"""
    # 此函数不再使用，保留兼容性
    return None


def fetch_nasdaq_calendar(symbols, session, max_days=90):
    """从NASDAQ官方API批量获取财报日历。
    查询未来max_days天内的财报日程，返回 {symbol: date_str} 字典。
    国内需代理访问 api.nasdaq.com，代理不通时返回空。
    """
    from datetime import date as dt_date, timedelta
    import time as _time

    today = dt_date.today()
    result = {}
    remaining = set(symbols)

    # 逐日查询（NASDAQ每日期权返回50条，覆盖不够广，多查几天）
    for offset in range(max_days):
        if not remaining:
            break
        d = today + timedelta(days=offset)
        # 周末通常没有财报，跳过加速
        if d.weekday() >= 5:  # Saturday=5, Sunday=6
            continue
        try:
            url = f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}"
            resp = session.get(url, timeout=(7, 12),
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            if resp.status_code != 200:
                continue
            data = resp.json()
            if not data or not isinstance(data, dict):
                continue
            rows = (data.get("data") or {}).get("rows") or []
            for row in rows:
                sym = row.get("symbol", "")
                if sym in remaining:
                    result[sym] = d.isoformat()
                    remaining.discard(sym)
        except Exception:
            pass  # 单日失败不影响整体
        _time.sleep(0.3)  # rate limit

    return result


def fetch_yahoo_earnings(symbols, session, max_days=120):
    """从 Yahoo Finance quoteSummary/calendarEvents 批量获取财报日期。
    覆盖港股(.HK)/A股(.SZ/.SS)/韩股(.KS)/日股(.T)等 NASDAQ API 不覆盖的标的。
    返回 {symbol: "YYYY-MM-DD"}。失败标的不会出现在结果中。
    """
    from datetime import date as dt_date, timedelta
    import time as _time

    today = dt_date.today()
    end = today + timedelta(days=max_days)
    result = {}

    # Yahoo quoteSummary 需要 crumb: 先访问 fc.yahoo.com 设置 cookie, 再取 crumb
    crumb = ""
    try:
        hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        session.get("https://fc.yahoo.com", headers=hdr, timeout=15)  # 404 也正常, 只为设置 cookie
        r = session.get("https://query1.finance.yahoo.com/v1/test/getcrumb", headers=hdr, timeout=15)
        if r.status_code == 200 and r.text.strip():
            crumb = r.text.strip()
    except Exception:
        pass
    if not crumb:
        print("  [WARN] Yahoo crumb 获取失败, 跳过非美股财报日期抓取")

    def _norm(sym):
        """港股去前导零: 00700.HK -> 0700.HK, 00100.HK -> 100.HK"""
        if sym.endswith(".HK") and len(sym) > 4:
            num = sym[:-3].lstrip("0") or "0"
            return num + ".HK"
        return sym

    for sym in symbols:
        if not crumb:
            break
        try:
            url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{_norm(sym)}"
            resp = session.get(url, timeout=(7, 12), params={"modules": "calendarEvents", "crumb": crumb},
                headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                continue
            data = resp.json().get("quoteSummary", {}).get("result") or []
            if not data:
                continue
            evs = (((data[0].get("calendarEvents") or {}).get("earnings") or {}).get("earningsDate")) or []
            for ev in evs:
                ts = ev.get("raw")
                if ts:
                    d = dt_date.fromtimestamp(ts)
                    if today <= d <= end:
                        result[sym] = d.isoformat()
                        break
        except Exception:
            pass  # 单只失败不影响整体
        _time.sleep(0.4)  # rate limit

    return result


def main():
    log("=" * 50)
    log("日历搜集脚本启动")

    # 读取现有日历
    if CAL_JSON.exists():
        with open(CAL_JSON, "r", encoding="utf-8") as f:
            cal = json.load(f)
    else:
        cal = {"updated": "", "events": [], "watch_items": []}

    events = cal.get("events", [])
    symbols = {e["symbol"] for e in events}
    log(f"现有事件: {len(events)}条, 涉及{len(symbols)}只股票")

    # 代理连通性检测：不通则跳过财报日期抓取(需访问 api.nasdaq.com)
    # NO_PROXY=1(云端/服务器)时直连可访问, 视为代理可用
    proxy_ok = bool(os.getenv("NO_PROXY"))
    if not proxy_ok:
        try:
            import socket
            for port in (7892, 7890, 7897, 10809, 10808):
                try:
                    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                    sock.close()
                    proxy_ok = True
                    break
                except Exception:
                    continue
        except Exception:
            pass

    # 1. 批量抓取pending/upcoming股票的财报日期 (NASDAQ API，需代理)
    updated = 0
    pending_events = [e for e in events if e.get("status") in ("pending", "upcoming")]
    if not proxy_ok:
        log(f"  [SKIP] 代理不可用(127.0.0.1:7892/7890/7897/10809/10808)，跳过财报日期抓取（{len(pending_events)}条待更新）")
    else:
        session = requests.Session()
        session.trust_env = False
        session.verify = False
        # NASDAQ 是国外域名: 本地走代理, 云端(NO_PROXY)直连
        session.proxies = _auto_proxy("https://api.nasdaq.com")

        # 0. 港股/A股/ADR 标的(有symbol)加入日历:
        #    - config 标记 earnings_done (最近一个月已公布财报) 的不加入
        #    - Yahoo 能查到未来财报日的直接定为 upcoming 确切日期
        #    - 其余保持 pending 待确认
        hk_added = 0
        try:
            hk_cfg_f = WATCHLIST_US / "config.json"
            if hk_cfg_f.exists():
                with open(hk_cfg_f, "r", encoding="utf-8") as f:
                    hk_cfg = json.load(f)
                existing_hk = {e.get("symbol", "") for e in events}
                _hk_new = []
                for h in hk_cfg.get("hk_stocks", []):
                    hs = (h.get("symbol") or "").strip()
                    if not hs or hs in existing_hk:
                        continue
                    if h.get("earnings_done"):
                        log(f"  [跳过] {hs} {h.get('name','')}: 最近一个月已公布财报")
                        existing_hk.add(hs)
                        continue
                    _hk_new.append((hs, h))
                if _hk_new:
                    _ym = fetch_yahoo_earnings([x[0] for x in _hk_new], session, max_days=120)
                else:
                    _ym = {}
                for hs, h in _hk_new:
                    fetched = _ym.get(hs)
                    if fetched:
                        events.append({
                            "date": fetched,
                            "symbol": hs,
                            "name": h.get("name", hs),
                            "period": "待定",
                            "status": "upcoming",
                            "file": None,
                            "note": (h.get("note") or "")[:80],
                        })
                    else:
                        events.append({
                            "date": "2026-??-??",
                            "symbol": hs,
                            "name": h.get("name", hs),
                            "period": "待定",
                            "status": "pending",
                            "file": None,
                            "note": (h.get("note") or "")[:80],
                        })
                    existing_hk.add(hs)
                    hk_added += 1
        except Exception as _he:
            log(f"  [WARN] 读取hk_stocks失败: {_he}")
        if hk_added:
            log(f"  [新增] 港股/A股/ADR财报事件: {hk_added}条")

        # 收集待查询的股票
        pending_syms = []
        for e in pending_events:
            if e.get("status") == "done":
                continue
            if e.get("status") == "upcoming" and not e.get("date", "").endswith("??"):
                continue
            pending_syms.append(e)
        if pending_syms:
            # 美股/ADR走NASDAQ API; 港股/A股/韩股/日股等非美股走Yahoo quoteSummary
            _us_syms = [e for e in pending_syms if "." not in e["symbol"] or e["symbol"].endswith(".US")]
            _nonus_syms = [e for e in pending_syms if e not in _us_syms]
            log(f"待更新事件: {len(pending_syms)}条 (美股{len(_us_syms)}条, 非美股{len(_nonus_syms)}条)")
            if _us_syms:
                symbols_to_fetch = list({e["symbol"] for e in _us_syms})
                log(f"  开始从NASDAQ API批量查询(未来90天)...")
                earnings_map = fetch_nasdaq_calendar(symbols_to_fetch, session, max_days=90)
                log(f"  NASDAQ查询完成: {len(earnings_map)}/{len(symbols_to_fetch)}只股票有确定日期")
                for e in _us_syms:
                    sym = e["symbol"]
                    fetched = earnings_map.get(sym)
                    if fetched:
                        old_date = e.get("date", "")
                        if old_date != fetched:
                            e["date"] = fetched
                            e["status"] = "upcoming"
                            log(f"  [更新] {sym}: {old_date} -> {fetched}")
                            updated += 1
                        else:
                            log(f"  [一致] {sym}: {fetched}")
                    else:
                        log(f"  [未获] {sym}: 未来90天内暂无财报日期")
            if _nonus_syms:
                symbols_to_fetch2 = list({e["symbol"] for e in _nonus_syms})
                log(f"  开始从Yahoo quoteSummary批量查询(未来120天)...")
                yahoo_map = fetch_yahoo_earnings(symbols_to_fetch2, session, max_days=120)
                log(f"  Yahoo查询完成: {len(yahoo_map)}/{len(symbols_to_fetch2)}只股票有确定日期")
                for e in _nonus_syms:
                    sym = e["symbol"]
                    fetched = yahoo_map.get(sym)
                    if fetched:
                        old_date = e.get("date", "")
                        if old_date != fetched:
                            e["date"] = fetched
                            e["status"] = "upcoming"
                            log(f"  [更新] {sym}: {old_date} -> {fetched}")
                            updated += 1
                        else:
                            log(f"  [一致] {sym}: {fetched}")
                    else:
                        log(f"  [未获] {sym}: 未来120天内暂无财报日期")
        else:
            log("待更新事件: 0条")

    # 2. 美联储FOMC日程: 检查是否已存在, 不存在则添加
    existing_keys = {(e.get("date", ""), e.get("symbol", ""), e.get("name", "")) for e in events}
    fomc_added = 0
    for f in FOMC_2026:
        key = (f["date"], "FED", f["name"])
        if key not in existing_keys:
            events.append({
                "date": f["date"],
                "symbol": "FED",
                "name": f["name"],
                "period": "FOMC",
                "status": "upcoming" if f["date"] >= date.today().isoformat() else "done",
                "file": None,
                "note": f["note"],
            })
            fomc_added += 1
            existing_keys.add(key)
    if fomc_added:
        log(f"  [新增] FOMC会议: {fomc_added}条")
    else:
        log("  [一致] FOMC日程已存在,无新增")

    # 3. 经济数据日程: 同样增量添加
    econ_added = 0
    for e in ECON_2026:
        key = (e["date"], "ECON", e["name"])
        if key not in existing_keys:
            events.append({
                "date": e["date"],
                "symbol": "ECON",
                "name": e["name"],
                "period": "经济数据",
                "status": "upcoming" if e["date"] >= date.today().isoformat() else "done",
                "file": None,
                "note": e["note"],
            })
            econ_added += 1
            existing_keys.add(key)
    if econ_added:
        log(f"  [新增] 经济数据: {econ_added}条")
    else:
        log("  [一致] 经济数据日程已存在,无新增")

    # 4. 状态维护: 日期已过去(非"??"待定)的 upcoming 事件自动标记为 done, 避免日历长期显示过期"即将发布"
    rolled = 0
    _today = date.today().isoformat()
    for e in events:
        d = e.get("date", "")
        if e.get("status") == "upcoming" and d and not d.endswith("??") and d < _today:
            e["status"] = "done"
            rolled += 1
    if rolled:
        log(f"  [状态] {rolled}条过期事件已标记为完成(done)")

    # 5. 按日期排序
    events.sort(key=lambda x: x.get("date", "9999"))
    cal["events"] = events
    cal["updated"] = date.today().isoformat()

    with open(CAL_JSON, "w", encoding="utf-8") as f:
        json.dump(cal, f, ensure_ascii=False, indent=2)

    # 6. 重新生成日历页
    import subprocess
    r = subprocess.run([sys.executable, str(WATCHLIST_US / "update_calendar.py")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=120, cwd=str(WATCHLIST_US))
    log(f"日历页已重新生成: {r.stdout.strip()[-100:] if r.stdout else ''}")

    log(f"完成: 更新财报日期{updated}条, 新增FOMC {fomc_added}条, 新增经济数据{econ_added}条")
    log("=" * 50)


if __name__ == "__main__":
    main()
