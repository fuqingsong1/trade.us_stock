#!/usr/bin/env python3
"""OKX USDT-SWAP T-Range Dashboard - 做T区间可视化看板
用法: python dashboard.py [--open]
选项:
  --open    生成后自动打开浏览器 (默认行为)
  --no-open 不自动打开
"""
import os, sys
# Auto-redirect to conda yolo26 env if not already running in it
_YOLO_PY = r"D:\Anaconda\envs\yolo26\python.exe"
if sys.executable.lower() != _YOLO_PY.lower() and os.path.isfile(_YOLO_PY):
    os.execv(_YOLO_PY, [_YOLO_PY] + sys.argv)
import json, math, webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path

from utils import PROXY, WORKSPACE_ROOT, SCRIPT_DIR, _auto_proxy, _safe_float
# 云端(GitHub Actions)与本地共用 web_shared: 密钥隔离 + Yahoo 行情兜底 + 只读 OKXAPI
# 不再 from strategy_v4, 避免云端 import 策略模块(含实盘下单逻辑与硬编码密钥)
from web_shared import (OKXAPI, CLOUD_MODE,
                        REORDER_PCT, SHORT_ENTRY_MIN, SHORT_TIER_STEP,
                        SHORT_TP1_PCT, SHORT_TP2_PCT, SHORT_REGIME_MAX, SHORT_LEV_TIER,
                        load_okx_keys, load_binance_keys)
API_KEY, API_SECRET, PASSPHRASE = load_okx_keys()
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

def get_price(inst_id):
    r = api.get_ticker(inst_id)
    if r.get("code") == "0" and r["data"]:
        v = float(r["data"][0].get("last", 0))
        return v if v > 0 else None
    return None

def _pos_margin_est(p):
    """估算持仓保证金: 逐仓模式 OKX 返回真实 margin; 全仓(cross)模式 margin 为空串 ''
    → 用名义价值(数量×均价)估算, 避免持仓被误判为观察仓. 与 strategy_v4._position_margin_est 一致."""
    m = _safe_float(p.get("margin", 0))
    if m <= 0:
        m = abs(_safe_float(p.get("pos", 0)) * _safe_float(p.get("avgPx", 0)))
    return m

def get_10d_range(inst_id):
    r = api.get_candles(inst_id, bar="1D", limit=16)
    if r.get("code") != "0" or not r.get("data"):
        return None, None
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

def calc_vol(inst_id, high, low):
    """Weekly volatility: 5-day log-return std * sqrt(5), fallback to range width."""
    try:
        r = api.get_candles(inst_id, bar="1D", limit=7)
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

def get_boll_pct(inst_id, current_price, bar="1D", period=20):
    """布林带 %B = (Price - Lower) / (Upper - Lower)
    <0 超卖(下轨下方), >1 超买(上轨上方), 0.5 在中轨
    bar: "1D"=日布林, "1W"=周布林"""
    min_period = max(10, period // 2)
    r = api.get_candles(inst_id, bar=bar, limit=period)
    if r.get("code") != "0" or not r.get("data") or len(r["data"]) < min_period:
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
results = []

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

# ===== End Market Regime =====


for s in stocks:
    sym = s["symbol"]
    buy_price = float(s.get("buy", 0))
    sell_price = float(s.get("sell", 0))
    name = s.get("name", "")
    industry = s.get("industry", "")
    note = s.get("note", "")
    inst_id = inst_map.get(sym, f"{sym}-USDT-SWAP")

    px = get_price(inst_id)
    if not px:
        results.append({"sym": sym, "name": name, "error": "no price"})
        continue

    boll_pct = get_boll_pct(inst_id, px)
    boll_pct_w = get_boll_pct(inst_id, px, bar="1W", period=20)

    manual = t_range.get(sym)
    if manual:
        actual_low = int(manual["low"] / 5) * 5
        actual_high = int(manual["high"]) + (1 if manual["high"] % 1 > 0 else 0)
        src = "manual"
    else:
        ref_high, ref_low = get_10d_range(inst_id)
        if ref_high and ref_low:
            actual_low = int(ref_low / 5) * 5
            actual_high = int(ref_high) + (1 if ref_high % 1 > 0 else 0)
            src = "10d"
        else:
            results.append({"sym": sym, "name": name, "error": "no range"})
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

    vol = calc_vol(inst_id, actual_high, actual_low)
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

    eligible = ratio > REORDER_PCT and loss_rate > -10

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
        "px": px, "alow": actual_low, "ahigh": actual_high,
        "vol": vol, "pct": pct, "src": src, "boll_pct": boll_pct, "boll_pct_w": boll_pct_w,
        "buy1_pct": buy1_pct, "buy2_pct": buy2_pct, "buy3_pct": buy3_pct,
        "sell1_pct": sell1_pct, "sell2_pct": sell2_pct,
        "p_buy1": p_buy1, "p_buy2": p_buy2, "p_buy3": p_buy3,
        "p_sell1": p_sell1, "p_sell2": p_sell2,
        "news_shift_pct": news_shift_pct,

        "ratio": ratio, "loss_rate": loss_rate, "eligible": eligible,
        "zone": zone, "zone_class": zone_class,
        "buy_cfg": buy_price, "sell_cfg": sell_price,
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
    px = get_price(inst_id)
    if not px:
        index_results.append({"sym": sym, "name": idx_info["name"], "error": "no price", "is_index": True})
        continue
    boll_pct = get_boll_pct(inst_id, px)
    boll_pct_w = get_boll_pct(inst_id, px, bar="1W", period=20)
    ref_high, ref_low = get_10d_range(inst_id)
    if ref_high and ref_low:
        actual_low = int(ref_low / 5) * 5
        actual_high = int(ref_high) + (1 if ref_high % 1 > 0 else 0)
        vol = calc_vol(inst_id, actual_high, actual_low)
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
        "has_pos": False, "is_obs": False,
        "pos_size": 0, "pos_entry": 0, "pos_lever": 0, "pos_pnl": 0, "pos_margin": 0,
    })

results = index_results + results

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
    pos_stocks = [r for r in results if r.get("has_pos") and not r.get("is_index")]
    # Collect eligible stocks (no position)
    eli_stocks = [r for r in results if r.get("eligible") and not r.get("has_pos") and not r.get("is_index")]
    # Collect observation stocks
    obs_stocks = [r for r in results if r.get("is_obs") and not r.get("is_index")]
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
        _wq = _st.get("wait_queue", [])
        if _wq:
            _wq_items = "".join(f'<span class="wq-item">⏳ {w["sym"]} <span class="wq-ratio">ratio={w.get("ratio",0)}</span></span>' for w in _wq)
            wait_queue_html = f'<div class="wait-queue-box"><h3>等待队列 <span class="wq-count">{len(_wq)}</span></h3><div class="wq-list">{_wq_items}</div><div class="wq-note">持仓卖出后自动按ratio优先补位</div></div>'
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

for _p, _t in ((SCRIPT_DIR / "strategy_binance_state.json", _bin_state),
               (SCRIPT_DIR / "strategy_binance_boll.json", _bin_boll),
               (SCRIPT_DIR / "script_status_binance.json", _bin_status)):
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
        calendar_html = f'''
<div style="background:#fff;border-radius:12px;border:1px solid #e5e5e5;padding:12px 20px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center">
  <div style="font-size:14px;font-weight:500">财报 / FOMC / 经济数据日历</div>
  <div style="font-size:12px;color:#888">数据更新: {_html.escape(str(_cal.get("updated","-")))} | 共 {len(_evts)} 条</div>
</div>
<div class="summary">
  <div class="card"><div class="label">7天内</div><div class="value red">{_up7}</div></div>
  <div class="card"><div class="label">30天内</div><div class="value" style="color:#BA7517">{_up30}</div></div>
  <div class="card"><div class="label">即将发布</div><div class="value blue">{len(_upcoming)}</div></div>
  <div class="card"><div class="label">待确认日期</div><div class="value" style="color:#185FA5">{len(_pending)}</div></div>
</div>
<div class="stock-section">
  <div class="stock-header"><div class="stock-info"><span class="stock-sym">⏰</span><span class="stock-name">即将发布</span><span class="macro-tag">{len(_upcoming)}</span></div></div>
  {_cal_table(_upcoming, '暂无即将发布的事件')}
</div>
<div class="stock-section">
  <div class="stock-header"><div class="stock-info"><span class="stock-sym">?</span><span class="stock-name">待确认日期</span><span class="macro-tag">{len(_pending)}</span></div></div>
  {_cal_table(_pending, '暂无待确认日期的事件')}
</div>
<div class="stock-section">
  <div class="stock-header"><div class="stock-info"><span class="stock-sym">✓</span><span class="stock-name">已完成</span><span class="macro-tag">{len(_done)}</span></div></div>
  {_cal_table(_done, '暂无已完成事件')}
</div>'''
        print(f"  Calendar tab content loaded ({len(_evts)} events)")
    except Exception as e:
        calendar_html = f'<div class="no-data">日历数据加载失败: {e}</div>'
else:
    calendar_html = '<div class="no-data">日历数据不存在，请先运行 watchlist_us/update_calendar.py</div>'

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

# Build JSON data for embedding
data_json = json.dumps(results, ensure_ascii=False, indent=2)
pos_data_json = json.dumps(all_positions, ensure_ascii=False, indent=2)
acc_bal_json = json.dumps(account_balance, ensure_ascii=False, indent=2)
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

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta http-equiv="refresh" content="300">
<title>OKX 做T看板 + 新闻</title>
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
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>OKX USDT-SWAP 做T看板</h1>
    <div class="time">更新时间: {now_str} (CST) | <span style="color:#888">双击update_all.bat刷新数据</span></div>
  </div>
  <div style="display:flex;align-items:center;gap:12px">
    <div style="font-size:13px">{status_html}</div>
    <button class="refresh" onclick="location.reload(true)">刷新数据</button>
  </div>
</div>

<div class="tabs">
  <button class="tab-btn active" onclick="switchTab('dashboard')">看板</button>
  <button class="tab-btn" onclick="switchTab('regime')">美股行情判断</button>
  <button class="tab-btn" onclick="switchTab('news')">新闻分析</button>
  <button class="tab-btn" onclick="switchTab('calendar')">日历提醒</button>
  <button class="tab-btn" onclick="switchTab('positions')">持仓</button>
  <button class="tab-btn" onclick="switchTab('short')">做空</button>
  <button class="tab-btn" onclick="switchTab('binance')">币安</button>
</div>

<div id="tab-dashboard" class="tab-content active">
  <div class="summary" id="summary"></div>
  <div class="legend">
    <span><span class="dot" style="background:rgba(151,196,89,0.3)"></span> 买入区 (buy1+buy2+buy3)</span>
    <span><span class="dot" style="background:rgba(226,75,74,0.25)"></span> 卖出区 (sell1+sell2)</span>
    <span><span style="display:inline-block;width:2px;height:10px;background:#444"></span> 当前价</span>
    <span>eligible = ratio&gt;{REORDER_PCT} 且 loss_rate&gt;-10%</span>
  </div>
  <table>
  <thead>
  <tr>
    <th style="width:60px">股票</th><th style="width:60px">中文名</th><th style="width:70px">行业</th>
    <th style="width:75px">当前价</th><th style="width:95px">做T区间</th><th style="width:60px">波动率</th>
    <th style="width:75px">Buy1</th><th style="width:75px">Buy2</th><th style="width:75px">Buy3</th><th style="width:75px">Sell1</th><th style="width:75px">Sell2</th>
    <th style="width:65px">日布林%</th><th style="width:65px">周布林%</th>
    <th style="width:55px">分位</th><th style="width:150px">区间图</th><th style="width:60px">状态</th>
    <th style="width:50px">Ratio</th><th style="width:60px">LossRate</th><th style="width:55px">Eligible</th><th style="width:70px" title="策略阶梯杠杆: Buy1/Buy2/Buy3">建议杠杆</th>
  </tr>
  </thead>
  <tbody id="tbody"></tbody>
  </table>
  <div class="advice-box">
    <h3>调仓建议 <span class="ai-tag">DeepSeek V4 Pro</span></h3>
    <div class="advice-content">{advice_text}</div>
  </div>
  {wait_queue_html}
  {deposit_alert_html}
</div>

<div id="tab-regime" class="tab-content">
  <div style="background:#fff;border-radius:12px;border:1px solid #e5e5e5;padding:12px 20px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center">
    <div style="font-size:14px;font-weight:500">美股行情判断</div>
    <div style="font-size:12px;color:#888">更新: {now_str} | 8维度评分体系</div>
  </div>
  {regime_html}
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

<div id="tab-positions" class="tab-content">
  <div class="pos-header-bar">
    <div style="font-size:14px;font-weight:500">持仓与账户</div>
    <div style="display:flex;align-items:center;gap:12px">
      <span id="pos-update-time" style="font-size:12px;color:#888"></span>
      <button class="refresh" onclick="location.reload(true)" style="padding:4px 12px;font-size:12px">刷新</button>
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

<div id="tab-short" class="tab-content">
  <div class="short-rule-box">
    <strong>做空总开关:</strong> 市场 regime 评分 <span id="short-regime-score"></span> (要求 &lt; {SHORT_REGIME_MAX})
    → <span id="short-regime-status"></span><br>
    开空需同时满足: <b>市场偏弱</b> + <b>负面新闻</b> + <b>高位 (pct ≥ 75%)</b>。<br>
    阶梯 short1/2/3 = 75%/81%/87% 分位入场 (杠杆 3x/5x/7x, 仓位 17%/28%/40%); 回落至区间 55% 平 60%、45% 平剩余; 止损 = 入场价 + 5%。<br>
    <span style="color:#888">注: 下方 Short1/2/3 价格为区间分位参考价; 策略实际挂单为 <b>现价×1.003</b> 限价(等反弹后成交), 与看板略有差异。</span>
  </div>
  <div class="summary" id="short-summary"></div>
  <div class="legend">
    <span><span class="dot" style="background:rgba(226,75,74,0.45)"></span> 做空区 (short1/2/3 入场)</span>
    <span><span class="dot" style="background:rgba(151,196,89,0.30)"></span> 止盈区 (TP 55%/45%)</span>
    <span><span style="display:inline-block;width:2px;height:10px;background:#444"></span> 当前价</span>
  </div>
  <table>
  <thead>
  <tr>
    <th style="width:60px">股票</th><th style="width:60px">中文名</th><th style="width:70px">行业</th>
    <th style="width:75px">当前价</th><th style="width:95px">做T区间</th><th style="width:60px">波动率</th>
    <th style="width:75px" title="pct≥75% 入场">Short1</th><th style="width:75px" title="pct≥81%">Short2</th><th style="width:75px" title="pct≥87%">Short3</th>
    <th style="width:75px" title="回落至区间55%平60%">止盈1</th><th style="width:75px" title="回落至区间45%平剩余">止盈2</th>
    <th style="width:55px">分位</th><th style="width:150px">区间图</th><th style="width:90px">状态</th>
    <th style="width:100px">做空条件</th><th style="width:85px">空头持仓</th><th style="width:65px">杠杆</th>
  </tr>
  </thead>
  <tbody id="short-tbody"></tbody>
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
  event.target.classList.add('active');
}}

const data = {data_json};

// Summary cards
const eligible = data.filter(d => d.eligible);
const buyZone = data.filter(d => d.zone && d.zone.startsWith('BUY'));
const sellZone = data.filter(d => d.zone && d.zone.startsWith('SELL'));
document.getElementById('summary').innerHTML = `
  <div class="card"><div class="label">监控股票</div><div class="value blue">${{data.length}}</div></div>
  <div class="card"><div class="label">Eligible (可交易)</div><div class="value green">${{eligible.length}}</div></div>
  <div class="card"><div class="label">买入区</div><div class="value green">${{buyZone.length}}</div></div>
  <div class="card"><div class="label">卖出区</div><div class="value red">${{sellZone.length}}</div></div>
`;

// Table rows
const tbody = document.getElementById('tbody');
for (const d of data) {{
  if (d.error) {{
    tbody.innerHTML += `<tr><td class="sym">${{d.sym}}</td><td colspan="20" style="color:#aaa">${{d.error}}</td></tr>`;
    continue;
  }}
  const pctW = Math.max(0, Math.min(1, d.pct)) * 100;
  const buy1W = d.buy1_pct * 100;
  const buy2W = d.buy2_pct * 100;
  const buy3W = d.buy3_pct * 100;
  const sell1W = d.sell1_pct * 100;
  const sell2W = d.sell2_pct * 100;

  const eligClass = d.eligible ? 'eligible' : 'ineligible';
  const eligText = d.eligible ? 'Y' : 'N';
  const bollColor = d.boll_pct === null ? '#aaa' : d.boll_pct < 0 ? '#3B6D11' : d.boll_pct > 1 ? '#A32D2D' : d.boll_pct > 0.8 ? '#BA7517' : '#2c2c2a';
  const bollText = d.boll_pct === null ? '-' : (d.boll_pct * 100).toFixed(1) + '%';
  const bollWColor = d.boll_pct_w === null ? '#aaa' : d.boll_pct_w < 0 ? '#3B6D11' : d.boll_pct_w > 1 ? '#A32D2D' : d.boll_pct_w > 0.8 ? '#BA7517' : '#2c2c2a';
  const bollWText = d.boll_pct_w === null ? '-' : (d.boll_pct_w * 100).toFixed(1) + '%';
  // eligible 绿色背景仅限买入区, SELL区/上半区的高分位股票不标绿, 避免误导
  const posClass = d.has_pos ? ' has-position' : (d.is_index ? '' : (d.eligible && d.zone && d.zone.startsWith('BUY') ? ' eligible-no-pos' : ''));
  const posBadge = d.has_pos ? `<span class="pos-badge">${{d.pos_lever}}x $${{d.pos_margin.toFixed(1)}}</span>` : (d.is_obs ? `<span class="obs-badge">${{d.pos_lever}}x $${{d.pos_margin.toFixed(2)}}</span>` : '-');
  // 建议杠杆: 跟随策略阶梯杠杆, 按周波动率缩放 (与 strategy_v4._lev_tier_map 一致)
  //   vol≤7.5%: 4/7/10x | 7.5-10%: 4/6/9x | >10%: 3/5/7x (超跌模式 4/5/6x 此处不单独区分)
  const _lv = (d.vol || 0) > 0.10 ? '3/5/7x' : (d.vol || 0) > 0.075 ? '4/6/9x' : '4/7/10x';
  const leverText = (d.eligible || d.has_pos) ? _lv : '-';
  const leverClass = (d.eligible || d.has_pos) ? 'lever-10' : '';
  const newsTag = d.news_shift_pct ? `<span style="font-size:10px;color:${{d.news_shift_pct < 0 ? '#A32D2D' : '#3B6D11'}};margin-left:2px">📰${{(d.news_shift_pct*100).toFixed(0)}}%</span>` : '';

  tbody.innerHTML += `
  <tr class="${{posClass.trim()}}">
    <td class="sym">${{d.sym}}</td>
    <td class="name">${{d.name}}</td>
    <td class="name">${{d.industry}}</td>
    <td class="num" style="font-weight:500">$${{d.px.toFixed(2)}}</td>
    <td class="num" style="font-size:12px;color:#888">$${{d.alow}} - $${{d.ahigh}}</td>
    <td class="num" style="font-size:12px">${{(d.vol*100).toFixed(1)}}%</td>
    <td class="num" style="color:#3B6D11">$${{d.p_buy1.toFixed(2)}}${{newsTag}}</td>
    <td class="num" style="color:#639922">$${{d.p_buy2.toFixed(2)}}${{newsTag}}</td>
    <td class="num" style="color:#97C459">$${{d.p_buy3.toFixed(2)}}${{newsTag}}</td>
    <td class="num" style="color:#BA7517">$${{d.p_sell1.toFixed(2)}}${{newsTag}}</td>
    <td class="num" style="color:#A32D2D">$${{d.p_sell2.toFixed(2)}}${{newsTag}}</td>
    <td class="num" style="color:${{bollColor}};font-weight:500">${{bollText}}</td>
    <td class="num" style="color:${{bollWColor}};font-weight:500">${{bollWText}}</td>
    <td class="num" style="font-weight:500">${{(d.pct*100).toFixed(1)}}%</td>
    <td class="bar-cell">
      <div class="bar-wrap">
        <div class="bar-buy1" style="left:0;width:${{buy1W}}%"></div>
        <div class="bar-buy2" style="left:0;width:${{buy2W}}%"></div>
        <div class="bar-buy3" style="left:0;width:${{buy3W}}%"></div>
        <div class="bar-sell1" style="left:${{sell1W}}%;width:${{sell2W - sell1W}}%"></div>
        <div class="bar-sell2" style="left:${{sell2W}}%;width:${{100 - sell2W}}%"></div>
        <div class="bar-pct" style="left:${{pctW}}%"></div>
        <div class="bar-pct-label" style="left:${{pctW}}%">${{d.zone}}</div>
      </div>
    </td>
    <td class="${{d.zone_class}}" style="font-size:12px">${{d.zone}}</td>
    <td class="num">${{d.ratio}}</td>
    <td class="num" style="color:${{d.loss_rate < -10 ? '#A32D2D' : '#3B6D11'}}">${{d.loss_rate}}%</td>
    <td class="${{eligClass}}">${{eligText}}</td>
    <td class="num ${{leverClass}}" style="font-weight:500">${{leverText}}</td>
    </tr>`;
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

// ===== 做空子页 (Short tab) =====
const shortRegime = {short_regime_json};
const SHORT_MAX = {SHORT_REGIME_MAX};
const shortScore = (shortRegime && shortRegime.score !== undefined) ? shortRegime.score : 99;
const shortAllowed = shortScore < SHORT_MAX;
// 做空档位分位(与 strategy_v4 常量一致): TP2=45% TP1=55% S1=75% S2=81% S3=87%
const TP2 = {SHORT_TP2_PCT} * 100;
const TP1 = {SHORT_TP1_PCT} * 100;
const S1 = {SHORT_ENTRY_MIN} * 100;
const S2 = ({SHORT_ENTRY_MIN} + {SHORT_TIER_STEP}) * 100;
const S3 = ({SHORT_ENTRY_MIN} + 2 * {SHORT_TIER_STEP}) * 100;
const SHORT_LEV_STR = '{short_lev_str}';
const shortScoreColor = shortScore < -3.5 ? '#8B0000' : shortScore < -1.5 ? '#A32D2D' : shortScore < 0.5 ? '#BA7517' : shortScore < 2 ? '#639922' : '#3B6D11';
document.getElementById('short-regime-score').innerHTML =
  `<b style="color:${{shortScoreColor}};font-size:15px">${{shortScore.toFixed(1)}}</b> <span style="color:#888;font-size:11px">(${{(shortRegime && shortRegime.stage) || '未知'}})</span>`;
document.getElementById('short-regime-status').innerHTML = shortAllowed
  ? '<b style="color:#3B6D11">✓ 放行做空</b>'
  : '<b style="color:#A32D2D">✗ 关闭 (市场偏强)</b>';

// 汇总卡片
const shortEligible = data.filter(d => d.short_eligible);
const shortHasPos = data.filter(d => d.short_has_pos);
const shortHigh = data.filter(d => d.short_zone && d.short_zone.startsWith('SHORT'));
const shortTP = data.filter(d => d.short_zone === '止盈区');
document.getElementById('short-summary').innerHTML = `
  <div class="card"><div class="label">监控股票</div><div class="value blue">${{data.length}}</div></div>
  <div class="card"><div class="label">可做空</div><div class="value red">${{shortEligible.length}}</div></div>
  <div class="card"><div class="label">空头持仓</div><div class="value blue">${{shortHasPos.length}}</div></div>
  <div class="card"><div class="label">做空区 (75%+)</div><div class="value red">${{shortHigh.length}}</div></div>
  <div class="card"><div class="label">止盈区</div><div class="value green">${{shortTP.length}}</div></div>
`;

// 做空表格行
const shortTbody = document.getElementById('short-tbody');
for (const d of data) {{
  if (d.error) {{
    shortTbody.innerHTML += `<tr><td class="sym">${{d.sym}}</td><td colspan="17" style="color:#aaa">${{d.error}}</td></tr>`;
    continue;
  }}
  // 指数行(QQQ/SPY)无做空字段 → 只显示基础行情
  const hasShort = d.short1_px !== undefined && d.short1_px !== null;
  const pctW = Math.max(0, Math.min(1, d.pct)) * 100;
  const fmt = v => (v !== undefined && v !== null) ? '$' + v.toFixed(2) : '-';
  const zone = d.short_zone || '-';
  const zcls = d.short_zone_class || 'szone-low';
  // 做空条件: 市场 + 新闻
  let condHtml = '-';
  if (hasShort) {{
    const mk = d.short_ok_market;
    const ns = d.short_news_neg;
    const nv = d.short_news_val;
    const nvTxt = nv ? `<span style="font-size:10px;color:#A32D2D">(${{nv}}%)</span>` : '';
    condHtml = `<span style="color:${{mk ? '#3B6D11' : '#A32D2D'}}">市场${{mk ? '✓' : '✗'}}</span> <span style="color:${{ns ? '#A32D2D' : '#888'}}">新闻${{ns ? '✓' : '✗'}}</span>${{nvTxt}}`;
  }}
  // 空头持仓
  let posHtml = '-';
  if (d.short_has_pos) {{
    const pnlCls = d.short_pos_pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
    posHtml = `<span class="short-badge">空 ${{d.short_pos_lever}}x</span> <span class="${{pnlCls}}">${{d.short_pos_pnl >= 0 ? '+' : ''}}${{d.short_pos_pnl.toFixed(2)}}</span><span style="color:#aaa;font-size:10px"> @${{d.short_pos_entry.toFixed(2)}}</span>`;
  }}
  // 建议杠杆: 与策略做空表一致(3/5/7x)
  const levHtml = (hasShort && (d.short_eligible || d.short_has_pos)) ? `<span class="lever-10">${{SHORT_LEV_STR}}x</span>` : '-';
  shortTbody.innerHTML += `
  <tr class="${{d.short_has_pos ? 'short-pos' : ''}}">
    <td class="sym">${{d.sym}}</td>
    <td class="name">${{d.name}}</td>
    <td class="name">${{d.industry}}</td>
    <td class="num" style="font-weight:500">$${{d.px.toFixed(2)}}</td>
    <td class="num" style="font-size:12px;color:#888">$${{d.alow}} - $${{d.ahigh}}</td>
    <td class="num" style="font-size:12px">${{(d.vol*100).toFixed(1)}}%</td>
    <td class="num" style="color:#E67E22">${{fmt(d.short1_px)}}</td>
    <td class="num" style="color:#C0392B">${{fmt(d.short2_px)}}</td>
    <td class="num" style="color:#A32D2D;font-weight:600">${{fmt(d.short3_px)}}</td>
    <td class="num" style="color:#3B6D11">${{fmt(d.short_tp1)}}</td>
    <td class="num" style="color:#639922">${{fmt(d.short_tp2)}}</td>
    <td class="num" style="font-weight:500">${{(d.pct*100).toFixed(1)}}%</td>
    <td class="bar-cell">
      <div class="bar-wrap">
        <div class="bar-tp" style="left:${{TP2}}%;width:${{TP1 - TP2}}%"></div>
        <div class="bar-short1" style="left:${{S1}}%;width:${{S2 - S1}}%"></div>
        <div class="bar-short2" style="left:${{S2}}%;width:${{S3 - S2}}%"></div>
        <div class="bar-short3" style="left:${{S3}}%;width:${{100 - S3}}%"></div>
        <div class="bar-pct" style="left:${{pctW}}%"></div>
        <div class="bar-pct-label" style="left:${{pctW}}%">${{zone}}</div>
      </div>
    </td>
    <td class="${{zcls}}" style="font-size:12px">${{zone}}</td>
    <td style="font-size:12px">${{condHtml}}</td>
    <td style="font-size:12px">${{posHtml}}</td>
    <td class="num" style="font-weight:500">${{levHtml}}</td>
  </tr>`;
}}

</script>
</body>
</html>
"""

if CLOUD_MODE:
    OUTPUT_F.parent.mkdir(parents=True, exist_ok=True)
with open(OUTPUT_F, "w", encoding="utf-8") as fp:
    fp.write(html)

print(f"Dashboard generated: {OUTPUT_F}")
print(f"  Stocks: {len(results)}")
print(f"  Eligible: {sum(1 for r in results if r.get('eligible'))}")

auto_open = "--no-open" not in sys.argv and not CLOUD_MODE
if auto_open:
    webbrowser.open(OUTPUT_F.as_uri())
    print("  Opened in browser")
