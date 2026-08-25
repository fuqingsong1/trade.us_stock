#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享 LLM 客户端模块：DeepSeek + Kimi 双模型统一调用

用途:
- call_deepseek / call_kimi: 单模型调用 (行情/新闻/财报解读)
- cross_verify: 双模型交叉验证 (财报解读防单一模型失误, 返回各自结论供策略比对)
- 配置来源: 环境变量 > news_config.json
  - DEEPSEEK_API_KEY / KIMI_API_KEY
  - KIMI_BASE_URL (默认 https://api.moonshot.cn)
  - KIMI_MODEL (默认 moonshot-v1-8k, 可用 kimi-k2 等)
"""
import os, json, re, time
from pathlib import Path

from utils import _auto_proxy

_SCRIPT_DIR = Path(__file__).parent
_NEWS_CFG_F = _SCRIPT_DIR / "news_config.json"


def _load_llm_config():
    """Load LLM config from news_config.json, with env var overrides."""
    cfg = {}
    if _NEWS_CFG_F.exists():
        try:
            with open(_NEWS_CFG_F, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
    return {
        "api_key":  os.getenv("DEEPSEEK_API_KEY", cfg.get("api_key", "")),
        "base_url": os.getenv("NEWS_LLM_BASE_URL", cfg.get("base_url", "https://api.deepseek.com")),
        "model":    os.getenv("NEWS_LLM_MODEL", cfg.get("model", "deepseek-v4-flash")),
        # Kimi (双模型交叉验证用, 财报解读)
        "kimi_api_key":  os.getenv("KIMI_API_KEY", cfg.get("kimi_api_key", "")),
        "kimi_base_url": os.getenv("KIMI_BASE_URL", cfg.get("kimi_base_url", "https://api.moonshot.cn")),
        "kimi_model":    os.getenv("KIMI_MODEL", cfg.get("kimi_model", "moonshot-v1-8k")),
        # 千问 (阿里云百炼 DashScope, 双模型交叉验证备选/主用)
        "qwen_api_key":  os.getenv("QWEN_API_KEY", cfg.get("qwen_api_key", "")),
        "qwen_base_url": os.getenv("QWEN_BASE_URL", cfg.get("qwen_base_url", "https://dashscope.aliyuncs.com/compatible-mode")),
        "qwen_model":    os.getenv("QWEN_MODEL", cfg.get("qwen_model", "qwen3.7-flash")),
        "exclude_stocks": cfg.get("exclude_stocks", []),
    }


LLM_CFG = _load_llm_config()


def _chat_completion(base_url, api_key, model, messages, temperature=0.3,
                     max_tokens=8192, max_retries=3, retry_delay=5,
                     extra_payload=None):
    """通用 OpenAI 兼容 chat/completions 调用(带重试).
    成功返回 (content, None); 失败返回 (None, error_msg)."""
    import requests as req
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if not api_key:
        return None, "missing api_key"

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens}
    if extra_payload:
        payload.update(extra_payload)

    for attempt in range(max_retries):
        try:
            proxies = _auto_proxy(base_url)
            # 直连场景(proxies为空)强制 trust_env=False, 绕过 Windows 系统代理设置;
            # 走代理场景 trust_env 无影响(显式 proxies 优先)
            s = req.Session()
            s.trust_env = bool(proxies)
            resp = s.post(f"{base_url}/v1/chat/completions",
                          headers=headers, json=payload, timeout=120,
                          proxies=proxies, verify=False)
            if resp.status_code != 200:
                err = resp.text[:200] if resp.text else "无详情"
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                return None, f"API错误 {resp.status_code}: {err}"
            msg = resp.json()["choices"][0]["message"]
            content = msg.get("content", "").strip()
            if not content:
                content = msg.get("reasoning_content", "").strip()
            if not content:
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                return None, "空响应"
            return content, None
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
            return None, f"调用失败: {e}"
    return None, "retries exhausted"


def call_deepseek(prompt, system_prompt="", temperature=0.3, max_tokens=8192,
                  extract_json=False, max_retries=3, retry_delay=5):
    """DeepSeek API call (兼容旧签名)."""
    messages = [{"role": "user", "content": prompt}]
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    content, err = _chat_completion(
        LLM_CFG["base_url"], LLM_CFG["api_key"], LLM_CFG["model"],
        messages, temperature=temperature, max_tokens=max_tokens,
        max_retries=max_retries, retry_delay=retry_delay)
    if content is None:
        return (err, None) if extract_json else err
    if extract_json:
        jm = re.search(r'\{.*\}', content, re.DOTALL)
        if jm:
            try:
                return content, json.loads(jm.group())
            except json.JSONDecodeError:
                return content, None
        return content, None
    return content


def call_kimi(prompt, system_prompt="", temperature=0.3, max_tokens=8192,
              extract_json=False, max_retries=3, retry_delay=5):
    """Kimi (Moonshot) API call. 未配置 kimi_api_key 时返回 None."""
    if not LLM_CFG["kimi_api_key"]:
        return None
    messages = [{"role": "user", "content": prompt}]
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    content, err = _chat_completion(
        LLM_CFG["kimi_base_url"], LLM_CFG["kimi_api_key"], LLM_CFG["kimi_model"],
        messages, temperature=temperature, max_tokens=max_tokens,
        max_retries=max_retries, retry_delay=retry_delay)
    if content is None:
        return (err, None) if extract_json else None
    if extract_json:
        jm = re.search(r'\{.*\}', content, re.DOTALL)
        if jm:
            try:
                return content, json.loads(jm.group())
            except json.JSONDecodeError:
                return content, None
        return content, None
    return content


def call_qwen(prompt, system_prompt="", temperature=0.3, max_tokens=8192,
              extract_json=False, max_retries=3, retry_delay=5):
    """千问 (阿里云百炼 DashScope) API call. 未配置 qwen_api_key 时返回 None."""
    if not LLM_CFG["qwen_api_key"]:
        return None
    messages = [{"role": "user", "content": prompt}]
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    content, err = _chat_completion(
        LLM_CFG["qwen_base_url"], LLM_CFG["qwen_api_key"], LLM_CFG["qwen_model"],
        messages, temperature=temperature, max_tokens=max_tokens,
        max_retries=max_retries, retry_delay=retry_delay)
    if content is None:
        return (err, None) if extract_json else None
    if extract_json:
        jm = re.search(r'\{.*\}', content, re.DOTALL)
        if jm:
            try:
                return content, json.loads(jm.group())
            except json.JSONDecodeError:
                return content, None
        return content, None
    return content


def cross_verify(prompt, system_prompt="", temperature=0.2, max_tokens=8192):
    """双模型交叉验证: DeepSeek + Kimi 各自独立回答同一 prompt.
    返回 {"deepseek": str|None, "kimi": str|None}.
    未配置 Kimi 时 kimi 为 None, 策略应降级为单模型."""
    out = {"deepseek": None, "kimi": None}
    ds = call_deepseek(prompt, system_prompt=system_prompt,
                       temperature=temperature, max_tokens=max_tokens)
    if ds:
        out["deepseek"] = ds
    km = call_kimi(prompt, system_prompt=system_prompt,
                   temperature=temperature, max_tokens=max_tokens)
    if km:
        out["kimi"] = km
    return out
