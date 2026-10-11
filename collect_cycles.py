# -*- coding: utf-8 -*-
"""
产业周期观察 —— 多指标并列版 (v2)
=================================================
每个产业周期用 **3 个独立指标交叉验证**, 而不是单指标拍脑袋:
  · 位置 = 各指标在过去若干期所处分位, 取均值作为综合位置
  · 分歧度 = 3 个指标分位的标准差 —— 分歧大说明周期位置本身不清晰
  · 前瞻 = 主指标历史上处于相似分位时, 其后 6/12 期的变化分布(后验统计, 非预测)

运行环境: 需要 akshare (D:/Anaconda/python.exe)
用法:     python collect_cycles.py                 # 距上次采集不足7天则跳过联网, 仅刷新渲染
          python collect_cycles.py --force         # 强制重新采集
          python collect_cycles.py --if-stale=14   # 自定义新鲜度窗口(天)
输出:     cycles_data.json      (数值, 供复用)
          cycles_fragment.html  (渲染好的页面片段)
"""
import os, sys, json, traceback
from datetime import datetime
from pathlib import Path

# 国内宏观/期货接口一律直连, 避免本地代理未开时全线报错
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

SCHEMA_VER = 2          # 结构版本; 与缓存不符则强制重采
MAX_KEEP = 700          # 单序列最多保留多少个周期点
MAX_PLOT = 380          # 大图折线最多点数
MINI_PLOT = 190         # 小图折线最多点数
TODAY = datetime.now().strftime("%Y%m%d")


# ══════════════════════════════════════════════════════════════
# 一、指标采集函数 —— 统一返回 (Series, 数据源说明, 单位)
# ══════════════════════════════════════════════════════════════
def _series(dates, values):
    s = pd.Series(np.asarray(values, dtype=float), index=pd.to_datetime(dates))
    s = s[~s.index.isna()]
    return s.dropna().sort_index()


def _pick_col(df, *keywords):
    for c in df.columns:
        if all(k in str(c) for k in keywords):
            return c
    return None


def _fut(sym, src, unit="元/吨"):
    df = ak.futures_main_sina(symbol=sym, start_date="20000101", end_date=TODAY)
    return _series(df["日期"], df["收盘价"]), src, unit


def _macro(src_fn, src, unit="点", col="最新值"):
    df = src_fn()
    return _series(df["日期"], df[col]), src, unit


# ---- 券商 ----
def f_turnover():
    """两市日成交额(亿元)。
    东财 amount 是唯一能给出**成交额**历史的免费源(且给出的是元)。
    回退到新浪时只有 volume(股), 那是成交量不是成交额, 单位与 1万亿参考线都不成立 ——
    所以回退时一并把参考线拿掉并改单位, 免得页面出现『1,091 亿元』这种错得离谱的读数。
    (交易所官方 stock_sse_deal_daily / stock_szse_summary 能给真成交额, 但只有单日快照, 建不了历史)"""
    def _em(sym):
        df = ak.stock_zh_index_daily_em(symbol=sym)
        dcol = "date" if "date" in df.columns else df.columns[0]
        acol = "amount" if "amount" in df.columns else _pick_col(df, "amount")
        if acol is None:
            raise ValueError("东财返回无 amount 列")
        return pd.Series(df[acol].astype(float).values, index=pd.to_datetime(df[dcol]))
    try:
        s = (_em("sh000001") + _em("sz399001")) / 1e8
        return s.sort_index(), "东方财富·沪深两市成交额", "亿元", {}
    except Exception:
        d1 = ak.stock_zh_index_daily(symbol="sh000001")
        d2 = ak.stock_zh_index_daily(symbol="sz399001")
        s = (_series(d1["date"], d1["volume"]) + _series(d2["date"], d2["volume"])) / 1e8
        return (s.sort_index(),
                "新浪·两市成交量(成交额代理, 非成交额)",
                "亿股",
                {"name": "两市成交量(成交额代理)", "ref": None,
                 "note": "降级源: 只有成交量, 无成交额历史"})


def f_margin():
    """两融余额合计(亿元)"""
    a = ak.macro_china_market_margin_sh()
    b = ak.macro_china_market_margin_sz()
    sa = pd.Series(a["融资融券余额"].astype(float).values, index=pd.to_datetime(a["日期"]))
    sb = pd.Series(b["融资融券余额"].astype(float).values, index=pd.to_datetime(b["日期"]))
    s = (sa.add(sb, fill_value=0)).dropna() / 1e8
    return s.sort_index(), "沪深交易所·融资融券余额合计", "亿元"


def f_broker_index():
    df = ak.stock_zh_index_daily(symbol="sz399975")
    return _series(df["date"], df["close"]), "中证全指证券公司指数(399975)", "点"


# ---- 生猪 ----
def f_hog_spot():
    df = ak.index_hog_spot_price()
    col = _pick_col(df, "指数") or df.columns[1]
    return _series(df["日期"], df[col]), "农业农村部·生猪现货价指数", ""


def f_hog_fut():
    return _fut("LH0", "大商所·生猪期货主力连续")


def f_corn():
    return _fut("C0", "大商所·玉米期货主力连续(饲料成本)")


# ---- 房地产 ----
def f_realestate():
    return _macro(ak.macro_china_real_estate, "国家统计局·国房景气指数(月度)", "", "最新值")


def f_house_price():
    """70城新建商品住宅价格指数同比, 取各城市均值, 转成 % 同比"""
    df = ak.macro_china_new_house_price()
    col = _pick_col(df, "新建商品住宅", "同比")
    sub = df[["日期", col]].copy()
    sub[col] = pd.to_numeric(sub[col], errors="coerce")
    g = sub.dropna().groupby("日期")[col].mean() - 100.0
    g.index = pd.to_datetime(g.index)      # 该接口日期是字符串, 必须显式转
    return g.sort_index(), "国家统计局·70城新房价格指数同比(城市均值)", "%"


def f_construction():
    return _macro(ak.macro_china_construction_index, "中国建材指数(周度)", "点", "最新值")


def f_rebar():
    return _fut("RB0", "上期所·螺纹钢期货主力连续(地产链用钢)")


# ---- 工业金属 ----
def f_cu():
    return _fut("CU0", "上期所·沪铜期货主力连续")


def f_al():
    return _fut("AL0", "上期所·沪铝期货主力连续")


def f_lme_cu():
    df = ak.macro_euro_lme_stock()
    col = _pick_col(df, "铜", "库存")
    return _series(df["日期"], df[col]), "LME 伦交所铜库存", "吨"


# ---- 化工 ----
def f_sa():
    return _fut("SA0", "郑商所·纯碱期货主力连续")


def f_ta():
    return _fut("TA0", "郑商所·PTA期货主力连续")


def f_ma():
    return _fut("MA0", "郑商所·甲醇期货主力连续")


# ---- 航运 ----
def f_bdi():
    return _macro(ak.macro_shipping_bdi, "波罗的海交易所·BDI 干散货综合指数", "点", "最新值")


def f_bci():
    return _macro(ak.macro_shipping_bci, "波罗的海交易所·BCI 海岬型船运价", "点", "最新值")


def f_bpi():
    return _macro(ak.macro_shipping_bpi, "波罗的海交易所·BPI 巴拿马型船运价", "点", "最新值")


# ---- 光伏 ----
def f_si():
    return _fut("SI0", "广期所·工业硅期货主力连续")


def f_ps():
    return _fut("PS0", "广期所·多晶硅期货主力连续")


def f_fg():
    return _fut("FG0", "郑商所·玻璃期货主力连续")


# ---- 存储 ----
def f_sox():
    df = ak.macro_global_sox_index()
    return _series(df["日期"], df["最新值"]), "费城半导体指数 SOX(1994 年起)", "点", {}


def f_micron():
    df = ak.stock_us_daily(symbol="MU")
    return _series(df["date"], df["close"]), "美光科技 MU(新浪美股, 2011 年起)", "美元", {}


def f_cn_memory_board():
    """A股存储板块。
    优先东财概念板块『存储』, 依次降级到东财『半导体』概念/行业板块,
    最后降级到新浪『国证算力』指数 —— 保证任何网络环境下都有数据可出。
    注意: 东财两个板块接口的 period 取值不同(概念='daily', 行业='日k'), 别传混。"""
    for kw, kind in (("存储", "concept"), ("半导体", "concept"), ("半导体", "industry")):
        try:
            if kind == "concept":
                names = ak.stock_board_concept_name_em()
                hist = ak.stock_board_concept_hist_em
                period = "daily"
            else:
                names = ak.stock_board_industry_name_em()
                hist = ak.stock_board_industry_hist_em
                period = "日k"
            hit = names[names["板块名称"].astype(str).str.contains(kw, na=False)]
            if not len(hit):
                continue
            target = str(hit.iloc[0]["板块名称"])
            df = hist(symbol=target, period=period, start_date="20140101",
                      end_date=TODAY, adjust="")
            dcol = "日期" if "日期" in df.columns else df.columns[0]
            ccol = "收盘" if "收盘" in df.columns else _pick_col(df, "收盘")
            s = _series(df[dcol], df[ccol])
            if len(s) < 60:
                continue
            return s, f"东方财富·{target}板块指数", "点", {}
        except Exception:
            continue
    df = ak.stock_zh_index_daily(symbol="sz399363")
    return (_series(df["date"], df["close"]),
            "新浪·国证算力指数(399363) —— 东财板块不可用时的降级代理",
            "点",
            {"name": "国证算力(存储板块降级)", "note": "降级源: 非存储专用板块"})


FE = {
    "turnover": f_turnover, "margin": f_margin, "broker_index": f_broker_index,
    "hog_spot": f_hog_spot, "hog_fut": f_hog_fut, "corn": f_corn,
    "realestate": f_realestate, "house_price": f_house_price, "construction": f_construction,
    "cu": f_cu, "al": f_al, "lme_cu": f_lme_cu,
    "sa": f_sa, "ta": f_ta, "ma": f_ma,
    "bdi": f_bdi, "bci": f_bci, "bpi": f_bpi,
    "si": f_si, "ps": f_ps, "fg": f_fg,
    "rebar": f_rebar,
    "sox": f_sox, "micron": f_micron, "cn_mem": f_cn_memory_board,
}


# ══════════════════════════════════════════════════════════════
# 二、周期定义 —— 每个周期 3 个并列指标
# ══════════════════════════════════════════════════════════════
def I(k, nm, fn, unit="元/吨", freq="W", agg="last", ma=52, ref=None, note="",
      main=False, pos=True):
    """pos=True: 过滤 <=0 的脏数据(运价指数/库存等不可能为负, 但源数据偶有负值毛刺)"""
    return dict(k=k, name=nm, fetch=fn, unit=unit, freq=freq, agg=agg,
                ma=ma, ref=ref, note=note, main=main, pos=pos)


CYCLES = [
    dict(key="memory", name="存储周期", tag="3-4 年 · DRAM/NAND",
         logic="存储是半导体里最纯粹的『蛛网周期』: 原厂建一条产线 2-3 年, 一旦同时扩产就过剩, "
               "一旦同时砍资本开支就缺货。历史上 2018 顶 → 2019 崩 → 2020-21 缺货 → 2022-23 崩 → "
               "2024 起 AI/HBM 拉动新一轮。三个指标互相验证: 费半是全球半导体景气的基准(含存储原厂), "
               "美光是纯 DRAM/NAND 原厂(弹性最大、最能代表存储本身), A股存储板块是国内映射。"
               "注意: 你原本要的『三大原厂资本开支同比』需要逐季读三星/海力士/美光财报, 没有免费实时接口; "
               "这里用原厂股价作代理 —— 股价是市场对资本开支周期的**提前定价**, 逻辑上甚至比财报更早。",
         indicators=[
             I("sox", "费城半导体指数", "sox", "点", "W", "last", 52, None, main=True),
             I("micron", "美光科技 MU", "micron", "美元", "W", "last", 52, None, note="纯存储原厂"),
             I("cn_mem", "A股存储板块", "cn_mem", "点", "W", "last", 52, None),
         ]),

    dict(key="broker", name="券商周期", tag="牛市杠杆",
         logic="券商是纯行情放大器, 没有生产, 营收≈佣金+自营+两融利息。三个指标互相验证: "
               "成交额是佣金基数, 两融余额是杠杆情绪, 券商指数是市场给的定价。"
               "成交额日均>1万亿全行业躺赚, <8000亿整体亏损。最值得看的是**背离** —— "
               "成交额和两融都在历史高位、券商股价却在中低位, 说明市场根本没给券商定价, 这种时候它才有性价比。",
         indicators=[
             I("turnover", "两市日成交额", "turnover", "亿元", "W", "mean", 52, (10000, "1万亿(行业盈亏线)")),
             I("margin", "两融余额", "margin", "亿元", "W", "last", 52, None, main=True),
             I("broker_index", "证券公司指数", "broker_index", "点", "W", "last", 52, None),
         ]),

    dict(key="hog", name="生猪周期", tag="蛛网 3-4 年",
         logic="猪从出生到出栏 6 个月, 母猪怀孕 4 个月, 供给调整天然滞后 10 个月。"
               "现货价是当下供需结果, 期货价包含市场对 10 个月后供给的预期, 玉米价格决定养殖成本线。"
               "三者一起看: 猪价跌 + 玉米稳 = 亏损去化; 猪价跌 + 玉米涨 = 加速出清(底部信号)。",
         indicators=[
             I("hog_spot", "生猪现货价指数", "hog_spot", "", "W", "last", 52, None, main=True),
             I("hog_fut", "生猪期货主力", "hog_fut", "元/吨", "W", "last", 52, None),
             I("corn", "玉米期货主力", "corn", "元/吨", "W", "last", 52, None, note="饲料成本端"),
         ]),

    dict(key="realestate", name="房地产周期", tag="18-20 年大周期",
         logic="产业链最长、牵连最广, 与城镇化/人口/居民杠杆绑定。"
               "70城房价是终端价格, 建材指数是上游需求(水泥玻璃)的真实回声, 螺纹钢是地产链用钢的即时报价。"
               "注意: 原来用的『国房景气指数』数据源已停更在 2025-12(滞后 10 个月), "
               "拿它算『当前位置』会得出错误结论, 故已剔除。这三个都是『同步或滞后』指标, 不是领先指标。",
         indicators=[
             I("house_price", "70城新房价格同比", "house_price", "%", "M", "last", 24,
               (0, "0%=同比持平"), note="统计局, 城市均值", main=True, pos=False),
             I("construction", "中国建材指数", "construction", "点", "W", "last", 52, None),
             I("rebar", "螺纹钢期货主力", "rebar", "元/吨", "W", "last", 52, None, note="地产链用钢"),
         ]),

    dict(key="copper", name="工业金属(铜)", tag="经济晴雨表",
         logic="铜矿建设周期 5-7 年, 供给端几乎调不动, 所以需求一波动就体现在库存和价格上。"
               "沪铜价格反映国内定价, 沪铝是同板块共振验证(铝走强说明整体需求不弱), "
               "LME 铜库存是海外供需的蓄水池 —— 注意伦交所库存数据滞后约 3 个月。",
         indicators=[
             I("cu", "沪铜期货主力", "cu", "元/吨", "W", "last", 52, None, main=True),
             I("al", "沪铝期货主力", "al", "元/吨", "W", "last", 52, None, note="板块共振"),
             I("lme_cu", "LME 铜库存", "lme_cu", "吨", "W", "last", 52, None, note="滞后约3个月"),
         ]),

    dict(key="chem", name="化工周期", tag="产能游戏",
         logic="重资产、同质化、无壁垒, 全是产能游戏: 赚钱→扩产→过剩→亏到停产→再涨。"
               "这里用三个不同下游的代表品种(纯碱→玻璃/光伏, PTA→聚酯纺织, 甲醇→煤化工/烯烃), "
               "避免单一品种的个体扰动。三者同向下跌到成本附近, 才是行业级出清信号。",
         indicators=[
             I("sa", "纯碱期货主力", "sa", "元/吨", "W", "last", 52, None, main=True),
             I("ta", "PTA 期货主力", "ta", "元/吨", "W", "last", 52, None),
             I("ma", "甲醇期货主力", "ma", "元/吨", "W", "last", 52, None),
         ]),

    dict(key="shipping", name="航运周期", tag="10-12 年大周期",
         logic="造船是重资产长周期, 新船下单到交付 2-3 年, 供给调整极慢, 需求一波动就是 3-5 年大行情。"
               "BDI 是干散货综合运价, BCI(海岬型/铁矿石煤炭)和 BPI(巴拿马型/粮食)是分船型拆解 —— "
               "三个同涨说明是全局需求回暖, 只有单一船型涨则多半是局部扰动。",
         indicators=[
             I("bdi", "BDI 干散货综合", "bdi", "点", "W", "last", 52, None, main=True),
             I("bci", "BCI 海岬型", "bci", "点", "W", "last", 52, None),
             I("bpi", "BPI 巴拿马型", "bpi", "点", "W", "last", 52, None),
         ]),

    dict(key="solar", name="光伏周期", tag="产能过剩",
         logic="技术迭代快、产能扩张猛, 硅料→硅片→电池片全是产能游戏, 一扩产就价格战。"
               "工业硅是上游原料, 多晶硅是核心环节, 玻璃是辅材/组件端 —— 三个环节一起看才能判断是全链条出清还是单环节涨价。"
               "注意: 多晶硅期货 2024 年底才上市, 样本短, 其分位参考价值有限。",
         indicators=[
             I("si", "工业硅期货主力", "si", "元/吨", "W", "last", 52, None, main=True),
             I("ps", "多晶硅期货主力", "ps", "元/吨", "W", "last", 52, None, note="样本短(2024-12起)"),
             I("fg", "玻璃期货主力", "fg", "元/吨", "W", "last", 52, None, note="辅材/组件端"),
         ]),
]


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
    x = s.values.astype(float)
    n = len(x)
    cur = float(x[-1])
    pct = float((x <= cur).mean())
    # 用"最近值 vs 更早值"算分位, 避免长尾分布里分位被极端值主导 —— 这里保持原定义(全样本分位)
    win = 52 if freq == "W" else 12
    recent = float(np.mean(x[-win:])) if n >= win else float(np.mean(x))
    prev = float(np.mean(x[-2 * win:-win])) if n >= 2 * win else float(np.mean(x[:max(1, n // 2)]))
    chg = (recent / prev - 1.0) if prev else 0.0
    mav = float(np.mean(x[-ma:])) if n >= ma else float(np.mean(x))
    dev = (cur / mav - 1.0) if mav else 0.0

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
    return dict(cur=cur, pct=pct, pos=pos, phase=phase, chg=chg, dev=dev, mav=mav,
                f6=forecast(x, h1), f12=forecast(x, h2), h1=h1, h2=h2, n=n)


def forecast(x, h, band=0.10, min_n=8):
    """前瞻统计: 历史上分位与当前相近(±10%)的时点, 其后 h 期的变化率分布"""
    n = len(x)
    if n < h + 40:
        return None
    cur = x[-1]
    q = float((x <= cur).mean())
    buf = []
    for t in range(30, n - h):
        if x[t] <= 0:
            continue
        qt = float((x[:t + 1] <= x[t]).mean())
        if abs(qt - q) <= band:
            buf.append(x[t + h] / x[t] - 1.0)
    if len(buf) < min_n:
        return None
    a = np.array(buf)
    return dict(n=int(len(a)), med=float(np.median(a)),
                p25=float(np.percentile(a, 25)), p75=float(np.percentile(a, 75)),
                win=float((a > 0).mean()))


def phase_of(pct, chg):
    up, dn = chg > 0.02, chg < -0.02
    if pct <= 0.25:
        return "底部回升" if up else ("底部探底" if dn else "底部徘徊")
    if pct >= 0.75:
        return "顶部回落" if dn else ("顶部冲高" if up else "高位徘徊")
    if up:
        return "上行中"
    if dn:
        return "下行中"
    return "横盘整理"


# ══════════════════════════════════════════════════════════════
# 四、SVG 曲线渲染
# ══════════════════════════════════════════════════════════════
def _fmt(v, unit):
    if unit == "亿元":
        return f"{v:,.0f}"
    if unit == "吨":
        return f"{v/1e4:,.1f}万"
    if unit == "亿股":
        return f"{v:,.0f}"
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


def svg_chart(s, info, unit, ma, ref, w=1180, h=250, compact=False, color="#58a6ff"):
    """历史曲线 + 均线 + 当前位置 (+ 大图带前瞻统计延伸带)"""
    xs = s.index
    ys = s.values.astype(float)
    idx = _thin(ys, MINI_PLOT if compact else MAX_PLOT)
    px_ys = [ys[i] for i in idx]
    n_hist = len(px_ys)

    f6 = None if compact else info.get("f6")
    ext_xs = 6 if f6 else 0
    f_ext = None
    if f6:
        f_ext = [info["cur"] * (1 + f6["med"]),
                 info["cur"] * (1 + f6["p25"]),
                 info["cur"] * (1 + f6["p75"])]

    hist_lo, hist_hi = min(px_ys), max(px_ys)
    span = (hist_hi - hist_lo) or abs(hist_hi) or 1.0
    pad_lo = 0.10 if compact else 0.06
    pad_hi = 0.10 if compact else 0.16
    lo = hist_lo - pad_lo * span
    hi = hist_hi + pad_hi * span
    if hist_lo > 0:
        lo = max(lo, 0.0)
    if ref and not (lo < ref[0] < hi):
        ref = None

    def _clip(v):
        return min(max(v, lo), hi)

    if compact:
        L, R, T, B, fs = 42, 10, 9, 17, 9
    else:
        L, R, T, B, fs = 66, 92, 16, 30, 11

    pw, ph = w - L - R, h - T - B
    total_n = n_hist + ext_xs

    def X(i):
        return L + pw * i / (total_n - 1)

    def Y(v):
        return T + ph * (1 - (v - lo) / (hi - lo))

    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(px_ys))

    mav = s.rolling(ma).mean().values
    ma_pts = " ".join(f"{X(k):.1f},{Y(mav[i]):.1f}"
                      for k, i in enumerate(idx) if not np.isnan(mav[i]))

    parts = [f'<svg viewBox="0 0 {w} {h}" '
             f'style="width:100%;height:auto;display:block">']

    # 网格 + Y 轴刻度
    ngrid = 3 if compact else 4
    for g in range(ngrid):
        v = lo + (hi - lo) * g / (ngrid - 1)
        y = Y(v)
        parts.append(f'<line x1="{L}" y1="{y:.1f}" x2="{w-R}" y2="{y:.1f}" '
                     f'stroke="#21262d" stroke-width="1"/>')
        parts.append(f'<text x="{L-5}" y="{y+3.5:.1f}" fill="#8b949e" font-size="{fs}" '
                     f'text-anchor="end">{_fmt(v, unit)}</text>')

    if ref and lo < ref[0] < hi:
        yr = Y(ref[0])
        parts.append(f'<line x1="{L}" y1="{yr:.1f}" x2="{w-R}" y2="{yr:.1f}" '
                     f'stroke="#d2991d" stroke-width="1" stroke-dasharray="6 4" opacity="0.75"/>')
        if not compact:
            parts.append(f'<text x="{w-R-4}" y="{yr-5:.1f}" fill="#d2991d" font-size="10" '
                         f'text-anchor="end">{ref[1]}</text>')

    if f_ext:
        x0, x1 = X(n_hist - 1), X(total_n - 1)
        cur_v = info["cur"]
        med, low_v, hi_v = _clip(f_ext[0]), _clip(f_ext[1]), _clip(f_ext[2])
        y0, y_med, y_low, y_hi = Y(cur_v), Y(med), Y(low_v), Y(hi_v)
        parts.append(f'<polygon points="{x0:.1f},{y0:.1f} {x1:.1f},{y_hi:.1f} '
                     f'{x1:.1f},{y_low:.1f} {x0:.1f},{y0:.1f}" fill="rgba(240,136,62,0.16)"/>')
        parts.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y_med:.1f}" '
                     f'stroke="#f0883e" stroke-width="1.6" stroke-dasharray="5 4"/>')
        parts.append(f'<circle cx="{x1:.1f}" cy="{y_med:.1f}" r="3" fill="#f0883e"/>')
        parts.append(f'<line x1="{x0:.1f}" y1="{T}" x2="{x0:.1f}" y2="{T+ph}" '
                     f'stroke="#30363d" stroke-width="1" stroke-dasharray="3 3"/>')
        parts.append(f'<text x="{(x0+x1)/2:.1f}" y="{T+11}" fill="#f0883e" '
                     f'font-size="10" text-anchor="middle">未来6期(统计外推)</text>')

    if ma_pts:
        parts.append(f'<polyline points="{ma_pts}" fill="none" stroke="#8b949e" '
                     f'stroke-width="1" stroke-dasharray="4 3" opacity="0.65"/>')
    parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" '
                 f'stroke-width="{1.3 if compact else 1.8}"/>')

    cx, cy = X(n_hist - 1), Y(px_ys[-1])
    parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{4 if compact else 6}" '
                 f'fill="rgba(240,246,252,0.18)"/>')
    parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{2 if compact else 3}" fill="#f0f6fc"/>')

    # 底轴基线: 让刻度有依附, 不然悬空的日期读起来没参照
    parts.append(f'<line x1="{L}" y1="{T+ph:.1f}" x2="{w-R}" y2="{T+ph:.1f}" '
                 f'stroke="#30363d" stroke-width="1"/>')

    # X 轴刻度: 紧凑图 5 个 / 大图 7 个, 全部精确到月(YYYY-MM)。
    # 首尾用 start/end 锚点, 避免第一个标签向左溢出压到 Y 轴刻度文字上。
    n_tick = 5 if compact else 7
    for k in range(n_tick):
        frac = k / (n_tick - 1)
        xi = (n_hist - 1) * frac
        i = idx[min(len(idx) - 1, int(round(xi)))]
        x = X(xi)
        anchor = "start" if k == 0 else ("end" if k == n_tick - 1 else "middle")
        parts.append(f'<line x1="{x:.1f}" y1="{T+ph:.1f}" x2="{x:.1f}" y2="{T+ph+3:.1f}" '
                     f'stroke="#484f58" stroke-width="1"/>')
        parts.append(f'<text x="{x:.1f}" y="{h-5}" fill="#8b949e" '
                     f'font-size="{fs}" text-anchor="{anchor}">{xs[i].strftime("%Y-%m")}</text>')
    parts.append('</svg>')
    return "".join(parts)


# ══════════════════════════════════════════════════════════════
# 五、HTML 片段渲染
# ══════════════════════════════════════════════════════════════
CYCLES_CSS = """
<style>
.cyc-wrap { font-family: -apple-system,'Segoe UI','Microsoft YaHei',sans-serif; }
.cyc-topbar { background:#161b22; border:1px solid #30363d; border-radius:12px;
  padding:14px 20px; margin-bottom:16px; }
.cyc-topbar .t { font-size:15px; font-weight:600; color:#e6edf3; margin-bottom:4px; }
.cyc-topbar .s { font-size:12px; color:#8b949e; line-height:1.7; }
.cyc-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:20px; }
.cyc-mini { background:#161b22; border:1px solid #30363d; border-radius:10px; padding:12px 14px; }
.cyc-mini:hover { border-color:#58a6ff; }
.cyc-mini .nm { font-size:13px; font-weight:600; color:#e6edf3; margin-bottom:2px; }
.cyc-mini .ind { font-size:10px; color:#8b949e; margin-bottom:8px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.cyc-mini .val { font-size:19px; font-weight:600; color:#e6edf3; font-variant-numeric:tabular-nums; }
.cyc-mini .val .u { font-size:11px; color:#8b949e; font-weight:400; margin-left:3px; }
.cyc-mini .ph { font-size:11px; font-weight:500; margin:6px 0 8px; }
.cyc-bar { position:relative; height:6px; background:#21262d; border-radius:3px; overflow:hidden; }
.cyc-bar.big { height:8px; }
.cyc-bar i { position:absolute; top:0; left:0; height:100%; border-radius:3px; }
.cyc-barlab { font-size:10px; color:#8b949e; text-align:right; margin-bottom:4px; }
.cyc-barlab.big { font-size:11px; color:#adbac7; margin-bottom:5px; }
.cyc-card { background:#161b22; border:1px solid #30363d; border-radius:12px;
  padding:16px 20px 14px; margin-bottom:16px; }
.cyc-hd { display:flex; justify-content:space-between; align-items:flex-start;
  flex-wrap:wrap; gap:10px; margin-bottom:10px; }
.cyc-hd .lft { display:flex; flex-direction:column; gap:3px; }
.cyc-hd .nm { font-size:15px; font-weight:600; color:#e6edf3; }
.cyc-hd .nm .tag { font-size:10px; font-weight:500; padding:2px 7px; border-radius:4px;
  margin-left:8px; vertical-align:middle; border:1px solid; }
.cyc-hd .src { font-size:11px; color:#8b949e; }
.cyc-hd .rgt { display:flex; gap:24px; align-items:center; }
.cyc-hd .metric { text-align:right; }
.cyc-hd .metric .k { font-size:10px; color:#8b949e; }
.cyc-hd .metric .v { font-size:17px; font-weight:600; color:#e6edf3; font-variant-numeric:tabular-nums; }
.cyc-combo { margin:10px 0 14px; }
.cyc-ind-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
.cyc-ind { background:#0d1117; border:1px solid #21262d; border-radius:9px;
  padding:10px 12px 8px; display:flex; flex-direction:column; }
.cyc-ind .ihd { display:flex; justify-content:space-between; align-items:baseline;
  gap:8px; margin-bottom:5px; }
.cyc-ind .inm { font-size:12px; font-weight:600; color:#e6edf3;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.cyc-ind .ipct { font-size:12px; font-weight:600; font-variant-numeric:tabular-nums; flex:none; }
.cyc-ind .ival { font-size:15px; font-weight:600; color:#e6edf3;
  font-variant-numeric:tabular-nums; margin-bottom:3px; }
.cyc-ind .ival .iu { font-size:10px; color:#8b949e; font-weight:400; margin-left:2px; }
.cyc-ind .iph { font-size:10.5px; font-weight:500; margin-bottom:8px; }
.cyc-ind .iph .dv { color:#6e7681; font-weight:400; }
.cyc-ind .ibox { margin-top:auto; }
.cyc-ind .isrc { font-size:9.5px; color:#6e7681; margin-top:6px; line-height:1.45; }
.cyc-ind .isrc .warn { color:#d2991d; }
.cyc-fc { display:grid; grid-template-columns:repeat(2,1fr); gap:10px; margin-top:14px; }
.cyc-fc .box { background:#0d1117; border:1px solid #21262d; border-radius:8px; padding:9px 12px; }
.cyc-fc .box .k { font-size:10px; color:#8b949e; margin-bottom:3px; }
.cyc-fc .box .v { font-size:14px; font-weight:600; font-variant-numeric:tabular-nums; }
.cyc-fc .box .d { font-size:10px; color:#6e7681; margin-top:3px; }
.cyc-logic { margin-top:12px; font-size:12.5px; line-height:1.75; color:#adbac7;
  border-left:3px solid #30363d; padding-left:12px; }
</style>
"""


def _ph_color(pct):
    if pct <= 0.25:
        return "#3fb950"
    if pct >= 0.75:
        return "#f85149"
    return "#58a6ff"


def _stale_warn(last_date, days=45):
    try:
        d = datetime.strptime(last_date, "%Y-%m-%d")
        return (datetime.now() - d).days > days
    except Exception:
        return False


def render_fragment(rows, updated):
    minis, cards = [], []
    for r in rows:
        agg = r["agg"]
        color = _ph_color(agg["pct_avg"])
        f6, f12 = agg.get("f6"), agg.get("f12")

        # 分歧度越大, 综合位置越不可信 —— 直接把它标出来, 而不是假装一个数就代表整个周期
        div_bad = agg["pct_std"] > 0.25
        div_txt = (f'<span style="color:#d2991d">±{agg["pct_std"]*100:.0f}pp 分歧大, 位置参考性低</span>'
                   if div_bad else f'分歧 ±{agg["pct_std"]*100:.0f}pp')

        # ---- 顶部 mini ----
        minis.append(
            f'<div class="cyc-mini">'
            f'<div class="nm">{r["name"]}</div>'
            f'<div class="ind" title="{r["tag"]}">{r["tag"]} · {len(r["indicators"])} 指标</div>'
            f'<div class="val">{agg["pct_avg"]*100:.0f}<span class="u">% 综合位置</span></div>'
            f'<div class="ph" style="color:{color}">{agg["phase"]}</div>'
            f'<div class="cyc-barlab">{div_txt}</div>'
            f'<div class="cyc-bar"><i style="width:{agg["pct_avg"]*100:.0f}%;background:{color}"></i></div>'
            f'</div>')

        # ---- 三个并列指标 ----
        ind_html = []
        for ind in r["indicators"]:
            st = ind["stats"]
            ic = _ph_color(st["pct"])
            # 对本身就是百分比(如房价同比)的序列,『偏离%』没有意义(接近0时会放大成±几百%),
            # 改成显示与长期均线的绝对差(pp)
            if ind["unit"] == "%":
                dev_txt = f'均线 {st.get("mav", 0):+.2f}%'
            else:
                dev_txt = f'偏离均线 {st["dev"]*100:+.1f}%'
            warn = _stale_warn(ind["last_date"])
            note = f' · {ind["note"]}' if ind.get("note") else ""
            svg = svg_chart_series(ind)
            ind_html.append(
                f'<div class="cyc-ind">'
                f'<div class="ihd"><span class="inm" title="{ind["name"]}">{ind["name"]}</span>'
                f'<span class="ipct" style="color:{ic}">分位 {st["pct"]*100:.0f}%</span></div>'
                f'<div class="ival">{_fmt(st["cur"], ind["unit"])}'
                f'<span class="iu">{ind["unit"] or "指数"}</span></div>'
                f'<div class="iph" style="color:{ic}">{st["phase"]}'
                f'<span class="dv"> · {dev_txt}</span></div>'
                f'<div class="ibox">{svg}</div>'
                f'<div class="isrc">{ind["source"]}'
                f'<br>{"<span class=\'warn\'>⚠ 数据滞后</span> " if warn else ""}'
                f'截至 {ind["last_date"]} · {ind["n_raw"]}个{ind["freq"] == "M" and "月" or "周"}样本{note}</div>'
                f'</div>')

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

        main_nm = next((i["name"] for i in r["indicators"] if i.get("main")), r["indicators"][0]["name"])

        cards.append(
            f'<div class="cyc-card">'
            f'<div class="cyc-hd">'
            f'<div class="lft"><div class="nm">{r["name"]}'
            f'<span class="tag" style="color:{color};border-color:{color}88;background:{color}18">'
            f'{agg["phase"]}</span></div>'
            f'<div class="src">{len(r["indicators"])} 个指标并列 · '
            f'综合位置 = 各指标历史分位的均值 · 分歧度 = 分位标准差'
            + ('<span style="color:#d2991d"> · ⚠ 指标分歧过大, 综合位置参考性低, 请看下方各指标</span>'
               if div_bad else '') + '</div></div>'
            f'<div class="rgt">'
            f'<div class="metric"><div class="k">综合位置</div>'
            f'<div class="v" style="color:{color}">{agg["pct_avg"]*100:.0f}%</div></div>'
            f'<div class="metric"><div class="k">指标分歧</div>'
            f'<div class="v" style="color:{"#d2991d" if div_bad else "#e6edf3"}">'
            f'±{agg["pct_std"]*100:.0f}pp</div></div>'
            f'</div></div>'
            f'<div class="cyc-combo">'
            f'<div class="cyc-barlab big">低位 ← 综合位置 {agg["pct_avg"]*100:.0f}% → 高位</div>'
            f'<div class="cyc-bar big">'
            f'<i style="width:{agg["pct_avg"]*100:.0f}%;background:{color}"></i></div></div>'
            f'<div class="cyc-ind-grid">{"".join(ind_html)}</div>'
            f'<div class="cyc-fc">{fc_txt(f6, f"未来半年 · 主指标「{main_nm}」历史相似位置后")}'
            f'{fc_txt(f12, f"未来一年 · 主指标「{main_nm}」历史相似位置后")}</div>'
            f'<div class="cyc-logic">{r["logic"]}</div>'
            f'</div>')

    return (CYCLES_CSS +
            '<div class="cyc-wrap">'
            f'<div class="cyc-topbar"><div class="t">产业周期观察 · 多指标并列</div>'
            f'<div class="s">每个周期用 <b>3 个独立指标</b>交叉验证, 单指标噪音大、易被个体事件扭曲。'
            f'<b>综合位置</b>=各指标历史分位的均值; <b>分歧度</b>=分位标准差。'
            f'<b>分歧度 &gt; 25pp 时综合位置本身就不成立</b> —— 说明这几条产业链已经割裂, '
            f'这时应该直接看下面三个指标各自的读数。'
            f'<br>前瞻统计取主指标「历史上处于相同分位时」其后 6/12 期的后验分布 —— 这是历史统计, '
            f'不是预测承诺。更新 {updated}</div></div>'
            f'<div class="cyc-grid">{"".join(minis)}</div>'
            f'{"".join(cards)}'
            f'<div class="cyc-topbar"><div class="s">'
            f'<b>数据说明:</b> 全部指标取自公开数据源(东方财富/新浪财经/国家统计局/农业农村部/LME伦交所/'
            f'波罗的海交易所/上期所/大商所/郑商所/广期所), 由 akshare 抓取, 未做人工调整。'
            f'每个指标都标了数据源与截止日期, 页面出现橙色「⚠ 数据滞后」即超过 45 天未更新。'
            f'<br><b>已知局限:</b> ①样本最长约 700 周(≈13.5 年), 对 18-20 年的房地产周期和 '
            f'10-12 年的航运周期而言不足一个完整周期; '
            f'②LME 铜库存滞后约 3 个月; '
            f'③多晶硅期货 2024 年底才上市(仅 93 周), 工业硅 2022 年底上市, 其分位参考价值有限; '
            f'④BDI/BCI/BPI 是高频现货运价, 衡量的是<b>运价水平</b>而非造船产能周期的位置 —— '
            f'真正的「新船订单簿/船队运力比」是 Clarksons 付费数据, 免费源拿不到; '
            f'⑤存储周期用的是<b>原厂股价</b>(美光 + 费半 + A股板块), 不是「三大原厂资本开支同比」—— '
            f'后者要逐季读三星/海力士/美光财报, 没有免费实时接口, 也无 DRAM/NAND 现货价免费源; '
            f'⑥用价格水平的绝对分位衡量周期位置, 会受成本推动(油价/煤价)和长期结构变化污染, '
            f'所以同一周期内三个指标的分歧本身就是信息, 不要只看均值。'
            f'<br><b>免责:</b> 周期位置为滚动历史分位, 属描述性统计, 与个股/指数买卖信号无关, 不构成投资建议。'
            f'</div></div>'
            '</div>')


def svg_chart_series(ind):
    """从缓存重建时用: ind 里可能只有 series 没有 SVG"""
    if ind.get("svg"):
        return ind["svg"]
    s = pd.Series([v for _, v in ind["series"]],
                  index=pd.to_datetime([d for d, _ in ind["series"]]))
    return svg_chart(s, ind["stats"], ind["unit"], ind["ma"], ind["ref"],
                     w=380, h=168, compact=True, color=_ph_color(ind["stats"]["pct"]))


# ══════════════════════════════════════════════════════════════
# 六、主流程
# ══════════════════════════════════════════════════════════════
def build_cycle_rows():
    """联网采集全部指标 → rows"""
    rows, failed = [], []
    for cfg in CYCLES:
        inds = []
        for ic in cfg["indicators"]:
            try:
                res = FE[ic["fetch"]]()
                s_raw, src, unit = res[0], res[1], res[2]
                over = res[3] if len(res) > 3 and isinstance(res[3], dict) else {}
                name = over.get("name", ic["name"])
                ref = over.get("ref", ic["ref"])
                note = over.get("note", ic.get("note", ""))
                s = resample(s_raw, ic["freq"], ic["agg"])
                if ic.get("pos", True):
                    s = s[s > 0]
                if len(s) > MAX_KEEP:
                    s = s.iloc[-MAX_KEEP:]
                if len(s) < 30:
                    raise ValueError(f"样本过短({len(s)})")
                info = analyze(s, ic["freq"], ic["ma"])
                inds.append(dict(
                    k=ic["k"], name=name, unit=unit, source=src, note=note,
                    freq=ic["freq"], ma=ic["ma"], ref=ref, main=ic.get("main", False),
                    last_date=s.index[-1].strftime("%Y-%m-%d"), n_raw=len(s),
                    stats=info, svg=svg_chart(s, info, unit, ic["ma"], ref,
                                              w=380, h=168, compact=True,
                                              color=_ph_color(info["pct"])),
                    series=[[d.strftime("%Y-%m-%d"), round(float(v), 4)]
                            for d, v in zip(s.index, s.values)]))
                print(f"  [OK] {cfg['name']:<9} {name:<12} {len(s):>4}点  "
                      f"{'':<22} 分位{info['pct']*100:>5.1f}%  {info['phase']}")
            except Exception as e:
                print(f"  [FAIL] {cfg['name']} / {ic['name']}: {type(e).__name__}: {str(e)[:90]}")
        if not inds:
            failed.append(cfg["key"])
            continue
        pcts = np.array([i["stats"]["pct"] for i in inds])
        main_ind = next((i for i in inds if i.get("main")), inds[0])
        avg = float(pcts.mean())
        rows.append(dict(
            key=cfg["key"], name=cfg["name"], tag=cfg["tag"], logic=cfg["logic"],
            indicators=inds,
            agg=dict(pct_avg=avg, pct_std=float(pcts.std()),
                     phase=phase_of(avg, main_ind["stats"]["chg"]),
                     f6=main_ind["stats"].get("f6"), f12=main_ind["stats"].get("f12"),
                     main=main_ind["name"]),
        ))
        print(f"  => {cfg['name']:<9} 综合位置 {avg*100:5.1f}%  分歧 ±{pcts.std()*100:.1f}pp  "
              f"{phase_of(avg, main_ind['stats']['chg'])}")
    return rows, failed


def rebuild_rows(cached):
    rows = []
    for c in cached:
        inds = []
        for ind in c["indicators"]:
            s = pd.Series([v for _, v in ind["series"]],
                          index=pd.to_datetime([d for d, _ in ind["series"]]))
            ind = dict(ind)
            ind["svg"] = svg_chart(s, ind["stats"], ind["unit"], ind["ma"], ind["ref"],
                                   w=380, h=168, compact=True,
                                   color=_ph_color(ind["stats"]["pct"]))
            inds.append(ind)
        rows.append(dict(c, indicators=inds))
    return rows


def main():
    force = "--force" in sys.argv
    stale_days = 7.0
    for _a in sys.argv[1:]:
        if _a.startswith("--if-stale="):
            try:
                stale_days = float(_a.split("=", 1)[1])
            except ValueError:
                pass

    if not force and stale_days > 0 and OUT_JSON.exists():
        try:
            _meta = json.loads(OUT_JSON.read_text(encoding="utf-8"))
            _ver_ok = _meta.get("ver") == SCHEMA_VER
            _prev = datetime.strptime(_meta["updated"], "%Y-%m-%d %H:%M")
            _age = (datetime.now() - _prev).total_seconds() / 86400.0
            if _ver_ok and _age < stale_days:
                n_ind = sum(len(c["indicators"]) for c in _meta["cycles"])
                print(f"[skip] 周期数据 {_age:.1f} 天前已采集, 未满 {stale_days:g} 天, "
                      f"跳过联网采集, 仅用缓存刷新渲染。强制采集请加 --force")
                try:
                    _rows = rebuild_rows(_meta["cycles"])
                    if _rows:
                        OUT_HTML.write_text(render_fragment(_rows, _meta["updated"]),
                                            encoding="utf-8")
                        print(f"已用缓存数据刷新页面: {OUT_HTML.name} "
                              f"({OUT_HTML.stat().st_size/1024:.0f}KB, "
                              f"{len(_rows)}个周期/{n_ind}个指标)")
                        return 0
                except Exception as _ce:
                    print(f"[warn] 缓存重建失败({_ce}), 改为完整重新采集")
            elif not _ver_ok:
                print("[info] 缓存结构版本过旧(单指标版), 强制重新采集…")
        except Exception:
            pass

    print("开始采集产业周期指标 (多指标并列版)…")
    rows, failed = build_cycle_rows()
    if not rows:
        print("全部采集失败, 保留原有页面。")
        return 1

    updated = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_ind = sum(len(r["indicators"]) for r in rows)
    payload = dict(
        ver=SCHEMA_VER, updated=updated, failed=failed,
        cycles=[{**{k: v for k, v in r.items() if k != "indicators"},
                 "indicators": [{k: v for k, v in i.items() if k != "svg"}
                                for i in r["indicators"]]} for r in rows])
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    OUT_HTML.write_text(render_fragment(rows, updated), encoding="utf-8")
    print(f"\n已写出: {OUT_JSON.name} ({OUT_JSON.stat().st_size/1024:.0f}KB)  "
          f"{OUT_HTML.name} ({OUT_HTML.stat().st_size/1024:.0f}KB)  "
          f"{len(rows)}个周期 / {n_ind}个指标")
    if failed:
        print(f"失败周期: {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
