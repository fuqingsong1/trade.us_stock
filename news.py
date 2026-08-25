#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
News analysis page for OKX strategy stocks.
Fetches recent news via Google News RSS + yfinance, analyzes impact (rule-based + LLM),
generates news.html with macro news section and earnings reminders.
Usage:
  python news.py              # Generate page (use cache if valid)
  python news.py --refresh    # Force refresh
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
import json, time, re, traceback, hmac, base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils import PROXY, WORKSPACE_ROOT, SCRIPT_DIR, _auto_proxy, _safe_float
from web_shared import CLOUD_MODE, load_okx_keys

REORDER_PCT = 2.0  # from strategy_v4: Minimum win/loss ratio for eligible stocks

# --- Config ---
WATCHLIST_F    = WORKSPACE_ROOT / "watchlist_us" / "config.json"
STATE_F        = SCRIPT_DIR / "strategy_state.json"
CACHE_F        = SCRIPT_DIR / "news_cache.json"

NEWS_CFG_F     = SCRIPT_DIR / "news_config.json"

def _check_proxy(timeout=5):
    """检测代理是否可用。返回 (bool, str)。
    NO_PROXY=1(云端/服务器)时直连可访问国外API, 视为代理可用无需检测。
    本地自动探测常见代理端口(Clash Verge 7890/7897, 飞鸟 7892, v2ray 10809/10808)。"""
    if os.getenv("NO_PROXY"):
        return True, ""
    import socket
    for port in (7892, 7890, 7897, 10809, 10808):
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
            sock.close()
            return True, f"127.0.0.1:{port}"
        except Exception:
            continue
    return False, "无可用代理端口(127.0.0.1:7892/7890/7897/10809/10808)"


def _request_with_retry(method, url, max_retries=3, base_delay=3, **kwargs):
    """通用请求重试：指数退避，适应VPN不稳定。自动分流国内/国外。
    method: 'get' or 'post'
    返回 (resp, error_msg) 二元组，resp 为 None 表示最终失败。
    """
    import requests as _req
    _err = ""
    # 提前提取参数，避免循环内pop导致重试时丢失
    proxy_cfg = kwargs.pop("proxies", _auto_proxy(url))
    verify_val = kwargs.pop("verify", False)
    timeout_val = kwargs.pop("timeout", 30)
    for attempt in range(max_retries + 1):
        try:
            s = _req.Session()
            s.trust_env = False
            s.verify = verify_val
            if proxy_cfg:
                s.proxies = proxy_cfg
            timeout = timeout_val
            # 统一拆分为 (connect, read) 超时，避免无响应请求长时间阻塞
            # connect上限15s：代理节点延迟波动大（2-6s），过短会误杀慢连接
            if isinstance(timeout, (int, float)):
                timeout = (min(timeout, 15), timeout)
            if method == "get":
                resp = s.get(url, timeout=timeout, **kwargs)
            else:
                resp = s.post(url, timeout=timeout, **kwargs)
            return resp, ""
        except Exception as e:
            _err = str(e)
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)  # 3s, 6s, 12s
                proxy_hint = "(代理)" if proxy_cfg else "(直连)"
                print(f"    [RETRY] 请求失败{proxy_hint}(第{attempt+1}次): {_err[:80]}... {delay}s后重试...")
                time.sleep(delay)
            else:
                print(f"    [RETRY] 最终失败{proxy_hint}(已重试{max_retries}次): {_err[:80]}")
    return None, _err

from llm_client import call_deepseek, LLM_CFG

# --- Rule-based keyword dictionaries ---
POSITIVE_KW = {
    "upgrade": 3, "buy rating": 4, "overweight": 3, "raise price target": 3,
    "price target raise": 3, "bullish": 2, "strong buy": 5, "outperform": 3,
    "FDA approval": 8, "approved": 4, "breakthrough": 5, "beat earnings": 5,
    "earnings beat": 5, "revenue surge": 4, "record revenue": 4, "top line beat": 4,
    "dividend increase": 2, "buyback": 3, "share buyback": 3, "share repurchase": 3,
    "contract win": 3, "partnership": 2, "strategic partnership": 3,
    "new deal": 3, "expand": 2, "growth": 2, "soar": 4, "surge": 4,
    "rally": 3, "jump": 3, "all-time high": 3, "52-week high": 2,
    "double down": 2, "best-in-class": 2,
}

NEGATIVE_KW = {
    "downgrade": -3, "sell rating": -4, "underweight": -3, "cut price target": -3,
    "price target cut": -3, "bearish": -2, "strong sell": -5, "underperform": -3,
    "FDA rejection": -8, "rejected": -4, "recall": -5, "lawsuit": -3,
    "antitrust": -4, "fine": -3, "penalty": -3, "earnings miss": -5,
    "revenue decline": -4, "revenue miss": -4, "layoff": -3, "layoffs": -3,
    "CEO resignation": -4, "data breach": -5, "fraud": -6,
    "debt": -2, "default": -5, "bankruptcy": -8,
    "stock sale": -3, "dilution": -3, "secondary offering": -4,
    "dive": -4, "plunge": -5, "crash": -6, "tumble": -4, "sink": -3,
    "slump": -3, "drop": -2, "fall": -2, "decline": -2,
    "52-week low": -2, "short": -1, "warning": -2,
}

MACRO_KW = ["Fed", "interest rate", "CPI", "jobs report", "nonfarm", "non-farm",
            "GDP", "tariff", "trade war", "inflation", "recession",
            "employment", "unemployment", "monetary policy", "quantitative easing",
            "rate hike", "rate cut", "treasury yield", "bond yield",
            "PMI", "manufacturing index", "consumer confidence", "retail sales",
            "FOMC", "federal reserve", "nonfarm payroll", "non-farm payroll",
            "jobs data", "ADP employment", "JOLTS", "initial claims"]

INDUSTRY_KW = ["chip", "semiconductor", "AI ", "artificial intelligence",
               "cloud computing", "SaaS", "software", "pharma", "biotech",
               "GLP-1", "weight-loss drug", "social media", "streaming",
               "e-commerce", "fabless", "foundry", "data center",
               "generative AI", "LLM", "large language model"]

# Authority ranking for news sources (higher = more authoritative)
AUTHORITY_RANK = {
    "Reuters": 100, "AP": 98, "Associated Press": 98,
    "Bloomberg": 95, "CNBC": 90, "Financial Times": 88, "Wall Street Journal": 88,
    "The Wall Street Journal": 88, "WSJ": 88,
    "MarketWatch": 80, "Barron's": 78, "Investor's Business Daily": 76,
    "Forbes": 74, "Business Insider": 72, "Yahoo Finance": 70,
    "Seeking Alpha": 68, "Motley Fool": 66, "Investopedia": 64,
    "TechCrunch": 60, "The Verge": 58, "Ars Technica": 56,
    "CNN Business": 54, "BBC": 52, "New York Times": 50, "NYTimes": 50,
    "Washington Post": 50, "Guardian": 48,
    "Google News": 30, "MSN": 28, "Apple News": 26,
}

NEWS_TYPE_WEIGHT = {
    "earnings": 1.5, "FDA": 1.5, "regulatory": 1.5,
    "merger": 1.4, "acquisition": 1.4, "M&A": 1.4,
    "management": 1.3, "CEO": 1.3, "CFO": 1.3,
    "guidance": 1.3, "outlook": 1.3, "forecast": 1.3,
    "macro": 1.2, "Fed": 1.2, "CPI": 1.2, "jobs": 1.2,
    "upgrade": 1.1, "downgrade": 1.1, "price target": 1.1,
    "industry": 1.0, "sector": 1.0,
    "lawsuit": 1.0, "legal": 1.0,
    "opinion": 0.6, "commentary": 0.6, "editorial": 0.6,
}

def _get_news_type_weight(title, summary=""):
    """Get news type weight based on keywords in title/summary."""
    text = (title + " " + summary).lower()
    best_weight = 1.0
    for kw, w in NEWS_TYPE_WEIGHT.items():
        if kw.lower() in text:
            best_weight = max(best_weight, w)
    return best_weight

def _get_time_decay(pub_date_str):
    """Get time decay factor. Today=1.0, 1d=0.8, 2d=0.6, 3d=0.4, 5d+=0.2."""
    if not pub_date_str:
        return 0.6
    try:
        pub = datetime.strptime(pub_date_str[:10], "%Y-%m-%d")
        days_ago = (datetime.now() - pub).days
        if days_ago <= 0: return 1.0
        if days_ago == 1: return 0.8
        if days_ago == 2: return 0.6
        if days_ago == 3: return 0.4
        if days_ago <= 4: return 0.3
        return 0.2
    except:
        return 0.6

# Macro news search queries
MACRO_QUERIES = [
    "Fed+interest+rate+OR+FOMC+OR+rate+hike+OR+rate+cut",
    "nonfarm+OR+jobs+report+OR+employment+OR+unemployment",
    "CPI+OR+inflation+OR+treasury+yield+OR+bond+yield",
]


# ============================================================
# 1. News fetching (Google News RSS + yfinance fallback)
# ============================================================
def _fetch_news_google_rss(query, max_items=5, days=7):
    """Fetch news via Google News RSS. No rate limits, no API key needed."""
    from xml.etree import ElementTree

    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

    resp, err = _request_with_retry("get", url, timeout=15)
    if resp is None or resp.status_code != 200:
        if resp is None:
            print(f"  [WARN] Google News RSS failed for query '{query}': {err}")
        return []
    try:
        root = ElementTree.fromstring(resp.text)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        items = []

        for item in root.findall('.//item')[:max_items * 2]:
            title = item.find('title')
            link = item.find('link')
            pub_date = item.find('pubDate')
            source = item.find('source')

            if title is None or not title.text:
                continue

            pub_date_str = ""
            if pub_date is not None and pub_date.text:
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(pub_date.text)
                    if dt < cutoff:
                        continue
                    pub_date_str = dt.strftime("%Y-%m-%d")
                except:
                    pub_date_str = pub_date.text[:10] if len(pub_date.text) >= 10 else ""

            items.append({
                "title": title.text,
                "summary": "",
                "pubDate": pub_date_str,
                "provider": source.text if source is not None else "Google News",
                "url": link.text if link is not None else "",
            })

            if len(items) >= max_items:
                break

        if not items:
            print(f"  [WARN] Google RSS returned 0 items for '{query[:40]}' (status={resp.status_code})")
        return items
    except Exception as e:
        print(f"  [WARN] Google News RSS failed for query '{query[:30]}': {e}")
        return []


def _fetch_news_yfinance(symbol, max_items=5, days=7):
    """Fetch news via yfinance (fallback, rate limited)."""
    import yfinance as yf
    import requests

    session = requests.Session()
    session.trust_env = False  # 禁用系统代理，避免https://格式冲突
    # yfinance 走代理（Yahoo Finance 是国外域名）
    session.proxies = {"https": PROXY, "http": PROXY}
    session.verify = False

    # yfinance retry with backoff
    all_news = []
    for yf_attempt in range(3):
        try:
            if yf_attempt > 0:
                delay = 5 * (2 ** yf_attempt)
                print(f"    [RETRY] yfinance {symbol} 第{yf_attempt+1}次... {delay}s后重试")
                time.sleep(delay)
            else:
                time.sleep(5)  # Initial delay to avoid rate limiting
            ticker = yf.Ticker(symbol, session=session)
            all_news = ticker.news
            break
        except Exception as e:
            if yf_attempt < 2:
                print(f"    [RETRY] yfinance {symbol} failed: {e}")
            else:
                print(f"  [WARN] yfinance news failed for {symbol}: {e}")
                return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = []
    for n in all_news:
        content = n.get("content", {})
        pub_date_str = content.get("pubDate", "")
        if pub_date_str:
            try:
                pub_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
                if pub_date < cutoff:
                    continue
            except:
                pass

        title = content.get("title", "")
        if not title:
            continue

        recent.append({
            "title": title,
            "summary": content.get("summary", ""),
            "pubDate": pub_date_str[:10] if pub_date_str else "",
            "provider": content.get("provider", {}).get("displayName", ""),
            "url": (content.get("clickThroughUrl", {}).get("url", "")
                    or content.get("canonicalUrl", {}).get("url", "")),
        })
        if len(recent) >= max_items:
            break

    return recent


RSS_ALIASES = {
    "TSM": "TSMC", "AVGO": "Broadcom", "BRK.B": "Berkshire Hathaway",
    "META": "Meta Platforms", "GOOGL": "Google Alphabet",
    "AMZN": "Amazon", "MSFT": "Microsoft", "AAPL": "Apple",
    "NVDA": "Nvidia", "AMD": "AMD", "INTC": "Intel",
    "QCOM": "Qualcomm", "CRM": "Salesforce", "NFLX": "Netflix",
    "TSLA": "Tesla", "UBER": "Uber", "COIN": "Coinbase",
    # 港股/A股/ADR (Google News 用中文名搜索更准确)
    "00700.HK": "腾讯控股 Tencent", "09988.HK": "阿里巴巴 Alibaba", "03690.HK": "美团 Meituan",
    "01810.HK": "小米 Xiaomi", "01024.HK": "快手 Kuaishou", "09992.HK": "泡泡玛特 Pop Mart",
    "02513.HK": "智谱 Zhipu", "00100.HK": "MiniMax 稀宇科技",
    "300308.SZ": "中际旭创", "603986.SS": "兆易创新", "688836.SS": "宇树科技", "688825.SS": "长鑫科技",
    "PDD": "PDD Holdings",
}


def fetch_stock_news(symbol, max_items=5, days=7):
    """Fetch recent news for a stock. Try Google News RSS first, yfinance fallback."""
    search_name = RSS_ALIASES.get(symbol, symbol)
    query = f"{search_name}+stock+OR+share"
    items = _fetch_news_google_rss(query, max_items, days)
    if items:
        print(f"    Got {len(items)} news from Google RSS for {symbol}")
        return items

    if search_name != symbol:
        query2 = f"{symbol}+stock+OR+share"
        items = _fetch_news_google_rss(query2, max_items, days)
        if items:
            print(f"    Got {len(items)} news from Google RSS (ticker) for {symbol}")
            return items

    time.sleep(5)
    items = _fetch_news_yfinance(symbol, max_items, days)
    if items:
        print(f"    Got {len(items)} news from yfinance for {symbol}")
    else:
        print(f"    No news found for {symbol}")
    return items


def fetch_macro_news(max_items=3, days=7):
    """Fetch macro news (Fed, jobs, CPI, etc.) from Google News RSS."""
    all_items = []
    seen_titles = set()
    for query in MACRO_QUERIES:
        items = _fetch_news_google_rss(query, max_items=max_items, days=days)
        for item in items:
            # Dedup by title
            title_key = item["title"][:50].lower()
            if title_key not in seen_titles:
                seen_titles.add(title_key)
                all_items.append(item)
        time.sleep(0.5)  # Be nice to Google

    print(f"  Got {len(all_items)} macro news items")
    return all_items[:8]  # Cap at 8


# ============================================================
# 2. Rule-based analysis
# ============================================================
def rule_based_analysis(title, summary=""):
    """Analyze news impact using keyword matching.
    Returns (direction, impact_pct, confidence, news_type).
    """
    text = (title + " " + summary).lower()

    news_type = "个股"
    for kw in MACRO_KW:
        if kw.lower() in text:
            news_type = "宏观"
            break
    else:
        for kw in INDUSTRY_KW:
            if kw.lower() in text:
                news_type = "行业"
                break

    best_pos = 0
    best_neg = 0
    matched_kw = []

    for kw, impact in POSITIVE_KW.items():
        if kw.lower() in text:
            best_pos = max(best_pos, impact)
            matched_kw.append(kw)

    for kw, impact in NEGATIVE_KW.items():
        if kw.lower() in text:
            best_neg = min(best_neg, impact)
            matched_kw.append(kw)

    if best_pos > 0 and best_neg < 0:
        return "neutral", 0, "low", news_type
    elif best_pos > 0:
        return "positive", best_pos, "high" if best_pos >= 3 else "medium", news_type
    elif best_neg < 0:
        return "negative", best_neg, "high" if abs(best_neg) >= 3 else "medium", news_type
    else:
        return "neutral", 0, "low", news_type


# ============================================================
# 2b. News deduplication
# ============================================================
def _normalize_title(title):
    """Normalize title for similarity comparison."""
    # Remove common prefixes, lowercase, strip punctuation
    t = title.lower().strip()
    # Remove stock ticker patterns like (NYSE:XXX), (NASDAQ:XXX)
    t = re.sub(r'\(nyse:\w+\)', '', t)
    t = re.sub(r'\(nasdaq:\w+\)', '', t)
    # Remove trailing punctuation
    t = re.sub(r'[\s\-_|:]+$', '', t)
    return t.strip()


def _title_similarity(t1, t2):
    """Check if two titles refer to the same event (0-1 similarity).
    Uses word overlap since we don't have NLP libraries.
    """
    n1, n2 = _normalize_title(t1), _normalize_title(t2)
    if n1 == n2:
        return 1.0
    words1 = set(n1.split())
    words2 = set(n2.split())
    if not words1 or not words2:
        return 0.0
    # Jaccard similarity
    intersection = words1 & words2
    union = words1 | words2
    return len(intersection) / len(union)


def _get_authority(provider):
    """Get authority score for a news source."""
    if not provider:
        return 20
    for name, score in AUTHORITY_RANK.items():
        if name.lower() in provider.lower():
            return score
    return 40  # Default for unknown sources (above aggregators)


def _extract_keywords(title):
    """Extract meaningful keywords from a title (remove stop words, stock tickers, generic terms)."""
    stop_words = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
                  "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
                  "being", "have", "has", "had", "do", "does", "did", "will", "would",
                  "could", "should", "may", "might", "shall", "can", "it", "its",
                  "this", "that", "these", "those", "not", "no", "nor", "so", "if",
                  "as", "up", "out", "how", "what", "which", "who", "when", "where",
                  "why", "all", "each", "every", "both", "few", "more", "most",
                  "other", "some", "such", "than", "too", "very", "just", "about",
                  "above", "after", "again", "also", "any", "because", "before",
                  "between", "during", "here", "into", "over", "then", "through",
                  "under", "until", "while", "still", "new", "say", "said", "says",
                  "report", "reports", "according", "based", "near", "nearby",
                  "stock", "stocks", "share", "shares", "price", "prices",
                  "company", "companies", "firm", "firms", "inc", "corp", "ltd",
                  "year", "years", "week", "month", "day", "time", "percent",
                  "chip", "chips", "semiconductor", "semiconductors", "tech",
                  "technology", "ai", "earnings", "revenue", "profit", "loss",
                  "quarter", "fiscal", "guidance", "forecast", "estimate",
                  "analyst", "upgrade", "downgrade", "buy", "sell", "hold",
                  "target", "rating", "trade", "trading", "market", "markets",
                  "investor", "investors", "fund", "portfolio", "index",
                  "growth", "decline", "rise", "fall", "gain", "drop",
                  "high", "low", "record", "beat", "miss", "expect", "expected",
                  "billion", "million", "reuters", "bloomberg", "cnbc", "yahoo",
                  "seeking", "alpha", "motley", "fool", "barrons", "investopedia"}
    t = re.sub(r'\(nyse:\w+\)', '', title.lower())
    t = re.sub(r'\(nasdaq:\w+\)', '', t)
    t = re.sub(r'\s*[-–—]\s*\w+(\s+\w+)?$', '', t)
    words = set(re.findall(r'[a-z]+', t))
    return words - stop_words


def _is_same_event(title1, title2):
    """Check if two titles refer to the same event using keyword overlap.
    More robust than pure Jaccard - focuses on meaningful keywords.
    """
    kw1 = _extract_keywords(title1)
    kw2 = _extract_keywords(title2)
    if not kw1 or not kw2:
        return False
    intersection = kw1 & kw2
    min_size = min(len(kw1), len(kw2))
    overlap_ratio = len(intersection) / min_size
    return overlap_ratio > 0.85 and len(intersection) >= 5


def llm_dedup(news_list):
    """Use LLM to identify which news refer to the same event.
    Returns list of indices to keep (one per event group).
    """
    if not LLM_CFG["api_key"] or len(news_list) <= 1:
        return list(range(len(news_list)))

    import requests as req

    titles_text = ""
    for i, n in enumerate(news_list, 1):
        titles_text += f"\n{i}. [{n.get('pubDate','')}] {n['title']}"

    prompt = f"""以下{len(news_list)}条新闻来自同一只股票/主题。判断哪些是同一事件的不同报道（应去重），哪些是不同事件（应保留）。

返回JSON: {{"keep": [保留的编号列表], "groups": {{"1": [同组编号], ...}}}}
- keep: 每组只保留1条（选最权威/信息最全的），编号从1开始
- groups: 标注哪些编号是同一事件
- 不同事件必须分别保留

新闻:{titles_text}

只返回JSON，不要其他文字。"""

    try:
        headers = {
            "Authorization": f"Bearer {LLM_CFG['api_key']}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": LLM_CFG["model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 500,
        }
        resp, _l_err = _request_with_retry("post",
            f"{LLM_CFG['base_url']}/v1/chat/completions",
            headers=headers, json=payload, timeout=30, max_retries=3, base_delay=3)
        if resp is None:
            print(f"  [WARN] LLM去重失败(已重试): {_l_err}")
            return list(range(len(news_list)))
        if resp.status_code == 200:
            msg = resp.json()["choices"][0]["message"]
            content = msg.get("content", "") or msg.get("reasoning_content", "")
            if content:
                json_match = re.search(r'\{.*\}', content, re.DOTALL)
                if json_match:
                    result = json.loads(json_match.group())
                    keep_indices = result.get("keep", [])
                    if keep_indices and all(1 <= k <= len(news_list) for k in keep_indices):
                        return [k - 1 for k in keep_indices]
    except Exception as e:
        print(f"  [WARN] LLM去重失败: {e}")
    return list(range(len(news_list)))


def deduplicate_news(news_list):
    """Remove duplicate news about the same event, keeping the most authoritative source.
    
    Two news items are considered duplicates if:
    1. Their titles have high keyword overlap (same event, different sources), OR
    2. Their title_cn (LLM-translated) are very similar
    
    Among duplicates, keep the one with the highest authority source.
    If sources have similar authority, prefer the one with LLM analysis.
    """
    if len(news_list) <= 1:
        return news_list
    
    # Group duplicates
    groups = []  # Each group: list of indices
    assigned = set()
    
    for i in range(len(news_list)):
        if i in assigned:
            continue
        group = [i]
        assigned.add(i)
        ni = news_list[i]
        
        for j in range(i + 1, len(news_list)):
            if j in assigned:
                continue
            nj = news_list[j]
            
            # Method 1: keyword-based event matching
            same_event = _is_same_event(ni.get("title", ""), nj.get("title", ""))
            # Method 2: Chinese title similarity (if LLM analyzed)
            cn_sim = _title_similarity(ni.get("title_cn", ""), nj.get("title_cn", ""))
            
            if same_event or cn_sim > 0.85:
                group.append(j)
                assigned.add(j)
        
        groups.append(group)
    
    # Pick best from each group
    result = []
    removed = 0
    for group in groups:
        if len(group) == 1:
            result.append(news_list[group[0]])
            continue
        
        # Sort by: LLM source > rule source, then authority, then impact magnitude
        def sort_key(idx):
            n = news_list[idx]
            auth = _get_authority(n.get("provider", ""))
            has_llm = 1 if n.get("source") == "llm" else 0
            impact = abs(n.get("impact_pct", 0))
            return (-has_llm, -auth, -impact)
        
        group.sort(key=sort_key)
        best = news_list[group[0]]
        
        # Merge: keep provider list from removed items
        other_providers = [news_list[idx].get("provider", "") for idx in group[1:]
                          if news_list[idx].get("provider")]
        if other_providers:
            best["other_sources"] = other_providers
        
        result.append(best)
        removed += len(group) - 1
    
    if removed > 0:
        print(f"    去重: {len(news_list)} -> {len(result)} (去除{removed}条重复)")
    
    return result


# ============================================================
# 3. LLM analysis (DeepSeek)
# ============================================================
def llm_analyze_batch(symbol, news_list, is_macro=False):
    """Send a batch of news to LLM for translation + impact analysis.
    Returns list of dicts with title_cn, direction, impact_pct, reason, type.
    Returns None if LLM is not available.
    """
    if not LLM_CFG["api_key"]:
        return None

    import requests as req

    news_text = ""
    for i, n in enumerate(news_list, 1):
        summary_part = f"\n   摘要: {n['summary']}" if n.get("summary") else ""
        news_text += f"\n{i}. [{n['pubDate']}] {n['title']}{summary_part}\n"

    if is_macro:
        context = "宏观市场新闻（美联储、就业、通胀等）"
    else:
        context = f"{symbol}相关美股新闻"

    prompt = f"""分析以下{context}，对每条返回JSON数组。

每条返回:
- "title_cn": 中文标题（简洁翻译，不超过30字）
- "direction": positive / negative / neutral
- "impact_pct": 预估对美股市场影响百分比（整数，如+3或-2，neutral为0）
- "reason": 一句话中文原因（20字以内）
- "summary": 一句话中文结论/观点摘要（30字以内，如"增发稀释股权，短期利空"）
- "type": 宏观 / 行业 / 个股

【关键要求】
1. reason必须解释根本原因，不要只重述股价变动事实。
   例如：❌"股价下跌" "市场抛售" ✅"AI芯片需求不及预期导致收入前景下调" "非农数据强于预期推迟降息"
2. summary是给交易者看的快速结论，要包含操作暗示（如"短期承压观望" "利好已price in"）
3. 宏观新闻的impact_pct是对美股大盘整体的影响，不是对个股的影响
4. 美联储加息/缩表预期对美股是利空（negative），降息/扩表预期是利好（positive）

新闻列表:{news_text}

只返回JSON数组，不要其他文字。示例:
[{{"title_cn":"...","direction":"positive","impact_pct":3,"reason":"AI芯片需求超预期推动收入增长","summary":"需求确认利好，可加仓","type":"个股"}}]"""

    try:
        time.sleep(3)  # Rate limit for DeepSeek V4 Pro
        headers = {
            "Authorization": f"Bearer {LLM_CFG['api_key']}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": LLM_CFG["model"],
            "messages": [{"role": "user", "content": prompt}],

            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        }
        resp, _l_err = _request_with_retry("post",
            f"{LLM_CFG['base_url']}/v1/chat/completions",
            headers=headers, json=payload, timeout=120, max_retries=3, base_delay=3)
        if resp is None:
            print(f"  [WARN] LLM请求失败(已重试): {_l_err}")
            return []
        if resp.status_code == 200:
            msg = resp.json()["choices"][0]["message"]
            content = msg.get("content", "").strip()
            reasoning = msg.get("reasoning_content", "").strip()
            if not content:
                content = reasoning
            json_match = re.search(r'\[\s*\{.*\}\s*\]', content, re.DOTALL)
            if not json_match and reasoning:
                json_match = re.search(r'\[\s*\{.*\}\s*\]', reasoning, re.DOTALL)
            if not json_match:
                combined = content + "\n" + reasoning
                json_match = re.search(r'\[\s*\{.*\}\s*\]', combined, re.DOTALL)
            if not json_match:
                print(f"  [WARN] LLM返回无JSON, retry...")
                time.sleep(5)
                resp, _l_err = _request_with_retry("post",
                    f"{LLM_CFG['base_url']}/v1/chat/completions",
                    headers=headers, json=payload, timeout=120, max_retries=2, base_delay=3)
                if resp is None:
                    print(f"  [WARN] LLM retry失败: {_l_err}")
                    return []
                if resp.status_code == 200:
                    msg = resp.json()["choices"][0]["message"]
                    content = msg.get("content", "").strip() or msg.get("reasoning_content", "").strip()
                    json_match = re.search(r'\[\s*\{.*\}\s*\]', content, re.DOTALL)
            if not json_match:
                print(f"  [WARN] LLM返回无JSON, skip")
                return None
            if json_match:
                raw_json = json_match.group()
                try:
                    result = json.loads(raw_json)
                except json.JSONDecodeError:
                    fixed = re.sub(r',\s*}', '}', raw_json)
                    fixed = re.sub(r',\s*]', ']', fixed)
                    try:
                        result = json.loads(fixed)
                    except json.JSONDecodeError:
                        print(f"  [WARN] JSON解析失败, raw={raw_json[:80]}")
                        return None
                label = "macro" if is_macro else symbol
                print(f"  LLM analyzed {len(result)} news for {label}")
                # 类型归一化：LLM可能返回字符串impact_pct（如"+3"/"3"/"-2.5"），统一转数字
                for _item in result:
                    if isinstance(_item, dict):
                        _v = _item.get("impact_pct")
                        if _v is not None and not isinstance(_v, (int, float)):
                            try:
                                _item["impact_pct"] = int(float(str(_v).replace("+", "").replace("%", "")))
                            except (ValueError, TypeError):
                                _item["impact_pct"] = 0
                        elif isinstance(_v, bool):
                            _item["impact_pct"] = 0
                return result
        else:
            print(f"  [WARN] LLM API error: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"  [WARN] LLM analysis failed: {e}")
    return None



# ============================================================
# 4. Earnings/event dates (yfinance calendar)
# ============================================================
def get_earnings_dates(symbols):
    """Get next earnings date for each stock.
    Uses NASDAQ official API (free, no key required).
    Returns dict of {symbol: earnings_date_str or "未公布"}.
    """
    import requests
    from datetime import date as dt_date, timedelta

    session = requests.Session()
    session.trust_env = False
    session.proxies = {"https": PROXY, "http": PROXY}
    session.verify = False

    result = {sym: "未公布" for sym in symbols}
    remaining = set(symbols)
    today = dt_date.today()
    max_days = 60  # query next 60 trading days

    for offset in range(max_days):
        if not remaining:
            break
        d = today + timedelta(days=offset)
        if d.weekday() >= 5:  # skip weekends
            continue
        try:
            url = f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}"
            resp = session.get(url, timeout=(7, 12),
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            if resp.status_code != 200:
                continue
            data = resp.json()
            if not data or not isinstance(data, dict):
                continue
            rows = (data.get("data") or {}).get("rows") or []
            for row in rows:
                sym = row.get("symbol", "")
                if sym in remaining:
                    result[sym] = d.isoformat()
                    remaining.discard(sym)
        except Exception:
            pass
        time.sleep(0.3)

    return result


# ============================================================
# 5. Stock selection (position + eligible)
# ============================================================
def get_relevant_stocks():
    """Get stocks with positions + eligible stocks (ratio>2.0, loss_rate>-10%).
    Excludes stocks in LLM_CFG['exclude_stocks'].
    Returns list of (symbol, name, has_position, is_eligible).
    """
    with open(WATCHLIST_F, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    stocks = {s["symbol"]: s for s in cfg.get("stocks", [])}
    # 港股/A股/ADR(有symbol且含买卖区间)并入, 新闻分析同样覆盖
    for h in cfg.get("hk_stocks", []):
        hs = (h.get("symbol") or "").strip()
        if hs and h.get("buy") and h.get("sell"):
            stocks[hs] = h

    # Load positions (exclude observation positions with margin < $1)
    positions = set()
    if STATE_F.exists():
        with open(STATE_F, "r", encoding="utf-8") as f:
            state = json.load(f)
        for sym, pos in state.get("positions", {}).items():
            if pos.get("remaining", 0) > 0:
                positions.add(sym)
    # Check OKX positions for margin filter
    obs_positions = set()
    try:
        _ak, _as, _pp = load_okx_keys()
        if not _ak:
            print("  [INFO] 未配置OKX密钥(云端只读模式), 跳过OKX持仓过滤")
        else:
            import requests as _r
            _s = _r.Session(); _s.trust_env = False; _s.verify = False
            _s.proxies = _auto_proxy("https://www.okx.com")
            _ts = datetime.now(timezone.utc).isoformat("T", "milliseconds").replace("+00:00", "Z")
            _msg = _ts + "GET/api/v5/account/positions"
            _sign = base64.b64encode(hmac.new(_as.encode(), _msg.encode(), digestmod="sha256").digest()).decode()
            _headers = {"Content-Type": "application/json", "OK-ACCESS-KEY": _ak,
                         "OK-ACCESS-SIGN": _sign, "OK-ACCESS-TIMESTAMP": _ts, "OK-ACCESS-PASSPHRASE": _pp, "x-simulated-trading": "0"}
            _resp = _s.get("https://www.okx.com/api/v5/account/positions", params={"instType": "SWAP"}, headers=_headers, timeout=10)
            if _resp.status_code == 200 and _resp.json().get("code") == "0":
                for p in _resp.json().get("data", []):
                    if float(p.get("pos", 0)) > 0:
                        # 全仓模式 margin 为空串: 用名义价值(数量×均价)近似判断灰尘仓
                        _m = _safe_float(p.get("margin", 0))
                        if _m <= 0:
                            _m = abs(_safe_float(p.get("pos", 0)) * _safe_float(p.get("avgPx", 0)))
                        if _m < 1:
                            obs_positions.add(p.get("instId", "").replace("-USDT-SWAP", ""))
            if obs_positions:
                print(f"  观察仓(保证金<$1)排除: {obs_positions}")
    except Exception as e:
        print(f"  [WARN] 获取OKX持仓失败: {e}")

    # Exclude list
    exclude = set(LLM_CFG.get("exclude_stocks", []))
    if exclude:
        print(f"  Excluding stocks: {exclude}")

    # --- Primary: Tencent Finance API (fast, no proxy, no rate limit) ---
    import requests as _req
    import time
    prices = {}
    sym_list = list(stocks.keys())

    _tq_session = _req.Session()
    _tq_session.trust_env = False
    # 腾讯是国内域名，直连（不设置proxies）
    print(f"  Fetching prices for {len(sym_list)} stocks from Tencent Finance...")
    def _tq_code(sym):
        """腾讯行情代码: 美股 usXXX, 港股 hk00700, A股 sz/sh, 其余(韩/日等)走yfinance兜底"""
        if sym.endswith(".HK"):
            return f"hk{sym[:-3]}"
        if sym.endswith(".SZ"):
            return f"sz{sym[:-3]}"
        if sym.endswith(".SS"):
            return f"sh{sym[:-3]}"
        return f"us{sym}"

    for sym in sym_list:
        try:
            tq_code = _tq_code(sym)
            url = f"https://qt.gtimg.cn/q={tq_code}"
            resp = _tq_session.get(url, timeout=10)
            if resp.status_code == 200 and resp.text.strip():
                parts = resp.text.split('~')
                if len(parts) > 3:
                    prices[sym] = float(parts[3])
        except:
            pass
    print(f"  Got {len(prices)}/{len(sym_list)} prices from Tencent")

    # --- Fallback: yfinance for missing stocks (slow, rate-limited) ---
    missing = [s for s in sym_list if s not in prices or prices[s] == 0]
    if missing:
        import yfinance as yf
        _dl_session = _req.Session()
        _dl_session.trust_env = False
        # yfinance 是国外域名: 本地走代理, 云端(NO_PROXY)直连
        _dl_session.proxies = _auto_proxy("https://query1.finance.yahoo.com")
        _dl_session.verify = False
        print(f"  [INFO] Fetching {len(missing)} missing prices from yfinance...")
        BATCH = 3
        for i in range(0, len(missing), BATCH):
            batch = missing[i:i+BATCH]
            try:
                syms_str = " ".join(batch)
                data = yf.download(syms_str, period="1d", progress=False, threads=False, session=_dl_session)
                if len(batch) == 1:
                    sym = batch[0]
                    close = data.get("Close")
                    if close is not None and len(close) > 0:
                        prices[sym] = float(close.iloc[-1])
                else:
                    close_df = data.get("Close")
                    if close_df is not None:
                        for sym in batch:
                            try:
                                val = close_df[sym].iloc[-1]
                                prices[sym] = float(val) if val == val else 0
                            except:
                                pass
            except Exception as e:
                print(f"  [WARN] yfinance batch failed: {e}")
            time.sleep(5)

    result = []
    for sym, s in stocks.items():
        if sym in exclude:
            continue
        buy = float(s.get("buy", 0))
        sell = float(s.get("sell", 0))
        if buy <= 0 or sell <= 0:
            continue

        price = prices.get(sym, 0)
        has_pos = sym in positions and sym not in obs_positions
        is_eligible = False
        if price and price > 0:
            win = sell - price
            loss = price - buy
            ratio = round(win / loss, 2) if loss > 0 else 999
            loss_rate = round((buy - price) / price * 100, 1)
            is_eligible = ratio > REORDER_PCT and loss_rate > -10

        if has_pos or is_eligible:
            result.append((sym, s.get("name", ""), has_pos, is_eligible))

    return result


# ============================================================
# 6. Cache management
# ============================================================


# ============================================================
# 3b. LLM stock/macro summary
# ============================================================
def _llm_stock_summary(symbol, name, news_list, has_position):
    """调用LLM对单只股票新闻做综合分析，返回 {"summary": str, "total_impact": int}"""
    if not LLM_CFG["api_key"] or not news_list:
        return {"summary": "", "total_impact": 0}
    import requests as req

    news_text = ""
    for i, n in enumerate(news_list, 1):
        direction_map = {"positive": "利好", "negative": "利空", "neutral": "中性"}
        d_cn = direction_map.get(n.get("direction", ""), n.get("direction", ""))
        auth = _get_authority(n.get("provider", ""))
        type_w = _get_news_type_weight(n.get("title", ""), n.get("summary", ""))
        time_w = _get_time_decay(n.get("pubDate", ""))
        weight = round(auth / 100 * type_w * time_w, 2)
        news_text += f"\n{i}. [{d_cn}, 影响{n.get('impact_pct',0)}%, 权重{weight}] {n.get('title_cn', n.get('title',''))}"
        if n.get("reason"):
            news_text += f" —— {n['reason']}"

    pos_str = "当前持仓" if has_position else "未持仓"
    prompt = f"""你是美股交易分析师。根据{name}({symbol})的近期新闻，返回JSON。

持仓状态: {pos_str}
近期新闻:{news_text}

返回JSON格式:
{{"summary": "80字以内综合分析+操作建议", "total_impact": 整数}}

total_impact规则：
- 这是所有新闻对{symbol}股价的综合影响百分比（如-5表示综合利空5%）
- 每条新闻有权重(0-1.5)，权重=来源权威度×类型权重×时间衰减
  - 来源权威: Reuters/Bloomberg=1.0, CNBC=0.9, Yahoo=0.7, 小站=0.3
  - 类型权重: 财报/FDA=1.5, 收购=1.4, 管理层/指引=1.3, 宏观=1.2, 评级=1.1, 评论=0.6
  - 时间衰减: 今天=1.0, 1天前=0.8, 2天=0.6, 3天=0.4, 5天+=0.2
- 高权重新闻影响更大，低权重新闻可忽略
- 同一事件不重复计算，利空利好可对冲
- 不要简单累加，基于权重加权判断

只返回JSON，不要其他文字。"""

    try:
        time.sleep(1.5)
        headers = {"Authorization": f"Bearer {LLM_CFG['api_key']}", "Content-Type": "application/json"}
        payload = {"model": LLM_CFG["model"], "messages": [{"role": "user", "content": prompt}], "thinking": {"type": "enabled"}, "reasoning_effort": "high"}
        resp, _l_err = _request_with_retry("post", f"{LLM_CFG['base_url']}/v1/chat/completions",
            headers=headers, json=payload, timeout=120, max_retries=3, base_delay=3)
        if resp is None:
            print(f"  [WARN] LLM总结失败(已重试) {symbol}: {_l_err}")
            return {"summary": "", "total_impact": 0}
        if resp.status_code == 200:
            msg = resp.json()["choices"][0]["message"]
            content = msg.get("content", "").strip()
            reasoning = msg.get("reasoning_content", "").strip()
            if not content:
                content = reasoning
            json_match = re.search(r'\{[^{}]*"summary"[^{}]*"total_impact"[^{}]*\}', content, re.DOTALL)
            if not json_match and reasoning:
                json_match = re.search(r'\{[^{}]*"summary"[^{}]*"total_impact"[^{}]*\}', reasoning, re.DOTALL)
            if not json_match:
                combined = content + "\n" + reasoning
                json_match = re.search(r'\{[^{}]*"summary"[^{}]*"total_impact"[^{}]*\}', combined, re.DOTALL)
            if not content:
                time.sleep(5)
                resp, _l_err2 = _request_with_retry("post", f"{LLM_CFG['base_url']}/v1/chat/completions",
                    headers=headers, json=payload, timeout=120, max_retries=2, base_delay=3)
                if resp is None:
                    print(f"  [WARN] LLM retry失败: {_l_err2}")
                elif resp.status_code == 200:
                    msg = resp.json()["choices"][0]["message"]
                    content = msg.get("content", "").strip() or msg.get("reasoning_content", "").strip()
                    json_match = re.search(r'\{[^{}]*"summary"[^{}]*"total_impact"[^{}]*\}', content, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                summary = result.get("summary", "")
                impact = int(result.get("total_impact", 0))
                impact = max(-20, min(20, impact))
                print(f"    LLM总结 {symbol}: impact={impact}%, {summary[:30]}...")
                return {"summary": summary, "total_impact": impact}
            else:
                print(f"    [WARN] LLM总结 {symbol}: 无法解析JSON, raw={content[:60]}")
                return {"summary": content[:80], "total_impact": 0}
        else:
            print(f"    [WARN] LLM总结失败 {symbol}: {resp.status_code}")
    except Exception as e:
        print(f"    [WARN] LLM总结异常 {symbol}: {e}")
    return {"summary": "", "total_impact": 0}


def _llm_macro_summary(macro_news):
    """调用LLM对宏观新闻做综合分析总结"""
    if not LLM_CFG["api_key"] or not macro_news:
        return ""
    import requests as req

    news_text = ""
    for i, n in enumerate(macro_news, 1):
        direction_map = {"positive": "利好", "negative": "利空", "neutral": "中性"}
        d_cn = direction_map.get(n.get("direction", ""), n.get("direction", ""))
        news_text += f"\n{i}. [{d_cn}, 影响{n.get('impact_pct',0)}%] {n.get('title_cn', n.get('title',''))}"
        if n.get("reason"):
            news_text += f" —— {n['reason']}"

    prompt = f"""你是宏观经济分析师。根据近期宏观新闻，给出简短综合分析（80字以内）。

近期宏观新闻:{news_text}

要求：
1. 总结宏观环境对美股的总体影响
2. 判断当前是否适合加仓
3. 用中文，简洁直接"""

    try:
        time.sleep(1.5)
        headers = {"Authorization": f"Bearer {LLM_CFG['api_key']}", "Content-Type": "application/json"}
        payload = {"model": LLM_CFG["model"], "messages": [{"role": "user", "content": prompt}], "thinking": {"type": "enabled"}, "reasoning_effort": "high"}
        resp, _l_err = _request_with_retry("post", f"{LLM_CFG['base_url']}/v1/chat/completions",
            headers=headers, json=payload, timeout=120, max_retries=3, base_delay=3)
        if resp is None:
            print(f"  [WARN] LLM宏观总结失败(已重试): {_l_err}")
            return ""
        if resp.status_code == 200:
            msg = resp.json()["choices"][0]["message"]
            summary = msg.get("content", "").strip()
            reasoning = msg.get("reasoning_content", "").strip()
            if not summary:
                summary = reasoning
            if not summary:
                time.sleep(5)
                resp, _l_err2 = _request_with_retry("post", f"{LLM_CFG['base_url']}/v1/chat/completions",
                    headers=headers, json=payload, timeout=120, max_retries=2, base_delay=3)
                if resp is None:
                    print(f"  [WARN] LLM retry失败: {_l_err2}")
                elif resp.status_code == 200:
                    msg = resp.json()["choices"][0]["message"]
                    summary = msg.get("content", "").strip() or msg.get("reasoning_content", "").strip()
            print(f"  LLM宏观总结: {summary[:40]}...")
            return summary
        else:
            print(f"  [WARN] LLM宏观总结失败: {resp.status_code}")
    except Exception as e:
        print(f"  [WARN] LLM宏观总结异常: {e}")
    return ""


def _calc_total_impact(news_list):
    """Calculate total impact from news list.
    Same-direction impacts: take max (not sum, to avoid double-counting same event).
    Different-direction impacts: sum separately, then net.
    e.g. [-4, -3, +2] -> max(-4,-3) + 2 = -4 + 2 = -2
    """
    neg = [n["impact_pct"] for n in news_list if n["impact_pct"] < 0]
    pos = [n["impact_pct"] for n in news_list if n["impact_pct"] > 0]
    total_neg = max(neg) if neg else 0
    total_pos = max(pos) if pos else 0
    return total_neg + total_pos


# ============================================================
# 7. Main analysis pipeline
# ============================================================
def analyze_all(refresh=False):
    """Fetch and analyze news for all relevant stocks + macro news.
    调度设计（用户方案）:
      - 工作日20:00 OKX_News_2000任务: 传 --refresh 强制全量搜集+LLM分析 → 写缓存
      - 工作日21:00 OKX_Daily_Run任务: 不传参数, 缓存4小时内有效则直接复用(秒回)
      - 手动运行: 不传参数时同样复用缓存; 想强制刷新用 --refresh
    注意: 搜集对象始终是"持仓+满足买入条件"的相关股票(约9只), 不是全量39只。
    """
    # --- Cache reuse: 4小时内且非强制刷新时直接复用 ---
    if not refresh and CACHE_F.exists():
        try:
            with open(CACHE_F, "r", encoding="utf-8") as f:
                cached = json.load(f)
            cached_at = cached.get("cached_at", "")
            if cached_at:
                age = (datetime.now() - datetime.fromisoformat(cached_at)).total_seconds()
                if age < 4 * 3600:
                    print(f"[缓存] 使用缓存 news_cache.json ({cached_at}, {age/60:.0f}分钟前)，跳过全量抓取")
                    return cached
                else:
                    print(f"[缓存] 缓存已过期 ({age/60:.0f}分钟前)，重新抓取")
            else:
                print("[缓存] 缓存无时间戳，重新抓取")
        except Exception as ex:
            print(f"[缓存] 缓存读取失败({ex})，重新抓取")

    # --- 代理连通性检测：不通时跳过所有需要代理的步骤 ---
    proxy_ok, proxy_err = _check_proxy(timeout=5)
    if not proxy_ok:
        print(f"  [SKIP] 代理不可用 ({proxy_err})，跳过新闻抓取和LLM分析")
        # 生成空结果，让HTML生成步骤正常走完
        skipped = {
            "stocks": [], "macro": [], "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "macro_summary": "[代理不可用] 请启动VPN后重新运行 update_all.bat",
            "cached_at": "",  # 不缓存，下次自动重试
        }
        return skipped

    print("Fetching news and analyzing...")
    _t_start = time.time()
    _MAX_RUNTIME = 20 * 60  # 20分钟护栏：超时自动保存已获取数据退出

    def _time_left():
        return _MAX_RUNTIME - (time.time() - _t_start)

    def _check_timeout():
        if _time_left() <= 0:
            print(f"  [WARN] 总时长护栏触发（{_MAX_RUNTIME/60:.0f}分钟），保存已获取数据退出")
            return True
        return False

    stocks = get_relevant_stocks()
    print(f"  Relevant stocks: {[s[0] for s in stocks]}")

    analyzed = {"stocks": [], "macro": [], "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # --- Macro news ---
    print("\n  Fetching macro news...")
    macro_news = fetch_macro_news(max_items=5, days=7)
    if macro_news:
        # Rule-based first, then LLM dedup, then LLM analysis
        macro_rule = []
        for n in macro_news:
            direction, impact, confidence, ntype = rule_based_analysis(n["title"], n.get("summary", ""))
            auth_w = _get_authority(n.get("provider", "")) / 100
            type_w = _get_news_type_weight(n["title"], n.get("summary", ""))
            time_w = _get_time_decay(n.get("pubDate", ""))
            weighted_impact = round(impact * auth_w * type_w * time_w, 1)
            macro_rule.append({**n, "direction": direction, "impact_pct": weighted_impact, "type": ntype})
        keep_idx = llm_dedup(macro_rule)
        macro_rule = [macro_rule[i] for i in keep_idx if i < len(macro_rule)]
        macro_rule.sort(key=lambda x: -abs(x.get("impact_pct", 0)))
        if len(macro_rule) > 5:
            macro_rule = macro_rule[:5]

        macro_llm = llm_analyze_batch("MACRO", macro_rule, is_macro=True)
        for i, n in enumerate(macro_rule):
            if macro_llm and i < len(macro_llm):
                lr = macro_llm[i]
                analyzed["macro"].append({
                    "title": n["title"],
                    "title_cn": lr.get("title_cn", n["title"]),
                    "summary": lr.get("summary", ""),
                    "pubDate": n["pubDate"],
                    "provider": n["provider"],
                    "url": n["url"],
                    "direction": lr.get("direction", n["direction"]),
                    "impact_pct": lr.get("impact_pct", n["impact_pct"]),
                    "reason": lr.get("reason", ""),
                    "type": "宏观",
                    "source": "llm",
                })
            else:
                analyzed["macro"].append({
                    "title": n["title"],
                    "title_cn": n["title"],
                    "summary": "",
                    "pubDate": n["pubDate"],
                    "provider": n["provider"],
                    "url": n["url"],
                    "direction": n["direction"],
                    "impact_pct": n["impact_pct"],
                    "reason": "",
                    "type": "宏观",
                    "source": "rule",
                })

    # 兜底清洗：确保macro列表impact_pct均为数字
    for _m in analyzed["macro"]:
        _v = _m.get("impact_pct")
        if not isinstance(_v, (int, float)) or isinstance(_v, bool):
            try:
                _m["impact_pct"] = int(float(str(_v).replace("+", "").replace("%", "")))
            except (ValueError, TypeError):
                _m["impact_pct"] = 0

    # Sort macro: most impactful first
    analyzed["macro"].sort(key=lambda x: -abs(x.get("impact_pct", 0)))

    # LLM综合分析宏观新闻
    analyzed["macro_summary"] = ""
    if LLM_CFG["api_key"] and analyzed["macro"]:
        analyzed["macro_summary"] = _llm_macro_summary(analyzed["macro"])

    # --- Stock news ---
    for idx, (sym, name, has_pos, is_eligible) in enumerate(stocks):
        if _check_timeout():
            break
        if idx > 0:
            time.sleep(2)
        print(f"\n  {sym} ({name}) pos={has_pos} eligible={is_eligible}")
        news = fetch_stock_news(sym, max_items=10, days=7)
        if not news:
            print(f"    No news found")
            continue

        # Step 1: Rule-based analysis on all fetched news (with authority/type/time weights)
        rule_results = []
        for n in news:
            direction, impact, confidence, ntype = rule_based_analysis(n["title"], n.get("summary", ""))
            auth_w = _get_authority(n.get("provider", "")) / 100
            type_w = _get_news_type_weight(n["title"], n.get("summary", ""))
            time_w = _get_time_decay(n.get("pubDate", ""))
            weighted_impact = round(impact * auth_w * type_w * time_w, 1)
            rule_results.append({
                **n,
                "direction": direction,
                "impact_pct": weighted_impact,
                "confidence": confidence,
                "type": ntype,
            })

        # Step 2: LLM dedup (identify same-event news), then keep top 5 BEFORE LLM analysis
        before_dedup = len(rule_results)
        keep_indices = llm_dedup(rule_results)
        rule_results = [rule_results[i] for i in keep_indices if i < len(rule_results)]
        removed = before_dedup - len(rule_results)
        if removed > 0:
            print(f"    LLM去重: {before_dedup} -> {len(rule_results)} (去除{removed}条同事件)")
        rule_results.sort(key=lambda x: -abs(x.get("impact_pct", 0)))
        if len(rule_results) > 5:
            rule_results = rule_results[:5]
        print(f"    After dedup: {len(rule_results)} news to analyze")

        # Step 3: LLM analyze only the kept news
        llm_results = llm_analyze_batch(sym, rule_results)

        final_news = []
        for i, rn in enumerate(rule_results):
            if llm_results and i < len(llm_results):
                lr = llm_results[i]
                final_news.append({
                    "title": rn["title"],
                    "title_cn": lr.get("title_cn", rn["title"]),
                    "summary": lr.get("summary", rn.get("summary", "")),
                    "pubDate": rn["pubDate"],
                    "provider": rn["provider"],
                    "url": rn["url"],
                    "direction": lr.get("direction", rn["direction"]),
                    "impact_pct": lr.get("impact_pct", rn["impact_pct"]),
                    "reason": lr.get("reason", ""),
                    "type": lr.get("type", rn["type"]),
                    "source": "llm",
                })
            else:
                final_news.append({
                    "title": rn["title"],
                    "title_cn": rn["title"],
                    "summary": rn.get("summary", ""),
                    "pubDate": rn["pubDate"],
                    "provider": rn["provider"],
                    "url": rn["url"],
                    "direction": rn["direction"],
                    "impact_pct": rn["impact_pct"],
                    "reason": "",
                    "type": rn["type"],
                    "source": "rule",
                })

        # 兜底清洗：确保final_news中impact_pct均为数字
        for _fn in final_news:
            _v = _fn.get("impact_pct")
            if not isinstance(_v, (int, float)) or isinstance(_v, bool):
                try:
                    _fn["impact_pct"] = int(float(str(_v).replace("+", "").replace("%", "")))
                except (ValueError, TypeError):
                    _fn["impact_pct"] = 0

        # Sort: by impact magnitude (most impactful first)
        final_news.sort(key=lambda x: -abs(x["impact_pct"]))

        # LLM综合分析：对这只股票的所有新闻给出总结+综合影响
        llm_result = {"summary": "", "total_impact": 0}
        if LLM_CFG["api_key"] and final_news:
            llm_result = _llm_stock_summary(sym, name, final_news, has_pos)

        stock_data = {
            "symbol": sym,
            "name": name,
            "has_position": has_pos,
            "is_eligible": is_eligible,
            "news": final_news,
            "stock_summary": llm_result.get("summary", ""),
            "total_impact": llm_result["total_impact"] if "total_impact" in llm_result else _calc_total_impact(final_news),
            "net_direction": ("positive" if sum(1 for n in final_news if n["direction"]=="positive") >
                             sum(1 for n in final_news if n["direction"]=="negative")
                             else "negative" if sum(1 for n in final_news if n["direction"]=="negative") >
                             sum(1 for n in final_news if n["direction"]=="positive")
                             else "neutral"),
        }
        analyzed["stocks"].append(stock_data)

    # Sort: position stocks first, then eligible, then by total_impact ascending (worst first)
    analyzed["stocks"].sort(key=lambda x: (0 if x["has_position"] else 1, 0 if x["is_eligible"] else 2, x["total_impact"]))

    # --- Earnings dates ---
    all_syms = [s["symbol"] for s in analyzed["stocks"]]
    if all_syms and not _check_timeout():
        print(f"\n  Fetching earnings dates for {len(all_syms)} stocks...")
        earnings = get_earnings_dates(all_syms)
        for s in analyzed["stocks"]:
            s["next_earnings"] = earnings.get(s["symbol"], "未公布")
    else:
        for s in analyzed["stocks"]:
            s["next_earnings"] = "未公布"

    analyzed["cached_at"] = datetime.now().isoformat()
    with open(CACHE_F, "w", encoding="utf-8") as f:
        json.dump(analyzed, f, ensure_ascii=False, indent=2)
    return analyzed



# ============================================================
# Main
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="OKX news analysis")
    parser.add_argument("--set-key", metavar="API_KEY", help="Set DeepSeek API key and save to config")
    parser.add_argument("--status", action="store_true", help="Show config status")
    parser.add_argument("--refresh", action="store_true", help="Force refresh, ignore cache")
    args = parser.parse_args()

    if args.status:
        llm_status = "已配置" if LLM_CFG["api_key"] else "未配置"
        exclude = LLM_CFG.get("exclude_stocks", [])
        print(f"LLM: {llm_status} (model={LLM_CFG['model']}, base={LLM_CFG['base_url']})")
        print(f"Exclude: {exclude}")
        return

    if args.set_key:
        cfg = {}
        if NEWS_CFG_F.exists():
            with open(NEWS_CFG_F, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        cfg["api_key"] = args.set_key
        with open(NEWS_CFG_F, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"API key saved to {NEWS_CFG_F}")
        return

    # Suppress SSL warnings
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        analyze_all(refresh=args.refresh)
    except Exception as e:
        print(f"[ERROR] analyze_all failed: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    main()


