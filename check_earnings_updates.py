#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""财报分析更新检测: 对比 config.report_date(最近财报日) 与 分析md文件日期,
列出"财报新于分析"或"无分析文件"的股票 → 生成待更新清单(供AI更新)。

用法: python check_earnings_updates.py [--open]
输出: okx/待更新分析清单.md + 控制台摘要
"""
import os, sys, re, json, glob

BASE = os.environ.get("WORKSPACE_ROOT", r"C:\Users\15949\WorkBuddy\xiaocai")
CFG = os.path.join(BASE, "watchlist_us", "config.json")
ANALYSIS_ROOT = os.path.join(BASE, "analysis", "港美股")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "待更新分析清单.md")

def scan_analysis():
    """扫描分析目录 → code/中文名 → 最新分析日期(YYYYMMDD)"""
    by_code, by_name = {}, {}
    for dname in ("美股", "港股", "A股"):
        for f in glob.glob(os.path.join(ANALYSIS_ROOT, dname, "*六步分析法-*.md")):
            m = re.match(r'(.+?)(?:-(.+?))?-六步分析法-(\d{8})\.md', os.path.basename(f))
            if not m:
                continue
            name, code, date = m.group(1), (m.group(2) or m.group(1)), m.group(3)
            if code not in by_code or date > by_code[code][0]:
                by_code[code] = (date, f)
            if name not in by_name or date > by_name[name][0]:
                by_name[name] = (date, f)
    return by_code, by_name

def match_analysis(sym, name, by_code, by_name):
    """与 build_analysis_pages 相同的匹配: sym → sym去后缀 → 中文名包含式"""
    hit = by_code.get(sym) or by_code.get(sym.split('.')[0])
    if not hit and name:
        for n2, v in by_name.items():
            if name in n2 or n2 in name:
                hit = v
                break
    return hit

def md_date_to_cmp(d):
    return d.replace('-', '')  # 2026-08-26 → 20260826

def main():
    if not os.path.exists(CFG):
        print('config 不存在:', CFG)
        return 1
    cfg = json.load(open(CFG, encoding='utf-8'))
    by_code, by_name = scan_analysis()

    need, ok = [], []
    for group in ('stocks', 'hk_stocks'):
        for s in cfg.get(group, []):
            sym = s.get('symbol', '')
            name = s.get('name', '')
            rd = md_date_to_cmp(s.get('report_date', '') or '')
            # 匹配分析文件: sym → sym去后缀 → 中文名包含式
            hit = match_analysis(sym, name, by_code, by_name)
            ana_date, ana_file = (hit if hit else ('', ''))
            if not rd:
                continue
            if not ana_file:
                need.append((name, sym, rd, '无分析文件', ana_file))
            elif rd > ana_date:
                need.append((name, sym, rd, f'分析止于{ana_date}', ana_file))
            else:
                ok.append((name, sym, rd, ana_date))

    need.sort(key=lambda x: x[2], reverse=True)  # 新财报在前
    today = __import__('datetime').date.today().isoformat()
    lines = [
        f'# 待更新分析清单（生成于 {today}）',
        '',
        f'> 检测逻辑: config.report_date(最近财报日, 每日自动校正) 晚于 分析文件日期 → 财报已出新分析未更新。',
        f'> 共扫描 {len(need) + len(ok)} 只股票: **待更新 {len(need)}**, 已覆盖 {len(ok)}。',
        '',
    ]
    if need:
        lines.append('## 待更新清单（财报新于分析，按财报日降序）')
        lines.append('')
        lines.append('把本节粘贴给 AI 助手，指令: 按最新六步分析法提示词（tools(不能删)\\股票分析六步提示词.md）'
                     '和英伟达标杆格式更新以下股票的分析文件，数据必须联网搜集真实值（股价/市值/PE/PB/股息率/近5年营收净利），'
                     '完成后跑 update_all.bat 部署。')
        lines.append('')
        for name, sym, rd, why, f in need:
            lines.append(f'- **{name}（{sym}）** 最近财报 {rd[:4]}-{rd[4:6]}-{rd[6:]}，{why}')
            if f:
                lines.append(f'  - 文件: `{f}`')
        lines.append('')
    else:
        lines.append('## 全部覆盖，无需更新 ✓')
        lines.append('')
    if ok:
        lines.append(f'<details><summary>已覆盖 {len(ok)} 只（点击展开）</summary>')
        lines.append('')
        for name, sym, rd, ad in ok:
            lines.append(f'- {name}（{sym}）财报 {rd} ≤ 分析 {ad}')
        lines.append('')
        lines.append('</details>')

    open(OUT, 'w', encoding='utf-8').write('\n'.join(lines))
    print(f'待更新 {len(need)} 只 / 已覆盖 {len(ok)} 只 → 清单: {OUT}')
    for name, sym, rd, why, _ in need[:20]:
        print(f'  {name:<10} {sym:<10} 财报{rd} {why}')
    if len(need) > 20:
        print(f'  ...等 {len(need)} 只, 详见清单文件')
    return 0

if __name__ == '__main__':
    sys.exit(main())
