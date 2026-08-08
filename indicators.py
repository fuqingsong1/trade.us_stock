#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享技术指标模块：RSI、背离检测、布林带"""
import numpy as np


def calc_rsi(close_series, period=14):
    """Compute RSI for a pandas Series of close prices."""
    delta = close_series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def detect_rsi_divergence(close, rsi, lookback, mode="bear"):
    """Detect RSI divergence.
    mode='bear': price higher high + RSI lower high (top divergence).
    mode='bull': price lower low  + RSI higher low  (bottom divergence).
    """
    if len(close) < lookback or len(rsi) < lookback:
        return False
    recent_close = close.iloc[-lookback:]
    recent_rsi = rsi.iloc[-lookback:]
    # 中点参照: lookback 过小时退化为窗口首点, 避免自我比较
    mid = -lookback // 2 if lookback >= 3 else 0
    # 对照区间(中点之前): lookback < 4 时退化为除最后一点外的所有点, 避免空切片
    if lookback >= 4:
        ref_rsi = recent_rsi.iloc[-lookback // 2:-lookback // 4]
    else:
        ref_rsi = recent_rsi.iloc[:-1]
    if mode == "bear":
        price_hh = recent_close.iloc[-1] > recent_close.iloc[mid]
        rsi_lower = recent_rsi.iloc[-1] < ref_rsi.max()
        return price_hh and rsi_lower and recent_rsi.iloc[-1] > 60
    else:  # bull
        price_ll = recent_close.iloc[-1] < recent_close.iloc[mid]
        rsi_higher = recent_rsi.iloc[-1] > ref_rsi.min()
        return price_ll and rsi_higher and recent_rsi.iloc[-1] < 40


def bollinger_pct(closes, period=20):
    """Compute %B position in Bollinger band from a pandas Series.
    Returns (pct, cur, ma, upper, lower) or None."""
    try:
        if closes is None or len(closes) < max(10, period // 2):
            return None
        cur = float(closes.iloc[-1])
        n = min(len(closes), period)
        use = closes.iloc[-n:]
        ma = float(use.mean())
        std = float(use.std())
        if std <= 0:
            return (0.5, cur, ma, ma, ma)
        upper = ma + 2 * std
        lower = ma - 2 * std
        bp = (cur - lower) / (upper - lower)
        return (bp, cur, ma, upper, lower)
    except Exception:
        return None
