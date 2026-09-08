# -*- coding: utf-8 -*-
"""
update_report_dates.py - 自动校正 config.json 里每只股票的真实最近财报发布日
================================================================================
背景: config.json 中 report_date 是批量/手工填报的"最近财报日"(常为统一日期如 8.25),
      并非每家公司真实的最近一次财报发布日期, 导致看板"最近财报日期"列失真,
      以及 "今天-report_date>80天→财报临近" 判断不可靠。

本脚本: 从 Yahoo Finance quoteSummary.calendarEvents.earningsCallDate 抓取
       "最近一次已举办的财报电话会/发布会日期" (即最近实际发布日),
       写入 config.json 的 report_date 字段(仅在有数据时更新, 失败/无数据保留原值)。

用法: python update_report_dates.py
建议: 在 update_all.bat / daily_run.py 中【财报日历步骤后】调用, 每次更新时自动校正。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SCRIPT_DIR = Path(__file__).parent
try:
    from utils import _auto_proxy
except ImportError:
    _auto_proxy = lambda url: {}

CONFIG_F = SCRIPT_DIR.parent / "watchlist_us" / "config.json"

# 港股去前导零并保到4位: 00700.HK -> 0700.HK, 00100.HK -> 0100.HK, 01810.HK -> 1810.HK
def _norm(sym):
    if sym.endswith(".HK") and len(sym) > 4:
        num = sym[:-3].lstrip("0").rjust(4, "0")
        return num + ".HK"
    return sym


def _get_crumb(session):
    hdr = {"User-Agent": "Mozilla/5.0"}
    try:
        session.get("https://fc.yahoo.com", headers=hdr, timeout=15)
    except Exception:
        pass
    try:
        r = session.get("https://query1.finance.yahoo.com/v1/test/getcrumb",
                        headers=hdr, timeout=15)
        if r.status_code == 200 and r.text.strip():
            return r.text.strip()
    except Exception:
        pass
    return ""


def fetch_last_report_date(session, crumb, sym):
    """返回最近一次【已发生】的实际财报发布日(YYYY-MM-DD), 取不到返回 None.
    earningsCallDate 可能是未来(下一次电话会), 只接受 今天及以前 的日期."""
    if not crumb:
        return None
    hdr = {"User-Agent": "Mozilla/5.0"}
    url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{_norm(sym)}"
    try:
        r = session.get(url, params={"modules": "calendarEvents", "crumb": crumb},
                        headers=hdr, timeout=20)
        if r.status_code != 200:
            return None
        res = (r.json().get("quoteSummary", {}).get("result") or [None])[0]
        if not res:
            return None
        ce = (res.get("calendarEvents") or {}).get("earnings") or {}
        calls = [e.get("fmt") for e in (ce.get("earningsCallDate") or [])]
        if not calls:
            return None
        d = calls[0]
        # 仅接受合法日期串
        _d = datetime.strptime(d, "%Y-%m-%d")
        # 未来日期(下一次发布会)不算"最近已发布", 跳过保留原值
        if _d > datetime.now():
            return None
        return d
    except Exception:
        return None


def main():
    if not CONFIG_F.exists():
        print(f"[ERR] config.json 不存在: {CONFIG_F}")
        return
    cfg = json.loads(CONFIG_F.read_text(encoding="utf-8"))

    session = requests.Session()
    session.trust_env = False
    session.proxies = _auto_proxy("https://query2.finance.yahoo.com")
    session.verify = False
    crumb = _get_crumb(session)
    if not crumb:
        print("[WARN] Yahoo crumb 获取失败, 本次不校正(保留原值)")
        return

    updated = unchanged = failed = 0
    for group in ("stocks", "hk_stocks"):
        for item in cfg.get(group, []):
            sym = (item.get("symbol") or "").strip()
            if not sym:
                continue
            d = fetch_last_report_date(session, crumb, sym)
            old = item.get("report_date", "")
            if d and d != old:
                item["report_date"] = d
                updated += 1
                print(f"  [更新] {sym:12s} {old or '(空)':12s} -> {d}")
            elif d == old:
                unchanged += 1
            else:
                failed += 1
                print(f"  [跳过] {sym:12s} 无法获取, 保留原值 '{old}'")
            time.sleep(0.4)  # rate limit

    if updated:
        CONFIG_F.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"\n完成: 更新 {updated} 只, 未变 {unchanged} 只, 失败保留 {failed} 只")
        print(f"已写回 {CONFIG_F}")
    else:
        print(f"\n完成: 无更新(更新{updated}/未变{unchanged}/失败{failed})")
    print("提示: 之后需重新运行 dashboard.py 生成看板使新日期生效.")


if __name__ == "__main__":
    main()