# -*- coding: utf-8 -*-
"""
产业周期识别 —— 数据采集 / 周期计量 / 页面渲染
=================================================
采集 7 个可获得稳定数据的产业周期核心指标, 计算:
  · 历史分位(周期位置)
  · 相位(底部回升/顶部回落...)
  · 前瞻统计(历史上处于相似分位时, 未来 6/12 个周期的变化分布)
并渲染成自包含的 HTML 片段, 供 dashboard.py 直接嵌入。

运行环境: 需要 akshare (D:/Anaconda/python.exe 或系统 Python3.12)
用法:     python collect_cycles.py                 # 距上次采集不足7天则自动跳过
          python collect_cycles.py --force         # 强制立即重新采集
          python collect_cycles.py --if-stale=14   # 自定义新鲜度窗口(天)
输出:     cycles_data.json      (数值, 供复用)
          cycles_fragment.html  (渲染好的页面片段)
"""
import os, sys, json, traceback
from datetime import datetime
from pathlib import Path

# 国内宏观/期货接口一律直连, 避免本地代理(127.0.0.1:7892)未开时全线报错
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)
os.environ.setdefault("NO_PROXY", "*")

import numpy as np
import pandas as pd

try:  # akshare 内部用 requests, 强制不走环境代理
    import requests
    _BaseSession = requests.Session

    class _DirectSession(_BaseSession):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.trust_env = False

    requests.Session = _DirectSession
except Exception:
    pass

import akshare as ak

SCRIPT_DIR = Path(__file__).resolve().parent
OUT_JSON = SCRIPT_DIR / "cycles_data.json"
OUT_HTML = SCRIPT_DIR / "cycles_fragment.html"

MAX_KEEP = 700      # 单序列最多保留多少个周期点
MAX_PLOT = 380      # SVG 折线最多画多少个点
TODAY = datetime.now().strftime("%Y%m%d")

# ══════════════════════════════════════════════════════════════
# 一、周期定义
# ══════════════════════════════════════════════════════════════
CYCLES = [
    dict(key="broker", name="券商周期", indicator="两市日成交额", unit="亿元",
         freq="W", agg="mean", ma=52, ref=(10000, "1万亿(行业盈亏线)"),
         logic="券商是纯行情放大器, 没有生产, 全靠佣金和自营盘。成交额是它的营收本身 —— "
               "日均>1万亿: 全行业躺赚; <8000亿: 整体亏损。看成交额比看研报管用。"),
    dict(key="hog", name="生猪周期", indicator="生猪现货价格指数", unit="",
         freq="W", agg="last", ma=52, ref=None,
         logic="猪从出生到出栏6个月, 母猪怀孕4个月, 供给调整天然滞后10个月。"
               "猪价本身就走3-4年的蛛网周期: 价高→补栏→10个月后过剩→价崩→去化→再涨。"),
    dict(key="realestate", name="房地产周期", indicator="国房景气指数", unit="",
         freq="M", agg="last", ma=24, ref=(100, "100=景气荣枯线"),
         logic="产业链最长、牵连最广, 与城镇化/人口/居民杠杆绑定, 18-20年一轮大周期。"
               "景气指数是量价投资的合成, 比房价本身更早见顶见底。"),
    dict(key="copper", name="工业金属(铜)", indicator="LME 铜库存", unit="吨",
         freq="W", agg="last", ma=52, ref=None,
         logic="铜是经济晴雨表, 矿山建设周期5-7年难以快速调节供给, 所以看库存就够了。"
               "库存是供需的蓄水池: 库存见底→抢货→价格涨; 库存堆积→无人接货→价格跌。"),
    dict(key="chem", name="化工周期", indicator="纯碱期货价", unit="元/吨",
         freq="W", agg="last", ma=52, ref=None,
         logic="重资产、同质化、无壁垒, 全是产能游戏: 赚钱→扩产→过剩→亏到停产→再涨。"
               "价格跌到行业现金成本附近即是出清信号, 是全行业最诚实的先行指标。"),
    dict(key="shipping", name="航运周期", indicator="波罗的海干散货指数 BDI", unit="点",
         freq="W", agg="last", ma=52, ref=None,
         logic="造船是重资产长周期, 新船下单到交付2-3年, 供给调整极慢, 需求一波动就是3-5年大行情。"
               "BDI 是全球干散货运价的即时体温计, 10-12年一个完整周期。"),
    dict(key="solar", name="光伏周期", indicator="工业硅期货价", unit="元/吨",
         freq="W", agg="last", ma=52, ref=None,
         logic="技术迭代快、产能扩张猛, 硅片电池片全是产能游戏, 一扩产就价格战, 崩到全行业亏损再出清。"
               "硅料/工业硅价格是产能周期的直接刻度。"),
]


# ══════════════════════════════════════════════════════════════
# 二、各周期数据采集
# ══════════════════════════════════════════════════════════════
def _series(dates, values):
    s = pd.Series(np.asarray(values, dtype=float), index=pd.to_datetime(dates))
    s = s[~s.index.isna()]
    s = s.dropna()
    return s.sort_index()


def _pick_col(df, *keywords):
    """在 DataFrame 列名里找同时包含所有关键词的列"""
    for c in df.columns:
        name = str(c)
        if all(k in name for k in keywords):
            return c
    return None


def fetch_broker():
    """两市日成交额(亿元)。优先东财 amount(真成交额), 回退新浪 volume(成交量)"""
    def _em(sym):
        df = ak.stock_zh_index_daily_em(symbol=sym)
        dcol = "date" if "date" in df.columns else df.columns[0]
        acol = "amount" if "amount" in df.columns else _pick_col(df, "amount")
        if acol is None:
            raise ValueError("东财返回无 amount 列")
        return pd.Series(df[acol].astype(float).values, index=pd.to_datetime(df[dcol]))
    try:
        s = (_em("sh000001") + _em("sz399001")) / 1e8
        return s.sort_index(), "东方财富·成交额", "亿元"
    except Exception:
        pass
    d1 = ak.stock_zh_index_daily(symbol="sh000001")
    d2 = ak.stock_zh_index_daily(symbol="sz399001")
    s = (_series(d1["date"], d1["volume"]) + _series(d2["date"], d2["volume"])) / 1e8
    return s.sort_index(), "新浪·成交量(代理)", "亿股"


def fetch_hog():
    df = ak.index_hog_spot_price()
    col = _pick_col(df, "指数") or df.columns[1]
    return _series(df["日期"], df[col]), "农业部·生猪现货价指数", ""


def fetch_realestate():
    df = ak.macro_china_real_estate()
    return _series(df["日期"], df["最新值"]), "国房景气指数(月度)", ""


def fetch_copper():
    df = ak.macro_euro_lme_stock()
    col = _pick_col(df, "铜", "库存")
    if col is None:
        col = _pick_col(df, "库存")
    return _series(df["日期"], df[col]), "LME 铜库存", "吨"


def fetch_futures(symbol, label):
    df = ak.futures_main_sina(symbol=symbol, start_date="20000101", end_date=TODAY)
    return _series(df["日期"], df["收盘价"]), label, "元/吨"


def fetch_shipping():
    df = ak.macro_shipping_bdi()
    return _series(df["日期"], df["最新值"]), "波罗的海干散货指数", "点"


FETCHERS = {
    "broker": fetch_broker,
    "hog": fetch_hog,
    "realestate": fetch_realestate,
    "copper": fetch_copper,
    "chem": lambda: fetch_futures("SA0", "纯碱期货主力连续"),
    "shipping": fetch_shipping,
    "solar": lambda: fetch_futures("SI0", "工业硅期货主力连续"),
}


# ══════════════════════════════════════════════════════════════
# 三、周期计量
# ══════════════════════════════════════════════════════════════
def resample(s, freq, agg):
    if freq == "W":
        return s.resample("W-FRI").agg(agg).dropna()
    if freq == "M":
        try:
            return s.resample("ME").agg(agg).dropna()   # pandas >= 2.2
        except ValueError:
            return s.resample("M").agg(agg).dropna()    # pandas < 2.2
    return s


def analyze(s, freq, ma):
    """返回周期位置/相位/趋势/前瞻统计"""
    x = s.values.astype(float)
    n = len(x)
    cur = float(x[-1])
    pct = float((x <= cur).mean())

    win = 52 if freq == "W" else 12          # 半年窗口
    recent = float(np.mean(x[-win:])) if n >= win else float(np.mean(x))
    prev = float(np.mean(x[-2 * win:-win])) if n >= 2 * win else float(np.mean(x[:max(1, n // 2)]))
    chg = (recent / prev - 1.0) if prev else 0.0

    # 长期均线位置(去趋势后的相对水平)
    mav = float(np.mean(x[-ma:])) if n >= ma else float(np.mean(x))
    dev = (cur / mav - 1.0) if mav else 0.0

    # 相位判断
    if pct <= 0.25:
        pos = "周期低位"
    elif pct >= 0.75:
        pos = "周期高位"
    else:
        pos = "周期中位"
    up, dn = chg > 0.02, chg < -0.02
    if pct <= 0.25:
        phase = "底部回升" if up else ("底部探底" if dn else "底部徘徊")
    elif pct >= 0.75:
        phase = "顶部回落" if dn else ("顶部冲高" if up else "高位徘徊")
    elif up:
        phase = "上行中"
    elif dn:
        phase = "下行中"
    else:
        phase = "横盘整理"

    h1, h2 = (26, 52) if freq == "W" else (6, 12)
    return dict(cur=cur, pct=pct, pos=pos, phase=phase, chg=chg, dev=dev,
                f6=forecast(x, h1), f12=forecast(x, h2),
                h1=h1, h2=h2, n=n)


def forecast(x, h, band=0.10, min_n=8):
    """前瞻统计: 历史上分位与当前相近(±10%)的时点, 其后 h 期的变化率分布"""
    n = len(x)
    if n < h + 40:
        return None
    cur = x[-1]
    q = float((x <= cur).mean())
    buf = []
    for t in range(30, n - h):
        qt = float((x[:t + 1] <= x[t]).mean())
        if abs(qt - q) <= band and x[t] > 0:
            buf.append(x[t + h] / x[t] - 1.0)
    if len(buf) < min_n:
        return None
    a = np.array(buf)
    return dict(n=int(len(a)), med=float(np.median(a)),
                p25=float(np.percentile(a, 25)), p75=float(np.percentile(a, 75)),
                win=float((a > 0).mean()))


# ══════════════════════════════════════════════════════════════
# 四、SVG 曲线渲染
# ══════════════════════════════════════════════════════════════
def _fmt(v, unit):
    if unit == "亿元":
        return f"{v:,.0f}"
    if unit == "吨":
        return f"{v/1e4:,.1f}万"
    if abs(v) >= 10000:
        return f"{v:,.0f}"
    if abs(v) >= 100:
        return f"{v:,.1f}"
    return f"{v:,.2f}"


def _thin(ys, maxn=MAX_PLOT):
    n = len(ys)
    if n <= maxn:
        return list(range(n))
    step = n / maxn
    idx = [int(i * step) for i in range(maxn)]
    if idx[-1] != n - 1:
        idx[-1] = n - 1
    return idx


def svg_chart(s, info, unit, ma, ref, w=1180, h=250):
    """历史曲线 + 均线 + 当前位置 + 前瞻统计延伸带"""
    xs = s.index
    ys = s.values.astype(float)
    idx = _thin(ys)
    px_ys = [ys[i] for i in idx]
    n_hist = len(px_ys)

    # 前瞻延伸(6期)
    f6 = info.get("f6")
    ext_xs = 6
    f_ext = None
    if f6:
        f_ext = [info["cur"] * (1 + f6["med"]),
                 info["cur"] * (1 + f6["p25"]),
                 info["cur"] * (1 + f6["p75"])]

    # Y 轴范围: 以历史数据为准(前瞻区间不参与, 避免坐标轴被拉爆失真)
    hist_lo, hist_hi = min(px_ys), max(px_ys)
    span = (hist_hi - hist_lo) or abs(hist_hi) or 1.0
    lo = hist_lo - 0.06 * span
    hi = hist_hi + 0.16 * span          # 上方多留空间给前瞻带
    if hist_lo > 0:
        lo = max(lo, 0.0)
    if ref and not (lo < ref[0] < hi):  # 参考线不在可视窗口内 → 不画, 免得曲线被压扁
        ref = None

    def _clip(v):
        return min(max(v, lo), hi)

    L, R, T, B = 66, 92, 16, 30
    pw, ph = w - L - R, h - T - B
    total_n = n_hist + ext_xs

    def X(i):
        return L + pw * i / (total_n - 1)

    def Y(v):
        return T + ph * (1 - (v - lo) / (hi - lo))

    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(px_ys))

    # 均线
    mav = s.rolling(ma).mean().values
    ma_pts = " ".join(f"{X(k):.1f},{Y(mav[i]):.1f}"
                      for k, i in enumerate(idx) if not np.isnan(mav[i]))

    parts = [f'<svg viewBox="0 0 {w} {h}" '
             f'style="width:100%;height:auto;display:block">']

    # 网格 + Y 轴刻度
    for g in range(4):
        v = lo + (hi - lo) * g / 3
        y = Y(v)
        parts.append(f'<line x1="{L}" y1="{y:.1f}" x2="{w-R}" y2="{y:.1f}" '
                     f'stroke="#21262d" stroke-width="1"/>')
        parts.append(f'<text x="{L-8}" y="{y+4:.1f}" fill="#8b949e" font-size="11" '
                     f'text-anchor="end">{_fmt(v, unit)}</text>')

    # 参考线(行业盈亏线等)
    if ref and lo < ref[0] < hi:
        yr = Y(ref[0])
        parts.append(f'<line x1="{L}" y1="{yr:.1f}" x2="{w-R}" y2="{yr:.1f}" '
                     f'stroke="#d2991d" stroke-width="1" stroke-dasharray="6 4" opacity="0.75"/>')
        parts.append(f'<text x="{w-R-4}" y="{yr-5:.1f}" fill="#d2991d" font-size="10" '
                     f'text-anchor="end">{ref[1]}</text>')

    # 前瞻延伸带 + 中位虚线
    if f_ext:
        x0, x1 = X(n_hist - 1), X(total_n - 1)
        cur_v = info["cur"]
        med, low, hi_v = _clip(f_ext[0]), _clip(f_ext[1]), _clip(f_ext[2])
        y0, y_med, y_low, y_hi = Y(cur_v), Y(med), Y(low), Y(hi_v)
        parts.append(f'<polygon points="{x0:.1f},{y0:.1f} {x1:.1f},{y_hi:.1f} '
                     f'{x1:.1f},{y_low:.1f} {x0:.1f},{y0:.1f}" '
                     f'fill="rgba(240,136,62,0.16)"/>')
        parts.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" '
                     f'x2="{x1:.1f}" y2="{y_med:.1f}" stroke="#f0883e" '
                     f'stroke-width="1.6" stroke-dasharray="5 4"/>')
        parts.append(f'<circle cx="{x1:.1f}" cy="{y_med:.1f}" r="3" fill="#f0883e"/>')
        # 预测区竖分隔
        parts.append(f'<line x1="{x0:.1f}" y1="{T}" x2="{x0:.1f}" y2="{T+ph}" '
                     f'stroke="#30363d" stroke-width="1" stroke-dasharray="3 3"/>')
        parts.append(f'<text x="{(x0+x1)/2:.1f}" y="{T+11}" fill="#f0883e" '
                     f'font-size="10" text-anchor="middle">未来6期(统计外推)</text>')

    # 均线 / 主线
    if ma_pts:
        parts.append(f'<polyline points="{ma_pts}" fill="none" stroke="#8b949e" '
                     f'stroke-width="1" stroke-dasharray="4 3" opacity="0.65"/>')
    parts.append(f'<polyline points="{pts}" fill="none" stroke="#58a6ff" stroke-width="1.8"/>')

    # 当前位置
    cx, cy = X(n_hist - 1), Y(px_ys[-1])
    parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="6" fill="rgba(88,166,255,0.22)"/>')
    parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3" fill="#f0f6fc"/>')

    # X 轴日期
    for frac in (0.0, 0.5, 1.0):
        i = idx[min(len(idx) - 1, int(round((len(idx) - 1) * frac)))]
        parts.append(f'<text x="{X((n_hist-1)*frac):.1f}" y="{h-9}" fill="#8b949e" '
                     f'font-size="10" text-anchor="middle">{xs[i].strftime("%Y-%m")}</text>')
    parts.append('</svg>')
    return "".join(parts)


# ══════════════════════════════════════════════════════════════
# 五、HTML 片段渲染
# ══════════════════════════════════════════════════════════════
CYCLES_CSS = """
<style>
.cyc-wrap { font-family: -apple-system,'Segoe UI','Microsoft YaHei',sans-serif; }
.cyc-topbar { background:#161b22; border:1px solid #30363d; border-radius:12px;
  padding:14px 20px; margin-bottom:16px; display:flex; justify-content:space-between;
  align-items:center; flex-wrap:wrap; gap:8px; }
.cyc-topbar .t { font-size:15px; font-weight:600; color:#e6edf3; }
.cyc-topbar .s { font-size:12px; color:#8b949e; }
.cyc-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:20px; }
.cyc-mini { background:#161b22; border:1px solid #30363d; border-radius:10px;
  padding:12px 14px; transition:all .2s; }
.cyc-mini:hover { border-color:#58a6ff; box-shadow:0 0 14px rgba(88,166,255,.10); }
.cyc-mini .nm { font-size:13px; font-weight:600; color:#e6edf3; margin-bottom:2px; }
.cyc-mini .ind { font-size:10px; color:#8b949e; margin-bottom:8px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.cyc-mini .val { font-size:19px; font-weight:600; color:#e6edf3; font-variant-numeric:tabular-nums; }
.cyc-mini .val .u { font-size:11px; color:#8b949e; font-weight:400; margin-left:3px; }
.cyc-mini .ph { font-size:11px; font-weight:500; margin:6px 0 8px; }
.cyc-bar { position:relative; height:6px; background:#21262d; border-radius:3px; overflow:hidden; }
.cyc-bar i { position:absolute; top:0; left:0; height:100%; border-radius:3px; }
.cyc-bar .pct-lab { position:absolute; right:0; top:-16px; font-size:10px; color:#8b949e; }
.cyc-card { background:#161b22; border:1px solid #30363d; border-radius:12px;
  padding:16px 20px 14px; margin-bottom:16px; }
.cyc-hd { display:flex; justify-content:space-between; align-items:flex-start;
  flex-wrap:wrap; gap:10px; margin-bottom:10px; }
.cyc-hd .lft { display:flex; flex-direction:column; gap:3px; }
.cyc-hd .nm { font-size:15px; font-weight:600; color:#e6edf3; }
.cyc-hd .nm .tag { font-size:10px; font-weight:500; padding:2px 7px; border-radius:4px;
  margin-left:8px; vertical-align:middle; background:#21262d; color:#8b949e; border:1px solid #30363d; }
.cyc-hd .src { font-size:11px; color:#8b949e; }
.cyc-hd .rgt { display:flex; gap:22px; align-items:center; }
.cyc-hd .metric { text-align:right; }
.cyc-hd .metric .k { font-size:10px; color:#8b949e; }
.cyc-hd .metric .v { font-size:17px; font-weight:600; color:#e6edf3; font-variant-numeric:tabular-nums; }
.cyc-fc { display:grid; grid-template-columns:repeat(2,1fr); gap:10px; margin-top:12px; }
.cyc-fc .box { background:#0d1117; border:1px solid #21262d; border-radius:8px; padding:9px 12px; }
.cyc-fc .box .k { font-size:10px; color:#8b949e; margin-bottom:3px; }
.cyc-fc .box .v { font-size:14px; font-weight:600; font-variant-numeric:tabular-nums; }
.cyc-fc .box .d { font-size:10px; color:#6e7681; margin-top:3px; }
.cyc-logic { margin-top:12px; font-size:12.5px; line-height:1.75; color:#adbac7;
  border-left:3px solid #30363d; padding-left:12px; }
.cyc-note { font-size:11px; color:#6e7681; font-weight:400; margin-left:6px; }
</style>
"""


def _ph_color(pct):
    if pct <= 0.25:
        return "#3fb950"
    if pct >= 0.75:
        return "#f85149"
    return "#58a6ff"


def render_fragment(rows, updated):
    css = CYCLES_CSS
    minis = []
    cards = []
    for r in rows:
        info = r["stats"]
        color = _ph_color(info["pct"])
        cur_txt = _fmt(info["cur"], r["unit"])
        unit = r["unit"] or ""
        dev = info["dev"] * 100
        f6, f12 = info.get("f6"), info.get("f12")

        def fc_txt(f, label):
            if not f:
                return (f'<div class="box"><div class="k">{label}</div>'
                        f'<div class="v" style="color:#6e7681">样本不足</div>'
                        f'<div class="d">该指标历史长度不足以统计</div></div>')
            c = "#3fb950" if f["med"] > 0 else "#f85149"
            return (f'<div class="box"><div class="k">{label}</div>'
                    f'<div class="v" style="color:{c}">{f["med"]*100:+.1f}%</div>'
                    f'<div class="d">25%~75%区间 {f["p25"]*100:+.1f}% ~ {f["p75"]*100:+.1f}%'
                    f' | 上涨概率 {f["win"]*100:.0f}% | n={f["n"]}</div></div>')

        minis.append(
            f'<div class="cyc-mini">'
            f'<div class="nm">{r["name"]}</div>'
            f'<div class="ind" title="{r["indicator"]}">{r["indicator"]}</div>'
            f'<div class="val">{cur_txt}<span class="u">{unit}</span></div>'
            f'<div class="ph" style="color:{color}">{info["phase"]}</div>'
            f'<div class="cyc-bar"><i style="width:{info["pct"]*100:.0f}%;background:{color}"></i>'
            f'<span class="pct-lab">位置 {info["pct"]*100:.0f}%</span></div>'
            f'</div>')

        cards.append(
            f'<div class="cyc-card">'
            f'<div class="cyc-hd">'
            f'<div class="lft"><div class="nm">{r["name"]}'
            f'<span class="tag" style="color:{color};border-color:{color}44">{info["phase"]}</span></div>'
            f'<div class="src">{r["indicator"]} · {r["source"]} · 数据截至 {r["last_date"]}'
            f' · {r["n_raw"]}个{r["freq"]=="M" and "月" or "周"}样本</div></div>'
            f'<div class="rgt">'
            f'<div class="metric"><div class="k">当前值</div><div class="v">{cur_txt} {unit}</div></div>'
            f'<div class="metric"><div class="k">历史分位</div>'
            f'<div class="v" style="color:{color}">{info["pct"]*100:.0f}%</div></div>'
            f'<div class="metric"><div class="k">偏离长期均线</div>'
            f'<div class="v" style="color:{"#3fb950" if dev>0 else "#f85149"}">{dev:+.1f}%</div></div>'
            f'</div></div>'
            f'{r["svg"]}'
            f'<div class="cyc-fc">{fc_txt(f6, "未来半年 · 历史相似位置后")}'
            f'{fc_txt(f12, "未来一年 · 历史相似位置后")}</div>'
            f'<div class="cyc-logic">{r["logic"]}</div>'
            f'</div>')

    return (css +
            '<div class="cyc-wrap">'
            f'<div class="cyc-topbar"><div class="t">产业周期识别</div>'
            f'<div class="s">位置=该指标在过去{MAX_KEEP}期内所处分位 · 前瞻=历史相似分位(±10%)样本的后验分布'
            f' · 更新 {updated}</div></div>'
            f'<div class="cyc-grid">{"".join(minis)}</div>'
            f'{"".join(cards)}'
            f'<div class="cyc-topbar"><div class="s">方法论: 曲线为原始指标(蓝线)+12月均线(灰虚线); '
            f'橙色虚线及阴影为"历史相似位置后6期"的统计中位数与25%~75%区间, 是历史后验而非预测承诺。'
            f'周期位置为滚动历史分位, 与个股/指数买卖信号无关。</div></div>'
            '</div>')


# ══════════════════════════════════════════════════════════════
# 六、主流程
# ══════════════════════════════════════════════════════════════
def main():
    # 采集频率控制: 产业周期数据本身更新很慢 —— BDI 虽日频但周期性强, LME 库存滞后约3个月,
    # 国房景气指数滞后约10个月。默认 7 天内不重复采集, 避免每天启动都白跑一遍网络。
    # 强制刷新: python collect_cycles.py --force
    force = "--force" in sys.argv
    stale_days = 7.0
    for _a in sys.argv[1:]:
        if _a.startswith("--if-stale="):
            try:
                stale_days = float(_a.split("=", 1)[1])
            except ValueError:
                pass
    if not force and stale_days > 0 and OUT_JSON.exists() and OUT_HTML.exists():
        try:
            _prev = datetime.strptime(
                json.loads(OUT_JSON.read_text(encoding="utf-8"))["updated"],
                "%Y-%m-%d %H:%M")
            _age = (datetime.now() - _prev).total_seconds() / 86400.0
            if _age < stale_days:
                print(f"[skip] 周期数据 {_age:.1f} 天前已采集, 未满 {stale_days:g} 天 "
                      f"(约 {stale_days - _age:.1f} 天后到期), 本次跳过。强制采集请加 --force")
                return 0
        except Exception:
            pass  # 时间戳读取异常则按需重新采集, 不做静默失败

    rows, failed = [], []
    for cfg in CYCLES:
        key = cfg["key"]
        try:
            s_raw, src, unit_override = FETCHERS[key]()
            s = resample(s_raw, cfg["freq"], cfg["agg"])
            if len(s) > MAX_KEEP:
                s = s.iloc[-MAX_KEEP:]
            if len(s) < 40:
                raise ValueError(f"样本过短({len(s)})")
            unit = unit_override or cfg["unit"]
            info = analyze(s, cfg["freq"], cfg["ma"])
            svg = svg_chart(s, info, unit, cfg["ma"], cfg.get("ref"))
            rows.append(dict(
                key=key, name=cfg["name"], indicator=cfg["indicator"], unit=unit,
                source=src, logic=cfg["logic"], freq=cfg["freq"],
                last_date=s.index[-1].strftime("%Y-%m-%d"), n_raw=len(s),
                stats=info, svg=svg,
                series=[[d.strftime("%Y-%m-%d"), round(float(v), 4)]
                        for d, v in zip(s.index, s.values)]))
            print(f"[OK] {cfg['name']:<10} {len(s):>4}点  {src:<18} "
                  f"位置{info['pct']*100:>5.1f}%  {info['phase']}")
        except Exception as e:
            failed.append(key)
            print(f"[FAIL] {cfg['name']}: {type(e).__name__}: {str(e)[:110]}")
            traceback.print_exc(limit=1)

    updated = datetime.now().strftime("%Y-%m-%d %H:%M")
    payload = dict(updated=updated, failed=failed,
                   cycles=[{k: v for k, v in r.items() if k != "svg"} for r in rows])
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    OUT_HTML.write_text(render_fragment(rows, updated), encoding="utf-8")
    print(f"\n已写出: {OUT_JSON.name} ({OUT_JSON.stat().st_size/1024:.0f}KB)  "
          f"{OUT_HTML.name} ({OUT_HTML.stat().st_size/1024:.0f}KB)")
    if failed:
        print(f"失败周期: {failed}")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
