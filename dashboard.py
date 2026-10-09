#!/usr/bin/env python3
"""OKX USDT-SWAP T-Range Dashboard - 做T区间可视化看板
用法: python dashboard.py [--open]
选项:
  --open    生成后自动打开浏览器 (默认行为)
  --no-open 不自动打开
"""
import os, sys
# Auto-redirect to conda yolo26 env if not already running in it
# 若由计划任务 pythonw.exe 启动(无控制台), 则重定向到 pythonw.exe 保持静默(不弹黑框)
_YOLO_PY = r"D:\Anaconda\envs\yolo26\python.exe"
_YOLO_PYW = r"D:\Anaconda\envs\yolo26\pythonw.exe"
if sys.executable.lower() not in (_YOLO_PY.lower(), _YOLO_PYW.lower()):
    _target = _YOLO_PYW if os.path.basename(sys.executable).lower().startswith("pythonw") else _YOLO_PY
    if os.path.isfile(_target):
        os.execv(_target, [_target] + sys.argv)
import json, math, webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path

from utils import PROXY, WORKSPACE_ROOT, SCRIPT_DIR, _auto_proxy, _safe_float
# 云端(GitHub Actions)与本地共用 web_shared: 密钥隔离 + Yahoo 行情兜底 + 只读 OKXAPI
# 不再 from strategy_v4, 避免云端 import 策略模块(含实盘下单逻辑与硬编码密钥)
from web_shared import (OKXAPI, CLOUD_MODE, yahoo_price, yahoo_candles,
                        REORDER_PCT, SHORT_ENTRY_MIN, SHORT_TIER_STEP,
                        SHORT_TP1_PCT, SHORT_TP2_PCT, SHORT_REGIME_MAX, SHORT_LEV_TIER,
                        load_okx_keys, load_binance_keys)
API_KEY, API_SECRET, PASSPHRASE = load_okx_keys()
# 货币符号映射 (config 中 ccy 字段; 默认 USD)
CCY_MAP = {"USD": "$", "KRW": "₩", "JPY": "JP¥", "HKD": "HK$", "CNY": "¥"}
# OKX 合约与美股代码冲突的标的: OKX 该 ticker 是加密货币而非美股代币, 价格必须走 Yahoo 兜底
# 例: STX-USDT-SWAP 在 OKX 是 Stacks 币(~$0.3), 而美股希捷 STX 真实价约 $700+
OKX_CONFLICT = {"STX"}
WATCHLIST   = WORKSPACE_ROOT / "watchlist_us" / "config.json"
INST_MAP_F  = SCRIPT_DIR / "instruments.json"
STATUS_F    = SCRIPT_DIR / "script_status.json"
T_RANGE_F   = SCRIPT_DIR / "t_range.json"
CALENDAR_F  = WORKSPACE_ROOT / "watchlist_us" / "earnings_calendar.json"  # 财报/FOMC/经济数据日历
# 云端输出到 web/ 子目录(随 gh-pages 发布), 本地保持原路径供 transform.py 读取
OUTPUT_F    = (SCRIPT_DIR / "web" / "dashboard.html") if CLOUD_MODE else (SCRIPT_DIR / "dashboard.html")

PLACE_EARLY = 0.05

api = OKXAPI(API_KEY, API_SECRET, PASSPHRASE, "0",
             "https://www.okx.com", proxy="" if CLOUD_MODE else PROXY)

# 读取策略运行状态
script_status = {"running": False, "last_check": "", "last_heartbeat": ""}
if STATUS_F.exists():
    try:
        with open(STATUS_F, "r", encoding="utf-8-sig") as f:
            script_status = json.load(f)
    except:
        pass

if script_status.get("running"):
    status_html = ('<span class="status-dot green"></span>'
                   '<span class="status-ok">策略运行中</span>'
                   f' <span style="color:#aaa">(心跳:{script_status.get("last_heartbeat","?")})</span>')
elif CLOUD_MODE and not STATUS_F.exists():
    # 云端只展示行情数据, 不运行交易策略 → 显示数据服务状态而非"策略已停止"
    status_html = ('<span class="status-dot green"></span>'
                   '<span class="status-ok">数据服务</span>'
                   ' <span style="color:#aaa">(云端定时更新)</span>')
else:
    status_html = ('<span class="status-dot red"></span>'
                   '<span class="status-warn">策略已停止</span>'
                   f' <span style="color:#aaa">(上次检查:{script_status.get("last_check","?")})</span>')

inst_map = {}
if INST_MAP_F.exists():
    with open(INST_MAP_F, "r", encoding="utf-8") as fp:
        inst_map = json.load(fp)

t_range = {}
if T_RANGE_F.exists():
    with open(T_RANGE_F, "r", encoding="utf-8") as fp:
        tr_data = json.load(fp)
        for k, v in tr_data.items():
            t_range[k] = v

with open(WATCHLIST, "r", encoding="utf-8") as fp:
    cfg = json.load(fp)

# 六步分析子页面索引: {sym: 'analysis/{sym}.html'} 供看板行跳转
_ANALYSIS_INDEX_F = SCRIPT_DIR / "analysis_index.json"
_ANALYSIS_INDEX = json.load(open(_ANALYSIS_INDEX_F, "r", encoding="utf-8")) if _ANALYSIS_INDEX_F.exists() else {}

def get_price(inst_id, sym=None):
    r = api.get_ticker(inst_id)
    if r.get("code") == "0" and r["data"]:
        v = float(r["data"][0].get("last", 0))
        return v if v > 0 else None
    # 非OKX合约(新增美股/韩股/日股/港股): Yahoo/腾讯兜底
    if sym:
        try:
            import time as _t
            _t.sleep(0.2)  # 缓速, 避免 Yahoo 连续请求被限流
            return yahoo_price(sym)
        except Exception:
            return None
    return None

def _pos_margin_est(p):
    """估算持仓保证金: 逐仓模式 OKX 返回真实 margin; 全仓(cross)模式 margin 为空串 ''
    → 用名义价值(数量×均价)估算, 避免持仓被误判为观察仓. 与 strategy_v4._position_margin_est 一致."""
    m = _safe_float(p.get("margin", 0))
    if m <= 0:
        m = abs(_safe_float(p.get("pos", 0)) * _safe_float(p.get("avgPx", 0)))
    return m

def get_10d_range(inst_id, sym=None):
    r = api.get_candles(inst_id, bar="1D", limit=16)
    if r.get("code") != "0" or not r.get("data"):
        rows = []
        if sym:
            rows = yahoo_candles(sym, "1D", 16)
        if not rows:
            return None, None
        r = {"code": "0", "data": rows}
    weekday_candles = []
    for c in r["data"]:
        ts = int(c[0]) / 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        if dt.weekday() < 5:
            weekday_candles.append(c)
        if len(weekday_candles) >= 10:
            break
    if not weekday_candles:
        return None, None
    highs = [float(c[2]) for c in weekday_candles]
    lows = [float(c[3]) for c in weekday_candles]
    return max(highs), min(lows)

def calc_vol(inst_id, high, low, sym=None):
    """Weekly volatility: 5-day log-return std * sqrt(5), fallback to range width."""
    try:
        r = api.get_candles(inst_id, bar="1D", limit=7)
        if (r.get("code") != "0" or len(r.get("data") or []) < 5) and sym:
            rows = yahoo_candles(sym, "1D", 7)
            if rows:
                r = {"code": "0", "data": rows}
        if r.get("code") == "0" and len(r["data"]) >= 5:
            closes = [float(c[4]) for c in r["data"]]
            closes.reverse()
            import math
            log_rets = [math.log(closes[i+1]/closes[i]) for i in range(len(closes)-1)]
            if len(log_rets) >= 4:
                mean_ret = sum(log_rets) / len(log_rets)
                variance = sum((lr - mean_ret)**2 for lr in log_rets) / (len(log_rets) - 1)
                return variance ** 0.5 * (5 ** 0.5)
    except Exception:
        pass
    if high <= low:
        return 0
    return (high - low) / ((high + low) / 2)

def get_boll_pct(inst_id, current_price, bar="1D", period=20, sym=None):
    """布林带 %B = (Price - Lower) / (Upper - Lower)
    <0 超卖(下轨下方), >1 超买(上轨上方), 0.5 在中轨
    bar: "1D"=日布林, "1W"=周布林"""
    min_period = max(10, period // 2)
    r = api.get_candles(inst_id, bar=bar, limit=period)
    if r.get("code") != "0" or not r.get("data") or len(r["data"]) < min_period:
        if sym:
            rows = yahoo_candles(sym, bar, period)
            if rows and len(rows) >= min_period:
                r = {"code": "0", "data": rows}
            else:
                return None
        else:
            return None
    closes = [float(c[4]) for c in r["data"][:period]]
    ma = sum(closes) / len(closes)
    var = sum((x - ma) ** 2 for x in closes) / len(closes)
    std = var ** 0.5
    upper = ma + 2 * std
    lower = ma - 2 * std
    if upper <= lower:
        return 0.5
    return (current_price - lower) / (upper - lower)

sym_industry = {s["symbol"]: s.get("industry", "") for s in cfg.get("stocks", [])}

# Get positions from OKX
okx_positions = {}
short_positions = {}   # 空头持仓 (net 模式 pos<0), 供"做空"子页使用
pos_r = api.get_positions()
all_positions = []
if pos_r.get("code") == "0" and pos_r.get("data"):
    for p in pos_r["data"]:
        pos_val = _safe_float(p.get("pos", 0))
        sym = p.get("instId", "").replace("-USDT-SWAP", "")
        if pos_val > 0:
            margin_raw = _safe_float(p.get("margin", 0))
            margin_est = _pos_margin_est(p)
            okx_positions[sym] = {
                "size": pos_val,
                "entry": _safe_float(p.get("avgPx", 0)),
                "lever": int(p.get("lever", 10)),
                "pnl": _safe_float(p.get("upl", 0)),
                "margin": margin_raw,
                "margin_est": margin_est,
                "margin_is_est": margin_est != margin_raw,
            }
            all_positions.append({
                "sym": sym, "instId": p.get("instId", ""),
                "size": pos_val, "entry": _safe_float(p.get("avgPx", 0)),
                "lever": int(p.get("lever", 10)),
                "pnl": _safe_float(p.get("upl", 0)),
                "margin": margin_est,
                "margin_is_est": margin_est != margin_raw,
                "pnlRatio": _safe_float(p.get("uplRatio", 0)) * 100,
                "markPx": _safe_float(p.get("markPx", 0)),
                "liqPx": _safe_float(p.get("liqPx", 0)),
                "notionalUsd": _safe_float(p.get("notionalUsd", 0)),
                "mgnRatio": _safe_float(p.get("mgnRatio", 0)),
                "industry": sym_industry.get(sym, ""),
            })
        elif pos_val < 0:
            # 空头: 数量取绝对值, 开仓价/杠杆/盈亏照常
            margin_raw = _safe_float(p.get("margin", 0))
            margin_est = _pos_margin_est(p)
            short_positions[sym] = {
                "size": abs(pos_val),
                "entry": _safe_float(p.get("avgPx", 0)),
                "lever": int(p.get("lever", 10)),
                "pnl": _safe_float(p.get("upl", 0)),
                "margin": margin_raw,
                "margin_est": margin_est,
                "markPx": _safe_float(p.get("markPx", 0)),
            }

# Get account balance


account_balance = {"totalEq": 0, "availBal": 0, "usedMargin": 0, "upl": 0}
bal_r = api.get_balance("USDT")
if bal_r.get("code") == "0" and bal_r.get("data"):
    bd = bal_r["data"][0]
    account_balance["totalEq"] = _safe_float(bd.get("totalEq", 0))
    account_balance["upl"] = _safe_float(bd.get("upl", 0))
    for d in bd.get("details", []):
        if d.get("ccy") == "USDT":
            account_balance["availBal"] = _safe_float(d.get("availBal", 0))
            account_balance["usedMargin"] = _safe_float(d.get("frozenBal", 0))

# Collect data
stocks = cfg.get("stocks", [])
# 港股/A股/ADR 标的并入主循环统一计算指标(非OKX合约, 价格/区间走 Yahoo 兜底)
# 未上市标的(如MOONSHOT, 无symbol)不进入计算, 单独在港股tab底部展示分析结论
_hk_extra = []
# 人民币计价(A股)标的 → 归入A股看板(财报分析基于A股数据); 港币/美元计价 → 港股看板
for _h in cfg.get("hk_stocks", []):
    _sym = (_h.get("symbol") or "").strip()
    if not _sym:
        continue
    _hk_extra.append({
        "symbol": _sym, "name": _h.get("name", ""),
        "buy": float(_h.get("buy") or 0), "sell": float(_h.get("sell") or 0),
        "industry": _h.get("industry", ""), "note": _h.get("note", ""),
        "ccy": _h.get("ccy", "HKD"), "market": _h.get("market", ""),
        "rating": _h.get("rating", ""), "report_date": _h.get("report_date", ""), "is_hk": True,
        "m1": _h.get("m1"), "m2": _h.get("m2"), "m3": _h.get("m3"), "pe": _h.get("pe"),
        "tab": "A股" if (_h.get("ccy") == "CNY" or _h.get("market") == "A股") else "港股",
    })
stocks = list(stocks) + _hk_extra
# 防重复保险: 同一 symbol 只保留首条 (配置手误/同步冲突可能引入重复条目, 曾致线上看板个股重复)
_seen, _dedup = set(), []
for _s in stocks:
    _k = (_s.get("symbol") or "").strip().upper()
    if not _k or _k in _seen:
        continue
    _seen.add(_k)
    _dedup.append(_s)
if len(_dedup) < len(stocks):
    print(f"[warn] 股票列表去重: {len(stocks)} -> {len(_dedup)}")
stocks = _dedup
results = []

def _derive_rating(note):
    """从 note 首部推测持仓建议(美股无rating字段时)。返回标准等级或空串。"""
    if not note:
        return ""
    n = note[:8]
    for kw in ("减持", "回避", "清仓"):  # 负面优先
        if kw in n:
            return kw
    for kw in ("增持", "强烈增持"):
        if kw in n:
            return "增持"
    for kw in ("买入", "建仓", "强烈买入"):
        if kw in n:
            return "买入"
    if "持有" in n:
        return "持有"
    if "观察" in n:
        return "观察"
    return ""

# Load news impact for range adjustment
_news_impact = {}
_absorbed_f = SCRIPT_DIR / "news_impact_absorbed.json"
_news_cache_f = SCRIPT_DIR / "news_cache.json"
if _absorbed_f.exists():
    try:
        with open(_absorbed_f, "r", encoding="utf-8") as f:
            _ab = json.load(f)
        for _sym, _info in _ab.items():
            _ti = _info.get("total_impact", 0)
            if abs(_ti) >= 1:
                # 保存完整字段(total/stock/macro), 供"做空条件"与 strategy 分项阈值一致(#7)
                _news_impact[_sym] = _info
    except:
        pass
if not _news_impact and _news_cache_f.exists():
    try:
        with open(_news_cache_f, "r", encoding="utf-8") as f:
            _nc = json.load(f)
        _macro_neg = [n.get("impact_pct", 0) for n in _nc.get("macro", []) if n.get("impact_pct", 0) < 0]
        _macro_pos = [n.get("impact_pct", 0) for n in _nc.get("macro", []) if n.get("impact_pct", 0) > 0]
        _macro_impact = (max(_macro_neg) if _macro_neg else 0) + (max(_macro_pos) if _macro_pos else 0)
        for _s in _nc.get("stocks", []):
            _si = _s.get("total_impact", 0)
            if not _si and _s.get("news"):
                _neg = [n.get("impact_pct", 0) for n in _s["news"] if n.get("impact_pct", 0) < 0]
                _pos = [n.get("impact_pct", 0) for n in _s["news"] if n.get("impact_pct", 0) > 0]
                _si = (max(_neg) if _neg else 0) + (max(_pos) if _pos else 0)
            _total = _si + _macro_impact * 0.5
            if abs(_total) >= 1:
                _news_impact[_s.get("symbol", "")] = {
                    "total_impact": _total,
                    "stock_impact": _si,
                    "macro_impact": _macro_impact,
                }
    except:
        pass

# ===== Market Regime Data for Dashboard =====
REGIME_CACHE_F = SCRIPT_DIR / "market_regime.json"

def _compute_market_regime():
    """Compute fresh market regime or load from cache if < 30 min old.
    Returns dict with score, stage, exposure, action, labels, or None.
    Primary data source: yfinance (via proxy), fallback: Eastmoney (direct)."""
    import math, time

    # Try cache first
    now_ts = time.time()
    if REGIME_CACHE_F.exists():
        try:
            with open(REGIME_CACHE_F, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if now_ts - cached.get("ts", 0) < 1800:  # 30 min
                return cached
        except:
            pass

    regime = {"score": 0, "stage": "未知", "exposure": 0.85, "action": "",
              "labels": [], "score_trend": 0, "error": None}

    try:
        import pandas as pd
        import numpy as np
        import requests, time, math

        # ===== 统一 K 线获取：yfinance 优先，东方财富 fallback =====
        _YF_MAP = {
            "QQQ": "QQQ", "SPY": "SPY", "NVDA": "NVDA", "MSFT": "MSFT",
            "AMD": "AMD", "AVGO": "AVGO", "AAPL": "AAPL", "TSLA": "TSLA",
            "META": "META", "GOOGL": "GOOGL",
            "SOX": "^SOX", "VIX": "^VIX", "TNX": "^TNX",
        }
        _EM_MAP = {
            "QQQ": (105, "QQQ"), "SPY": (107, "SPY"), "NVDA": (105, "NVDA"),
            "MSFT": (105, "MSFT"), "AMD": (105, "AMD"), "AVGO": (105, "AVGO"),
            "AAPL": (105, "AAPL"), "TSLA": (105, "TSLA"), "META": (105, "META"),
            "GOOGL": (105, "GOOGL"),
        }

        # 云端直连无需代理; 本地走代理。置空代理则 yfinance/requests 走直连
        if CLOUD_MODE or os.getenv("NO_PROXY"):
            _proxy = ""
        else:
            _proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or "http://127.0.0.1:7892"
        os.environ["HTTPS_PROXY"] = _proxy
        os.environ["HTTP_PROXY"] = _proxy

        def _yf_kline(sym, limit=120, interval="1d"):
            try:
                yf_sym = _YF_MAP.get(sym, sym)
                if interval == "1d":
                    if limit > 250: rng = "2y"
                    elif limit > 180: rng = "1y"
                    elif limit > 120: rng = "6mo"
                    else: rng = "3mo"
                elif interval == "1wk":
                    rng = "2y" if limit > 80 else "1y"
                elif interval == "1mo":
                    rng = "5y"
                else:
                    rng = "6mo"
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                proxy_env = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
                proxies = {"http": proxy_env, "https": proxy_env} if proxy_env else None
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}?interval={interval}&range={rng}"
                r = requests.get(url, headers=headers, proxies=proxies, timeout=20)
                data = r.json()
                chart_result = data.get("chart", {}).get("result")
                if not chart_result: return None
                ts = chart_result[0]["timestamp"]
                closes = chart_result[0]["indicators"]["quote"][0]["close"]
                if not ts or not closes: return None
                rows = []
                for t, c in zip(ts, closes):
                    if c is not None:
                        rows.append({"date": pd.to_datetime(t, unit="s"), "close": float(c)})
                if not rows: return None
                df = pd.DataFrame(rows)
                df.set_index("date", inplace=True)
                days_needed = max(30, limit) if interval == "1d" else limit
                return df["close"].tail(days_needed)
            except Exception:
                return None

        def _em_kline(sym, limit=120, klt=101):
            try:
                cfg = _EM_MAP.get(sym)
                if cfg is None:
                    return None
                market, code = cfg
                url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
                       f"?secid={market}.{code}&fields1=f1,f2,f3,f4,f5,f6"
                       f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
                       f"&klt={klt}&fqt=0&end=20500101&lmt={limit}")
                s = requests.Session(); s.trust_env = False; s.proxies = {}
                r = s.get(url, timeout=15)
                data = r.json()
                klines = data.get("data", {}).get("klines", [])
                if not klines: return None
                rows = []
                for line in klines:
                    parts = line.split(",")
                    rows.append({"date": parts[0], "close": float(parts[2])})
                df = pd.DataFrame(rows)
                df["date"] = pd.to_datetime(df["date"])
                df.set_index("date", inplace=True)
                return df["close"].tail(limit)
            except Exception:
                return None

        def _get_kline(sym, limit=120, interval="1d"):
            """Unified: yfinance first, eastmoney fallback."""
            if interval == "1d":
                klt = 101
            elif interval == "1wk":
                klt = 102
            elif interval == "1mo":
                klt = 103
            else:
                klt = 101
            result = _yf_kline(sym, limit=limit, interval=interval)
            if result is not None and len(result) >= 10:
                return result
            if sym in _EM_MAP:
                return _em_kline(sym, limit=limit, klt=klt)
            return None

        from indicators import calc_rsi, detect_rsi_divergence, bollinger_pct

        score = 0.0
        labels = []
        qqq_close = None
        spy_close = None

        # Dim1: SOX 真实费城半导体指数
        try:
            sox_close = _get_kline("SOX", limit=120, interval="1d")
            if sox_close is not None and len(sox_close) >= 20:
                sc = float(sox_close.iloc[-1])
                sma5 = float(sox_close.rolling(5).mean().iloc[-1])
                sma10 = float(sox_close.rolling(10).mean().iloc[-1])
                sma20 = float(sox_close.rolling(20).mean().iloc[-1])
                srsi_raw = calc_rsi(sox_close, 14)
                srsi = float(srsi_raw.iloc[-1]) if not np.isnan(srsi_raw.iloc[-1]) else None
                sox_score = 0.0
                if sc > sma5: sox_score += 0.8; labels.append(f"SOX {sc:.0f}>{sma5:.0f}(5MA)")
                else: labels.append(f"SOX {sc:.0f}<{sma5:.0f}(5MA)")
                if sc > sma10: sox_score += 0.6; labels.append(f"SOX>{sma10:.0f}(10MA)")
                else: labels.append(f"SOX<{sma10:.0f}(10MA)")
                if sc < sma20: sox_score -= 1.0; labels.append(f"SOX<{sma20:.0f}(20MA·转弱)")
                else: labels.append(f"SOX>{sma20:.0f}(20MA)")
                if srsi:
                    if srsi > 75:
                        labels.append(f"SOX_RSI={srsi:.0f}(超买)")
                        if detect_rsi_divergence(sox_close, srsi_raw, 20, "bear"):
                            sox_score -= 0.8; labels.append("SOX_顶背离!")
                    elif srsi < 25:
                        labels.append(f"SOX_RSI={srsi:.0f}(超卖)")
                        if detect_rsi_divergence(sox_close, srsi_raw, 20, "bull"):
                            sox_score += 0.8; labels.append("SOX_底背离!")
                    else: labels.append(f"SOX_RSI={srsi:.0f}")
                score += sox_score
            else: labels.append("SOX:无数据")
        except Exception as e:
            labels.append(f"SOX:n/a({e})")

        # Dim2: TNX 10年美债收益率
        try:
            tnx_close = _get_kline("TNX", limit=60, interval="1d")
            if tnx_close is not None and len(tnx_close) >= 10:
                tnx_cur = float(tnx_close.iloc[-1])
                tnx_10 = float(tnx_close.iloc[-10])
                tnx_change = (tnx_cur - tnx_10) / tnx_10 * 100 if tnx_10 != 0 else 0
                if tnx_change > 10: score -= 0.5; labels.append(f"TNX {tnx_cur:.2f}%(10日+{tnx_change:.0f}%·快速加息利空)")
                elif tnx_change > 5: score -= 0.3; labels.append(f"TNX {tnx_cur:.2f}%(10日+{tnx_change:.0f}%·偏空)")
                elif tnx_change < -10: score += 0.5; labels.append(f"TNX {tnx_cur:.2f}%(10日{tnx_change:.0f}%·快速降息利好)")
                elif tnx_change < -5: score += 0.3; labels.append(f"TNX {tnx_cur:.2f}%(10日{tnx_change:.0f}%·偏多)")
                else: labels.append(f"TNX {tnx_cur:.2f}%(10日{tnx_change:+.0f}%·中性)")
                if tnx_cur > 4.5: labels.append(f"TNX高位{tnx_cur:.2f}%(融资压力)")
            else: labels.append("TNX:无数据")
        except Exception as e:
            labels.append(f"TNX:n/a({e})")

        # Dim3: VIX 恐慌指数
        try:
            vix_close = _get_kline("VIX", limit=60, interval="1d")
            if vix_close is not None and len(vix_close) >= 10:
                vix_cur = float(vix_close.iloc[-1])
                if vix_cur < 15: labels.append(f"VIX {vix_cur:.1f}(贪婪)")
                elif vix_cur < 25: labels.append(f"VIX {vix_cur:.1f}(正常)")
                elif vix_cur < 35: score -= 0.5; labels.append(f"VIX {vix_cur:.1f}(恐慌)")
                else: score += 0.5; labels.append(f"VIX {vix_cur:.1f}(极端恐慌·反向)")
            else: labels.append("VIX:无数据")
        except Exception:
            labels.append("VIX:无数据")

        # Dim4: QQQ 短期趋势
        try:
            qqq_close = _get_kline("QQQ", limit=120, interval="1d")
            if qqq_close is not None and len(qqq_close) >= 60:
                cq = float(qqq_close.iloc[-1])
                qma5 = float(qqq_close.rolling(5).mean().iloc[-1])
                qma10 = float(qqq_close.rolling(10).mean().iloc[-1])
                qma20 = float(qqq_close.rolling(20).mean().iloc[-1])
                qma50 = float(qqq_close.rolling(50).mean().iloc[-1])
                qrsi_raw = calc_rsi(qqq_close, 14)
                qrsi = float(qrsi_raw.iloc[-1]) if not np.isnan(qrsi_raw.iloc[-1]) else None
                qqq_score = 0.0
                if cq > qma5: qqq_score += 0.3
                if cq > qma10: qqq_score += 0.3
                if qma5 > qma10: qqq_score += 0.6; labels.append("QQQ_5MA>10MA(多头)")
                else: qqq_score -= 0.6; labels.append("QQQ_5MA<10MA(空头)")
                if cq < qma20: qqq_score -= 0.8; labels.append(f"QQQ ${cq:.0f}<{qma20:.0f}(20MA·转熊)")
                else: labels.append(f"QQQ ${cq:.0f}>{qma20:.0f}(20MA)")
                if cq < qma50: qqq_score -= 0.5; labels.append(f"QQQ<{qma50:.0f}(50MA)")
                if qrsi:
                    if qrsi > 75:
                        labels.append(f"QQQ_RSI={qrsi:.0f}(超买)")
                        if detect_rsi_divergence(qqq_close, qrsi_raw, 20, "bear"):
                            qqq_score -= 0.5; labels.append("QQQ_顶背离!")
                    elif qrsi < 25:
                        labels.append(f"QQQ_RSI={qrsi:.0f}(超卖)")
                        if detect_rsi_divergence(qqq_close, qrsi_raw, 20, "bull"):
                            qqq_score += 0.5; labels.append("QQQ_底背离!")
                    else: labels.append(f"QQQ_RSI={qrsi:.0f}")
                qqq_weekly = _get_kline("QQQ", limit=60, interval="1wk")
                qqq_bw = bollinger_pct(qqq_weekly, period=20) if qqq_weekly is not None else None
                if qqq_bw is not None:
                    bp, cur, ma, upper, lower = qqq_bw
                    if bp > 0.85: qqq_score -= 0.3; labels.append(f"QQQ周布林={bp:.0%}(高位·${cur:.0f}/${upper:.0f})")
                    elif bp < 0.15: qqq_score += 0.3; labels.append(f"QQQ周布林={bp:.0%}(低位·${cur:.0f}/${lower:.0f})")
                    else: labels.append(f"QQQ周布林={bp:.0%}(${cur:.0f},MA={ma:.0f})")
                score += qqq_score
        except Exception:
            pass

        # Dim5: SPY 系统趋势
        try:
            spy_close = _get_kline("SPY", limit=250, interval="1d")
            if spy_close is not None and len(spy_close) >= 200:
                csp = float(spy_close.iloc[-1])
                sma200 = float(spy_close.rolling(200).mean().iloc[-1])
                sma50 = float(spy_close.rolling(50).mean().iloc[-1])
                if csp > sma200: score += 0.3; labels.append(f"SPY ${csp:.0f}>{sma200:.0f}(200MA·健康)")
                elif csp > sma50: score -= 0.5; labels.append(f"SPY ${csp:.0f}<{sma200:.0f}(200MA·转弱)")
                else: score -= 1.0; labels.append(f"SPY ${csp:.0f}<{sma200:.0f}且<{sma50:.0f}(50MA·系统熊!)")
                spy_weekly = _get_kline("SPY", limit=60, interval="1wk")
                spy_bw = bollinger_pct(spy_weekly, period=20) if spy_weekly is not None else None
                if spy_bw is not None:
                    bp, cur, ma, upper, lower = spy_bw
                    if bp > 0.85: labels.append(f"SPY周布林={bp:.0%}(高位·${cur:.0f}/${upper:.0f})")
                    elif bp < 0.15: labels.append(f"SPY周布林={bp:.0%}(低位·${cur:.0f}/${lower:.0f})")
                    else: labels.append(f"SPY周布林={bp:.0%}(${cur:.0f},MA={ma:.0f})")
        except Exception:
            pass

        # Dim6: 龙头 NVDA+MSFT
        try:
            ok = 0; nvda_up = False
            leader_details = []
            for sym in ["NVDA", "MSFT"]:
                df = _get_kline(sym, limit=60, interval="1d")
                if df is not None and len(df) >= 25:
                    cur = float(df.iloc[-1])
                    ma20 = float(df.rolling(20).mean().iloc[-1])
                    pct = (cur - ma20) / ma20 * 100
                    if cur > ma20: ok += 1
                    if sym == "NVDA" and cur > ma20: nvda_up = True
                    leader_details.append(f"{sym}${cur:.0f}(MA20={ma20:.0f},{pct:+.1f}%)")
            if leader_details:
                if ok >= 2: score += 0.5; labels.append("龙头:NVDA+MSFT>20MA(" + ",".join(leader_details) + ")")
                elif ok == 1 and nvda_up: score -= 1.0; labels.append("龙头:仅NVDA涨(行情脆弱!" + ",".join(leader_details) + ")")
                else: labels.append(f"龙头:{ok}/2>20MA(" + ",".join(leader_details) + ")")
            else: labels.append("龙头:无数据")
        except Exception:
            labels.append("龙头:无数据")

        # Dim7: QQQ/SPY 相对强弱
        if qqq_close is not None and spy_close is not None:
            try:
                df_q = pd.DataFrame({"q": qqq_close})
                df_s = pd.DataFrame({"s": spy_close})
                combined = df_q.join(df_s, how="inner").dropna()
                if len(combined) >= 20:
                    qa = combined["q"]; sa = combined["s"]
                    qr = float(qa.iloc[-1] / qa.iloc[-10] - 1)
                    sr = float(sa.iloc[-1] / sa.iloc[-10] - 1)
                    diff = qr - sr
                    if diff > 0.02: score += 0.4; labels.append(f"QQQ/SPY:科技领涨({diff:+.1%})")
                    elif diff < -0.02: score -= 0.4; labels.append(f"QQQ/SPY:科技跑输({diff:+.1%})")
                    else: labels.append(f"QQQ/SPY:同步({diff:+.1%})")
            except Exception: pass

        # Dim8: 新闻情绪
        try:
            ncf = SCRIPT_DIR / "news_cache.json"
            if ncf.exists():
                with open(ncf, "r", encoding="utf-8") as f: nc = json.load(f)
                macro = nc.get("macro", [])
                if macro:
                    neg_total = sum(n.get("impact_pct", 0) for n in macro if n.get("impact_pct", 0) < 0)
                    pos_total = sum(n.get("impact_pct", 0) for n in macro if n.get("impact_pct", 0) > 0)
                    count = len(macro)
                    if neg_total <= -10: score -= 1.5; labels.append(f"新闻:宏观强利空({neg_total:+.0f}%·{count}条)")
                    elif neg_total <= -5: score -= 0.8; labels.append(f"新闻:宏观偏空({neg_total:+.0f}%·{count}条)")
                    elif pos_total >= 10: score += 1.5; labels.append(f"新闻:宏观强利好({pos_total:+.0f}%·{count}条)")
                    elif pos_total >= 5: score += 0.8; labels.append(f"新闻:宏观偏多({pos_total:+.0f}%·{count}条)")
                    elif neg_total < 0: labels.append(f"新闻:略偏空({neg_total:+.0f}%·{count}条)")
                    else: labels.append(f"新闻:中性({count}条)")
                else: labels.append("新闻:无数据")
            else: labels.append("新闻:无数据")
        except Exception: pass

        # Score → Stage + Exposure
        k = 0.75
        raw = 1.0 / (1.0 + math.exp(-k * score))
        exposure = 0.12 + 0.73 * raw

        # 趋势方向: 复用 market_regime.json 缓存的历史评分推算, 与 strategy_v4._finalize_regime 一致.
        # (原代码 _st 恒为 0, 导致"牛市末期/熊市初期/熊市中期"阶段永远不可达)
        score_trend = 0
        prev_scores = []
        try:
            if REGIME_CACHE_F.exists():
                with open(REGIME_CACHE_F, "r", encoding="utf-8") as f:
                    _prev = json.load(f).get("prev_scores", [])
                if isinstance(_prev, list):
                    prev_scores = [float(x) for x in _prev if isinstance(x, (int, float))]
        except Exception:
            pass
        prev_scores.append(round(score, 2))
        prev_scores = prev_scores[-3:]
        if len(prev_scores) >= 2:
            recent_avg = sum(prev_scores[-2:]) / 2
            earlier_avg = sum(prev_scores[:-2]) / len(prev_scores[:-2]) if len(prev_scores) > 2 else prev_scores[0]
            if recent_avg - earlier_avg > 0.3:
                score_trend = +1
            elif recent_avg - earlier_avg < -0.3:
                score_trend = -1

        if score >= 2.5:
            stage = "牛市中期" if score_trend >= 0 else "牛市末期"
        elif score >= 1.0:
            stage = "牛市初期" if score_trend >= 0 else "牛市末期"
        elif score >= -1.5:
            if score_trend > 0: stage = "牛市初期"
            elif score_trend < 0: stage = "熊市初期"
            else: stage = "震荡"
        elif score >= -3.5:
            stage = "熊市末期" if score_trend >= 0 else "熊市中期"
        else:
            stage = "熊市末期" if score_trend >= 0 else "熊市中期"

        if stage == "熊市初期": exposure = min(exposure, 0.20)
        elif stage == "熊市中期": exposure = min(exposure, 0.15)
        elif stage == "牛市末期": exposure = min(exposure, 0.30)

        stage_action = {"牛市初期":"建仓30-50%","牛市中期":"满仓持有","牛市末期":"减仓至30%以下",
                        "震荡":"维持现有仓位","熊市初期":"立即清仓","熊市中期":"空仓观望","熊市末期":"小仓试探10-20%"}
        stage_color = {"牛市中期":"🟢","牛市初期":"🟡","牛市末期":"🟠",
                       "震荡":"⚪","熊市初期":"🟠","熊市中期":"🔴","熊市末期":"🟡"}

        display_stage = stage
        if stage == "震荡":
            display_stage = "牛市震荡" if score >= 0 else "熊市震荡"

        regime = {
            "score": round(score, 2), "stage": stage, "display_stage": display_stage,
            "exposure": round(exposure, 3),
            "action": stage_action.get(stage, ""), "labels": labels,
            "score_trend": score_trend, "emoji": stage_color.get(stage, ""), "ts": time.time()
        }
        return regime
    except Exception as e:
        return {"score": 0, "stage": "错误", "exposure": 0, "action": "",
                "labels": [f"计算失败:{e}"], "score_trend": 0, "error": str(e)}


# Load regime data
market_regime = _compute_market_regime()

# ===== A股行情判断 (大盘+板块+资金流 Balanced 版) =====
_CN_REGIME_CACHE_F = SCRIPT_DIR / "market_regime_cn.json"

def _cn_kline(secid, limit=120):
    """A股指数日K线(收盘): Yahoo chart. 依次尝试 直连/本地代理(7892,7890) 多源兜底."""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    import requests as _rq, pandas as pd
    attempts = [None, "http://127.0.0.1:7892", "http://127.0.0.1:7890"]
    for _px in attempts:
        try:
            _kw = {}
            if _px:
                _kw["proxies"] = {"http": _px, "https": _px}
            u = (f"https://query1.finance.yahoo.com/v8/finance/chart/{secid}"
                 f"?interval=1d&range=6mo&includePrePost=false")
            j = _rq.get(u, timeout=15, headers={"User-Agent": "Mozilla/5.0"}, verify=False, **_kw).json()
            res = j["chart"]["result"][0]
            ts = res["timestamp"]; closes = res["indicators"]["quote"][0]["close"]
            rows = []
            for t, c in zip(ts, closes):
                if c is not None:
                    rows.append({"date": pd.to_datetime(t, unit="s"), "close": float(c)})
            if len(rows) >= 30:
                df = pd.DataFrame(rows).set_index("date")
                return df["close"].tail(limit)
        except Exception:
            continue
    return None

def _cn_sector_flow():
    """板块主力资金流(东财, 当日, 前3净流入+前3净流出). 失败返回 None。返回 (top_in, top_out, net_total)。"""
    try:
        import requests as _r
        s = _r.Session(); s.trust_env = False; s.proxies = {}
        # 行业资金流排行(按净流入)
        url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=8&po=1&np=1"
               "&fltt=2&invt=2&fid=f62&fs=m:90+t:2"
               "&fields=f12,f14,f62")
        r = s.get(url, timeout=15)
        ak = r.json().get("data", {}).get("diff", [])
        if not ak:
            return None
        top = []
        for it in ak:
            top.append({"name": it.get("f14", ""), "net": it.get("f62")})
        total = sum(x["net"] for x in top if isinstance(x["net"], (int, float)))
        # 净流出侧再取一版按升序
        url_out = url.replace("po=1", "po=0")
        r2 = s.get(url_out, timeout=15)
        bk = r2.json().get("data", {}).get("diff", []) or []
        bot = [{"name": it.get("f14", ""), "net": it.get("f62")} for it in bk][:3]
        return (top[:3], bot, total)
    except Exception:
        return None

def _compute_cn_regime():
    import math, time
    now_ts = time.time()
    if _CN_REGIME_CACHE_F.exists():
        try:
            with open(_CN_REGIME_CACHE_F, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if now_ts - cached.get("ts", 0) < 1800:
                return cached
        except Exception:
            pass
    regime = {"score": 0, "stage": "未知", "exposure": 0.6, "action": "",
              "labels": [], "score_trend": 0, "error": None}
    try:
        import numpy as np, math, time
        from indicators import calc_rsi, detect_rsi_divergence, bollinger_pct
        score = 0.0
        labels = []

        # 指数趋势(Yahoo): 上证/深证可靠(66日数据); 其余若不足序列自动跳过
        _idx = {"上证指数": "000001.SS", "深证成指": "399001.SZ", "创业板指": "399006.SZ",
                "沪深300": "000300.SS", "科创50": "000688.SS"}
        _have = {}
        for nm, sc in _idx.items():
            df = _cn_kline(sc, limit=120)
            if df is not None and len(df) >= 50:
                _have[nm] = df
        if not _have:
            labels.append("A股指数:无数据(东财不可达?)")
        up50, up200 = 0, 0

        # 大盘宽基趋势为主评分轴: 沪深300优先, 失败回退上证(数据可靠)
        hsz = _have.get("沪深300") or _have.get("上证指数")
        hsz_name = "沪深300" if _have.get("沪深300") is not None else ("上证指数" if _have.get("上证指数") is not None else None)
        if hsz is not None:
            cur = float(hsz.iloc[-1]); ma5 = float(hsz.rolling(5).mean().iloc[-1])
            ma10 = float(hsz.rolling(10).mean().iloc[-1]); ma20 = float(hsz.rolling(20).mean().iloc[-1])
            ma60 = float(hsz.rolling(60).mean().iloc[-1])
            if cur > ma5: score += 0.3
            if ma5 > ma10: score += 0.6; labels.append(f"{hsz_name}_5MA>{ma10:.0f}(多头)")
            else: score -= 0.6; labels.append(f"{hsz_name}_5MA<{ma10:.0f}(空头)")
            if cur < ma20: score -= 0.8; labels.append(f"{hsz_name} {cur:.0f}<{ma20:.0f}(20MA·转弱)")
            else: labels.append(f"{hsz_name} {cur:.0f}>{ma20:.0f}(20MA)")
            if cur < ma60: score -= 0.5; labels.append(f"{hsz_name}<{ma60:.0f}(60MA·中期弱)")
            else: labels.append(f"{hsz_name}>{ma60:.0f}(60MA·中期强)")
        else:
            labels.append("大盘指数:无数据")
        # 各指数维度(供前端分行显示)
        for _nm, _df in _have.items():
            _c = float(_df.iloc[-1]); _m20 = float(_df.rolling(20).mean().iloc[-1])
            if _c > _m20:
                labels.append(f"{_nm}: {_c:.0f}>{_m20:.0f}(20MA·多头)")
            else:
                labels.append(f"{_nm}: {_c:.0f}<{_m20:.0f}(20MA·空头)")
        # 市场广度: 各指数相对20MA
        for nm, df in _have.items():
            ma20 = float(df.rolling(20).mean().iloc[-1])
            if float(df.iloc[-1]) > ma20:
                up50 += 1
        if _have:
            breadth = up50 / len(_have)
            if breadth >= 0.8: score += 0.5; labels.append(f"广度: {up50}/{len(_have)}>20MA(普涨)")
            elif breadth <= 0.2: score -= 0.5; labels.append(f"广度: {up50}/{len(_have)}>20MA(普跌)")
            else: labels.append(f"广度: {up50}/{len(_have)}>20MA")

        # 创业板/科创 风险偏好(成长)
        cyb = _have.get("创业板指"); kc = _have.get("科创50")
        if cyb is not None and kc is not None:
            if float(cyb.iloc[-1]) > float(cyb.rolling(20).mean().iloc[-1]) and float(kc.iloc[-1]) > float(kc.rolling(20).mean().iloc[-1]):
                score += 0.4; labels.append("成长(创业板+科创)>20MA(风险偏好高)")
            elif float(cyb.iloc[-1]) < float(cyb.rolling(20).mean().iloc[-1]) and float(kc.iloc[-1]) < float(kc.rolling(20).mean().iloc[-1]):
                score -= 0.4; labels.append("成长(创业板+科创)<20MA(避险)")
            else: labels.append("成长风格分化")

        # 板块资金流
        sf = _cn_sector_flow()
        if sf:
            top_in, top_out, _ = sf
            if top_in:
                _in_sum = sum(x["net"] for x in top_in if isinstance(x["net"], (int, float)))
                _out_sum = sum(x["net"] for x in top_out if isinstance(x["net"], (int, float)))
                _net = _in_sum + _out_sum  # out 为负值
                if _net > 0: score += 0.5; labels.append(f"板块资金: 净流入{_net/1e8:.1f}亿(" + ",".join(x["name"] for x in top_in) + ")")
                elif _net < -0: score -= 0.5; labels.append(f"板块资金: 净流出{-_net/1e8:.1f}亿(" + ",".join(x["name"] for x in top_out) + ")")
                else: labels.append("板块资金: 中性")

        # 新闻情绪(复用宏观)
        try:
            ncf = SCRIPT_DIR / "news_cache.json"
            if ncf.exists():
                with open(ncf, "r", encoding="utf-8") as f:
                    nc = json.load(f)
                macro = nc.get("macro", [])
                if macro:
                    neg_total = sum(n.get("impact_pct", 0) for n in macro if n.get("impact_pct", 0) < 0)
                    pos_total = sum(n.get("impact_pct", 0) for n in macro if n.get("impact_pct", 0) > 0)
                    if neg_total <= -10: score -= 1.0; labels.append(f"新闻:宏观强利空({neg_total:+.0f}%·{len(macro)}条)")
                    elif pos_total >= 10: score += 1.0; labels.append(f"新闻:宏观强利好({pos_total:+.0f}%·{len(macro)}条)")
                    elif neg_total <= -5: score -= 0.5; labels.append(f"新闻:宏观偏空({neg_total:+.0f}%·{len(macro)}条)")
                    elif pos_total >= 5: score += 0.5; labels.append(f"新闻:宏观偏多({pos_total:+.0f}%·{len(macro)}条)")
                    else: labels.append(f"新闻:中性({len(macro)}条)")
                else: labels.append("新闻:无数据")
        except Exception:
            pass

        # Score → Stage + Exposure (复用美股同一映射)
        k = 0.75
        raw = 1.0 / (1.0 + math.exp(-k * score))
        exposure = 0.12 + 0.73 * raw

        score_trend = 0
        prev_scores = []
        try:
            if _CN_REGIME_CACHE_F.exists():
                with open(_CN_REGIME_CACHE_F, "r", encoding="utf-8") as f:
                    _prev = json.load(f).get("prev_scores", [])
                if isinstance(_prev, list):
                    prev_scores = [float(x) for x in _prev if isinstance(x, (int, float))]
        except Exception:
            pass
        prev_scores.append(round(score, 2))
        prev_scores = prev_scores[-3:]
        if len(prev_scores) >= 2:
            recent_avg = sum(prev_scores[-2:]) / 2
            earlier_avg = sum(prev_scores[:-2]) / max(1, len(prev_scores[:-2]))
            if recent_avg - earlier_avg > 0.3: score_trend = +1
            elif recent_avg - earlier_avg < -0.3: score_trend = -1

        if score >= 2.5: stage = "牛市中期" if score_trend >= 0 else "牛市末期"
        elif score >= 1.0: stage = "牛市初期" if score_trend >= 0 else "牛市末期"
        elif score >= -1.5:
            stage = "牛市初期" if score_trend > 0 else ("熊市初期" if score_trend < 0 else "震荡")
        elif score >= -3.5: stage = "熊市末期" if score_trend >= 0 else "熊市中期"
        else: stage = "熊市末期" if score_trend >= 0 else "熊市中期"

        if stage == "熊市初期": exposure = min(exposure, 0.20)
        elif stage == "熊市中期": exposure = min(exposure, 0.15)
        elif stage == "牛市末期": exposure = min(exposure, 0.30)

        stage_action = {"牛市初期":"建仓30-50%","牛市中期":"满仓持有","牛市末期":"减仓至30%以下",
                        "震荡":"维持现有仓位","熊市初期":"立即清仓","熊市中期":"空仓观望","熊市末期":"小仓试探10-20%"}
        stage_color = {"牛市中期":"🟢","牛市初期":"🟡","牛市末期":"🟠",
                       "震荡":"⚪","熊市初期":"🟠","熊市中期":"🔴","熊市末期":"🟡"}
        display_stage = stage
        if stage == "震荡": display_stage = "牛市震荡" if score >= 0 else "熊市震荡"

        regime = {"score": round(score, 2), "stage": stage, "display_stage": display_stage,
                  "exposure": round(exposure, 3), "action": stage_action.get(stage, ""),
                  "labels": labels, "score_trend": score_trend,
                  "emoji": stage_color.get(stage, ""), "ts": time.time()}
        return regime
    except Exception as e:
        return {"score": 0, "stage": "错误", "exposure": 0, "action": "",
                "labels": [f"A股计算失败:{e}"], "score_trend": 0, "error": str(e)}

cn_regime = _compute_cn_regime()

# ===== End A股 Market Regime =====

# 每只股票最近实际财报日(财报日历来 done 事件), 用于"最近财报日期"列与估值区间过期标红
_past_report = {}
try:
    import json as _jce
    _ce_evs = _jce.load(open(CALENDAR_F, "r", encoding="utf-8")).get("events", [])
    for _ce in _ce_evs:
        if _ce.get("status") == "done" and _ce.get("symbol") not in ("FED", "ECON"):
            _cs, _cd = _ce.get("symbol"), _ce.get("date")
            if _cs and _cd and not _cd.endswith("??") and _cd > _past_report.get(_cs, ""):
                _past_report[_cs] = _cd
except Exception:
    _past_report = {}


def _report_imminent(report_date):
    """今天 - 最近财报日 > 80 天 → 财报临近(约每季90天一报). 解析失败返回False."""
    try:
        if not report_date:
            return False
        _rd = datetime.strptime(report_date.strip()[:10], "%Y-%m-%d")
        return (datetime.now() - _rd).days > 80
    except Exception:
        return False


for s in stocks:
    sym = s["symbol"]
    buy_price = float(s.get("buy", 0))
    sell_price = float(s.get("sell", 0))
    name = s.get("name", "")
    industry = s.get("industry", "")
    note = s.get("note", "")
    ccy = CCY_MAP.get(s.get("ccy", "USD"), "$")
    # 韩股/日股: 用实时汇率(USDKRW/USDJPY)把当地价/区间/估值统一换算成美元显示。
    # 分位/波动率/盈亏比/亏损率等相对指标不依赖币种, 换算前后不变, 故只需缩放价格类字段。
    fx = 1.0
    _local_ccy = s.get("ccy", "USD")
    if _local_ccy in ("KRW", "JPY"):
        _fxt = "KRW=X" if _local_ccy == "KRW" else "JPY=X"
        _fxr = yahoo_price(_fxt)
        if _fxr and _fxr > 0:
            fx = _fxr
        ccy = "$"   # 统一按美元显示
    inst_id = inst_map.get(sym, f"{sym}-USDT-SWAP")
    # OKX 合约与美股代码冲突的标的: 置空 inst_id 强制走 Yahoo, 避免误用加密货币价格(如 STX=Stacks)
    if sym in OKX_CONFLICT:
        inst_id = ""

    px = get_price(inst_id, sym)
    if not px:
        results.append({"sym": sym, "name": name, "error": "no price",
                        "is_hk": s.get("is_hk", False), "market": s.get("market", "美股"), "rating": s.get("rating", "")})
        continue

    boll_pct = get_boll_pct(inst_id, px, sym=sym)
    boll_pct_w = get_boll_pct(inst_id, px, bar="1W", period=20, sym=sym)

    manual = t_range.get(sym)
    if manual:
        actual_low = int(manual["low"] / 5) * 5
        actual_high = int(manual["high"]) + (1 if manual["high"] % 1 > 0 else 0)
        src = "manual"
    else:
        ref_high, ref_low = get_10d_range(inst_id, sym)
        if ref_high and ref_low:
            actual_low = int(ref_low / 5) * 5
            actual_high = int(ref_high) + (1 if ref_high % 1 > 0 else 0)
            src = "10d"
        else:
            results.append({"sym": sym, "name": name, "error": "no range",
                            "is_hk": s.get("is_hk", False), "market": s.get("market", "美股"), "rating": s.get("rating", "")})
            continue

    # Apply news impact: asymmetric adjustment on 10d high/low, then recalc
    news_shift_pct = 0
    ni = _news_impact.get(sym, 0)
    if ni:
        ni_total = ni.get("total_impact", 0) if isinstance(ni, dict) else ni
        news_shift_pct = max(-15, min(15, ni_total)) / 100.0
    if abs(news_shift_pct) >= 0.01:
        range_width = actual_high - actual_low
        if news_shift_pct > 0:
            actual_low += range_width * news_shift_pct * 0.3
            actual_high += range_width * news_shift_pct * 1.2
        else:
            actual_low += range_width * news_shift_pct * 1.2
            actual_high += range_width * news_shift_pct * 0.3
        actual_low = int(actual_low / 5) * 5
        actual_high = int(actual_high) + (1 if actual_high % 1 > 0 else 0)

    vol = calc_vol(inst_id, actual_high, actual_low, sym)
    pct = (px - actual_low) / (actual_high - actual_low) if actual_high > actual_low else 0.5

    if vol > 0.075:
        buy1_pct, buy2_pct, buy3_pct, sell1_pct, sell2_pct = 0.27, 0.19, 0.11, 0.75, 0.82
    else:
        buy1_pct, buy2_pct, buy3_pct, sell1_pct, sell2_pct = 0.32, 0.24, 0.16, 0.70, 0.78

    p_buy1 = actual_low + (actual_high - actual_low) * buy1_pct
    p_buy2 = actual_low + (actual_high - actual_low) * buy2_pct
    p_buy3 = actual_low + (actual_high - actual_low) * buy3_pct
    p_sell1 = actual_low + (actual_high - actual_low) * sell1_pct
    p_sell2 = actual_low + (actual_high - actual_low) * sell2_pct

    if buy_price > 0 and sell_price > 0:
        win = sell_price - px
        loss = px - buy_price
        ratio = round(win / loss, 2) if loss > 0 else 999
        loss_rate = round((buy_price - px) / px * 100, 1)
    else:
        ratio = 0
        loss_rate = -999

    # 可交易三条件(与美股/港股/A股统一): 现价处于Buy1线以下 + 盈亏比>REORDER_PCT + 潜在亏损>-10%
    eligible = (px <= p_buy1) and ratio > REORDER_PCT and loss_rate > -10

    if pct <= buy3_pct + PLACE_EARLY:
        zone = "BUY3区"
        zone_class = "zone-buy3"
    elif pct <= buy2_pct + PLACE_EARLY:
        zone = "BUY2区"
        zone_class = "zone-buy2"
    elif pct <= buy1_pct + PLACE_EARLY:
        zone = "BUY1区"
        zone_class = "zone-buy1"
    elif pct >= sell2_pct - PLACE_EARLY:
        zone = "SELL2区"
        zone_class = "zone-sell2"
    elif pct >= sell1_pct - PLACE_EARLY:
        zone = "SELL1区"
        zone_class = "zone-sell1"
    elif pct < 0.50:
        zone = "下半区"
        zone_class = "zone-lower"
    else:
        zone = "上半区"
        zone_class = "zone-upper"

    # --- 做空视角 (与 strategy_v4._manage_short_orders 一致) ---
    # 入场阶梯: short1/2/3 = 75%/81%/87% 分位; 止盈: 回落至 55% 平60%, 45% 平剩余
    # 新闻条件与策略一致(#7): 个股 impact <= -2 或 宏观 impact <= -3 才判定为可做空
    short_ok_market = market_regime.get("score", 99) < SHORT_REGIME_MAX
    news_val = _news_impact.get(sym, 0)
    if isinstance(news_val, dict):
        short_news_neg = (news_val.get("stock_impact", 0) <= -2) or (news_val.get("macro_impact", 0) <= -3)
        short_news_val = news_val.get("total_impact", 0)
    else:
        short_news_neg = False
        short_news_val = 0
    _w = actual_high - actual_low
    short1_px = actual_low + _w * SHORT_ENTRY_MIN
    short2_px = actual_low + _w * (SHORT_ENTRY_MIN + SHORT_TIER_STEP)
    short3_px = actual_low + _w * (SHORT_ENTRY_MIN + 2 * SHORT_TIER_STEP)
    short_tp1 = actual_low + _w * SHORT_TP1_PCT
    short_tp2 = actual_low + _w * SHORT_TP2_PCT
    if pct >= SHORT_ENTRY_MIN + 2 * SHORT_TIER_STEP:
        short_zone = "SHORT3区"; short_zone_class = "szone3"
    elif pct >= SHORT_ENTRY_MIN + SHORT_TIER_STEP:
        short_zone = "SHORT2区"; short_zone_class = "szone2"
    elif pct >= SHORT_ENTRY_MIN:
        short_zone = "SHORT1区"; short_zone_class = "szone1"
    elif pct >= SHORT_TP1_PCT:
        short_zone = "接近做空区"; short_zone_class = "szone-near"
    elif pct >= SHORT_TP2_PCT:
        short_zone = "止盈区"; short_zone_class = "szone-tp"
    else:
        short_zone = "低位观望"; short_zone_class = "szone-low"
    short_eligible = short_ok_market and short_news_neg and pct >= SHORT_ENTRY_MIN
    sp = short_positions.get(sym)

    pos_info = okx_positions.get(sym)
    results.append({
        "sym": sym, "name": name, "industry": industry, "note": note,
        "ccy": ccy,
        "is_hk": s.get("is_hk", False), "market": s.get("market", "美股"),
        "rating": s.get("rating", "") or _derive_rating(note),
        "tab": s.get("tab", "港股") if s.get("is_hk", False) else "美股",
        # 财务指标(财报更新后手动维护于config watch), 第二列为同比(正红负绿)
        "m1": s.get("m1"), "m2": s.get("m2"), "m3": s.get("m3"), "pe": s.get("pe"),
        "has_analysis": sym in _ANALYSIS_INDEX,
        "px": px/fx, "alow": actual_low/fx, "ahigh": actual_high/fx,
        "vol": vol, "pct": pct, "src": src, "boll_pct": boll_pct, "boll_pct_w": boll_pct_w,
        "buy1_pct": buy1_pct, "buy2_pct": buy2_pct, "buy3_pct": buy3_pct,
        "sell1_pct": sell1_pct, "sell2_pct": sell2_pct,
        "p_buy1": p_buy1/fx, "p_buy2": p_buy2/fx, "p_buy3": p_buy3/fx,
        "p_sell1": p_sell1/fx, "p_sell2": p_sell2/fx,
        "news_shift_pct": news_shift_pct,

        "ratio": ratio, "loss_rate": loss_rate, "eligible": eligible,
        "zone": zone, "zone_class": zone_class,
        "buy_cfg": buy_price/fx, "sell_cfg": sell_price/fx,
        "report_date": s.get("report_date", ""),
        "stale_valuation": bool(s.get("report_date") and _past_report.get(sym) and _past_report[sym] > s.get("report_date", "")),
        # 财报临近: 今天 - 最近财报日 > 80 天 → 即将发布下一份财报(每季约90天)
        "report_imminent": _report_imminent(s.get("report_date", "")),
        "round_price": s.get("ccy", "USD") in ("KRW", "JPY"),   # 韩/日股换算美元后全档取整显示
        # 全仓持仓 margin 可能为空串 → 用 margin_est(名义价值估算) 判断真实持仓/观察仓
        "has_pos": pos_info is not None and pos_info["margin_est"] >= 1,
        "is_obs": pos_info is not None and pos_info["margin_est"] < 1,
        "pos_size": pos_info["size"] if pos_info else 0,
        "pos_entry": pos_info["entry"] if pos_info else 0,
        "pos_lever": pos_info["lever"] if pos_info else 0,
        "pos_pnl": pos_info["pnl"] if pos_info else 0,
        "pos_margin": pos_info["margin_est"] if pos_info else 0,

        # 做空字段
        "short1_px": short1_px, "short2_px": short2_px, "short3_px": short3_px,
        "short_tp1": short_tp1, "short_tp2": short_tp2,
        "short_ok_market": short_ok_market, "short_news_neg": short_news_neg,
        "short_news_val": round(short_news_val, 1),
        "short_zone": short_zone, "short_zone_class": short_zone_class,
        "short_eligible": short_eligible,
        "short_has_pos": sp is not None,
        "short_pos_lever": sp["lever"] if sp else 0,
        "short_pos_margin": sp["margin_est"] if sp else 0,
        "short_pos_pnl": sp["pnl"] if sp else 0,
        "short_pos_entry": sp["entry"] if sp else 0,
    })

# Sort by pct
results.sort(key=lambda x: x.get("pct", 0))

# Add index monitors (QQQ, SPY) at the top
INDEX_MONITORS = [
    {"sym": "QQQ", "name": "纳斯达克100", "industry": "指数"},
    {"sym": "SPY", "name": "标普500",    "industry": "指数"},
]
index_results = []
for idx_info in INDEX_MONITORS:
    sym = idx_info["sym"]
    inst_id = f"{sym}-USDT-SWAP"
    px = get_price(inst_id, sym)
    if not px:
        index_results.append({"sym": sym, "name": idx_info["name"], "error": "no price", "is_index": True})
        continue
    boll_pct = get_boll_pct(inst_id, px, sym=sym)
    boll_pct_w = get_boll_pct(inst_id, px, bar="1W", period=20, sym=sym)
    ref_high, ref_low = get_10d_range(inst_id, sym)
    if ref_high and ref_low:
        actual_low = int(ref_low / 5) * 5
        actual_high = int(ref_high) + (1 if ref_high % 1 > 0 else 0)
        vol = calc_vol(inst_id, actual_high, actual_low, sym)
        pct = (px - actual_low) / (actual_high - actual_low) if actual_high > actual_low else 0.5
    else:
        actual_low = actual_high = 0
        vol = 0
        pct = 0.5

    if vol > 0.075:
        buy1_pct, buy2_pct, buy3_pct, sell1_pct, sell2_pct = 0.27, 0.19, 0.11, 0.75, 0.82
    else:
        buy1_pct, buy2_pct, buy3_pct, sell1_pct, sell2_pct = 0.32, 0.24, 0.16, 0.70, 0.78

    p_buy1 = actual_low + (actual_high - actual_low) * buy1_pct if actual_high > 0 else 0
    p_buy2 = actual_low + (actual_high - actual_low) * buy2_pct if actual_high > 0 else 0
    p_buy3 = actual_low + (actual_high - actual_low) * buy3_pct if actual_high > 0 else 0
    p_sell1 = actual_low + (actual_high - actual_low) * sell1_pct if actual_high > 0 else 0
    p_sell2 = actual_low + (actual_high - actual_low) * sell2_pct if actual_high > 0 else 0

    if pct <= buy3_pct + PLACE_EARLY:
        zone, zone_class = "BUY3区", "zone-buy3"
    elif pct <= buy2_pct + PLACE_EARLY:
        zone, zone_class = "BUY2区", "zone-buy2"
    elif pct <= buy1_pct + PLACE_EARLY:
        zone, zone_class = "BUY1区", "zone-buy1"
    elif pct >= sell2_pct - PLACE_EARLY:
        zone, zone_class = "SELL2区", "zone-sell2"
    elif pct >= sell1_pct - PLACE_EARLY:
        zone, zone_class = "SELL1区", "zone-sell1"
    elif pct < 0.50:
        zone, zone_class = "下半区", "zone-lower"
    else:
        zone, zone_class = "上半区", "zone-upper"

    index_results.append({
        "sym": sym, "name": idx_info["name"], "industry": idx_info["industry"],
        "px": px, "alow": actual_low, "ahigh": actual_high,
        "ccy": "$",
        "is_hk": False, "market": "美股", "rating": "",
        "vol": vol, "pct": pct, "src": "10d", "boll_pct": boll_pct, "boll_pct_w": boll_pct_w,
        "is_index": True,
        "buy1_pct": buy1_pct, "buy2_pct": buy2_pct, "buy3_pct": buy3_pct,
        "sell1_pct": sell1_pct, "sell2_pct": sell2_pct,
        "p_buy1": p_buy1, "p_buy2": p_buy2, "p_buy3": p_buy3,
        "p_sell1": p_sell1, "p_sell2": p_sell2,
        "news_shift_pct": 0,

        "ratio": 0, "loss_rate": 0, "eligible": False,
        "zone": zone, "zone_class": zone_class,
        "buy_cfg": 0, "sell_cfg": 0,
        "report_date": "", "stale_valuation": False, "report_imminent": False,
        "round_price": False,
        "has_pos": False, "is_obs": False,
        "pos_size": 0, "pos_entry": 0, "pos_lever": 0, "pos_pnl": 0, "pos_margin": 0,
    })

results = index_results + results

# ============================================================
# 港股/A股/ADR 未上市标的 (无symbol, 未进入主循环, 单独在港股tab底部展示分析结论)
# ============================================================
_hk_extra_rows = []
for _h in cfg.get("hk_stocks", []):
    _sym = (_h.get("symbol") or "").strip()
    if _sym:
        continue
    _hk_extra_rows.append({
        "name": _h.get("name", ""), "market": _h.get("market", ""),
        "rating": _h.get("rating", ""), "note": _h.get("note", ""),
        "ccy": CCY_MAP.get(_h.get("ccy", "HKD"), "HK$"),
    })
hk_extra_json = json.dumps(_hk_extra_rows, ensure_ascii=False)

# ============================================================
# Generate rebalance advice via DeepSeek V4 Pro
# ============================================================
def _generate_rebalance_advice(results, index_results):
    """调用DeepSeek V4 Pro生成调仓建议"""
    import requests as req
    import time as _time

    NEWS_CFG_F = SCRIPT_DIR / "news_config.json"
    NEWS_CACHE_F = SCRIPT_DIR / "news_cache.json"
    llm_cfg = {}
    if NEWS_CFG_F.exists():
        with open(NEWS_CFG_F, "r", encoding="utf-8") as f:
            llm_cfg = json.load(f)
    api_key = os.getenv("DEEPSEEK_API_KEY", llm_cfg.get("api_key", ""))
    model = os.getenv("NEWS_LLM_MODEL", llm_cfg.get("model", "deepseek-v4-flash"))
    base_url = os.getenv("NEWS_LLM_BASE_URL", llm_cfg.get("base_url", "https://api.deepseek.com"))

    if not api_key:
        return "DeepSeek API未配置，无法生成调仓建议。请在news_config.json中配置api_key。"

    # Collect position stocks
    pos_stocks = [r for r in results if r.get("has_pos") and not r.get("is_index") and not r.get("is_hk")]
    # Collect eligible stocks (no position)
    eli_stocks = [r for r in results if r.get("eligible") and not r.get("has_pos") and not r.get("is_index") and not r.get("is_hk")]
    # Collect observation stocks
    obs_stocks = [r for r in results if r.get("is_obs") and not r.get("is_index") and not r.get("is_hk")]
    # Index info
    idx_info = {}
    for ir in index_results:
        if ir.get("px"):
            boll_d = ir.get("boll_pct")
            boll_w = ir.get("boll_pct_w")
            idx_info[ir["sym"]] = {
                "price": ir["px"],
                "boll_d": f"{boll_d*100:.1f}%" if boll_d is not None else "N/A",
                "boll_w": f"{boll_w*100:.1f}%" if boll_w is not None else "N/A",
            }

    # Load news cache
    news_summary = ""
    if NEWS_CACHE_F.exists():
        try:
            with open(NEWS_CACHE_F, "r", encoding="utf-8") as f:
                nc = json.load(f)
            parts = []
            for mn in nc.get("macro", []):
                parts.append(f"[宏观] {mn.get('title_cn', mn.get('title',''))} ({mn.get('direction','')}, 影响{mn.get('impact_pct',0)}%)")
            for st in nc.get("stocks", []):
                for n in st.get("news", []):
                    parts.append(f"[{st['symbol']}] {n.get('title_cn', n.get('title',''))} ({n.get('direction','')}, 影响{n.get('impact_pct',0)}%)")
            news_summary = "\n".join(parts[:30])
        except:
            pass

    # Build prompt
    pos_lines = []
    for r in pos_stocks:
        boll_d = f"{r['boll_pct']*100:.1f}%" if r.get("boll_pct") is not None else "N/A"
        boll_w = f"{r['boll_pct_w']*100:.1f}%" if r.get("boll_pct_w") is not None else "N/A"
        pos_lines.append(f"  {r['sym']}({r['name']}) 价格${r['px']:.2f} 区间{r['zone']} 日布林{boll_d} 周布林{boll_w} 盈亏${r.get('pos_pnl',0):.2f} 杠杆{r.get('pos_lever',10)}x 📎{r.get('news_shift_pct',0)*100:+.0f}%")

    eli_lines = []
    for r in eli_stocks:
        boll_d = f"{r['boll_pct']*100:.1f}%" if r.get("boll_pct") is not None else "N/A"
        boll_w = f"{r['boll_pct_w']*100:.1f}%" if r.get("boll_pct_w") is not None else "N/A"
        eli_lines.append(f"  {r['sym']}({r['name']}) 价格${r['px']:.2f} 区间{r['zone']} 日布林{boll_d} 周布林{boll_w} ratio={r['ratio']}")

    obs_lines = []
    for r in obs_stocks:
        obs_lines.append(f"  {r['sym']}({r['name']}) 价格${r['px']:.2f} 保证金${r.get('pos_margin',0):.2f}")

    idx_lines = []
    for sym, info in idx_info.items():
        idx_lines.append(f"  {sym}: 价格${info['price']:.2f} 日布林{info['boll_d']} 周布林{info['boll_w']}")

    prompt = f"""你是美股合约交易顾问。根据以下数据直接给出调仓建议。

【大盘】
{chr(10).join(idx_lines) if idx_lines else '无数据'}

【持仓】
{chr(10).join(pos_lines) if pos_lines else '无持仓'}

【可买入】
{chr(10).join(eli_lines) if eli_lines else '无'}

【观察仓(保证金<$1)】
{chr(10).join(obs_lines) if obs_lines else '无'}

【新闻影响】
{news_summary if news_summary else '无新闻数据'}

【输出规则】严格按以下5条格式，每条给出具体股票、关键数据、操作理由：
1. 大盘：[QQQ/SPY布林位置+宏观新闻→加仓/观望/减仓，说明理由]
2. 止盈：[股票+盈亏金额+布林位置+新闻→减仓/锁利/持有，说明理由] 或 无
3. 止损：[股票+亏损金额+布林位置+新闻→止损/减仓/持有，说明理由] 或 无
4. 买入：[股票+区间位置+ratio+布林+新闻→买入/轻仓/观望，说明理由] 或 无
5. 观察：[股票+现状→加仓/清仓/继续观察，说明理由] 或 无

禁止重复原始数据，引用关键数字即可。每条30-50字，总300字以内。"""

    try:
        _time.sleep(2)
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        for _attempt in range(3):
            try:
                # DeepSeek 是国外域名: 本地走代理, 云端(NO_PROXY)直连
                resp = req.post(f"{base_url}/v1/chat/completions", headers=headers, json=payload, timeout=120,
                               proxies=_auto_proxy(base_url), verify=False)
                if resp.status_code == 200:
                    msg = resp.json()["choices"][0]["message"]
                    content = msg.get("content", "").strip()
                    reasoning = msg.get("reasoning_content", "").strip()
                    advice = content if content else reasoning
                    if not advice:
                        print(f"  [WARN] 调仓建议返回空content, retry {_attempt+1}/3...")
                        _time.sleep(5)
                        continue
                    print(f"  调仓建议生成成功 ({len(advice)}字)")
                    return advice
                else:
                    err_msg = resp.text[:200] if resp.text else "无详情"
                    print(f"  [WARN] DeepSeek API错误: {resp.status_code} {err_msg}")
                    return f"DeepSeek API错误: {resp.status_code} - {err_msg}"
            except Exception as e:
                if _attempt < 2:
                    print(f"  [WARN] 调仓建议超时({e}), retry {_attempt+1}/3...")
                    _time.sleep(5)
                else:
                    raise
    except Exception as e:
        print(f"  [WARN] 调仓建议生成失败: {e}")
        return f"调仓建议生成失败: {e}"

print("Generating rebalance advice...")
advice_text = ""
try:
    advice_text = _generate_rebalance_advice(results, index_results)
except Exception as e:
    print(f"  [WARN] 调仓建议生成异常: {e}")

# Load wait_queue & deposit_alert from strategy_state.json
STATE_F = SCRIPT_DIR / "strategy_state.json"
wait_queue_html = ""
deposit_alert_html = ""
if STATE_F.exists():
    try:
        with open(STATE_F, "r", encoding="utf-8") as f:
            _st = json.load(f)
        _wq = [r for r in results
               if r.get("eligible") and not r.get("is_hk") and not r.get("is_index")
               and not r.get("has_pos") and (r.get("zone") or "").startswith("BUY")]
        _wq.sort(key=lambda r: r.get("ratio", 0), reverse=True)
        if _wq:
            _wq_items = "".join(f'<span class="wq-item">⏳ {w["sym"]} <span class="wq-ratio">ratio={"∞" if w.get("ratio",0)>=999 else w.get("ratio",0)}</span></span>' for w in _wq)
            wait_queue_html = f'<div class="wait-queue-box"><h3>等待队列 <span class="wq-count">{len(_wq)}</span></h3><div class="wq-list">{_wq_items}</div><div class="wq-note">仅符合买入规则(ratio&gt;2、买入区、潜在亏损&gt;-10%)的标的, 持仓卖出后按ratio优先补位</div></div>'
        _da = _st.get("deposit_alert")
        if _da:
            deposit_alert_html = f'<div class="deposit-alert-box"><h3>⚠️ 入金提醒</h3><div class="da-detail">{_da["eligible"]}只eligible但仅{_da["positions"]}个持仓 | 每股预算: ${_da["per_stock_budget"]:.2f} | 可用: ${_da["available"]:.2f}</div><div class="da-action">建议入金 <strong>${_da["shortfall"]:.0f}</strong></div></div>'
    except:
        pass

# Load news data for the News tab
NEWS_CACHE_F = SCRIPT_DIR / "news_cache.json"
news_content_html = ""
news_meta = "未更新"
if NEWS_CACHE_F.exists():
    try:
        with open(NEWS_CACHE_F, "r", encoding="utf-8") as f:
            news_data = json.load(f)
        _dir_badge = {"positive": '<span class="badge positive">利好</span>', "negative": '<span class="badge negative">利空</span>', "neutral": '<span class="badge neutral">中性</span>'}
        _type_clr = {"宏观": "#378ADD", "行业": "#6C5CE7", "个股": "#2c2c2a"}
        _parts = []
        if news_data.get("macro"):
            _items = ""
            for n in news_data["macro"]:
                b = _dir_badge.get(n["direction"], _dir_badge["neutral"])
                tc = _type_clr.get(n["type"], "#888")
                it = f"+{n['impact_pct']}%" if n["impact_pct"] > 0 else f"{n['impact_pct']}%"
                ic = "#3B6D11" if n["impact_pct"] > 0 else "#A32D2D" if n["impact_pct"] < 0 else "#888"

                ls = f'<a href="{n["url"]}" target="_blank" rel="noopener">' if n.get("url") else ""
                le = "</a>" if n.get("url") else ""
                _summ = f'<span class="news-summary-inline">{n.get("summary","")}</span>' if n.get("summary") else ""
                _reason = f'<span class="reason-inline">{n["reason"]}</span>' if n.get("reason") else ""
                _items += f'<div class="news-card"><div class="news-header"><span class="date">{n.get("pubDate","")}</span><span class="provider">{n.get("provider","")}</span><span class="type-tag" style="background:{tc}">{n.get("type","宏观")}</span>{b}<span class="impact" style="color:{ic}">{it}</span></div>{ls}<div class="title-row"><span class="title-cn">{n.get("title_cn",n.get("title",""))}</span>{_reason}{_summ}</div>{le}</div>'
            _macro_summary = ""
            if news_data.get("macro_summary"):
                _macro_summary = f'<div class="ai-summary"><span class="ai-summary-tag">AI总结</span>{news_data["macro_summary"]}</div>'
            _parts.append(f'<div class="stock-section macro-section"><div class="stock-header macro-header"><div class="stock-info"><span class="stock-sym">宏观</span><span class="stock-name">美联储 / 就业 / 通胀 / 利率</span><span class="macro-tag">全局影响</span></div></div><div class="news-list">{_items}</div>{_macro_summary}</div>')
        for s in news_data.get("stocks", []):
            pt = '<span class="pos-tag">持仓</span>' if s.get("has_position") else ""
            et = '<span class="eli-tag">可买</span>' if s.get("is_eligible") and not s.get("has_position") else ""
            tot = s.get("total_impact", 0)
            agg = f'<span class="agg-positive">综合影响 +{tot}%</span>' if tot > 0 else (f'<span class="agg-negative">综合影响 {tot}%</span>' if tot < 0 else '<span class="agg-neutral">综合影响 0%</span>')
            ne = s.get("next_earnings", "未公布")
            el = ""
            if ne and ne not in ("未公布", "查询失败"):
                try:
                    ed = datetime.strptime(ne[:10], "%Y-%m-%d"); du = (ed - datetime.now()).days
                    if du < 0: el = f"上次财报: {ne[:10]}"
                    elif du <= 7: el = f'<span class="ern-soon">财报 {ne[:10]}（{du}天后）</span>'
                    elif du <= 30: el = f'<span class="ern-near">财报 {ne[:10]}（{du}天后）</span>'
                    else: el = f'<span class="ern-far">财报 {ne[:10]}（{du}天后）</span>'
                except: el = f"财报: {ne[:10]}"
            _items = ""
            for n in s.get("news", []):
                b = _dir_badge.get(n["direction"], _dir_badge["neutral"])
                tc = _type_clr.get(n.get("type", "个股"), "#888")
                it = f"+{n['impact_pct']}%" if n["impact_pct"] > 0 else f"{n['impact_pct']}%"
                ic = "#3B6D11" if n["impact_pct"] > 0 else "#A32D2D" if n["impact_pct"] < 0 else "#888"

                ls = f'<a href="{n["url"]}" target="_blank" rel="noopener">' if n.get("url") else ""
                le = "</a>" if n.get("url") else ""
                _summ = f'<span class="news-summary-inline">{n.get("summary","")}</span>' if n.get("summary") else ""
                _reason = f'<span class="reason-inline">{n["reason"]}</span>' if n.get("reason") else ""
                _items += f'<div class="news-card"><div class="news-header"><span class="date">{n.get("pubDate","")}</span><span class="provider">{n.get("provider","")}</span><span class="type-tag" style="background:{tc}">{n.get("type","个股")}</span>{b}<span class="impact" style="color:{ic}">{it}</span></div>{ls}<div class="title-row"><span class="title-cn">{n.get("title_cn",n.get("title",""))}</span>{_reason}{_summ}</div>{le}</div>'
            _stock_summary = ""
            if s.get("stock_summary"):
                _stock_summary = f'<div class="ai-summary"><span class="ai-summary-tag">AI总结</span>{s["stock_summary"]}</div>'
            _parts.append(f'<div class="stock-section"><div class="stock-header"><div class="stock-info"><span class="stock-sym">{s["symbol"]}</span><span class="stock-name">{s["name"]}</span>{pt}{et}</div><div class="stock-agg">{agg}</div></div><div class="earnings-row">{el}</div><div class="news-list">{_items}</div>{_stock_summary}</div>')
        news_content_html = "\n".join(_parts) if _parts else '<div class="no-data">暂无新闻数据</div>'
        news_meta = f"更新: {news_data.get('generated_at', '-')} | 来源: Google News"
        print(f"  News tab content loaded ({len(news_data.get('stocks',[]))} stocks)")
    except Exception as e:
        news_content_html = f'<div class="no-data">新闻数据加载失败: {e}</div>'
else:
    news_content_html = '<div class="no-data">暂无新闻数据，请先运行 news.py</div>'

# ===== 币安互补策略数据(双账户互备看板; 无网络/未运行时降级显示, 不阻断主看板) =====
BINANCE_API_KEY, BINANCE_API_SECRET = load_binance_keys()
_bapi = None
_bin_bal = None
_bin_pos = []
_bin_inst_map = {}
_bin_state = {}
_bin_boll = {}
_bin_status = {"running": False, "last_heartbeat": ""}
try:
    sys.path.insert(0, str(SCRIPT_DIR / "binance"))   # 币安模块已移入 binance/ 子目录
    from api_binance import BinanceAPI as _BAPI
    _bapi = _BAPI(BINANCE_API_KEY, BINANCE_API_SECRET, "", "0",
                  "https://fapi.binance.com", proxy="" if CLOUD_MODE else PROXY)
    _r = _bapi.get_balance("USDT")
    if _r.get("code") == "0":
        _bd = _r["data"][0]
        _det = (_bd.get("details") or [{}])[0]
        _bin_bal = {
            "totalEq": _safe_float(_bd.get("totalEq", 0)),
            "upl": _safe_float(_bd.get("upl", 0)),
            "availBal": _safe_float(_det.get("availEq", 0)),
            "usedMargin": _safe_float(_det.get("frozenBal", 0)),
        }
    _r = _bapi.get_positions()
    if _r.get("code") == "0":
        for _p in _r.get("data", []):
            _bin_pos.append({
                "sym": _p.get("instId", "").replace("-USDT-SWAP", ""),
                "instId": _p.get("instId", ""),
                "size": _safe_float(_p.get("pos", 0)),
                "entry": _safe_float(_p.get("avgPx", 0)),
                "lever": int(_safe_float(_p.get("lever", 0)) or 10),
                "pnl": _safe_float(_p.get("upl", 0)),
                "markPx": _safe_float(_p.get("markPx", 0)),
                "notionalUsd": _safe_float(_p.get("notionalUsd", 0)),
                "mgnRatio": _safe_float(_p.get("mgnRatio", 0)),
            })
    _r = _bapi.get_instruments("SWAP")
    if _r.get("code") == "0":
        for _i in _r["data"]:
            _bin_inst_map[_i["instId"]] = _i
except Exception as _e:
    print(f"  [WARN] 币安 API 数据获取失败(降级): {_e}")

for _p, _t in ((SCRIPT_DIR / "binance" / "strategy_binance_state.json", _bin_state),
               (SCRIPT_DIR / "binance" / "strategy_binance_boll.json", _bin_boll),
               (SCRIPT_DIR / "binance" / "script_status_binance.json", _bin_status)):
    if _p.exists():
        try:
            with open(_p, "r", encoding="utf-8") as _f:
                _t.update(json.load(_f))
        except Exception:
            pass

def _bin_boll_pct(inst_id, bar="1D", period=20):
    """币安布林带 %B(与 OKX get_boll_pct 同口径, 用币安 K线)."""
    if _bapi is None:
        return None
    try:
        r = _bapi.get_candles(inst_id, bar=bar, limit=period)
        if r.get("code") != "0" or not r.get("data") or len(r["data"]) < 10:
            return None
        closes = [float(c[4]) for c in r["data"]]
        ma = sum(closes) / len(closes)
        std = (sum((x - ma) ** 2 for x in closes) / len(closes)) ** 0.5
        upper, lower = ma + 2 * std, ma - 2 * std
        if upper <= lower:
            return None
        return (closes[0] - lower) / (upper - lower)
    except Exception:
        return None

def _bin_leg_html():
    """黄金/BTC 布林带腿卡片."""
    legs = {
        "XAU": ("XAU-USDT-SWAP", "黄金", "10/15/20x"),
        "BTC": ("BTC-USDT-SWAP", "比特币", "5/7/10x"),
    }
    out = ""
    for sym, (inst_id, name, lev_txt) in legs.items():
        st = _bin_boll.get(sym) or {}
        d_pct = _bin_boll_pct(inst_id, "1D")
        w_pct = _bin_boll_pct(inst_id, "1W")
        direction = "做多" if (w_pct is not None and w_pct >= 0.5) else "做空" if w_pct is not None else "行情不可用"
        side = st.get("side", "空仓")
        side_txt = {"long": "多", "short": "空"}.get(side, "空仓")
        pos_txt = (f'<span class="pos-tag">持仓 {side_txt} {st.get("size", 0):.4f}</span>'
                   f' <span style="color:#666;font-size:12px">@{st.get("entry", 0):.2f} {st.get("lev", "-")}x</span>') if side in ("long", "short") else ""
        tiers = st.get("entry_tiers", [])
        tiers_txt = "".join(f'<span class="eli-tag">档{i}</span>' for i in tiers) if tiers else '<span style="color:#aaa;font-size:12px">未挂单</span>'
        boll_d_txt = f'{(d_pct*100):.1f}%' if d_pct is not None else "N/A"
        boll_w_txt = f'{(w_pct*100):.1f}%' if w_pct is not None else "N/A"
        boll_d_color = "#3B6D11" if (d_pct is not None and d_pct <= 0.10) else "#A32D2D" if (d_pct is not None and d_pct >= 0.90) else "#2c2c2a"
        out += f'''<div class="stock-section">
  <div class="stock-header"><div class="stock-info">
    <span class="stock-sym">{sym}</span><span class="stock-name">{name}</span>
    <span class="macro-tag">{lev_txt}</span>{pos_txt}</div>
    <div class="stock-agg">周布林方向: <strong>{direction}</strong></div>
  </div>
  <div style="padding:12px 20px;display:flex;gap:32px;flex-wrap:wrap;font-size:13px">
    <span>日布林 %B: <b style="color:{boll_d_color}">{boll_d_txt}</b> <span style="color:#aaa">(≤10% 多/≥90% 空入场)</span></span>
    <span>周布林 %B: <b>{boll_w_txt}</b> <span style="color:#aaa">(≥0.5 做多 / <0.5 做空)</span></span>
    <span>已挂档位: {tiers_txt}</span>
    <span>状态: {side_txt}</span>
  </div>
</div>'''
    return out

binance_tab_html = ""
try:
    _legs_html = _bin_leg_html()
    # 币安美股腿持仓(排除黄金/BTC 后按 symbol 归并)
    _stock_pos = {}
    for _p in _bin_pos:
        _sym = _p["sym"]
        if _sym in ("XAU", "BTC"):
            continue
        _stock_pos[_sym] = _p
    _pos_rows = ""
    for _sym, _p in sorted(_stock_pos.items()):
        _side = "多" if _p["size"] > 0 else "空"
        _pnl_cls = "pnl-pos" if _p["pnl"] >= 0 else "pnl-neg"
        _pos_rows += (f'<tr><td style="font-weight:600">{_sym}</td>'
                      f'<td>{_side}</td><td>{abs(_p["size"]):.4f}</td>'
                      f'<td>${_p["entry"]:.2f}</td><td>${_p["markPx"]:.2f}</td>'
                      f'<td>{_p["lever"]}x</td><td>${_p["notionalUsd"]:.2f}</td>'
                      f'<td class="{_pnl_cls}">{"+" if _p["pnl"] >= 0 else ""}{_p["pnl"]:.2f}</td></tr>')
    _pos_html = (f'<table class="pos-table"><thead><tr><th>代码</th><th>方向</th><th>数量</th>'
                 f'<th>开仓价</th><th>标记价</th><th>杠杆</th><th>名义价值</th><th>盈亏</th></tr></thead>'
                 f'<tbody>{_pos_rows}</tbody></table>') if _pos_rows else '<div class="no-data">币安美股腿无持仓</div>'
    # 币安策略运行状态
    _bstatus = _bin_status.get("running", False)
    _bstatus_html = ('<span class="status-dot green"></span><span class="status-ok">币安策略运行中</span>'
                     f' <span style="color:#aaa">(心跳:{_bin_status.get("last_heartbeat", "?")})</span>'
                     if _bstatus else
                     '<span class="status-dot red"></span><span class="status-warn">币安策略未运行</span>'
                     f' <span style="color:#aaa">(状态文件:{_bin_status.get("last_check", "?")})</span>')
    # 账户汇总(币安 vs 欧易)
    if _bin_bal:
        _cards = (f'<div class="card"><div class="label">币安总权益</div><div class="value blue">${_bin_bal["totalEq"]:.2f}</div></div>'
                  f'<div class="card"><div class="label">币安可用</div><div class="value green">${_bin_bal["availBal"]:.2f}</div></div>'
                  f'<div class="card"><div class="label">币安已用保证金</div><div class="value">${_bin_bal["usedMargin"]:.2f}</div></div>'
                  f'<div class="card"><div class="label">币安持仓数</div><div class="value blue">{len(_bin_pos)}</div></div>')
    else:
        _cards = '<div class="no-data">币安行情/账户不可用(需代理连接 fapi.binance.com)</div>'
    # 美股腿状态(等待队列/每股预算/持仓数)
    _bin_base_capital = _bin_state.get("base_capital", 0)
    _bin_positions = _bin_state.get("positions", {})
    _bin_wq = _bin_state.get("wait_queue", [])
    _wq_html = ("".join(f'<span class="wq-item">⏳ {w["sym"]}</span>' for w in _bin_wq)) if _bin_wq else '<span style="color:#aaa">空</span>'
    binance_tab_html = f'''
<div class="pos-header-bar">
  <div style="font-size:14px;font-weight:500">币安互补账户（防守腿）</div>
  <div style="display:flex;align-items:center;gap:12px;font-size:13px">{_bstatus_html}</div>
</div>
<div class="summary">{_cards}</div>
<div class="summary" style="grid-template-columns:repeat(3,1fr)">
  <div class="card"><div class="label">币安 base_capital</div><div class="value blue">${_bin_base_capital:.2f}</div></div>
  <div class="card"><div class="label">美股腿持仓数</div><div class="value blue">{len(_bin_positions)}</div></div>
  <div class="card"><div class="label">等待队列</div><div class="value" style="font-size:14px">{_wq_html}</div></div>
</div>
<div class="stock-section"><div class="stock-header"><div class="stock-info"><span class="stock-sym">🥇</span><span class="stock-name">黄金/BTC 布林带腿</span><span class="macro-tag">双向 · 新闻调区间</span></div></div></div>
{_legs_html}
<div class="pos-header-bar" style="margin-top:12px"><div style="font-size:14px;font-weight:500">币安美股腿持仓（区间马丁，保守化）</div></div>
{_pos_html}
'''
except Exception as _e:
    print(f"  [WARN] 币安看板构建失败: {_e}")
    binance_tab_html = f'<div class="no-data">币安看板数据不可用: {_e}</div>'

# ===== Build Calendar tab HTML (财报/FOMC/经济数据日历) =====
import html as _html
calendar_html = ""
if CALENDAR_F.exists():
    try:
        with open(CALENDAR_F, "r", encoding="utf-8") as f:
            _cal = json.load(f)
        _now = datetime.now()
        _evts = _cal.get("events", [])
        _upcoming = [e for e in _evts if e.get("status") == "upcoming"]
        _pending  = [e for e in _evts if e.get("status") == "pending"]
        _done     = [e for e in _evts if e.get("status") == "done"]

        def _cal_type_tag(sym):
            return {"FED": ("🏦 FOMC", "#6C5CE7"), "ECON": ("📊 经济数据", "#00838F")}.get(sym, ("📅 财报", "#2c2c2a"))

        def _cal_row(e):
            sym = e.get("symbol", "")
            tag, tc = _cal_type_tag(sym)
            d = e.get("date", "")
            days = ""
            if d and d.endswith("??"):
                date_display = d.replace("2026-08-??", "8月待定")
                if date_display == d:
                    date_display = "待确认"
            else:
                date_display = d
                try:
                    du = (datetime.strptime(d, "%Y-%m-%d") - _now).days
                    if du >= 0:
                        cls = "ern-soon" if du <= 7 else ("ern-near" if du <= 30 else "ern-far")
                        days = f'<span class="{cls}">（{du}天后）</span>'
                except Exception:
                    pass
            file = e.get("file") or ""
            fnote = f'<a style="color:#185FA5;text-decoration:none" title="{_html.escape(file)}" href="javascript:void(0)">📄</a> ' if file else ""
            return (f'<tr><td class="date">{date_display}</td>'
                    f'<td><span class="type-tag" style="background:{tc}">{tag}</span></td>'
                    f'<td class="stock"><strong>{_html.escape(e.get("name",""))}</strong> <span class="sym">{_html.escape(sym)}</span></td>'
                    f'<td>{_html.escape(e.get("period",""))}</td>'
                    f'<td>{fnote}<span class="note">{_html.escape(e.get("note",""))}</span>{days}</td></tr>')

        def _cal_table(events, empty_msg):
            if not events:
                return f'<div class="no-data">{empty_msg}</div>'
            rows = "".join(_cal_row(e) for e in sorted(events, key=lambda x: (x.get("date","").endswith("??"), x.get("date",""))))
            return ('<table class="cal-table"><thead><tr><th>日期</th><th>类型</th><th>事件/公司</th><th>财期</th><th>备注</th></tr></thead>'
                    f'<tbody>{rows}</tbody></table>')

        _up7  = sum(1 for e in _upcoming if e.get("date") and not e.get("date","").endswith("??")
                    and 0 <= (datetime.strptime(e["date"], "%Y-%m-%d") - _now).days <= 7)
        _up30 = sum(1 for e in _upcoming if e.get("date") and not e.get("date","").endswith("??")
                    and 0 <= (datetime.strptime(e["date"], "%Y-%m-%d") - _now).days <= 30)
        cal_events_json = json.dumps(_evts, ensure_ascii=False)
        calendar_html = f'''
<div style="background:#fff;border-radius:12px;border:1px solid #e5e5e5;padding:12px 20px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center">
  <div style="font-size:14px;font-weight:500">财报 / FOMC / 经济数据日历</div>
  <div style="font-size:12px;color:#888">数据更新: {_html.escape(str(_cal.get("updated","-")))} | 共 {len(_evts)} 条 | 月历视图, 点击 ◀ ▶ 切换月份</div>
</div>
<div class="summary">
  <div class="card"><div class="label">7天内</div><div class="value red">{_up7}</div></div>
  <div class="card"><div class="label">30天内</div><div class="value" style="color:#BA7517">{_up30}</div></div>
  <div class="card"><div class="label">即将发布</div><div class="value blue">{len(_upcoming)}</div></div>
  <div class="card"><div class="label">待确认日期</div><div class="value" style="color:#185FA5">{len(_pending)}</div></div>
</div>
<div class="stock-section">
  <div class="stock-header"><div class="stock-info"><span class="stock-sym">📅</span><span class="stock-name">月历提醒</span><span class="macro-tag">一行一周</span></div></div>
  <div class="cal-nav">
    <button class="cal-nav-btn" id="cal-prev">◀ 上月</button>
    <span class="cal-title" id="cal-title"></span>
    <button class="cal-nav-btn" id="cal-next">下月 ▶</button>
  </div>
  <div class="cal-grid cal-dow-row">
    <div class="cal-dow">周一</div><div class="cal-dow">周二</div><div class="cal-dow">周三</div><div class="cal-dow">周四</div><div class="cal-dow">周五</div><div class="cal-dow">周六</div><div class="cal-dow">周日</div>
  </div>
  <div class="cal-grid" id="cal-grid"></div>
</div>
<div class="stock-section">
  <div class="stock-header"><div class="stock-info"><span class="stock-sym">?</span><span class="stock-name">待确认日期</span><span class="macro-tag">{len(_pending)}</span></div></div>
  {_cal_table(_pending, '暂无待确认日期的事件')}
</div>'''
        print(f"  Calendar tab content loaded ({len(_evts)} events)")
    except Exception as e:
        calendar_html = f'<div class="no-data">日历数据加载失败: {e}</div>'
        cal_events_json = "[]"
else:
    calendar_html = '<div class="no-data">日历数据不存在，请先运行 watchlist_us/update_calendar.py</div>'
    cal_events_json = "[]"

# ===== Build Market Regime HTML =====
regime_html = ""
if market_regime.get("error"):
    regime_html = f'<div class="no-data">行情判断计算失败: {market_regime["error"]}</div>'
else:
    r = market_regime
    score = r["score"]
    stage = r.get("display_stage", r["stage"])
    exposure = r["exposure"]
    action = r["action"]
    labels = r["labels"]
    emoji = r.get("emoji", "")
    
    # ----- Verdict Banner -----
    score_color = "#3B6D11" if score >= 2.0 else "#639922" if score >= 0.5 else "#BA7517" if score >= -1.5 else "#A32D2D" if score >= -3.5 else "#8B0000"
    stage_bg = {"牛市中期":"#e8f5e9","牛市初期":"#fff8e1","牛市末期":"#fff3e0",
                "震荡":"#f5f5f5","熊市初期":"#fce4ec","熊市中期":"#ffebee","熊市末期":"#fff8e1",
                "牛市震荡":"#f0f4e8","熊市震荡":"#faf0f0"}.get(stage, "#f5f5f5")
    
    # ----- Build dimension data with direction scores -----
    dim_groups = {
        "SOX":     {"label": "费城半导体 SOX",   "color": "#1565C0", "score": None, "detail": []},
        "TNX":     {"label": "10Y美债利率",      "color": "#6A1B9A", "score": None, "detail": []},
        "VXN":     {"label": "波动率 VXN/VIX",  "color": "#E65100", "score": None, "detail": []},
        "QQQ":     {"label": "纳指100 QQQ",      "color": "#2E7D32", "score": None, "detail": []},
        "SPY":     {"label": "标普500 SPY",      "color": "#5D4037", "score": None, "detail": []},
        "龙头":    {"label": "龙头股 NVDA/MSFT", "color": "#C62828", "score": None, "detail": []},
        "QQQ/SPY": {"label": "科技相对强弱",     "color": "#00838F", "score": None, "detail": []},
        "新闻":    {"label": "宏观新闻情绪",     "color": "#4527A0", "score": None, "detail": []},
    }
    # 按 key 长度降序匹配: "QQQ/SPY" 必须先于 "QQQ" 判断, 否则 QQQ/SPY 背离标签被错误归入 QQQ 行
    # VXN/VIX 同组: 策略缓存标签为 VXN, dashboard 自算标签为 VIX
    for lb in labels:
        for key, info in sorted(dim_groups.items(), key=lambda kv: -len(kv[0])):
            if lb.startswith(key) or (key == "VXN" and lb.startswith("VIX")):
                info["detail"].append(lb)
                break
    
    for key, info in dim_groups.items():
        detail = info["detail"]
        if not detail:
            info["score"] = 50  # neutral / no data
            info["detail"].append("未获取到数据")
            continue
        # Score from label content: positive keywords → bullish, negative → bearish
        bullish = 0; bearish = 0; total = 0
        for d in detail:
            total += 1
            if any(w in d for w in ["涨","多头","利好","偏多",">","底背离","上行","健康","反向","低价"]):
                bullish += 1
            if any(w in d for w in ["跌","空头","利空","偏空","强利空","<","顶背离","下行","转弱","转熊","恐慌","系统熊","脆弱","跑输","高位"]):
                bearish += 1
        if total == 0:
            info["score"] = 50
        elif bullish + bearish == 0:
            info["score"] = 50  # all neutral signals
        else:
            info["score"] = int(bullish / (bullish + bearish) * 100)
        # Adjust for extremes
        if key == "VXN" and info["score"] > 50:
            info["score"] = min(info["score"], 50)  # high VXN = bad for bulls
        if "贪婪" in " ".join(detail):
            info["score"] = 25  # greed = late bull risk
    
    # ----- Build rows -----
    rows_html = ""
    for key in ["TNX", "VXN", "SOX", "QQQ", "SPY", "QQQ/SPY", "龙头", "新闻"]:
        info = dim_groups[key]
        s = info["score"]
        # Color gradient: red(0-40) → gray(40-60) → green(60-100)
        if s <= 30:    bar_color = "#C62828"  # strong bearish
        elif s <= 45:  bar_color = "#E57373"  # mild bearish
        elif s <= 55:  bar_color = "#9E9E9E"  # neutral
        elif s <= 70:  bar_color = "#66BB6A"  # mild bullish
        else:          bar_color = "#2E7D32"  # strong bullish
        
        # Direction label
        if s <= 35:     dir_text = "偏空"
        elif s <= 48:   dir_text = "略空"
        elif s <= 52:   dir_text = "中性"
        elif s <= 65:   dir_text = "略多"
        else:           dir_text = "偏多"
        dir_color = bar_color
        
        # Detail text (first 2 items)
        detail_str = " · ".join(info["detail"][:3]) if info["detail"] else "无数据"
        
        rows_html += f'''
        <div class="r-row">
            <div class="r-label">{info["label"]}</div>
            <div class="r-bar-wrap">
                <div class="r-bar-track">
                    <div class="r-bar-fill" style="width:{s}%;background:{bar_color}"></div>
                    <div class="r-bar-marker" style="left:{s}%"></div>
                </div>
            </div>
            <div class="r-score" style="color:{dir_color}">{s}% {dir_text}</div>
            <div class="r-detail">{detail_str}</div>
        </div>'''
    
    # ----- Exposure explanation with account comparison -----
    total_eq = account_balance.get("totalEq", 0)
    used_mgn = account_balance.get("usedMargin", 0)
    exposure_usd = total_eq * exposure
    used_pct = (used_mgn / total_eq * 100) if total_eq > 0 else 0
    remain_usd = total_eq * exposure - used_mgn
    exposure_explain = (f'建议敞口: <strong>{exposure*100:.0f}% = ${exposure_usd:,.0f}</strong> '
                        f'| 当前已用保证金: <strong style="color:#{"BA7517" if used_pct > exposure*100 else "3B6D11"}">${used_mgn:,.2f} ({used_pct:.1f}%)</strong> '
                        f'| 剩余可用: <strong style="color:#{"3B6D11" if remain_usd > 0 else "A32D2D"}">${remain_usd:+,.2f}</strong>')
    
    all_labels_str = " | ".join(labels)
    regime_html = f'''
    <div class="regime-verdict" style="background:{stage_bg}">
        <div class="regime-verdict-left">
            <div class="regime-verdict-stage">{emoji} {stage}</div>
            <div class="regime-verdict-score" style="color:{score_color}">综合评分: {score:+.2f}</div>
        </div>
        <div class="regime-verdict-right">
            <div class="regime-verdict-exposure">敞口限额: <strong>{exposure*100:.0f}%</strong></div>
            <div class="regime-verdict-action">建议: <strong>{action}</strong></div>
        </div>
    </div>
    <div class="regime-explain">{exposure_explain}</div>
    <div style="background:#fff;border-radius:12px;border:1px solid #e5e5e5;padding:8px 16px 12px">
        <div class="r-legend"><span class="r-leg-dot" style="background:#C62828"></span>偏空 <span class="r-leg-dot" style="background:#9E9E9E;margin-left:12px"></span>中性 <span class="r-leg-dot" style="background:#2E7D32;margin-left:12px"></span>偏多</div>
        {rows_html}
    </div>
    <div class="regime-detail-bar">{all_labels_str}</div>'''

# ===== End Market Regime HTML =====

# ===== Build A股行情判断 HTML (复用 .regime-* CSS) =====
cn_regime_html = ""
if cn_regime.get("error"):
    cn_regime_html = f'<div class="no-data">A股行情判断计算失败: {cn_regime["error"]}</div>'
else:
    cr = cn_regime
    c_score = cr["score"]; c_stage = cr.get("display_stage", cr["stage"])
    c_exposure = cr["exposure"]; c_action = cr["action"]
    c_labels = cr["labels"]; c_emoji = cr.get("emoji", "")
    c_score_color = "#3B6D11" if c_score >= 2.0 else "#639922" if c_score >= 0.5 else "#BA7517" if c_score >= -1.5 else "#A32D2D" if c_score >= -3.5 else "#8B0000"
    c_stage_bg = {"牛市中期":"#e8f5e9","牛市初期":"#fff8e1","牛市末期":"#fff3e0",
                  "震荡":"#f5f5f5","熊市初期":"#fce4ec","熊市中期":"#ffebee","熊市末期":"#fff8e1",
                  "牛市震荡":"#f0f4e8","熊市震荡":"#faf0f0"}.get(c_stage, "#f5f5f5")
    c_dim_groups = {
        "沪深300": {"label": "沪深300 宽基",   "color": "#1565C0", "score": None, "detail": []},
        "上证":    {"label": "上证指数",         "color": "#6A1B9A", "score": None, "detail": []},
        "深证":    {"label": "深证成指",         "color": "#E65100", "score": None, "detail": []},
        "创业板":  {"label": "创业板指",         "color": "#2E7D32", "score": None, "detail": []},
        "广度":    {"label": "市场广度",         "color": "#5D4037", "score": None, "detail": []},
        "成长":    {"label": "成长风格",         "color": "#C62828", "score": None, "detail": []},
        "板块":    {"label": "板块资金",         "color": "#00838F", "score": None, "detail": []},
        "新闻":    {"label": "宏观新闻情绪",     "color": "#4527A0", "score": None, "detail": []},
    }
    for lb in c_labels:
        for key, info in sorted(c_dim_groups.items(), key=lambda kv: -len(kv[0])):
            if lb.startswith(key) or lb.startswith(key.rstrip("指数")):
                info["detail"].append(lb)
                break
    _c_bull = ["涨","多头","利好","偏多",">","底背离","上行","健康","反向","低位","普涨","风险偏好高"]
    _c_bear = ["跌","空头","利空","偏空","强利空","<","顶背离","下行","转弱","转熊","恐慌","跑输","高位","弱势","普跌","避险"]
    for key, info in c_dim_groups.items():
        detail = info["detail"]
        if not detail:
            info["score"] = 50; info["detail"].append("未获取到数据"); continue
        bullish = bearish = total = 0
        for d in detail:
            total += 1
            if any(w in d for w in _c_bull): bullish += 1
            if any(w in d for w in _c_bear): bearish += 1
        info["score"] = 50 if bullish + bearish == 0 else int(bullish / (bullish + bearish) * 100)
    c_rows = ""
    for key in ["沪深300", "上证", "深证", "创业板", "广度", "成长", "板块", "新闻"]:
        info = c_dim_groups[key]; s = info["score"]
        if not info["detail"] or info["detail"] == ["未获取到数据"]:
            continue  # 无数据维度不渲染, 保持页面干净
        bar_color = "#C62828" if s <= 30 else "#E57373" if s <= 45 else "#9E9E9E" if s <= 55 else "#66BB6A" if s <= 70 else "#2E7D32"
        dir_text = "偏空" if s <= 35 else ("略空" if s <= 48 else ("中性" if s <= 52 else ("略多" if s <= 65 else "偏多")))
        detail_str = " · ".join(info["detail"][:3]) if info["detail"] else "无数据"
        c_rows += f'''
        <div class="r-row">
            <div class="r-label">{info["label"]}</div>
            <div class="r-bar-wrap">
                <div class="r-bar-track">
                    <div class="r-bar-fill" style="width:{s}%;background:{bar_color}"></div>
                    <div class="r-bar-marker" style="left:{s}%"></div>
                </div>
            </div>
            <div class="r-score" style="color:{bar_color}">{s}% {dir_text}</div>
            <div class="r-detail">{detail_str}</div>
        </div>'''
    total_eq = account_balance.get("totalEq", 0)
    c_exposure_usd = total_eq * c_exposure
    c_used_mgn = account_balance.get("usedMargin", 0)
    c_expose_txt = (f'建议总仓位: <strong>{c_exposure*100:.0f}%</strong> '
                    f'| 敞口参考(含现货/合约): ${c_exposure_usd:,.0f} '
                    f'| 说明: A股维度基于大盘趋势+板块资金+市场广度, 不代表单一标的买卖点')
    c_all_labels = " | ".join(c_labels)
    cn_regime_html = f'''
    <div class="regime-verdict" style="background:{c_stage_bg}">
        <div class="regime-verdict-left">
            <div class="regime-verdict-stage">{c_emoji} {c_stage}</div>
            <div class="regime-verdict-score" style="color:{c_score_color}">综合评分: {c_score:+.2f}</div>
        </div>
        <div class="regime-verdict-right">
            <div class="regime-verdict-exposure">建议仓位: <strong>{c_exposure*100:.0f}%</strong></div>
            <div class="regime-verdict-action">建议: <strong>{c_action}</strong></div>
        </div>
    </div>
    <div class="regime-explain">{c_expose_txt}</div>
    <div style="background:#fff;border-radius:12px;border:1px solid #e5e5e5;padding:8px 16px 12px">
        <div class="r-legend"><span class="r-leg-dot" style="background:#C62828"></span>偏空 <span class="r-leg-dot" style="background:#9E9E9E;margin-left:12px"></span>中性 <span class="r-leg-dot" style="background:#2E7D32;margin-left:12px"></span>偏多</div>
        {c_rows}
    </div>
    <div class="regime-detail-bar">{c_all_labels}</div>'''
# ===== End A股 Market Regime HTML =====

def _qry_closes(sym, interval, range_):
    """Yahoo chart 收盘价查询: 用于落地页ticker涨跌幅与大宗商品布林. 返回 (px, closes, highs, lows)."""
    import requests as _rq
    _kw = {}
    if not CLOUD_MODE and PROXY:
        _kw["proxies"] = {"http": PROXY, "https": PROXY}
    u = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval={interval}&range={range_}&includePrePost=false"
    j = _rq.get(u, timeout=20, headers={"User-Agent": "Mozilla/5.0"}, **_kw).json()
    meta = j["chart"]["result"][0]["meta"]
    q = j["chart"]["result"][0]["indicators"]["quote"][0]
    closes = [c for c in (q.get("close") or []) if c is not None]
    highs = [h for h in (q.get("high") or []) if h is not None]
    lows = [l for l in (q.get("low") or []) if l is not None]
    px = meta.get("regularMarketPrice") if meta.get("regularMarketPrice") is not None else (closes[-1] if closes else None)
    return px, closes, highs, lows

# Build JSON data for embedding
# 落地页 ticker 用标的: 附加当日涨跌幅%(用于封面循环展示)
_TICK_SYMS = {"SPY", "QQQ", "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "TSM", "AMD", "MU", "AVGO", "GLW", "COST", "PLTR"}
for _r in results:
    if _r.get("sym") in _TICK_SYMS:
        try:
            _pxc, _clc, _, _ = _qry_closes(_r["sym"], "1d", "5d")
            if _pxc and len(_clc) >= 2 and _clc[-2]:
                _r["chg"] = round((_pxc - _clc[-2]) / _clc[-2] * 100, 2)
            else:
                _r["chg"] = None
        except Exception:
            _r["chg"] = None
# 防重复保险: 同一 sym 只保留首行 (正常每股票仅 append 一次; 双保险防未来改动/合并引入重复行)
_syms = [r.get("sym") for r in results if r.get("sym")]
if len(_syms) != len(set(_syms)):
    _dups = sorted({s for s in _syms if _syms.count(s) > 1})
    print(f"[warn] results 存在重复行, 已去重: {_dups}")
    _seen2, _clean = set(), []
    for _r in results:
        _k = _r.get("sym")
        if _k:
            if _k in _seen2:
                continue
            _seen2.add(_k)
        _clean.append(_r)
    results = _clean
# allData 必须单行压缩注入: 多行格式会让 git rebase 对两份不同批次生成的 index.html 做行级合并,
# 曾把两次云端运行的个股交错拼重(95->104)。单行后任何两个版本必然整行冲突, -X theirs 整体取新, 杜绝交错。
data_json = json.dumps(results, ensure_ascii=False, separators=(",", ":"))
pos_data_json = json.dumps(all_positions, ensure_ascii=False)
acc_bal_json = json.dumps(account_balance, ensure_ascii=False)
# 做空子页: 注入市场 regime(总分/阶段/敞口), JS 据此显示"做空总开关"放行状态
short_regime_json = json.dumps({
    "score": market_regime.get("score", 99),
    "stage": market_regime.get("display_stage", market_regime.get("stage", "未知")),
    "exposure": market_regime.get("exposure", 0),
    "action": market_regime.get("action", ""),
    "score_trend": market_regime.get("score_trend", 0),
    "error": market_regime.get("error"),
}, ensure_ascii=False)
short_lev_str = "/".join(str(v) for v in SHORT_LEV_TIER.values())  # "3/5/7" 供 JS 显示做空杠杆
# 云端 runner 时区是 UTC, 需 +8 显示北京时间; 本地直接取系统时间
now_str = ((datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
           if CLOUD_MODE else datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

# 股票 Logo: 符号 → 公司域名, 用于 Clearbit Logo API(https://logo.clearbit.com/{domain})
LOGO_DOMAINS = {
    "NOK": "nokia.com", "ASML": "asml.com", "ADBE": "adobe.com", "IBM": "ibm.com",
    "HPE": "hpe.com", "DELL": "dell.com", "TSM": "tsmc.com", "META": "meta.com",
    "MSFT": "microsoft.com", "LLY": "lilly.com", "NFLX": "netflix.com", "GOOGL": "google.com",
    "AMZN": "amazon.com", "NVDA": "nvidia.com", "AAPL": "apple.com", "SNDK": "sandisk.com",
    "TSLA": "tesla.com", "AMD": "amd.com", "ORCL": "oracle.com", "INTC": "intel.com",
    "COST": "costco.com", "MU": "micron.com", "ARM": "arm.com", "AVGO": "broadcom.com",
    "PLTR": "palantir.com", "RKLB": "rocketlabusa.com", "COHR": "coherent.com",
    "HIMS": "hims.com", "HOOD": "robinhood.com", "QCOM": "qualcomm.com", "GLW": "corning.com",
    "GEV": "gevernova.com", "CSCO": "cisco.com", "WDC": "westerndigital.com",
    "MSTR": "microstrategy.com", "MRVL": "marvell.com", "LITE": "lumentum.com",
    "SPCX": "spacex.com", "SKHY": "skhynix.com", "CRWV": "coreweave.com", "NBIS": "nebius.com",
    "AMAT": "amat.com", "DIS": "disney.com", "WMT": "walmart.com", "UNH": "unitedhealthgroup.com",
    "JNJ": "jnj.com", "JPM": "jpmorganchase.com", "SONY": "sony.com", "BX": "blackstone.com",
    "NVO": "novonordisk.com", "KO": "coca-cola.com", "TXN": "ti.com", "CAT": "caterpillar.com",
    "STX": "seagate.com",
    "005930.KS": "samsung.com", "005380.KS": "hyundai.com", "066570.KS": "lg.com", "285A.T": "kioxia.com",
    "NKE": "nike.com", "MCD": "mcdonalds.com", "BB": "blackberry.com", "ZM": "zoom.us", "9984.T": "softbank.jp",
    # 中概股/港股/A股
    "00700.HK": "tencent.com", "09988.HK": "alibaba.com", "03690.HK": "meituan.com", "01810.HK": "mi.com",
    "01024.HK": "kuaishou.com", "09992.HK": "popmart.com", "02513.HK": "zhipuai.cn", "00100.HK": "minimaxi.com",
    "00625.HK": "shein.com", "00992.HK": "lenovo.com", "PDD": "pinduoduo.com",
    "300308.SZ": "innolight.com", "603986.SS": "gigadevice.com", "688836.SS": "unitree.com", "688825.SS": "cxmt.com",
    "300750.SZ": "catl.com", "002594.SZ": "byd.com", "000333.SZ": "midea.com", "601138.SS": "foxconn.com",
    "600036.SS": "cmbchina.com", "600309.SS": "whchem.com", "600031.SS": "sany.com",
    "300274.SZ": "sungrowpower.com", "603259.SS": "wuxiapptec.com",
    "000338.SZ": "weichai.com", "300124.SZ": "inovance.com", "600941.SS": "chinamobileltd.com",
    # 指数(标普500/纳指100): 有logo用logo, 否则前端回退首字母徽标
    "SPY": "ssga.com", "QQQ": "invesco.com",
}
logo_domains_json = json.dumps(LOGO_DOMAINS, ensure_ascii=False)

# ===== 自托管 logo: 下载到仓库 logos/ 目录(仅缺省时下载), 网页从本站加载, 不依赖外部 CDN =====
import os as _os
_LOGO_DIR = SCRIPT_DIR / "logos"
_LOGO_DIR.mkdir(exist_ok=True)
for _lsym, _ld in LOGO_DOMAINS.items():
    _lf = _LOGO_DIR / f"{_lsym}.png"
    if _lf.exists():
        continue
    try:
        import requests as _rq
        _kw = {}
        if not CLOUD_MODE and PROXY:
            _kw["proxies"] = {"http": PROXY, "https": PROXY}
        # Clearbit Logo API 已废弃, 改用 Google favicon(手机/云端均可加载), 转存为本地 PNG
        _r = _rq.get(f"https://www.google.com/s2/favicons?domain={_ld}&sz=128", timeout=15,
                     headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True, **_kw)
        if _r.ok and _r.content and len(_r.content) > 100:
            _lf.write_bytes(_r.content)
    except Exception:
        pass
logo_selfhost_json = json.dumps([s for s in LOGO_DOMAINS if (_LOGO_DIR / f"{s}.png").exists()], ensure_ascii=False)

# ============================= 大宗商品子页面 =============================
# 黄金/白银/布伦特原油/天然气/比特币: 统一用 Yahoo (商品 OKX/币安不覆盖; 比特币用 BTC-USD)。
# 与美股/中概股不同, 无财报/估值/做T/盈亏比等列, 仅 当前价 + 日/周/月布林。
COMMODS = [
    {"sym": "GC=F",   "name": "黄金"},
    {"sym": "SI=F",   "name": "白银"},
    {"sym": "BZ=F",   "name": "布伦特原油"},
    {"sym": "NG=F",   "name": "天然气"},
    {"sym": "BTC-USD","name": "比特币"},
]
def _boll_b(closes, px):
    import math as _m
    if not px: return None
    c = closes[-20:] if len(closes) >= 20 else closes
    if len(c) < 5: return None
    ma = sum(c) / len(c); sd = _m.sqrt(sum((x - ma) ** 2 for x in c) / len(c))
    up, lo = ma + 2 * sd, ma - 2 * sd
    return (px - lo) / (up - lo) if up > lo else 0.5
def _regime_zone(pct, vol):
    b1, b2, b3, s1, s2 = (0.27, 0.19, 0.11, 0.75, 0.82) if vol > 0.075 else (0.32, 0.24, 0.16, 0.70, 0.78)
    pe = 0.05
    if pct <= b3 + pe: return "BUY3区", "zone-buy3"
    if pct <= b2 + pe: return "BUY2区", "zone-buy2"
    if pct <= b1 + pe: return "BUY1区", "zone-buy1"
    if pct >= s2 - pe: return "SELL2区", "zone-sell2"
    if pct >= s1 - pe: return "SELL1区", "zone-sell1"
    return ("下半区", "zone-lower") if pct < 0.50 else ("上半区", "zone-upper")

commodities = []
for _co in COMMODS:
    _px, _dc, _dh, _dl = _qry_closes(_co["sym"], "1d", "6mo")
    _pxw, _wc, _, _ = _qry_closes(_co["sym"], "1wk", "2y")
    _pxm, _mc, _, _ = _qry_closes(_co["sym"], "1mo", "5y")
    _hh = _dh[-10:] if _dh else []; _ll = _dl[-10:] if _dl else []
    _al = min(_ll) if _ll else 0; _ah = max(_hh) if _hh else 0
    _cur = _px if _px is not None else 0
    _pct = (_cur - _al) / (_ah - _al) if _ah > _al else 0.5
    _vol = (_ah - _al) / ((_ah + _al) / 2) if _ah > _al else 0
    _bp1, _bp2, _bp3, _sp1, _sp2 = (0.27, 0.19, 0.11, 0.75, 0.82) if _vol > 0.075 else (0.32, 0.24, 0.16, 0.70, 0.78)
    _w = _ah - _al
    _zone, _zcls = _regime_zone(_pct, _vol)
    commodities.append({"sym": _co["sym"], "name": _co["name"], "ccy": "$",
                        "px": _cur, "alow": _al, "ahigh": _ah, "vol": _vol,
                        "pct": round(min(max(_pct, 0), 1), 3),
                        "bp1": _bp1, "bp2": _bp2, "bp3": _bp3, "sp1": _sp1, "sp2": _sp2,
                        "p_buy2": _al + _w * _bp2, "p_buy3": _al + _w * _bp3,
                        "p_sell1": _al + _w * _sp1, "p_sell2": _al + _w * _sp2,
                        "zone": _zone, "zone_class": _zcls,
                        "boll_d": _boll_b(_dc, _px), "boll_w": _boll_b(_wc, _pxw or _px), "boll_m": _boll_b(_mc, _pxm or _px)})
commodities_json = json.dumps(commodities, ensure_ascii=False)
regime_card_json = json.dumps({"action": market_regime.get("action", ""), "exposure": market_regime.get("exposure", 0)}, ensure_ascii=False)

# ============================= 黄金每日晨报子页面 =============================
# 参考"晨报"式结构, 八块: ①市场总览 ②核心摘要(黄金视角) ③消息面多空 ④黄金技术面(客观计算)
# ⑤黄金ETF资金流(SPDR官方) ⑥今日宏观数据(简化版) ⑦今日日历 ⑧今日观点(黄金视角)。
# 摘要/观点聚焦金价及驱动因素(美元/美债收益率/VIX避险/FOMC与经济数据), 个股新闻不进入这两块。
# 全部构建时生成(每次 update 重跑 dashboard.py 即自动更新): 行情 _qry_closes + news_cache.json
# + earnings_calendar.json + DeepSeek(可选)。无key/调用失败 → 全中文规则模板兜底, 绝不显示"API未配置"。
_BRIEF_IDX_SYMS = [
    {"sym": "^GSPC",    "name": "标普500"},
    {"sym": "^IXIC",    "name": "纳斯达克"},
    {"sym": "^DJI",     "name": "道琼斯"},
    {"sym": "^HSI",     "name": "恒生指数"},
    {"sym": "^VIX",     "name": "VIX恐慌"},
    {"sym": "DX-Y.NYB", "name": "美元指数"},
    {"sym": "^TNX",     "name": "美债10Y"},  # Yahoo已直接返回收益率%(如5.22=5.22%), 无需换算
    {"sym": "GC=F",     "name": "黄金"},
    {"sym": "BZ=F",     "name": "布油"},
    {"sym": "BTC-USD",  "name": "比特币"},
]
_BRIEF_CACHE_F = SCRIPT_DIR / "morning_brief_cache.json"

def _brief_today_str():
    # 与 now_str 同口径: 云端 runner 是 UTC, +8 取北京日期
    return ((datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")
            if CLOUD_MODE else datetime.now().strftime("%Y-%m-%d"))

def _brief_quote(sym):
    """单标的: (最新价, 日涨跌幅%)。Yahoo一次请求同时取现价与前收, 失败返回 (None, None)。"""
    try:
        _pxc, _clc, _, _ = _qry_closes(sym, "1d", "5d")
        if not _pxc or len(_clc) < 2 or not _clc[-2]:
            return (None, None)
        return (_pxc, round((_pxc - _clc[-2]) / _clc[-2] * 100, 2))
    except Exception:
        return (None, None)

def _brief_fmt_px(sym, px):
    if px is None:
        return "-"
    if sym == "^TNX":
        return f"{px:.2f}%"
    a = abs(px)
    if a >= 1000:
        return f"{px:,.0f}"
    if a >= 100:
        return f"{px:.1f}"
    return f"{px:.2f}"

def build_market_overview():
    print("  晨报: 拉取市场总览行情(10标的)...")
    out = []
    for _b in _BRIEF_IDX_SYMS:
        px, chg = _brief_quote(_b["sym"])
        out.append({"sym": _b["sym"], "name": _b["name"], "px": px, "chg": chg})
    return out

def _brief_cal_events():
    """今日+未来7天事件(FOMC/经济数据/自选股财报), 返回 list[dict]。"""
    items = []
    if CALENDAR_F.exists():
        try:
            with open(CALENDAR_F, "r", encoding="utf-8") as f:
                _bcal = json.load(f)
            _bnow = datetime.now()
            for e in _bcal.get("events", []):
                d = e.get("date", "")
                if not d or d.endswith("??"):
                    continue
                try:
                    du = (datetime.strptime(d, "%Y-%m-%d") - _bnow).days
                except Exception:
                    continue
                if 0 <= du <= 7:
                    _bsym = e.get("symbol", "")
                    _btag, _btc = {"FED": ("🏦 FOMC", "#6C5CE7"), "ECON": ("📊 经济数据", "#00838F")}.get(
                        _bsym, ("📅 财报", "#2c2c2a"))
                    items.append({"date": d, "days": du, "tag": _btag, "tc": _btc,
                                  "name": e.get("name", ""), "sym": _bsym, "note": e.get("note", "")})
            items.sort(key=lambda x: (x["date"], x["sym"]))
        except Exception:
            pass
    return items

def _brief_news():
    """从 news_cache.json 提取多空新闻, 按|影响|降序。返回完整 (利多list, 利空list), 由调用方按需截取。"""
    pos, neg = [], []
    try:
        with open(SCRIPT_DIR / "news_cache.json", "r", encoding="utf-8") as f:
            _bnews = json.load(f)
        _all = []
        for n in _bnews.get("macro", []):
            _all.append({"label": "宏观", "title": n.get("title_cn", n.get("title", "")),
                         "imp": n.get("impact_pct", 0), "summary": n.get("summary") or n.get("reason", ""),
                         "direction": n.get("direction", "")})
        for s in _bnews.get("stocks", []):
            for n in s.get("news", []):
                _all.append({"label": s.get("symbol", ""), "title": n.get("title_cn", n.get("title", "")),
                             "imp": n.get("impact_pct", 0), "summary": n.get("summary") or n.get("reason", ""),
                             "direction": n.get("direction", "")})
        pos = [x for x in _all if x["direction"] == "positive" and x["title"]]
        neg = [x for x in _all if x["direction"] == "negative" and x["title"]]
        pos.sort(key=lambda x: abs(x["imp"]), reverse=True)
        neg.sort(key=lambda x: abs(x["imp"]), reverse=True)
    except Exception:
        pass
    return pos, neg

def _brief_rule_summary(idx, events, pos, neg):
    """无LLM时的全中文规则模板(黄金视角): 返回 (summary_list, viewpoint)。"""
    summary = []
    _g = next((b for b in idx if b["sym"] == "GC=F"), None)
    if _g and _g["chg"] is not None:
        _gmove = "大跌" if _g["chg"] <= -1.5 else ("大涨" if _g["chg"] >= 1.5 else ("小涨" if _g["chg"] > 0 else "小跌" if _g["chg"] < 0 else "持平"))
        summary.append(f"黄金 {_brief_fmt_px('GC=F', _g['px'])} ({'+' if _g['chg'] > 0 else ''}{_g['chg']}%, {_gmove})。")
    else:
        summary.append("黄金: 行情数据暂缺, 请点击刷新或稍后查看。")
    _dxy = next((b for b in idx if b["sym"] == "DX-Y.NYB"), None)
    if _dxy and _dxy["chg"] is not None:
        summary.append(f"美元指数 {_brief_fmt_px('DX-Y.NYB', _dxy['px'])} ({'+' if _dxy['chg'] > 0 else ''}{_dxy['chg']}%), "
                       + ("美元走强对金价构成压制。" if _dxy["chg"] > 0 else "美元走弱对金价构成支撑。"))
    _tnx = next((b for b in idx if b["sym"] == "^TNX"), None)
    if _tnx and _tnx["chg"] is not None:
        summary.append(f"美债10Y收益率 {_brief_fmt_px('^TNX', _tnx['px'])} ({'+' if _tnx['chg'] > 0 else ''}{_tnx['chg']}%), "
                       + ("实际利率抬升, 持金机会成本上升。" if _tnx["chg"] > 0 else "利率回落利好无息资产黄金。"))
    _vix = next((b for b in idx if b["sym"] == "^VIX"), None)
    if _vix and _vix["chg"] is not None:
        summary.append(f"VIX {_brief_fmt_px('^VIX', _vix['px'])} ({'+' if _vix['chg'] > 0 else ''}{_vix['chg']}%), "
                       + ("避险情绪升温, 资金或流入黄金。" if _vix["chg"] > 2 else "避险需求对金价影响中性。"))
    _mp = [x for x in pos if x["label"] == "宏观"]
    _mn = [x for x in neg if x["label"] == "宏观"]
    if _mp or _mn:
        _mix = []
        if _mp:
            _mix.append(f"利多{len(_mp)}条(最强: {_mp[0]['title'][:28]})")
        if _mn:
            _mix.append(f"利空{len(_mn)}条(最强: {_mn[0]['title'][:28]})")
        summary.append("宏观消息面: " + "；".join(_mix) + "。")
    _ev = [e for e in events if e["sym"] in ("FED", "ECON")]
    if _ev:
        _names = "、".join(f"{e['name']}({e['date'][5:]})" for e in _ev[:3])
        summary.append(f"对金价关键事件: {_names}{'等' if len(_ev) > 3 else ''}, 公布前后波动或放大。")
    else:
        summary.append("今日无FOMC/重大经济数据, 金价或延续技术面震荡。")
    # 方向打分: 美元(跌→利多) / 收益率(跌→利多) / 避险(VIX大涨→利多) / 宏观消息多空
    _score = 0
    if _dxy and _dxy["chg"] is not None:
        _score += 1 if _dxy["chg"] < 0 else -1
    if _tnx and _tnx["chg"] is not None:
        _score += 1 if _tnx["chg"] < 0 else -1
    if _vix and _vix["chg"] is not None:
        _score += 1 if _vix["chg"] > 2 else 0
    if _mp or _mn:
        _score += 1 if len(_mp) > len(_mn) else (-1 if len(_mn) > len(_mp) else 0)
    _bias = "偏多" if _score >= 2 else ("偏空" if _score <= -2 else "震荡")
    viewpoint = (f"综合美元与美债收益率方向、避险情绪及宏观消息, 今日金价倾向{_bias}"
                 f"(驱动评分{_score:+d})。以上为规则模板自动生成, 仅供参考, 不构成投资建议; "
                 f"操作请结合买入区间与止损纪律。")
    return summary, viewpoint

def _brief_llm(api_key, model, base_url, digest):
    """调LLM生成晨报摘要+观点。成功返回 (summary_list, viewpoint, "llm"); 失败 (None, None, None)。"""
    import requests as req
    import time as _time
    import re as _re
    _digest_str = json.dumps(digest, ensure_ascii=False)
    prompt = f"""你是黄金市场分析师。根据以下实时数据撰写"黄金每日晨报"。

{_digest_str}

【数据说明】gold=黄金期货价格(美元/盎司); technicals=客观计算的支撑/阻力位、20日区间分位与5日涨跌; etf_flow=SPDR黄金ETF持仓吨数及1/5/20日变动(增持=看涨情绪, 减持=看跌情绪); drivers中美元指数、美债10Y收益率、VIX、原油、比特币、美股与港股指数用于判断金价驱动环境; macro_news为美联储/通胀/就业等宏观多空消息(与金价直接相关); key_events为今日及未来数日FOMC与重要经济数据。

【要求】
1. 全部内容围绕黄金及金价驱动因素(美元/实际利率/避险/央行与降息预期/地缘/ETF资金流), 禁止编造数据中没有的信息; 某类数据缺失就跳过相应要点。
2. 输出严格JSON(不要markdown代码块): {{"summary": ["要点1", "要点2", "要点3"], "viewpoint": "..."}}
3. summary: 3-5条, 每条不超过45字, 依次覆盖: 金价表现及幅度、美元与美债收益率对金价的方向性影响、技术面支撑阻力与ETF资金流变化、避险情绪与宏观消息(降息预期/通胀/地缘)对金价的利多利空、今日关键事件对金价的提示。
4. viewpoint: 80-150字, 给出今日金价倾向(看涨/看跌/震荡)+核心理由(结合美元与实际利率方向、避险需求)+一条风险提示。全部用中文。"""
    try:
        _time.sleep(2)
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        for _attempt in range(3):
            try:
                # DeepSeek 是国外域名: 本地走代理, 云端(NO_PROXY)直连
                resp = req.post(f"{base_url}/v1/chat/completions", headers=headers, json=payload, timeout=120,
                                proxies=_auto_proxy(base_url), verify=False)
                if resp.status_code == 200:
                    msg = resp.json()["choices"][0]["message"]
                    content = (msg.get("content") or msg.get("reasoning_content") or "").strip()
                    _m = _re.search(r"\{.*\}", content, _re.DOTALL)
                    if not _m:
                        raise ValueError("响应中无JSON")
                    _obj = json.loads(_m.group(0))
                    _summ, _view = _obj.get("summary"), _obj.get("viewpoint")
                    if isinstance(_summ, list) and _summ and isinstance(_view, str) and _view.strip():
                        print(f"  晨报: LLM摘要生成成功({len(_summ)}条要点)")
                        return ([str(x) for x in _summ], _view.strip(), "llm")
                    raise ValueError("JSON字段缺失")
                else:
                    print(f"  [WARN] 晨报LLM错误: {resp.status_code} {resp.text[:150]}")
                    return (None, None, None)
            except Exception as e:
                if _attempt < 2:
                    print(f"  [WARN] 晨报LLM超时({e}), retry {_attempt+1}/3...")
                    _time.sleep(5)
                else:
                    print(f"  [WARN] 晨报LLM失败: {e}")
        return (None, None, None)
    except Exception as e:
        print(f"  [WARN] 晨报LLM异常: {e}")
        return (None, None, None)

def _brief_news_col(items, pos_side):
    """消息面单列HTML。pos_side=True 为利多列。输出 gb-news 行(不带外层容器)。"""
    if not items:
        return '<div class="gb-empty">今日暂无相关消息</div>'
    _rows = ""
    for x in items:
        _it = f"+{x['imp']}%" if x["imp"] > 0 else f"{x['imp']}%"
        _vc = "v" if x["imp"] > 0 else "v neg"
        _summ = _html.escape(str(x["summary"] or "")[:44])
        _t = _html.escape(x["title"])
        _body = f'<span class="t">{_t}' + (f'<small>{_summ}</small>' if _summ else "") + "</span>"
        _rows += f'<div class="n">{_body}<span class="{_vc}">{_it}</span></div>'
    return _rows

def _brief_boll_bands(closes, n=20):
    c = closes[-n:]
    if len(c) < n:
        return None
    ma = sum(c) / n
    sd = (sum((x - ma) ** 2 for x in c) / n) ** 0.5
    return (ma, ma + 2 * sd, ma - 2 * sd)

def _brief_technical():
    """黄金技术面(客观计算): 支撑/阻力候选位+20日分位+5日趋势。失败返回 None。"""
    try:
        px, dc, dh, dl = _qry_closes("GC=F", "1d", "3mo")
        if not px or len(dc) < 20 or len(dh) < 20 or len(dl) < 20:
            return None
        _b = _brief_boll_bands(dc, 20)
        hi20, lo20 = max(dh[-20:]), min(dl[-20:])
        hi10, lo10 = max(dh[-10:]), min(dl[-10:])
        pct = (px - lo20) / (hi20 - lo20) if hi20 > lo20 else 0.5
        chg5 = round((px - dc[-6]) / dc[-6] * 100, 2) if len(dc) >= 6 else None
        # 阻力: 布林上轨/10日高/20日高中高于现价、最近优先
        _res = []
        if _b:
            _res.append(("日线布林上轨", _b[1]))
        _res += [("10日高点", hi10), ("20日高点", hi20)]
        _res = sorted([x for x in _res if x[1] and x[1] > px], key=lambda x: x[1])[:3] or [("20日高点", hi20)]
        _sup = []
        if _b:
            _sup.append(("日线布林下轨", _b[2]))
        _sup += [("10日低点", lo10), ("20日低点", lo20)]
        _sup = sorted([x for x in _sup if x[1] and x[1] < px], key=lambda x: -x[1])[:3] or [("20日低点", lo20)]
        return {"px": px, "sup": _sup, "res": _res, "pct": pct, "chg5": chg5}
    except Exception:
        return None

def _brief_spdr_tonnes():
    """SPDR官方GLD持仓吨数(historical-archive XLSX → zipfile+xml解析, 免新依赖)。失败返回 None。"""
    try:
        import requests as _rq2
        import io as _io
        import zipfile as _zf
        import xml.etree.ElementTree as _ET
        import urllib3 as _u3
        _u3.disable_warnings(_u3.exceptions.InsecureRequestWarning)
        u = "https://api.spdrgoldshares.com/api/v1/historical-archive?product=gld&exchange=NYSE&lang=en"
        r = _rq2.get(u, timeout=40, headers={"User-Agent": "Mozilla/5.0"},
                     proxies=_auto_proxy(u), verify=False)
        if r.status_code != 200 or not r.content or r.content[:2] != b"PK":
            return None
        M = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
        NS = {'m': M[1:-1]}
        z = _zf.ZipFile(_io.BytesIO(r.content))
        ss = []
        try:
            _root = _ET.fromstring(z.read('xl/sharedStrings.xml'))
            for _si in _root.findall('m:si', NS):
                ss.append(''.join(t.text or '' for t in _si.iter(M + 't')))
        except KeyError:
            pass

        def _cell_str(c):
            v = c.find('m:v', NS)
            val = v.text if v is not None else ''
            if c.get('t') == 's' and val != '':
                val = ss[int(val)]
            return str(val or '')

        def _col_of(c):
            return ''.join(ch for ch in (c.get('r') or '') if ch.isalpha())

        # 找数据表: 首行含 Date 与 Tonnes 列
        data_rows, col_date, col_ton = None, None, None
        for _name in [n for n in z.namelist() if n.startswith('xl/worksheets/sheet')]:
            _root = _ET.fromstring(z.read(_name))
            _rows = _root.findall('.//m:row', NS)
            if not _rows:
                continue
            hdr = {_col_of(c): _cell_str(c) for c in _rows[0].findall('m:c', NS)}
            _cd = next((k for k, v in hdr.items() if v.strip() == 'Date'), None)
            _ct = next((k for k, v in hdr.items() if 'Tonnes' in v), None)
            if _cd and _ct:
                data_rows, col_date, col_ton = _rows, _cd, _ct
                break
        if not data_rows or len(data_rows) < 22:
            return None
        recs = []
        for row in data_rows[1:]:
            vals = {_col_of(c): _cell_str(c) for c in row.findall('m:c', NS)}
            if vals.get(col_date) and vals.get(col_ton):
                try:
                    recs.append((vals[col_date].strip(), float(vals[col_ton])))
                except ValueError:
                    continue  # "US Holiday"等非数值备注行
        if len(recs) < 21:
            return None
        _mons = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
                 "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}

        def _pdate(s):
            try:
                _d, _m, _y = s.split('-')
                return datetime(int(_y), _mons[_m], int(_d))
            except Exception:
                return None

        recs2 = sorted([(_pdate(a), t) for a, t in recs if _pdate(a)], key=lambda x: x[0])
        last_d, last_t = recs2[-1]
        return {"date": last_d.strftime("%m-%d"), "tonnes": last_t,
                "d1": last_t - recs2[-2][1] if len(recs2) >= 2 else None,
                "d5": last_t - recs2[-6][1] if len(recs2) >= 6 else None,
                "d20": last_t - recs2[-21][1] if len(recs2) >= 21 else None}
    except Exception:
        return None

# 宏观数据事件关键词 → 用于从宏观新闻标题匹配实际值
_BRIEF_ECON_TOKENS = [
    ("CPI", ("CPI", "通胀")), ("PPI", ("PPI",)), ("非农", ("非农", "就业")),
    ("失业率", ("失业率", "初请")), ("FOMC", ("FOMC", "美联储", "利率", "降息", "加息")),
    ("PCE", ("PCE",)), ("PMI", ("PMI",)), ("零售", ("零售",)), ("GDP", ("GDP",)),
]

def _brief_macro_rows(macro_news):
    """宏观数据简化版: 近7日已公布+今日+未来3日的FOMC/经济数据事件, 关键词匹配宏观新闻实际值。"""
    out = []
    if CALENDAR_F.exists():
        try:
            with open(CALENDAR_F, "r", encoding="utf-8") as f:
                _mc = json.load(f)
            _mnow = datetime.now()
            for e in _mc.get("events", []):
                if e.get("symbol") not in ("FED", "ECON"):
                    continue
                d = e.get("date", "")
                if not d or d.endswith("??"):
                    continue
                try:
                    du = (datetime.strptime(d, "%Y-%m-%d") - _mnow).days
                except Exception:
                    continue
                if not (-7 <= du <= 3):
                    continue
                _name = e.get("name", "")
                _matched = ""
                for _tok, _words in _BRIEF_ECON_TOKENS:
                    if _tok in _name or any(w in _name for w in _words):
                        for x in macro_news:
                            if x["title"] and any(w in x["title"] for w in (_tok,) + _words):
                                _matched = x["title"][:46]
                                break
                        break
                out.append({"date": d, "days": du, "name": _name, "match": _matched})
            out.sort(key=lambda x: x["date"])
        except Exception:
            pass
    return out

def _brief_technical_html(t):
    """黄金技术面: 客观计算的支撑/阻力+分位+5日趋势(gb紧凑样式)。"""
    if not t:
        return '<div class="gb-empty">技术面数据暂不可用</div>'
    _fmt = lambda v: f"{v:,.1f}"
    _res_rows = "".join(f'<div class="gb-lv"><i>{_html.escape(lbl)}</i><b style="color:#f85149">{_fmt(v)}</b></div>'
                        for lbl, v in t["res"])
    _sup_rows = "".join(f'<div class="gb-lv"><i>{_html.escape(lbl)}</i><b style="color:#3fb950">{_fmt(v)}</b></div>'
                        for lbl, v in t["sup"])
    _chg5 = "-" if t["chg5"] is None else f"{'+' if t['chg5'] > 0 else ''}{t['chg5']}%"
    _chg5c = "#8b949e" if t["chg5"] is None else ("#3fb950" if t["chg5"] > 0 else "#f85149")
    return (f'<div class="gb-statline">现价 <b>{_fmt(t["px"])}</b>'
            f' ｜ 20日区间分位 <b>{t["pct"]*100:.0f}%</b>'
            f' ｜ 近5日 <b style="color:{_chg5c}">{_chg5}</b></div>'
            f'<div class="gb-levels">'
            f'<div><div class="gb-subh neg">上方阻力</div>{_res_rows}</div>'
            f'<div><div class="gb-subh pos">下方支撑</div>{_sup_rows}</div>'
            f'</div>'
            f'<div class="gb-note">支撑/阻力为客观计算位(10/20日高低点+日线布林轨道), 非主观策略价位</div>')

def _brief_spdr_html(s):
    """SPDR黄金ETF持仓块(gb紧凑样式)。"""
    if not s:
        return '<div class="gb-empty">SPDR持仓数据暂不可用</div>'
    def _c(v):
        return "—" if v is None else f"{v:+.2f}吨"
    def _cc(v):
        return "" if v is None else ("pos" if v > 0 else "neg" if v < 0 else "")
    return (f'<div class="gb-stats4">'
            f'<div class="gb-stat"><i>最新持仓({s["date"]}更新)</i><b>{s["tonnes"]:,.2f}<small>吨</small></b></div>'
            f'<div class="gb-stat"><i>较上日</i><b class="{_cc(s["d1"])}">{_c(s["d1"])}</b></div>'
            f'<div class="gb-stat"><i>近5日变动</i><b class="{_cc(s["d5"])}">{_c(s["d5"])}</b></div>'
            f'<div class="gb-stat"><i>近20日变动</i><b class="{_cc(s["d20"])}">{_c(s["d20"])}</b></div>'
            f'</div>'
            f'<div class="gb-note">数据源: SPDR Gold Shares 官方档案(T+1)。持仓增加通常反映看涨情绪, 减持为看跌信号</div>')

def _brief_macro_html(rows):
    """宏观数据简化版块(gb紧凑样式)。"""
    if not rows:
        return '<div class="gb-empty">近7日无FOMC/重要经济数据事件</div>'
    _rows = ""
    for r in rows:
        if r["days"] < 0:
            _st, _sc = "已公布", "#6e7681"
        elif r["days"] == 0:
            _st, _sc = "今日公布", "#f85149"
        else:
            _st, _sc = f"待公布({r['days']}天后)", "#58a6ff"
        _mv = _html.escape(r["match"]) if r["match"] else '<span class="muted">—</span>'
        _rows += (f'<tr><td class="d">{r["date"][5:]}</td>'
                  f'<td><strong>{_html.escape(r["name"])}</strong></td>'
                  f'<td><span class="pill" style="background:{_sc}">{_st}</span></td>'
                  f'<td class="muted">{_mv}</td></tr>')
    return (f'<table class="gb-table"><thead><tr><th>日期</th><th>宏观事件</th><th>状态</th><th>实际值(来自宏观新闻)</th></tr></thead>'
            f'<tbody>{_rows}</tbody></table>'
            f'<div class="gb-note">注: 预期值(市场共识)无免费数据源, 此处为简化版; 实际数字以官方公布为准</div>')

# ---- 晨报构建主流程 ----
print("Building morning brief...")
_brief_idx = build_market_overview()
brief_idx_json = json.dumps(_brief_idx, ensure_ascii=False)
brief_events = _brief_cal_events()
brief_pos, brief_neg = _brief_news()
_brief_tech = _brief_technical()
print(f"  晨报: 技术面计算{'完成' if _brief_tech else '不可用'}")
_brief_spdr = _brief_spdr_tonnes()
print(f"  晨报: SPDR持仓{'获取成功' if _brief_spdr else '不可用'}")
_brief_macro_all = sorted([x for x in (brief_pos + brief_neg) if x["label"] == "宏观"],
                          key=lambda x: abs(x["imp"]), reverse=True)[:8]
brief_macro = _brief_macro_rows(_brief_macro_all)

# 摘要+观点: 当日缓存优先(LLM结果当日稳定复用; 旧缓存为规则结果且本次有key则重试LLM覆盖)
_brief_llmcfg = {}
_brief_cfgf = SCRIPT_DIR / "news_config.json"
if _brief_cfgf.exists():
    try:
        with open(_brief_cfgf, "r", encoding="utf-8") as f:
            _brief_llmcfg = json.load(f)
    except Exception:
        pass
_brief_key = os.getenv("DEEPSEEK_API_KEY", _brief_llmcfg.get("api_key", ""))
_brief_model = os.getenv("NEWS_LLM_MODEL", _brief_llmcfg.get("model", "deepseek-v4-flash"))
_brief_baseurl = os.getenv("NEWS_LLM_BASE_URL", _brief_llmcfg.get("base_url", "https://api.deepseek.com"))

_brief_today = _brief_today_str()
brief_summary, brief_view, brief_src = None, None, None
if _BRIEF_CACHE_F.exists():
    try:
        with open(_BRIEF_CACHE_F, "r", encoding="utf-8") as f:
            _bc = json.load(f)
        if _bc.get("date") == _brief_today and _bc.get("v") == 3 and _bc.get("summary") and _bc.get("viewpoint"):
            if _bc.get("src") == "llm" or not _brief_key:
                brief_summary, brief_view, brief_src = _bc["summary"], _bc["viewpoint"], _bc.get("src", "rule")
                print(f"  晨报: 复用当日缓存(src={brief_src})")
    except Exception:
        pass
if brief_summary is None:
    _gold_b = next((b for b in _brief_idx if b["sym"] == "GC=F"), None)
    _brief_digest = {
        "build_time_beijing": now_str,
        "gold": {"price_usd": _gold_b["px"], "chg_pct": _gold_b["chg"]} if _gold_b else None,
        "technicals": ({"support_levels": [round(v, 1) for _, v in _brief_tech["sup"]],
                        "resistance_levels": [round(v, 1) for _, v in _brief_tech["res"]],
                        "range_20d_position_pct": round(_brief_tech["pct"] * 100, 1),
                        "chg_5d_pct": _brief_tech["chg5"]} if _brief_tech else None),
        "etf_flow": ({"spdr_tonnes": round(_brief_spdr["tonnes"], 2), "as_of": _brief_spdr["date"],
                      "chg_1d_tonnes": round(_brief_spdr["d1"], 2) if _brief_spdr["d1"] is not None else None,
                      "chg_5d_tonnes": round(_brief_spdr["d5"], 2) if _brief_spdr["d5"] is not None else None,
                      "chg_20d_tonnes": round(_brief_spdr["d20"], 2) if _brief_spdr["d20"] is not None else None}
                     if _brief_spdr else None),
        "drivers": [{"name": b["name"], "price": b["px"], "chg_pct": b["chg"]}
                    for b in _brief_idx if b["sym"] != "GC=F"],
        "macro_news": [{"title": x["title"], "direction": x["direction"], "impact_pct": x["imp"]}
                       for x in _brief_macro_all],
        "key_events": [{"date": e["date"], "tag": e["tag"], "name": e["name"]}
                       for e in brief_events if e["sym"] in ("FED", "ECON")][:6],
    }
    if _brief_key:
        brief_summary, brief_view, brief_src = _brief_llm(_brief_key, _brief_model, _brief_baseurl, _brief_digest)
    else:
        print("  晨报: 未配置API key, 使用规则模板")
    if brief_summary is None:
        brief_summary, brief_view = _brief_rule_summary(_brief_idx, brief_events, brief_pos, brief_neg)
        brief_src = "rule"
    try:
        with open(_BRIEF_CACHE_F, "w", encoding="utf-8") as f:
            json.dump({"v": 3, "date": _brief_today, "summary": brief_summary, "viewpoint": brief_view,
                       "src": brief_src, "generated_at": now_str}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# ---- 晨报HTML拼装 (复用现有CSS类; 文案避开"币安"等 transform 替换敏感词) ----
# 排版v2: 专属 gb-* 样式(body级<style>, transform只替换head第一个<style>故不受影响), grid自适应, 无水平溢出
BRIEF_CSS = '''
/* 每日晨报专属样式(黄金/原油共用): 仅作用于 #tab-commodities 内的 .gb-* 元素 */
#tab-commodities .gb-head { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px;
  background:#161b22; border:1px solid #30363d; border-radius:12px; padding:12px 18px; margin-bottom:14px; }
#tab-commodities .gb-title { font-size:16px; font-weight:700; color:#e6b85c; letter-spacing:1px; }
#tab-commodities .gb-sub { font-size:11px; color:#8b949e; margin-top:3px; }
#tab-commodities .gb-card { background:#161b22; border:1px solid #30363d; border-radius:12px; padding:14px 18px; }
#tab-commodities .gb-card, #tab-commodities .gb-row { margin-bottom:14px; }
#tab-commodities .gb-card:last-child { margin-bottom:0; }
#tab-commodities .gb-h { font-size:13.5px; font-weight:600; color:#c9d1d9; margin-bottom:10px; display:flex; align-items:center; gap:8px; }
#tab-commodities .gb-h::before { content:''; width:3px; height:13px; background:#e6b85c; border-radius:2px; }
#tab-commodities .gb-tag { font-size:10px; padding:2px 7px; border-radius:4px; background:linear-gradient(135deg,#8b5cf6,#6c5ce7); color:#fff; font-weight:500; }
#tab-commodities .gb-tag.grey { background:#30363d; color:#8b949e; }
#tab-commodities .gb-tag.gold { background:#9e7c3c; }
#tab-commodities .gb-tag.teal { background:#0e7a80; }
#tab-commodities .gb-row { display:grid; grid-template-columns:1fr 1fr; gap:14px; align-items:stretch; }
#tab-commodities .gb-row .gb-card { margin-bottom:0; }
#tab-commodities .gb-idx { display:grid; grid-template-columns:repeat(auto-fill, minmax(145px,1fr)); gap:10px; }
#tab-commodities .gb-idx .card { background:#0d1117; border:1px solid #21262d; border-radius:10px; padding:10px 12px; margin:0; }
#tab-commodities .gb-idx .card .label { font-size:11px; color:#8b949e; margin-bottom:3px; }
#tab-commodities .gb-idx .card .value { font-size:19px; font-weight:600; color:#e6edf3; }
#tab-commodities .gb-idx .card .chg { font-size:12px; font-weight:600; margin-top:2px; }
#tab-commodities .gb-ol { margin:0; padding-left:18px; font-size:13px; line-height:1.75; color:#c9d1d9; }
#tab-commodities .gb-ol li { margin:2px 0; }
#tab-commodities .gb-subh { font-size:12.5px; font-weight:600; margin:8px 0 4px; }
#tab-commodities .gb-subh:first-child { margin-top:0; }
#tab-commodities .gb-subh.pos { color:#3fb950; }
#tab-commodities .gb-subh.neg { color:#f85149; }
#tab-commodities .gb-subh em { font-style:normal; font-size:10px; background:#21262d; border-radius:8px; padding:1px 7px; margin-left:6px; color:#8b949e; }
#tab-commodities .gb-news { font-size:12.5px; }
#tab-commodities .gb-news .n { display:flex; justify-content:space-between; gap:10px; padding:5px 0; border-bottom:1px dashed #21262d; }
#tab-commodities .gb-news .n:last-child { border-bottom:none; }
#tab-commodities .gb-news .n .t { color:#c9d1d9; line-height:1.45; }
#tab-commodities .gb-news .n .t small { display:block; color:#8b949e; font-size:11px; margin-top:1px; }
#tab-commodities .gb-news .n .v { color:#3fb950; font-weight:600; white-space:nowrap; }
#tab-commodities .gb-news .n .v.neg { color:#f85149; }
#tab-commodities .gb-statline { font-size:12.5px; color:#8b949e; margin-bottom:10px; }
#tab-commodities .gb-statline b { color:#e6edf3; font-size:15px; }
#tab-commodities .gb-levels { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
#tab-commodities .gb-lv { display:flex; justify-content:space-between; gap:8px; font-size:12.5px; padding:5px 0; border-bottom:1px dashed #21262d; }
#tab-commodities .gb-lv:last-child { border-bottom:none; }
#tab-commodities .gb-lv i { font-style:normal; color:#8b949e; }
#tab-commodities .gb-lv b { font-weight:600; white-space:nowrap; }
#tab-commodities .gb-stats4 { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }
#tab-commodities .gb-stat { background:#0d1117; border:1px solid #21262d; border-radius:10px; padding:10px 12px; }
#tab-commodities .gb-stat i { display:block; font-style:normal; font-size:11px; color:#8b949e; margin-bottom:3px; }
#tab-commodities .gb-stat b { font-size:18px; font-weight:600; color:#e6edf3; }
#tab-commodities .gb-stat b.pos { color:#3fb950; }
#tab-commodities .gb-stat b.neg { color:#f85149; }
#tab-commodities .gb-stat b small { font-size:11px; color:#8b949e; font-weight:400; }
#tab-commodities .gb-table { width:100%; border-collapse:collapse; font-size:12.5px; }
#tab-commodities .gb-table th { text-align:left; padding:7px 10px; background:#0d1117; color:#8b949e; font-weight:600; border-bottom:1px solid #30363d; }
#tab-commodities .gb-table td { padding:7px 10px; border-bottom:1px solid #21262d; color:#c9d1d9; }
#tab-commodities .gb-table td strong { color:#e6edf3; }
#tab-commodities .gb-table .d { white-space:nowrap; }
#tab-commodities .gb-table .sym { color:#8b949e; font-size:11px; }
#tab-commodities .gb-table .muted { color:#6e7681; }
#tab-commodities .gb-table .pill { font-size:10.5px; padding:2px 8px; border-radius:9px; color:#fff; white-space:nowrap; }
#tab-commodities .gb-note { font-size:11px; color:#6e7681; margin-top:10px; }
#tab-commodities .gb-empty { text-align:center; padding:22px 10px; color:#6e7681; font-size:13px; }
#tab-commodities .gb-view { margin:0; font-size:13px; line-height:1.8; color:#c9d1d9; }
@media (max-width: 900px) {
  #tab-commodities .gb-row, #tab-commodities .gb-levels { grid-template-columns:1fr; }
  #tab-commodities .gb-stats4 { grid-template-columns:repeat(2,1fr); }
}
'''
# 原油晨报样式: 复用黄金晨报同款 CSS(已作用到 #tab-commodities)
OIL_CSS = BRIEF_CSS
_brief_pos_m = [x for x in brief_pos if x["label"] == "宏观"][:6]   # 只保留宏观(黄金相关), 个股消息不进晨报
_brief_neg_m = [x for x in brief_neg if x["label"] == "宏观"][:6]
_brief_src_tag = '<span class="gb-tag">DeepSeek</span>' if brief_src == "llm" else '<span class="gb-tag grey">规则生成</span>'
_brief_sum_items = "".join(f"<li>{_html.escape(s)}</li>" for s in brief_summary)
brief_html = f'''
<style>{BRIEF_CSS}</style>
<div class="gb-page">
<div class="gb-head">
  <div><div class="gb-title">黄金每日晨报</div>
  <div class="gb-sub">生成: {now_str} ｜ 行情: Yahoo · 消息: Google News · 摘要: {"DeepSeek AI" if brief_src == "llm" else "规则模板"}</div></div>
</div>
<div class="gb-card"><div class="gb-h">01 今日市场总览</div><div class="gb-idx" id="brief-idx-grid"></div></div>
<div class="gb-row">
  <div class="gb-card"><div class="gb-h">02 核心摘要 {_brief_src_tag}</div><ol class="gb-ol">{_brief_sum_items}</ol></div>
  <div class="gb-card"><div class="gb-h">03 黄金消息面</div>
    <div class="gb-subh pos">利多因素 <em>{len(_brief_pos_m)}</em></div>
    <div class="gb-news">{_brief_news_col(_brief_pos_m, True)}</div>
    <div class="gb-subh neg">利空因素 <em>{len(_brief_neg_m)}</em></div>
    <div class="gb-news">{_brief_news_col(_brief_neg_m, False)}</div>
  </div>
</div>
<div class="gb-row">
  <div class="gb-card"><div class="gb-h">04 黄金技术面 <span class="gb-tag grey">客观计算</span></div>{_brief_technical_html(_brief_tech)}</div>
  <div class="gb-card"><div class="gb-h">05 黄金ETF资金流 <span class="gb-tag gold">SPDR官方</span></div>{_brief_spdr_html(_brief_spdr)}</div>
</div>
<div class="gb-card"><div class="gb-h">06 今日宏观数据 <span class="gb-tag teal">简化版</span></div>{_brief_macro_html(brief_macro)}</div>
<div class="gb-card"><div class="gb-h">07 今日观点 {_brief_src_tag}</div><p class="gb-view">{_html.escape(brief_view)}</p></div>
</div>'''

# =====================================================================
# 原油每日晨报 (每日生成, 仿黄金晨报; 无SPDR持仓, 用供需新闻+美元替代)
# 结构: ①市场总览(布油/美油/美元/美债) ②核心摘要 ③供需消息面 ④技术面 ⑤宏观数据 ⑥今日观点
# =====================================================================
_OIL_CACHE_F = SCRIPT_DIR / "oil_brief_cache.json"
_OIL_IDX_SYMS = [
    {"sym": "BZ=F",     "name": "布伦特原油"},
    {"sym": "CL=F",     "name": "WTI美油"},
    {"sym": "DX-Y.NYB", "name": "美元指数"},
    {"sym": "^TNX",     "name": "美债10Y"},
    {"sym": "GC=F",     "name": "黄金"},
    {"sym": "^VIX",     "name": "VIX恐慌"},
]

def _oil_overview():
    print("  原油晨报: 拉取行情...")
    out = []
    for _b in _OIL_IDX_SYMS:
        out.append({"sym": _b["sym"], "name": _b["name"], **{"px": None, "chg": None}})
        out[-1]["px"], out[-1]["chg"] = _brief_quote(_b["sym"])
    return out

def _oil_technical():
    """原油技术面(客观计算, 以布油BZ=F为锚)。"""
    try:
        px, dc, dh, dl = _qry_closes("BZ=F", "1d", "3mo")
        if not px or len(dc) < 20:
            return None
        _b = _brief_boll_bands(dc, 20)
        hi20, lo20 = max(dh[-20:]), min(dl[-20:])
        hi10, lo10 = max(dh[-10:]), min(dl[-10:])
        pct = (px - lo20) / (hi20 - lo20) if hi20 > lo20 else 0.5
        chg5 = round((px - dc[-6]) / dc[-6] * 100, 2) if len(dc) >= 6 else None
        _res, _sup = [], []
        if _b:
            _res.append(("日线布林上轨", _b[1])); _sup.append(("日线布林下轨", _b[2]))
        _res += [("10日高点", hi10), ("20日高点", hi20)]
        _sup += [("10日低点", lo10), ("20日低点", lo20)]
        _res = sorted([x for x in _res if x[1] and x[1] > px], key=lambda x: x[1])[:3] or [("20日高点", hi20)]
        _sup = sorted([x for x in _sup if x[1] and x[1] < px], key=lambda x: -x[1])[:3] or [("20日低点", lo20)]
        return {"px": px, "sup": _sup, "res": _res, "pct": pct, "chg5": chg5}
    except Exception:
        return None

_OIL_NEWS_CACHE_F = SCRIPT_DIR / "oil_news_cache.json"

def _oil_news():
    """油供需/地缘新闻: 优先当日缓存; 否则 Google News RSS 专搜油相关(含伊朗/地缘/OPEC/EIA),
    规则判多空(供应中断/减产/地缘紧张→利多油价; 库存增/需求疲软/增产→利空);
    RSS 失败回退到宏观 news_cache 里油相关条目。返回 (利多list, 利空list)。
    条目: {title, direction: 'positive'/'negative', imp: ±N, label:'宏观', summary}"""
    import datetime as _dt
    _today = _brief_today_str()
    # 1) 当日缓存
    if _OIL_NEWS_CACHE_F.exists():
        try:
            with open(_OIL_NEWS_CACHE_F, "r", encoding="utf-8") as f:
                _oc = json.load(f)
            if _oc.get("date") == _today and ("pos" in _oc):
                return _oc["pos"], _oc["neg"]
        except Exception:
            pass
    pos, neg = [], []

    # 2) Google News RSS 专搜(两组查询: 中文供需+地缘 / 英文供应)
    def _rss(query, max_items=8):
        items = []
        try:
            from xml.etree import ElementTree as _ET
            import requests as _rq
            _kw = {}
            if not CLOUD_MODE and PROXY:
                _kw["proxies"] = {"http": PROXY, "https": PROXY}
            u = f"https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
            r = _rq.get(u, timeout=15, headers={"User-Agent": "Mozilla/5.0"}, verify=False, **_kw)
            if r.status_code == 200 and r.text:
                root = _ET.fromstring(r.text)
                cutoff = datetime.now(timezone.utc) - timedelta(days=2)
                from email.utils import parsedate_to_datetime as _pd
                for item in root.findall('.//item')[:max_items * 3]:
                    _t = item.find('title')
                    _d = item.find('pubDate')
                    _s = item.find('source')
                    if _t is None or not _t.text:
                        continue
                    _date = ""
                    try:
                        if _d is not None and _d.text:
                            _dtv = _pd(_d.text)
                            if _dtv.tzinfo is None:
                                _dtv = _dtv.replace(tzinfo=timezone.utc)
                            if _dtv < cutoff:
                                continue
                            _date = _dtv.strftime("%Y-%m-%d")
                    except Exception:
                        _date = (_d.text[:10] if _d is not None and _d.text else "")
                    items.append({"title": _t.text, "date": _date,
                                  "provider": _s.text if _s is not None else "Google News"})
                    if len(items) >= max_items:
                        break
        except Exception:
            pass
        return items

    _queries = ["原油 OR OPEC OR 石油", "伊朗 石油 OR 原油", "oil supply OR OPEC OR crude"]
    _seen = set()
    _items = []
    for _q in _queries:
        for _x in _rss(_q):
            if _x["title"] not in _seen:
                _seen.add(_x["title"])
                _items.append(_x)

    # 3) 规则判多空(油价视角)
    _bull = ("减产", "中断", "袭击", "制裁", "紧张", "升级", "冲突", "打击", "封锁", "地缘",
             "削减", "收紧", "短缺", "上涨", "飙升", "供应受限", "tension", "attack", "sanction", "cut")
    _bear = ("增产", "库存增加", "累库", "需求疲软", "需求放缓", "衰退", "下跌", "回落", "暴跌",
             "超供", "供应过剩", "豁免", "和解", "停火", "oversupply", "glut", "recession")
    for _x in _items:
        _t = _x["title"]
        is_bull = any(k in _t for k in _bull)
        is_bear = any(k in _t for k in _bear)
        if is_bull == is_bear:
            continue  # 双向命中或均未命中 → 跳过
        _dst = pos if is_bull else neg
        _dst.append({"label": "宏观", "title": _t, "summary": _x.get("provider", ""),
                     "direction": "positive" if is_bull else "negative",
                     "imp": 2 if is_bull else -2, "date": _x.get("date", "")})
    pos.sort(key=lambda x: x.get("imp", 0), reverse=True)
    neg.sort(key=lambda x: abs(x.get("imp", 0)), reverse=True)

    # 4) RSS 无结果 → 回退宏观 news_cache 油相关
    if not pos and not neg:
        _kw = ("原油", "OPEC", "欧佩克", "EIA", "石油", "油价", "伊朗", "中东", "减产", "增产")
        for it in ("positive", "negative"):
            _src = brief_pos if it == "positive" else brief_neg
            _dst = pos if it == "positive" else neg
            for x in _src:
                _t = str(x.get("title", ""))
                if x.get("label") == "宏观" and any(k in _t for k in _kw):
                    _dst.append(x)
    try:
        with open(_OIL_NEWS_CACHE_F, "w", encoding="utf-8") as f:
            json.dump({"date": _today, "pos": pos[:6], "neg": neg[:6]}, f, ensure_ascii=False)
    except Exception:
        pass
    return pos[:6], neg[:6]

def _oil_llm(api_key, model, base_url, digest):
    """调LLM生成原油晨报摘要+观点。复用 _brief_llm 的 HTTP 封装。"""
    import requests as req
    import time as _time
    import re as _re
    _digest_str = json.dumps(digest, ensure_ascii=False)
    prompt = f"""你是国际原油市场分析师。根据以下实时数据撰写"原油每日晨报"。

{_digest_str}

【数据说明】oil=原油期货价格(布油BZ/美油CL, 美元/桶); technicals=客观计算的支撑/阻力位、20日区间分位与5日涨跌; dxy=美元指数(美元强→油价承压); tnx=美债10Y收益率; macro_news=宏观多空(与油价直接相关); oil_news=OPEC/EIA/库存/地缘/产油国相关新闻(供需核心驱动)。

【要求】
1. 全部围绕油价及驱动因素(OPEC+供应端政策/全球库存/EIA数据/地缘政治/产油国动态/美元与全球需求预期), 禁止编造数据中没有的信息; 缺数据就跳过相应要点。
2. 输出严格JSON(不要markdown代码块): {{"summary": ["要点1", "要点2", "要点3"], "viewpoint": "..."}}
3. summary: 3-5条, 每条≤45字, 依次覆盖: 油价表现及幅度、供需面核心驱动(OPEC/库存/地缘)、美元与宏观对油价的方向性影响、技术面支撑阻力、今日关键事件提示。
4. viewpoint: 80-150字, 给出今日油价倾向(看涨/看跌/震荡)+核心驱动理由+一条风险提示。全部用中文。"""
    try:
        _time.sleep(2)
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        for _attempt in range(3):
            try:
                resp = req.post(f"{base_url}/v1/chat/completions", headers=headers, json=payload, timeout=120,
                                proxies=_auto_proxy(base_url), verify=False)
                if resp.status_code == 200:
                    msg = resp.json()["choices"][0]["message"]
                    content = (msg.get("content") or msg.get("reasoning_content") or "").strip()
                    _m = _re.search(r"\{.*\}", content, _re.DOTALL)
                    if not _m:
                        raise ValueError("响应中无JSON")
                    _obj = json.loads(_m.group(0))
                    _summ, _view = _obj.get("summary"), _obj.get("viewpoint")
                    if isinstance(_summ, list) and _summ and _view:
                        return (_summ, _view, "llm")
                    raise ValueError("返回结构异常")
            except Exception as e:
                if _attempt < 2:
                    print(f"  [WARN] 原油晨报LLM超时({e}), retry {_attempt+1}/3...")
                    _time.sleep(5)
                else:
                    print(f"  [WARN] 原油晨报LLM失败: {e}")
        return (None, None, None)
    except Exception as e:
        print(f"  [WARN] 原油晨报LLM异常: {e}")
        return (None, None, None)

def _oil_rule_summary(idx, pos, neg):
    """无LLM时原油规则模板: 返回 (summary_list, viewpoint)。"""
    summary = []
    _bzt = next((b for b in idx if b["sym"] == "BZ=F"), None)
    if _bzt and _bzt["chg"] is not None:
        _mv = "大涨" if _bzt["chg"] >= 1.5 else ("大跌" if _bzt["chg"] <= -1.5 else ("小涨" if _bzt["chg"] > 0 else "小跌" if _bzt["chg"] < 0 else "持平"))
        summary.append(f"布油 {_brief_fmt_px('BZ=F', _bzt['px'])} ({'+' if _bzt['chg']>0 else ''}{_bzt['chg']}%, {_mv})。")
    _clt = next((b for b in idx if b["sym"] == "CL=F"), None)
    if _clt and _clt["chg"] is not None:
        summary.append(f"WTI美油 {_brief_fmt_px('CL=F', _clt['px'])} ({'+' if _clt['chg']>0 else ''}{_clt['chg']}%)。")
    _dxy = next((b for b in idx if b["sym"] == "DX-Y.NYB"), None)
    if _dxy and _dxy["chg"] is not None:
        summary.append(f"美元指数 {_brief_fmt_px('DX-Y.NYB', _dxy['px'])} ({'+' if _dxy['chg']>0 else ''}{_dxy['chg']}%), "
                       + ("美元走强对以美元计价的油价构成压制。" if _dxy["chg"] > 0 else "美元走弱对油价构成支撑。"))
    if pos or neg:
        _mix = []
        if pos:
            _mix.append(f"利多{len(pos)}条(最强: {pos[0]['title'][:24]})")
        if neg:
            _mix.append(f"利空{len(neg)}条(最强: {neg[0]['title'][:24]})")
        summary.append("供需消息面: " + "；".join(_mix) + "。")
    else:
        summary.append("今日无显著OPEC/EIA/地缘消息, 油价或延续技术面与美元驱动。")
    _score = 0
    if _dxy and _dxy["chg"] is not None:
        _score += 1 if _dxy["chg"] < 0 else -1
    if pos or neg:
        _score += 1 if len(pos) > len(neg) else (-1 if len(neg) > len(pos) else 0)
    _bias = "偏多" if _score >= 2 else ("偏空" if _score <= -2 else "震荡")
    viewpoint = (f"综合美元方向与供需消息面, 今日油价倾向{_bias}(驱动评分{_score:+d})。"
                 f"以上为规则模板自动生成, 仅供参考, 不构成投资建议; 操作请结合买入区间与止损纪律。")
    return summary, viewpoint

print("Building oil brief...")
_oil_idx = _oil_overview()
_oil_tech = _oil_technical()
print(f"  原油晨报: 技术面{'完成' if _oil_tech else '不可用'}")
_oil_pos, _oil_neg = _oil_news()
_oil_macro_all = sorted([x for x in (brief_pos + brief_neg) if x["label"] == "宏观" and any(k in str(x.get('title','')) for k in ('原油','OPEC','OPEC+','库存','能源','EIA'))],
                        key=lambda x: abs(x.get('imp', 0)), reverse=True)[:6]
_oil_macro = _brief_macro_rows(_oil_macro_all)
_oil_today = _brief_today_str()
oil_summary, oil_view, oil_src = None, None, None
if _OIL_CACHE_F.exists():
    try:
        with open(_OIL_CACHE_F, "r", encoding="utf-8") as f:
            _oc = json.load(f)
        if _oc.get("date") == _oil_today and _oc.get("v") == 1 and _oc.get("summary") and _oc.get("viewpoint"):
            if _oc.get("src") == "llm" or not _brief_key:
                oil_summary, oil_view, oil_src = _oc["summary"], _oc["viewpoint"], _oc.get("src", "rule")
                print("  原油晨报: 复用当日缓存")
    except Exception:
        pass
if oil_summary is None:
    _bzt = next((b for b in _oil_idx if b["sym"] == "BZ=F"), None)
    _oil_digest = {
        "build_time_beijing": now_str,
        "oil": {"brent_usd": _bzt["px"], "chg_pct": _bzt["chg"]} if _bzt else None,
        "technicals": ({"support_levels": [round(v,1) for _, v in _oil_tech["sup"]],
                        "resistance_levels": [round(v,1) for _, v in _oil_tech["res"]],
                        "range_20d_position_pct": round(_oil_tech["pct"]*100, 1),
                        "chg_5d_pct": _oil_tech["chg5"]} if _oil_tech else None),
        "dxy": ({"price": _dxy_b["px"], "chg_pct": _dxy_b["chg"]} if (_dxy_b := next((x for x in _oil_idx if x["sym"]=="DX-Y.NYB"), None)) else None),
        "tnx": next((b["px"] for b in _oil_idx if b["sym"]=="^TNX"), None),
        "oil_news": [{"title": x["title"], "direction": x["direction"], "impact_pct": x["imp"]} for x in (_oil_pos + _oil_neg)],
        "macro_news": [{"title": x["title"], "direction": x["direction"]} for x in _oil_macro_all],
    }
    if _brief_key:
        oil_summary, oil_view, oil_src = _oil_llm(_brief_key, _brief_model, _brief_baseurl, _oil_digest)
    else:
        print("  原油晨报: 未配置API key, 使用规则模板")
    if oil_summary is None:
        oil_summary, oil_view = _oil_rule_summary(_oil_idx, _oil_pos, _oil_neg)
        oil_src = "rule"
    try:
        with open(_OIL_CACHE_F, "w", encoding="utf-8") as f:
            json.dump({"date": _oil_today, "v": 1, "summary": oil_summary, "viewpoint": oil_view, "src": oil_src}, f, ensure_ascii=False)
    except Exception:
        pass
_oil_src_tag = '<span class="gb-tag">DeepSeek</span>' if oil_src == "llm" else '<span class="gb-tag grey">规则生成</span>'
_oil_sum_items = "".join(f"<li>{_html.escape(s)}</li>" for s in oil_summary)
_oil_idx_grid = "".join(
    f'<div class="card"><div class="label">{_html.escape(b["name"])}</div>'
    f'<div class="value">{_brief_fmt_px(b["sym"], b["px"])}</div>'
    f'<div class="chg" style="color:{("#f85149" if (b["chg"] or 0) > 0 else ("#3fb950" if (b["chg"] or 0) < 0 else "#8b949e"))}">{"+" if (b["chg"] or 0) > 0 else ""}{b["chg"]}%</div></div>'
    for b in _oil_idx)
oil_html = f'''
<style>{OIL_CSS}</style>
<div class="gb-page">
<div class="gb-head">
  <div><div class="gb-title">原油每日晨报</div>
  <div class="gb-sub">生成: {now_str} ｜ 行情: Yahoo · 消息: Google News · 摘要: {"DeepSeek AI" if oil_src == "llm" else "规则模板"} ｜ 布油BZ/F 美油CL/F</div></div>
</div>
<div class="gb-card"><div class="gb-h">01 今日市场总览</div><div class="gb-idx">{_oil_idx_grid}</div></div>
<div class="gb-row">
  <div class="gb-card"><div class="gb-h">02 核心摘要 {_oil_src_tag}</div><ol class="gb-ol">{_oil_sum_items}</ol></div>
  <div class="gb-card"><div class="gb-h">03 供需消息面</div>
    <div class="gb-subh pos">利多因素 <em>{len(_oil_pos)}</em></div>
    <div class="gb-news">{_brief_news_col(_oil_pos, True)}</div>
    <div class="gb-subh neg">利空因素 <em>{len(_oil_neg)}</em></div>
    <div class="gb-news">{_brief_news_col(_oil_neg, False)}</div>
  </div>
</div>
<div class="gb-row">
  <div class="gb-card"><div class="gb-h">04 原油技术面 <span class="gb-tag grey">客观计算</span></div>{_brief_technical_html(_oil_tech)}</div>
  <div class="gb-card"><div class="gb-h">05 今日宏观数据 <span class="gb-tag teal">简化版</span></div>{_brief_macro_html(_oil_macro)}</div>
</div>
<div class="gb-card"><div class="gb-h">06 今日观点 {_oil_src_tag}</div><p class="gb-view">{_html.escape(oil_view)}</p></div>
</div>'''

# =====================================================================
# dashboard_js: 两张表的行渲染 + 汇总 + 手动"刷新数据"按钮的浏览器端重算。
# 用普通(非f)字符串承载, 内部 JS 的 ${...} 与 { } 均无需转义, 通过 {dashboard_js} 注入 f-string。
# 部署在静态 GitHub Pages(无后端), 故刷新只能前端调 Yahoo 接口拉行情、重算当下列并重绘。
# =====================================================================
dashboard_js = r'''

const WHITE_LOGOS = {MRVL:1, AMD:1, STX:1, MU:1, NVO:1}; // 深色logo转白, 避免与深色背景混淆
const _letBadge = (sym) => { const ch = ((sym.match(/[A-Za-z]/) || [])[0] || sym[0] || '?').toUpperCase(); return `<span class="logo-letter" style="display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:4px;background:#34558b;color:#fff;font-size:11px;font-weight:600">${ch}</span>`; };
// 仅 SPY/QQQ/无logo满记 用首字母徽标; 其余: 优先本站 logos/{sym}.png, 否则 Google favicon(失败则删除), 无域名留空
const LETTER_LOGO = {SPY:1, QQQ:1, GLW:1, '600519.SS':1, '601088.SS':1, '600406.SS':1};
const logoCell = (sym) => {
  if (LETTER_LOGO[sym]) return `<td class="logo-cell">${_letBadge(sym)}</td>`;
  let src = '';
  if (LOGO_DOMAINS[sym]) src = (SELF_LOGOS.indexOf(sym) >= 0) ? './logos/' + sym + '.png' : 'https://www.google.com/s2/favicons?domain=' + LOGO_DOMAINS[sym] + '&sz=64';
  if (src === '') return '<td class="logo-cell"></td>';
  const fb = (src.indexOf('./') === 0) ? 'https://www.google.com/s2/favicons?domain=' + LOGO_DOMAINS[sym] + '&sz=64' : src;
  return `<td class="logo-cell"><img class="stock-logo" loading="lazy" style="filter:${WHITE_LOGOS[sym]?'brightness(0) invert(1)':''}" src="${src}" onerror="if(!this.dataset.f){this.dataset.f=1;this.src='${fb}';}else{this.remove();}" alt=""></td>`;
};

// 价格格式化: 大数(韩元/日元等)不显示小数
const pxf = (v) => v >= 1000 ? v.toFixed(0) : v.toFixed(2);
// 财务指标统一加%: 值本身已带%则原样, 否则追加%(数字或字符串)
const mpct = (v) => {
  if (v === null || v === undefined || v === '') return '-';
  const s = String(v);
  return s.includes('%') ? s : (s + '%');
};

// ---- 汇总卡片 ----
function renderSummaries(){
  const _watchTxt = WATCH_CARD.action || ('仓位 ' + Math.round((WATCH_CARD.exposure || 0) * 100) + '%');
  const _watchCls = 'green';
  const e = data.filter(d => d.eligible), bz = data.filter(d => d.zone && d.zone.startsWith('BUY')), sz = data.filter(d => d.zone && d.zone.startsWith('SELL'));
  document.getElementById('summary').innerHTML = `
  <div class="card"><div class="label">监控股票</div><div class="value blue">${data.length}</div></div>
  <div class="card"><div class="label">可交易股票</div><div class="value green">${e.length}</div></div>
  <div class="card"><div class="label">买入区</div><div class="value green">${bz.length}</div></div>
  <div class="card"><div class="label">卖出区</div><div class="value red">${sz.length}</div></div>
  <div class="card"><div class="label">建议仓位</div><div class="value ${_watchCls}" style="font-size:14px;line-height:1.4">${_watchTxt}</div></div>`;
  const he = hkData.filter(d => d.eligible), hbz = hkData.filter(d => d.zone && d.zone.startsWith('BUY')), hsz = hkData.filter(d => d.zone && d.zone.startsWith('SELL'));
  document.getElementById('hk-summary').innerHTML = `
  <div class="card"><div class="label">监控股票</div><div class="value blue">${hkData.length}</div></div>
  <div class="card"><div class="label">可交易股票</div><div class="value green">${he.length}</div></div>
  <div class="card"><div class="label">买入区</div><div class="value green">${hbz.length}</div></div>
  <div class="card"><div class="label">卖出区</div><div class="value red">${hsz.length}</div></div>`;
  const ae = aData.filter(d => d.eligible), abz = aData.filter(d => d.zone && d.zone.startsWith('BUY')), asz = aData.filter(d => d.zone && d.zone.startsWith('SELL'));
  document.getElementById('a-summary').innerHTML = `
  <div class="card"><div class="label">监控股票</div><div class="value blue">${aData.length}</div></div>
  <div class="card"><div class="label">可交易股票</div><div class="value green">${ae.length}</div></div>
  <div class="card"><div class="label">买入区</div><div class="value green">${abz.length}</div></div>
  <div class="card"><div class="label">卖出区</div><div class="value red">${asz.length}</div></div>`;
}

// ---- 美股行 ----
function usRow(d){
  if (d.error) return `<tr><td class="sym"></td><td colspan="23" style="color:#aaa">${d.error}</td></tr>`;
  const pctW = Math.max(0, Math.min(1, d.pct)) * 100;
  const buy1W = d.buy1_pct * 100, buy2W = d.buy2_pct * 100, buy3W = d.buy3_pct * 100;
  const sell1W = d.sell1_pct * 100, sell2W = d.sell2_pct * 100;
  const eligClass = d.eligible ? 'eligible' : 'ineligible', eligText = d.eligible ? 'Y' : 'N';
  const bollColor = d.boll_pct === null ? '#aaa' : d.boll_pct < 0.2 ? '#3B6D11' : d.boll_pct > 1 ? '#A32D2D' : d.boll_pct > 0.8 ? '#BA7517' : '#2c2c2a';
  const bollText = d.boll_pct === null ? '-' : (d.boll_pct * 100).toFixed(1) + '%';
  const bollWColor = d.boll_pct_w === null ? '#aaa' : d.boll_pct_w < 0.2 ? '#3B6D11' : d.boll_pct_w > 1 ? '#A32D2D' : d.boll_pct_w > 0.8 ? '#BA7517' : '#2c2c2a';
  const bollWText = d.boll_pct_w === null ? '-' : (d.boll_pct_w * 100).toFixed(1) + '%';
  const bollWt = (d.boll_pct !== null && d.boll_pct < 0.2) ? 700 : 500;
  const bollWWt = (d.boll_pct_w !== null && d.boll_pct_w < 0.2) ? 700 : 500;
  const posClass = d.has_pos ? ' has-position' : (d.is_index ? '' : (d.eligible && d.zone && d.zone.startsWith('BUY') ? ' eligible-no-pos' : ''));
  const valText = (d.buy_cfg > 0 && d.sell_cfg > 0) ? `${d.ccy}${d.buy_cfg.toFixed(0)} - ${d.sell_cfg.toFixed(0)}` : '-';
  const _rd = d.report_date || ''; let rdText = '-';
  if (_rd) { const _p = _rd.split('-'); rdText = parseInt(_p[1]) + '.' + parseInt(_p[2]); }
  const valColor = d.stale_valuation ? '#A32D2D' : '#e6e8ef';
  const rdColor = d.stale_valuation ? '#A32D2D' : '#e6e8ef';
  const rdMark = d.report_imminent ? ' <span style="color:#A32D2D;font-size:10px">⚠️新财报·临近</span>' : '';
  const pfr = d.round_price ? (v => v.toFixed(0)) : pxf;
  const newsTag = d.news_shift_pct ? `<span style="font-size:10px;color:${d.news_shift_pct < 0 ? '#A32D2D' : '#3B6D11'};margin-left:2px">📰${(d.news_shift_pct*100).toFixed(0)}%</span>` : '';
  const ratingTag = d.rating ? `<span style="font-size:10px;background:#8b5cf6;color:#fff;padding:1px 5px;border-radius:3px">${d.rating}</span>` : '-';
  const m2Style = (d.m2 !== null && d.m2 !== undefined && d.m2 !== '') ? (d.m2 < 0 ? 'color:#3fb950;font-weight:700' : 'color:#e6e8ef;font-weight:500') : 'color:#aaa';
  const m2Text = (d.m2 !== null && d.m2 !== undefined && d.m2 !== '') ? `${d.m2}%` : '-';
  const peText = (d.pe !== null && d.pe !== undefined && d.pe !== '') ? d.pe : '-';
  return `
  <tr class="${posClass.trim()}">
    ${logoCell(d.sym)}
    ${d.has_analysis ? `<td class="sym"><a href="analysis/${d.sym}.html" style="color:inherit;text-decoration:none;border-bottom:1px dashed #999">${d.sym}</a></td>` : `<td class="sym">${d.sym}</td>`}
    <td class="name" style="font-weight:500">${d.name}</td>
    <td class="name" style="font-weight:500">${d.industry}</td>
    <td class="num" style="font-weight:500">${d.ccy}${pfr(d.px)}</td>
    <td class="num" style="font-size:12px">${ratingTag}</td>
    <td class="num" style="color:${rdColor};font-size:12px;font-weight:500">${rdText}${rdMark}</td>
    <td class="num" style="font-size:12px;color:#e6e8ef;font-weight:500">${peText}</td>
    <td class="num" style="font-size:12px;color:#e6e8ef;font-weight:500">${mpct(d.m1)}</td>
    <td class="num" style="font-size:12px;${m2Style}">${m2Text}</td>
    <td class="num" style="font-size:12px;color:#e6e8ef;font-weight:500">${mpct(d.m3)}</td>
        <td class="num" style="font-size:12px;color:${valColor};font-weight:500">${valText}</td>
    <td class="num" style="font-size:12px;color:#e6e8ef;font-weight:500">${d.ccy}${d.alow.toFixed(0)} - ${d.ahigh.toFixed(0)}</td>
    <td class="num" style="font-size:12px;color:#e6e8ef;font-weight:500">${(d.vol*100).toFixed(1)}%</td>
    <td class="num" style="color:#97C459">${d.ccy}${pfr(d.p_buy3)}${newsTag}</td>
    <td class="num" style="color:#A32D2D">${d.ccy}${pfr(d.p_sell2)}${newsTag}</td>
    <td class="num" style="color:${bollColor};font-weight:${bollWt}">${bollText}</td>
    <td class="num" style="color:${bollWColor};font-weight:${bollWWt}">${bollWText}</td>
    <td class="num" style="font-weight:500">${(d.pct*100).toFixed(1)}%</td>
    <td class="bar-cell"><div class="bar-wrap">
      <div class="bar-buy1" style="left:0;width:${buy1W}%"></div>
      <div class="bar-buy3" style="left:0;width:${buy3W}%"></div>
      <div class="bar-sell2" style="left:${sell2W}%;width:${100 - sell2W}%"></div>
      <div class="bar-pct" style="left:${pctW}%"></div>
      <div class="bar-pct-label" style="left:${pctW}%">${d.zone}</div>
    </div></td>
    <td class="num" title="${d.ratio>=999 ? '现价低于买入区, 强买信号' : ''}" style="${d.ratio>=999 ? 'color:#3B6D11;font-weight:600' : ''}">${d.ratio>=999 ? '∞' : d.ratio}</td>
    <td class="num" style="color:${d.loss_rate < -10 ? '#A32D2D' : '#3B6D11'}">${d.loss_rate}%</td>
    <td class="${eligClass}">${eligText}</td>
  </tr>`;
}

// ---- 中概股行 ----
function hkRow(d){
  if (d.error) return `<tr><td class="sym"></td><td colspan="23" style="color:#aaa">${d.error}</td></tr>`;
  const pctW = Math.max(0, Math.min(1, d.pct)) * 100;
  const buy1W = d.buy1_pct * 100, buy2W = d.buy2_pct * 100, buy3W = d.buy3_pct * 100;
  const sell1W = d.sell1_pct * 100, sell2W = d.sell2_pct * 100;
  const eligClass = d.eligible ? 'eligible' : 'ineligible', eligText = d.eligible ? 'Y' : 'N';
  const bollColor = d.boll_pct === null ? '#aaa' : d.boll_pct < 0.2 ? '#3B6D11' : d.boll_pct > 1 ? '#A32D2D' : d.boll_pct > 0.8 ? '#BA7517' : '#2c2c2a';
  const bollText = d.boll_pct === null ? '-' : (d.boll_pct * 100).toFixed(1) + '%';
  const bollWColor = d.boll_pct_w === null ? '#aaa' : d.boll_pct_w < 0.2 ? '#3B6D11' : d.boll_pct_w > 1 ? '#A32D2D' : d.boll_pct_w > 0.8 ? '#BA7517' : '#2c2c2a';
  const bollWText = d.boll_pct_w === null ? '-' : (d.boll_pct_w * 100).toFixed(1) + '%';
  const bollWt = (d.boll_pct !== null && d.boll_pct < 0.2) ? 700 : 500;
  const bollWWt = (d.boll_pct_w !== null && d.boll_pct_w < 0.2) ? 700 : 500;
  const valText = (d.buy_cfg > 0 && d.sell_cfg > 0) ? `${d.ccy}${d.buy_cfg.toFixed(0)} - ${d.sell_cfg.toFixed(0)}` : '-';
  const _rd = d.report_date || ''; let rdText = '-';
  if (_rd) { const _p = _rd.split('-'); rdText = parseInt(_p[1]) + '.' + parseInt(_p[2]); }
  const valColor = d.stale_valuation ? '#A32D2D' : '#e6e8ef';
  const rdColor = d.stale_valuation ? '#A32D2D' : '#e6e8ef';
  const rdMark = d.report_imminent ? ' <span style="color:#A32D2D;font-size:10px">⚠️新财报·临近</span>' : '';
  const pfr = d.round_price ? (v => v.toFixed(0)) : pxf;
  const ratingTag = d.rating ? `<span style="font-size:10px;background:#8b5cf6;color:#fff;padding:1px 5px;border-radius:3px;margin-left:4px">${d.rating}</span>` : '';
  const m2Style = (d.m2 !== null && d.m2 !== undefined && d.m2 !== '') ? (d.m2 < 0 ? 'color:#3fb950;font-weight:700' : 'color:#e6e8ef;font-weight:500') : 'color:#aaa';
  const m2Text = (d.m2 !== null && d.m2 !== undefined && d.m2 !== '') ? `${d.m2}%` : '-';
  const peText = (d.pe !== null && d.pe !== undefined && d.pe !== '') ? d.pe : '-';
  return `
  <tr>
    ${logoCell(d.sym)}
    ${d.has_analysis ? `<td class="sym"><a href="analysis/${d.sym}.html" style="color:inherit;text-decoration:none;border-bottom:1px dashed #999">${d.sym}</a></td>` : `<td class="sym">${d.sym}</td>`}
    <td class="name" style="font-weight:500">${d.name}</td>
    <td class="name" style="font-weight:500">${d.industry}</td>
    <td class="num" style="font-weight:500">${d.ccy}${pfr(d.px)}</td>
    <td class="num" style="font-size:12px">${ratingTag || '-'}</td>
    <td class="num" style="color:${rdColor};font-size:12px;font-weight:500">${rdText}${rdMark}</td>
    <td class="num" style="font-size:12px;color:#e6e8ef;font-weight:500">${peText}</td>
    <td class="num" style="font-size:12px;color:#e6e8ef;font-weight:500">${mpct(d.m1)}</td>
    <td class="num" style="font-size:12px;${m2Style}">${m2Text}</td>
    <td class="num" style="font-size:12px;color:#e6e8ef;font-weight:500">${mpct(d.m3)}</td>
        <td class="num" style="font-size:12px;color:${valColor};font-weight:500">${valText}</td>
    <td class="num" style="font-size:12px;color:#e6e8ef;font-weight:500">${d.ccy}${d.alow.toFixed(0)} - ${d.ahigh.toFixed(0)}</td>
    <td class="num" style="font-size:12px;color:#e6e8ef;font-weight:500">${(d.vol*100).toFixed(1)}%</td>
    <td class="num" style="color:#97C459">${d.ccy}${pfr(d.p_buy3)}</td>
    <td class="num" style="color:#A32D2D">${d.ccy}${pfr(d.p_sell2)}</td>
    <td class="num" style="color:${bollColor};font-weight:${bollWt}">${bollText}</td>
    <td class="num" style="color:${bollWColor};font-weight:${bollWWt}">${bollWText}</td>
    <td class="num" style="font-weight:500">${(d.pct*100).toFixed(1)}%</td>
    <td class="bar-cell"><div class="bar-wrap">
      <div class="bar-buy1" style="left:0;width:${buy1W}%"></div>
      <div class="bar-buy3" style="left:0;width:${buy3W}%"></div>
      <div class="bar-sell2" style="left:${sell2W}%;width:${100 - sell2W}%"></div>
      <div class="bar-pct" style="left:${pctW}%"></div>
      <div class="bar-pct-label" style="left:${pctW}%">${d.zone}</div>
    </div></td>
    <td class="num" title="${d.ratio>=999 ? '现价低于买入区, 强买信号' : ''}" style="${d.ratio>=999 ? 'color:#3B6D11;font-weight:600' : ''}">${d.ratio>=999 ? '∞' : d.ratio}</td>
    <td class="num" style="color:${d.loss_rate < -10 ? '#A32D2D' : '#3B6D11'}">${d.loss_rate}%</td>
    <td class="${eligClass}">${eligText}</td>
  </tr>`;
}

function renderTables(){ document.getElementById('tbody').innerHTML = data.map(usRow).join(''); document.getElementById('hk-tbody').innerHTML = hkData.map(hkRow).join(''); document.getElementById('a-tbody').innerHTML = aData.map(hkRow).join(''); }

// ---- 大宗商品行 ----
function commodityRow(d){
  const cf = (v) => v >= 1000 ? v.toFixed(0) : v.toFixed(1);
  const bollTxt = (b) => (b === null || b === undefined) ? '-' : (b * 100).toFixed(1) + '%';
  const bollCol = (b) => (b === null || b === undefined) ? '#aaa' : b < 0.2 ? '#3B6D11' : b > 1 ? '#A32D2D' : b > 0.8 ? '#BA7517' : '#e6e8ef';
  const pctW = Math.max(0, Math.min(1, d.pct)) * 100;
  const buy1W = d.bp1 * 100, buy3W = d.bp3 * 100, sell2W = d.sp2 * 100;
  return `
  <tr>
    <td class="sym" title="${d.sym}" style="font-weight:600">${d.name}</td>
    <td class="num" style="font-weight:500">${d.ccy}${pxf(d.px)}</td>
    <td class="num" style="color:#97C459">${d.ccy}${cf(d.p_buy2)}</td>
    <td class="num" style="color:#97C459">${d.ccy}${cf(d.p_buy3)}</td>
    <td class="num" style="color:#A32D2D">${d.ccy}${cf(d.p_sell1)}</td>
    <td class="num" style="color:#A32D2D">${d.ccy}${cf(d.p_sell2)}</td>
    <td class="num" style="color:${bollCol(d.boll_d)}">${bollTxt(d.boll_d)}</td>
    <td class="num" style="color:${bollCol(d.boll_w)}">${bollTxt(d.boll_w)}</td>
    <td class="num" style="color:${bollCol(d.boll_m)}">${bollTxt(d.boll_m)}</td>
    <td class="num" style="font-weight:500">${(d.pct*100).toFixed(1)}%</td>
    <td class="bar-cell"><div class="bar-wrap">
      <div class="bar-buy1" style="left:0;width:${buy1W}%"></div>
      <div class="bar-buy3" style="left:0;width:${buy3W}%"></div>
      <div class="bar-sell2" style="left:${sell2W}%;width:${100 - sell2W}%"></div>
      <div class="bar-pct" style="left:${pctW}%"></div>
      <div class="bar-pct-label" style="left:${pctW}%">${d.zone}</div>
    </div></td>
    <td class="num ${d.zone_class}">${d.zone}</td>
  </tr>`;
}

function renderCommod(){ const el = document.getElementById('commod-tbody'); if (el) el.innerHTML = commodities.map(commodityRow).join(''); }
// ---- 每日晨报: 市场总览卡片渲染/刷新 (BRIEF_IDX 由后端注入) ----
function briefFmtPx(b){
  if (b.px == null || isNaN(b.px)) return '-';
  if (b.sym === '^TNX') return b.px.toFixed(2) + '%';
  const a = Math.abs(b.px);
  return a >= 1000 ? b.px.toLocaleString('en-US', {maximumFractionDigits: 0}) : a >= 100 ? b.px.toFixed(1) : b.px.toFixed(2);
}
function renderBriefIdx(){
  const el = document.getElementById('brief-idx-grid'); if (!el) return;
  el.innerHTML = BRIEF_IDX.map(b => {
    const chg = (b.chg == null || isNaN(b.chg)) ? '-' : (b.chg > 0 ? '+' : '') + b.chg.toFixed(2) + '%';
    const cc = (b.chg == null || isNaN(b.chg)) ? '#8b949e' : (b.chg > 0 ? '#f85149' : b.chg < 0 ? '#3fb950' : '#8b949e');
    return `<div class="card"><div class="label">${b.name}</div><div class="value">${briefFmtPx(b)}</div>` +
           `<div class="chg" style="color:${cc}">${chg}</div></div>`;
  }).join('');
}


// ---- 截图分享 ----
async function loadH2C(){
  if (window.html2canvas) return window.html2canvas;
  const urls = ['https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js',
                'https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js',
                'https://unpkg.com/html2canvas@1.4.1/dist/html2canvas.min.js'];
  for (const u of urls) {
    try {
      await new Promise((res, rej) => { const s = document.createElement('script'); s.src = u; s.onload = res; s.onerror = rej; document.head.appendChild(s); });
      if (window.html2canvas) return window.html2canvas;
    } catch(e) {}
  }
  throw new Error('截图库加载失败(检查网络)');
}
async function shareShot(btn){
  const orig = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '生成中…';
  try {
    const h2c = await loadH2C();
    const target = document.getElementById('dashboard-main') || document.querySelector('.wrap') || document.body;
    const bg = getComputedStyle(document.body).backgroundColor || '#0d1117';
    const canvas = await h2c(target, { backgroundColor: bg, useCORS: true, scale: Math.min(2, window.devicePixelRatio || 1) });
    const blob = await new Promise(res => canvas.toBlob(res, 'image/png'));
    const fname = 'dashboard_' + new Date().toISOString().slice(0,10) + '.png';
    const file = new File([blob], fname, { type: 'image/png' });
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      await navigator.share({ files: [file], title: '交易看板' });
    } else {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = fname; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 8000);
    }
    btn.innerHTML = '✓ 已生成';
  } catch(e) {
    btn.innerHTML = '✗ 失败';
    alert('截图失败: ' + (e.message || e));
  }
  setTimeout(() => { btn.innerHTML = orig; btn.disabled = false; }, 1500);
}

// 初始渲染
renderSummaries(); renderTables(); renderCommod(); renderBriefIdx();
'''

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta http-equiv="refresh" content="300">
<title>交易看板</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif;
  background: #f5f5f4; color: #2c2c2a; padding: 20px; min-width: 1350px;
}}
.header {{
  display: flex; justify-content: space-between; align-items: center;
  padding: 16px 24px; background: #fff; border-radius: 12px;
  border: 1px solid #e5e5e5; margin-bottom: 16px;
}}
.header h1 {{ font-size: 18px; font-weight: 500; }}
.header .time {{ font-size: 13px; color: #378ADD; font-weight: 500; }}
.header .refresh {{
  background: #378ADD; color: #fff; border: none; border-radius: 8px;
  padding: 8px 16px; font-size: 13px; cursor: pointer; font-weight: 500;
}}
.header .refresh:hover {{ background: #185FA5; }}
/* Tabs */
.tabs {{
  display: flex; gap: 0; margin-bottom: 16px;
}}
.tab-btn {{
  background: #fff; border: 1px solid #e5e5e5; border-bottom: none;
  padding: 10px 24px; font-size: 14px; font-weight: 500; cursor: pointer;
  color: #666; border-radius: 12px 12px 0 0; margin-right: -1px;
}}
.tab-btn.active {{
  background: #fff; color: #378ADD; border-bottom: 2px solid #fff;
  position: relative; z-index: 1; font-weight: 600;
}}
.tab-btn:not(.active) {{ background: #fafaf9; }}
.tab-btn:hover:not(.active) {{ color: #378ADD; }}
.tab-content {{ display: none; }}
.tab-content.active {{ display: block; }}
.summary {{
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px;
}}
.summary .card {{
  background: #fff; border-radius: 10px; padding: 16px; border: 1px solid #e5e5e5;
}}
.summary .card .label {{ font-size: 12px; color: #888; margin-bottom: 4px; }}
.summary .card .value {{ font-size: 22px; font-weight: 500; }}
.summary .card .value.green {{ color: #3B6D11; }}
.summary .card .value.red {{ color: #A32D2D; }}
.summary .card .value.blue {{ color: #378ADD; }}
table {{
  width: 100%; border-collapse: collapse; background: #fff;
  border-radius: 12px; overflow: hidden; border: 1px solid #e5e5e5; table-layout: fixed;
}}
thead {{ background: #fafaf9; }}
th {{
  padding: 10px 4px; font-size: 12px; font-weight: 500; color: #666;
  text-align: center; border-bottom: 1px solid #e5e5e5; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis;
}}
td {{ padding: 8px 4px; font-size: 13px; border-bottom: 1px solid #f0f0f0; text-align: center; white-space: nowrap; }}
td.sym {{ font-weight: 500; }}
td.name {{ color: #888; font-size: 12px; }}
td.num {{ font-variant-numeric: tabular-nums; }}
.eligible {{ color: #3B6D11; font-weight: 500; }}
.ineligible {{ color: #A32D2D; font-weight: 500; }}
.bar-cell {{ width: 200px; }}
.bar-wrap {{ position: relative; height: 20px; background: #f5f5f4; border-radius: 3px; overflow: hidden; }}
.bar-buy1 {{ position: absolute; top: 0; height: 100%; background: rgba(151,196,89,0.30); }}
.bar-buy2 {{ position: absolute; top: 0; height: 100%; background: rgba(151,196,89,0.20); }}
.bar-buy3 {{ position: absolute; top: 0; height: 100%; background: rgba(151,196,89,0.10); }}
.bar-sell1 {{ position: absolute; top: 0; height: 100%; background: rgba(226,75,74,0.15); }}
.bar-sell2 {{ position: absolute; top: 0; height: 100%; background: rgba(226,75,74,0.25); }}
.bar-pct {{ position: absolute; top: 0; width: 2px; height: 100%; background: #444; }}
.bar-pct-label {{ position: absolute; top: -1px; font-size: 9px; font-weight: 500; transform: translateX(4px); line-height: 20px; white-space: nowrap; }}
.zone-buy1 {{ color: #3B6D11; font-weight: 500; }}
.zone-buy2 {{ color: #639922; font-weight: 500; }}
.zone-buy3 {{ color: #97C459; font-weight: 500; }}
.zone-sell1 {{ color: #BA7517; font-weight: 500; }}
.zone-sell2 {{ color: #A32D2D; font-weight: 500; }}
.zone-lower {{ color: #5F5E5A; }}
.zone-upper {{ color: #5F5E5A; }}
/* 做空子页: 档位 bar + 状态 */
.bar-short1 {{ position: absolute; top: 0; height: 100%; background: rgba(226,75,74,0.22); }}
.bar-short2 {{ position: absolute; top: 0; height: 100%; background: rgba(226,75,74,0.45); }}
.bar-short3 {{ position: absolute; top: 0; height: 100%; background: rgba(226,75,74,0.30); }}
.bar-tp     {{ position: absolute; top: 0; height: 100%; background: rgba(151,196,89,0.30); }}
.szone1 {{ color: #BA7517; font-weight: 500; }}
.szone2 {{ color: #C0392B; font-weight: 600; }}
.szone3 {{ color: #A32D2D; font-weight: 700; }}
.szone-near {{ color: #BA7517; }}
.szone-tp {{ color: #3B6D11; }}
.szone-low {{ color: #5F5E5A; }}
.short-rule-box {{ background: #FDF2F2; border-radius: 12px; border: 1px solid #E8C4C4; padding: 14px 20px; margin-bottom: 16px; font-size: 13px; color: #6E3B3B; line-height: 1.8; }}
tr.short-pos {{ background: rgba(226, 75, 74, 0.06); }}
.short-badge {{ display: inline-block; background: #A32D2D; color: #fff; font-size: 9px; padding: 1px 4px; border-radius: 3px; margin-left: 4px; vertical-align: middle; font-weight: 500; }}
.legend {{ display: flex; gap: 20px; margin: 12px 0; font-size: 12px; color: #888; padding: 0 4px; }}
.legend span {{ display: flex; align-items: center; gap: 4px; }}
.legend .dot {{ width: 10px; height: 10px; border-radius: 2px; }}
.advice-box {{ background: #fff; border-radius: 12px; border: 1px solid #e5e5e5; margin-top: 16px; padding: 16px 20px; }}
.advice-box h3 {{ font-size: 15px; font-weight: 500; margin-bottom: 10px; color: #2c2c2a; }}
.advice-box h3 .ai-tag {{ background: #6C5CE7; color: #fff; font-size: 10px; padding: 2px 6px; border-radius: 4px; margin-left: 8px; font-weight: 500; vertical-align: middle; }}
.advice-content {{ font-size: 14px; line-height: 1.7; color: #333; white-space: pre-wrap; }}
.wait-queue-box {{ background: #fff; border-radius: 12px; border: 1px solid #e5e5e5; margin-top: 16px; padding: 16px 20px; }}
.wait-queue-box h3 {{ font-size: 15px; font-weight: 500; margin-bottom: 10px; color: #2c2c2a; }}
.wq-count {{ background: #378ADD; color: #fff; font-size: 10px; padding: 2px 6px; border-radius: 4px; margin-left: 8px; font-weight: 500; vertical-align: middle; }}
.wq-list {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }}
.wq-item {{ background: #F0F7FF; border: 1px solid #B8D4F0; border-radius: 8px; padding: 6px 12px; font-size: 13px; font-weight: 500; }}
.wq-ratio {{ color: #888; font-size: 11px; margin-left: 4px; }}
.wq-note {{ font-size: 12px; color: #888; }}
.deposit-alert-box {{ background: #FFF5F5; border-radius: 12px; border: 2px solid #F56565; margin-top: 16px; padding: 16px 20px; }}
.deposit-alert-box h3 {{ font-size: 15px; font-weight: 600; margin-bottom: 10px; color: #C53030; }}
.da-detail {{ font-size: 13px; color: #555; margin-bottom: 6px; }}
.da-action {{ font-size: 14px; color: #C53030; font-weight: 500; }}
.footer {{ text-align: center; padding: 20px; font-size: 12px; color: #aaa; }}
.status-ok {{ color: #3B6D11; font-weight: 500; }}
.status-warn {{ color: #A32D2D; font-weight: 500; }}
.status-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; vertical-align: middle; }}
.status-dot.green {{ background: #3B6D11; }}
.status-dot.red {{ background: #A32D2D; }}
tr.has-position {{ background: rgba(55, 138, 221, 0.07); }}
tr.has-obs {{ background: rgba(136, 136, 136, 0.06); }}
tr.eligible-no-pos {{ background: rgba(151, 196, 89, 0.15); }}
.lever-10 {{ color: #3B6D11; font-weight: 600; }}
.stock-logo {{ height: 16px; width: 22px; max-width: 22px; margin: 0 auto; display: block; border-radius: 3px; object-fit: contain; }}
.logo-col {{ width: 28px; min-width: 28px; }}
.logo-cell {{ width: 28px; min-width: 28px; max-width: 28px; padding: 0 2px !important; text-align: center; overflow: hidden; }}
.lever-7 {{ color: #639922; font-weight: 600; }}
tr.has-position td.sym {{ position: relative; }}
tr.has-position td.sym::before {{ content: ''; position: absolute; left: 0; top: 25%; height: 50%; width: 3px; background: #378ADD; border-radius: 2px; }}
.pos-badge {{ display: inline-block; background: #378ADD; color: #fff; font-size: 9px; padding: 1px 4px; border-radius: 3px; margin-left: 4px; vertical-align: middle; font-weight: 500; }}
.obs-badge {{ display: inline-block; background: #999; color: #fff; font-size: 9px; padding: 1px 4px; border-radius: 3px; margin-left: 4px; vertical-align: middle; font-weight: 500; }}
tr.has-obs td.sym {{ position: relative; }}
tr.has-obs td.sym::before {{ content: ''; position: absolute; left: 0; top: 25%; height: 50%; width: 3px; background: #999; border-radius: 2px; }}
tr.is-index {{ border-bottom: 2px solid #e0e0e0; }}
tr.is-index td.sym {{ font-weight: 700; color: #185FA5; }}
/* News tab styles */
.stock-section {{ background: #fff; border-radius: 12px; border: 1px solid #e5e5e5; margin-bottom: 16px; overflow: hidden; }}
.macro-section {{ border-color: #378ADD; border-width: 2px; }}
.macro-header {{ background: #EFF6FF !important; }}
.stock-header {{ display: flex; justify-content: space-between; align-items: center; padding: 14px 20px; background: #fafaf9; border-bottom: 1px solid #e5e5e5; }}
.stock-info {{ display: flex; align-items: center; gap: 8px; }}
.stock-sym {{ font-size: 16px; font-weight: 600; }}
.stock-name {{ font-size: 14px; color: #666; }}
.pos-tag {{ background: #378ADD; color: #fff; font-size: 10px; padding: 2px 6px; border-radius: 4px; font-weight: 500; }}
.eli-tag {{ background: #3B6D11; color: #fff; font-size: 10px; padding: 2px 6px; border-radius: 4px; font-weight: 500; }}
.macro-tag {{ background: #378ADD; color: #fff; font-size: 10px; padding: 2px 6px; border-radius: 4px; font-weight: 500; }}
.stock-agg {{ font-size: 14px; font-weight: 500; }}
.agg-positive {{ color: #A32D2D; }}
.agg-negative {{ color: #3B6D11; }}
.agg-neutral {{ color: #888; }}
.earnings-row {{ padding: 6px 20px; font-size: 12px; color: #666; background: #FAFAF9; border-bottom: 1px solid #f0f0f0; display: flex; align-items: center; gap: 6px; }}
.earnings-row::before {{ content: "\\1F4C5"; font-size: 12px; }}
.ern-soon {{ color: #A32D2D; font-weight: 600; }}
.ern-near {{ color: #BA7517; font-weight: 500; }}
.ern-far {{ color: #666; }}
.news-list {{ padding: 8px 16px; }}
.news-card {{ padding: 12px 8px; border-bottom: 1px solid #f0f0f0; }}
.news-card:last-child {{ border-bottom: none; }}
.news-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }}
.date {{ font-size: 12px; color: #888; }}
.provider {{ font-size: 12px; color: #666; font-weight: 500; }}
.type-tag {{ color: #fff; font-size: 10px; padding: 1px 6px; border-radius: 3px; font-weight: 500; }}
.badge {{ font-size: 10px; padding: 1px 6px; border-radius: 3px; font-weight: 600; }}
.badge.positive {{ color: #3B6D11; background: #E8F5E9; }}
.badge.negative {{ color: #A32D2D; background: #FDECEA; }}
.badge.neutral {{ color: #888; background: #F0F0F0; }}
.impact {{ font-size: 13px; font-weight: 600; }}
.pos-header-bar {{ background: #fff; border-radius: 12px; border: 1px solid #e5e5e5; padding: 12px 20px; margin-bottom: 16px; display: flex; justify-content: space-between; align-items: center; }}
.pos-table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 12px; overflow: hidden; border: 1px solid #e5e5e5; }}
.pos-table th {{ padding: 10px 12px; text-align: center; font-size: 12px; font-weight: 500; color: #888; background: #fafaf9; border-bottom: 1px solid #e5e5e5; }}
.pos-table td {{ padding: 10px 12px; text-align: center; font-size: 13px; border-bottom: 1px solid #f0f0f0; }}
.pos-table .pnl-pos {{ color: #3B6D11; font-weight: 600; }}
.pos-table .pnl-neg {{ color: #A32D2D; font-weight: 600; }}
.pie-container {{ background: #fff; border-radius: 12px; border: 1px solid #e5e5e5; padding: 16px 20px; margin-top: 12px; }}
.pie-title {{ font-size: 14px; font-weight: 500; margin-bottom: 12px; color: #333; }}
.pie-body {{ display: flex; align-items: center; gap: 24px; }}
.pie-svg {{ width: 180px; height: 180px; flex-shrink: 0; }}
.pie-legend {{ flex: 1; display: flex; flex-direction: column; gap: 6px; }}
.pie-legend-item {{ display: flex; align-items: center; gap: 8px; font-size: 13px; }}
.pie-dot {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}
.pie-label {{ flex: 1; color: #333; }}
.pie-pct {{ font-weight: 600; color: #333; min-width: 48px; text-align: right; }}
.pie-val {{ color: #888; font-size: 12px; min-width: 60px; text-align: right; }}
.title-row {{ display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }}
.title-cn {{ font-size: 15px; font-weight: 500; line-height: 1.4; color: #1a1a1a; text-decoration: none; }}
a .title-cn:hover {{ color: #378ADD; }}
.reason-inline {{
  font-size: 12px; color: #555;
  padding-left: 6px; border-left: 2px solid #ddd;
  line-height: 1.4; white-space: nowrap;
}}
.news-summary-inline {{
  font-size: 12px; color: #6C5CE7; font-weight: 500;
  padding-left: 6px; border-left: 2px solid #6C5CE7;
  line-height: 1.4; white-space: nowrap;
}}
.ai-summary {{ padding: 10px 20px; background: linear-gradient(90deg, #F8F4FF 0%, #EFF6FF 100%); border-top: 1px solid #e5e5e5; font-size: 13px; color: #333; line-height: 1.6; }}
.ai-summary-tag {{ display: inline-block; background: #6C5CE7; color: #fff; font-size: 10px; padding: 2px 6px; border-radius: 4px; margin-right: 8px; font-weight: 500; vertical-align: middle; }}
.no-data {{ text-align: center; padding: 40px; color: #888; font-size: 14px; }}
/* Market Regime tab - visual bar styles */
.regime-verdict {{
  display: flex; justify-content: space-between; align-items: center;
  border-radius: 12px; padding: 20px 24px; margin-bottom: 12px;
  border: 1px solid #e5e5e5;
}}
.regime-verdict-left {{ display: flex; flex-direction: column; gap: 4px; }}
.regime-verdict-stage {{ font-size: 22px; font-weight: 600; color: #2c2c2a; }}
.regime-verdict-score {{ font-size: 16px; font-weight: 500; }}
.regime-verdict-right {{ display: flex; flex-direction: column; gap: 6px; text-align: right; }}
.regime-verdict-exposure {{ font-size: 16px; color: #2c2c2a; }}
.regime-verdict-exposure strong {{ font-size: 24px; color: #378ADD; }}
.regime-verdict-action {{ font-size: 15px; color: #555; }}
.regime-verdict-action strong {{ color: #C53030; }}
.regime-explain {{
  background: #F0F7FF; border-radius: 8px; border: 1px solid #B8D4F0;
  padding: 10px 16px; margin-bottom: 12px;
  font-size: 13px; color: #378ADD; line-height: 1.6;
}}
/* Dimension rows */
.r-row {{
  display: flex; align-items: center; gap: 12px;
  padding: 10px 0; border-bottom: 1px solid #f5f5f4;
}}
.r-row:last-child {{ border-bottom: none; }}
.r-label {{
  width: 130px; font-size: 13px; font-weight: 600; color: #333;
  flex-shrink: 0;
}}
.r-bar-wrap {{ flex: 1; padding: 0 4px; }}
.r-bar-track {{
  position: relative; height: 14px; background: #f0f0f0;
  border-radius: 7px; overflow: hidden;
}}
.r-bar-fill {{
  position: absolute; top: 0; left: 0; height: 100%;
  border-radius: 7px; transition: width 0.5s ease;
}}
.r-bar-marker {{
  position: absolute; top: -3px; width: 3px; height: 20px;
  background: #333; border-radius: 2px;
}}
.r-score {{
  width: 65px; font-size: 13px; font-weight: 600; text-align: center;
  flex-shrink: 0;
}}
.r-detail {{
  font-size: 11px; color: #888; flex: 0 1 280px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}}
.r-legend {{
  display: flex; align-items: center; font-size: 12px; color: #888;
  padding: 4px 0 8px 136px;
}}
.r-leg-dot {{
  display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  margin-right: 4px;
}}
.regime-detail-bar {{
  background: #fff; border-radius: 10px; border: 1px solid #e5e5e5;
  padding: 10px 16px; margin-top: 12px;
  font-size: 12px; color: #666; line-height: 1.6;
  word-break: break-all;
}}
.cal-table {{ width: 100%; border-collapse: collapse; }}
.cal-table th {{ text-align: left; padding: 10px 20px; background: #f8f9fa; color: #555; font-size: 13px; font-weight: 600; }}
.cal-table td {{ text-align: left; padding: 10px 20px; font-size: 14px; }}
.cal-table .date {{ color: #666; white-space: nowrap; }}
.cal-table .sym {{ color: #999; font-size: 12px; }}
.cal-table .note {{ color: #777; font-size: 13px; }}
/* ===== 月历视图 ===== */
.cal-nav {{ display: flex; align-items: center; justify-content: space-between; padding: 12px 20px; }}
.cal-nav-btn {{ background: #f1f3f5; border: 1px solid #e5e5e5; border-radius: 6px; padding: 6px 14px; font-size: 13px; cursor: pointer; color: #555; }}
.cal-nav-btn:hover {{ background: #e9ecef; border-color: #378ADD; color: #378ADD; }}
.cal-title {{ font-size: 15px; font-weight: 600; color: #333; }}
.cal-grid {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; padding: 0 20px 16px; }}
.cal-dow-row {{ border-bottom: 1px solid #f0f0f0; margin-bottom: 6px; }}
.cal-dow {{ font-size: 11px; color: #888; text-align: center; padding: 6px 0; font-weight: 500; }}
.cal-cell {{ min-height: 84px; background: #fff; border: 1px solid #eee; border-radius: 8px; padding: 4px 6px; overflow: hidden; }}
.cal-cell.dim {{ background: #fafafa; opacity: 0.45; }}
.cal-cell.today {{ border: 1px solid #378ADD; box-shadow: inset 0 0 0 1px #378ADD; }}
.cal-day {{ font-size: 12px; font-weight: 600; color: #555; }}
.cal-cell.today .cal-day {{ color: #378ADD; }}
.cal-chip {{ display: block; font-size: 10px; padding: 1px 4px; border-radius: 3px; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #fff; cursor: default; }}
.cal-chip.done {{ opacity: 0.5; }}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>交易看板</h1>
    <div class="time">更新时间: {now_str} (CST) | <span style="color:#888">数据每交易日收盘后自动更新</span></div>
  </div>
  <div style="display:flex;align-items:center;gap:12px">
    <div style="font-size:13px">{status_html}</div>
    <button class="refresh" onclick="shareShot(this)">📸 截图分享</button>
  </div>
</div>

<div class="tabs">
  <button class="tab-btn active" onclick="switchTab('dashboard')">美股看板</button>
  <button class="tab-btn" onclick="switchTab('hk')">港股看板</button>
  <button class="tab-btn" onclick="switchTab('a')">A股看板</button>
  <button class="tab-btn" onclick="switchTab('commodities')">大宗看板</button>
  <button class="tab-btn" onclick="switchTab('regime')">行情判断</button>
  <button class="tab-btn" onclick="switchTab('news')">新闻分析</button>
  <button class="tab-btn" onclick="switchTab('calendar')">日历提醒</button>
  <button class="tab-btn" onclick="switchTab('positions')">持仓</button>
  <button class="tab-btn" onclick="switchTab('binance')">币安</button>
</div>

<div id="tab-dashboard" class="tab-content active">
  <div class="summary summary-col5" id="summary"></div>
  <table>
  <thead>
  <tr>
    <th class="logo-col"></th><th style="width:60px">股票</th><th style="width:60px">名称</th><th style="width:70px">行业</th>
    <th style="width:75px">当前价</th><th style="width:70px">持仓建议</th><th style="width:60px" title="当前估值区间基于的最近财报">最近财报</th><th style="width:65px" title="市盈率">市盈率</th><th style="width:70px" title="扣非加权ROE">ROIC</th><th style="width:80px" title="摊薄EPS同比">摊薄EPS同比</th><th style="width:70px" title="自由现金流利润率">FCF利润率</th><th style="width:95px" title="config 买入区~卖出区">估值区间</th><th style="width:95px">做T区间</th><th style="width:60px">波动率</th>
    <th style="width:75px" title="买入区">Buy</th><th style="width:75px" title="卖出区">Sell</th>
    <th style="width:65px">日布林%</th><th style="width:65px">周布林%</th>
    <th style="width:55px">分位</th><th style="width:150px">区间图</th>
    <th style="width:50px">盈亏比</th><th style="width:60px">潜在亏损</th><th style="width:55px">可交易</th>
  </tr>
  </thead>
  <tbody id="tbody"></tbody>
  </table>
  <div class="advice-box">
    <h3>调仓建议 <span class="ai-tag">DeepSeek V4 Flash</span></h3>
    <div class="advice-content">{advice_text}</div>
  </div>
  {wait_queue_html}
  {deposit_alert_html}
</div>

<div id="tab-regime" class="tab-content">
  <div style="background:#fff;border-radius:12px;border:1px solid #e5e5e5;padding:12px 20px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center">
    <div style="font-size:14px;font-weight:500">美股行情判断</div>
    <div style="font-size:12px;color:#888">更新: {now_str} | 8维度评分体系 | 美股</div>
  </div>
  {regime_html}
  <div style="background:#fff;border-radius:12px;border:1px solid #e5e5e5;padding:12px 20px;margin:24px 0 16px;display:flex;justify-content:space-between;align-items:center">
    <div style="font-size:14px;font-weight:500">A股行情判断</div>
    <div style="font-size:12px;color:#888">更新: {now_str} | 大盘趋势+板块资金 | A股</div>
  </div>
  {cn_regime_html}
</div>

<div id="tab-news" class="tab-content">
  <div style="background:#fff;border-radius:12px;border:1px solid #e5e5e5;padding:12px 20px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center">
    <div style="font-size:14px;font-weight:500">新闻影响分析</div>
    <div style="font-size:12px;color:#888">{news_meta}</div>
  </div>
  {news_content_html}
</div>

<div id="tab-calendar" class="tab-content">
  {calendar_html}
</div>

<div id="tab-hk" class="tab-content">
  <div class="summary" id="hk-summary"></div>
  <div class="legend"></div>
  <table>
  <thead>
  <tr>
    <th class="logo-col"></th><th style="width:60px">股票</th><th style="width:60px">名称</th><th style="width:70px">行业</th>
    <th style="width:75px">当前价</th><th style="width:70px">持仓建议</th><th style="width:60px" title="当前估值区间基于的最近财报">最近财报</th><th style="width:65px" title="市盈率">市盈率</th><th style="width:70px" title="核心ROE">核心ROE</th><th style="width:80px" title="核心净利润同比">核心净利同比</th><th style="width:75px" title="经营现金流/核心净利润">经营现金流/净利</th><th style="width:95px" title="config 买入区~卖出区">估值区间</th><th style="width:95px">做T区间</th><th style="width:60px">波动率</th>
    <th style="width:75px" title="买入区">Buy</th><th style="width:75px" title="卖出区">Sell</th>
    <th style="width:65px">日布林%</th><th style="width:65px">周布林%</th>
    <th style="width:55px">分位</th><th style="width:150px">区间图</th>
    <th style="width:50px">盈亏比</th><th style="width:60px">潜在亏损</th><th style="width:55px">可交易</th>
  </tr>
  </thead>
  <tbody id="hk-tbody"></tbody>
  </table>
  <div class="stock-section" style="margin-top:16px">
    <div class="stock-header"><div class="stock-info"><span class="stock-sym">🕐</span><span class="stock-name">未上市 / 无行情</span><span class="macro-tag">{len(_hk_extra_rows)}</span></div></div>
    <div id="hk-extra"></div>
  </div>
</div>

<div id="tab-a" class="tab-content">
  <div class="summary" id="a-summary"></div>
  <div class="legend"></div>
  <table>
  <thead>
  <tr>
    <th class="logo-col"></th><th style="width:60px">股票</th><th style="width:60px">名称</th><th style="width:70px">行业</th>
    <th style="width:75px">当前价</th><th style="width:70px">持仓建议</th><th style="width:60px" title="当前估值区间基于的最近财报">最近财报</th><th style="width:65px" title="扣非TTM市盈率">扣非TTM市盈率</th><th style="width:80px" title="扣非加权ROE">扣非加权ROE</th><th style="width:90px" title="扣非归母净利润同比">扣非归母净利同比</th><th style="width:60px" title="经营活动现金流/归母净利润">净现比</th><th style="width:95px" title="config 买入区~卖出区">估值区间</th><th style="width:95px">做T区间</th><th style="width:60px">波动率</th>
    <th style="width:75px" title="买入区">Buy</th><th style="width:75px" title="卖出区">Sell</th>
    <th style="width:65px">日布林%</th><th style="width:65px">周布林%</th>
    <th style="width:55px">分位</th><th style="width:150px">区间图</th>
    <th style="width:50px">盈亏比</th><th style="width:60px">潜在亏损</th><th style="width:55px">可交易</th>
  </tr>
  </thead>
  <tbody id="a-tbody"></tbody>
  </table>
</div>

<div id="tab-commodities" class="tab-content">
  <div style="background:#fff;border-radius:12px;border:1px solid #e5e5e5;padding:12px 20px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center">
    <div style="font-size:14px;font-weight:500">大宗看板</div>
    <div style="font-size:12px;color:#888">价格源: Yahoo Finance | 布林: 20期收盘</div>
  </div>
  <div class="legend"><span><span class="dot" style="background:rgba(151,196,89,0.3)"></span> 布林&lt;20% 超卖(绿)</span> <span><span class="dot" style="background:rgba(186,117,23,0.3)"></span> 布林&gt;80% 偏热(橙)</span></div>
  <table>
  <thead>
  <tr>
    <th style="width:80px">名称</th><th style="width:80px">当前价</th>
    <th style="width:70px">Buy2</th><th style="width:70px">Buy3</th><th style="width:70px">Sell1</th><th style="width:70px">Sell2</th>
    <th style="width:70px">日布林%</th><th style="width:70px">周布林%</th><th style="width:70px">月布林%</th>
    <th style="width:55px">分位</th><th style="width:160px">区间图</th><th style="width:60px">状态</th>
  </tr>
  </thead>
  <tbody id="commod-tbody"></tbody>
  </table>
  <div style="margin-top:24px;font-size:18px;font-weight:700;color:#B8860B;border-left:4px solid #B8860B;padding-left:10px">黄金每日晨报</div>
  {brief_html}
  <div style="margin-top:28px;font-size:18px;font-weight:700;color:#1f6feb;border-left:4px solid #1f6feb;padding-left:10px">原油每日晨报</div>
  {oil_html}
</div>

<div id="tab-positions" class="tab-content">
  <div class="pos-header-bar">
    <div style="font-size:14px;font-weight:500">持仓与账户</div>
    <div style="display:flex;align-items:center;gap:12px">
      <span id="pos-update-time" style="font-size:12px;color:#888"></span>
      <button class="refresh" onclick="shareShot(this)" style="padding:4px 12px;font-size:12px">📸 截图</button>
    </div>
  </div>
  <div class="summary" id="pos-summary"></div>
  <table class="pos-table">
  <thead>
  <tr>
    <th>股票</th><th>行业</th><th>方向</th><th>数量</th><th>开仓价</th><th>标记价</th>
    <th>杠杆</th><th>保证金</th><th>维持率</th><th>名义价值</th><th>盈亏</th><th>盈亏%</th><th>强平价</th>
  </tr>
  </thead>
  <tbody id="pos-tbody"></tbody>
  </table>
</div>

<div id="tab-binance" class="tab-content">
  {binance_tab_html}
</div>

<div class="footer">
  数据来源: OKX API + Binance API + config.json | 做T区间: 近10个交易日最高最低价 (manual覆盖优先) | 策略状态每5分钟更新 | <a href="javascript:void(0)" onclick="location.reload(true)">刷新</a>
</div>

<script>
function switchTab(name) {{
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  const btn = Array.from(document.querySelectorAll('.tab-btn')).find(b => (b.getAttribute('onclick')||'').includes("'" + name + "'"));
  if (btn) btn.classList.add('active');
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
}}
window.addEventListener('DOMContentLoaded', () => {{
  const h = (location.hash || '').replace('#', '');
  if (h && document.getElementById('tab-' + h)) {{
    switchTab(h);
    // transform版页面有着陆页遮罩: 带hash打开时自动穿透进入看板
    try {{ if (document.getElementById('dashboard-main').style.display === 'none') enterDashboard(); }} catch(e) {{}}
  }}
}});

const allData = {data_json};
// 主看板: 指数+美股; 港股看板(排除A股看板); 仅纯A股标的进 A股看板
const data = allData.filter(d => !d.is_hk);
const aData = allData.filter(d => d.is_hk && d.tab === 'A股');
const hkData = allData.filter(d => d.is_hk && d.tab !== 'A股');
const LOGO_DOMAINS = {logo_domains_json};
const SELF_LOGOS = {logo_selfhost_json};
const LIVE_REORDER = {REORDER_PCT};
const commodities = {commodities_json};
const WATCH_CARD = {regime_card_json};
const BRIEF_IDX = {brief_idx_json};
{dashboard_js}
// 未上市/无行情标的 (MOONSHOT 等)
const hkExtra = {hk_extra_json};
const hkExtraEl = document.getElementById('hk-extra');
if (hkExtra.length > 0) {{
  let exHtml = '';
  for (const h of hkExtra) {{
    exHtml += `<div style="padding:10px 20px;font-size:13px;border-bottom:1px solid #f0f0f0;display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap">
      <span style="font-weight:600;white-space:nowrap">${{h.name}}</span>
      <span style="font-size:11px;color:#8b949e;white-space:nowrap">${{h.market}} | 评级: ${{h.rating}}</span>
      <span style="color:#777;line-height:1.6;white-space:normal;flex:1;min-width:300px">${{h.note}}</span>
    </div>`;
  }}
  hkExtraEl.innerHTML = exHtml;
}}

// Positions tab
const posData = {pos_data_json};
const accBal = {acc_bal_json};
document.getElementById('pos-update-time').textContent = '更新时间: ' + new Date().toLocaleString('zh-CN');

document.getElementById('pos-summary').innerHTML = `
  <div class="card"><div class="label">总权益</div><div class="value blue">$${{accBal.totalEq.toFixed(2)}}</div></div>
  <div class="card"><div class="label">可用余额</div><div class="value green">$${{accBal.availBal.toFixed(2)}}</div></div>
  <div class="card"><div class="label">已用保证金</div><div class="value">${{accBal.usedMargin.toFixed(2)}}</div></div>
  <div class="card"><div class="label">持仓数</div><div class="value blue">${{posData.length}}</div></div>
`;

// Industry pie chart
const indMap = {{}};
const indStocks = {{}};
let indTotal = 0;
for (const p of posData) {{
  const ind = p.industry || '未知';
  indMap[ind] = (indMap[ind] || 0) + p.margin;
  if (!indStocks[ind]) indStocks[ind] = [];
  indStocks[ind].push(p.sym);
  indTotal += p.margin;
}}
const indEntries = Object.entries(indMap).sort((a,b) => b[1]-a[1]);
const pieColors = ['#378ADD','#3B6D11','#BA7517','#A32D2D','#7B61FF','#00B8A9','#F4511E','#6D4C41','#00838F','#C2185B'];
let svgSlices = '';
let legendHtml = '';
let cumPct = 0;
for (let i = 0; i < indEntries.length; i++) {{
  const [ind, val] = indEntries[i];
  const pct = val / indTotal;
  const startAngle = cumPct * 360;
  const endAngle = (cumPct + pct) * 360;
  const largeArc = pct > 0.5 ? 1 : 0;
  const r = 80, cx = 100, cy = 100;
  const x1 = cx + r * Math.cos((startAngle - 90) * Math.PI / 180);
  const y1 = cy + r * Math.sin((startAngle - 90) * Math.PI / 180);
  const x2 = cx + r * Math.cos((endAngle - 90) * Math.PI / 180);
  const y2 = cy + r * Math.sin((endAngle - 90) * Math.PI / 180);
  if (pct > 0.001) {{
    svgSlices += `<path d="M${{cx}},${{cy}} L${{x1.toFixed(2)}},${{y1.toFixed(2)}} A${{r}},${{r}} 0 ${{largeArc}},1 ${{x2.toFixed(2)}},${{y2.toFixed(2)}} Z" fill="${{pieColors[i % pieColors.length]}}" stroke="#fff" stroke-width="2"/>`;
  }}
  legendHtml += `<div class="pie-legend-item"><span class="pie-dot" style="background:${{pieColors[i % pieColors.length]}}"></span><span class="pie-label">${{ind}} <span style="color:#999;font-size:11px">${{indStocks[ind].join(' ')}}</span></span><span class="pie-pct">${{(pct*100).toFixed(1)}}%</span><span class="pie-val">$${{val.toFixed(2)}}</span></div>`;
  cumPct += pct;
}}
const pieHtml = indEntries.length > 0 ? `
<div class="pie-container">
  <div class="pie-title">行业分布（按保证金/名义占比，全仓为估算值）</div>
  <div class="pie-body">
    <svg viewBox="0 0 200 200" class="pie-svg">${{svgSlices}}<circle cx="${{100}}" cy="${{100}}" r="45" fill="#fff"/><text x="100" y="105" text-anchor="middle" font-size="13" font-weight="600" fill="#333">${{indEntries.length}}行业</text></svg>
    <div class="pie-legend">${{legendHtml}}</div>
  </div>
</div>` : '';
document.getElementById('pos-summary').innerHTML += pieHtml;

const posTbody = document.getElementById('pos-tbody');
let totalPnl = 0, totalMargin = 0;
for (const p of posData) {{
  totalPnl += p.pnl;
  totalMargin += p.margin;
  const pnlClass = p.pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
  const pnlPctClass = p.pnlRatio >= 0 ? 'pnl-pos' : 'pnl-neg';
  // 保证金维持率。OKX返回mgnRatio是原始比值(如4.73=473%)，需×100转为百分比
  // 备用：mgnRatio=0时用保证金/(名义价值/杠杆)*100估算
  let mgnRatio = p.mgnRatio;
  if (mgnRatio > 0) {{
    mgnRatio = mgnRatio * 100;  // ratio to percentage
  }} else if (p.notionalUsd > 0 && p.lever > 0 && !p.margin_is_est) {{
    mgnRatio = p.margin / (p.notionalUsd / p.lever) * 100;
  }}
  const mrStr = mgnRatio > 0 ? mgnRatio.toFixed(0) + '%' : '-';
  const mrColor = mgnRatio > 400 ? '#3B6D11' : mgnRatio > 200 ? '#BA7517' : '#A32D2D';
  posTbody.innerHTML += `<tr>
    <td style="font-weight:600">${{p.sym}}</td>
    <td style="font-size:12px;color:#888">${{p.industry || '-'}}</td>
    <td>多</td>
    <td>${{p.size.toFixed(4)}}</td>
    <td>$${{p.entry.toFixed(2)}}</td>
    <td>$${{p.markPx.toFixed(2)}}</td>
    <td>${{p.lever}}x</td>
    <td>$${{p.margin.toFixed(2)}}${{p.margin_is_est ? '<span style="font-size:10px;color:#aaa">估</span>' : ''}}</td>
    <td style="color:${{mrColor}};font-weight:500">${{mrStr}}</td>
    <td>$${{p.notionalUsd.toFixed(2)}}</td>
    <td class="${{pnlClass}}">${{p.pnl >= 0 ? '+' : ''}}${{p.pnl.toFixed(2)}}</td>
    <td class="${{pnlPctClass}}">${{p.pnlRatio >= 0 ? '+' : ''}}${{p.pnlRatio.toFixed(2)}}%</td>
    <td style="color:#A32D2D;font-size:12px">$${{p.liqPx.toFixed(2)}}</td>
  </tr>`;
}}
if (posData.length > 0) {{
  posTbody.innerHTML += `<tr style="background:#fafaf9;font-weight:600">
    <td>合计</td><td></td><td></td><td></td><td></td><td></td><td></td>
    <td>$${{totalMargin.toFixed(2)}}</td><td></td><td></td>
    <td class="${{totalPnl >= 0 ? 'pnl-pos' : 'pnl-neg'}}">${{totalPnl >= 0 ? '+' : ''}}${{totalPnl.toFixed(2)}}</td>
    <td></td><td></td>
  </tr>`;
}}

// ===== 日历月历视图 (一行一周, 事件放入对应日期格子) =====
const calEvents = {cal_events_json};
const calByDate = {{}};
for (const ev of calEvents) {{
  if (!ev.date || ev.date.indexOf('??') >= 0) continue;
  if (!calByDate[ev.date]) calByDate[ev.date] = [];
  calByDate[ev.date].push(ev);
}}
const calTypeColor = (s) => s === 'FED' ? '#6C5CE7' : s === 'ECON' ? '#00838F' : '#3B5BDB';
const calTypeIcon = (s) => s === 'FED' ? '🏦' : s === 'ECON' ? '📊' : '📅';
const calNow = new Date();
let calY = calNow.getFullYear(), calM = calNow.getMonth();
const calTodayStr = calY + '-' + String(calM + 1).padStart(2, '0') + '-' + String(calNow.getDate()).padStart(2, '0');
function renderCal() {{
  const first = new Date(calY, calM, 1);
  const startDow = (first.getDay() + 6) % 7;
  const daysInMonth = new Date(calY, calM + 1, 0).getDate();
  let html = '';
  for (let i = 0; i < startDow; i++) html += '<div class="cal-cell dim"></div>';
  for (let day = 1; day <= daysInMonth; day++) {{
    const ds = calY + '-' + String(calM + 1).padStart(2, '0') + '-' + String(day).padStart(2, '0');
    const evs = calByDate[ds] || [];
    let chips = '';
    for (const ev of evs) {{
      const tip = ((ev.period || '') + ' | ' + (ev.name || '') + (ev.note ? ' | ' + ev.note : '')).replace(/"/g, '&quot;');
      chips += `<span class="cal-chip ${{ev.status}}" style="background:${{calTypeColor(ev.symbol)}}" title="${{tip}}">${{calTypeIcon(ev.symbol)}} ${{ev.name}}</span>`;
    }}
    html += `<div class="cal-cell${{ds === calTodayStr ? ' today' : ''}}"><div class="cal-day">${{day}}</div>${{chips}}</div>`;
  }}
  const total = startDow + daysInMonth;
  const rem = total % 7;
  if (rem > 0) for (let i = rem; i < 7; i++) html += '<div class="cal-cell dim"></div>';
  document.getElementById('cal-grid').innerHTML = html;
  document.getElementById('cal-title').textContent = calY + '年' + (calM + 1) + '月';
}}
renderCal();
document.getElementById('cal-prev').addEventListener('click', () => {{ calM--; if (calM < 0) {{ calM = 11; calY--; }} renderCal(); }});
document.getElementById('cal-next').addEventListener('click', () => {{ calM++; if (calM > 11) {{ calM = 0; calY++; }} renderCal(); }});

</script>
</body>
</html>
"""

if CLOUD_MODE:
    OUTPUT_F.parent.mkdir(parents=True, exist_ok=True)
with open(OUTPUT_F, "w", encoding="utf-8") as fp:
    fp.write(html)

# 本地模式: 自动调用 transform.py 应用深色主题(与线上 GitHub Pages 一致)。
# 保证无论谁运行 dashboard.py(手动/计划任务), 本地 dashboard.html 始终是深色, 不被浅色覆盖。
if not CLOUD_MODE:
    try:
        import subprocess as _sp
        _tf = Path(r"C:\Users\15949\WorkBuddy\2026-08-04-21-02-03\transform.py")
        if _tf.exists():
            _r = _sp.run([sys.executable, str(_tf), str(OUTPUT_F), str(OUTPUT_F)],
                         capture_output=True, text=True, timeout=120)
            if _r.returncode == 0:
                print("  [theme] 已应用深色主题(与线上一致)")
            else:
                print(f"  [WARN] 深色主题应用失败: {_r.stderr[-300:]}")
            # 同步本地 web/ 镜像(直接打开 web\dashboard.html 时同样为最新内容+深色, 与线上一致)
            _mirror = SCRIPT_DIR / "web" / "dashboard.html"
            if _r.returncode == 0:
                try:
                    import shutil as _sh
                    _mirror.parent.mkdir(parents=True, exist_ok=True)
                    _sh.copyfile(OUTPUT_F, _mirror)   # 先用最新本地版覆盖镜像, 再应用深色
                    _sp.run([sys.executable, str(_tf), str(_mirror), str(_mirror)],
                            capture_output=True, text=True, timeout=120)
                except Exception as _me:
                    print(f"  [WARN] web 镜像同步失败: {_me}")
        else:
            print(f"  [WARN] transform.py 不存在: {_tf}")
    except Exception as _e:
        print(f"  [WARN] 深色主题应用异常: {_e}")

print(f"Dashboard generated: {OUTPUT_F}")
print(f"  Stocks: {len(results)}")
print(f"  Eligible: {sum(1 for r in results if r.get('eligible'))}")

auto_open = "--no-open" not in sys.argv and not CLOUD_MODE
if auto_open:
    webbrowser.open(OUTPUT_F.as_uri())
    print("  Opened in browser")
