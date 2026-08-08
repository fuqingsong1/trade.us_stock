#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享 LLM 客户端模块：DeepSeek API 统一调用"""
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
        "exclude_stocks": cfg.get("exclude_stocks", []),
    }


LLM_CFG = _load_llm_config()


def call_deepseek(prompt, system_prompt="", temperature=0.3, max_tokens=8192,
                  extract_json=False, max_retries=3, retry_delay=5):
    """Unified DeepSeek API call with retry.

    Args:
        prompt: user message content
        system_prompt: optional system message
        temperature: sampling temperature
        max_tokens: max output tokens
        extract_json: if True, extract and parse JSON from response
        max_retries: max retry attempts
        retry_delay: seconds between retries

    Returns:
        (content, parsed_json) tuple if extract_json=True, else content string.
        Returns (None, None) or None on failure.
    """
    import requests as req
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    api_key = os.getenv("DEEPSEEK_API_KEY", LLM_CFG.get("api_key", ""))
    if not api_key:
        return (None, None) if extract_json else None

    model = os.getenv("NEWS_LLM_MODEL", LLM_CFG.get("model", "deepseek-v4-flash"))
    base_url = os.getenv("NEWS_LLM_BASE_URL", LLM_CFG.get("base_url", "https://api.deepseek.com"))

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    messages = [{"role": "user", "content": prompt}]
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})

    payload = {"model": model, "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens}

    for attempt in range(max_retries):
        try:
            resp = req.post(f"{base_url}/v1/chat/completions",
                           headers=headers, json=payload, timeout=120,
                           proxies=_auto_proxy(base_url), verify=False)
            if resp.status_code != 200:
                err = resp.text[:200] if resp.text else "无详情"
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                return (f"API错误 {resp.status_code}: {err}", None) if extract_json else None

            msg = resp.json()["choices"][0]["message"]
            content = msg.get("content", "").strip()
            if not content:
                content = msg.get("reasoning_content", "").strip()
            if not content:
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                return (None, None) if extract_json else None

            if extract_json:
                jm = re.search(r'\{.*\}', content, re.DOTALL)
                if jm:
                    try:
                        return content, json.loads(jm.group())
                    except json.JSONDecodeError:
                        if attempt < max_retries - 1:
                            time.sleep(retry_delay)
                            continue
                        return content, None
                return content, None
            return content

        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
            return (f"调用失败: {e}", None) if extract_json else f"调用失败: {e}"

    return (None, None) if extract_json else None
