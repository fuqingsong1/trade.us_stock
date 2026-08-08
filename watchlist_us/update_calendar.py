# -*- coding: utf-8 -*-
"""财报日历页生成器 - 读取 earnings_calendar.json 生成 earnings_calendar.html"""
import json
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DATA = BASE / 'earnings_calendar.json'
OUT = BASE / 'earnings_calendar.html'

STATUS_META = {
    'done': ('已完成', '#27ae60', '✓'),
    'upcoming': ('即将发布', '#e67e22', '⏰'),
    'pending': ('待确认日期', '#3498db', '?'),
}

def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def load_data():
    with open(DATA, 'r', encoding='utf-8') as f:
        return json.load(f)

def render_event_rows(events):
    rows = []
    for e in sorted(events, key=lambda x: (x['date'] == '2026-08-??', x['date'])):
        label, color, icon = STATUS_META.get(e['status'], STATUS_META['pending'])
        date_display = e['date'].replace('2026-08-??', '8月中旬待定')
        # 判断今天之后
        rows.append(f'''
        <tr>
            <td class="date">{date_display}</td>
            <td class="stock"><strong>{esc(e['name'])}</strong> <span class="sym">{esc(e['symbol'])}</span></td>
            <td>{esc(e['period'])}</td>
            <td><span class="badge" style="background:{color}">{icon} {label}</span></td>
            <td class="note">{esc(e['note'])}</td>
        </tr>''')
    return '\n'.join(rows)

def render_watch_table(items):
    rows = []
    for it in items:
        rows.append(f'''
        <tr>
            <td class="stock"><strong>{esc(it['name'])}</strong> <span class="sym">{esc(it['symbol'])}</span></td>
            <td class="note">{esc(it['note'])}</td>
        </tr>''')
    return '\n'.join(rows)

def main():
    data = load_data()
    done_events = [e for e in data['events'] if e['status'] == 'done']
    upcoming_events = [e for e in data['events'] if e['status'] == 'upcoming']
    pending_events = [e for e in data['events'] if e['status'] == 'pending']
    updated = data['updated']
    today = datetime.now().strftime('%Y-%m-%d')

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>美股财报日历</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
            background: #f5f5f5; padding: 20px; line-height: 1.6;
        }}
        .header {{
            background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .header h1 {{ font-size: 24px; color: #333; margin-bottom: 8px; }}
        .header .meta {{ color: #666; font-size: 14px; }}
        .nav {{
            margin-top: 12px; display: flex; gap: 10px; flex-wrap: wrap;
        }}
        .nav a {{
            display: inline-block; padding: 8px 16px; border-radius: 4px;
            text-decoration: none; font-size: 14px; transition: background 0.2s;
        }}
        .nav .active {{
            background: #2c3e50; color: white;
        }}
        .nav .normal {{
            background: #3498db; color: white;
        }}
        .nav .normal:hover {{ background: #2980b9; }}
        .stats {{
            display: flex; gap: 15px; margin-top: 15px; flex-wrap: wrap;
        }}
        .stat-card {{
            background: #f8f9fa; border-radius: 6px; padding: 10px 18px; text-align: center;
            flex: 1; min-width: 120px;
        }}
        .stat-card .num {{ font-size: 26px; font-weight: 700; color: #2c3e50; }}
        .stat-card .lbl {{ font-size: 12px; color: #888; margin-top: 2px; }}
        .section {{
            background: white; border-radius: 8px; margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1); overflow: hidden;
        }}
        .section h2 {{
            font-size: 18px; color: #2c3e50; padding: 16px 20px;
            border-bottom: 1px solid #eee;
        }}
        .section h2 .count {{ color: #999; font-weight: 400; font-size: 14px; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th {{
            background: #f8f9fa; color: #555; font-size: 13px; font-weight: 600;
            text-align: left; padding: 10px 20px; border-bottom: 2px solid #eee;
        }}
        td {{ padding: 10px 20px; border-bottom: 1px solid #f0f0f0; font-size: 14px; }}
        tr:hover td {{ background: #fafafa; }}
        .date {{ color: #666; white-space: nowrap; }}
        .sym {{ color: #999; font-size: 12px; margin-left: 4px; }}
        .badge {{
            display: inline-block; padding: 2px 10px; border-radius: 12px;
            color: white; font-size: 12px; white-space: nowrap;
        }}
        .note {{ color: #777; font-size: 13px; }}
        .footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 20px; }}
        @media (max-width: 768px) {{
            body {{ padding: 10px; }}
            th, td {{ padding: 8px 10px; }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>📅 美股财报日历</h1>
        <div class="meta">数据更新：{updated} ｜ 今日：{today} ｜ 共 {len(data['events'])} 条财报事件</div>
        <div class="nav">
            <a class="normal" href="index.html">← 返回观察清单</a>
            <span class="active">📅 财报日历</span>
        </div>
        <div class="stats">
            <div class="stat-card"><div class="num" style="color:#27ae60">{len(done_events)}</div><div class="lbl">已完成</div></div>
            <div class="stat-card"><div class="num" style="color:#e67e22">{len(upcoming_events)}</div><div class="lbl">即将发布</div></div>
            <div class="stat-card"><div class="num" style="color:#3498db">{len(pending_events)}</div><div class="lbl">待确认日期</div></div>
        </div>
    </div>

    <div class="section">
        <h2>⏰ 即将发布 <span class="count">({len(upcoming_events)})</span></h2>
        <table>
            <thead><tr><th>日期</th><th>公司</th><th>财报期</th><th>状态</th><th>备注</th></tr></thead>
            <tbody>{render_event_rows(upcoming_events)}</tbody>
        </table>
    </div>

    <div class="section">
        <h2>? 待确认日期 <span class="count">({len(pending_events)})</span></h2>
        <table>
            <thead><tr><th>日期</th><th>公司</th><th>财报期</th><th>状态</th><th>备注</th></tr></thead>
            <tbody>{render_event_rows(pending_events)}</tbody>
        </table>
    </div>

    <div class="section">
        <h2>✓ 本季已完成 <span class="count">({len(done_events)})</span></h2>
        <table>
            <thead><tr><th>日期</th><th>公司</th><th>财报期</th><th>状态</th><th>备注</th></tr></thead>
            <tbody>{render_event_rows(done_events)}</tbody>
        </table>
    </div>

    <div class="section">
        <h2>🔍 其他持仓待核实 <span class="count">({len(data['watch_items'])})</span></h2>
        <table>
            <thead><tr><th>公司</th><th>备注</th></tr></thead>
            <tbody>{render_watch_table(data['watch_items'])}</tbody>
        </table>
    </div>

    <div class="footer">由 earnings_calendar.json 自动生成 ｜ 财报日期以公司官方公告为准</div>
</body>
</html>'''

    OUT.write_text(html, encoding='utf-8')
    print(f'已生成: {OUT} ({len(html)} 字节)')

if __name__ == '__main__':
    main()
