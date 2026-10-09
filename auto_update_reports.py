#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI财报分析自动增量更新 (数据与写作分离, 防幻觉)

流程 (每只有新财报的股票):
  1. 代码抓真实数据包 (yfinance): 头部行情 + 最近一季财报 + 新财年历史数据   ← 零幻觉
  2. 代码直接重写 md 头部元数据行 / 趋势表追加新财年 / 插入最新季报速览块     ← 零幻觉
  3. DeepSeek 只做解读与评分 (输入=原文件+新数据包, 输出=严格JSON, 禁止数据包外数字)
  4. 代码合并 JSON 进原文件, 用渲染器校验可解析性, 失败重试/跳过
  5. 写新日期文件, 可选部署

用法:
  python auto_update_reports.py               # 检测新财报股票并增量更新
  python auto_update_reports.py --dry-run     # 试运行, 输出到 %TEMP%, 不写回不部署
  python auto_update_reports.py --force AAPL  # 强制更新指定股票(调试)
  python auto_update_reports.py --deploy      # 更新后调用 update_all.bat 部署(仅本地)
"""
import os, sys, re, json, glob, argparse, traceback
from datetime import date, datetime

BASE = os.environ.get("WORKSPACE_ROOT", r"C:\Users\15949\WorkBuddy\xiaocai")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_analysis_pages as B          # 复用提取器做输出校验 + 匹配逻辑
from check_earnings_updates import scan_analysis, match_analysis, md_date_to_cmp
CFG = os.path.join(BASE, "watchlist_us", "config.json")
ANALYSIS_ROOT = os.path.join(BASE, "analysis", "港美股")

CUR = {'.SS': '¥', '.SZ': '¥', '.HK': 'HK$', '.T': '¥'}
NORM = {'¥': '¥', 'HK$': 'HK$', '$': '$'}


def currency_of(sym):
    for suf, c in CUR.items():
        if sym.upper().endswith(suf):
            return c
    return '$'


def _gtimg_code(sym):
    """symbol → 腾讯行情代码。A股 sh/sz+6位; 美股 us+TICKER; 港股 hk+4位。日股等返回 None。"""
    s = sym.upper()
    m = re.match(r'^(\d{6})\.(SS|SZ)$', s)
    if m:
        return ('sh' if m.group(2) == 'SS' else 'sz') + m.group(1)
    m = re.match(r'^(\d{4})\.HK$', s)
    if m:
        return 'hk' + m.group(1)
    if re.match(r'^[A-Z.\-]+$', s) and '.' not in s:
        return 'us' + s
    return None


def fetch_gtimg(sym):
    """腾讯行情接口 (国内直连稳定): 价格/PE/市值/52周区间。返回 dict, 市值单位=亿(本地货币)。"""
    code = _gtimg_code(sym)
    if not code:
        return {}
    try:
        import requests, urllib3
        urllib3.disable_warnings()
        s = requests.Session(); s.trust_env = False; s.verify = False
        r = s.get(f'https://qt.gtimg.cn/q={code}', timeout=10)
        f = r.text.split('="', 1)[-1].strip('";\n ').split('~')

        def num(i):
            try:
                return float(f[i])
            except (IndexError, ValueError):
                return None
        out = {'px': num(3), 'pe': num(41) or num(39), 'mcap_yi': num(45) or num(44)}
        w52h, w52l = (num(47), num(48)) if sym.upper().endswith(('.SS', '.SZ')) else (num(48), num(49))
        if w52h and w52l:
            out['w52h'], out['w52l'] = w52h, w52l
        if sym.upper().endswith(('.SS', '.SZ')):
            out['pb'] = num(46)
        elif code.startswith('us'):
            out['div'] = num(52) if num(52) else None
        return out
    except Exception:
        return {}


def _fmt_amount(v, cur):
    """数值 → 亿/万亿 展示字符串 (v 为原生货币单位)"""
    if v is None:
        return None
    yi = v / 1e8
    if abs(yi) >= 10000:
        return f'{cur}{yi/10000:,.2f}万亿'
    if abs(yi) >= 1000:
        return f'{cur}{yi:,.0f}亿'
    return f'{cur}{yi:,.1f}亿'


def fetch_all(sym):
    """双源融合: 腾讯行情(稳, 价格/PE/市值/52周) 优先, yfinance 补充(PB/股息率/季度/年报财报, 可能限流)。"""
    data = fetch_gtimg(sym)
    data['cur'] = currency_of(sym)
    yf_data = {}
    try:
        yf_data = fetch_yf(sym) or {}
    except Exception:
        pass
    for k in ('pb', 'div', 'last_quarter', 'annual'):
        if yf_data.get(k) and not data.get(k):
            data[k] = yf_data[k]
    if yf_data.get('mcap') and not data.get('mcap_yi'):
        data['mcap_yi'] = yf_data['mcap'] / 1e8
    return data


def fetch_yf(sym):
    """yfinance 抓取: 头部行情 + 最近一季 + 年度历史。返回 dict (失败字段=None)。"""
    import yfinance as yf
    out = {}
    try:
        tk = yf.Ticker(sym)
        info = {}
        try:
            info = tk.info or {}
        except Exception:
            pass
        def g(k):
            v = info.get(k)
            return v if isinstance(v, (int, float)) else None
        out['px'] = g('currentPrice') or g('regularMarketPrice')
        out['mcap'] = g('marketCap')
        out['pe'] = g('trailingPE')
        out['pb'] = g('priceToBook')
        div = g('dividendYield')
        out['div'] = (div * 100 if div is not None and div < 1 else div)  # 0.0042→0.42
        out['w52l'] = g('fiftyTwoWeekLow')
        out['w52h'] = g('fiftyTwoWeekHigh')
        cur = currency_of(sym)
        rows_q = []
        try:
            q = tk.quarterly_income_stmt
            if q is not None and not q.empty:
                col = q.columns[0]  # 最近一季
                d = col.to_pydatetime().date() if hasattr(col, 'to_pydatetime') else col
                rev = q.loc['Total Revenue', col] if 'Total Revenue' in q.index else None
                ni = q.loc['Net Income', col] if 'Net Income' in q.index else None
                rows_q.append((str(d), rev, ni))
        except Exception:
            pass
        out['last_quarter'] = rows_q
        rows_a = []
        try:
            a = tk.income_stmt
            if a is not None and not a.empty:
                for col in list(a.columns)[:6]:
                    d = col.to_pydatetime().date() if hasattr(col, 'to_pydatetime') else col
                    rev = a.loc['Total Revenue', col] if 'Total Revenue' in a.index else None
                    ni = a.loc['Net Income', col] if 'Net Income' in a.index else None
                    if rev is not None and ni is not None:
                        rows_a.append((str(d), float(rev), float(ni)))
        except Exception:
            pass
        out['annual'] = rows_a
        out['cur'] = cur
    except Exception as e:
        out['err'] = str(e)[:120]
    return out


def header_line(sym, d):
    """由抓取数据直接生成头部元数据行 (代码生成, 零幻觉)。"""
    cur = d.get('cur') or currency_of(sym)
    segs = [f'**分析日期：** {date.today().isoformat()}']
    if d.get('px'):
        segs.append(f'**收盘价：** {cur}{d["px"]:,.2f}')
    bits = []
    if d.get('mcap_yi'):
        bits.append(f'**市值：** ≈{_fmt_amount(d["mcap_yi"] * 1e8, cur)}')
    if d.get('w52l') and d.get('w52h'):
        bits.append(f'**52周区间：** {cur}{d["w52l"]:,.2f}–{d["w52h"]:,.2f}')
    if bits:
        segs.append(' | '.join(bits))
    m = []
    if d.get('pe'):
        m.append(f'**PE(TTM)：** ≈{d["pe"]:.1f}')
    if d.get('pb'):
        m.append(f'**PB：** ≈{d["pb"]:.1f}')
    if d.get('div') is not None:
        m.append(f'**股息率(TTM)：** ≈{d["div"]:.2f}%' if d['div'] > 0 else '**股息率(TTM)：** 0（不分红）')
    if m:
        segs.append(' | '.join(m))
    return segs[0] + ' ｜ ' + ' ｜ '.join(segs[1:])


def new_quarter_block(d, cur):
    """最新季报速览块 (代码生成)。无数据返回 ''。"""
    qs = d.get('last_quarter') or []
    if not qs:
        return ''
    dt, rev, ni = qs[0]
    if not rev:
        return ''
    lines = [f'### 最新季报速览（代码抓取 {dt}，yfinance口径）', '',
             '| 指标 | 数值 |', '|------|------|',
             f'| 营业收入 | {_fmt_amount(float(rev), cur)} |']
    if ni:
        lines.append(f'| 净利润 | {_fmt_amount(float(ni), cur)} |')
    lines.append('')
    return '\n'.join(lines)


SYSTEM_PROMPT = """你是专业股票分析师。任务：一份已有的六步分析报告因公司发布新财报需要增量更新。
铁律:
1. 只允许使用【新数据包】和你眼前的【原报告】中出现的数字, 严禁编造任何数据包之外的数值; 需要外部数据处写"不确定"。
2. 你的工作是"解读与判断"(评级/逻辑/评分/结论), 不是收集数据——硬数据已由程序写入, 不要复述改写头部与表格。
3. 输出严格 JSON (UTF-8), 字段:
   rating: 主评级(买入/增持/持有/减持/回避, 2-4字)
   rating_detail: 评级补充说明一句(30-80字)
   conclusion_head: 更新后的"结论先行"整段(250-450字, 须引用新数据包中的最新财报数字与现价, 判断是否维持/调整原逻辑)
   latest_quarter_comment: 新财报解读段落(250-450字, 基于数据包的营收/净利与原报告的历史语境做归因和评价)
   radar: 数组6项, 按顺序[商业模式,护城河,盈利能力,现金流与资产安全,估值安全边际,成长性], 每项{score:0-5半刻度, fact:支持事实(引用具体数字), counter:反证/不确定性, conf:高/中/低}
   radar_avg: 六项均分(1位小数)
   tldr: 一句话结论(2-4句, 含评级与关键价位)
4. 原报告的分析逻辑仍是基底: 新财报若不改变原结论就微调表述, 若明显改变(超预期/暴雷)才调整评级。"""


def build_user_prompt(sym, name, orig_text, data_json, cur):
    return (f'股票: {name}（{sym}）\n【新数据包(程序抓取, 唯一可信新数据)】:\n{data_json}\n\n'
            f'【原报告全文】:\n{orig_text[:24000]}\n\n请输出更新JSON。')


def merge_json(text, j, hl, data):
    """把 LLM JSON 合并进原文件。返回 (新文本, 变更列表)。"""
    chg = []
    new = text
    # 1) 头部元数据行: 定位旧头部行区间 → 整块替换为程序生成行 (零幻觉)
    lines = new.splitlines()
    h1 = next((i for i, ln in enumerate(lines) if ln.startswith('# ')), 0)
    start = None
    for i in range(h1 + 1, min(h1 + 12, len(lines))):
        if re.match(r'^\*\*(分析日期|当前股价|收盘价|现价)', lines[i]):
            start = i
            break
    if start is None:
        lines.insert(h1 + 1, hl)
        chg.append('头部块:新增')
    else:
        end = start
        while end < len(lines) and lines[end].startswith('**'):
            end += 1
        keep = [ln for ln in lines[start:end] if re.match(r'^\*\*股息率', ln) and '股息率' not in hl]
        lines[start:end] = [hl] + keep
        chg.append('头部块:刷新')
    new = '\n'.join(lines)
    # 2) 结论先行段落替换 (两种格式: ## 结论先行 节 / > **先给结论** blockquote)
    if j.get('conclusion_head'):
        m = re.search(r'(##[^\n]*(?:结论先行|核心结论)[^\n]*\n)(.*?)(?=\n## |\n---)', new, re.S)
        if m:
            body = re.sub(r'\*\*综合评级[：:].*?(?=\n\n|\Z)', '', m.group(2), flags=re.S)
            body = re.sub(r'\*\*核心逻辑[：:].*?(?=\n\n|\Z)', '', body, flags=re.S)
            newseg = (f'**综合评级：{j["rating"]}（{j.get("rating_detail","")}）**\n\n'
                      f'{j["conclusion_head"]}')
            new = new[:m.start()] + m.group(1) + '\n' + newseg + '\n' + new[m.end():]
            chg.append('结论先行:重写')
        else:
            mq = re.search(r'^((?:>\s*)?\*\*(?:先给结论|结论先行|核心结论)[^\n]*)$', new, re.M)
            if mq:
                qtext = j['conclusion_head'].replace('\n', '\n> ')
                new = (new[:mq.start()] + f'> **结论先行（{j["rating"]}）：** {qtext}'
                       + '\n' + new[mq.end():])
                chg.append('结论blockquote:重写')
    # 3) 最新季报块插入第一步开头
    qb = new_quarter_block(data, data.get('cur') or '$')
    if qb and '最新季报速览' not in new:
        m = re.search(r'(##[^\n]*第一步[^\n]*\n)', new)
        if m:
            new = new[:m.end()] + '\n' + qb + new[m.end():]
            chg.append('最新季报块:插入')
    # 4) 雷达表替换 (重建标准表)
    rd = j.get('radar') or []
    if len(rd) == 6:
        dims = ('商业模式', '护城河', '盈利能力', '现金流与资产安全', '估值安全边际', '成长性')
        rows = '\n'.join(f'| {dim} | {r["score"]} | {r["fact"]} | {r["counter"]} | {r["conf"]} |'
                         for dim, r in zip(dims, rd))
        ssum = sum(x['score'] for x in rd)
        soper = ssum - rd[4]['score']
        avg = round(ssum / 6, 1)
        sumline = (f'**六项均分{avg} → {round(avg*20)}分；'
                   f'剔除估值轴的五项经营均分{soper/5:.1f} → {round(soper/5*20)}分。**')
        table = ('### 六维雷达评分（0–5分，均分×20=百分制）\n\n'
                 '| 维度 | 评分 | 支持事实 | 反证/不确定性 | 置信度 |\n'
                 '|------|------|---------|--------------|--------|\n' + rows +
                 f'\n\n{sumline}')
        m = re.search(r'###?[^\n]*六维雷达评分[^\n]*\n(?:.*?)(?=\n###|\n## |\n\*\*D-I-V)', new, re.S)
        if m:
            new = new[:m.start()] + table + '\n' + new[m.end():]
            chg.append('雷达表:重写')
    # 5) TLDR 替换
    if j.get('tldr'):
        t = j['tldr'].strip()
        new2 = re.sub(r'\*\*一句话结论[：:]\*\*[^\n]+', f'**一句话结论：** {t}', new)
        if new2 == new:
            new = new.rstrip() + f'\n\n**一句话结论：** {t}\n'
        else:
            new = new2
        chg.append('TLDR:更新')
    return new, chg


def validate(text):
    """用渲染器提取函数校验输出可解析性"""
    met = B.extract_header_metrics(text)
    radar = B.extract_radar(text)
    trend = B.extract_trend(text)
    dcf = B.extract_dcf(text)
    ok = len(radar) == 6 and len(trend) >= 3 and met.get('px')
    return ok, {'px': met.get('px'), 'radar': len(radar), 'trend': len(trend), 'dcf': len(dcf)}


def find_md(sym, name):
    by_code, by_name = scan_analysis()
    hit = match_analysis(sym, name, by_code, by_name)
    return hit[1] if hit else None


def process_one(sym, name, orig_file, dry=False):
    print(f'--- {name}（{sym}）')
    orig = open(orig_file, encoding='utf-8').read()
    data = fetch_all(sym)
    if data.get('err') or not data.get('px'):
        print(f'  [SKIP] 行情抓取失败: {data.get("err", "无价格")}')
        return False
    cur = data.get('cur') or currency_of(sym)
    hl = header_line(sym, data)
    pack = {k: data.get(k) for k in ('px', 'pe', 'pb', 'div', 'w52l', 'w52h', 'cur')}
    pack['mcap_disp'] = _fmt_amount(data.get('mcap_yi', 0) * 1e8, cur) if data.get('mcap_yi') else None
    pack['last_quarter'] = [(dt, _fmt_amount(float(r), cur), _fmt_amount(float(n), cur) if n else None)
                            for dt, r, n in (data.get('last_quarter') or [])]
    pack['annual_recent'] = [(dt, _fmt_amount(r, cur), _fmt_amount(n, cur))
                             for dt, r, n in (data.get('annual') or [])[:3]]
    from llm_client import call_deepseek
    import time as _time

    def ask_llm(sys_extra=''):
        for attempt in range(3):
            content, jobj = call_deepseek(build_user_prompt(sym, name, orig, json.dumps(pack, ensure_ascii=False, indent=1), cur),
                                          system_prompt=SYSTEM_PROMPT + sys_extra, temperature=0.3, max_tokens=4096,
                                          extract_json=True, max_retries=2)
            if jobj:
                return jobj
            if isinstance(content, str) and content.strip():
                try:
                    return json.loads(content)
                except Exception:
                    pass
            print(f'  [LLM] 第{attempt + 1}次失败: {str(content)[:100]}')
            _time.sleep(8)
        return None

    j = ask_llm()
    if not j:
        print('  [SKIP] LLM失败或JSON为空')
        return False
    j['_cur'] = cur
    new_text, chg = merge_json(orig, j, hl, data)
    ok, info = validate(new_text)
    if not ok:
        print(f'  [RETRY] 校验未过 {info}, 重试LLM...')
        j = ask_llm(' 注意: 输出必须通过格式校验: rating 2-4字, radar 恰好6项, 每项score为0-5半刻度数字。')
        if j:
            j['_cur'] = cur
            try:
                new_text, chg = merge_json(orig, j, hl, data)
                ok, info = validate(new_text)
            except Exception:
                ok = False
    if not ok:
        print(f'  [SKIP] 二次校验未过 {info}, 保留原文件')
        return False
    new_date = date.today().strftime('%Y%m%d')
    m = re.search(r'-六步分析法-(\d{8})\.md', orig_file)
    dest = orig_file.replace(m.group(1), new_date) if m else orig_file
    if dry:
        import tempfile
        dest = os.path.join(tempfile.gettempdir(), f'dry_{sym}.md')
    if not dry and dest != orig_file and os.path.exists(orig_file):
        os.remove(orig_file)
    open(dest, 'w', encoding='utf-8').write(new_text)
    print(f'  [OK] {",".join(chg)} | 校验 {info} | {os.path.basename(dest)}')
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--deploy', action='store_true')
    ap.add_argument('--force', help='强制更新指定 symbol (调试)')
    args = ap.parse_args()

    if args.force:
        sym = args.force
        f = find_md(sym, '')
        if not f:
            print('找不到分析文件:', sym); return 1
        process_one(sym, sym, f, dry=args.dry_run)
        return 0

    cfg = json.load(open(CFG, encoding='utf-8'))
    need = []
    for group in ('stocks', 'hk_stocks'):
        for s in cfg.get(group, []):
            sym, name = s.get('symbol', ''), s.get('name', '')
            rd = md_date_to_cmp(s.get('report_date', '') or '')
            if not rd:
                continue
            f = find_md(sym, name)
            if not f or rd > re.search(r'-(\d{8})\.md', f).group(1):
                need.append((sym, name, f))
    print(f'检测到 {len(need)} 只股票财报新于分析:')
    for sym, name, _ in need:
        print(' ', name, sym)
    if not need:
        print('全部覆盖, 无需更新 ✓')
        return 0
    n_ok = 0
    for sym, name, f in need:
        try:
            if f and process_one(sym, name, f, dry=args.dry_run):
                n_ok += 1
        except Exception:
            print(f'  [EXC] {sym}:', traceback.format_exc(limit=2))
    print(f'完成: 成功更新 {n_ok}/{len(need)}')
    if args.deploy and not args.dry_run and n_ok:
        bat = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'update_all.bat')
        os.system(f'call "{bat}"')
    return 0


if __name__ == '__main__':
    sys.exit(main())
