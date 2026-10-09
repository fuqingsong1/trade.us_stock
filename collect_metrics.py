# -*- coding: utf-8 -*-
"""补全 config.json 中每只股票的三个财务指标 (m1/m2/m3).

三表口径:
  美股:  m1=ROIC(NOPAT/投入资本)  m2=摊薄EPS同比  m3=FCF利润率
  A股:   m1=扣非加权ROE(≈roe_dt)  m2=扣非归母净利同比(dt_netprofit_yoy)  m3=净现比(经营现金流/归母净利)
  港股:  m1/m2/m3 主要从已有分析文件提取, 缺失标 None 由前端显示 '-'

数据源:
  - A股: Tushare (fina_indicator / income / cashflow), 需全局 python (含 tushare) + TUSHARE_TOKEN
  - 美股: yfinance 三表算 ROIC / EPS YoY / FCF margin
  - 港股: 优先复用 config 已有 m 值, 缺失尝试从 analysis .md 提取

用法:
  python collect_metrics.py              # 全量: 仅补缺失, 写回 config.json + metrics_cache.json
  python collect_metrics.py --only A美股  # 可选快筛(预留)
运行环境: 用含 tushare + yfinance 的全局 python (当前机器 'python' 同时有两者)。
"""
import os, sys, json, re, glob, argparse, datetime

BASE = os.environ.get("WORKSPACE_ROOT", r"C:\Users\15949\WorkBuddy\xiaocai")
CFG = os.path.join(BASE, "watchlist_us", "config.json")
CACHE_F = os.path.join(os.path.dirname(__file__), "metrics_cache.json")
ANALYSIS_ROOT = os.path.join(BASE, "analysis", "港美股")
TODAY = datetime.date.today().isoformat()

# A股 tushare ts_code 映射: config .SS->.SH, .SZ->.SZ
def to_ts_code(sym):
    return sym[:6] + ".SH" if sym.endswith(".SS") else sym


def metric_sources():
    """返回 {symbol: {m1,m2,m3}} 最多取到已有的。"""
    if not os.path.exists(CFG):
        return {}
    cfg = json.load(open(CFG, encoding="utf-8"))
    out = {}
    for group in ("stocks", "hk_stocks"):
        for s in cfg.get(group, []):
            sym = s.get("symbol", "")
            if sym:
                out[sym] = {"m1": s.get("m1"), "m2": s.get("m2"), "m3": s.get("m3")}
    return out


# ---------- A股 (Tushare) ----------
def fetch_ashare(sym):
    """返回 (m1,m2,m3,src)。src=tushare 或 None。"""
    try:
        import tushare as ts
    except ImportError:
        return None, None, None, None
    tok = os.getenv("TUSHARE_TOKEN") or ts.get_token()
    if not tok:
        return None, None, None, None
    pro = ts.pro_api(tok)
    tsc = to_ts_code(sym)
    try:
        fi = pro.fina_indicator(ts_code=tsc, fields="ts_code,end_date,roe_dt,roe_waa,dt_netprofit_yoy,ocfps,n_income_attr_p")
        if fi is None or fi.empty:
            return None, None, None, None
        row = fi.iloc[0]
        m1 = None
        v = row.get("roe_dt") or row.get("roe_waa")
        m1 = (f"{v:.1f}%" if isinstance(v, (int, float)) else None)
        m2 = row.get("dt_netprofit_yoy")
        # 净现比 = 经营现金流 / 归母净利 : 用 cashflow + income 同期
        m3 = None
        try:
            ic = pro.cashflow(ts_code=tsc, fields="end_date,n_cashflow_act")
            ii = pro.income(ts_code=tsc, fields="end_date,n_income_attr_p")
            if ic is not None and not ic.empty and ii is not None and not ii.empty:
                d0 = row.get("end_date")
                icr = ic[ic["end_date"] == d0]
                iir = ii[ii["end_date"] == d0]
                if not icr.empty and not iir.empty:
                    ocf = float(icr.iloc[0]["n_cashflow_act"])
                    np_ = float(iir.iloc[0]["n_income_attr_p"])
                    if np_:
                        m3 = round(ocf / np_, 2)
        except Exception:
            pass
        # m2 数值化(可带符号)
        m2 = round(float(m2), 1) if isinstance(m2, (int, float)) else m2
        return m1, m2, m3, "tushare"
    except Exception as e:
        print(f"  [A股] {sym} err: {e}")
        return None, None, None, None


# ---------- 美股 (yfinance) ----------
def fetch_us(sym):
    """返回 (m1,m2,m3,src)。src=yf 或 None。"""
    try:
        import yfinance as yf
        import warnings
        warnings.filterwarnings("ignore")
        t = yf.Ticker(sym)
        f = t.financials; b = t.balance_sheet; c = t.cashflow
        if f is None or f.empty:
            return None, None, None, None
        # m2 摊薄EPS同比: 优先最新季度同比(及时, 少受全年一次性扭曲), 年度口径兜底
        m2 = None
        try:
            q = t.quarterly_income_stmt
            if q is not None and not q.empty and "Diluted EPS" in q.index:
                eps_q = q.loc["Diluted EPS"].dropna()
                if len(eps_q) >= 2 and float(eps_q.iloc[1]):
                    _y = (float(eps_q.iloc[0]) / float(eps_q.iloc[1]) - 1) * 100
                    if abs(_y) <= 300:
                        m2 = round(_y, 1)
        except Exception:
            pass
        if m2 is None and "Diluted EPS" in f.index:
            eps = f.loc["Diluted EPS"].dropna()
            if len(eps) >= 2 and eps.iloc[0] and eps.iloc[1]:
                _y = (float(eps.iloc[0]) / float(eps.iloc[1]) - 1) * 100
                if abs(_y) <= 300:
                    m2 = round(_y, 1)
        # m3 FCF margin = (OCF - capex)/revenue
        m3 = None
        rev = f.loc["Total Revenue"].dropna().iloc[0] if "Total Revenue" in f.index else None
        ocf = c.loc["Operating Cash Flow"].dropna().iloc[0] if (c is not None and "Operating Cash Flow" in c.index) else None
        capex = c.loc["Capital Expenditure"].dropna().iloc[0] if (c is not None and "Capital Expenditure" in c.index) else None
        if rev and ocf is not None and capex is not None:
            _fcf = (float(ocf) - float(capex)) / float(rev) * 100
            if abs(_fcf) <= 200:
                m3 = round(_fcf, 2)
        # m1 ROIC = NOPAT / invcap = EBIT*(1-tax) / (equity+debt-cash)
        m1 = None
        try:
            ebit = f.loc["EBIT"].dropna().iloc[0] if "EBIT" in f.index else None
            if ebit is None and "Operating Income" in f.index:
                ebit = f.loc["Operating Income"].dropna().iloc[0]
            tax = f.loc["Tax Provision"].dropna().iloc[0] if "Tax Provision" in f.index else None
            pre = f.loc["Pre Tax Income"].dropna().iloc[0] if "Pre Tax Income" in f.index else None
            eq = b.loc["Stockholders Equity"].dropna().iloc[0] if (b is not None and "Stockholders Equity" in b.index) else None
            td = b.loc["Total Debt"].dropna().iloc[0] if (b is not None and "Total Debt" in b.index) else None
            cash = b.loc["Cash And Cash Equivalents"].dropna().iloc[0] if (b is not None and "Cash And Cash Equivalents" in b.index) else None
            if ebit:
                denom = float(pre) if pre not in (None, 0) else float(ebit)
                tr = (float(tax) / denom) if (tax not in (None, 0) and denom) else 0.0
                tr = min(max(tr, 0), 0.4)  # 限制合理税率窗口
                nopat = float(ebit) * (1 - tr)
                eq_f = float(eq) if eq is not None else 0.0
                td_f = float(td) if td is not None else 0.0
                cash_f = float(cash) if cash is not None else 0.0
                invcap = eq_f + td_f - cash_f
                if invcap:
                    m1 = round(nopat / invcap * 100, 1)
        except Exception:
            pass
        return m1, m2, m3, "yf"
    except Exception as e:
        print(f"  [US] {sym} err: {e}")
        return None, None, None, None


# ---------- 港股/其他: 从分析文件提取 (复用 build_analysis_pages 的 extract_metrics) ----------
def find_analysis(sym):
    name = None
    for d in ("美股", "港股", "A股"):
        dd = os.path.join(ANALYSIS_ROOT, d)
        if not os.path.isdir(dd):
            continue
        cands = [os.path.basename(x) for x in glob.glob(os.path.join(dd, "*六步分析法-*.md")) if sym in os.path.basename(x) or sym.split(".")[0] in os.path.basename(x)]
        if cands:
            cands.sort()
            return os.path.join(dd, cands[-1])
    return None


def extract_from_file(sym):
    """尝试从分析文件提取 ROE/净利同比/净现比。返回 (m1,m2,m3) 或 (None,None,None)。"""
    path = find_analysis(sym)
    if not path:
        return None, None, None
    text = open(path, encoding="utf-8").read()
    m1 = m2 = m3 = None
    m = re.search(r"(?:扣非加权|扣非|加权|平均|核心)?\s*ROE[^%\d\n]{0,12}?([\d.]+)\s*%", text)
    if not m:
        m = re.search(r"ROIC[^%\d\n]{0,12}?([\d.]+)\s*%", text)
    if m:
        m1 = m.group(1) + "%"
    m = re.search(r"(?:扣非归母净利润|扣非归母净利|归母净利润|归母净利|核心净利润|核心净利|核心经营利润|净利润)\s*[^\n%]{0,18}?([-+]?[\d.]+)%", text)
    if m and abs(float(m.group(1))) <= 300:
        m2 = float(m.group(1))
    m = re.search(r"净现比[^=\d]*?[=≈]\s*([\d.]+)", text)
    if m:
        m3 = float(m.group(1))
    return m1, m2, m3


_HK_YF_MAP = {
    "00992.HK": "0992.HK", "00100.HK": "0100.HK", "02513.HK": "2513.HK",
    "00625.HK": "0625.HK", "00700.HK": "0700.HK", "09988.HK": "9988.HK",
    "03690.HK": "3690.HK", "01810.HK": "1810.HK", "01024.HK": "1024.HK",
    "09992.HK": "9992.HK",
}


def _gtimg_pe(sym):
    """腾讯行情兜底: 美股[41]=TTM PE, 港股[39]=TTM PE, A股[39]=PE。返回 float 或 None。"""
    try:
        import requests, urllib3
        urllib3.disable_warnings()
        s = requests.Session(); s.trust_env = False; s.verify = False
        sUP = sym.upper()
        if sUP.endswith(".SS"):
            code = "sh" + sUP[:6]
        elif sUP.endswith(".SZ"):
            code = "sz" + sUP[:6]
        elif sUP.endswith(".HK"):
            code = "hk" + sUP.split(".")[0]
        elif re.match(r"^[A-Z.\-]+$", sUP) and "." not in sUP:
            code = "us" + sUP
        else:
            return None
        r = s.get(f"https://qt.gtimg.cn/q={code}", timeout=10)
        f = r.text.split('="', 1)[-1].strip('";\n ').split('~')
        idx = 39 if (sUP.endswith((".SS", ".SZ", ".HK")) or code.startswith(("sh", "sz", "hk"))) else 41
        v = float(f[idx])
        if v > 0:
            return round(v, 1)
        if v < 0:
            return "亏损"
        return None
    except Exception:
        return None


def fetch_pe(sym, is_cn, is_hk):
    """抓市盈率: 美股=Forward PE(yf, 失败腾讯TTM兜底); A股=扣非TTM PE(市值/扣非TTM净利); 港股=TTM PE(yf, 失败腾讯兜底)。失败返回 None。"""
    try:
        if is_cn:
            import tushare as ts
            pro = ts.pro_api()
            ts_code = to_ts_code(sym)
            d0 = TODAY.replace("-", "")
            db = pro.daily_basic(ts_code=ts_code, start_date=d0, end_date=d0, fields="total_mv")
            total_mv = float(db["total_mv"].iloc[0]) if (db is not None and len(db)) else None  # 万元
            if not total_mv:
                return _gtimg_pe(sym)
            # 扣非TTM归母净利 = 最新年报扣非 + 最新累计扣非 - 上年同期累计扣非 (dt_profit, 元)
            try:
                fi = pro.fina_indicator(ts_code=ts_code, start_date="20240101", end_date=d0,
                                        fields="end_date,dt_profit")
                if fi is not None and len(fi) and "dt_profit" in fi.columns and fi["dt_profit"].notna().sum() >= 3:
                    fi = fi.dropna().sort_values("end_date", ascending=False).reset_index(drop=True)
                    latest, latest_end = float(fi["dt_profit"].iloc[0]), fi["end_date"].iloc[0]
                    fy_last = float(fi[fi["end_date"].str.endswith("1231")]["dt_profit"].iloc[0])
                    prev_same = None
                    same_md = latest_end[4:]  # 如 0930
                    for _, row in fi.iloc[1:].iterrows():
                        if row["end_date"][4:] == same_md:
                            prev_same = float(row["dt_profit"])
                            break
                    if prev_same is not None:
                        ttm_profit = fy_last + latest - prev_same  # 元
                        if ttm_profit and ttm_profit > 0:
                            return round(total_mv * 1e4 / ttm_profit, 1)
            except Exception:
                pass
            # tushare扣非字段不可用时: 腾讯 TTM PE 兜底(含非经常损益口径)
            return _gtimg_pe(sym)
        import yfinance as yf
        import warnings
        warnings.filterwarnings("ignore")
        yf_sym = _HK_YF_MAP.get(sym, sym) if is_hk else sym
        try:
            info = yf.Ticker(yf_sym).info or {}
            if is_hk:
                v = info.get("trailingPE")
            else:
                v = info.get("forwardPE") or info.get("trailingPE")
            if v is not None:
                v = float(v)
                return round(v, 1) if v > 0 else "亏损"
        except Exception:
            pass
        return _gtimg_pe(sym)
    except Exception:
        return None

def _gtimg_code(sym):
    """symbol → 腾讯行情代码 (A股 sh/sz+6位, 港股 hk+数字, 美股 us+TICKER)。不支持市场返回 None。"""
    s = sym.upper()
    m = re.match(r"^(\d{6})\.(SS|SZ)$", s)
    if m:
        return ("sh" if m.group(2) == "SS" else "sz") + m.group(1)
    m = re.match(r"^(\d{4,5})\.HK$", s)
    if m:
        return "hk" + m.group(1)
    if re.match(r"^[A-Z\-]+$", s):
        return "us" + s
    return None


def refresh_pe_batch(cfg):
    """批量腾讯行情刷新全市场 TTM PE (一次请求, 每日随价更新)。返回更新数。"""
    import requests, urllib3
    urllib3.disable_warnings()
    items = []
    for group in ("stocks", "hk_stocks"):
        for s in cfg.get(group, []):
            sym = (s.get("symbol") or "").strip().upper()
            if not sym:
                continue
            code = _gtimg_code(sym)
            if code:
                items.append((sym, code, s))
    n = 0
    s_req = requests.Session(); s_req.trust_env = False; s_req.verify = False
    for i in range(0, len(items), 60):
        chunk = items[i:i + 60]
        try:
            r = s_req.get("https://qt.gtimg.cn/q=" + ",".join(c for _, c, _ in chunk), timeout=15)
            lines = {m.group(1): m.group(2).split("~")
                     for m in re.finditer(r'v_([^=]+)="([^"]+)"', r.text)}
        except Exception:
            continue
        for sym, code, s in chunk:
            f = lines.get(code)
            if not f:
                continue
            idx = 41 if code.startswith("us") else 39
            try:
                v = float(f[idx])
            except (IndexError, ValueError):
                continue
            if v > 0:
                s["pe"] = round(v, 1)
                n += 1
            elif v < 0:
                s["pe"] = "亏损"
                n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="仅处理含该关键字的板块(留空全量)")
    ap.add_argument("--pe-only", action="store_true", help="仅批量刷新市盈率(腾讯接口, 秒级), 不跑m1/m2/m3")
    args = ap.parse_args()

    cfg = json.load(open(CFG, encoding="utf-8"))
    if args.pe_only:
        n = refresh_pe_batch(cfg)
        json.dump(cfg, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"PE批量刷新完成: 更新 {n} 只 (腾讯TTM口径)")
        return 0

    existing = metric_sources()
    cfg = json.load(open(CFG, encoding="utf-8"))
    cache = {}
    if os.path.exists(CACHE_F):
        try:
            cache = json.load(open(CACHE_F, encoding="utf-8"))
        except Exception:
            cache = {}

    stats = {"filled": 0, "still_missing": 0, "skip_existing": 0, "none": []}

    for group in ("stocks", "hk_stocks"):
        for s in cfg.get(group, []):
            sym = s.get("symbol", "")
            if not sym:
                continue
            if args.only and not sym.endswith(args.only):
                continue
            cur = existing.get(sym, {})
            # 市盈率(缺失时抓取): 美股=Forward PE / A股=扣非TTM PE / 港股=TTM PE
            if s.get("pe") in (None, ""):
                _pe = fetch_pe(sym, sym.endswith((".SZ", ".SS")), sym.endswith(".HK"))
                if _pe:
                    s["pe"] = _pe
                    print(f"[{sym}] PE = {_pe}")
            # 已有完整三指标则跳过
            if cur.get("m1") not in (None, "") and cur.get("m2") not in (None, "") and cur.get("m3") not in (None, ""):
                stats["skip_existing"] += 1
                continue
            missing = [k for k in ("m1", "m2", "m3") if cur.get(k) in (None, "")]
            m1, m2, m3, src = None, None, None, None
            # 板块判定: A股(.SZ/.SS 且带 market A股)优先 tushare; 港股(.HK) 优先分析文件; 美股其余 yfinance
            is_cn = sym.endswith((".SZ", ".SS"))
            is_hk = sym.endswith(".HK")
            print(f"[{sym}] 缺 {missing} -> ", end="")
            if is_cn:
                m1, m2, m3, src = fetch_ashare(sym)
            elif is_hk:
                m1, m2, m3 = extract_from_file(sym)
                src = "analysis" if (m1 or m2 or m3) else None
                # 分析文件无 → 尝试 yfinance 港股 (只补缺失项)
                if not src:
                    yf_sym = _HK_YF_MAP.get(sym)
                    if yf_sym:
                        _y1, _y2, _y3, _y_src = fetch_us(yf_sym)
                        if _y_src:
                            m1, m2, m3, src = _y1, _y2, _y3, "yf-hk"
            else:
                # 美股: 优先从分析文件提取(官方财报核实数据), yfinance 兜底
                m1, m2, m3 = extract_from_file(sym)
                src = "analysis" if (m1 or m2 or m3) else None
                if not src:
                    m1, m2, m3, src = fetch_us(sym)
            if src:
                stats["filled"] += 1
                if m1 is not None and (cur.get("m1") in (None, "")):
                    s["m1"] = m1
                if m2 is not None and (cur.get("m2") in (None, "")):
                    s["m2"] = m2
                if m3 is not None and (cur.get("m3") in (None, "")):
                    s["m3"] = m3
                cache[sym] = {"m1": m1, "m2": m2, "m3": m3, "src": src, "as_of": TODAY}
                print(f"src={src} m1={m1} m2={m2} m3={m3}")
            else:
                stats["still_missing"] += 1
                stats["none"].append(sym)
                print("仍缺(无数据源)")

    json.dump(cfg, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(cache, open(CACHE_F, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n=== 采集统计 ===")
    print(f"已补齐(至少1项): {stats['filled']} | 仍缺全部: {stats['still_missing']} | 已是完整跳过: {stats['skip_existing']}")
    if stats["none"]:
        print("仍缺标的:", stats["none"])


if __name__ == "__main__":
    main()