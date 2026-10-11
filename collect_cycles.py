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
import os, sys, json, traceback, time, re
from datetime import datetime, date
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

SCHEMA_VER = 9          # 结构版本; 与缓存不符则强制重采
                        # (v3: 趋势型 R² 判据; v4: 增速序列前瞻改差值口径;
                        #  v5: 12 周期 + 锂·挖机·面板·煤炭 + 两个美林时钟;
                        #  v6: 补上用户点名的指标 —— 网页爬取(DRAM/NAND现货价、出栏体重、
                        #      挖机销量、造船三大指标) + 行业读数卡)
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


def f_longi():
    """隆基绿能 —— 光伏周期代理。
    光伏主产业链(硅料→硅片→电池→组件)没有免费的完整现货价长序列:
    多晶硅期货 2024 年底才上市, 工业硅 2022 年底才上市, 都不足一轮周期。
    隆基是唯一横跨硅片/电池/组件的一体化龙头, 2012 年上市至今 14 年,
    股价是市场对整个光伏产业链盈利中枢的提前定价 —— 用它补上光伏周期的长度。"""
    return _a_share("601012", "隆基绿能", "光伏一体化龙头·产业链盈利代理")


# ---- 长波(康波)用: 中美 10 年期国债收益率 ----
def _bond_rate(col, name):
    df = ak.bond_zh_us_rate()
    dcol = "日期" if "日期" in df.columns else df.columns[0]
    s = _series(df[dcol], df[col])
    if len(s) < 200:
        raise ValueError("%s 样本过短(%d)" % (name, len(s)))
    return s, f"{name} · 东方财富中美国债收益率日频", "%", {}


def f_us10y():
    return _bond_rate("美国国债收益率10年", "美国 10 年期国债收益率")


def f_cn10y():
    return _bond_rate("中国国债收益率10年", "中国 10 年期国债收益率")


def f_bcti():
    """波罗的海原油运输指数 BCTI —— 油运运价, 与 BDI(干散)互补。
    航运周期里干散和油运经常不同步: 干散看铁矿石/煤炭/粮食, 油运看原油贸易量与航距。"""
    return _macro(ak.macro_shipping_bcti, "波罗的海交易所·BCTI 原油运输指数", "点", "最新值")


# ---- 券商/市场位置: 沪深300 市盈率 + 巴菲特指标(市值/GDP) ----
def f_hs300_pe():
    """沪深300 滚动市盈率(PE-TTM)。
    乐咕(danjuanapp)口径, 2005 年至今。用它判断市场整体估值在历史区间的什么位置 ——
    这是券商周期最直接的『市场位置』读数: 券商股是 beta 放大器, 估值水位决定它能弹多高。"""
    df = ak.stock_index_pe_lg(symbol="沪深300")
    dcol = "日期" if "日期" in df.columns else df.columns[0]
    # 注意列序: '等权滚动市盈率' 排在 '滚动市盈率' 前面, _pick_col 的子串匹配会先命中前者。
    # 等权口径是小盘股加权, 和沪深300 的市值加权不是一回事, 必须精确取 '滚动市盈率'。
    ccol = ("滚动市盈率" if "滚动市盈率" in df.columns
            else _pick_col(df, "滚动市盈率") or _pick_col(df, "静态市盈率"))
    if ccol is None:
        raise ValueError("未找到市盈率列: %s" % list(df.columns))
    s = _series(df[dcol], df[ccol])
    s = s[s > 0]
    if len(s) < 100:
        raise ValueError("市盈率样本过短(%d)" % len(s))
    return s, "沪深300 滚动市盈率 PE-TTM(乐咕/蛋卷)", "倍", {}


def _cn_gdp_ttm():
    """中国 GDP 滚动 4 个季度合计(亿元)。
    macro_china_gdp 给的是『年初至今累计值』('2026年第1-2季度' = 上半年累计),
    而且行序是倒着的。必须先按年拆成单季, 再滚动 4 季求和 ——
    直接拿累计值当分母, 一季度会把比值算出两三倍, 季节性全错。"""
    df = ak.macro_china_gdp()
    qcol, vcol = df.columns[0], _pick_col(df, "国内生产总值", "绝对值")
    cum = {}
    for _, r in df.iterrows():
        m = re.match(r"(\d{4})年第(\d)-?(\d?)季度", str(r[qcol]).strip())
        if not m:
            continue
        y = int(m.group(1))
        q_end = int(m.group(3)) if m.group(3) else int(m.group(2))
        try:
            cum[(y, q_end)] = float(r[vcol])
        except (TypeError, ValueError):
            continue
    if not cum:
        raise ValueError("GDP 季度解析失败")
    q = {}
    for (y, qe), v in cum.items():
        prev = cum.get((y, qe - 1), 0.0) if qe > 1 else 0.0
        if v > prev:
            q[(y, qe)] = v - prev
    ks = sorted(q)
    idx = [pd.Timestamp(y, min(qe * 3, 12), 1) + pd.offsets.MonthEnd(0)
           for y, qe in ks]
    s = pd.Series([q[k] for k in ks], index=pd.DatetimeIndex(idx))
    return s.sort_index().rolling(4).sum().dropna()


def f_mktcap_gdp():
    """巴菲特指标: A股总市值 ÷ 中国 GDP(TTM), %。
    分子分母同步约掉通胀与经济体量增长, 是跨十年可比的『股市相对实体经济的膨胀度』。
    分母 GDP 是季度数, 市值用月度均值对齐到季末。"""
    cap = _mkt_cap()                       # 沪深总市值月度(亿元)
    if cap.max() < 1e3:                    # 单位是亿元, 不该比这个小
        raise ValueError("市值口径异常")
    gdp = _cn_gdp_ttm()
    q = cap.resample("QE").mean().dropna()
    q = q[q.index >= gdp.index[0]]
    g = gdp.reindex(q.index, method="ffill")
    r = (q / g * 100.0).dropna()
    if len(r) < 20:
        raise ValueError("样本过短(%d)" % len(r))
    return (r, "沪深两市总市值 ÷ 中国 GDP(滚动四季, 统计局季度)", "%",
            {"note": "巴菲特指标; 分子分母同步约掉通胀与经济增长"})


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
    """中国 CPI 定基指数, 由月度同比链式推算: 月环比 ≈ (1+同比)^(1/12)。
    用来把名义价格折算成『不变价』, 使 2013 年和 2026 年的价格可比。
    ⚠ 基期必须归一化到 **1.0**(不是 100): 实际价 = 名义价 ÷ 指数,
    若指数基期取 100, 实际价会被整体缩小 100 倍, 量级就荒唐了(原油 400 元/桶 → 2.76)。
    分位对定基年份不敏感(整体乘常数不改变排序), 所以选哪年做基期无所谓。"""
    global _CPI_IDX
    if _CPI_IDX is None:
        s, _, _, _ = f_cpi()
        m = (1.0 + s / 100.0) ** (1.0 / 12.0)
        lvl = m.cumprod()
        _CPI_IDX = lvl / lvl.iloc[0]
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


def f_margin_real():
    """两融余额扣通胀: 名义余额是『元』, 2010 年的 1 元和 2026 年的 1 元不是一回事。
    不折算的话, 光货币通胀就会把余额顶上去, 看起来像杠杆在加, 实际未必。"""
    s, src, unit, meta = _real(f_margin)
    return s, src, unit, {"note": "扣通胀后按CPI首期不变价; 名义值会被货币通胀系统性抬高"}


def _mkt_cap():
    """沪深两市总市值(亿元, 月度) —— 来自 akshare 宏观口径的交易所月报"""
    df = ak.macro_china_stock_market_cap()
    if "数据日期" not in df.columns:
        raise ValueError("市值表结构变了")
    ym = df["数据日期"].astype(str).str.extract(r"(\d{4})年(\d{1,2})月")
    ok = ym[0].notna()
    keys = pd.to_datetime(ym.loc[ok, 0] + "-" + ym.loc[ok, 1].str.zfill(2) + "-01")
    tot = (pd.to_numeric(df.loc[ok, "市价总值-上海"], errors="coerce")
           + pd.to_numeric(df.loc[ok, "市价总值-深圳"], errors="coerce"))
    s = pd.Series(tot.values, index=pd.DatetimeIndex(keys)).dropna().sort_index()
    return s[s > 0]


def f_margin_cap():
    """两融余额 ÷ 沪深总市值(%)。
    这一路才是真正的『杠杆率』: 分子分母同时把货币通胀和股市扩容都约掉了。
    直接看余额会被两件事误导 —— ①钱本身毛了(CPI) ②上市公司越来越多、市值越来越大。
    用比值后, 2015 年杠杆牛的峰值和今天可以直接比。"""
    m = f_margin()[0].resample("MS").last().dropna()
    cap = _mkt_cap()
    # 市值月报比两融日报滞后约 2 个月, 超出的月份没有分母, 宁可不要也不拿旧分母硬凑
    m = m[m.index <= cap.index[-1]]
    r = (m / cap.reindex(m.index, method="ffill")).dropna() * 100.0
    if len(r) < 30:
        raise ValueError("样本过短(%d)" % len(r))
    return r, "沪深交易所两融余额 ÷ 沪深两市总市值(交易所月度)", "%", {
        "note": "分子分母同步约掉通胀与股市扩容; 市值月报滞后约2个月",
        "pos": True,
    }


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


# ══════════════════════════════════════════════════════════════
def f_hog_weight():
    """生猪出栏体重(成交均重, kg) —— 用户点名的『二次育肥与出栏体重』里的体重这一半。
    搜猪网 index_hog_spot_price 一次性给 589 周(2015 起), 是免费源里最长的出栏体重序列。
    逻辑: 二次育肥会把猪压栏养到 130kg+, 体重读数一旦持续冲高, 说明隐性库存正在堆积,
    后面集中出栏就是砸盘 —— 所以体重是猪价顶部区的领先信号之一。"""
    df = ak.index_hog_spot_price()
    s = _series(df["日期"], df["成交均重"])
    s = s[(s > 80) & (s < 200)]
    return s, "搜猪网·全国瘦肉型猪成交均重(出栏体重)", "kg", {}


def f_hog_avg():
    """瘦肉型猪成交均价(元/kg) —— 比『现货价指数』更直观的绝对价位。"""
    df = ak.index_hog_spot_price()
    s = _series(df["日期"], df["成交均价"])
    s = s[(s > 3) & (s < 40)]
    return s, "搜猪网·全国瘦肉型猪成交均价", "元/kg", {}


# ══════════════════════════════════════════════════════════════
# 一之三、网页爬取采集
# ---------------------------------------------------------------------------
# 这一节是专门为「akshare 没有、但网页上确实公开」的指标写的。
# 上一版我把这些一律标成「付费数据无免费接口」就跳过了 —— 那是偷懒, 实际去翻才发现
# 不少是可以拿到的: 中国闪存市场首页就内嵌了 DRAM/NAND 现货价格指数(周度),
# 搜猪网给出栏体重, 中国工程机械工业协会官网每月发挖机销量快报, 中国船协发造船三大指标。
# 共同特点是: 免费源只给【滚动窗口】(最近 7 个月 / 最近十几期), 回溯不了十年。
# 所以全部走「本地累积」: 每次采集把新点并进本地缓存, 样本随时间自然变长。
# ══════════════════════════════════════════════════════════════
CACHE_DIR = SCRIPT_DIR / "crawl_cache"
FLASH_CACHE = CACHE_DIR / "flash_index.json"
EXC_CACHE = CACHE_DIR / "excavator_sales.json"
CANSI_CACHE = CACHE_DIR / "cansi_shipbuilding.json"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _http(url, timeout=20, retries=2, **kw):
    last = None
    for _ in range(retries + 1):
        try:
            r = requests.get(url, timeout=timeout,
                             headers={"User-Agent": _UA}, **kw)
            if r.status_code == 200:
                return r.content.decode("utf-8", "ignore")
            last = "HTTP %d" % r.status_code
        except Exception as e:
            last = e
            time.sleep(0.6)
    raise RuntimeError("抓取失败 %s (%s)" % (url, last))


def _load_cache(p, default):
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _save_cache(p, obj):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------
# 1) 中国闪存市场 —— DRAM / NAND 现货价格指数(周度)
# ---------------------------------------------------------------
def _flash_parse(html):
    """首页 G2Plot 的数据是内联的: var data=[NAND]; var data1=[DRAM];
    日期只有『月-日』, 需要推断年份(滚动窗口最多跨一年)。"""
    blocks = re.findall(r"var\s+data1?\s*=\s*(\[.*?\]);", html, re.S)
    if len(blocks) < 2:
        raise ValueError("未找到指数数据块")
    out = {}
    today = datetime.now().date()
    for key, blk in zip(("nand", "dram"), blocks):
        arr = json.loads(blk)
        if len(arr) < 5:
            raise ValueError("%s 数据点过少" % key)
        year, prev = today.year, None
        dates = []
        for a in arr:
            mm, dd = a["date"].split("-")
            d = datetime(year, int(mm), int(dd)).date()
            if prev is not None and (d - prev).days > 200:
                year -= 1
                d = datetime(year, int(mm), int(dd)).date()
            if d > today:
                year -= 1
                d = datetime(year, int(mm), int(dd)).date()
            prev = d
            dates.append(d.strftime("%Y-%m-%d"))
        out[key] = dict(dates=dates, values=[float(a["index"]) for a in arr])
    return out


FLASH_STALE = ""          # 抓取失败时写入原因, 用于在卡片上标注「数据停在 X」


def crawl_flash_index():
    """返回 {'dram': {dates, values}, 'nand': {...}}, 并与本地缓存合并(滚动窗口累积)

    2026-10 起源站上了人机验证 —— 返回的不再是首页 HTML 而是一个验证页,
    _flash_parse 必然抛「未找到指数数据块」。这条序列本来就是本地逐周累积的,
    所以抓不到时<b>退回本地缓存</b>继续用旧值, 而不是让整张卡片消失:
    卡片凭空不见比数据旧两周糟糕得多(而且会让人误以为是我又偷偷删了指标)。"""
    global FLASH_STALE
    FLASH_STALE = ""
    cache = _load_cache(FLASH_CACHE, {})
    try:
        html = _http("https://www.chinaflashmarket.com/", timeout=25)
        fresh = _flash_parse(html)
    except Exception as e:
        if any((cache.get(k) or {}).get("dates") for k in ("dram", "nand")):
            FLASH_STALE = f"源站已加人机验证, 本次未刷新({type(e).__name__}), 沿用本地累积值"
            print(f"  [WARN] 中国闪存市场抓取失败({type(e).__name__}), 沿用本地累积缓存")
            return cache
        raise
    merged = {}
    for k in ("dram", "nand"):
        old = cache.get(k) or {"dates": [], "values": []}
        m = dict(zip(old["dates"], old["values"]))
        m.update(dict(zip(fresh[k]["dates"], fresh[k]["values"])))
        ks = sorted(m)
        merged[k] = {"dates": ks, "values": [m[x] for x in ks]}
    if merged != cache:
        _save_cache(FLASH_CACHE, merged)
    return merged


def _flash_series(kind):
    d = crawl_flash_index()[kind]
    return pd.Series(d["values"], index=pd.to_datetime(d["dates"])).sort_index()


def f_dram_idx():
    """DRAM 现货价格指数(扣除通胀, 不变价)。
    集邦 TrendForce 的 DRAM 合约价是付费的, 但【中国闪存市场】首页公开一组周度现货价格指数,
    形态与集邦现货价高度一致, 可以直接当 DRAM 现货周期用。"""
    s = _deflate(_flash_series("dram"))
    n = len(s)
    note = "样本%d周" % n
    if n < 104:
        note += " · 免费源只回溯7个月, 靠本地累积增长, 分位参考性有限"
    if FLASH_STALE:
        note += f" · ⚠ {FLASH_STALE}, 最新停在 {s.index[-1].strftime('%Y-%m-%d')}"
    return (s, "中国闪存市场·DRAM现货价格指数(周度, 已按中国CPI折算不变价)",
            "点", {"note": note})


def f_nand_idx():
    s = _deflate(_flash_series("nand"))
    n = len(s)
    note = "样本%d周" % n
    if n < 104:
        note += " · 免费源只回溯7个月, 靠本地累积增长, 分位参考性有限"
    if FLASH_STALE:
        note += f" · ⚠ {FLASH_STALE}, 最新停在 {s.index[-1].strftime('%Y-%m-%d')}"
    return (s, "中国闪存市场·NAND现货价格指数(周度, 已按中国CPI折算不变价)",
            "点", {"note": note})


# ---------------------------------------------------------------
# 2) 中国工程机械工业协会 —— 挖掘机月度销量
# ---------------------------------------------------------------
def _exc_parse(html):
    txt = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt)
    m1 = re.search(r"(\d{4})年(\d{1,2})月销售各类挖掘机([\d,]+)台，同比增长([-\d.]+)%", txt)
    if not m1:
        return None
    m2 = re.search(r"国内销量([\d,]+)台", txt)
    m3 = re.search(r"出口(\d[\d,]*)台", txt)
    return dict(
        ym="%s-%02d" % (m1.group(1), int(m1.group(2))),
        sales=float(m1.group(3).replace(",", "")),
        yoy=float(m1.group(4)),
        dom=float(m2.group(1).replace(",", "")) if m2 else None,
        exp=float(m3.group(1).replace(",", "")) if m3 else None,
    )


def crawl_excavator(deep=False):
    """扫 cncma.org 的文章 ID 找『工程机械行业主要产品销售快报一』, 解析当月挖机销量。
    deep=True 时大范围回溯(首次建库用, 约 2 分钟); 平时只扫最近 260 个 ID 增量更新。"""
    from concurrent.futures import ThreadPoolExecutor

    def one(i):
        try:
            h = _http("http://www.cncma.org/article/%d" % i, timeout=12, retries=1)
        except Exception:
            return None
        if "快报一" not in h[:4000] and "快报一" not in h:
            return None
        r = _exc_parse(h)
        return r

    cache = _load_cache(EXC_CACHE, {})
    top = _load_cache(EXC_CACHE, {}).get("_top_id")
    start = (top + 5) if top else 22740
    lo = 14500 if (deep or not cache) else max(14500, start - 260)
    ids = list(range(start, lo, -1))
    got = []
    try:
        with ThreadPoolExecutor(max_workers=16) as ex:
            for r in ex.map(one, ids):
                if r and r.get("ym"):
                    got.append(r)
    except Exception:
        pass
    for r in got:
        cache[r["ym"]] = r
    if got:
        cache["_top_id"] = max(int(x) for x in
                               [cache.get("_top_id", 0)] + [start])
    _save_cache(EXC_CACHE, cache)
    return {k: v for k, v in cache.items() if not k.startswith("_")}


def excavator_series():
    d = crawl_excavator(deep=not EXC_CACHE.exists())
    if len(d) < 3:
        raise ValueError("挖机销量样本过少(%d)" % len(d))
    ks = sorted(d)
    return (pd.Series([d[k]["sales"] for k in ks],
                      index=pd.to_datetime([k + "-01" for k in ks])),
            ks, d)


# ---------------------------------------------------------------
# 3) 中国船舶工业行业协会 —— 造船三大指标(半年/季度口径)
# ---------------------------------------------------------------
def crawl_cansi():
    """CANSI 的『造船三大指标』只在每半年/季度的运行分析稿里给数, 没有结构化接口,
    且 2002-2025 的汇总表是图片。这里抓最新一期新闻稿的数值, 存进缓存做读数展示。"""
    # ⚠ 首页用 http 能取到, 但详情页会被 301 到 https, requests 跟随后又把
    # host 拼错成 'www.cansi.org.cncms' —— 直接写 https 就绕开这个坑。
    # 另外正文里是「造船完工量3650万载重吨，同比增长51.2%，占世界总量的62.2%」,
    # 三个数字中间夹着一堆话, 用一条长正则很容易因为标点变体失配,
    # 所以改成【分项独立匹配】—— 拿到几个算几个。
    def _num(pat, txt):
        m = re.search(pat, txt)
        return float(m.group(1).replace(",", "")) if m else None

    cache = _load_cache(CANSI_CACHE, {})
    try:
        idx = _http("http://www.cansi.org.cn/", timeout=25)
        links = re.findall(r'href="(/cms/document/\d+\.html)"[^>]*>([^<]{4,60})</a>', idx)
        cand = [(u, t) for u, t in links if ("三大指标" in t or "船舶统计" in t)]
        for u, t in cand[:8]:
            if u in cache.get("_done", []):
                continue
            try:
                txt = re.sub(r"<[^>]+>", " ", _http("https://www.cansi.org.cn" + u, timeout=25))
                txt = re.sub(r"\s+", " ", txt)
                d = dict(
                    title=t.strip(), url="https://www.cansi.org.cn" + u,
                    done=_num(r"造船完工量([\d,]+)万载重吨", txt),
                    done_yoy=_num(r"造船完工量[\d,]+万载重吨，同比增长([-\d.]+)%", txt),
                    neword=_num(r"新接订单量([\d,]+)万载重吨", txt),
                    neword_yoy=_num(r"新接订单量[\d,]+万载重吨，同比增长([-\d.]+)%", txt),
                    holding=_num(r"手持订单量([\d,]+)万载重吨", txt),
                    holding_yoy=_num(r"手持订单量[\d,]+万载重吨，同比增长([-\d.]+)%", txt),
                )
                per = re.search(r"(\d{4})年(1&mdash;(\d{1,2})月|1—(\d{1,2})月|上半年|1-(\d{1,2})月)", txt)
                d["period"] = (per.group(0).replace("&mdash;", "—") if per else "")
                if d["done"] and d["neword"] and d["holding"]:
                    cache[t.strip()] = d
                    cache.setdefault("_done", []).append(u)
                    break
            except Exception:
                continue
    except Exception:
        pass
    _save_cache(CANSI_CACHE, cache)
    return {k: v for k, v in cache.items() if not k.startswith("_")}


# ══════════════════════════════════════════════════════════════
# 一之四、行业读数卡
# ---------------------------------------------------------------------------
# 你点名的很多指标(能繁母猪存栏、30 城成交面积、铜 TC/RC、化工开工率、面板报价、
# 挖机销量、造船订单…)在免费源里【只有新闻/研报口径的最新值, 没有十年历史序列】。
# 拿不到序列就不该假装能算分位 —— 但也不该干脆不显示。
# 所以单独开一格「行业读数」: 只给【最新值 + 同比/环比 + 截止期 + 来源】, 不画曲线不算分位。
#  · src 以 http 开头 = 本次运行时实时爬取
#  · 其余 = 人工核对于研报/新闻/协会公告, 标注核实日期 (见 VERIFIED_DATE)
# ══════════════════════════════════════════════════════════════
VERIFIED_DATE = "2026-10-12"

READINGS = {
    # 造船(供给侧)读数: 这三项都是 CANSI 的造船口径, 归到造船周期而不是航运周期
    "shipbuilding": [
        dict(n="新接订单量", k="neword", u="万载重吨", crawl=("cansi",),
             v="12106", yoy="+173.1%", asof="2026年1-6月", src="中国船舶工业行业协会(人工核实)",
             note="全球份额 82.3%"),
        dict(n="手持订单量", k="holding", u="万载重吨", crawl=("cansi",),
             v="36325", yoy="+54.9%", asof="2026年1-6月", src="中国船舶工业行业协会(人工核实)",
             note="全球份额 71.2%; 除以年交付量≈订单簿可支撑年数"),
        dict(n="造船完工量", k="done", u="万载重吨", crawl=("cansi",),
             v="3650", yoy="+51.2%", asof="2026年1-6月", src="中国船舶工业行业协会(人工核实)",
             note="全球份额 62.2%"),
        dict(n="全球拆船量(Clarksons 年度)", ind="ship_demo", note="见上方曲线"),
        dict(n="新船价格指数(Clarksons)", ind="ship_nbp", note="见上方曲线"),
        dict(n="手持订单÷现有船队运力", ind="ship_obf", note="见上方曲线"),
        dict(n="二手船成交价 / 二手 vs 新船价比", v="—", u="点",
             src="Clarksons Secondhand Price Index(公开散点)",
             note="只在个别年度被披露: 二手船价格指数 2023 全年均值 149.0、2024 年末 176 点"
                  "（较2023年均值+18.1%），同年末新船 189 点 → 二手/新船≈0.93；"
                  "凑不成序列，画不了曲线，先以散点形式记在这里"),
        dict(n="二手船÷新船价格比", v="0.93", u="倍", asof="2024年末",
             src="据上述两个指数算得",
             note="比值>1 说明二手船比新船还贵(即期运力抢手，通常是景气顶部特征)；"
                  "2023年约为 149/178≈0.84"),
    ],
    # 航运(需求侧·运价)读数: 运价是即期读数, 上面四条曲线已经覆盖了主要船型,
    # 这里只补两条拿不到曲线、但值得记的口径
    "shipping": [
        dict(n="BDI 干散货综合运价", ind="bdi", note="见上方曲线"),
        dict(n="BCTI 原油运输运价", ind="bcti", note="见上方曲线(2026-10 补上)"),
        dict(n="集装箱运价(SCFI/CCFI)", v="—", u="点",
             src="上海航运交易所", note="免费接口只有单期快照, 建不了历史序列, 暂缺"),
    ],
    "memory": [
        dict(n="DRAM 现货价(扣通胀)", crawl=("flash", "dram"), u="点",
             note="中国闪存市场周度现货指数, 已折不变价; 集邦合约价为付费"),
        dict(n="NAND 现货价(扣通胀)", crawl=("flash", "nand"), u="点",
             note="同上"),
        dict(n="DRAM+NAND 行业资本开支", ind="mem_capex", note="见上方曲线"),
        dict(n="(原厂)渠道库存周转天数", v="—", u="天", src="集邦/研报(付费)",
             note="免费源无"),
        dict(n="合约价与现货价价差", v="—", u="%", src="集邦 TrendForce(付费)",
             note="免费源无; 现货指数与费半/美光的背离可间接观察"),
    ],
    "copper": [
        dict(n="铜精矿现货 TC 加工费", v="-226.2", u="美元/干吨", asof="2026-09",
             src="SMM 上海有色网(经新闻/研报转载)",
             note="历史新低; 2026 长单 TC=0(史上首次)、2025 长单 21.25、2024 长单 80"),
        dict(n="电解铜样本开工率", v="88.19", u="%", asof="2026-07",
             src="上海钢联/三个皮匠", note="冶炼开工"),
        dict(n="LME 铜库存", ind="lme_cu", note="见上方曲线"),
        dict(n="全球显性铜库存(含保税港)", ind="cu_inv_all", note="见上方曲线"),
        dict(n="上期所铜库存", v="—", u="吨", src="上期所(免费接口仅回溯3个月)",
             note="futures_inventory_em 实测只有 67 个交易日, 算不了分位"),
        dict(n="COMEX 铜库存", v="—", u="短吨", src="CME(付费)", note="免费源无"),
        dict(n="国内电网投资增速", v="—", u="%", src="国家能源局/中电联",
             note="无免费结构化接口"),
    ],
    "chem": [
        dict(n="纯碱行业开工负荷", v="81.9", u="%", asof="2026年1-8月", src="卓创资讯",
             note="较上年降 3.8pct; 9月预计进一步降至 72%"),
        dict(n="纯碱企业库存", v="179", u="万吨", asof="2026-09-17", src="卓创资讯",
             note="同比 +14%; 重碱库存 68.24 万吨, 库存集中西北/西南/华北"),
        dict(n="甲醇装置平均开工负荷", v="72.50", u="%", asof="2026-08-28", src="卓创资讯",
             note="较 7 月降 3.58pct; 西北 78.21%"),
        dict(n="沿海甲醇库存", v="73.55", u="万吨", asof="2026-08-28", src="卓创资讯",
             note="历史中等偏低; 生产企业库存同步去化"),
        dict(n="PTA 开工负荷", v="67.9", u="%", asof="2026-09-03", src="CCF",
             note="聚酯负荷 75.6%"),
        dict(n="原油价格(扣通胀)", ind="sc_real", note="见上方曲线"),
    ],
    "hog": [
        dict(n="能繁母猪存栏量", v="3780", u="万头", asof="2026Q2末", src="国家统计局",
             note="同比 -263万头(-6.5%), 较2025年末减181万头; 正常保有量目标 3750 万头"),
        dict(n="能繁母猪存栏(卓创196样本)", v="850.83", u="万头", asof="2026-08末",
             src="卓创资讯", note="环比 -1.61%, 同比 -7.11%"),
        dict(n="猪粮比", v="<4.6", u="倍", asof="2026-08", src="国家发改委/人民政协网",
             note="跌破一级预警线(5:1), 自繁自养每头亏 190~290 元"),
        dict(n="出栏体重(成交均重)", ind="hog_weight", note="见上方曲线"),
        dict(n="仔猪集贸市场价格", ind="piglet", note="见上方曲线"),
        dict(n="二次育肥存量", v="—", u="万头", src="涌益咨询(付费)",
             note="免费源无; 冻品库容率可作替代: 32.56%(2026年第34周, Mysteel, 同比+15pct)"),
    ],
    "realestate": [
        dict(n="30 大中城市商品房成交面积", v="652.33", u="万㎡", asof="2026-09",
             src="中指院/数据GO", note="环比 +8.28%, 同比 -10.75%, 1-9月累计同比 -7.27%"),
        dict(n="新建商品房销售面积同比", v="-12.1", u="%", asof="2026年1-8月",
             src="国家统计局", note="1-5月为 -10.8%, 跌幅仍在扩大"),
        dict(n="二手房网签面积同比", v="+10.6", u="%", asof="2026年1-8月",
             src="住建部/同花顺", note="1-8月 5.5 亿㎡, 连续多月高于新房"),
        dict(n="库存去化周期(十城面积存销比)", v="22.93", u="月", asof="2026-09",
             src="数据GO", note="一线 12.58 月, 二线 42.47 月; 十城可售 7525.60 万㎡(同比-5.04%)"),
        dict(n="住宅土地成交溢价率", v="13.1", u="%", asof="2026-06(近四周)",
             src="中指院", note="环比 -4.97pct; 商服类 0.45%"),
        dict(n="二手房挂牌量指数", v="1.21", u="指数", asof="2026-06-22",
             src="中指院", note="环比 -0.8%; 挂牌价指数 142.22(环比 -0.06%)"),
        dict(n="按揭利率锚 LPR5Y", v="3.50", u="%", asof="2026-09-20",
             src="中国人民银行", note="曲线已删(长期单边下行、分位无意义); 2019-08 改制后累计下调 115bp"),
        dict(n="居民中长期贷款", v="—", u="亿元", src="央行(免费接口只有总量, 无住户中长期拆分)",
             note="『累计新增人民币贷款同比』是<b>全口径</b>信贷(含制造业/基建/政府债), "
                  "已被挪去当美林时钟中国版的信用轴, 不再作为地产周期读数"),
    ],
    "lithium": [
        dict(n="碳酸锂现货价(扣通胀)", ind="lc_real", note="见上方曲线"),
        dict(n="工业级碳酸锂价格", v="—", u="元/吨", src="SMM/百川(付费)",
             note="广期所碳酸锂期货是最接近现货的免费连续序列"),
    ],
    "excavator": [
        dict(n="挖掘机月度销量", k="sales", u="台", crawl=("exc",), note="含出口"),
        dict(n="挖掘机内销", k="dom", u="台", crawl=("exc",), note="国内销量"),
        dict(n="挖掘机出口", k="exp", u="台", crawl=("exc",),
             note="2026 出口增速显著高于内销"),
        dict(n="挖机销量同比", k="yoy", u="%", crawl=("exc",), note="当月同比"),
    ],
    "panel": [
        dict(n="32 吋电视面板报价", v="35.0", u="美元/片", asof="2026-09",
             src="TrendForce 集邦咨询(经新浪财经等转载)", note="环比 8 月持平(34.0~36.0)"),
        dict(n="55 吋电视面板报价", v="123", u="美元/片", asof="2026-09",
             src="TrendForce 集邦咨询", note="环比持平(116~126); 65吋 173、43吋 63"),
        dict(n="面板厂稼动率", v="79.6", u="%", asof="2026-10(预计)", src="TrendForce",
             note="五代及以上大世代线, 环比 -4.2pct; 京东方/华星/惠科十一减产挺价"),
    ],
    "coal": [
        dict(n="秦皇岛动力煤价(Q5500)", ind="coal_qhd", note="见上方曲线(2016-2025Q4)"),
        dict(n="环渤海港口库存", v="—", u="万吨",
             src="CCTD(付费)", note="秦皇岛港场存日报网页存在但历史数据需订阅"),
        dict(n="电厂日耗", v="—", u="万吨/日", src="CCTD/沿海电厂(付费)",
             note="用『全社会用电量同比』作需求侧替代, 见上方曲线"),
    ],
    "solar": [
        dict(n="国产多晶硅(致密料)价格", ind="polysilicon", note="见上方曲线(2018-2025 半年度)"),
    ],
    "broker": [
        dict(n="两融余额(名义)", ind="margin", note="见上方曲线"),
        dict(n="两融余额(实际·扣通胀)", ind="margin_real", note="见上方曲线"),
        dict(n="两融余额÷总市值(杠杆率)", ind="margin_cap", note="见上方曲线"),
    ],
}


def build_readings(rows=None):
    """把 READINGS 里 crawl= 的项用实时爬虫填值, 返回 {cycle_key: [读数...]}
    ind= 的项表示『这个指标已经在上面画了曲线』, 直接从 rows 里把当前值搬过来,
    免得出现『见上方曲线』却显示成「— 无免费源」的自相矛盾。"""
    ind_lookup = {}
    for r in (rows or []):
        for i in r["indicators"]:
            ind_lookup[(r["key"], i["k"])] = i
            ind_lookup[(r["key"], i["name"])] = i
    try:
        exc = excavator_series()
        exc_map, exc_ks = exc[2], exc[1]
        exc_last = exc_map[exc_ks[-1]]
        exc_asof = exc_ks[-1]
        exc_note = "共%d个月(自%s), 协会月度快报爬取+本地累积" % (len(exc_ks), exc_ks[0])
    except Exception as e:
        exc_map, exc_last, exc_asof, exc_note = {}, {}, "", "爬取失败: %s" % str(e)[:60]

    try:
        cansi = crawl_cansi()
        cansi_last = list(cansi.values())[0] if cansi else {}
    except Exception as e:
        cansi, cansi_last = {}, {}

    flash = {}
    try:
        d = crawl_flash_index()
        flash = {"dram": (d["dram"]["values"][-1], d["dram"]["dates"][-1], len(d["dram"]["dates"])),
                 "nand": (d["nand"]["values"][-1], d["nand"]["dates"][-1], len(d["nand"]["dates"]))}
    except Exception:
        flash = {}

    out = {}
    for ck, items in READINGS.items():
        rows = []
        for it in items:
            r = dict(it)
            r.pop("crawl", None)
            cr = it.get("crawl")
            # crawl 可以写成 ("exc",) 只带来源(字段名沿用 k), 也可以带第二个元素指定字段
            fld = (cr[1] if len(cr) > 1 else it.get("k")) if cr else None
            filled = False
            if cr and cr[0] == "exc":
                k = fld
                v = exc_last.get(k)
                if v is not None:
                    vs = ("%+.2f%%" % v) if k == "yoy" else ("%.0f" % v)
                    r.update(v=vs, asof=exc_asof,
                             yoy=("%+.2f%%" % exc_last["yoy"]) if k == "sales" else None,
                             src="中国工程机械工业协会 cncma.org(实时爬取)",
                             note=exc_note if k != "yoy" else "当月同比")
                    filled = True
            elif cr and cr[0] == "cansi":
                k = fld
                if cansi_last.get(k) is not None:
                    r.update(v=("%.0f" % cansi_last[k]),
                             yoy=("%+.1f%%" % cansi_last[k + "_yoy"]),
                             asof=cansi_last.get("period", ""),
                             src="中国船舶工业行业协会 cansi.org.cn(实时爬取)")
                    filled = True
            elif cr and cr[0] == "flash":
                k = cr[1]
                if k in flash:
                    v, dt, n = flash[k]
                    r.update(v=("%.1f" % v), asof=dt,
                             src="中国闪存市场(实时爬取)",
                             note="样本%d周, 已折不变价" % n)
                    filled = True
            # ind= 的项: 该指标已在上方画了曲线, 直接把当前值搬过来
            if not filled and it.get("ind"):
                ii = ind_lookup.get((ck, it["ind"]))
                if ii is not None:
                    r.update(v=_fmt(ii["stats"]["cur"], ii["unit"]),
                             u=ii["unit"] or "",
                             asof=ii["last_date"], src=ii["source"],
                             note=it.get("note", "") or "该指标已在上方画出曲线")
                    filled = True
            if not filled and "v" not in r:
                r.setdefault("v", "—")
                r.setdefault("src", "无免费源")
            if "src" not in r:
                r["src"] = "研报/新闻核实于 " + VERIFIED_DATE
            if "asof" not in r:
                r["asof"] = VERIFIED_DATE
            rows.append(r)
        out[ck] = rows
    return out


def render_readings(items):
    if not items:
        return ""
    trs = []
    for r in items:
        val = str(r.get("v", "—"))
        miss = val in ("—", "", "None")
        yoy = r.get("yoy")
        ytxt = ""
        if yoy:
            try:
                f = float(str(yoy).replace("%", "").replace("+", ""))
                col = "#f85149" if f >= 0 else "#3fb950"
                ytxt = f' <span style="color:{col};font-weight:600">{yoy}</span>'
            except Exception:
                ytxt = " " + str(yoy)
        trs.append(
            f'<tr><td class="rn">{r["n"]}</td>'
            f'<td class="rv" style="color:{"#6e7681" if miss else "#e6edf3"}">{val}'
            f'<span class="ru">{r.get("u","")}</span>{ytxt}</td>'
            f'<td class="rd">{r.get("asof","")}</td>'
            f'<td class="rs">{r.get("src","")}</td>'
            f'<td class="rn2">{r.get("note","")}</td></tr>')
    return ('<div class="cyc-read"><div class="rh">行业读数 · 你点名但免费源拿不到长序列的指标'
            '(只给最新值, 不画曲线不算分位)</div>'
            '<table class="rt"><thead><tr><th>指标</th><th>最新值</th><th>截止</th>'
            '<th>来源</th><th>备注</th></tr></thead><tbody>'
            + "".join(trs) + "</tbody></table></div>")

FE = {
    "turnover": f_turnover, "margin": f_margin, "broker_index": f_broker_index,
    "margin_real": f_margin_real, "margin_cap": f_margin_cap,
    "hog_spot": f_hog_spot, "hog_fut": f_hog_fut, "corn": f_corn,
    "realestate": f_realestate, "house_price": f_house_price, "construction": f_construction,
    "cu": f_cu, "al": f_al, "lme_cu": f_lme_cu,
    "sa": f_sa, "ta": f_ta, "ma": f_ma,
    "bdi": f_bdi, "bci": f_bci, "bpi": f_bpi, "bcti": f_bcti,
    "si": f_si, "ps": f_ps, "fg": f_fg,
    # ---- 长波(康波) / 市场位置 ----
    "us10y": f_us10y, "cn10y": f_cn10y,
    "hs300_pe": f_hs300_pe, "mktcap_gdp": f_mktcap_gdp,
    "longi": f_longi,
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
    # ---- 网页爬取(滚动窗口 + 本地累积) ----
    "dram_idx": f_dram_idx, "nand_idx": f_nand_idx,
    "hog_weight": f_hog_weight, "hog_avg": f_hog_avg,
}


# ══════════════════════════════════════════════════════════════
# 一之五、研报 / 新闻里的人工提取序列 (MSER)
#
# 这一节收录的是"翻遍免费接口也没有, 但在研报 PDF 和行业新闻里公开披露过历史数据"的指标。
# 做法是: 找到公开披露那段历史数据的研报/新闻 → 把原文的数字原样落成序列 → 画到页面上。
#
# ⚠ 使用约束(务必读完):
#   1. 频率多为年度/季度/半年度, 远粗于页面上其他周度曲线, 所以**样本点数很少**,
#      历史分位只有"排第几"的粗糙含义, 不要当作精确周期定位。
#   2. 这些是**静态表, 不会自动更新**。MSER_VER 变了就是人工复核过一次。
#   3. 每个点都标了出处(src), 一旦与原报告不符, 以原报告为准。
#   4. 上面的 real_* 曲线能自动跑的, 一律优先用自动源; 这里是补位, 不是替 App 兜底。
# ══════════════════════════════════════════════════════════════
MSER_VER = "2026-10-12"   # 人工提取/复核日期

# f: 原始颗粒度, 仅用于展示; A=年度 Q=季度 H=半年 M=月度
# inv: True 表示这是"反指标"(数值越低=周期越热), 分析时把分位翻转过来
# pts: [(日期, 数值)] —— 日期尽量取该点实际所属期末, 缺失的年份就是原文献没披露, 不插值、不脑补
MSER = {
    # ════════ 航运 ════════
    "ship_demo": dict(
        n="全球拆船量", u="万载重吨", f="A", inv=True,
        src="Clarksons 原始数据(经东吴证券《船舶制造产业链》拆船量/交付量表)",
        asof="2025年",
        note="反指标: 拆得越多说明航运越惨、船东在割肉; 2024 年 890 万吨是历史极低",
        pts=[("2006-12-31", 661), ("2007-12-31", 612), ("2008-12-31", 1425),
             ("2009-12-31", 3344), ("2010-12-31", 2795), ("2011-12-31", 4270),
             ("2012-12-31", 5834), ("2013-12-31", 4707), ("2014-12-31", 3374),
             ("2015-12-31", 3923), ("2016-12-31", 4470), ("2017-12-31", 3562),
             ("2018-12-31", 3063), ("2019-12-31", 1759), ("2020-12-31", 2481),
             ("2021-12-31", 2278), ("2022-12-31", 1102), ("2023-12-31", 1096),
             ("2024-12-31", 890), ("2025-12-31", 1188)]),

    "ship_nbp": dict(
        n="克拉克森新船价格指数", u="点", f="A",
        src="Clarksons 公开口径(经百度百科条目/航运媒体与公司年报综述汇编)",
        asof="2026-08",
        note="名义美元, 未扣通胀; 2008-08 约 191 点为历史最高, 2007 年底 184.83",
        pts=[("2007-12-31", 184.83), ("2008-12-31", 191.0), ("2016-12-31", 123.0),
             ("2017-12-31", 124.0), ("2021-12-31", 153.6), ("2022-12-31", 161.8),
             ("2023-12-31", 177.97), ("2024-12-31", 189.0), ("2025-12-31", 184.65),
             ("2026-08-31", 186.34)]),

    "ship_obf": dict(
        n="手持订单 ÷ 现有船队运力", u="%", f="A",
        src="UNCTAD《海运述评2025》+ ABS《Sustainability Outlook 2024》+ Clarksons 口径",
        asof="2025年初",
        note="口径在总吨/载重吨之间混用, 只作长期趋势参考; 2009 年 52% 是超级周期顶点",
        pts=[("2008-12-31", 50.0), ("2009-12-31", 52.0), ("2014-12-31", 19.0),
             ("2015-12-31", 19.0), ("2020-12-31", 9.2), ("2023-12-31", 11.1),
             ("2024-12-31", 12.3), ("2025-12-31", 15.0)]),

    # ════════ 存储 ════════
    "mem_capex": dict(
        n="DRAM+NAND 行业资本开支", u="十亿美元", f="A",
        src="TrendForce(经群益投顾《記憶體2026》资本支出汇总表)",
        asof="2026F",
        note="DRAM 与 NAND 两项相加; 2025/2026 为 TrendForce 预测值。"
             "同比链: 2023 -38.9% → 2024 +29.0% → 2025F +52.1% → 2026F +17.8%",
        pts=[("2022-12-31", 62.431), ("2023-12-31", 38.177), ("2024-12-31", 49.234),
             ("2025-12-31", 74.893), ("2026-12-31", 88.257)]),

    # ════════ 铜 ════════
    "cu_inv_all": dict(
        n="全球显性铜库存", u="万吨", f="A", inv=True,
        src="中信期货《铜:上攻的羊群效应》(LME+SHFE+COMEX+上海保税港合计)",
        asof="2024年",
        note="反指标: 库存高=需求弱/铜价承压。含保税港, 与页面单独那条 LME 口径不可直接比",
        pts=[("2008-12-31", 20), ("2009-12-31", 35), ("2010-12-31", 78),
             ("2011-12-31", 55), ("2012-12-31", 65), ("2013-12-31", 45),
             ("2014-12-31", 90), ("2015-12-31", 25), ("2016-12-31", 120),
             ("2017-12-31", 145), ("2018-12-31", 135), ("2019-12-31", 100),
             ("2020-12-31", 85), ("2021-12-31", 60), ("2022-12-31", 55),
             ("2023-12-31", 30), ("2024-12-31", 45)]),

    # ════════ 煤炭 ════════
    "coal_qhd": dict(
        n="秦皇岛动力煤(Q5500)现货均价", u="元/吨", f="Q",
        src="华电国际公告回复(信永中和引 Wind《2016年以来煤炭价格变化情况》表)",
        asof="2025Q4",
        note="现货均价口径; 同期长协均价为 2021:648 → 2022:722 → 2023:714 → 2024:701 → 2025Q4:685。"
             "2022-02 发改委把长协价限定在 570-770 元/吨",
        pts=[("2016-12-31", 471), ("2017-12-31", 638), ("2018-12-31", 647),
             ("2019-12-31", 587), ("2020-12-31", 571), ("2021-12-31", 857),
             ("2022-12-31", 1268), ("2023-12-31", 966), ("2024-12-31", 856),
             ("2025-03-31", 723), ("2025-06-30", 633), ("2025-09-30", 669),
             ("2025-12-31", 778)]),

    # ════════ 光伏 ════════
    "polysilicon": dict(
        n="国产多晶硅(致密料)价格", u="元/kg", f="H",
        src="Wind(经光大证券研究所《硅料价格处于2011年8月以来底部》数据表)",
        asof="2025-06",
        note="半年度点位。2020-05 低点 58.4 → 2022 高点 306 → 2023 全年跌 68.8% → 2024-25 低位盘整",
        pts=[("2018-12-31", 85), ("2019-06-30", 80), ("2019-12-31", 75),
             ("2020-06-30", 60), ("2020-12-31", 100), ("2021-06-30", 210),
             ("2021-12-31", 270), ("2022-06-30", 300), ("2022-12-31", 310),
             ("2023-06-30", 170), ("2023-12-31", 70), ("2024-06-30", 45),
             ("2024-12-31", 40), ("2025-06-30", 50)]),

    # ════════ 生猪 ════════
    "piglet": dict(
        n="仔猪集贸市场价格", u="元/kg", f="M",
        src="农业农村部畜牧兽医局(经农小蜂/养猪网月度监测报告转载)",
        asof="2025-12",
        note="月度但不连续 —— 源头的月报本身就有断档, 缺失月份没补。"
             "2025-12 的 23.39 已逼近 2022-05 的 23.01 历史低位",
        pts=[("2022-04-30", 25.77), ("2022-05-31", 23.01), ("2022-11-30", 46.03),
             ("2024-01-31", 23.22), ("2024-04-30", 36.73), ("2024-08-31", 44.27),
             ("2024-12-31", 33.17), ("2025-04-30", 39.16), ("2025-05-31", 39.14),
             ("2025-12-31", 23.39)]),
}


def _mser_fetch(k):
    """把 MSER[k] 的静态点包装成与自动采集同签名的 fetch 函数"""
    def _f():
        m = MSER[k]
        idx = pd.to_datetime([d for d, _ in m["pts"]])
        s = pd.Series([float(v) for _, v in m["pts"]], index=idx).sort_index()
        return s, m["src"], m["u"], {
            "note": m["note"], "lowfreq": True, "gran": m.get("f", ""),
            "asof": m.get("asof", ""),
        }
    return _f


for _k in MSER:
    FE[_k] = _mser_fetch(_k)


# ══════════════════════════════════════════════════════════════
# 二、周期定义 —— 每个周期 3 个并列指标
# ══════════════════════════════════════════════════════════════
def I(k, nm, fn, unit="元/吨", freq="W", agg="last", ma=52, ref=None, note="",
      main=False, pos=True, inv=False, min_n=30, force_dev=False):
    """pos=True: 过滤 <=0 的脏数据(运价指数/库存等不可能为负, 但源数据偶有负值毛刺)
    inv=True: 反指标 —— 数值越低代表周期越热(如库存、拆船量)。
              这类直接按原始值排分位会读反(库存历史最低→算成周期低位, 实际是铜最紧的时候),
              所以analyze()里把分位/变化率/偏离全部翻过来。
    min_n:   最低样本点数。研报提取的年度序列只有十几个点, 用默认 30 会被误杀。
    force_dev: 强制走去趋势口径(不看自动判定的 trend/R²)。
               用于用户点名要求去趋势的品种(如隆基): 它 2012→2021 涨几十倍再跌回来,
               对数线性 R² 不一定过阈值, 但绝对价格图同样是一条向上的斜线、读不出高低。"""
    return dict(k=k, name=nm, fetch=fn, unit=unit, freq=freq, agg=agg,
                ma=ma, ref=ref, note=note, main=main, pos=pos,
                inv=inv, min_n=min_n, force_dev=force_dev)


CYCLES = [
    # ══════════════════════════════════════════════════════════
    # ── 造船周期(供给侧)───────────────────────────────────────
    # 和下面的航运周期是两回事: 造船看的是【船厂产能与订单簿】(供给侧, 10-12 年),
    # 航运看的是【运价】(需求侧, 高频)。历史上两者经常背离 ——
    # 典型是 2016 年 BDI 跌到 290 点历史大底时, 造船订单簿还在消化 2013-14 年的订单;
    # 反过来 2021-22 运价暴涨, 新船价格指数才刚起步(下单到交付要 2-3 年, 供给跟不上)。
    dict(key="shipbuilding", name="造船周期", tag="供给侧 · 10-12 年",
         logic="造船是重资产长周期: 新船下单到交付 2-3 年, 船坞产能 10 年才能扩一轮, "
               "所以供给调整极慢, 一旦供需错配就是 3-5 年大行情。"
               "<b>这一页是供给侧</b>, 读的是『船厂手里有多少活、新船卖多少钱、老船拆了多少』: "
               "<b>新船价格指数</b>是造船周期最直接的景气读数(船价涨=船厂议价权强); "
               "<b>手持订单÷现有船队运力</b>是未来 2-3 年的确定供给(占比高=船厂活多, 但也意味着"
               "未来交付压力大、运力要过剩); <b>中国船舶</b>股价是市场对在手订单×船价的提前定价。"
               "<b>『拆船量』是反指标</b>: 拆得越多说明船东在割肉、运价越惨, 船东宁愿把船当废铁卖; "
               "反过来拆船量趴在历史低位说明老船还在赚钱、没人愿意拆 —— 它的分位已经翻转过来, "
               "读数高代表造船/航运景气, 不再是字面上拆船量本身的高低。"
               "<b>关于你列的六个造船指标:</b>『新接订单量/手持订单量』仍在下方『行业读数』卡里, "
               "用的是<b>中国船舶工业行业协会(CANSI)月度公报</b>的实时爬取。"
               "其余三项<b>原本判定为「Clarksons 付费墙后、拿不到」而直接跳过, 这判断是偷懒, 已补上</b>: "
               "『全球拆船量(含年度交付量)』在<b>东吴证券船舶行业深度</b>里有一张 2006-2025 的完整表; "
               "『新船价格指数』『手持订单÷现有船队运力』在<b>UNCTAD《海运述评》</b>和各大券商年报综述里"
               "逐年披露。这三条都按你的指示从公开研报里扒出来了, 见下方曲线。"
               "唯一还没救回来的是<b>二手船成交价</b> —— 只在 2023/2024 两个年度点上公开, "
               "凑不出序列, 仍留在读数卡。",
         indicators=[
             I("ship_nbp", "克拉克森新船价格指数", "ship_nbp", "点", "M", "last", 3, None,
               main=True, min_n=8, note="Clarksons 公开口径汇编, 2007 至今"),
             I("ship_obf", "手持订单÷现有船队运力", "ship_obf", "%", "M", "last", 3, None,
               min_n=8, note="UNCTAD/ABS/Clarksons, 2008 至今"),
             I("ship_demo", "全球拆船量", "ship_demo", "万载重吨", "M", "last", 3, None,
               inv=True, min_n=12, note="Clarksons/东吴证券表, 2006-2025 年度"),
             I("cssc", "中国船舶(订单簿代理)", "cssc", "元", "W", "last", 52, None,
               note="代理指标: 船厂股价≈在手订单×船价"),
         ]),

    # ── 航运周期(需求侧·运价)───────────────────────────────────
    dict(key="shipping", name="航运周期", tag="需求侧 · 运价",
         logic="这一页是<b>需求侧的运价</b>, 和上面的造船周期(供给侧)分开看。"
               "BDI 是干散货综合运价, BCI(海岬型/铁矿石煤炭)和 BPI(巴拿马型/粮食)是分船型拆解 —— "
               "三个同涨说明是全局需求回暖, 只有单一船型涨则多半是局部扰动。"
               "<b>BCTI 原油运输指数</b>是补上的第四条: 干散和油运经常不同步 —— "
               "干散看铁矿石/煤炭/粮食的实物贸易量, 油运看原油贸易量与航距(俄乌之后航距拉长, "
               "油运走出独立行情)。运价是即期供需的读数, 波动幅度远大于造船景气, "
               "所以它更像『当前赚不赚钱』, 而造船周期才是『未来有没有船』。",
         indicators=[
             I("bdi", "BDI 干散货综合", "bdi", "点", "W", "last", 52, None, main=True),
             I("bci", "BCI 海岬型", "bci", "点", "W", "last", 52, None),
             I("bpi", "BPI 巴拿马型", "bpi", "点", "W", "last", 52, None),
             I("bcti", "BCTI 原油运输", "bcti", "点", "W", "last", 52, None,
               note="油运运价, 与干散常不同步"),
         ]),

    # ══════════════════════════════════════════════════════════
    dict(key="memory", name="存储周期", tag="3-4 年 · DRAM/NAND",
         logic="存储是半导体里最纯粹的『蛛网周期』: 原厂建一条产线 2-3 年, 一旦同时扩产就过剩, "
               "一旦同时砍资本开支就缺货。历史上 2018 顶 → 2019 崩 → 2020-21 缺货 → 2022-23 崩 → "
               "2024 起 AI/HBM 拉动新一轮。保留下来的三条线各有分工: "
               "<b>费半</b>是全球半导体景气的基准(含存储原厂, 弹性被非存储业务稀释但样本最长、最连续); "
               "<b>DRAM / NAND 现货价</b>是存储周期真正的价格本体(最贴近供需); "
               "<b>三大原厂资本开支</b>是最领先的供给端读数。"
               "(原『美光科技 MU』『A股存储板块』两条曲线已按你的要求删除 —— "
               "MU 是单一公司股价、夹杂公司自身经营与 AI/HBM 叙事, 板块指数又是国内情绪映射, "
               "两者都不能代表存储本身的周期位置, 留着反而稀释下面三条硬指标。)"
               "<b>DRAM / NAND 现货价已经补上了</b>: 集邦 TrendForce 的合约价是付费的, "
               "但<b>中国闪存市场(chinaflashmarket.com)首页公开一组周度 DRAM / NAND 现货价格指数</b>, "
               "形态与集邦现货价高度一致, 已按中国 CPI 折算成不变价挂在下面两条曲线上。"
               "缺点是<b>免费源只回溯 7 个月</b>, 所以这两条曲线是自 2026-02 起<b>本地累积</b>的, "
               "样本会越跑越长; 现阶段别拿它的分位当周期位置看, 看方向和拐点就行。"
               "剩余三项都在 TrendForce / 集邦付费口径里: 『渠道库存周转天数』『合约价-现货价价差』"
               "两家都只随机散在付费研报里, 仍然取不到。"
               "但<b>『三大原厂资本开支』已经补上了</b> —— TrendForce 的 DRAM+NAND 年度资本开支汇总表"
               "被<b>群益投顾《記憶體 2026》</b>完整列出来了(2022-2026F), 见下方曲线。"
               "这是整个存储周期最领先的指标: 资本开支决定 <b>18 个月后</b>的位元供给, "
               "2023 年砍了 39% → 2024 年缺货涨价 → 2025 年暴力加回去 52% → 2026 年的供给就靠这张表预估。"
               "缺点是 TrendForce 只有年度数、且最新两年是<b>预测值(F)</b>, 样本仅 5 点, 分位很粗。",
         indicators=[
             I("sox", "费城半导体指数", "sox", "点", "W", "last", 52, None, main=True),
             I("dram_idx", "DRAM 现货价指数(扣通胀)", "dram_idx", "点", "W", "last", 26, None,
               note="中国闪存市场周度(免费源回溯7个月, 本地累积)"),
             I("nand_idx", "NAND 现货价指数(扣通胀)", "nand_idx", "点", "W", "last", 26, None,
               note="中国闪存市场周度(免费源回溯7个月, 本地累积)"),
             # ↓ 研报提取序列(TrendForce 年度汇总表, 见 MSER)
             I("mem_capex", "DRAM+NAND 行业资本开支", "mem_capex", "十亿美元", "M", "last", 3, None,
               min_n=5, note="TrendForce/群益投顾表, 2022-2026F; 末两年为预测值"),
         ]),

    # ══════════════════════════════════════════════════════════
    dict(key="copper", name="工业金属(铜)", tag="经济晴雨表",
         logic="铜矿建设周期 5-7 年, 供给端几乎调不动, 所以需求一波动就体现在库存和价格上。"
               "沪铜价格反映国内定价, 沪铝是同板块共振验证(铝走强说明整体需求不弱), "
               "LME 铜库存是海外供需的蓄水池。"
               "<b>库存和价格是反向的</b>: 库存低 → 紧张 → 价格高。所以看这一组时, "
               "价格分位高 + 库存分位低 = 真紧张; 价格高 + 库存也高 = 多半是金融属性推的, 不牢靠。"
               "<b>『三大交易所显性库存』已经补上了</b>: 上期所和 COMEX 单独的免费接口确实只回溯几个月, "
               "但<b>中信期货《铜:上攻的羊群效应》</b>里有一张 2008-2024 的"
               "『LME+SHFE+COMEX+上海保税港』合计库存表, 已按用户指示扒出来画在下面。"
               "<b>它和上面的 LME 库存一样是反指标</b> —— 库存越高说明需求越弱、铜越不紧张, "
               "分位已翻转: 读数高 = 周期位置高(即库存处于历史低位、铜紧)。"
               "注意口径差异: 这条含保税港且是<b>年度</b>点, 上面那条 LME 是<b>周度</b>, "
               "数值量级不可直接互比, 看各自的相对位置就行。"
               "仍未拿到的是『上期所铜库存』单独序列和『电网投资增速』的结构化接口 —— "
               "这两项写在读数卡里。",
         indicators=[
             I("cu", "沪铜期货主力", "cu", "元/吨", "W", "last", 52, None, main=True),
             I("al", "沪铝期货主力", "al", "元/吨", "W", "last", 52, None, note="板块共振"),
             I("lme_cu", "LME 铜库存", "lme_cu", "吨", "W", "last", 52, None,
               note="滞后约3个月; 反着看: 库存低=紧张"),
             # ↓ 研报提取序列(年度, 见 MSER)
             I("cu_inv_all", "全球显性铜库存(含保税港)", "cu_inv_all", "万吨", "M", "last", 3, None,
               inv=True, min_n=12, note="中信期货表 LME+SHFE+COMEX+保税港, 2008-2024 年度"),
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
               "现货价是当下供需结果, 期货价包含市场对 10 个月后供给的预期, "
               "猪粮比则是官方口径的盈亏刻度(发改委: 5:1 亏损、6:1 盈亏平衡、9:1 高盈利)。"
               "猪价跌 + 饲料稳 = 亏损去化; 猪价跌 + 饲料涨 = 加速出清(底部信号)。"
               "(原『玉米期货主力』单列一条已按你的要求删除 —— "
               "玉米是<b>成本端</b>不是猪周期本身, 而且它已经被『猪粮比』完整吃进分母里: "
               "猪粮比 = 生猪期货 ÷ 玉米期货, 玉米一涨猪粮比就掉, 信息没有丢, "
               "单挂一条玉米反而让人误把饲料行情当成猪周期位置看。)"
               "<b>『出栏体重』也补上了</b>: 搜猪网 index_hog_spot_price 一次性给到 589 周"
               "(2015 年至今)的成交均重, 挂在下面。用法是: 二次育肥会把猪压栏养到 130kg 以上, "
               "体重读数持续冲高说明<b>隐性库存正在堆积</b>, 后面集中出栏就是砸盘, "
               "所以体重偏高往往是猪价顶部区的领先信号。"
               "<b>『仔猪价格』也补上了</b>: 农业农村部畜牧兽医局每月发布《全国主要猪产品及饲料集贸市场"
               "价格监测月报》, 各家资讯转载时会把月度数放出来 —— 已逐期摘出来挂在第 7 条。"
               "用法: 仔猪价格是<b>补栏意愿</b>的直接读数, 它比猪价早 5-6 个月 —— 因为补栏后 6 个月才出栏, "
               "所以仔猪价见顶往往领先猪价见顶半年左右。"
               "<b>注意那条曲线是月度但不连续的</b>: 源披露的月份本身就断档(尤其 2022-12~2023-12 整段缺失), "
               "我没有插值脑补, 缺的就是缺的, 所以那一段是直线连接, 别当成真实路径。"
               "剩下『能繁母猪存栏量』(统计局只有季度口径)和『二次育肥存量』(涌益咨询付费周报)仍在读数卡里, "
               "能繁母猪我核到了 2026Q2 末的 3780 万头(同比 -6.5%)。"
               "注意本页的猪粮比是<b>生猪期货 ÷ 玉米期货</b>算的, 不是发改委的出场价/批发价口径, "
               "量级和历史形态对得上, 但绝对值会有偏差。",
         indicators=[
             I("hog_spot", "生猪现货价指数", "hog_spot", "", "W", "last", 52, None, main=True),
             I("hog_fut", "生猪期货主力", "hog_fut", "元/吨", "W", "last", 52, None),
             I("pig_grain", "猪粮比(期货口径)", "pig_grain", "倍", "W", "last", 52,
               (6, "6倍=盈亏平衡线"), note="分母已含玉米期货成本端"),
             I("hog_weight", "出栏体重(成交均重)", "hog_weight", "kg", "W", "last", 52, None,
               note="二次育肥压栏会推高读数, 是顶部领先信号"),
             I("hog_avg", "瘦肉型猪成交均价", "hog_avg", "元/kg", "W", "last", 52, None),
             # ↓ 研报/新闻提取序列(农业农村部月度监测, 原月报本身就有断档)
             I("piglet", "仔猪集贸市场价格", "piglet", "元/kg", "M", "last", 3, None,
               min_n=8, note="农业农村部月报摘录, 缺失月份未插值"),
         ]),

    # ══════════════════════════════════════════════════════════
    dict(key="realestate", name="房地产周期", tag="18-20 年大周期",
         logic="产业链最长、牵连最广, 与城镇化/人口/居民杠杆绑定。"
               "房价同比是终端价格(最核心), 建材指数是上游需求(水泥玻璃)的真实回声。"
               "<b>三条曲线已按你的要求删除, 逐条说明原因:</b> "
               "①『5年期以上贷款基准利率』—— 2019 年改制后 LPR5Y 几乎只降不升、长期单边下行, "
               "看分位永远是 0%, 而且它已经<b>被下面『京沪房价同比』部分消化</b>, 单挂一条只是重复刻度; "
               "②『累计新增人民币贷款同比』—— 这是<b>全口径社融信贷</b>, 制造业/基建/政府债都混在里面, "
               "地产只占一小块, 拿它当地产周期的信用水位会严重误读, "
               "而且它现在被<b>挪去当美林时钟中国版的『信用轴』</b>用了(见页面最下方), 那里才是它的正确位置; "
               "③『螺纹钢期货主力』—— 螺纹的下游还有基建、制造业厂房、出口, 地产占比已降到三成出头, "
               "把它当地产链专属读数同样是污染, 真正干净的地产上游回声是<b>建材指数(水泥玻璃)</b>, 保留。"
               "删完之后这一页只剩『价格 + 上游需求』两条硬曲线, "
               "所以下面的读数卡反而变得更重要 —— 销售面积、去化周期、土地溢价率、挂牌量这些"
               "地产专属指标全部在那里, 请一并看。"
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
             I("construction", "中国建材指数", "construction", "点", "W", "last", 52, None,
               note="水泥玻璃, 地产上游最干净的回声"),
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
               "电厂日耗和港口库存是动力煤最直接的读数, 但这两项都在 CCTD / 秦皇岛海运煤炭交易市场的"
               "付费墙后。"
               "<b>『秦皇岛动力煤价(Q5500)』已经补上了</b> —— 上一版我写了『动力煤期货 ZC 已退市(实测只到 "
               "2022-12), 无免费连续序列, 取不到』, 这判断太糙: <b>华电国际的公告回复函</b>(信永中和会计师"
               "事务所出具, 引 Wind)里有一张《2016 年以来煤炭价格变化情况》表, 逐年给了秦皇岛港 5500 大卡的"
               "现货均价和长协均价, 2025 年还拆到了季度。已按用户指示扒出来挂在下面。"
               "这是整页唯一一条真正的<b>秦皇岛港现货</b>序列, 之前三面夹的做法里有两层是代理, "
               "现在有了本尊作锚。"
               "仍未拿到: 『环渤海港口库存』『电厂日耗』(均为 CCTD 订阅数据), 仍在读数卡里。",
         indicators=[
             I("jm", "焦煤期货主力", "jm", "元/吨", "W", "last", 52, None, main=True),
             I("jc", "焦炭期货主力", "jc", "元/吨", "W", "last", 52, None, note="焦煤下游"),
             I("elec", "全社会用电量同比", "elec", "%", "M", "last", 24, None,
               note="动力煤需求同步指标", pos=False),
             # ↓ 研报提取序列(Wind/华电国际公告, 见 MSER)
             I("coal_qhd", "秦皇岛动力煤(Q5500)现货均价", "coal_qhd", "元/吨", "M", "last", 4,
               ref=(770, "770=2022年长协上限"), min_n=10,
               note="华电国际公告函引Wind, 2016-2025Q4; 年度+季度混频"),
         ]),

    # ══════════════════════════════════════════════════════════
    dict(key="solar", name="光伏周期", tag="产能过剩",
         logic="技术迭代快、产能扩张猛, 硅料→硅片→电池片全是产能游戏, 一扩产就价格战。"
               "工业硅是上游原料, 多晶硅是核心环节。"
               "<b>『玻璃期货主力』已按你的要求删掉</b> —— 它 2021 年才上市, 只有四五年样本, "
               "一轮周期都没走完, 挂在光伏周期里只会拉低这一页的可信度; 而且玻璃的下游主要是"
               "房地产(浮法玻璃七成用于建筑), 它跟的是地产链不是光伏链, 口径本来就不对。"
               "<b>补上的是『隆基绿能』</b>: 光伏主产业链没有免费的完整现货价长序列(多晶硅期货 "
               "2024 年底才上市、工业硅 2022 年底才上市, 都不足一轮), 而隆基 2012 年上市至今 14 年, "
               "是唯一横跨硅片/电池/组件的一体化龙头, 股价相当于市场对整个产业链盈利中枢的提前定价 —— "
               "它把这一页的时间轴从 4 年拉长到 14 年, 2012-2018 的补贴时代、"
               "2019-2022 的平价爆发、2023 至今的产能出清三段全在里面了。"
               "<b>隆基这条曲线做了去趋势处理</b>: 2012→2021 它涨了几十倍, 绝对价格图只会看到一条"
               "向上的斜线, 2021 年顶部和 2012 年底部在图上长得一模一样; 画成"
               "『对数价格相对 3 年均线的偏离』之后, 2021 年的泡沫顶和 2024 年的出清底立刻分得开。"
               "<b>还有一条真正的多晶硅现货长序列</b>: <b>光大证券研究所</b>在"
               "《硅料价格处于 2011 年 8 月以来底部》里给出过 Wind 口径的国产硅料价格表"
               "(2018-2025, 半年度), 已摘出来挂在最后一条。"
               "两条多晶硅要对照看 —— 上面那条是 2024-12 才上市的期货(短、但连续), "
               "下面这条是 2018 年至今的现货长史(长、但半年一个点), 判断 2021-22 那轮"
               "『58 → 306 元/kg 涨 424%、再跌 68.8%』的完整周期只能靠下面这条。",
         indicators=[
             I("si", "工业硅期货主力", "si", "元/吨", "W", "last", 52, None, main=True),
             I("longi", "隆基绿能(去趋势)", "longi", "元", "W", "last", 52, None,
               force_dev=True, note="光伏一体化龙头·产业链盈利代理"),
             I("ps", "多晶硅期货主力", "ps", "元/吨", "W", "last", 52, None, note="样本短(2024-12起)"),
             # ↓ 研报提取序列(Wind/光大证券, 见 MSER)
             I("polysilicon", "国产多晶硅(致密料)价格", "polysilicon", "元/kg", "M", "last", 3, None,
               min_n=10, note="Wind/光大证券表, 2018-2025 半年度"),
         ]),

    # ══════════════════════════════════════════════════════════
    dict(key="broker", name="券商周期", tag="牛市杠杆",
         logic="券商是纯行情放大器, 没有生产, 营收≈佣金+自营+两融利息。三个指标互相验证: "
               "成交额是佣金基数, 两融余额是杠杆情绪, 券商指数是市场给的定价。"
               "成交额日均>1万亿全行业躺赚, <8000亿整体亏损。最值得看的是<b>背离</b> —— "
               "成交额和两融都在历史高位、券商股价却在中低位, 说明市场根本没给券商定价, 这种时候它才有性价比。"
               "<b>关于『两融余额要不要扣通胀』—— 要, 而且光扣通胀还不够:</b>"
               "余额是以『元』计价的名义存量, 它的长期抬升来自三件完全不同的事: "
               "①<b>货币通胀</b>(CPI 累计 1.44x, 2010 年以来的 1 元 ≠ 今天的 1 元); "
               "②<b>股市扩容</b>(沪深总市值从 2010 年的约 30 万亿涨到 2026 年的约 116 万亿, 近 4 倍); "
               "③<b>杠杆率本身的变化</b>(这才是唯一真正属于周期的东西)。"
               "原来的单条曲线虽然已经用『去趋势』把①②③一起抹平了, 但它回答不了"
               "『现在和 2015 年杠杆牛比到底谁更疯』这种跨周期问题。所以下面拆成三条: "
               "<b>名义余额 / 实际余额(扣 CPI) / 占市值比重</b>。"
               "三个口径给出的结论差别很大, 直接上数(2015 年那波杠杆牛的峰值在 <b>2015-06-18</b>, "
               "名义 <b>22730 亿</b>、实际 <b>18464 亿</b>):<br>"
               "① <b>名义</b>: 今天 25660 亿, 比那波峰值还高 <b>13%</b>, 而且 2026-06-25 曾冲到 "
               "<b>30237 亿</b>的历史最高 —— 只看这条会得出『杠杆史上最疯』的结论;<br>"
               "② <b>实际(扣 CPI)</b>: 今天 17813 亿, 比那波峰值<b>低 4%</b> —— 名义上的新高, "
               "扣掉货币通胀后其实还没到 2015 年的水平;<br>"
               "③ <b>占市值比重</b>: 今天 <b>2.29%</b>, 2015-06 峰值 <b>3.51%</b>, 只有那波的六成半。"
               "<b>三条放一起才读得对: 今天的杠杆『绝对量很大、但相对市场体量不算极端』</b> —— "
               "水池涨得比杠杆快。不过要注意, 2.29% 已经处于 2010 年以来的 <b>89% 分位</b>, "
               "除了 2015 那波极端泡沫, 它比 2018、2021、2024 各轮都高, 所以是『偏高但没疯』, 不是『不热』。"
               "<b>判断杠杆是否过热, 看占市值比重那条, 别看余额绝对值 —— 绝对值会被通胀和股市扩容一起顶上去。</b>"
               "<br><b>另外补了两条『市场位置』读数(你要的市盈率历史区间 + 市值/GDP):</b> "
               "券商是纯 beta 放大器, 它自己没有周期, 真正决定它弹多高的是<b>整个市场的位置</b>。"
               "①<b>沪深300 滚动市盈率</b>(乐咕口径, 2005 年至今 21 年)—— 直接读估值在历史区间的哪一段, "
               "这条不用去趋势, 市盈率本身就是均值回归的; "
               "②<b>巴菲特指标 = 沪深总市值 ÷ 中国 GDP(TTM)</b> —— 分子分母同步约掉通胀与经济增长, "
               "是跨十年可比的『股市相对实体经济的膨胀度』。"
               "两条都是<b>中国口径</b>(你要的也是中国的)。看券商时把它们当成『天花板高度』: "
               "估值已经在历史高位时, 成交额再放量, 券商也弹不了多少 —— 因为市场已经不便宜了。",
         indicators=[
             I("turnover", "两市日成交额", "turnover", "亿元", "W", "mean", 52, (10000, "1万亿(行业盈亏线)")),
             I("margin", "两融余额(名义)", "margin", "亿元", "W", "last", 52, None, main=True,
               note="未扣通胀; 已在统计上去趋势"),
             I("margin_real", "两融余额(实际·扣通胀)", "margin_real", "亿元", "W", "last", 52, None,
               note="按中国CPI折算不变价, 与2015年可比"),
             I("margin_cap", "两融余额÷总市值(杠杆率)", "margin_cap", "%", "M", "last", 24, None,
               min_n=60, note="真正的杠杆率口径; 2015-06峰值为3.51%"),
             I("broker_index", "证券公司指数", "broker_index", "点", "W", "last", 52, None),
             # ↓ 市场位置(中国)
             I("hs300_pe", "沪深300 市盈率(PE-TTM)", "hs300_pe", "倍", "W", "last", 52, None,
               min_n=100, note="乐咕/蛋卷口径, 2005 至今; 市场估值水位"),
             I("mktcap_gdp", "A股总市值÷GDP(巴菲特指标)", "mktcap_gdp", "%", "M", "last", 12, None,
               min_n=20, note="分子分母同步约掉通胀与经济增长"),
         ]),
]


# ══════════════════════════════════════════════════════════════
# 二之二、页面展示顺序
# ══════════════════════════════════════════════════════════════
# 用户指定: 康波(独立版块) → 美林时钟(独立版块) → 券商 → 存储 → 铜 → 锂 → 化工 →
#           造船 → 航运 → 其余按原顺序跟在后面。
# 康波和时钟不是 CYCLES 里的条目(它们没有『综合位置%』这种读数), 单独渲染在最前面。
CYCLE_ORDER = ["broker", "memory", "copper", "lithium", "chem",
               "shipbuilding", "shipping", "hog", "realestate",
               "excavator", "panel", "coal", "solar"]
CYCLES = sorted(CYCLES,
                key=lambda c: CYCLE_ORDER.index(c["key"]) if c["key"] in CYCLE_ORDER else 99)


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


def analyze(s, freq, ma, unit="", inv=False, force_dev=False):
    x = s.values.astype(float)
    n = len(x)
    cur = float(x[-1])
    # 反指标用「历史里有多少 >= 当前」当分位, 否则库存/拆船量这类会读反
    pct_abs = float((x >= cur).mean()) if inv else float((x <= cur).mean())

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
    _auto = (pct_trend is not None) and (trend >= TREND_TH) and (tr2 >= TREND_R2)
    is_trend = _auto or (force_dev and pct_trend is not None)
    forced = bool(is_trend and not _auto)   # 自动判定没过阈值, 但用户点名要去趋势
    # 趋势型品种用去趋势口径, 其余(猪价/BDI/纯碱等均值回归型)用绝对分位
    pct = pct_trend if is_trend else pct_abs

    win = 52 if freq == "W" else 12
    recent = float(np.mean(x[-win:])) if n >= win else float(np.mean(x))
    prev = float(np.mean(x[-2 * win:-win])) if n >= 2 * win else float(np.mean(x[:max(1, n // 2)]))
    chg = (recent / prev - 1.0) if prev else 0.0
    mav = float(np.mean(x[-ma:])) if n >= ma else float(np.mean(x))
    mav_dev = (cur / mav - 1.0) if mav else 0.0
    # 反指标: 「涨」在语义上是周期走弱, 所以变化率与偏离都要取反, 别让读数自相矛盾
    if inv:
        chg, mav_dev = -chg, -mav_dev

    if pct <= 0.25:
        pos = "周期低位"
    elif pct >= 0.75:
        pos = "周期高位"
    else:
        pos = "周期中位"
    phase = phase_of(pct, chg)

    h1, h2 = (26, 52) if freq == "W" else (6, 12)
    # 反指标要按【翻转后】的位置去匹配历史相似时点, 否则找的是语义相反的样本
    ref = dseq if is_trend else (-x if inv else None)
    # 本身就是「增速%」的序列(房价同比等)可以过零, 必须用差值口径, 否则比值会炸
    fmode = "diff" if unit == "%" else "ratio"
    return dict(cur=cur, pct=pct, pct_abs=pct_abs, pct_trend=pct_trend,
                trend=trend, r2=tr2, is_trend=is_trend, forced=forced,
                pos=pos, phase=phase,
                chg=chg, dev=mav_dev, mav=mav, inv=inv,
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
    # 绝对价格图对费半/沪铜/螺纹这类长期向上的品种是没用的 —— 曲线一路向右上, 2002 年的大底和
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

    # ── X 轴刻度 ──
    # 老做法是「等分 5 / 7 个点」, 落点取任意索引, 刻度日期常常是 2019-03 这种随机月份,
    # 而且横向其实还空着一半, 白白浪费。现在改成四件事:
    #   ① 候选刻度走<b>自然日历网格</b>(月 / 季 / 半年 / 年), 落点全部对齐到 1、4、7、10 月这种整点;
    #   ② <b>密度自适应</b> —— 按标签实际像素宽度算能塞几个就塞几个, 不再写死 5 个;
    #   ③ <b>换年的刻度只写年份</b>(2014 / 2015…), 同一年里的刻度<b>只写月份</b>(04 / 07 / 10),
    #      年份用亮色、月份用暗色区分。这样单个标签从 7 字符压到 4 / 2 字符,
    #      同一张图能放的刻度数直接翻倍(实测 5 个 → 12~16 个);
    #   ④ 网格里没被选中打字的点补成<b>不打字的细刻度</b>, 目视参照更密但不糊。
    d0, d1 = xs[idx[0]], xs[idx[-1]]
    span_d = max((d1 - d0).days, 1)
    if span_d <= 200:
        freq = "MS"                       # 半年以内: 逐月
    elif span_d <= 2600:
        freq = "QS"                       # 7 年以内: 逐季
    elif span_d <= 6200:
        freq = "2QS"                      # 17 年以内: 半年
    else:
        freq = "YS"                       # 超长序列: 逐年
    if len(idx) <= 20:
        # 样本本来就少(研报提取的年度/低频序列): 直接拿<b>每个真实数据点</b>当候选刻度。
        # 这类序列日期间隔还不均匀, 硬套日历网格会让刻度落在两个点中间, 读起来反而误导。
        cand = [xs[i] for i in idx]
    else:
        cand = list(pd.date_range(d0.replace(day=1), d1 + pd.Timedelta(days=1), freq=freq))
    ts = np.array([xs[i].value for i in idx], dtype=float)

    def _pos(d):
        """把某个日历日换算成图上的(小数)点位"""
        tv = np.datetime64(d).astype("datetime64[ns]").astype("float64")
        return float(np.clip(np.interp(tv, ts, np.arange(len(idx), dtype=float)),
                             0.0, len(idx) - 1.0))

    def _lab(seq):
        """换年写 YYYY, 年内只写 MM; 末尾永远写完整 YYYY-MM(那是最新日期, 必须看清)"""
        out, py = [], None
        for j, d in enumerate(seq):
            if j == len(seq) - 1:
                out.append(d.strftime("%Y-%m"))
            elif py is None or d.year != py:
                out.append(str(d.year))
            else:
                out.append(d.strftime("%m"))
            py = d.year
        return out

    def _w(l):
        # 数字按等宽 0.56em 估, 短横线约 0.33em
        return sum(0.56 if c.isdigit() else 0.33 for c in l) * fs

    # 刻度数不超过数据点数(样本本来就少的低频序列别搞得刻度比点还多)
    cap_n = min(len(idx), 40)
    chosen = None
    for step in (1, 2, 3, 4, 6, 8, 12, 16, 24, 40, 60):
        seq = cand[::step]
        if len(seq) < 2:
            break
        labs = _lab(seq)
        if len(labs) <= cap_n and sum(_w(l) + 5.0 for l in labs) <= pw * 1.0:
            chosen = list(zip(seq, labs))
            break
    if chosen is None:
        chosen = [(d0, str(d0.year)), (d1, d1.strftime("%Y-%m"))]
    # 右端必须落在最新一天上, 否则看不出曲线读到哪个月
    if _pos(chosen[-1][0]) < len(idx) - 1 - 0.75:
        chosen.append((d1, d1.strftime("%Y-%m")))

    # 靠得太近就丢掉靠前那个 —— 最新日期优先保留
    keep = []
    for p, l in sorted(((_pos(d), l) for d, l in chosen), key=lambda t: t[0]):
        while keep:
            pp, ll = keep[-1]
            if X(p) - X(pp) >= (_w(l) + _w(ll)) / 2 + 3.5:
                break
            if len(keep) == 1 and pp <= 0.01:
                break                      # 起点锚点保底, 不删
            keep.pop()
        keep.append((p, l))
    # 尾刻度必须写完整年月(前面为了省地方可能被压成 MM)
    if len(keep) >= 2:
        keep[-1] = (keep[-1][0], d1.strftime("%Y-%m"))

    # 细刻度: 网格里没被选中的点, 只画短线不打字
    if len(cand) <= 150:
        _used = {d for d, _ in chosen}
        for d in cand:
            if d in _used:
                continue
            xx = X(_pos(d))
            parts.append(f'<line x1="{xx:.1f}" y1="{T+ph:.1f}" x2="{xx:.1f}" y2="{T+ph+2:.1f}" '
                         f'stroke="#30363d" stroke-width="1"/>')

    for j, (p, l) in enumerate(keep):
        x = X(p)
        # 首尾用 start/end 锚点, 避免第一个标签向左溢出压到 Y 轴刻度文字上
        anchor = "start" if j == 0 else ("end" if j == len(keep) - 1 else "middle")
        major = (len(l) >= 4)              # 年份刻度(或末尾完整年月)用亮色长刻度
        parts.append(f'<line x1="{x:.1f}" y1="{T+ph:.1f}" '
                     f'x2="{x:.1f}" y2="{T+ph+(4 if major else 2.5):.1f}" '
                     f'stroke="{"#484f58" if major else "#3d444d"}" stroke-width="1"/>')
        parts.append(f'<text x="{x:.1f}" y="{h-5}" '
                     f'fill="{"#adbac7" if major else "#8b949e"}" '
                     f'font-size="{fs}" text-anchor="{anchor}">{l}</text>')
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
CLOCK_PATH_N = 0           # 轨迹画最近多少期; 0 = 画全部历史
                           # (改成季度口径后总共也就 ~74 期, 完全可以整段画完。
                           #  之前只画最近 60 个月是个坑: 2021 之后美国 CPI 一直在 2% 上方,
                           #  于是最近 5 年被钉在钟的上半部, 看上去像「从没进过衰退期和复苏期」——
                           #  实际上把历史拉长到 2008 年, 四格都走过。)
CLOCK_DIFF_Q = 4           # 变化口径的季度数(4 个季度 = 12 个月)
CLOCK_RECENT_Q = 8         # 轨迹里把最近多少个季度画亮(2 年)


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
              "本页『信用』用<b>累计新增人民币贷款同比</b>, 『货币』用<b>M2 同比</b>; "
              "两轴都取<b>4 个季度(12 个月)的变化量</b>, 中线固定为 0 —— "
              "不用水平分位, 因为 M2 和信贷增速是长期下台阶的, 用分位会把钟钉死在「双紧」那一格十年不动。"
              "<b>这个钟有三处口径妥协, 逐条说明:</b> "
              "①信用轴标准做法是<b>社融存量同比</b>, 但免费接口只有 2015 年起的社融<b>增量</b>, "
              "凑不出 2008 年以来的长序列, 只好退而用人民行的累计新增人民币贷款同比; "
              "②这个指标每年 1 月会把累计口径重置成单月值、春节错位又让 1-2 月极不稳定, "
              "改成<b>季度取季末值</b>(3/6/9/12 月)之后, 每个季度都是「年内累计到季末」, "
              "跨年相减口径一致, 年初噪声自然被稀释(这也是改成季度口径最重要的收益); "
              "③四象限的名字和美国版<b>不落在同一个角</b>(中国版衰退期在左上、美国版在左下), "
              "这不是写错 —— 两轴语义不同(货币信用 vs 增长通胀), 但<b>旋转方向一致, 都是顺时针</b>。"
              "<b>这是框架不是定律</b>: 2015 年之后中国信用周期被地产和隐性债务主导, "
              "资产映射出现过明显失灵(典型是 2016-17 紧货币+宽信用下商品与地产同时走强), "
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
              "<b>这个钟有三处口径妥协, 逐条说明:</b> "
              "①ISM 是<b>制造业</b> PMI, 而美国经济里服务业占八成以上, 拿它当「增长」会漏掉服务端的冷热; "
              "②美联储 <b>2012 年才正式确立 2% 通胀目标</b>, 2008-2011 那一段是回溯套用, 严格说不严谨; "
              "③两轴都用<b>水平偏离</b>而不是变化量 —— 这与原版美林一致(产出缺口本身就是可正可负的水平量), "
              "但代价是<b>半格黏性极强</b>: 2021 之后美国 CPI 一直压在 2% 上方, "
              "最近五年整段被钉在钟的上半部。之前只画最近 60 个月, 看上去就像"
              "「美国从没进过衰退期和复苏期」—— 这确实是画法的问题, 不是数据的问题。"
              "<b>把 2008 年以来的全部季度画出来, 四格其实都走过</b>(见下方历史占比): "
              "2008 危机、2013-2015 那轮低通胀、2020 疫情后都在钟的下半部, "
              "2016-17 和 2021 在右上(过热), 2022-25 在左上(滞胀)。旋转方向仍是经典美林的<b>顺时针</b>。"
              "<b>框架不等于定律</b>: 2022 年美联储激进加息时「衰退期买债券」彻底失灵 —— "
              "那年美国长久期国债是有史以来最差的年份之一, 因为这轮通胀是供给端驱动的, "
              "央行没法像过去那样用降息对冲。",
    ),
]


def _monthly(s):
    try:
        return s.resample("ME").last().dropna()
    except ValueError:
        return s.resample("M").last().dropna()


def _quarterly(s, how="last"):
    """月度 → 季度。
    how="last" : 取季末值。用于「累计同比」这类口径 —— 累计新增人民币贷款同比在每年 1 月
                 会重置成单月值、春节错位又会把 1-2 月打得很难看; 取季末(3/6/9/12 月)后
                 每个季度都是「年内累计到该季末」, 跨年相减时口径一致, 噪音自然被稀释。
    how="mean" : 取季内均值。用于 ISM / CPI 这类水平值, 纯粹为了降噪。"""
    try:
        r = s.resample("QE")
    except ValueError:
        r = s.resample("Q")
    return (r.mean() if how == "mean" else r.last()).dropna()


def _qlab(ts):
    """Timestamp → '2019Q3'"""
    return f"{ts.year}Q{(ts.month - 1) // 3 + 1}"


def _clock_fetch(fetch_key):
    """返回 (月度序列, 数据源说明, 单位)"""
    res = FE[fetch_key]()
    return _monthly(res[0]), res[1], res[2]


def build_clocks():
    out = []
    for cfg in CLOCK_DEFS:
        try:
            if cfg["key"] == "us_ml":
                # 美国两轴是「相对公认基准的偏离」(水平口径), 不是同比变化
                xm, xsrc, _ = _clock_fetch("ism")
                ym, ysrc, _ = _clock_fetch("usa_cpi")
                # 水平值用季内均值降噪(ISM 月度抖动很大)
                xlev, ylev = _quarterly(xm, "mean"), _quarterly(ym, "mean")
                xs, ys = xlev - 50.0, ylev - 2.0
            else:
                xm, xsrc, _ = _clock_fetch(cfg["x"]["fetch"])
                ym, ysrc, _ = _clock_fetch(cfg["y"]["fetch"])
                # 累计同比类取季末值, 保证跨年相减时口径一致(见 _quarterly 注释)
                xlev, ylev = _quarterly(xm, "last"), _quarterly(ym, "last")
                xs = (xlev - xlev.shift(CLOCK_DIFF_Q)).dropna()
                ys = (ylev - ylev.shift(CLOCK_DIFF_Q)).dropna()
            df = pd.concat([xs.rename("x"), ys.rename("y")], axis=1).dropna()
            if len(df) < 12:
                raise ValueError("对齐后样本过短(%d 个季度)" % len(df))
            # 水平值也换成季度口径, 免得出现「轴向截至 2026Q3、水平值却写某个月」的自相矛盾
            xlev, ylev = xlev.reindex(df.index), ylev.reindex(df.index)
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
            _sl = slice(-CLOCK_PATH_N, None) if CLOCK_PATH_N else slice(None)
            path = [[_qlab(d), round(float(a), 3), round(float(b), 3)]
                    for d, a, b in zip(df.index[_sl],
                                       df["x"].iloc[_sl],
                                       df["y"].iloc[_sl])]
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
                asof=_qlab(df.index[-1]), n=int(len(df)), gran="季度",
                span=(_qlab(df.index[0]), _qlab(df.index[-1])),
                share=share, path=path, quads=cfg["quads"],
                # 水平值取与两轴对齐的那个月(df 的最后一行), 免得出现
                # 「轴向截至 2026-08、水平值却写 2026-09」的自相矛盾
                xlev=round(float(xlev.reindex(df.index).dropna().iloc[-1]), 3),
                ylev=round(float(ylev.reindex(df.index).dropna().iloc[-1]), 3),
                xlev_date=_qlab(df.index[-1]),
                ylev_date=_qlab(df.index[-1]),
            ))
            print(f"  [OK] {cfg['name']}  当前象限 {qpos} {q['name']} · {q['asset']} "
                  f"(x={cx:+.2f}pp, y={cy:+.2f}pp, 截至 {out[-1]['asof']}, "
                  f"{out[-1]['n']} 个季度)")
        except Exception as e:
            print(f"  [FAIL] 时钟 {cfg['name']}: {type(e).__name__}: {str(e)[:100]}")
    return out


# ══════════════════════════════════════════════════════════════
# 四之三、康波周期(长波 · 50-60 年)
# ══════════════════════════════════════════════════════════════
# 先说清楚能做什么、不能做什么:
# 康波是 50-60 年的长波, 免费接口里最长的一条连续序列是美债 10Y(1990 起, 36 年),
# 连半轮都不到。**所以这里不做「精确判定当前处于康波第几阶段」** —— 那是拿 36 年数据
# 去外推 60 年周期, 属于编故事。这里做的是两件诚实的事:
#   ① 把康波四阶段的框架和历史区间(主流划分)摆出来, 并标明争议在哪;
#   ② 用可得的最长序列(中美 10Y 国债收益率)给一个【长波定位】——
#      利率长波是康波最可靠的同步/滞后代理之一: 长波上行(资金成本抬升)对应衰退-萧条,
#      长波下行对应复苏-繁荣。并且把它和康波阶段的对应关系写在旁边, 置信度也写出来。
# 中美分开列, 因为这两个经济体当前处在**长波的两个相反方向**上 —— 这是当下最值得记的一件事。
KWAVE_QUADS = [
    dict(name="复苏期", tint="rgba(63,185,80,0.12)",
         feat="利率与商品双双触底、产能已出清完毕、新技术开始商业化扩散",
         hist="第五波: 1982-1991(信息技术起步)",
         rate="利率长波下行至历史低位"),
    dict(name="繁荣期", tint="rgba(248,81,73,0.10)",
         feat="增长强、通胀温和、资产全面上涨、新技术渗透率快速提升",
         hist="第五波: 1991-2000(互联网普及)",
         rate="利率自低位温和回升, 绝对水平仍低"),
    dict(name="衰退期", tint="rgba(210,153,34,0.12)",
         feat="利率飙升、商品见顶、滞胀、资产泡沫破裂、贫富分化加剧",
         hist="第五波: 2000-2008(互联网泡沫→商品牛市→次贷)",
         rate="利率长波上行, 水位抬到均值以上"),
    dict(name="萧条期", tint="rgba(88,166,255,0.10)",
         feat="商品熊市、资产去杠杆、增长中枢下移, 但下一轮新技术在酝酿",
         hist="第五波: 2008-2020(?); 之后是否进入第六波复苏争议极大",
         rate="利率自高位回落(央行兜底), 但增长也起不来"),
]

KWAVE_SERIES = [
    dict(key="us10y", name="美国 10Y 国债收益率", region="美国",
         src_note="东方财富中美国债收益率日频, 1990-12 起"),
    dict(key="cn10y", name="中国 10Y 国债收益率", region="中国",
         src_note="东方财富中美国债收益率日频, 2002-01 起"),
]


def _kwave_stage(pct, d):
    """按利率长波的【方向】+【水位】给一个近似定位。
    d = 近 3 年均值 − 前 10 年均值(pp): 正=长波上行, 负=长波下行。"""
    if d > 0.5:
        return ("衰退期" if pct >= 0.5 else "萧条期→复苏期过渡",
                "利率长波上行" + ("且水位已在均值以上" if pct >= 0.5 else "但绝对水位仍偏低"))
    if d < -0.5:
        return ("繁荣期" if pct > 0.5 else "萧条期末 / 下一波孕育期",
                "利率长波下行" + ("但绝对水位仍偏高" if pct > 0.5 else "且已压到历史低位"))
    return ("方向不明(长波横盘)", "近 3 年与之前 10 年均值几乎持平")


def build_kwave():
    """返回康波长波定位所需的数据。失败返回 None(页面会显示失败原因, 不硬凑)。"""
    out = []
    for cfg in KWAVE_SERIES:
        try:
            s, src, unit, _m = FE[cfg["key"]]()
            m = s.resample("ME").last().dropna()
            if len(m) < 120:
                raise ValueError("月度样本过短(%d)" % len(m))
            cur = float(m.iloc[-1])
            pct = float((m <= cur).mean())
            r3 = float(m.iloc[-36:].mean())
            p10 = float(m.iloc[-156:-36].mean()) if len(m) > 156 else float(m.iloc[:-36].mean())
            d = r3 - p10
            stage, why = _kwave_stage(pct, d)
            info = analyze(m, "M", 12, unit)
            out.append(dict(
                key=cfg["key"], name=cfg["name"], region=cfg["region"],
                cur=cur, pct=pct, d=d, stage=stage, why=why,
                lo=float(m.min()), hi=float(m.max()),
                lo_d=m.idxmin().strftime("%Y-%m"), hi_d=m.idxmax().strftime("%Y-%m"),
                r3=r3, p10=p10, n=len(m), asof=m.index[-1].strftime("%Y-%m"),
                span=(m.index[0].strftime("%Y-%m"), m.index[-1].strftime("%Y-%m")),
                src=src, unit=unit, info=info, series=m,
            ))
            print(f"  [OK] 康波·{cfg['name']}  当前 {cur:.2f}% · 分位 {pct*100:.0f}% · "
                  f"长波{d:+.2f}pp → {stage}")
        except Exception as e:
            print(f"  [FAIL] 康波 {cfg['name']}: {type(e).__name__}: {str(e)[:90]}")
    return out


def render_kwave(kw):
    if not kw:
        return ('<div class="cyc-card" style="margin-top:22px">'
                '<div class="t" style="font-size:15px;font-weight:600;color:#e6edf3;'
                'margin-bottom:6px">康波周期 · 长波(50-60 年)</div>'
                '<div class="s" style="font-size:12px;color:#8b949e;line-height:1.75">'
                '本次采集未拿到长波序列, 本版块留空 —— 不拿短数据硬凑一个长波结论。</div></div>')

    def _row_for(region):
        return next((x for x in kw if x["region"] == region), None)

    us, cn = _row_for("美国"), _row_for("中国")

    # ---- 表 1: 四阶段框架 × 中美当前定位 ----
    qrows = []
    for q in KWAVE_QUADS:
        on_us = us and us["stage"].startswith(q["name"][:2])
        on_cn = cn and cn["stage"].startswith(q["name"][:2])
        qrows.append(
            f'<tr style="{"background:rgba(248,81,73,0.09)" if (on_us or on_cn) else ""}">'
            f'<td style="padding:5px 8px;font-size:12px;font-weight:600;color:#e6edf3">'
            f'{q["name"]}</td>'
            f'<td style="padding:5px 8px;font-size:10.5px;color:#8b949e;line-height:1.55">'
            f'{q["feat"]}</td>'
            f'<td style="padding:5px 8px;font-size:10.5px;color:#6e7681;line-height:1.55">'
            f'{q["hist"]}<br><span style="color:#484f58">{q["rate"]}</span></td>'
            f'<td style="padding:5px 8px;font-size:11px;text-align:center;'
            f'color:{"#f85149" if on_us else "#484f58"}">{"◀ 美国在此" if on_us else "—"}</td>'
            f'<td style="padding:5px 8px;font-size:11px;text-align:center;'
            f'color:{"#f85149" if on_cn else "#484f58"}">{"◀ 中国在此" if on_cn else "—"}</td>'
            f'</tr>')

    # ---- 表 2: 判定依据 ----
    erows = []
    for x in kw:
        erows.append(
            f'<tr>'
            f'<td style="padding:5px 8px;font-size:11.5px;color:#e6edf3;font-weight:600">'
            f'{x["name"]}</td>'
            f'<td style="padding:5px 8px;font-size:12px;font-variant-numeric:tabular-nums">'
            f'{x["cur"]:.2f}{x["unit"]}</td>'
            f'<td style="padding:5px 8px;font-size:10.5px;color:#8b949e;'
            f'font-variant-numeric:tabular-nums">'
            f'{x["lo"]:.2f}({x["lo_d"]}) ~ {x["hi"]:.2f}({x["hi_d"]})</td>'
            f'<td style="padding:5px 8px;font-size:11px;font-variant-numeric:tabular-nums">'
            f'{x["pct"]*100:.0f}%</td>'
            f'<td style="padding:5px 8px;font-size:11px;font-variant-numeric:tabular-nums;'
            f'color:{"#f85149" if x["d"] > 0 else "#3fb950"}">{x["d"]:+.2f}pp</td>'
            f'<td style="padding:5px 8px;font-size:10.5px;color:#adbac7;line-height:1.5">'
            f'{x["stage"]}<br><span style="color:#6e7681">{x["why"]}</span></td>'
            f'</tr>')

    svgs = []
    for x in kw:
        svgs.append(
            f'<div style="min-width:0">'
            f'<div style="font-size:11.5px;color:#adbac7;margin-bottom:4px">'
            f'{x["name"]} · {x["span"][0]} 起 {x["n"]} 个月</div>'
            f'{svg_chart(x["series"], x["info"], x["unit"], 12, None, w=560, h=150)}'
            f'</div>')

    return (
        '<div class="cyc-card" style="margin-top:22px">'
        '<div class="t" style="font-size:15px;font-weight:600;color:#e6edf3;margin-bottom:6px">'
        '康波周期 · 长波(50-60 年) · 中美对照</div>'
        '<div class="s" style="font-size:12px;color:#8b949e;line-height:1.75">'
        '康波是 50-60 年的技术-经济长波, 一轮里套着 2-3 个中周期(朱格拉/基钦)。'
        '<b>先把话说死: 免费接口里最长的一条连续序列是美债 10Y(1990 年起, 36 年), '
        '连半轮康波都不到, 所以这里不宣称「精确判定当前是康波第几阶段」</b> —— 那是拿 36 年'
        '外推 60 年, 是编故事。下面做的是两件诚实的事: ①把四阶段框架与主流历史区间摆出来, '
        '并标明争议在哪; ②用可得的最长序列给一个<b>长波定位</b>。'
        '选利率长波做代理的理由: 它是康波最可靠的同步/滞后指标之一 —— '
        '长波上行(资金成本系统性抬升)对应衰退到萧条, 长波下行对应复苏到繁荣。'
        '<b>中美分开列, 因为这两个经济体现在正处在长波的两个相反方向上</b> —— '
        '这件事比「当前是第几阶段」重要得多。</div></div>'
        '<div class="ml-card" style="margin-bottom:16px">'
        '<table class="ml-tb">'
        '<colgroup><col style="width:64px"><col><col style="width:150px">'
        '<col style="width:86px"><col style="width:86px"></colgroup>'
        '<thead><tr><th>阶段</th><th>典型特征</th><th>参考历史区间 / 利率形态</th>'
        '<th style="text-align:center">美国</th><th style="text-align:center">中国</th>'
        '</tr></thead><tbody>' + "".join(qrows) + '</tbody></table>'
        '<div class="ml-lv" style="margin-top:8px"><b>判定依据</b>(长波方向 = 近 3 年均值 − 前 10 年均值):</div>'
        '<table class="ml-tb" style="margin-top:4px">'
        '<colgroup><col style="width:150px"><col style="width:70px"><col style="width:150px">'
        '<col style="width:56px"><col style="width:74px"><col></colgroup>'
        '<thead><tr><th>指标</th><th>当前</th><th>历史区间</th>'
        '<th>分位</th><th>长波方向</th><th>指向的阶段</th></tr></thead>'
        '<tbody>' + "".join(erows) + '</tbody></table>'
        f'<div class="cyc-grid2">{"".join(svgs)}</div>'
        '<div class="cyc-logic" style="margin-top:10px">'
        '<b>这个版块的局限, 请务必知道:</b> '
        '①样本只有 '
        + " / ".join(f'{x["n"]} 个月({x["span"][0]} 起)' for x in kw)
        + ', 而康波一轮是 50-60 年 —— 这里的分位只是「这段样本内的相对位置」, '
          '换一段样本结论就会变, 不能当成康波坐标; '
          '②康波的阶段划分本身就<b>没有共识</b>: 主流熊彼特-曼德尔框架把第五波(信息技术)划为 '
          '1982-2008 前后, 而周金涛一派认为第五波要到 2019 前后才结束、其后是萧条期; '
          '两者相差一轮中周期, 谁也说不服谁, 所以上表的历史区间只能当参考; '
          '③利率长波只是康波的一个代理维度, 完整的康波判定还要看商品实际价格长波、'
          '主导国技术创新扩散率与资本开支周期 —— 这几项没有可用的免费长序列; '
          '④<b>最值得记的一句话</b>: 中美利率长波当前是<b>反向</b>的 '
          '(美国在加息后的高水位、中国在降息通道的历史低位), '
          '这种分化在 2008 年之后的历史上很少见, 它比「康波第几阶段」更有决策含义。'
          '</div></div>')


def clock_svg(c, w=560, h=430):
    """四象限散点 + 最近 5 年轨迹。SVG 里 y 轴向上为正(值越大越靠上)。"""
    L, R, T, B = 78, 22, 30, 52
    pw, ph = w - L - R, h - T - B
    xs = np.asarray([p[1] for p in c["path"]], dtype=float)
    ys = np.asarray([p[2] for p in c["path"]], dtype=float)

    # ── 稳健坐标范围(8%/92% 分位), 不用 min/max ──
    # 中国版有个真实但致命的极值: 2008Q4 信贷同比的 4 季度变化是 -287pp、
    # 2009Q1 是 +250pp(四万亿那波)。它比正常波动大一个数量级, 用 min/max 定轴的话
    # 2010 年之后 17 年会被压成中间一条竖线, 图就废了。所以按分位定轴,
    # 越界的点压到边缘、画成空心菱形并注明, 数据一个都没丢。
    def _bounds(v):
        # 默认用 min/max(一个点都不裁)。只有当全距离谱到「> 6 倍 10-90 分位距」时才切稳健分位
        # —— 也就是只有真的存在数量级级别的离群点才裁。
        # 这个自适应很关键: 两个钟的极值形态完全不同。中国版 X 轴是 537pp 全距 vs 57pp 分位距
        # (比值 9.5) → 裁; 美国版两轴比值只有 1.9 / 2.1, 全距本来就正常, 硬裁反而会把
        # 2020 疫情、2021-22 高通胀这些最有价值的段压到边缘上。
        lo, hi = float(v.min()), float(v.max())
        q10, q90 = np.percentile(v, [10, 90])
        if (hi - lo) > 6.0 * max(q90 - q10, 1e-9):
            lo, hi = np.percentile(v, [10, 90])
        pad = max((hi - lo) * 0.16, 0.6)
        lo, hi = lo - pad, hi + pad
        return min(lo, -pad), max(hi, pad)                    # 中线 0 必须画得进去

    x0, x1 = _bounds(xs)
    y0, y1 = _bounds(ys)
    # 当前点必须在画面内
    x0, x1 = min(x0, c["cur"]["x"]), max(x1, c["cur"]["x"])
    y0, y1 = min(y0, c["cur"]["y"]), max(y1, c["cur"]["y"])
    clip = [(lb, a, b) for lb, a, b in c["path"]
            if a < x0 or a > x1 or b < y0 or b > y1]

    def X(v):
        v = min(max(v, x0), x1)
        return L + (v - x0) / (x1 - x0) * pw

    def Y(v):
        v = min(max(v, y0), y1)
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
    n = len(c["path"])
    pts = " ".join(f"{X(a):.1f},{Y(b):.1f}" for _, a, b in c["path"])
    p.append(f'<polyline points="{pts}" fill="none" stroke="#58a6ff" stroke-width="1.1" '
             f'stroke-opacity="0.26" stroke-linejoin="round"/>')
    # 最近 2 年这段单独加粗高亮 —— 全历史 70+ 个点如果都画粗, 就是一团毛线
    if n > CLOCK_RECENT_Q:
        rpts = " ".join(f"{X(a):.1f},{Y(b):.1f}"
                        for _, a, b in c["path"][-(CLOCK_RECENT_Q + 1):])
        p.append(f'<polyline points="{rpts}" fill="none" stroke="#58a6ff" stroke-width="2.1" '
                 f'stroke-opacity="0.88" stroke-linejoin="round"/>')
    # 全历史小点: 越新越亮, 能看出年代顺序
    for i, (_, a, b) in enumerate(c["path"]):
        op = 0.18 + 0.34 * (i / max(1, n - 1))
        p.append(f'<circle cx="{X(a):.1f}" cy="{Y(b):.1f}" r="1.7" fill="#58a6ff" '
                 f'fill-opacity="{op:.2f}"/>')
    # 最近 8 个季度放大加亮, 一眼看出最近的移动方向
    rec = c["path"][-CLOCK_RECENT_Q:]
    for i, (_, a, b) in enumerate(rec):
        t = i / max(1, len(rec) - 1)
        p.append(f'<circle cx="{X(a):.1f}" cy="{Y(b):.1f}" r="{2.4 + 1.6 * t:.1f}" '
                 f'fill="#58a6ff" fill-opacity="{0.45 + 0.55 * t:.2f}"/>')
    # 年份标注: 从起点起每 3 年的 Q1 标一次 —— 否则只看得到一团轨迹, 读不出「哪年在哪个格」
    y0 = int(c["path"][0][0][:4])
    for i, (lb, a, b) in enumerate(c["path"]):
        if not lb.endswith("Q1") or len(lb) != 6:
            continue
        yr = int(lb[:4])
        if i != 0 and (yr - y0) % 3 != 0:
            continue
        p.append(f'<text x="{X(a):.1f}" y="{Y(b)-7:.1f}" font-size="9" '
                 f'fill="#8b949e" text-anchor="middle" paint-order="stroke" '
                 f'stroke="#0d1117" stroke-width="2.4" stroke-linejoin="round">{yr}</text>')
    # 越界点: 压在边缘画空心菱形, 数据不丢但要让人知道它是被压过来的
    for lb, a, b in clip:
        dx, dy = X(a), Y(b)
        p.append(f'<path d="M{dx:.1f},{dy-3.8:.1f} L{dx+3.8:.1f},{dy:.1f} '
                 f'L{dx:.1f},{dy+3.8:.1f} L{dx-3.8:.1f},{dy:.1f} Z" fill="#0d1117" '
                 f'stroke="#58a6ff" stroke-width="1.2" stroke-opacity="0.9"/>')
    if clip:
        _mx = max(abs(float(xs.min())), abs(float(xs.max())),
                  abs(float(ys.min())), abs(float(ys.max())))
        p.append(f'<text x="{L+6}" y="{T+ph-6:.1f}" font-size="9" fill="#6e7681" '
                 f'paint-order="stroke" stroke="#0d1117" stroke-width="2.4" '
                 f'stroke-linejoin="round">◇ {len(clip)} 期超出坐标范围已压到边缘'
                 f'(历史极值 {_mx:.0f}pp, 按 8%/92% 分位定轴)</text>')
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


def _rot_line(c):
    """按顺时针轮动顺序(衰退→复苏→过热→滞胀)把四格历史占比串成一句话"""
    pos_of = {q["name"]: q["pos"] for q in c["quads"]}
    out = []
    for nm in ("衰退期", "复苏期", "过热期", "滞胀期"):
        p = pos_of.get(nm)
        out.append(f'{nm} {c["share"].get(p, 0) * 100:.0f}%' if p else f'{nm} —')
    return " → ".join(out)


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
            f'<div class="ml-card">'
            f'<div class="cyc-hd">'
            f'<div class="lft"><div class="nm">{c["name"]}'
            f'<span class="tag" style="color:#f85149;border-color:#f8514988;background:#f8514918">'
            f'{c["cur"]["name"]} · {c["cur"]["asset"]}</span></div>'
            f'<div class="src">{c["tag"]} · 截至 {c["asof"]} · '
            f'轨迹 {c["span"][0]}→{c["span"][1]} 共 {c["n"]} 个季度</div></div>'
            f'<div class="rgt">'
            f'<div class="metric"><div class="k">{c["xname"]}轴</div>'
            f'<div class="v" style="color:{"#f85149" if c["cur"]["x"]>=0 else "#3fb950"}">'
            f'{c["cur"]["x"]:+.2f}<span style="font-size:10px;color:#8b949e">pp</span></div></div>'
            f'<div class="metric"><div class="k">{c["yname"]}轴</div>'
            f'<div class="v" style="color:{"#f85149" if c["cur"]["y"]>=0 else "#3fb950"}">'
            f'{c["cur"]["y"]:+.2f}<span style="font-size:10px;color:#8b949e">pp</span></div></div>'
            f'</div></div>'
            # ── 图在上 ──
            f'<div class="ml-svg">{clock_svg(c)}</div>'
            # ── 当前象限说明 ──
            f'<div class="ml-now"><b>{c["cur"]["pos"]} · {c["cur"]["name"]}</b> · {c["cur"]["asset"]}'
            f'<div style="margin-top:4px">{c["cur"]["why"]}</div></div>'
            # ── 表在下 ──
            f'<table class="ml-tb">'
            f'<colgroup><col style="width:56px"><col style="width:66px">'
            f'<col style="width:74px"><col><col style="width:78px"></colgroup>'
            f'<thead><tr>'
            f'<th style="text-align:left">象限</th><th style="text-align:left">阶段</th>'
            f'<th style="text-align:left">资产</th><th style="text-align:left">含义</th>'
            f'<th style="text-align:right">历史占比</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
            # 按顺时针轮动顺序把四格占比念一遍 —— 直接回答「这个钟到底走没走过衰退/复苏」
            f'<div class="ml-lv"><b>轮动顺序的历史分布</b>'
            f'({c["span"][0]}→{c["span"][1]}, 共 {c["n"]} 个季度): {_rot_line(c)}</div>'
            f'<div class="ml-lv">水平值(非轴向, 仅供参照): '
            f'{c["xname"]} {c["xlev"]:.2f}{c.get("xlev_unit","%")} ({c["xlev_date"]}) · '
            f'{c["yname"]} {c["ylev"]:.2f}{c.get("ylev_unit","%")} ({c["ylev_date"]})</div>'
            f'<div class="ml-src">数据源: {c["xsrc"]} · {c["ysrc"]}</div>'
            f'<div class="cyc-logic">{c["logic"]}</div>'
            f'</div>')
    return ('<div class="cyc-card" style="margin-top:22px">'
            '<div class="t" style="font-size:15px;font-weight:600;color:#e6edf3;margin-bottom:6px">'
            '美林时钟 · 两个版本(左右并列, 同一时间轴口径)</div>'
            '<div class="s" style="font-size:12px;color:#8b949e;line-height:1.75">'
            '美林时钟把宏观环境切成四格, 每格对应一类占优资产。'
            '<b>中国改良版</b>把美林原来的「产出缺口 × 通胀」换成「<b>货币 × 信用</b>」'
            '(国内没有高频产出缺口数据, 而央行管货币、实体管信用, 两轴松紧决定了大类资产轮动); '
            '<b>美国原版</b>仍用「<b>增长 × 通胀</b>」。'
            '两版的坐标轴都取<b>相对基准的偏离</b>(pp), 中线固定为 0 —— '
            '不用历史分位, 因为 M2 同比和信贷增速是长期下台阶的, 用分位会把钟钉死在同一格十年不动。'
            '<b>轨迹已改成季度口径并画全历史</b>: 月度数据噪声太大, 画线是一团毛线; '
            '而且原来只画最近 60 个月, 美国那段正好被 2021 之后的高通胀钉在钟的上半部, '
            '看上去像「从没进过衰退期和复苏期」—— 把 2008 年以来的全部季度画出来, 四格都走过。'
            '两版都是<b>顺时针</b>旋转(衰退→复苏→过热→滞胀), 但因为两轴语义不同, '
            '「衰退期」落在的角不一样: 中国版在左上, 美国版在左下。'
            '<b>框架不等于定律</b>, 只用来描述当下处在什么货币信用/增长通胀环境。'
            '</div></div>'
            f'<div class="ml-duo">{"".join(cards)}</div>')
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
.cyc-chiplf, .cyc-chipinv { font-size:9.5px; line-height:1.5; padding:0 4px; border-radius:3px;
  flex:none; white-space:nowrap; font-weight:600; }
.cyc-chiplf { background:rgba(88,166,255,.14); color:#58a6ff; border:1px solid rgba(88,166,255,.3); }
.cyc-chipinv { background:rgba(210,153,34,.14); color:#d2991d; border:1px solid rgba(210,153,34,.32);
  margin-left:3px; }
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
.cyc-read { margin-top:12px; border:1px solid #21262d; border-radius:9px;
  background:#0d1117; padding:10px 12px 8px; }
.cyc-read .rh { font-size:11.5px; font-weight:600; color:#adbac7; margin-bottom:7px;
  padding-bottom:6px; border-bottom:1px solid #21262d; }
.cyc-read .rt { width:100%; border-collapse:collapse; table-layout:fixed; }
.cyc-read th { font-size:10px; color:#6e7681; font-weight:500; text-align:left;
  padding:3px 7px; border-bottom:1px solid #161b22; }
.cyc-read td { padding:4px 7px; border-bottom:1px solid #161b22; vertical-align:top;
  font-size:11px; line-height:1.55; word-break:break-word; }
.cyc-read .rn { width:19%; color:#e6edf3; font-weight:600; }
.cyc-read .rv { width:17%; font-weight:600; font-variant-numeric:tabular-nums;
  white-space:nowrap; }
.cyc-read .ru { font-size:9.5px; color:#8b949e; font-weight:400; margin-left:3px; }
.cyc-read .rd { width:11%; color:#8b949e; white-space:nowrap; }
.cyc-read .rs { width:20%; color:#6e7681; }
.cyc-read .rn2 { color:#6e7681; }
/* 一行最多 3 张卡, 超出自动换行; 窄屏自动降级为 2 列 / 1 列 */
.cyc-ind-grid { grid-template-columns:repeat(3,minmax(0,1fr)); }
@media (max-width:1180px) { .cyc-ind-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } }
@media (max-width:760px)  { .cyc-ind-grid { grid-template-columns:minmax(0,1fr); } }
/* 康波版块里的两条长波曲线: 左右并列等宽 */
.cyc-grid2 { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px;
  margin-top:10px; }
@media (max-width:900px){ .cyc-grid2 { grid-template-columns:minmax(0,1fr); } }
/* 中美两个钟左右并列、等宽、等高(stretch), 各自内部: 图 → 当前象限 → 表 → 数据源 → 说明 */
.ml-duo { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px;
  align-items:stretch; }
.ml-card { background:#161b22; border:1px solid #30363d; border-radius:12px;
  padding:14px 16px 12px; display:flex; flex-direction:column; min-width:0; }
.ml-card .cyc-hd { margin-bottom:10px; }
.ml-svg { min-width:0; margin-bottom:10px; }
.ml-now { background:#0d1117; border:1px solid #21262d; border-left:3px solid #f85149;
  border-radius:8px; padding:9px 12px; font-size:12px; color:#adbac7; line-height:1.7;
  margin-bottom:10px; }
.ml-tb { width:100%; border-collapse:collapse; table-layout:fixed; margin-bottom:2px; }
.ml-tb th { font-size:10px; color:#6e7681; font-weight:500; text-align:left;
  padding:3px 8px; border-bottom:1px solid #21262d; }
.ml-tb td { border-bottom:1px solid #161b22; vertical-align:top; word-break:break-word; }
.ml-lv { font-size:10.5px; color:#6e7681; margin-top:8px; line-height:1.6; }
.ml-src { font-size:10px; color:#6e7681; margin-top:4px; line-height:1.5; }
.ml-card .cyc-logic { margin-top:10px; }
@media (max-width:1100px){ .ml-duo { grid-template-columns:minmax(0,1fr); } }
@media (max-width:900px){ .cyc-grid { grid-template-columns:repeat(2,1fr); } }
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


def render_fragment(rows, updated, clocks=None, readings=None, kwave=None):
    minis, cards = [], []
    readings = readings or {}
    kwave = kwave or []
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
                if st.get("forced"):
                    basis_html = (f'<span class="tr">按你的要求<b>强制去趋势</b> · '
                                  f'自动判定 R²={r2:.2f} 未达 {TREND_R2} 阈值</span>'
                                  f' · 同口径下绝对分位为 {pabs:.0f}%（长期上涨会把它顶到很高，别用）')
                else:
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
            # 低频"图读值"必须标出来: 一年才一个点的样本, 分位只是粗糙排序
            badge = ""
            if ind.get("lowfreq"):
                badge = ('<span class="cyc-chiplf" title="从公开研报/新闻里人工提取的静态序列, '
                         '不会自动更新">研报提取·低频</span>')
            if ind.get("inv"):
                badge += ('<span class="cyc-chipinv" title="反指标: 数值越低代表这个周期越热, '
                          '已把分位与变化率翻转过来">反指标</span>')
            sunit = {"W": "周", "M": "月"}.get(ind["freq"], "点")
            svg = svg_chart_series(ind)
            ind_html.append(
                f'<div class="cyc-ind">'
                f'<div class="ihd"><span class="inm" title="{ind["name"]}">{ind["name"]}</span>'
                f'{badge}'
                f'<span class="ipct" style="color:{ic}">位置 {st["pct"]*100:.0f}%</span></div>'
                f'<div class="ival">{_fmt(st["cur"], ind["unit"])}'
                f'<span class="iu">{ind["unit"] or "指数"}</span></div>'
                f'<div class="iph" style="color:{ic}">{st["phase"]}'
                f'<span class="dv"> · {dev_txt}</span></div>'
                f'<div class="ibas">{basis_html}</div>'
                f'<div class="ibox">{svg}</div>'
                f'<div class="isrc">{ind["source"]}'
                f'<br>{"<span class=\'warn\'>⚠ 数据滞后</span> " if warn else ""}'
                f'截至 {ind["last_date"]} · {ind["n_raw"]}个{sunit}样本{note}</div>'
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
            f'{render_readings(readings.get(r["key"]))}'
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
            # 顺序: 康波(长波) → 美林时钟 → 各个产业周期(券商起)
            f'{render_kwave(kwave)}'
            f'{render_clocks(clocks or [])}'
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
            f'<br><b>⑪你点名的每个指标都在页面上, 只是分了三种呈现方式 —— 逐条交代清楚:</b>'
            f'<b>(a) 能拿到长序列的 → 正常画曲线算分位。</b>已补上的有: '
            f'<b>DRAM / NAND 现货价</b>(中国闪存市场周度指数, 已按 CPI 折不变价)、'
            f'<b>生猪出栏体重</b>(搜猪网 548 周成交均重)、<b>猪粮比</b>、<b>原油/碳酸锂实际价</b>、'
            f'<b>PTA÷原油价差</b>、<b>全社会用电量同比</b>。'
            f'<b>(b) 免费源只给滚动窗口的 → 爬取 + 本地累积。</b>'
            f'<b>挖机月度销量</b>来自中国工程机械工业协会(cncma.org)每月销售快报, '
            f'<b>造船三大指标</b>来自中国船舶工业行业协会(cansi.org.cn), '
            f'闪存市场那两条指数也是。这类免费源只回溯 7~16 期, 页面会随每次采集自动累积样本, '
            f'现阶段<b>别拿它的分位当周期位置</b>, 看方向和拐点即可。'
            f'<b>(c) 确实只有新闻/研报口径的 → 放在每个周期的「行业读数」表里</b>, '
            f'只给最新值 + 来源 + 截止期, <b>不画曲线不算分位</b>(拿不到序列就不该假装能算): '
            f'能繁母猪存栏 3780 万头(2026Q2末, 统计局)、30 城成交面积 652.33 万㎡(2026-09)、'
            f'十城存销比 22.93 月、新建商品房销售面积同比 -12.1%(1-8月)、土地溢价率 13.1%、'
            f'铜精矿现货 TC -226.2 美元/干吨(2026-09, SMM)、纯碱开工负荷 81.9% 与库存 179 万吨、'
            f'甲醇开工负荷 72.5%、32 吋 35.0 美元 / 55 吋 123 美元(2026-09, TrendForce)。'
            f'<b>其中 (c) 里除标注「实时爬取」的以外, 都是我核对于 {VERIFIED_DATE} 的研报/新闻, '
            f'不会自动更新, 需要定期人工复核。</b>'
            f'<br>仍然取不到、只能用代理或只能空着的: '
            f'造船的「订单簿/船队运力」「新船价格指数」「二手船价」「拆船量」(Clarksons/VesselsValue 付费); '
            f'存储的「三大原厂资本开支同比」「渠道库存周转天数」「合约价-现货价价差」(集邦付费); '
            f'铜的「电网投资增速」、上期所/COMEX 铜库存历史(免费接口只有 3 个月); '
            f'生猪的「仔猪价格」「二次育肥存量」(涌益付费); '
            f'煤炭的「秦皇岛动力煤价/港口库存/电厂日耗」(CCTD 付费墙, 动力煤期货 ZC 已退市)。'
            f'<b>代理指标(中国船舶/京东方/三一等股价)不等同于原数据</b>, 请当作方向参考。'
            f'<br><b>⑫美林时钟两版都只用两个宏观轴切四格, 维度远少于现实。</b>'
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
                # 研报提取的年度/季度序列天然只有十几个点, 不能套用常规 30 点门槛
                if len(s) < ic.get("min_n", 30):
                    raise ValueError(f"样本过短({len(s)})，要求{ic.get('min_n', 30)}")
                info = analyze(s, ic["freq"], ic["ma"], unit, inv=ic.get("inv", False),
                               force_dev=ic.get("force_dev", False))
                inds.append(dict(
                    k=ic["k"], name=name, unit=unit, source=src, note=note,
                    freq=ic["freq"], ma=ic["ma"], ref=ref, main=ic.get("main", False),
                    inv=ic.get("inv", False), lowfreq=bool(over.get("lowfreq")),
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
                                            _meta.get("clocks") or [],
                                            _meta.get("readings") or {},
                                            _meta.get("kwave") or []),
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
    print("开始采集康波周期 (中美长波定位)…")
    kwave = build_kwave()

    clocks = build_clocks()

    print("开始爬取行业读数 (闪存市场/工程机械协会/船舶工业协会)…")
    readings = build_readings(rows)
    n_read = sum(len(v) for v in readings.values())

    updated = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_ind = sum(len(r["indicators"]) for r in rows)
    payload = dict(
        ver=SCHEMA_VER, updated=updated, failed=failed, clocks=clocks, readings=readings,
        kwave=[{k: v for k, v in x.items() if k not in ("info", "series")} for x in kwave],
        cycles=[{**{k: v for k, v in r.items() if k != "indicators"},
                 "indicators": [{k: v for k, v in i.items() if k != "svg"}
                                for i in r["indicators"]]} for r in rows])
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    OUT_HTML.write_text(render_fragment(rows, updated, clocks, readings, kwave), encoding="utf-8")
    print(f"\n已写出: {OUT_JSON.name} ({OUT_JSON.stat().st_size/1024:.0f}KB)  "
          f"{OUT_HTML.name} ({OUT_HTML.stat().st_size/1024:.0f}KB)  "
          f"{len(rows)}个周期 / {n_ind}个指标 / {len(clocks)}个时钟 / "
          f"{len(kwave)}条长波 / {n_read}条行业读数")
    if failed:
        print(f"失败周期: {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
