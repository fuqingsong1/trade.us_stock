#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享工具模块：代理分流、路径常量、安全转换"""
import os
from pathlib import Path

PROXY = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or "http://127.0.0.1:7890"
# 代理端口7890（Clash Verge mixed-port），如梯子更换端口需同步修改
# NO_PROXY=1 时强制无代理直连(香港/海外服务器部署用, 直连 OKX/币安/Yahoo)

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
               "eastmoney.com", "10jqka.com.cn", "cls.cn", "tushare.pro"}

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
