#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享工具模块：代理分流、路径常量、安全转换"""
import os
from pathlib import Path

def _detect_proxy():
    """自动探测本机可用代理端口: 环境变量优先, 否则从常用端口选第一个可用.
    覆盖 Clash Verge(7890/7897)、飞鸟 FlyingBird(7892)、v2ray(10809) 等.
    NO_PROXY=1 时强制无代理直连(香港/海外服务器部署用, 直连 OKX/币安/Yahoo)."""
    env = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    if env:
        return env
    if os.getenv("NO_PROXY"):
        return ""
    import socket
    for port in (7892, 7890, 7897, 10809, 10808):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.3)
            s.close()
            return f"http://127.0.0.1:{port}"
        except OSError:
            continue
    # 所有代理端口都不可用: 返回空串走直连(避免代理软件未启动时全部请求失败)
    return ""


PROXY = _detect_proxy()
# 注释: 原硬编码 7890(Clash Verge mixed-port), 现自动探测, 更换梯子/端口无需改代码

# 工作区根目录(股票池 watchlist_us、日历等): 本机用 Windows 路径, 服务器(Linux)默认 /opt/trader,
# 均可通过 WORKSPACE_ROOT 环境变量覆盖
if os.name == "nt":
    _default_ws = Path("C:/Users/15949/.qclaw/workspace-agent-639a44f3")
else:
    _default_ws = Path("/opt/trader")
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", str(_default_ws)))
SCRIPT_DIR = Path(__file__).parent

# --- 智能分流：国内直连，国外走代理 ---
_CN_DOMAINS = {"gtimg.cn", "qq.com", "baidu.com", "sina.com.cn",
               "eastmoney.com", "10jqka.com.cn", "cls.cn", "tushare.pro",
               "aliyuncs.com"}  # 阿里云百炼/千问 DashScope: 国内直连

def _is_china_url(url: str) -> bool:
    """判断URL是否属于国内域名（无需代理）。"""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    return any(host.endswith(d) or host == d for d in _CN_DOMAINS)

def _auto_proxy(url: str) -> dict:
    """根据URL自动选择代理配置：NO_PROXY/国内直连，国外走代理。"""
    if os.getenv("NO_PROXY"):
        return {}  # 服务器无代理部署: 全部直连
    if not PROXY:
        return {}  # 未配置代理: 直连
    if _is_china_url(url):
        return {}  # 直连，空dict表示不走代理
    return {"https": PROXY, "http": PROXY}

def _safe_float(v, default=0):
    try:
        return float(v) if v else default
    except (ValueError, TypeError):
        return default
