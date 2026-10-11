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
import os, sys, json, traceback, time
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

SCHEMA_VER = 5          # 结构版本; 与缓存不符则强制重采
                        # (v3: 趋势型 R² 判据; v4: 增速序列前瞻改差值口径;
                        #  v5: 指标扩充到 12 周期/43 指标 + 锂·挖机·面板·煤炭 + 两个美林时钟)
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
    """京沪 新房+二手 住宅价格指数同比均值。
    ⚠ akshare 的 macro_china_new_house_price 名字带『70城』, 实际只返回北京/上海两市
    (每月恰好 2 条 × 188 个月 = 376 行), 不是真·70 城口径 —— 源标签别写成 70 城。
    新房受限价管制会钝化, 二手更贴市场, 取两者均值比只看新房稳。"""
    df = ak.macro_china_new_house_price()
    cols = [c for c in ("新建商品住宅价格指数-同比", "二手住宅价格指数-同比")
            if c in df.columns]
    if not cols:
        raise ValueError("房价接口缺少同比列")
    sub = df[["日期"] + cols].copy()
    for c in cols:
        sub[c] = pd.to_numeric(sub[c], errors="coerce")
    g = sub.dropna(subset=cols).groupby("日期")[cols].mean().mean(axis=1) - 100.0
    g.index = pd.to_datetime(g.index)      # 该接口日期是字符串, 必须显式转
    return (g.sort_index(),
            "国家统计局·京沪 新房+二手 住宅价格指数同比(免费接口实仅含北京/上海两市)",
            "%")


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


# ══════════════════════════════════════════════════════════════
# 一之二、2026-10 改版新增的采集函数
# ══════════════════════════════════════════════════════════════
# ---- 煤炭 ----
def f_jm():
    return _fut("JM0", "大商所·焦煤期货主力连续")


def f_jc():
    return _fut("J0", "大商所·焦炭期货主力连续")


# ---- 锂 ----
def f_lc():
    return _fut("LC0", "广期所·碳酸锂期货主力连续", "元/吨")


# ---- 化工补充 ----
def f_sc():
    return _fut("SC0", "上期所(INE)·原油期货主力连续", "元/桶")


def f_pp():
    return _fut("PP0", "大商所·聚丙烯期货主力连续")


# ---- A股: 用于「没有免费现货价」的周期, 用龙头股价做市场定价代理 ----
def _a_share(sym, name, note=""):
    """周线前复权收盘价。
    主源东财 stock_zh_a_hist(直接给周线), 但它连续请求时会偶发 RemoteDisconnected;
    降级用新浪 stock_zh_a_daily(日线前复权)自行汇总成周线 —— 历史更长(可到 2000 年前后),
    实测在东财不通的环境里也能拿到。两条路都失败才抛错。"""
    last = None
    for i in range(2):
        try:
            df = ak.stock_zh_a_hist(symbol=sym, period="weekly",
                                    start_date="20100101", end_date=TODAY, adjust="qfq")
            dcol = "日期" if "日期" in df.columns else df.columns[0]
            ccol = "收盘" if "收盘" in df.columns else _pick_col(df, "收盘")
            s = _series(df[dcol], df[ccol])
            if len(s) < 30:
                raise ValueError("东财周线样本过短(%d)" % len(s))
            return s, f"{name} {sym} · 周线前复权(东财)", "元", {"note": note}
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
    try:
        code = ("sh" if str(sym).startswith("6") else "sz") + str(sym)
        df = ak.stock_zh_a_daily(symbol=code, adjust="qfq")
        dcol = "date" if "date" in df.columns else df.columns[0]
        ccol = "close" if "close" in df.columns else _pick_col(df, "close")
        s = _series(df[dcol], df[ccol])
        s = s[s.index >= pd.Timestamp("2010-01-01")]
        s = s.resample("W-FRI").last().dropna()
        if len(s) < 30:
            raise ValueError("新浪日线汇总后样本过短(%d)" % len(s))
        return s, f"{name} {sym} · 周线前复权(新浪日线汇总)", "元", {"note": note}
    except Exception as e:
        raise (last if last is not None else e)


def f_boe():
    """京东方A —— 面板周期代理(面板厂股价是市场对面板报价周期的提前定价)"""
    return _a_share("000725", "京东方A", "面板龙头")


def f_tcl():
    return _a_share("000100", "TCL科技", "面板+光伏硅片")


def f_sany():
    """三一重工 —— 挖机周期代理"""
    return _a_share("600031", "三一重工", "挖机龙头")


def f_hengyi():
    return _a_share("601100", "恒立液压", "挖机油缸/泵阀")


def f_ganfeng():
    return _a_share("002460", "赣锋锂业", "锂资源龙头")


def f_tianqi():
    return _a_share("002466", "天齐锂业", "锂资源")


def f_cssc():
    """中国船舶 —— 造船订单簿的【市场定价代理】。
    你要的『全球新船订单簿 / 现有船队运力』是 Clarksons 付费数据, 免费源没有;
    而船厂股价 = 市场对在手订单量 × 船价 × 交付周期的提前定价, 与订单簿高度同向。"""
    return _a_share("600150", "中国船舶", "造船龙头·订单簿代理")


def f_xcmg():
    return _a_share("000425", "徐工机械", "挖机/起重机械")


def f_tianma():
    return _a_share("000050", "深天马A", "中小尺寸面板")


# ---- 上期所铜库存 ----
def f_shfe_cu_inv():
    """上期所铜库存(吨, 日度)。
    ⚠ 实测只回溯约 3 个月(2026-07 起 67 个交易日), 周化后仅 15 点 ——
    算不了历史分位, 所以【当前未挂进周期表】, 留着等接口放宽再用。"""
    df = ak.futures_inventory_em(symbol="沪铜")
    dcol = "日期" if "日期" in df.columns else df.columns[0]
    ccol = "库存" if "库存" in df.columns else _pick_col(df, "库存")
    s = _series(df[dcol], df[ccol])
    if len(s.resample("W-FRI").last().dropna()) < 52:
        raise ValueError("上期所铜库存回溯仅约3个月, 样本不足以算分位")
    return s, "上期所·铜库存(东财)", "吨", {}


# ---- 扣除通胀: 名义价 → 实际价 ----
_CPI_IDX = None


def _cpi_index():
    """中国 CPI 定基指数(首期=100), 由月度同比链式推算: 月环比 ≈ (1+同比)^(1/12)。
    用来把名义价格折算成『不变价』, 使 2013 年和 2026 年的价格可比。
    分位对定基年份不敏感(整体乘常数不改变排序), 所以选哪年做基期无所谓。"""
    global _CPI_IDX
    if _CPI_IDX is None:
        s, _, _, _ = f_cpi()
        m = (1.0 + s / 100.0) ** (1.0 / 12.0)
        lvl = m.cumprod()
        _CPI_IDX = lvl / lvl.iloc[0] * 100.0
    return _CPI_IDX


def _deflate(s):
    """名义序列 ÷ CPI 定基指数 → 实际序列(按 CPI 首期不变价)"""
    lvl = _cpi_index()
    idx = s.index.union(lvl.index)
    lv2 = lvl.reindex(idx).ffill().bfill()
    return (s / lv2.reindex(s.index)).dropna()


def _real(fn, extra_note=""):
    """把一个名义价格采集函数包成实际价版本。
    注意采集函数可能返回 3 元组(期货类)也可能返回 4 元组(带 meta), 两种都要吃。"""
    res = fn()
    s, src, unit = res[0], res[1], res[2]
    meta = res[3] if len(res) > 3 and isinstance(res[3], dict) else {}
    r = _deflate(s)
    if len(r) < 30:
        raise ValueError("折算后样本过短(%d)" % len(r))
    meta = dict(meta)
    meta.pop("name", None)
    return r, src + " · 已按中国CPI折算为实际价(不变价)", unit, meta


def f_sc_real():
    s, src, unit, meta = _real(f_sc)
    return s, src, unit, {"note": "扣除通胀后的实际油价(元/桶, 不变价)"}


def f_lc_real():
    s, src, unit, meta = _real(f_lc)
    return s, src, unit, {"note": "扣除通胀后的实际锂价(元/吨, 不变价)"}


# ---- 派生序列: 比价 / 比值 ----
def _fut_series(sym):
    df = ak.futures_main_sina(symbol=sym, start_date="20000101", end_date=TODAY)
    return _series(df["日期"], df["收盘价"])


def _ratio(sa, sb):
    """两个不同频率序列相除: 按日期内对齐(取交集)后相除"""
    a, b = sa.align(sb, join="inner")
    r = (a / b).dropna()
    return r[~np.isinf(r)]


def f_pig_grain():
    """猪粮比 = 生猪期货主力(元/吨) / 玉米期货主力(元/吨)。
    发改委猪粮比价用的是出场价/玉米批发价, 免费接口没有; 用两个期货主力价的比值做代理,
    量级和历史形态都对得上(5:1 亏损、6:1 盈亏平衡、9:1 高盈利)。"""
    s = _ratio(_fut_series("LH0"), _fut_series("C0"))
    return s, "大商所·猪粮比(生猪期货主力 ÷ 玉米期货主力)", "倍", {}


def f_spread_pta():
    """油化工利润代理 = PTA(元/吨) ÷ 原油(元/桶)。
    真正的产品-原料价差要减去 PX, 而 PX 没有免费历史接口; PTA/原油这个比价
    是产业链上最常用的『化工利润 vs 原油成本』替代指标"""
    s = _ratio(_fut_series("TA0"), _fut_series("SC0"))
    return s, "PTA ÷ 原油(INE) · 油化工利润代理", "倍", {}


def f_spread_ma():
    """煤化工利润代理 = 甲醇(元/吨) ÷ 焦煤(元/吨)"""
    s = _ratio(_fut_series("MA0"), _fut_series("JM0"))
    return s, "甲醇 ÷ 焦煤 · 煤化工利润代理", "倍", {}


# ---- 宏观: 中国 ----
def f_m2():
    df = ak.macro_china_money_supply()
    idx = pd.to_datetime(df["月份"].astype(str).str.replace("年", "-", regex=False)
                         .str.replace("月份", "", regex=False), format="%Y-%m")
    return _series(idx, df["货币和准货币(M2)-同比增长"]), "人民银行·M2 同比", "%", {}


def f_m1():
    df = ak.macro_china_money_supply()
    idx = pd.to_datetime(df["月份"].astype(str).str.replace("年", "-", regex=False)
                         .str.replace("月份", "", regex=False), format="%Y-%m")
    return _series(idx, df["货币(M1)-同比增长"]), "人民银行·M1 同比", "%", {}


def f_credit():
    """累计新增人民币贷款同比 —— 改良版美林时钟的『信用』轴。
    用累计值而不是当月值: 当月新增信贷季末冲量、年初回落, 噪音极大, 累计同比才是趋势口径。
    (社融存量增速是更标准的信用指标, 但该免费接口滞后到 2026-04, 会算错当前象限)"""
    df = ak.macro_china_new_financial_credit()
    idx = pd.to_datetime(df["月份"].astype(str).str.replace("年", "-", regex=False)
                         .str.replace("月份", "", regex=False), format="%Y-%m")
    return _series(idx, df["累计-同比增长"]), "人民银行·累计新增人民币贷款同比", "%", {}


def f_shrzgm():
    """社融增量滚动 12 月同比。数据只到 2026-04(源滞后), 仅作参考备用。"""
    df = ak.macro_china_shrzgm()
    idx = pd.to_datetime(df["月份"].astype(str), format="%Y%m")
    v = pd.to_numeric(df["社会融资规模增量"], errors="coerce")
    s = pd.Series(v.values, index=idx).sort_index()
    r = s.rolling(12).sum()
    yoy = (r / r.shift(12) - 1.0) * 100
    return yoy.dropna(), "人民银行·社融增量滚动12月同比", "%", {}


def f_lpr5():
    """5 年期以上贷款基准利率 = 2019-08 之后的 LPR5Y + 之前的『5年以上贷款基准利率』拼接。
    这是按揭利率的锚, 拼起来有 35 年历史, 足够算长周期位置。"""
    df = ak.macro_china_lpr().sort_values("TRADE_DATE")
    idx = pd.to_datetime(df["TRADE_DATE"])
    lpr = pd.to_numeric(df["LPR5Y"], errors="coerce")
    base = pd.to_numeric(df["RATE_2"], errors="coerce")
    v = lpr.copy()
    m = (idx < pd.Timestamp("2019-08-20")).values
    v[m] = base[m]
    return (pd.Series(np.asarray(v, dtype=float), index=idx).dropna().sort_index(),
            "人民银行·5年期以上贷款基准利率(LPR5Y 及 2019-08 前的贷款基准利率)", "%", {})


def f_cpi():
    df = ak.macro_china_cpi()
    idx = pd.to_datetime(df["月份"].astype(str).str.replace("年", "-", regex=False)
                         .str.replace("月份", "", regex=False), format="%Y-%m")
    return _series(idx, df["全国-同比增长"]), "国家统计局·CPI 同比", "%", {}


def f_elec():
    """全社会用电量同比 —— 电力/动力煤需求的同步-领先指标"""
    df = ak.macro_china_society_electricity()
    idx = pd.to_datetime(df["统计时间"].astype(str).str.replace(".", "-", regex=False) + "-01")
    return _series(idx, df["全社会用电量同比"]), "国家能源局·全社会用电量同比", "%", {}


# ---- 宏观: 美国(东财 datacenter 比 akshare 的金十系列新鲜得多) ----
def _em_usa(ind_id, label, unit):
    """akshare 的 macro_usa_* 大多来自金十, 已经停在 2025-09;
    东财 datacenter 的同名指标还是新鲜的(ISM PMI 到 2026-09)。"""
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {"reportName": "RPT_ECONOMICVALUE_USA", "columns": "ALL",
              "filter": '(INDICATOR_ID="%s")' % ind_id,
              "sortColumns": "REPORT_DATE", "sortTypes": "-1",
              "source": "WEB", "client": "WEB", "pageSize": "600"}
    r = requests.get(url, params=params, timeout=25)
    rows = r.json()["result"]["data"]
    df = pd.DataFrame(rows)
    idx = pd.to_datetime(df["REPORT_DATE"])
    v = pd.to_numeric(df["VALUE"], errors="coerce")
    s = pd.Series(v.values, index=idx).dropna().sort_index()
    if len(s) < 30:
        raise ValueError("东财美国宏观样本过短(%d)" % len(s))
    return s, label, unit, {}


def f_ism():
    return _em_usa("EMG00002790", "美国 ISM 制造业 PMI(东财 datacenter, 2008 起)", "")


def f_usa_cpi():
    return _em_usa("EMG00000733", "美国 CPI 年率(东财 datacenter, 2008 起)", "%")


def f_usa_unemp():
    return _em_usa("EMG00001039", "美国失业率(东财 datacenter, 2008 起)", "%")


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
    # ---- 2026-10 改版新增 ----
    "jm": f_jm, "jc": f_jc, "lc": f_lc, "sc": f_sc, "pp": f_pp,
    "sc_real": f_sc_real, "lc_real": f_lc_real,
    "boe": f_boe, "tcl": f_tcl, "sany": f_sany, "hengyi": f_hengyi,
    "ganfeng": f_ganfeng, "tianqi": f_tianqi,
    "cssc": f_cssc, "xcmg": f_xcmg, "tianma": f_tianma,
    "shfe_cu_inv": f_shfe_cu_inv,
    "pig_grain": f_pig_grain, "spread_pta": f_spread_pta, "spread_ma": f_spread_ma,
    "m2": f_m2, "m1": f_m1, "credit": f_credit, "lpr5": f_lpr5,
    "cpi": f_cpi, "elec": f_elec,
    "ism": f_ism, "usa_cpi": f_usa_cpi, "usa_unemp": f_usa_unemp,
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
    # ══════════════════════════════════════════════════════════
    dict(key="shipping", name="航运周期", tag="10-12 年大周期",
         logic="造船是重资产长周期, 新船下单到交付 2-3 年, 供给调整极慢, 需求一波动就是 3-5 年大行情。"
               "BDI 是干散货综合运价, BCI(海岬型/铁矿石煤炭)和 BPI(巴拿马型/粮食)是分船型拆解 —— "
               "三个同涨说明是全局需求回暖, 只有单一船型涨则多半是局部扰动。"
               "<b>关于你列的六个造船指标:</b>『全球新船订单簿/现有船队运力』是 Clarksons 的付费数据, "
               "『新接订单量/手持订单量』只有中国船舶工业行业协会(CANSI)的月度公报有(且网页已改版, 抓不到), "
               "『新船价格指数』同样是 Clarksons, 『二手船成交价 / 二手船 vs 新船价比』在 VesselsValue 付费库里, "
               "『全球拆船量』只有年度口径。这六项<b>没有任何免费实时接口</b>, 所以第 4 个指标改用"
               "<b>中国船舶(600150)股价</b>做订单簿的代理 —— 船厂市值 ≈ 在手订单量 × 船价 × 交付周期, "
               "市场对订单簿的定价通常比协会公报还早。这是代理, 不是原数据, 请勿当原数据读。",
         indicators=[
             I("bdi", "BDI 干散货综合", "bdi", "点", "W", "last", 52, None, main=True),
             I("bci", "BCI 海岬型", "bci", "点", "W", "last", 52, None),
             I("bpi", "BPI 巴拿马型", "bpi", "点", "W", "last", 52, None),
             I("cssc", "中国船舶(订单簿代理)", "cssc", "元", "W", "last", 52, None,
               note="代理指标: 船厂股价≈在手订单×船价"),
         ]),

    # ══════════════════════════════════════════════════════════
    dict(key="memory", name="存储周期", tag="3-4 年 · DRAM/NAND",
         logic="存储是半导体里最纯粹的『蛛网周期』: 原厂建一条产线 2-3 年, 一旦同时扩产就过剩, "
               "一旦同时砍资本开支就缺货。历史上 2018 顶 → 2019 崩 → 2020-21 缺货 → 2022-23 崩 → "
               "2024 起 AI/HBM 拉动新一轮。三个指标互相验证: 费半是全球半导体景气的基准(含存储原厂), "
               "美光是纯 DRAM/NAND 原厂(弹性最大、最能代表存储本身), A股存储板块是国内映射。"
               "<b>关于你列的五个存储指标:</b>『三大原厂资本开支同比』要逐季读三星/海力士/美光财报, "
               "『DRAM / NAND 现货价』在集邦 TrendForce 付费库和 DXI 指数里(免费源已停更), "
               "『渠道库存周转天数』和『合约价-现货价价差』同样是付费研报口径。"
               "这四项<b>没有免费接口</b>, 页面只能继续用原厂股价作代理 —— "
               "股价是市场对资本开支与现货价周期的<b>提前定价</b>, 逻辑上比季报更早, "
               "但它是预期而非现货, 别当成现货价读。",
         indicators=[
             I("sox", "费城半导体指数", "sox", "点", "W", "last", 52, None, main=True),
             I("micron", "美光科技 MU", "micron", "美元", "W", "last", 52, None, note="纯存储原厂"),
             I("cn_mem", "A股存储板块", "cn_mem", "点", "W", "last", 52, None),
         ]),

    # ══════════════════════════════════════════════════════════
    dict(key="copper", name="工业金属(铜)", tag="经济晴雨表",
         logic="铜矿建设周期 5-7 年, 供给端几乎调不动, 所以需求一波动就体现在库存和价格上。"
               "沪铜价格反映国内定价, 沪铝是同板块共振验证(铝走强说明整体需求不弱), "
               "LME 铜库存是海外供需的蓄水池。"
               "<b>库存和价格是反向的</b>: 库存低 → 紧张 → 价格高。所以看这一组时, "
               "价格分位高 + 库存分位低 = 真紧张; 价格高 + 库存也高 = 多半是金融属性推的, 不牢靠。"
               "<b>要的三项库存只拿到了 LME:</b> 上期所和 COMEX 的铜库存免费接口都不给历史 —— "
               "akshare 的 futures_inventory_em(沪铜)只回溯 <b>3 个月</b>(实测 2026-07 起共 67 个交易日, "
               "周化后仅 15 点), 拿它算历史分位毫无意义, 已弃用; 上期所仓单接口只有单日快照。"
               "『铜精矿 TC/RC 加工费』在 SMM/安泰科付费周报里, 『电网投资增速』无免费接口, 同样取不到。",
         indicators=[
             I("cu", "沪铜期货主力", "cu", "元/吨", "W", "last", 52, None, main=True),
             I("al", "沪铝期货主力", "al", "元/吨", "W", "last", 52, None, note="板块共振"),
             I("lme_cu", "LME 铜库存", "lme_cu", "吨", "W", "last", 52, None, note="滞后约3个月"),
         ]),

    # ══════════════════════════════════════════════════════════
    dict(key="chem", name="化工周期", tag="产能游戏",
         logic="重资产、同质化、无壁垒, 全是产能游戏: 赚钱→扩产→过剩→亏到停产→再涨。"
               "这里用三个不同下游的代表品种(纯碱→玻璃/光伏, PTA→聚酯纺织, 甲醇→煤化工/烯烃), "
               "避免单一品种的个体扰动; 再叠上原油(成本端, 已扣除通胀)和 PTA/原油比价(产品-原料价差的代理)。"
               "三者同向下跌到成本附近, 才是行业级出清信号。"
               "<b>关于『行业整体开工率』和『行业库存』:</b> 都是隆众/卓创的付费周度数据, 免费源没有, "
               "只能靠价差间接反映 —— 价差压缩到极致, 就是开工率要下来的意思。",
         indicators=[
             I("sa", "纯碱期货主力", "sa", "元/吨", "W", "last", 52, None, main=True),
             I("ta", "PTA 期货主力", "ta", "元/吨", "W", "last", 52, None),
             I("ma", "甲醇期货主力", "ma", "元/吨", "W", "last", 52, None),
             I("sc_real", "原油实际价(扣通胀)", "sc_real", "元/桶", "W", "last", 52, None,
               note="成本端, 已按中国CPI折算不变价", pos=True),
             I("spread_pta", "PTA÷原油(价差代理)", "spread_pta", "倍", "W", "last", 52, None,
               note="产品-原料价差代理, 真价差需减PX"),
         ]),

    # ══════════════════════════════════════════════════════════
    dict(key="hog", name="生猪周期", tag="蛛网 3-4 年",
         logic="猪从出生到出栏 6 个月, 母猪怀孕 4 个月, 供给调整天然滞后 10 个月。"
               "现货价是当下供需结果, 期货价包含市场对 10 个月后供给的预期, 玉米价格决定养殖成本线, "
               "猪粮比则是官方口径的盈亏刻度(发改委: 5:1 亏损、6:1 盈亏平衡、9:1 高盈利)。"
               "猪价跌 + 玉米稳 = 亏损去化; 猪价跌 + 玉米涨 = 加速出清(底部信号)。"
               "<b>关于『能繁母猪存栏量』(农业农村部月度, 免费接口已失效)、『仔猪价格』和"
               "『二次育肥/出栏体重』(涌益咨询付费周报): 都取不到。</b>"
               "注意本页的猪粮比是<b>生猪期货 ÷ 玉米期货</b>算的, 不是发改委的出场价/批发价口径, "
               "量级和历史形态对得上, 但绝对值会有偏差。",
         indicators=[
             I("hog_spot", "生猪现货价指数", "hog_spot", "", "W", "last", 52, None, main=True),
             I("hog_fut", "生猪期货主力", "hog_fut", "元/吨", "W", "last", 52, None),
             I("corn", "玉米期货主力", "corn", "元/吨", "W", "last", 52, None, note="饲料成本端"),
             I("pig_grain", "猪粮比(期货口径)", "pig_grain", "倍", "W", "last", 52, (6, "6倍=盈亏平衡线")),
         ]),

    # ══════════════════════════════════════════════════════════
    dict(key="realestate", name="房地产周期", tag="18-20 年大周期",
         logic="产业链最长、牵连最广, 与城镇化/人口/居民杠杆绑定。"
               "房价同比是终端价格, 按揭利率锚(LPR5Y)是需求端的闸门, 信贷累计同比是信用端的真实水位, "
               "建材指数是上游需求(水泥玻璃)的真实回声, 螺纹钢是地产链用钢的即时报价。"
               "<b>三处数据坑, 逐条说:</b> ①原来用的『国房景气指数』数据源已停更在 2025-12(滞后 10 个月), "
               "拿它算『当前位置』会得出错误结论, 已剔除; "
               "②akshare 的 macro_china_new_house_price 虽然叫『70 城』, 实际只返回<b>北京/上海两市</b>"
               "(每月恰好 2 条 × 188 个月 = 376 行), 所以房价那一路是一线城市口径, 不是全国 70 城 —— "
               "一线城市房价通常是这轮周期的领先段, 用来看方向比看绝对水平更合适; "
               "③你要的『销售面积增速』『30 城成交面积同比』(统计局/中指院)、『二手房挂牌量/带看量』"
               "(贝壳付费)、『土地成交溢价率』(中指院)、『库存去化周期』(克而瑞)全部<b>无免费接口</b>, "
               "这里只能用信贷+利率+上游需求三面去夹。这五个指标都是同步或滞后指标, 不是领先指标。",
         indicators=[
             I("house_price", "京沪新房+二手价格同比", "house_price", "%", "M", "last", 24,
               (0, "0%=同比持平"),
               note="新房有限价钝化, 二手更贴市场, 取两者均值", main=True, pos=False),
             I("lpr5", "5年期以上贷款基准利率", "lpr5", "%", "M", "last", 24, None,
               note="按揭利率锚; 2019-08 前为贷款基准利率"),
             I("credit", "累计新增人民币贷款同比", "credit", "%", "M", "last", 24, None,
               note="信用端; 用累计值避开季末冲量噪音", pos=False),
             I("construction", "中国建材指数", "construction", "点", "W", "last", 52, None),
             I("rebar", "螺纹钢期货主力", "rebar", "元/吨", "W", "last", 52, None, note="地产链用钢"),
         ]),

    # ══════════════════════════════════════════════════════════
    dict(key="lithium", name="锂周期", tag="新能源上游 · 新增",
         logic="碳酸锂是这十年最烈的产能周期: 2021-22 新能源车爆发 → 锂价从 5 万涨到 60 万 → "
               "全球疯狂扩产 → 2023-24 跌回 7 万, 全行业亏损。锂矿从勘探到出产品 3-5 年, "
               "盐湖/云母/辉石三种路线成本差异大, 所以底部通常是高成本产能先停。"
               "碳酸锂期货(广期所)是最直接的现货锚, 赣锋/天齐两家龙头的股价是市场对锂价中枢的提前定价。"
               "锂价已按中国 CPI 折算成不变价, 便于和 2021 年那波高点直接比。"
               "注意碳酸锂期货 2023-07 才上市, 样本仅 3 年出头, 长周期分位的参考价值有限 —— "
               "想看更长的锂价史只能用 SMM/安泰科现货价(付费)。",
         indicators=[
             I("lc_real", "碳酸锂实际价(扣通胀)", "lc_real", "元/吨", "W", "last", 52, None, main=True,
               note="广期所碳酸锂期货主力, 已折不变价"),
             I("ganfeng", "赣锋锂业", "ganfeng", "元", "W", "last", 52, None, note="锂资源龙头"),
             I("tianqi", "天齐锂业", "tianqi", "元", "W", "last", 52, None, note="锂资源"),
         ]),

    # ══════════════════════════════════════════════════════════
    dict(key="excavator", name="机械(挖机)周期", tag="3-4 年 · 新增",
         logic="挖机是工程机械里最典型的更新周期: 设备寿命 8-10 年, 上一轮销售高峰(2010-11、2021)之后"
               "约 8-10 年就是置换高峰, 叠加基建/地产投资强度, 形成 3-4 年的销量波动。"
               "挖机销量本身由<b>中国工程机械工业协会(CCMA)</b>每月发布, 但协会官网没有稳定免费接口, "
               "所以这里用三家整机/零部件龙头的股价做代理: 三一重工(整机)、徐工机械(整机/起重)、"
               "恒立液压(油缸泵阀, 排产最先体现在它身上, 通常领先整机 1-2 个月)。"
               "股价代理的问题是会被大盘和估值扰动, 看相对强弱比看绝对分位更靠谱。",
         indicators=[
             I("sany", "三一重工", "sany", "元", "W", "last", 52, None, main=True, note="挖机整机龙头"),
             I("xcmg", "徐工机械", "xcmg", "元", "W", "last", 52, None, note="整机/起重"),
             I("hengyi", "恒立液压", "hengyi", "元", "W", "last", 52, None,
               note="油缸泵阀, 排产通常领先整机"),
         ]),

    # ══════════════════════════════════════════════════════════
    dict(key="panel", name="面板周期", tag="2-3 年 · 新增",
         logic="面板是标准的『资本开支蛛网周期』: 一条 G8.5/G10.5 线要 2-3 年建成、几百亿投入, "
               "投产即过剩、亏到减产才涨价, 周期 2-3 年一轮。32/55 吋电视面板报价是最核心的观测指标, "
               "但那是<b>集邦 TrendForce / 群智咨询的付费月度报价</b>, 免费接口取不到。"
               "这里用三家面板厂的股价做代理: 京东方A(全球出货第一, 最纯的面板 beta)、"
               "TCL科技(面板+光伏硅片双主业, 有杂质)、深天马A(中小尺寸, 手机/车载)。"
               "三家同向是最好的确认信号; 若 TCL 偏离, 多半是被光伏那块带偏的。",
         indicators=[
             I("boe", "京东方A", "boe", "元", "W", "last", 52, None, main=True, note="面板龙头"),
             I("tcl", "TCL科技", "tcl", "元", "W", "last", 52, None, note="面板+光伏硅片"),
             I("tianma", "深天马A", "tianma", "元", "W", "last", 52, None, note="中小尺寸面板"),
         ]),

    # ══════════════════════════════════════════════════════════
    dict(key="coal", name="煤炭周期", tag="3-4 年 · 新增",
         logic="煤价看两头: 供给端是产能核准+安监+进口, 需求端是火电+冶金+化工。"
               "焦煤/焦炭期货最接近炼焦煤这条链(钢铁需求), 全社会用电量同比则是动力煤需求的同步指标 —— "
               "电厂日耗和港口库存是动力煤最直接的读数, 但『秦皇岛动力煤价(Q5500)』已随动力煤期货"
               "(ZC)退市而无免费连续序列, 『环渤海港口库存』『电厂日耗』在 CCTD/鄂尔多斯煤炭网付费库里。"
               "所以这里用<b>焦煤 + 焦炭 + 用电量同比</b>三面夹: 焦煤涨说明冶金需求好, "
               "用电量增速高说明动力煤需求不弱, 两者同向才是全局缺煤。",
         indicators=[
             I("jm", "焦煤期货主力", "jm", "元/吨", "W", "last", 52, None, main=True),
             I("jc", "焦炭期货主力", "jc", "元/吨", "W", "last", 52, None, note="焦煤下游"),
             I("elec", "全社会用电量同比", "elec", "%", "M", "last", 24, None,
               note="动力煤需求同步指标", pos=False),
         ]),

    # ══════════════════════════════════════════════════════════
    dict(key="solar", name="光伏周期", tag="产能过剩",
         logic="技术迭代快、产能扩张猛, 硅料→硅片→电池片全是产能游戏, 一扩产就价格战。"
               "工业硅是上游原料, 多晶硅是核心环节, 玻璃是辅材/组件端 —— 三个环节一起看才能判断是全链条出清还是单环节涨价。"
               "注意: 多晶硅期货 2024 年底才上市, 样本短, 其分位参考价值有限。",
         indicators=[
             I("si", "工业硅期货主力", "si", "元/吨", "W", "last", 52, None, main=True),
             I("ps", "多晶硅期货主力", "ps", "元/吨", "W", "last", 52, None, note="样本短(2024-12起)"),
             I("fg", "玻璃期货主力", "fg", "元/吨", "W", "last", 52, None, note="辅材/组件端"),
         ]),

    # ══════════════════════════════════════════════════════════
    dict(key="broker", name="券商周期", tag="牛市杠杆",
         logic="券商是纯行情放大器, 没有生产, 营收≈佣金+自营+两融利息。三个指标互相验证: "
               "成交额是佣金基数, 两融余额是杠杆情绪, 券商指数是市场给的定价。"
               "成交额日均>1万亿全行业躺赚, <8000亿整体亏损。最值得看的是<b>背离</b> —— "
               "成交额和两融都在历史高位、券商股价却在中低位, 说明市场根本没给券商定价, 这种时候它才有性价比。",
         indicators=[
             I("turnover", "两市日成交额", "turnover", "亿元", "W", "mean", 52, (10000, "1万亿(行业盈亏线)")),
             I("margin", "两融余额", "margin", "亿元", "W", "last", 52, None, main=True),
             I("broker_index", "证券公司指数", "broker_index", "点", "W", "last", 52, None),
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


# 去趋势用的长期均线窗口 = 3 年。费半/美光/沪铜这类长期单边上涨的品种,
# 用绝对价格的滚动分位会永远贴着 100%(2022 年那波底部, 费半的绝对分位居然还有 92%),
# 根本读不出高低 —— 只能用「相对自身趋势的偏离」来定位。
DEV_WIN = {"W": 156, "M": 36}
TREND_TH = 1.5      # 后 1/3 均值 / 前 1/3 均值 超过此值 → 有「长期抬升」
TREND_R2 = 0.50     # 对数价格线性拟合的 R² 超过此值 → 抬升是【单调持续】的, 不是大起大落
#
# 两个条件必须同时成立, 缺一不可。只看 TREND_TH 会误伤 BDI/BCI/BPI:
# 它们后 1/3 均值确实是前 1/3 的 1.7~1.9 倍, 但那是被 2016 年历史大底(BDI 290)
# 拉出来的 —— 干散货运价是出了名的剧烈均值回归品种, 不是长期趋势。
# 实测对数线性拟合 R²: BDI 0.31 / BCI 0.13 / BPI 0.30(判否),
# 而费半 0.96 / 美光 0.74 / 沪铜 0.70 / 沪铝 0.72(判是), 分得很干净。
#
# 另外只对【上涨】趋势去趋势, 长期下跌的品种(工业硅 r13=0.60)仍用绝对分位:
# 下跌品种的绝对分位贴底, 恰好是「相对历史便宜」这个做均值回归真正要用的信息;
# 而上涨品种的绝对分位永远贴顶(崩盘那一刻都还有 92%), 什么也读不出来。


def detrend_dev(x, freq):
    """对数价格相对 3 年滚动均线的偏离序列(去趋势)。样本不足则全 NaN。
    freq 也可以直接传整数窗口(绘图时用)。"""
    n = len(x)
    W = freq if isinstance(freq, int) else DEV_WIN.get(freq, 156)
    dev = np.full(n, np.nan)
    if n <= W:
        return dev
    lv = np.log(np.maximum(x, 1e-9))
    cs = np.cumsum(lv)
    dev[W - 1:] = lv[W - 1:] - (cs[W - 1:] - np.concatenate([[0], cs[:-W]])) / W
    return dev


def trend_r2(x):
    """对数价格的线性拟合 R²: 衡量「抬升是否单调持续」。
    高 R² = 一条平滑向上的直线(费半), 低 R² = 大起大落(BDI)。"""
    n = len(x)
    if n < 40:
        return 0.0
    lv = np.log(np.maximum(np.asarray(x, float), 1e-9))
    t = np.arange(n, dtype=float)
    tv = t - t.mean()
    den = float((tv * tv).sum())
    if den <= 0:
        return 0.0
    slope = float((tv * (lv - lv.mean())).sum()) / den
    fit = lv.mean() + slope * tv
    ss_res = float(((lv - fit) ** 2).sum())
    ss_tot = float(((lv - lv.mean()) ** 2).sum())
    return (1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0


def hist_pct_series(v):
    """每个点相对其【截至当时】全部历史的分位(无前视偏差)"""
    n = len(v)
    out = np.full(n, np.nan)
    tmp = np.empty(n)
    for t in range(n):
        if np.isnan(v[t]):
            continue
        w = v[:t + 1]
        w = w[~np.isnan(w)]
        if len(w):
            out[t] = float((w <= v[t]).mean())
    return out


def analyze(s, freq, ma, unit=""):
    x = s.values.astype(float)
    n = len(x)
    cur = float(x[-1])
    pct_abs = float((x <= cur).mean())

    # ── 趋势诊断 + 去趋势位置 ──
    dseq = detrend_dev(x, freq)
    trend = float(np.mean(x[2 * n // 3:]) / np.mean(x[:max(1, n // 3)]))
    tr2 = trend_r2(x)
    # 会取到 0 / 负值的序列(同比增速、价差等)不能做对数去趋势 —— log 会被裁到 1e-9 而彻底失真,
    # 而且这种序列的「前 1/3 vs 后 1/3 比值」本身也没意义(分母可能接近 0)。直接判为非趋势型。
    if np.any(x <= 0):
        dseq = np.full(n, np.nan)
        trend = 0.0
        tr2 = 0.0
    if not np.isnan(dseq[-1]):
        dv = dseq[~np.isnan(dseq)]
        pct_trend = float((dv <= dseq[-1]).mean())
    else:
        pct_trend = None
    is_trend = (pct_trend is not None) and (trend >= TREND_TH) and (tr2 >= TREND_R2)
    # 趋势型品种用去趋势口径, 其余(猪价/BDI/纯碱等均值回归型)用绝对分位
    pct = pct_trend if is_trend else pct_abs

    win = 52 if freq == "W" else 12
    recent = float(np.mean(x[-win:])) if n >= win else float(np.mean(x))
    prev = float(np.mean(x[-2 * win:-win])) if n >= 2 * win else float(np.mean(x[:max(1, n // 2)]))
    chg = (recent / prev - 1.0) if prev else 0.0
    mav = float(np.mean(x[-ma:])) if n >= ma else float(np.mean(x))
    mav_dev = (cur / mav - 1.0) if mav else 0.0

    if pct <= 0.25:
        pos = "周期低位"
    elif pct >= 0.75:
        pos = "周期高位"
    else:
        pos = "周期中位"
    phase = phase_of(pct, chg)

    h1, h2 = (26, 52) if freq == "W" else (6, 12)
    ref = dseq if is_trend else None          # 前瞻也按同一口径匹配历史相似位置
    # 本身就是「增速%」的序列(房价同比等)可以过零, 必须用差值口径, 否则比值会炸
    fmode = "diff" if unit == "%" else "ratio"
    return dict(cur=cur, pct=pct, pct_abs=pct_abs, pct_trend=pct_trend,
                trend=trend, r2=tr2, is_trend=is_trend, pos=pos, phase=phase,
                chg=chg, dev=mav_dev, mav=mav,
                f6=forecast(x, ref, h1, mode=fmode),
                f12=forecast(x, ref, h2, mode=fmode),
                h1=h1, h2=h2, n=n)


def forecast(x, ref, h, band=0.10, min_n=8, mode="ratio"):
    """前瞻统计: 历史上『位置』与当前相近(±10%)的时点, 其后 h 期的变化分布。
    ref 给定时按去趋势偏离的位置匹配, 否则退回价格绝对分位。
    mode="ratio" 算变化率 x[t+h]/x[t]-1 (价格/指数用);
    mode="diff"  算绝对差 x[t+h]-x[t] (本身已是增速%的序列用 —— 增速可以过零,
                 比值会除以一个接近 0 的数, 得出 -500% 这种荒唐读数)。"""
    n = len(x)
    if n < h + 40:
        return None
    rp = hist_pct_series(ref if ref is not None else x)
    q = rp[-1]
    if np.isnan(q):
        return None
    buf = []
    for t in range(30, n - h):
        if np.isnan(rp[t]) or np.isnan(x[t]) or np.isnan(x[t + h]):
            continue
        if mode == "diff":
            delta = float(x[t + h] - x[t])
        else:
            if x[t] <= 0:
                continue
            delta = float(x[t + h] / x[t] - 1.0)
        if abs(rp[t] - q) <= band:
            buf.append(delta)
    if len(buf) < min_n:
        return None
    a = np.array(buf)
    return dict(n=int(len(a)), med=float(np.median(a)),
                p25=float(np.percentile(a, 25)), p75=float(np.percentile(a, 75)),
                win=float((a > 0).mean()), mode=mode)


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


def _pp(v, unit="pp"):
    """分歧度格式化: <1 时保留一位小数, 否则取整 —— 避免出现「±0pp」这种读数"""
    return f"{v:.1f}{unit}" if 0 < v < 1 else f"{v:.0f}{unit}"


def _fmt_axis(v, unit):
    """Y 轴刻度专用: 去趋势偏离(单位 %)带正负号且不留小数, 版面更干净"""
    if unit == "%":
        return f"{v:+.0f}%"
    return _fmt(v, unit)


def _thin(ys, maxn=MAX_PLOT):
    n = len(ys)
    if n <= maxn:
        return list(range(n))
    step = n / maxn
    idx = [int(i * step) for i in range(maxn)]
    if idx[-1] != n - 1:
        idx[-1] = n - 1
    return idx


def svg_chart(s, info, unit, ma, ref, w=1180, h=250, compact=False, color="#58a6ff", dev_ma=None):
    """历史曲线 + 均线 + 当前位置 (+ 大图带前瞻统计延伸带)
    dev_ma: 传窗口(如 156 周)时, 该指标判为趋势型 —— 曲线改画
            「对数价格相对该窗口均线的偏离(%)」, 而不是绝对价格。"""
    xs = s.index
    ys = s.values.astype(float)

    # ── 趋势型指标: 直接画「对数偏离 3 年均线」的曲线, 而不是绝对价格 ──
    # 绝对价格图对费半/美光/沪铜是没用的 —— 曲线一路向右上, 2002 年的大底和
    # 今天的顶部在图上长得一模一样。画偏离量, 高低立刻分得开。
    dev_plot = None
    if dev_ma:
        _d = detrend_dev(ys, dev_ma)
        if np.count_nonzero(~np.isnan(_d)) >= 20:
            dev_plot = _d
        else:
            dev_ma = None

    if dev_plot is not None:
        idx = _thin(np.nan_to_num(dev_plot, nan=0.0), MINI_PLOT if compact else MAX_PLOT)
        idx = [i for i in idx if not np.isnan(dev_plot[i])]
        if len(idx) >= 20:
            px_ys = [dev_plot[i] * 100.0 for i in idx]   # 单位: %
            y_unit = "%"
        else:
            dev_plot = None

    if dev_plot is None:
        idx = _thin(ys, MINI_PLOT if compact else MAX_PLOT)
        px_ys = [ys[i] for i in idx]
        y_unit = unit
    n_hist = len(px_ys)
    is_dev = dev_plot is not None

    f6 = None if (compact or is_dev) else info.get("f6")
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
    if hist_lo > 0 and not is_dev:
        lo = max(lo, 0.0)
    if is_dev:
        # 去趋势图的 0 是「3 年均线」这个基准, 必须留在画面里, 不然读不出正负
        _pad = 0.20 * max(abs(hist_lo), abs(hist_hi), 1e-9)
        lo = min(lo, -_pad)
        hi = max(hi, _pad)
    if ref and not (lo < ref[0] < hi):
        ref = None

    def _clip(v):
        return min(max(v, lo), hi)

    if compact:
        L, R, B, fs = 42, 10, 17, 9
        T = 20 if is_dev else 9       # 去趋势图标个角标, 留出顶部空间
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

    # ── 背景带: 高位区(浅红) / 低位区(浅绿) ──
    # 边界取「所画序列」的 75% / 25% 分位, 与卡片里周期高位/低位的判定阈值(0.75/0.25)一致。
    # 配色沿用 A 股习惯: 红=高(涨)、绿=低(跌)。画在最底层, 网格线压在它上面, 保证刻度可读。
    _bsrc = (dev_plot * 100.0) if is_dev else np.asarray(ys, dtype=float)
    _bs = _bsrc[~np.isnan(_bsrc)]
    if len(_bs) >= 30:
        q75 = float(np.percentile(_bs, 75))
        q25 = float(np.percentile(_bs, 25))
        y_hi = Y(min(max(q75, lo), hi))
        y_lo = Y(min(max(q25, lo), hi))
        # 高位带
        if y_hi > T + 2:
            parts.append(f'<rect x="{L}" y="{T}" width="{pw:.1f}" '
                         f'height="{y_hi-T:.1f}" fill="rgba(248,81,73,0.11)"/>')
            parts.append(f'<line x1="{L}" y1="{y_hi:.1f}" x2="{w-R}" y2="{y_hi:.1f}" '
                         f'stroke="rgba(248,81,73,0.45)" stroke-width="1" stroke-dasharray="4 3"/>')
            if y_hi - T > 11:
                parts.append(f'<text x="{w-R-3}" y="{T+9:.1f}" fill="rgba(248,81,73,0.82)" '
                             f'font-size="{fs-1}" text-anchor="end" paint-order="stroke" '
                             f'stroke="#0d1117" stroke-width="2.4" stroke-linejoin="round">'
                             f'高位区 ≥75% 分位</text>')
        # 低位带
        if y_lo < T + ph - 2:
            parts.append(f'<rect x="{L}" y="{y_lo:.1f}" width="{pw:.1f}" '
                         f'height="{T+ph-y_lo:.1f}" fill="rgba(63,185,80,0.12)"/>')
            parts.append(f'<line x1="{L}" y1="{y_lo:.1f}" x2="{w-R}" y2="{y_lo:.1f}" '
                         f'stroke="rgba(63,185,80,0.45)" stroke-width="1" stroke-dasharray="4 3"/>')
            if T + ph - y_lo > 11:
                parts.append(f'<text x="{w-R-3}" y="{T+ph-3.5:.1f}" fill="rgba(63,185,80,0.82)" '
                             f'font-size="{fs-1}" text-anchor="end" paint-order="stroke" '
                             f'stroke="#0d1117" stroke-width="2.4" stroke-linejoin="round">'
                             f'低位区 ≤25% 分位</text>')

    # 网格 + Y 轴刻度
    ngrid = 3 if compact else 4
    for g in range(ngrid):
        v = lo + (hi - lo) * g / (ngrid - 1)
        y = Y(v)
        parts.append(f'<line x1="{L}" y1="{y:.1f}" x2="{w-R}" y2="{y:.1f}" '
                     f'stroke="#21262d" stroke-width="1"/>')
        parts.append(f'<text x="{L-5}" y="{y+3.5:.1f}" fill="#8b949e" font-size="{fs}" '
                     f'text-anchor="end">{_fmt_axis(v, y_unit)}</text>')

    if is_dev:
        # 去趋势图: 0 就是「3 年均线」这个基准, 用虚线标出来
        if lo < 0 < hi:
            y0 = Y(0.0)
            parts.append(f'<line x1="{L}" y1="{y0:.1f}" x2="{w-R}" y2="{y0:.1f}" '
                         f'stroke="#d2991d" stroke-width="1" stroke-dasharray="5 4" '
                         f'opacity="0.8"/>')
        parts.append(f'<text x="{L+3}" y="{T-6}" fill="#d2991d" font-size="9">'
                     f'已去趋势 · 对数偏离 3 年均线 (%)</text>')

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

    if ma_pts and not is_dev:
        parts.append(f'<polyline points="{ma_pts}" fill="none" stroke="#8b949e" '
                     f'stroke-width="1" stroke-dasharray="4 3" opacity="0.65"/>')
    if dev_ma and not is_dev:
        dmav = s.rolling(dev_ma).mean().values
        dm_pts = " ".join(f"{X(k):.1f},{Y(_clip(dmav[i])):.1f}"
                          for k, i in enumerate(idx) if not np.isnan(dmav[i]))
        if dm_pts:
            parts.append(f'<polyline points="{dm_pts}" fill="none" stroke="#d2991d" '
                         f'stroke-width="1.2" stroke-dasharray="3 3" opacity="0.9"/>')
            if not compact:
                parts.append(f'<text x="{L+4}" y="{T+11}" fill="#d2991d" font-size="10">'
                             f'3 年均线(去趋势基准)</text>')
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
# ══════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════
# 四之二、美林时钟(中国改良版 + 美国原版)
# ══════════════════════════════════════════════════════════════
# 为什么用「12 个月的变化」而不是「水平分位」做坐标轴:
# 中国 M2 同比从 2009 年的 29% 一路降到 2026 年的 8%, 信贷增速也是长期下台阶。
# 用水平的历史分位当轴, 这个钟会被结构性因素钉死在「紧货币+紧信用」那一格, 十年不动,
# 完全没有指导意义。「宽货币/紧货币」在实践里指的是【边际方向】——
# 央行这个月比一年前更松还是更紧。所以两个时钟的轴都统一取「同比增速的 12 个月变化(pp)」,
# 中线固定为 0。美国那边同理: 增长用 ISM 减荣枯线 50、通胀用 CPI 减美联储目标 2%,
# 这样中线才有公认含义, 而不是一个随样本漂移的分位数。
CLOCK_PATH_N = 60          # 轨迹画最近多少个月
CLOCK_DIFF_M = 12          # 变化口径的月数


CLOCK_DEFS = [
    dict(
        key="cn_ml",
        name="美林时钟 · 中国改良版(货币 × 信用)",
        tag="宽货币/紧货币 × 宽信用/紧信用",
        x=dict(fetch="credit", name="信用", lab=("紧信用", "宽信用"),
               desc="累计新增人民币贷款同比的 12 个月变化"),
        y=dict(fetch="m2", name="货币", lab=("紧货币", "宽货币"),
               desc="M2 同比的 12 个月变化"),
        quads=[
            dict(pos="左上", name="衰退期", asset="债券最佳", tint="rgba(63,185,80,0.10)",
                 why="货币松、信用没起来 —— 钱在金融体系里空转, 实体不借钱, 债券走牛"),
            dict(pos="右上", name="复苏期", asset="股票最佳", tint="rgba(248,81,73,0.10)",
                 why="货币和信用同时扩张 —— 宽货币传导到实体了, 盈利与估值一起抬"),
            dict(pos="右下", name="过热期", asset="商品最佳", tint="rgba(210,153,34,0.12)",
                 why="货币开始收、信用还在扩 —— 需求惯性最强, 通胀抬头, 实物资产占优"),
            dict(pos="左下", name="滞胀期", asset="现金最佳", tint="rgba(88,166,255,0.10)",
                 why="货币和信用同时收缩 —— 需求走弱而成本已抬, 股债商品三杀, 拿现金"),
        ],
        logic="美林时钟原本的两轴是「产出缺口 × 通胀」, 但中国没有可用的产出缺口高频数据, "
              "所以国内券商(任泽平/华创等)普遍改用<b>货币 × 信用</b>两轴: "
              "央行管货币(M2/利率), 银行与实体管信用(社融/信贷), 两轴的松紧组合决定了大类资产轮动。"
              "本页的『信用』用<b>累计新增人民币贷款同比</b>(社融存量增速更标准, 但那个免费接口滞后到 2026-04, "
              "会把当前象限算错), 『货币』用<b>M2 同比</b>; 两轴都取 12 个月的变化(pp), 中线为 0。"
              "<b>这是框架不是定律</b>: 2015 年之后中国信用周期被地产和隐性债务主导, "
              "四象限的资产映射出现过明显失灵(典型是 2016-17 紧货币+宽信用下商品与地产同时走强), "
              "请把它当成描述当前所处的货币信用环境, 而不是买卖信号。",
    ),
    dict(
        key="us_ml",
        name="美林时钟 · 美国原版(增长 × 通胀)",
        tag="ISM PMI × CPI",
        x=dict(fetch="ism", name="经济增长", lab=("收缩", "扩张"),
               desc="ISM 制造业 PMI 相对荣枯线 50 的偏离"),
        y=dict(fetch="usa_cpi", name="通胀", lab=("低通胀", "高通胀"),
               desc="美国 CPI 年率相对美联储目标 2% 的偏离"),
        quads=[
            dict(pos="左上", name="滞胀期", asset="现金最佳", tint="rgba(88,166,255,0.10)",
                 why="增长掉到荣枯线下、通胀还在目标上 —— 成本压力 + 需求走弱, 现金为王"),
            dict(pos="右上", name="过热期", asset="商品最佳", tint="rgba(248,81,73,0.10)",
                 why="增长扩张 + 通胀超目标 —— 需求最旺, 实物资产和大宗占优, 债最差"),
            dict(pos="右下", name="复苏期", asset="股票最佳", tint="rgba(63,185,80,0.10)",
                 why="增长回到扩张区、通胀温 —— 盈利修复而货币不必收紧, 股票最舒服"),
            dict(pos="左下", name="衰退期", asset="债券最佳", tint="rgba(210,153,34,0.12)",
                 why="增长收缩 + 通胀回落 —— 央行降息在路上, 长久期债券最佳"),
        ],
        logic="原版美林时钟用「产出缺口 × 通胀」划分四阶段, 依次对应债券→股票→商品→现金的轮动。"
              "这里用可免费拿到、且足够高频的两个代理: 增长用 <b>ISM 制造业 PMI</b>"
              "(相对荣枯线 50 的偏离, 50 是官方扩张/收缩的分界), 通胀用 <b>美国 CPI 年率</b>"
              "(相对美联储 2% 目标的偏离)。数据源是东方财富 datacenter —— "
              "akshare 自带的 macro_usa_* 大多来自金十, 已停更在 2025-09, 会把当前象限算错。"
              "轨迹线画的是最近 5 年的月度路径, 越新的点颜色越亮, 可以看出这个钟是<b>顺时针</b>转的"
              "(经典美林时钟的旋转方向): 衰退→复苏→过热→滞胀→衰退。"
              "同样提醒: 2020 年之后财政直接发钱、2022-23 供应链冲击, 都让这个框架短暂失效过。",
    ),
]


def _monthly(s):
    try:
        return s.resample("ME").last().dropna()
    except ValueError:
        return s.resample("M").last().dropna()


def _clock_fetch(fetch_key):
    """返回 (月度序列, 数据源说明, 单位)"""
    res = FE[fetch_key]()
    return _monthly(res[0]), res[1], res[2]


def build_clocks():
    out = []
    for cfg in CLOCK_DEFS:
        try:
            if cfg["key"] == "us_ml":
                # 美国两轴是「相对公认基准的偏离」, 不是 12 月变化
                xlev, xsrc, _ = _clock_fetch("ism")
                ylev, ysrc, _ = _clock_fetch("usa_cpi")
                xs, ys = xlev - 50.0, ylev - 2.0
            else:
                xlev, xsrc, _ = _clock_fetch(cfg["x"]["fetch"])
                ylev, ysrc, _ = _clock_fetch(cfg["y"]["fetch"])
                xs = (xlev - xlev.shift(CLOCK_DIFF_M)).dropna()
                ys = (ylev - ylev.shift(CLOCK_DIFF_M)).dropna()
            df = pd.concat([xs.rename("x"), ys.rename("y")], axis=1).dropna()
            if len(df) < 24:
                raise ValueError("对齐后样本过短(%d)" % len(df))
            cx, cy = float(df["x"].iloc[-1]), float(df["y"].iloc[-1])
            # 象限: SVG 里 y 越小越高。命名顺序与 CLOCK_DEFS 的 quads 保持一致(先左右后上下)
            qpos = ("右" if cx >= 0 else "左") + ("上" if cy >= 0 else "下")
            q = next((q for q in cfg["quads"] if q["pos"] == qpos), cfg["quads"][0])
            # 各象限历史占比
            tot = len(df)
            share = {}
            for qq in cfg["quads"]:
                if qq["pos"] == "左上":
                    m = (df["x"] < 0) & (df["y"] >= 0)
                elif qq["pos"] == "右上":
                    m = (df["x"] >= 0) & (df["y"] >= 0)
                elif qq["pos"] == "右下":
                    m = (df["x"] >= 0) & (df["y"] < 0)
                else:
                    m = (df["x"] < 0) & (df["y"] < 0)
                share[qq["pos"]] = int(m.sum()) / tot
            path = [[d.strftime("%Y-%m"), round(float(a), 3), round(float(b), 3)]
                    for d, a, b in zip(df.index[-CLOCK_PATH_N:],
                                       df["x"].iloc[-CLOCK_PATH_N:],
                                       df["y"].iloc[-CLOCK_PATH_N:])]
            out.append(dict(
                key=cfg["key"], name=cfg["name"], tag=cfg["tag"], logic=cfg["logic"],
                xname=cfg["x"]["name"], yname=cfg["y"]["name"],
                xlab=list(cfg["x"]["lab"]), ylab=list(cfg["y"]["lab"]),
                xdesc=cfg["x"]["desc"], ydesc=cfg["y"]["desc"],
                xsrc=xsrc, ysrc=ysrc, unit="pp",
                xlev_unit=("" if cfg["key"] == "us_ml" else "%"),
                ylev_unit="%",
                cur=dict(x=round(cx, 3), y=round(cy, 3), pos=qpos,
                         name=q["name"], asset=q["asset"], why=q["why"]),
                asof=df.index[-1].strftime("%Y-%m"), n=int(len(df)),
                share=share, path=path, quads=cfg["quads"],
                # 水平值取与两轴对齐的那个月(df 的最后一行), 免得出现
                # 「轴向截至 2026-08、水平值却写 2026-09」的自相矛盾
                xlev=round(float(xlev.reindex(df.index).dropna().iloc[-1]), 3),
                ylev=round(float(ylev.reindex(df.index).dropna().iloc[-1]), 3),
                xlev_date=df.index[-1].strftime("%Y-%m"),
                ylev_date=df.index[-1].strftime("%Y-%m"),
            ))
            print(f"  [OK] {cfg['name']}  当前象限 {qpos} {q['name']} · {q['asset']} "
                  f"(x={cx:+.2f}pp, y={cy:+.2f}pp, 截至 {out[-1]['asof']})")
        except Exception as e:
            print(f"  [FAIL] 时钟 {cfg['name']}: {type(e).__name__}: {str(e)[:100]}")
    return out


def clock_svg(c, w=560, h=430):
    """四象限散点 + 最近 5 年轨迹。SVG 里 y 轴向上为正(值越大越靠上)。"""
    L, R, T, B = 78, 22, 30, 52
    pw, ph = w - L - R, h - T - B
    xs = [p[1] for p in c["path"]]
    ys = [p[2] for p in c["path"]]
    lo_x, hi_x = min(xs), max(xs)
    lo_y, hi_y = min(ys), max(ys)
    pad_x = max((hi_x - lo_x) * 0.18, 0.6)
    pad_y = max((hi_y - lo_y) * 0.18, 0.6)
    # 中线 0 必须画得进去
    x0, x1 = min(lo_x - pad_x, -pad_x), max(hi_x + pad_x, pad_x)
    y0, y1 = min(lo_y - pad_y, -pad_y), max(hi_y + pad_y, pad_y)

    def X(v):
        return L + (v - x0) / (x1 - x0) * pw

    def Y(v):
        return T + ph - (v - y0) / (y1 - y0) * ph

    zx, zy = X(0), Y(0)
    p = [f'<svg viewBox="0 0 {w} {h}" width="100%" height="auto" '
         f'style="display:block;background:#0d1117;border-radius:9px" '
         f'xmlns="http://www.w3.org/2000/svg">']
    tint = {q["pos"]: q["tint"] for q in c["quads"]}
    nm = {q["pos"]: q["name"] for q in c["quads"]}
    ast = {q["pos"]: q["asset"] for q in c["quads"]}
    # 四象限底色 + 角标
    rects = [("左上", L, T, zx - L, zy - T), ("右上", zx, T, L + pw - zx, zy - T),
             ("右下", zx, zy, L + pw - zx, T + ph - zy), ("左下", L, zy, zx - L, T + ph - zy)]
    for kk, rx, ry, rw, rh in rects:
        if rw <= 0 or rh <= 0:
            continue
        on = (kk == c["cur"]["pos"])
        p.append(f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{rw:.1f}" height="{rh:.1f}" '
                 f'fill="{tint.get(kk, "#161b22")}" '
                 + (f'stroke="{_ph_color(0.9)}" stroke-width="1.6" ' if on else '')
                 + f'rx="4"/>')
        ax = rx + 9
        ay = ry + 17
        anc = "start"
        col = "#e6edf3" if on else "#6e7681"
        p.append(f'<text x="{ax:.1f}" y="{ay:.1f}" font-size="11.5" font-weight="600" '
                 f'fill="{col}" text-anchor="{anc}">{nm.get(kk, "")}</text>')
        p.append(f'<text x="{ax:.1f}" y="{ay+14:.1f}" font-size="10" '
                 f'fill="{"#adbac7" if on else "#6e7681"}" text-anchor="{anc}">'
                 f'{ast.get(kk, "")}</text>')
        if on:
            p.append(f'<text x="{ax:.1f}" y="{ay+28:.1f}" font-size="10" font-weight="600" '
                     f'fill="{_ph_color(0.9)}" text-anchor="{anc}">◀ 当前</text>')
    # 中线
    p.append(f'<line x1="{L}" y1="{zy:.1f}" x2="{L+pw}" y2="{zy:.1f}" stroke="#484f58" '
             f'stroke-width="1" stroke-dasharray="4 4"/>')
    p.append(f'<line x1="{zx:.1f}" y1="{T}" x2="{zx:.1f}" y2="{T+ph}" stroke="#484f58" '
             f'stroke-width="1" stroke-dasharray="4 4"/>')
    p.append(f'<rect x="{L}" y="{T}" width="{pw}" height="{ph}" fill="none" stroke="#30363d"/>')
    # 轨迹
    pts = " ".join(f"{X(a):.1f},{Y(b):.1f}" for _, a, b in c["path"])
    p.append(f'<polyline points="{pts}" fill="none" stroke="#58a6ff" stroke-width="1.4" '
             f'stroke-opacity="0.42" stroke-linejoin="round"/>')
    n = len(c["path"])
    for i, (_, a, b) in enumerate(c["path"][-18:]):
        op = 0.25 + 0.6 * (i / max(1, len(c["path"][-18:]) - 1))
        p.append(f'<circle cx="{X(a):.1f}" cy="{Y(b):.1f}" r="1.9" fill="#58a6ff" '
                 f'fill-opacity="{op:.2f}"/>')
    # 当前点
    cx, cy = X(c["cur"]["x"]), Y(c["cur"]["y"])
    p.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="7" fill="none" '
             f'stroke="{_ph_color(0.9)}" stroke-width="1.6" stroke-opacity="0.75"/>')
    p.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="{_ph_color(0.9)}"/>')
    lbl = c["asof"]
    tx = min(max(cx + 10, L + 4), L + pw - 52)
    ty = max(min(cy - 9, T + ph - 6), T + 13)
    p.append(f'<text x="{tx:.1f}" y="{ty:.1f}" font-size="10.5" font-weight="600" '
             f'fill="#f0b8b5" paint-order="stroke" stroke="#0d1117" stroke-width="2.6" '
             f'stroke-linejoin="round">{lbl}</text>')
    # 轴标签
    p.append(f'<text x="{L}" y="{T+ph+20}" font-size="11" fill="#8b949e" '
             f'text-anchor="start">{c["xlab"][0]}</text>')
    p.append(f'<text x="{L+pw}" y="{T+ph+20}" font-size="11" fill="#8b949e" '
             f'text-anchor="end">{c["xlab"][1]}</text>')
    p.append(f'<text x="{L+pw/2:.0f}" y="{T+ph+38}" font-size="11.5" fill="#adbac7" '
             f'text-anchor="middle">→ {c["xname"]} ({c["xdesc"]})</text>')
    p.append(f'<text x="{L-8}" y="{T+ph-2:.0f}" font-size="10.5" fill="#8b949e" '
             f'text-anchor="end">{c["ylab"][0]}</text>')
    p.append(f'<text x="{L-8}" y="{T+11:.0f}" font-size="10.5" fill="#8b949e" '
             f'text-anchor="end">{c["ylab"][1]}</text>')
    p.append(f'<text transform="translate(18,{T+ph/2:.0f}) rotate(-90)" font-size="11.5" '
             f'fill="#adbac7" text-anchor="middle">→ {c["yname"]} ({c["ydesc"]})</text>')
    p.append("</svg>")
    return "".join(p)


def render_clocks(clocks):
    if not clocks:
        return ""
    cards = []
    for c in clocks:
        rows = []
        for q in c["quads"]:
            on = q["pos"] == c["cur"]["pos"]
            rows.append(
                f'<tr style="{"background:rgba(248,81,73,0.10)" if on else ""}">'
                f'<td style="padding:5px 8px;font-size:11px;color:#8b949e;white-space:nowrap">'
                f'{q["pos"]}<br><span style="color:#6e7681;font-size:9.5px">'
                f'{c["xlab"][0 if "左" in q["pos"] else 1]}+'
                f'{c["ylab"][0 if "下" in q["pos"] else 1]}</span></td>'
                f'<td style="padding:5px 8px;font-size:12px;font-weight:600;'
                f'color:{"#e6edf3" if on else "#adbac7"}">{q["name"]}</td>'
                f'<td style="padding:5px 8px;font-size:11.5px;'
                f'color:{"#e6edf3" if on else "#8b949e"}">{q["asset"]}</td>'
                f'<td style="padding:5px 8px;font-size:10.5px;color:#6e7681;line-height:1.5">'
                f'{q["why"]}</td>'
                f'<td style="padding:5px 8px;font-size:11px;color:#8b949e;'
                f'font-variant-numeric:tabular-nums;text-align:right">'
                f'{c["share"].get(q["pos"], 0)*100:.0f}%</td></tr>')
        cards.append(
            f'<div class="cyc-card">'
            f'<div class="cyc-hd">'
            f'<div class="lft"><div class="nm">{c["name"]}'
            f'<span class="tag" style="color:#f85149;border-color:#f8514988;background:#f8514918">'
            f'{c["cur"]["name"]} · {c["cur"]["asset"]}</span></div>'
            f'<div class="src">{c["tag"]} · 截至 {c["asof"]} · 轨迹为最近 {len(c["path"])} 个月'
            f' · 历史样本 {c["n"]} 个月</div></div>'
            f'<div class="rgt">'
            f'<div class="metric"><div class="k">{c["xname"]}轴</div>'
            f'<div class="v" style="color:{"#f85149" if c["cur"]["x"]>=0 else "#3fb950"}">'
            f'{c["cur"]["x"]:+.2f}<span style="font-size:10px;color:#8b949e">pp</span></div></div>'
            f'<div class="metric"><div class="k">{c["yname"]}轴</div>'
            f'<div class="v" style="color:{"#f85149" if c["cur"]["y"]>=0 else "#3fb950"}">'
            f'{c["cur"]["y"]:+.2f}<span style="font-size:10px;color:#8b949e">pp</span></div></div>'
            f'</div></div>'
            f'<div class="ml-wrap"><div class="ml-svg">{clock_svg(c)}</div>'
            f'<div class="ml-side">'
            f'<div class="ml-now"><b>{c["cur"]["name"]}</b> · {c["cur"]["asset"]}'
            f'<div style="margin-top:4px">{c["cur"]["why"]}</div></div>'
            f'<table class="ml-tb"><thead><tr>'
            f'<th style="text-align:left">象限</th><th style="text-align:left">阶段</th>'
            f'<th style="text-align:left">资产</th><th style="text-align:left">含义</th>'
            f'<th style="text-align:right">历史占比</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
            f'<div class="ml-lv">水平值(非轴向, 仅供参照): '
            f'{c["xname"]} {c["xlev"]:.2f}{c.get("xlev_unit","%")} ({c["xlev_date"]}) · '
            f'{c["yname"]} {c["ylev"]:.2f}{c.get("ylev_unit","%")} ({c["ylev_date"]})</div>'
            f'<div class="ml-src">数据源: {c["xsrc"]} · {c["ysrc"]}</div>'
            f'</div></div>'
            f'<div class="cyc-logic">{c["logic"]}</div>'
            f'</div>')
    return ('<div class="cyc-card" style="margin-top:22px">'
            '<div class="t" style="font-size:15px;font-weight:600;color:#e6edf3;margin-bottom:6px">'
            '美林时钟 · 两个版本</div>'
            '<div class="s" style="font-size:12px;color:#8b949e;line-height:1.75">'
            '美林时钟把宏观环境切成四格, 每格对应一类占优资产。'
            '<b>中国改良版</b>把美林原来的「产出缺口 × 通胀」换成「<b>货币 × 信用</b>」'
            '(国内没有高频产出缺口数据, 而央行管货币、实体管信用, 两轴松紧决定了大类资产轮动); '
            '<b>美国原版</b>仍用「<b>增长 × 通胀</b>」。'
            '两版的坐标轴都取<b>相对基准的偏离</b>(pp), 中线固定为 0 —— '
            '不用历史分位, 因为 M2 同比和信贷增速是长期下台阶的, 用分位会把钟钉死在同一格十年不动。'
            '图中轨迹是最近 5 年的月度路径, 越新的点越亮, 经典美林时钟应<b>顺时针</b>旋转: '
            '衰退→复苏→过热→滞胀。<b>框架不等于定律</b>, 只用来描述当下处在什么货币信用/增长通胀环境。'
            '</div></div>' + "".join(cards))
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
.cyc-ind .iph { font-size:10.5px; font-weight:500; margin-bottom:4px; }
.cyc-ind .iph .dv { color:#6e7681; font-weight:400; }
.cyc-ind .ibas { font-size:9.5px; color:#6e7681; line-height:1.45; margin-bottom:7px; }
.cyc-ind .ibas .tr { color:#d2991d; }
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
.cyc-ind-grid { grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); }
.ml-wrap { display:grid; grid-template-columns:minmax(420px,1fr) minmax(320px,1.15fr);
  gap:16px; align-items:start; }
.ml-svg { min-width:0; }
.ml-side { min-width:0; }
.ml-now { background:#0d1117; border:1px solid #21262d; border-left:3px solid #f85149;
  border-radius:8px; padding:9px 12px; font-size:12px; color:#adbac7; line-height:1.7;
  margin-bottom:10px; }
.ml-tb { width:100%; border-collapse:collapse; }
.ml-tb th { font-size:10px; color:#6e7681; font-weight:500; text-align:left;
  padding:3px 8px; border-bottom:1px solid #21262d; }
.ml-tb td { border-bottom:1px solid #161b22; vertical-align:top; }
.ml-lv { font-size:10.5px; color:#6e7681; margin-top:8px; line-height:1.6; }
.ml-src { font-size:10px; color:#6e7681; margin-top:4px; line-height:1.5; }
@media (max-width:900px){ .ml-wrap { grid-template-columns:1fr; }
  .cyc-grid { grid-template-columns:repeat(2,1fr); } }
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


def render_fragment(rows, updated, clocks=None):
    minis, cards = [], []
    for r in rows:
        agg = r["agg"]
        color = _ph_color(agg["pct_avg"])
        f6, f12 = agg.get("f6"), agg.get("f12")

        # 分歧度越大, 综合位置越不可信 —— 直接把它标出来, 而不是假装一个数就代表整个周期
        div_bad = agg["pct_std"] > 0.25
        div_txt = (f'<span style="color:#d2991d">±{_pp(agg["pct_std"]*100)} 分歧大, 位置参考性低</span>'
                   if div_bad else f'分歧 ±{_pp(agg["pct_std"]*100)}')

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
            # 改成显示与长期均线的绝对差
            if ind["unit"] == "%":
                dev_txt = f'均线 {st.get("mav", 0):+.2f}%'
            else:
                dev_txt = f'偏离均线 {st["dev"]*100:+.1f}%'
            # 位置口径透明度: 趋势型品种必须说明用的是去趋势口径, 否则绝对分位会误导
            pabs = st["pct_abs"] * 100
            ptr = st["pct_trend"]
            r2 = st.get("r2", 0.0)
            if st["is_trend"]:
                basis_html = (f'<span class="tr">趋势型 {st["trend"]:.1f}x · R²={r2:.2f} 单调上涨'
                              f' · 位置取去趋势口径</span>'
                              f' · 同口径下绝对分位为 {pabs:.0f}%（长期上涨会把它顶到很高，别用）')
            else:
                why = ""
                if ptr is not None and st["trend"] >= TREND_TH and r2 < TREND_R2:
                    why = f"（涨幅够但 R²={r2:.2f}<{TREND_R2}，是大起大落不是趋势）"
                basis_html = (f'均值回归型 {st["trend"]:.2f}x · R²={r2:.2f} · 位置取绝对分位{why}'
                              + (f' · 同口径去趋势分位 {ptr*100:.0f}%' if ptr is not None else ''))
            warn = _stale_warn(ind["last_date"])
            note = f' · {ind["note"]}' if ind.get("note") else ""
            svg = svg_chart_series(ind)
            ind_html.append(
                f'<div class="cyc-ind">'
                f'<div class="ihd"><span class="inm" title="{ind["name"]}">{ind["name"]}</span>'
                f'<span class="ipct" style="color:{ic}">位置 {st["pct"]*100:.0f}%</span></div>'
                f'<div class="ival">{_fmt(st["cur"], ind["unit"])}'
                f'<span class="iu">{ind["unit"] or "指数"}</span></div>'
                f'<div class="iph" style="color:{ic}">{st["phase"]}'
                f'<span class="dv"> · {dev_txt}</span></div>'
                f'<div class="ibas">{basis_html}</div>'
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
            if f.get("mode") == "diff":
                # 序列本身已是增速%, 后验分布只能用百分点差表示, 不能用「涨跌幅」
                v = f'{f["med"]:+.2f}pp'
                rng = f'{f["p25"]:+.2f}pp ~ {f["p75"]:+.2f}pp'
                unit_note = "pp=百分点; 该指标本身是同比增速, 故统计的是增速的变化"
            else:
                v = f'{f["med"]*100:+.1f}%'
                rng = f'{f["p25"]*100:+.1f}% ~ {f["p75"]*100:+.1f}%'
                unit_note = "25%~75%区间"
            return (f'<div class="box"><div class="k">{label}</div>'
                    f'<div class="v" style="color:{c}">{v}</div>'
                    f'<div class="d">{unit_note} {rng}'
                    f' | 上行概率 {f["win"]*100:.0f}% | n={f["n"]}</div></div>')

        main_nm = next((i["name"] for i in r["indicators"] if i.get("main")), r["indicators"][0]["name"])

        cards.append(
            f'<div class="cyc-card">'
            f'<div class="cyc-hd">'
            f'<div class="lft"><div class="nm">{r["name"]}'
            f'<span class="tag" style="color:{color};border-color:{color}88;background:{color}18">'
            f'{agg["phase"]}</span></div>'
            f'<div class="src">{len(r["indicators"])} 个指标并列 · '
            f'综合位置 = 各指标位置的均值 · 分歧度 = 位置的标准差'
            + ('<span style="color:#d2991d"> · ⚠ 指标分歧过大, 综合位置参考性低, 请看下方各指标</span>'
               if div_bad else '') + '</div></div>'
            f'<div class="rgt">'
            f'<div class="metric"><div class="k">综合位置</div>'
            f'<div class="v" style="color:{color}">{agg["pct_avg"]*100:.0f}%</div></div>'
            f'<div class="metric"><div class="k">指标分歧</div>'
            f'<div class="v" style="color:{"#d2991d" if div_bad else "#e6edf3"}">'
            f'±{_pp(agg["pct_std"]*100)}</div></div>'
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
            f'<div class="s">每个周期用 <b>3~5 个独立指标</b>交叉验证, 单指标噪音大、易被个体事件扭曲。'
            f'<b>综合位置</b>=各指标位置的均值; <b>分歧度</b>=位置的标准差 —— '
            f'<b>分歧度 &gt; 25pp 时综合位置本身就不成立</b>, 说明这几条产业链已经割裂, '
            f'这时应直接看下面三个指标各自的读数。'
            f'<br><b>位置口径分两种, 页面上逐个标了。</b>'
            f'长期单边上涨的品种(费半、美光、沪铜)如果用「绝对价格的历史分位」, 结果会永远贴着 100% —— '
            f'以费半为例, 2018-10 那波顶部绝对分位 81.7%, 而 2022-10 实打实的下行周期底反而只有 77.2%, '
            f'两个截然相反的时点读数几乎一样, 这叫分辨不出高低。'
            f'所以对这类<b>趋势型</b>指标, 位置改用'
            f'<b>对数价格相对 3 年均线的偏离</b>所处分位: 同一口径下费半 2018-10 顶 14%、'
            f'2020-03 疫情底 0.5%、2022-10 周期底 0.3%、现在 96.7%, 高低一目了然。'
            f'判定「趋势型」要<b>同时</b>满足两条: 后 1/3 均值 / 前 1/3 均值 ≥ 1.5(有长期抬升), '
            f'且对数价格的线性拟合 R² ≥ 0.50(抬升是单调持续的)。只看第一条会误伤 BDI —— '
            f'它涨幅够(1.9x)但 R² 只有 0.31, 是被 2016 年历史大底(290 点)拉出来的剧烈均值回归, '
            f'不是趋势。猪价/纯碱/BDI 这类均值回归品种不满足条件, 仍用绝对分位。'
            f'每个指标下方的灰色小字都写明了它用的是哪种口径、以及 R² 是多少。'
            f'<br><b>曲线背景的两条色带</b>是高低位区(按 A 股习惯: 红=高、绿=低): '
            f'浅红带 = 历史上落到该品种<b>前 25% 高位</b>的取值区间, 浅绿带 = <b>后 25% 低位</b>区间, '
            f'分界虚线就是 75% / 25% 分位对应的数值(趋势型指标按去趋势后的偏离算, '
            f'所以和它的「位置」读数同一口径)。曲线跑进红带就是历史上贵, 跑进绿带就是历史上便宜。'
            f'<br>前瞻统计取主指标「历史上处于相同位置时」其后 6/12 期的后验分布 —— 这是历史统计, '
            f'不是预测承诺。更新 {updated}</div></div>'
            f'<div class="cyc-grid">{"".join(minis)}</div>'
            f'{"".join(cards)}'
            f'{render_clocks(clocks or [])}'
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
            f'⑥<b>位置口径不是统一的</b> —— 趋势型指标(后 1/3 均值 / 前 1/3 均值 ≥ 1.5 且 对数线性 R² ≥ 0.50)'
            f'取「对数价格相对 3 年均线的偏离」的历史分位, 均值回归型指标取绝对价格分位。'
            f'混用两种口径的读数不可直接横向比较; '
            f'⑦用价格/指数衡量周期, 终究会受成本推动与长期结构变化污染, '
            f'所以同一周期内三个指标的分歧本身就是信息, 不要只看均值; '
            f'⑧去趋势口径需要至少 3 年+40 期样本, 短序列(多晶硅 93 周)只能退回绝对分位; '
            f'⑨趋势判据本身也是统计量, 阈值(1.5 / 0.50)是人为设定, 临界品种(沪铝 1.55x、'
            f'BDI 的 R²=0.31)换个窗口可能翻转, 所以每个指标都同时展示两种口径的分位供你自行判断; '
            f'⑩房地产周期里的房价同比是<b>京沪两市</b>口径而非全国 70 城 —— akshare 那个接口'
            f'名字叫 70 城, 实际只返回北京/上海(每月 2 条), 一线城市房价一般是周期的领先段, '
            f'拿它看方向可以, 别当成全国房价水平。'
            f'<br><b>⑪你点名的很多行业指标是付费数据, 本页只能用代理, 已在每个指标处逐条标注:</b>'
            f'造船的「新船订单簿/船队运力」「新接订单/手持订单」「新船价格指数」「二手船价」「拆船量」→ '
            f'Clarksons / VesselsValue / 中国船协付费或停更, 改用 BDI 三兄弟 + 中国船舶股价代理; '
            f'存储的「三大原厂资本开支同比」「DRAM/NAND 现货价」「渠道库存周转天数」「合约价-现货价价差」→ '
            f'集邦 TrendForce 付费 + 无免费接口, 继续用费半/美光/A股板块股价代理; '
            f'铜的「TC/RC 加工费」(SMM/安泰科)、「电网投资增速」→ 无免费接口; '
            f'化工的「行业开工率」「行业库存」(隆众/卓创)→ 付费, 改用 PTA÷原油 等效价差代理; '
            f'生猪的「能繁母猪存栏」(农业农村部接口已失效)、「仔猪价格」「出栏体重」(涌益付费)→ '
            f'改用生猪期货 ÷ 玉米期货 的猪粮比; '
            f'地产的「销售面积增速」「30 城成交」「二手房挂牌/带看」(贝壳)、「土地溢价率」、「去化周期」→ '
            f'全部付费或无接口, 只能用房价+利率+信贷+上游四面包抄; '
            f'「挖机月度销量」(工程机械协会无免费接口)→ 用三一/徐工/恒立股价代理; '
            f'「32/55 吋面板报价」(集邦/群智付费)→ 用京东方/TCL科技/深天马股价代理; '
            f'「秦皇岛动力煤价/港口库存/电厂日耗」(动力煤期货 ZC 已退市, CCTD 付费)→ '
            f'用焦煤/焦炭期货 + 全社会用电量同比代理。'
            f'<b>代理指标不等同于原数据</b>, 请当作方向参考。'
            f'<br><b>⑫美林时钟两版都只用两个宏观轴切四格, 维度远少于现实。</b>'
            f'轴向统一取「相对公认基准的偏离」(中国: 增速的 12 个月变化; 美国: ISM-50、CPI-2%), '
            f'中线固定为 0, 不用历史分位(否则会被 M2/信贷增速的长期下台阶钉死在同一格)。'
            f'框架在 2015 年后的中国和 2020 年后的美国都出现过阶段失灵, 只用于描述环境, 不是买卖信号。'
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
                info = analyze(s, ic["freq"], ic["ma"], unit)
                inds.append(dict(
                    k=ic["k"], name=name, unit=unit, source=src, note=note,
                    freq=ic["freq"], ma=ic["ma"], ref=ref, main=ic.get("main", False),
                    last_date=s.index[-1].strftime("%Y-%m-%d"), n_raw=len(s),
                    stats=info, svg=svg_chart(s, info, unit, ic["ma"], ref,
                                              w=380, h=168, compact=True,
                                              color=_ph_color(info["pct"]),
                                              dev_ma=(DEV_WIN.get(ic["freq"], 156)
                                                      if info.get("is_trend") else None)),
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
                                   color=_ph_color(ind["stats"]["pct"]),
                                   dev_ma=(DEV_WIN.get(ind["freq"], 156)
                                           if ind["stats"].get("is_trend") else None))
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
                        OUT_HTML.write_text(
                            render_fragment(_rows, _meta["updated"],
                                            _meta.get("clocks") or []),
                            encoding="utf-8")
                        print(f"已用缓存数据刷新页面: {OUT_HTML.name} "
                              f"({OUT_HTML.stat().st_size/1024:.0f}KB, "
                              f"{len(_rows)}个周期/{n_ind}个指标"
                              f"/{len(_meta.get('clocks') or [])}个时钟)")
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

    print("开始采集美林时钟 (中国改良版 / 美国原版)…")
    clocks = build_clocks()

    updated = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_ind = sum(len(r["indicators"]) for r in rows)
    payload = dict(
        ver=SCHEMA_VER, updated=updated, failed=failed, clocks=clocks,
        cycles=[{**{k: v for k, v in r.items() if k != "indicators"},
                 "indicators": [{k: v for k, v in i.items() if k != "svg"}
                                for i in r["indicators"]]} for r in rows])
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    OUT_HTML.write_text(render_fragment(rows, updated, clocks), encoding="utf-8")
    print(f"\n已写出: {OUT_JSON.name} ({OUT_JSON.stat().st_size/1024:.0f}KB)  "
          f"{OUT_HTML.name} ({OUT_HTML.stat().st_size/1024:.0f}KB)  "
          f"{len(rows)}个周期 / {n_ind}个指标 / {len(clocks)}个时钟")
    if failed:
        print(f"失败周期: {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
