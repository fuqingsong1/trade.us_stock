#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""币安 USDT-M 永续 API 封装 — OKXAPI 兼容适配器

设计目标: 让 strategy_v4.py 的美股腿逻辑能原样跑在币安上, 无需修改策略代码.

关键约定(与 strategy_v4 的 OKXAPI 对齐):
  1. 方法签名完全一致:
     get_instruments / get_ticker / get_candles / get_balance / get_positions /
     get_pending_orders / get_order_history / place_order / set_leverage / cancel_order
  2. 返回体统一为 OKX 风格: 成功 {"code": "0", "data": [...]}, 失败 {"code": "-1", "msg": ...}
  3. instId 使用 OKX 风格 "NVDA-USDT-SWAP" / "XAU-USDT-SWAP" / "BTC-USDT-SWAP",
     内部自动映射币安符号 "NVDAUSDT" / "XAUUSDT" / "BTCUSDT"
  4. 错误语义保持 strategy_v4 约定:
     - _okx_positions 返回 (ok, data), ok=False 表示 API 失败(不能据此认为无仓位)
     - _get_order_history 返回 None(API失败) / {}(查无此单) / dict(明细)
  5. 币安 K线(klines)是旧->新排列, 此处统一翻转为新->旧, 与 OKX candles 语义一致
  6. 双向持仓(hedge)模式: 根据 /fapi/v1/positionSide/dual 自动探测,
     下单自动补 positionSide; 黄金/BTC 腿可显式传 pos_side

真实账户信号: 所有写操作(下单/撤单/改杠杆)仅由策略触发; 本模块只做封装不做交易决策.
"""
import os, json, time, hmac, hashlib
from datetime import datetime, timezone
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from utils import _auto_proxy, _safe_float

DEFAULT_BASE_URL = "https://fapi.binance.com"


def _to_binance_symbol(inst_id):
    """OKX instId -> 币安合约符号: "XAU-USDT-SWAP" -> "XAUUSDT". 已是符号则原样返回."""
    if inst_id.endswith("-USDT-SWAP"):
        return inst_id[:-len("-USDT-SWAP")] + "USDT"
    return inst_id


def _to_okx_inst_id(symbol):
    """币安合约符号 -> OKX instId: "XAUUSDT" -> "XAU-USDT-SWAP"."""
    if symbol.endswith("USDT"):
        return symbol[:-4] + "-USDT-SWAP"
    return symbol


def _map_bar(bar):
    """OKX bar -> 币安 interval."""
    return {
        "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1H": "1h",
        "4H": "4h", "1D": "1d", "1W": "1w", "1M": "1M",
    }.get(bar, "1d")


def _map_order_status(status):
    """币安 status -> OKX state."""
    return {
        "NEW": "live", "PARTIALLY_FILLED": "partially_filled",
        "FILLED": "filled", "CANCELED": "canceled", "EXPIRED": "canceled",
        "NEW_ADL": "live", "NOTIONAL_MATCHED": "live",
    }.get(status, status.lower())


class BinanceAPI:
    def __init__(self, api_key, secret, passphrase="", flag="0",
                 base_url=DEFAULT_BASE_URL, proxy=""):
        self.api_key = api_key
        self.secret = secret
        self.passphrase = passphrase          # 币安无此概念, 占位保持签名兼容
        self.flag = flag                      # 占位, 币安无模拟盘概念
        self.base_url = base_url
        self.proxy = proxy
        self._inst_cache = None               # exchangeInfo 缓存
        self._margin_type_set = set()         # 已设置 CROSS 保证金模式的符号
        self.dual_side = False                # 双向持仓模式(hedge)探测结果
        import requests
        self.session = requests.Session()
        self.session.verify = False
        self.session.timeout = 30
        self.session.trust_env = False
        proxy_for_test = _auto_proxy(base_url)
        if proxy_for_test:
            try:
                test = requests.get(base_url + "/fapi/v1/ping",
                                    proxies=proxy_for_test,
                                    verify=False, timeout=8)
                self.session.proxies = proxy_for_test
                print(f"  [NET] Binance proxy OK: {proxy}")
            except Exception:
                self.session.proxies = proxy_for_test
                print(f"  [NET] Binance proxy unavailable ({proxy}), keep proxy and retry per request")
                print(f"  [NET] Check VPN/network if requests keep failing!")
        else:
            try:
                test = requests.get(base_url + "/fapi/v1/ping",
                                    verify=False, timeout=5)
                print(f"  [NET] Binance direct connection OK")
            except Exception as e:
                print(f"  [NET] Binance direct connection failed: {e}")
                print(f"  [NET] Check VPN/network!")
        self._detect_dual_side()

    # ---------- 基础请求 ----------
    def _ts(self):
        return int(time.time() * 1000)

    def _sign(self, qs):
        mac = hmac.new(self.secret.encode(), qs.encode(), digestmod=hashlib.sha256)
        return mac.hexdigest()

    def request(self, method, path, params=None, body=None, signed=True):
        params = {k: v for k, v in (params or {}).items()
                  if v is not None and v != ""}
        if signed:
            params["timestamp"] = self._ts()
            params["recvWindow"] = 10000
        # 币安签名基于完整 queryString(参数按字母序并非必须, 但签名串必须与实际 URL 完全一致)
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        if signed:
            sign = self._sign(qs)
            url = f"{self.base_url}{path}?{qs}&signature={sign}"
        else:
            url = f"{self.base_url}{path}" + (f"?{qs}" if qs else "")
        headers = {}
        if signed:
            headers["X-MBX-APIKEY"] = self.api_key
        if body:
            headers["Content-Type"] = "application/json"
        try:
            if method == "GET":
                r = self.session.get(url, headers=headers)
            elif method == "POST":
                r = self.session.post(url, data=json.dumps(body) if body else None,
                                      headers=headers)
            elif method == "DELETE":
                r = self.session.delete(url, headers=headers)
            else:
                return {"code": "-1", "msg": f"unsupported method {method}"}
            if r.status_code == 429:
                return {"code": "-1", "msg": "rate limited (429)"}
            if r.status_code == 418:
                return {"code": "-1", "msg": "IP banned (418)"}
            try:
                data = r.json()
            except Exception:
                return {"code": "-1", "msg": f"non-json response: {r.text[:200]}"}
            # 币安错误: {"code": int, "msg": str}; 成功: 裸 dict/list
            if isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] != 0:
                return {"code": "-1", "msg": f"{data['code']}: {data.get('msg', '')}"}
            if isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] == 0:
                return {"code": "0", "data": [data]}
            return {"code": "0", "data": data if isinstance(data, list) else [data]}
        except Exception as e:
            err_type = type(e).__name__
            print(f"[NET ERROR] {err_type}: {e} | {method} {path}")
            return {"code": "-1", "msg": f"{err_type}: {e}"}

    def _detect_dual_side(self):
        """探测账户持仓模式: true=双向持仓(hedge), false=单向持仓(one-way).
        失败时保守默认单向(BOTH), 下单失败会自动降级重试."""
        try:
            r = self.request("GET", "/fapi/v1/positionSide/dual")
            if r.get("code") == "0":
                self.dual_side = bool(r["data"][0].get("dualSidePosition"))
                print(f"  [NET] Binance position mode: {'hedge(双向)' if self.dual_side else 'one-way(单向)'}")
        except Exception:
            self.dual_side = False

    def _infer_position_side(self, side, reduce_only):
        """按策略 net 语义推断币安 positionSide."""
        if not self.dual_side:
            return "BOTH"
        if side == "buy":
            return "LONG"
        return "LONG" if reduce_only else "SHORT"   # 平多/开空

    # ---------- 账户/行情 ----------
    def get_balance(self, ccy="USDT"):
        r = self.request("GET", "/fapi/v2/account")
        if r.get("code") != "0":
            return r
        a = r["data"][0]
        wallet = _safe_float(a.get("totalWalletBalance"))
        upl = _safe_float(a.get("totalUnrealizedProfit"))
        avail = _safe_float(a.get("availableBalance"))
        used = _safe_float(a.get("totalInitialMargin"))
        return {"code": "0", "data": [{
            "totalEq": f"{wallet + upl:.8f}",
            "upl": f"{upl:.8f}",
            "details": [{
                "ccy": "USDT",
                "eq": f"{wallet + upl:.8f}",
                "availEq": f"{avail:.8f}",
                "availBal": f"{avail:.8f}",
                "frozenBal": f"{used:.8f}",
            }],
        }]}

    def get_positions(self, instId=""):
        r = self.request("GET", "/fapi/v2/positionRisk")
        if r.get("code") != "0":
            return r
        # 币安按 (symbol, positionSide) 返回多行; 合并为 OKX net 语义单行(pos 有符号)
        merged = {}
        for p in r["data"]:
            sym = p.get("symbol", "")
            amt = _safe_float(p.get("positionAmt"))
            if abs(amt) < 1e-12:
                continue
            m = merged.setdefault(sym, {
                "positionAmt": 0.0, "entryPrice": 0.0, "markPrice": _safe_float(p.get("markPrice")),
                "unRealizedProfit": 0.0, "leverage": p.get("leverage", "10"),
                "marginType": p.get("marginType", "cross"), "isolatedMargin": 0.0,
                "liquidationPrice": _safe_float(p.get("liquidationPrice")),
                "marginRatio": _safe_float(p.get("marginRatio")),
                "side_amt": 0.0, "side_entry": 0.0,
            })
            m["positionAmt"] += amt
            m["unRealizedProfit"] += _safe_float(p.get("unRealizedProfit"))
            m["isolatedMargin"] += _safe_float(p.get("isolatedMargin"))
            # 记录占主导的一侧作为 entry(多空并存时按净方向取主侧)
            if abs(amt) > abs(m["side_amt"]):
                m["side_amt"] = amt
                m["side_entry"] = _safe_float(p.get("entryPrice"))
        rows = []
        for sym, m in merged.items():
            pos = m["positionAmt"]
            entry = m["side_entry"] if m["side_entry"] else m["entryPrice"]
            notional = abs(pos) * entry if entry else 0.0
            upl = m["unRealizedProfit"]
            rows.append({
                "instId": _to_okx_inst_id(sym),
                "pos": pos,
                "avgPx": entry,
                "lever": int(_safe_float(m.get("leverage")) or 10),
                "upl": upl,
                "margin": m["isolatedMargin"] if m.get("marginType") == "isolated" else 0.0,
                "markPx": m["markPrice"],
                "liqPx": m["liquidationPrice"],
                "notionalUsd": notional,
                "mgnRatio": m["marginRatio"],
                "uplRatio": upl / notional if notional > 0 else 0.0,
            })
        if instId:
            want = _to_binance_symbol(instId)
            rows = [x for x in rows if x["instId"] == instId or x["instId"].replace("-USDT-SWAP", "") + "USDT" == want]
        return {"code": "0", "data": rows}

    def get_account_config(self):
        return {"code": "0", "data": [{}]}

    def set_leverage(self, instId, lever, mgnMode="cross", posSide="net"):
        symbol = _to_binance_symbol(instId)
        r = self.request("POST", "/fapi/v1/leverage",
                         params={"symbol": symbol, "leverage": str(int(lever))})
        if r.get("code") != "0":
            return r
        # 保证金模式(逐仓/全仓)是 per-symbol, 首次设置 CROSS 即可
        if mgnMode == "cross" and symbol not in self._margin_type_set:
            m = self.request("POST", "/fapi/v1/marginType",
                             params={"symbol": symbol, "marginType": "CROSS"})
            if m.get("code") == "0":
                self._margin_type_set.add(symbol)
            # 已是 CROSS 时币安返回 -4046 "No need to change margin type", 忽略
        return {"code": "0", "data": [{}]}

    def add_margin(self, instId, amt, posSide="net", ccy="USDT"):
        """币安全仓(cross)模式不支持单独加保证金, 返回成功占位."""
        return {"code": "0", "data": [{}]}

    def get_ticker(self, instId):
        r = self.request("GET", "/fapi/v1/ticker/price",
                         params={"symbol": _to_binance_symbol(instId)}, signed=False)
        if r.get("code") != "0":
            return r
        return {"code": "0", "data": [{"last": r["data"][0].get("price", "0")}]}

    def get_candles(self, instId, bar="1D", limit=10):
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 10
        r = self.request("GET", "/fapi/v1/klines",
                         params={"symbol": _to_binance_symbol(instId),
                                 "interval": _map_bar(bar),
                                 "limit": str(min(limit, 1000))}, signed=False)
        if r.get("code") != "0":
            return r
        candles = []
        for k in r["data"]:
            candles.append([str(k[0]), str(k[1]), str(k[2]), str(k[3]),
                            str(k[4]), str(k[5]), "0", "0", "0"])
        # 币安 klines 旧->新, OKX candles 新->旧, 翻转保持一致
        candles.reverse()
        return {"code": "0", "data": candles}

    def get_instruments(self, instType="SWAP"):
        if self._inst_cache is not None:
            return self._inst_cache
        r = self.request("GET", "/fapi/v1/exchangeInfo", signed=False)
        if r.get("code") != "0":
            return r
        rows = []
        for s in r["data"][0].get("symbols", []):
            if s.get("quoteAsset") != "USDT":
                continue
            if s.get("contractType") not in ("PERPETUAL", ""):
                continue
            filters = {f.get("filterType"): f for f in s.get("filters", [])}
            tick_sz = filters.get("PRICE_FILTER", {}).get("tickSize", "0.01")
            lot = filters.get("LOT_SIZE", {})
            rows.append({
                "instId": _to_okx_inst_id(s.get("symbol", "")),
                "minSz": lot.get("minQty", "0.001"),
                "lotSz": lot.get("stepSize", "0.001"),
                "tickSz": tick_sz,
                "lever": "125",
                "ctVal": "1",
                "ctValCcy": "USDT",
            })
        self._inst_cache = {"code": "0", "data": rows}
        return self._inst_cache

    # ---------- 交易 ----------
    def place_order(self, instId, side, sz, px="",
                    ordType="limit", tdMode="cross",
                    reduceOnly=False, pos_side=None):
        symbol = _to_binance_symbol(instId)
        otype = "LIMIT" if ordType == "limit" else "MARKET"
        params = {
            "symbol": symbol,
            "side": "BUY" if side == "buy" else "SELL",
            "type": otype,
            "quantity": str(sz),
        }
        if otype == "LIMIT" and px:
            params["price"] = str(px)
            params["timeInForce"] = "GTC"
        if reduceOnly:
            params["reduceOnly"] = "true"
        ps = pos_side or self._infer_position_side(side, reduce_only=bool(reduceOnly))
        params["positionSide"] = ps
        r = self.request("POST", "/fapi/v1/order", params=params)
        # 双向/单向模式不匹配时降级重试一次(探测可能因启动时网络失败而失真)
        if r.get("code") != "0" and any(k in r.get("msg", "") for k in
                                        ("-4131", "-4130", "-4138", "-4044", "positionSide")):
            if ps == "BOTH":
                alt = "LONG" if side == "buy" else ("LONG" if reduceOnly else "SHORT")
            else:
                alt = "BOTH"
            if alt != ps:
                self.dual_side = (ps == "BOTH")  # 被拒的是 BOTH → 双向; 被拒的是 LONG/SHORT → 单向
                print(f"  [WARN] positionSide {ps} rejected, retry with {alt}")
                r = self.request("POST", "/fapi/v1/order", params={**params, "positionSide": alt})
        if r.get("code") != "0":
            return r
        return {"code": "0", "data": [{"ordId": str(r["data"][0].get("orderId", ""))}]}

    def cancel_order(self, instId, ordId):
        r = self.request("DELETE", "/fapi/v1/order",
                         params={"symbol": _to_binance_symbol(instId),
                                 "orderId": str(ordId)})
        if r.get("code") != "0":
            return r
        return {"code": "0", "data": [{}]}

    def get_pending_orders(self, instId=""):
        params = {}
        if instId:
            params["symbol"] = _to_binance_symbol(instId)
        r = self.request("GET", "/fapi/v1/openOrders", params=params)
        if r.get("code") != "0":
            return r
        rows = []
        for o in r["data"]:
            rows.append({
                "ordId": str(o.get("orderId", "")),
                "instId": _to_okx_inst_id(o.get("symbol", "")),
                "side": "buy" if o.get("side") == "BUY" else "sell",
                "px": str(o.get("price", "")),
                "sz": str(o.get("origQty", "")),
                "accFillSz": str(o.get("executedQty", "")),
                "ordType": "limit" if o.get("type") == "LIMIT" else "market",
                "state": _map_order_status(o.get("status", "")),
                "posSide": o.get("positionSide", ""),
            })
        return {"code": "0", "data": rows}

    def get_order_history(self, instId="", limit=100):
        if not instId:
            return {"code": "-1", "msg": "get_order_history requires instId on Binance"}
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 100
        r = self.request("GET", "/fapi/v1/allOrders",
                         params={"symbol": _to_binance_symbol(instId),
                                 "limit": str(min(limit, 1000))})
        if r.get("code") != "0":
            return r
        rows = []
        for o in r["data"]:
            rows.append({
                "ordId": str(o.get("orderId", "")),
                "instId": _to_okx_inst_id(o.get("symbol", "")),
                "side": "buy" if o.get("side") == "BUY" else "sell",
                "px": str(o.get("price", "")),
                "sz": str(o.get("origQty", "")),
                "accFillSz": str(o.get("executedQty", "")),
                "ordType": "limit" if o.get("type") == "LIMIT" else "market",
                "state": _map_order_status(o.get("status", "")),
            })
        return {"code": "0", "data": rows}

    def close(self):
        self.session.close()


if __name__ == "__main__":
    # 只读自测: python api_binance.py
    from utils import PROXY
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    api = BinanceAPI(os.getenv("BINANCE_API_KEY", ""),
                     os.getenv("BINANCE_API_SECRET", ""), proxy=PROXY)
    b = api.get_balance("USDT")
    print("balance:", json.dumps(b, ensure_ascii=False)[:300])
    p = api.get_positions()
    print("positions:", json.dumps(p, ensure_ascii=False)[:300])
    ins = api.get_instruments()
    want = {x["instId"] for x in ins["data"] if x["instId"] in
            ("XAU-USDT-SWAP", "BTC-USDT-SWAP", "TSLA-USDT-SWAP", "MSTR-USDT-SWAP", "NVDA-USDT-SWAP")}
    print("tradfi instruments found:", sorted(want))
    k = api.get_candles("XAU-USDT-SWAP", bar="1D", limit=5)
    print("xau klines:", k.get("data", [])[:2])
    api.close()
