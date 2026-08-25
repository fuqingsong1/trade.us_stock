#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web_shared.py - 云端/本地共享模块 (推送 GitHub, 供 dashboard/news/calendar 使用)

设计:
- CLOUD_MODE=1 (GitHub Actions 美区): 行情走 Yahoo Finance 兜底(OKX 在美区不可达),
  持仓/余额返回空(不暴露账户), 全部直连无需代理。
- 本地模式: 行情/持仓/余额走 OKX 只读接口, 走本机代理。
- 密钥加载: 环境变量优先, 本地 secrets_local.py 兜底(secrets_local.py 不推 GitHub)。
""" 
import os, sys, json, time, hmac, base64
from datetime import datetime, timezone
from pathlib import Path

from utils import PROXY, WORKSPACE_ROOT, SCRIPT_DIR, _auto_proxy, _safe_float

CLOUD_MODE = os.getenv("CLOUD_MODE") == "1"

# ════════════════════════════════════════════════════
# 常量 (与 strategy_v4.py 同步, 供 dashboard 计算使用)
# ════════════════════════════════════════════════════
REORDER_PCT   = 2.0   # Minimum win/loss ratio for eligible stocks
SHORT_ENTRY_MIN = 0.75  # 做空入场最低pct(价格需高于区间75%)
SHORT_TIER_STEP = 0.06  # 做空各档pct间距
SHORT_TP1_PCT   = 0.55  # 第一止盈: 回落到区间55%平60%
SHORT_TP2_PCT   = 0.45  # 第二止盈: 回落到区间45%平剩余
SHORT_REGIME_MAX = 2.0  # 市场regime score < 2.0 才允许做空
SHORT_LEV_TIER = {1: 3, 2: 5, 3: 7}  # 做空各档杠杆(低于做多)

# ════════════════════════════════════════════════════
# 密钥加载: 环境变量 > secrets_local.py (本地文件, 不推 GitHub)
# ════════════════════════════════════════════════════
def load_okx_keys():
    """返回 (api_key, api_secret, passphrase). 云端返回空(不需要OKX密钥)."""
    api_key = os.getenv("OKX_API_KEY", "")
    api_secret = os.getenv("OKX_API_SECRET", "")
    passphrase = os.getenv("OKX_PASSPHRASE", "")
    if not api_key:
        try:
            import secrets_local as sl
            api_key = getattr(sl, "OKX_API_KEY", "")
            api_secret = getattr(sl, "OKX_API_SECRET", "")
            passphrase = getattr(sl, "OKX_PASSPHRASE", "")
        except ImportError:
            pass
    return api_key, api_secret, passphrase


def load_binance_keys():
    """返回 (api_key, api_secret). 云端返回空(不需要币安密钥)."""
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    if not api_key:
        try:
            import secrets_local as sl
            api_key = getattr(sl, "BINANCE_API_KEY", "")
            api_secret = getattr(sl, "BINANCE_API_SECRET", "")
        except ImportError:
            pass
    return api_key, api_secret


# ════════════════════════════════════════════════════
# Yahoo Finance 行情兜底 (CLOUD_MODE=1 使用, 直连无需代理)
# ════════════════════════════════════════════════════
_YF_BAR_MAP = {"1D": "1d", "1W": "1wk", "1M": "1mo", "4H": "1h", "1H": "1h"}


def _yf_sym(inst_id):
    """OKX inst_id -> Yahoo symbol. 如 'NOK-USDT-SWAP' -> 'NOK'."""
    return str(inst_id).replace("-USDT-SWAP", "").replace("-USDT", "")


def _norm_yf_sym(sym):
    """Yahoo 符号规范化: 港股去前导零 (00700.HK -> 0700.HK, 00100.HK -> 100.HK)."""
    s = str(sym).strip()
    if s.endswith(".HK") and len(s) > 4:
        num = s[:-3]
        if num.startswith("0"):
            num = num.lstrip("0") or "0"
            s = num + ".HK"
    return s


def _yahoo_chart(sym, interval, rng):
    """拉取 Yahoo 日K/周K, 返回 OKX candle 格式 [[ts_ms,o,h,l,c,vol,0,0,'1'],...] (旧→新).
    先 requests 直取, 403/异常时用 curl_cffi 浏览器指纹兜底(与 yfinance 同款防反爬).
    trust_env=False 避免 Windows 系统代理干扰分流. 失败返回 []."""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    sym = _norm_yf_sym(sym)  # 港股去前导零
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    params = {"interval": interval, "range": rng}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    proxies = _auto_proxy(url)
    resp = None
    try:
        import requests as req
        s = req.Session(); s.trust_env = False; s.verify = False
        if proxies:
            s.proxies = proxies
        resp = s.get(url, params=params, headers=headers, timeout=20)
        if resp.status_code != 200:
            resp = None
    except Exception:
        resp = None
    if resp is None:
        try:
            from curl_cffi import requests as cr
            resp = cr.get(url, params=params, headers=headers, impersonate="chrome",
                          proxies=proxies or None, timeout=20)
            if resp.status_code != 200:
                return []
        except Exception:
            return []
    res = resp.json()["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    n = len(ts)
    opens = q.get("open") or [None] * n
    highs = q.get("high") or [None] * n
    lows = q.get("low") or [None] * n
    closes = q.get("close") or [None] * n
    vols = q.get("volume") or [None] * n
    rows = []
    for i, t in enumerate(ts):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if o is None or h is None or l is None or c is None:
            continue
        rows.append([int(t) * 1000, float(o), float(h), float(l),
                     float(c), float(vols[i] or 0), 0, 0, "1"])
    return rows  # 旧→新


def _eastmoney_candles(sym, limit=16):
    """东方财富日K兜底(push2his.eastmoney.com, 与 regime 模块同源, 全球可达).
    美股 secid: 105=纳斯达克 106=纽交所 107=美交所, 依次探测. 返回 OKX 顺序(新→旧) rows.
    行格式 [date, open, close, high, low, volume, ...]."""
    import time
    try:
        import requests as req
        for mkt in ("105", "106", "107"):
            try:
                s = req.Session(); s.trust_env = False; s.verify = False
                s.proxies = _auto_proxy("https://push2his.eastmoney.com")
                url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
                       f"?secid={mkt}.{sym}&fields1=f1,f2,f3,f4,f5,f6"
                       f"&fields2=f51,f52,f53,f54,f55,f56,f57"
                       f"&klt=101&fqt=0&end=20500101&lmt={limit}")
                kl = (s.get(url, timeout=12).json().get("data") or {}).get("klines") or []
                if not kl:
                    continue
                rows = []
                for line in reversed(kl):
                    p = line.split(",")
                    if len(p) < 6:
                        continue
                    from datetime import datetime as _dt
                    ts_ms = int(_dt.strptime(p[0], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) * 1000
                    rows.append([ts_ms, float(p[1]), float(p[3]), float(p[4]), float(p[2]),
                                 float(p[5] or 0), 0, 0, "1"])
                return rows
            except Exception:
                time.sleep(0.3)
                continue
    except Exception:
        pass
    return []


# 进程内缓存: 同一轮跑批中 get_candles 会被多次调用(10d区间/波动率/布林), 减少请求量
_CANDLE_CACHE = {}


def _tencent_kline(sym, limit=16):
    """腾讯日K兜底 (web.ifzq.gtimg.cn, 国内可达, 无速率限制).
    支持港股(hk)/A股(sz/sh). 返回 OKX 顺序(旧→新) rows."""
    import time
    try:
        import requests as req
        if sym.endswith(".HK"):
            code = f"hk{sym[:-3]}"
        elif sym.endswith(".SZ"):
            code = f"sz{sym[:-3]}"
        elif sym.endswith(".SS"):
            code = f"sh{sym[:-3]}"
        elif sym.endswith(".KS"):
            code = f"kr{sym[:-3]}"
        elif sym.endswith(".T"):
            code = f"jp{sym[:-2]}"
        else:
            return []
        s = req.Session(); s.trust_env = False
        s.proxies = _auto_proxy("https://web.ifzq.gtimg.cn")
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{limit},qfq"
        resp = s.get(url, timeout=12)
        data = resp.json().get("data", {}).get(code) or {}
        rows_raw = data.get("qfqday") or data.get("day") or []
        from datetime import datetime as _dt
        rows = []
        for p in rows_raw:
            if len(p) < 6:
                continue
            date_str = str(p[0]).replace("-", "")
            try:
                ts_ms = int(_dt.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()) * 1000
            except Exception:
                continue
            rows.append([ts_ms, float(p[1]), float(p[3]), float(p[4]), float(p[2]), float(p[5] or 0), 0, 0, "1"])
        time.sleep(0.2)
        return rows  # 旧→新
    except Exception:
        return []


def yahoo_candles(sym, bar="1D", limit=16):
    """返回 OKX 顺序(新→旧)的 candle rows, 与 OKXAPI.get_candles 兼容.
    Yahoo 优先, 腾讯日K兜底(港股/A股), 东方财富日K再兜底. 结果按 (sym, bar) 缓存."""
    key = (sym, bar)
    if key in _CANDLE_CACHE:
        return _CANDLE_CACHE[key][:limit]
    interval = _YF_BAR_MAP.get(bar, "1d")
    rng = "3mo" if interval == "1d" else ("2y" if interval == "1wk" else "5y")
    try:
        rows = _yahoo_chart(sym, interval, rng)
    except Exception:
        rows = []
    if not rows and interval == "1d":
        rows = _tencent_kline(sym, max(limit, 30))
    if not rows and interval == "1d":
        rows = _eastmoney_candles(sym, max(limit, 30))
    rows.reverse()  # 新→旧
    _CANDLE_CACHE[key] = rows
    return rows[:limit]


def yahoo_price(sym):
    """最新价: Yahoo 优先, 腾讯行情(qt.gtimg.cn, 全球可达)兜底. 失败返回 None."""
    rows = yahoo_candles(sym, "1D", 5)
    if rows:
        return rows[0][4]
    # 腾讯兜底: 美股 v_usXXX, 港股 v_hk00700, A股 v_sz/sh (现价字段index=3)
    try:
        import requests as req
        s = req.Session(); s.trust_env = False
        s.proxies = _auto_proxy("https://qt.gtimg.cn")
        if sym.endswith(".HK"):
            code = f"hk{sym[:-3]}"
        elif sym.endswith(".SZ"):
            code = f"sz{sym[:-3]}"
        elif sym.endswith(".SS"):
            code = f"sh{sym[:-3]}"
        elif sym.endswith(".KS"):
            code = f"kr{sym[:-3]}"
        elif sym.endswith(".T"):
            code = f"jp{sym[:-2]}"
        else:
            code = f"us{sym}"
        r = s.get(f"https://qt.gtimg.cn/q={code}", timeout=10)
        parts = r.text.split("~")
        if len(parts) > 3:
            px = _safe_float(parts[3])
            if px > 0:
                return px
    except Exception:
        pass
    return None


# ════════════════════════════════════════════════════
# OKX 只读 API (本地模式; 云端 CLOUD_MODE=1 时行情走 Yahoo, 持仓/余额返回空)
# ════════════════════════════════════════════════════
class OKXAPI:
    """OKX 只读行情接口, 与 strategy_v4.OKXAPI 的 get_* 方法兼容.
    云端模式: get_ticker/get_candles -> Yahoo; get_positions/get_balance -> 空."""

    def __init__(self, api_key="", secret="", passphrase="", flag="0",
                 base_url="https://www.okx.com", proxy=""):
        self.api_key = api_key
        self.secret = secret
        self.passphrase = passphrase
        self.flag = flag
        self.base_url = base_url
        self.proxy = proxy
        import requests
        self.session = requests.Session()
        self.session.verify = False
        self.session.timeout = 30
        self.session.trust_env = False
        if proxy:
            self.session.proxies = {"https": proxy, "http": proxy}

    # ---- 签名 (本地 OKX 用) ----
    def _ts(self):
        return datetime.now(timezone.utc).isoformat("T", "milliseconds").replace("+00:00", "Z")

    def _sign(self, ts, method, path, body=""):
        msg = ts + method.upper() + path + str(body)
        mac = hmac.new(self.secret.encode(), msg.encode(), digestmod="sha256")
        return base64.b64encode(mac.digest()).decode()

    def _headers(self, ts, sign):
        return {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "x-simulated-trading": self.flag,
        }

    def request(self, method, path, params=None, body=None):
        ts = self._ts()
        rpath = path
        if method == "GET" and params:
            qs = "&".join(f"{k}={v}" for k, v in params.items()
                          if v is not None and v != "")
            if qs:
                rpath = f"{path}?{qs}"
        body_str = json.dumps(body) if body and method == "POST" else ""
        sign = self._sign(ts, method, rpath, body_str)
        headers = self._headers(ts, sign)
        url = self.base_url + rpath
        try:
            if method == "GET":
                r = self.session.get(url, headers=headers)
            elif method == "POST":
                r = self.session.post(url, data=body_str, headers=headers)
            else:
                return {"code": "-1", "msg": f"unsupported method {method}"}
            try:
                return r.json()
            except Exception:
                return {"code": "-1", "msg": f"non-json response: {r.text[:200]}"}
        except Exception as e:
            print(f"[NET ERROR] {type(e).__name__}: {e} | {method} {rpath}")
            return {"code": "-1", "msg": f"{type(e).__name__}: {e}"}

    # ---- 行情: 云端走 Yahoo, 本地走 OKX ----
    def get_ticker(self, inst_id):
        if CLOUD_MODE:
            px = yahoo_price(_yf_sym(inst_id))
            if px is not None:
                return {"code": "0", "data": [{"last": px}]}
            return {"code": "-1", "msg": "yahoo no data"}
        r = self.request("GET", "/api/v5/market/ticker", params={"instId": inst_id})
        if r.get("code") == "0" and r.get("data"):
            v = _safe_float(r["data"][0].get("last", 0))
            if v > 0:
                return r
        return {"code": "-1", "msg": "okx ticker no data"}

    def get_candles(self, inst_id, bar="1D", limit=10):
        if CLOUD_MODE:
            rows = yahoo_candles(_yf_sym(inst_id), bar, limit)
            return {"code": "0", "data": rows}
        return self.request("GET", "/api/v5/market/candles",
                            params={"instId": inst_id, "bar": bar, "limit": str(limit)})

    # ---- 账户: 云端返回空, 本地走 OKX ----
    def get_positions(self, instId=""):
        if CLOUD_MODE:
            return {"code": "0", "data": []}
        params = {"instType": "SWAP"}
        if instId:
            params["instId"] = instId
        return self.request("GET", "/api/v5/account/positions", params=params)

    def get_balance(self, ccy="USDT"):
        if CLOUD_MODE:
            return {"code": "0", "data": []}
        params = {"ccy": ccy} if ccy else {}
        return self.request("GET", "/api/v5/account/balance", params=params)

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass
