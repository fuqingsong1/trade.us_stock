#!/usr/bin/env python3
"""
transform.py - 将 dashboard.py 生成的原始 dashboard.html 转换为定制版 index.html
用法: python transform.py [input.html] [output.html]
默认: input=C:\\Users\\15949\\.qclaw\\workspace-agent-639a44f3\\okx\\dashboard.html
      output=同目录下 index.html
"""
import re, sys, os

INPUT_DEFAULT = r"C:\Users\15949\.qclaw\workspace-agent-639a44f3\okx\dashboard.html"
OUTPUT_DEFAULT = os.path.join(os.path.dirname(INPUT_DEFAULT), "index.html")

# ════════════════════════════════════════════════════
# 1. 深色主题 CSS（从 deploy/index.html 提取，含响应式）
# ════════════════════════════════════════════════════
DARK_CSS = r'''* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif;
  background: #0d1117; color: #c9d1d9; padding: 20px; min-width: 1350px;
  background-image: radial-gradient(circle at 20% 20%, rgba(55,138,221,0.04) 0%, transparent 40%),
                    radial-gradient(circle at 80% 60%, rgba(108,92,231,0.03) 0%, transparent 40%);
}
.header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 16px 24px; background: #161b22; border-radius: 12px;
  border: 1px solid #30363d; margin-bottom: 16px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.3);
}
.header h1 { font-size: 18px; font-weight: 600; background: linear-gradient(135deg, #58a6ff, #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.header .time { font-size: 13px; color: #8b949e; font-weight: 500; }
.header .refresh {
  background: linear-gradient(135deg, #238636, #1a7f37); color: #fff; border: 1px solid #2ea043;
  border-radius: 8px; padding: 8px 16px; font-size: 13px; cursor: pointer; font-weight: 500;
  transition: all 0.2s;
}
.header .refresh:hover { background: linear-gradient(135deg, #2ea043, #238636); box-shadow: 0 0 12px rgba(46,160,67,0.3); }
.tabs { display: flex; gap: 4px; margin-bottom: 16px; }
.tab-btn {
  background: #21262d; border: 1px solid #30363d;
  padding: 10px 24px; font-size: 13px; font-weight: 500; cursor: pointer;
  color: #8b949e; border-radius: 8px 8px 0 0; transition: all 0.2s;
}
.tab-btn.active {
  background: #161b22; color: #58a6ff; border-color: #30363d #30363d #161b22;
  position: relative; z-index: 1; font-weight: 600;
  box-shadow: 0 -2px 0 #58a6ff;
}
.tab-btn:hover:not(.active) { color: #c9d1d9; background: #1c2128; border-color: #8b949e; }
.tab-content { display: none; overflow-x: auto; }
.tab-content.active { display: block; animation: fadeIn 0.3s ease; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
.summary {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px;
}
.summary .card {
  background: #161b22; border-radius: 10px; padding: 18px 16px;
  border: 1px solid #30363d; transition: all 0.2s;
}
.summary .card:hover { border-color: #58a6ff; box-shadow: 0 0 16px rgba(88,166,255,0.1); }
.summary .card .label { font-size: 11px; color: #8b949e; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
.summary .card .value { font-size: 24px; font-weight: 600; }
.summary .card .value.green { color: #3fb950; }
.summary .card .value.red { color: #f85149; }
.summary .card .value.blue { color: #58a6ff; }
table {
  width: 100%; border-collapse: collapse; background: #161b22;
  border-radius: 12px; overflow: hidden; border: 1px solid #30363d; table-layout: fixed;
}
thead { background: #1c2128; }
th {
  padding: 10px 4px; font-size: 12px; font-weight: 500; color: #8b949e;
  text-align: center; border-bottom: 1px solid #30363d; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis;
}
td { padding: 8px 4px; font-size: 13px; border-bottom: 1px solid #21262d; text-align: center; white-space: nowrap; }
tr:hover td { background: #1c2128; }
td.sym { font-weight: 500; }
td.name { color: #8b949e; font-size: 12px; }
td.num { font-variant-numeric: tabular-nums; }
.eligible { color: #3fb950; font-weight: 600; }
.ineligible { color: #f85149; font-weight: 600; }
.bar-cell { width: 200px; }
.bar-wrap { position: relative; height: 20px; background: #21262d; border-radius: 3px; overflow: hidden; }
.bar-buy1 { position: absolute; top: 0; height: 100%; background: rgba(63,185,80,0.35); }
.bar-buy2 { position: absolute; top: 0; height: 100%; background: rgba(63,185,80,0.22); }
.bar-buy3 { position: absolute; top: 0; height: 100%; background: rgba(63,185,80,0.12); }
.bar-sell1 { position: absolute; top: 0; height: 100%; background: rgba(248,81,73,0.18); }
.bar-sell2 { position: absolute; top: 0; height: 100%; background: rgba(248,81,73,0.30); }
.bar-pct { position: absolute; top: 0; width: 2px; height: 100%; background: #c9d1d9; }
.bar-pct-label { position: absolute; top: -1px; font-size: 9px; font-weight: 500; transform: translateX(4px); line-height: 20px; white-space: nowrap; color: #c9d1d9; }
.zone-buy1 { color: #3fb950; font-weight: 600; }
.zone-buy2 { color: #56d364; font-weight: 600; }
.zone-buy3 { color: #7ee787; font-weight: 600; }
.zone-sell1 { color: #d2991d; font-weight: 600; }
.zone-sell2 { color: #f85149; font-weight: 600; }
.zone-lower { color: #8b949e; }
.zone-upper { color: #8b949e; }
.bar-short1 { position: absolute; top: 0; height: 100%; background: rgba(248,81,73,0.25); }
.bar-short2 { position: absolute; top: 0; height: 100%; background: rgba(248,81,73,0.45); }
.bar-short3 { position: absolute; top: 0; height: 100%; background: rgba(248,81,73,0.32); }
.bar-tp     { position: absolute; top: 0; height: 100%; background: rgba(63,185,80,0.30); }
.szone1 { color: #d2991d; font-weight: 600; }
.szone2 { color: #f85149; font-weight: 700; }
.szone3 { color: #f85149; font-weight: 700; }
.szone-near { color: #d2991d; }
.szone-tp { color: #3fb950; }
.szone-low { color: #8b949e; }
.short-rule-box { background: rgba(248,81,73,0.08); border-radius: 12px; border: 1px solid rgba(248,81,73,0.2); padding: 14px 20px; margin-bottom: 16px; font-size: 13px; color: #f0883e; line-height: 1.8; }
tr.short-pos { background: rgba(248, 81, 73, 0.08); }
.short-badge { display: inline-block; background: #f85149; color: #fff; font-size: 9px; padding: 1px 4px; border-radius: 3px; margin-left: 4px; vertical-align: middle; font-weight: 500; }
.legend { display: flex; gap: 20px; margin: 12px 0; font-size: 12px; color: #8b949e; padding: 0 4px; }
.legend span { display: flex; align-items: center; gap: 4px; }
.legend .dot { width: 10px; height: 10px; border-radius: 2px; }
.advice-box { background: #161b22; border-radius: 12px; border: 1px solid #30363d; margin-top: 16px; padding: 16px 20px; }
.advice-box h3 { font-size: 15px; font-weight: 600; margin-bottom: 10px; color: #c9d1d9; }
.advice-box h3 .ai-tag { background: linear-gradient(135deg, #8b5cf6, #6c5ce7); color: #fff; font-size: 10px; padding: 2px 8px; border-radius: 4px; margin-left: 8px; font-weight: 500; vertical-align: middle; }
.advice-content { font-size: 14px; line-height: 1.7; color: #c9d1d9; white-space: pre-wrap; }
.wait-queue-box { background: #161b22; border-radius: 12px; border: 1px solid #30363d; margin-top: 16px; padding: 16px 20px; }
.wait-queue-box h3 { font-size: 15px; font-weight: 600; margin-bottom: 10px; color: #c9d1d9; }
.wq-count { background: #58a6ff; color: #fff; font-size: 10px; padding: 2px 8px; border-radius: 4px; margin-left: 8px; font-weight: 500; vertical-align: middle; }
.wq-list { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
.wq-item { background: rgba(88,166,255,0.1); border: 1px solid rgba(88,166,255,0.2); border-radius: 8px; padding: 6px 12px; font-size: 13px; font-weight: 500; color: #58a6ff; }
.wq-ratio { color: #8b949e; font-size: 11px; margin-left: 4px; }
.wq-note { font-size: 12px; color: #8b949e; }
.deposit-alert-box { background: rgba(248,81,73,0.08); border-radius: 12px; border: 2px solid rgba(248,81,73,0.3); margin-top: 16px; padding: 16px 20px; }
.deposit-alert-box h3 { font-size: 15px; font-weight: 600; margin-bottom: 10px; color: #f85149; }
.da-detail { font-size: 13px; color: #8b949e; margin-bottom: 6px; }
.da-action { font-size: 14px; color: #f85149; font-weight: 500; }
.footer { text-align: center; padding: 20px; font-size: 12px; color: #484f58; }
.status-ok { color: #3fb950; font-weight: 600; }
.status-warn { color: #f85149; font-weight: 600; }
.status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; vertical-align: middle; animation: pulse 2s infinite; }
.status-dot.green { background: #3fb950; box-shadow: 0 0 6px rgba(63,185,80,0.5); }
.status-dot.red { background: #f85149; box-shadow: 0 0 6px rgba(248,81,73,0.5); }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
tr.has-position { background: rgba(88,166,255,0.08); }
tr.has-position:hover td { background: rgba(88,166,255,0.12); }
tr.has-obs { background: rgba(139,148,158,0.06); }
tr.eligible-no-pos { background: rgba(63,185,80,0.12); }
tr.eligible-no-pos:hover td { background: rgba(63,185,80,0.16); }
.lev-badge { display:inline-block; background:rgba(63,185,80,0.15); color:#3fb950; font-size:11px; padding:2px 6px; border-radius:4px; margin:0 2px; font-weight:600; border:1px solid rgba(63,185,80,0.25); }
.lev-badge-7 { background:rgba(86,211,100,0.15); color:#56d364; border-color:rgba(86,211,100,0.25); }
.lev-badge-10 { background:rgba(88,166,255,0.12); color:#58a6ff; border-color:rgba(88,166,255,0.2); }
.lever-10 { color: #3fb950; font-weight: 600; }
.lever-7 { color: #56d364; font-weight: 600; }
tr.has-position td.sym { position: relative; }
tr.has-position td.sym::before { content: ''; position: absolute; left: 0; top: 25%; height: 50%; width: 3px; background: #58a6ff; border-radius: 2px; }
.pos-badge { display: inline-block; background: #58a6ff; color: #fff; font-size: 9px; padding: 1px 4px; border-radius: 3px; margin-left: 4px; vertical-align: middle; font-weight: 500; }
.obs-badge { display: inline-block; background: #8b949e; color: #fff; font-size: 9px; padding: 1px 4px; border-radius: 3px; margin-left: 4px; vertical-align: middle; font-weight: 500; }
tr.has-obs td.sym { position: relative; }
tr.has-obs td.sym::before { content: ''; position: absolute; left: 0; top: 25%; height: 50%; width: 3px; background: #8b949e; border-radius: 2px; }
tr.is-index { border-bottom: 2px solid #30363d; }
tr.is-index td.sym { font-weight: 700; color: #58a6ff; }
.stock-section { background: #161b22; border-radius: 12px; border: 1px solid #30363d; margin-bottom: 16px; overflow: hidden; }
.macro-section { border-color: #58a6ff; border-width: 2px; }
.macro-header { background: rgba(88,166,255,0.08) !important; }
.stock-header { display: flex; justify-content: space-between; align-items: center; padding: 14px 20px; background: #1c2128; border-bottom: 1px solid #30363d; }
.stock-info { display: flex; align-items: center; gap: 8px; }
.stock-sym { font-size: 16px; font-weight: 600; color: #58a6ff; }
.stock-name { font-size: 14px; color: #8b949e; }
.pos-tag { background: #58a6ff; color: #fff; font-size: 10px; padding: 2px 6px; border-radius: 4px; font-weight: 500; }
.eli-tag { background: #238636; color: #fff; font-size: 10px; padding: 2px 6px; border-radius: 4px; font-weight: 500; }
.macro-tag { background: #8b5cf6; color: #fff; font-size: 10px; padding: 2px 6px; border-radius: 4px; font-weight: 500; }
.stock-agg { font-size: 14px; font-weight: 500; }
.agg-positive { color: #3fb950; }
.agg-negative { color: #f85149; }
.agg-neutral { color: #8b949e; }
.earnings-row { padding: 6px 20px; font-size: 12px; color: #8b949e; background: #1c2128; border-bottom: 1px solid #21262d; display: flex; align-items: center; gap: 6px; }
.earnings-row::before { content: "\1F4C5"; font-size: 12px; }
.ern-soon { color: #f85149; font-weight: 600; }
.ern-near { color: #d2991d; font-weight: 500; }
.ern-far { color: #8b949e; }
.news-list { padding: 8px 16px; }
.news-card { padding: 12px 8px; border-bottom: 1px solid #21262d; transition: background 0.2s; }
.news-card:hover { background: #1c2128; }
.news-card:last-child { border-bottom: none; }
.news-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
.date { font-size: 12px; color: #8b949e; }
.provider { font-size: 12px; color: #8b949e; font-weight: 500; }
.type-tag { color: #fff; font-size: 10px; padding: 1px 6px; border-radius: 3px; font-weight: 500; }
.badge { font-size: 10px; padding: 1px 6px; border-radius: 3px; font-weight: 600; }
.badge.positive { color: #3fb950; background: rgba(63,185,80,0.15); }
.badge.negative { color: #f85149; background: rgba(248,81,73,0.15); }
.badge.neutral { color: #8b949e; background: rgba(139,148,158,0.15); }
.impact { font-size: 13px; font-weight: 600; }
.pos-header-bar { background: #161b22; border-radius: 12px; border: 1px solid #30363d; padding: 12px 20px; margin-bottom: 16px; display: flex; justify-content: space-between; align-items: center; }
.pos-table { width: 100%; border-collapse: collapse; background: #161b22; border-radius: 12px; overflow: hidden; border: 1px solid #30363d; }
.pos-table th { padding: 10px 12px; text-align: center; font-size: 12px; font-weight: 500; color: #8b949e; background: #1c2128; border-bottom: 1px solid #30363d; }
.pos-table td { padding: 10px 12px; text-align: center; font-size: 13px; border-bottom: 1px solid #21262d; }
.pos-table tr:hover td { background: #1c2128; }
.pos-table .pnl-pos { color: #3fb950; font-weight: 600; }
.pos-table .pnl-neg { color: #f85149; font-weight: 600; }
.pie-container { background: #161b22; border-radius: 12px; border: 1px solid #30363d; padding: 16px 20px; margin-top: 12px; }
.pie-title { font-size: 14px; font-weight: 600; margin-bottom: 12px; color: #c9d1d9; }
.pie-body { display: flex; align-items: center; gap: 24px; }
.pie-svg { width: 180px; height: 180px; flex-shrink: 0; }
.pie-legend { flex: 1; display: flex; flex-direction: column; gap: 6px; }
.pie-legend-item { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.pie-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.pie-label { flex: 1; color: #c9d1d9; }
.pie-pct { font-weight: 600; color: #c9d1d9; min-width: 48px; text-align: right; }
.pie-val { color: #8b949e; font-size: 12px; min-width: 60px; text-align: right; }
.title-row { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.title-cn { font-size: 15px; font-weight: 600; line-height: 1.4; color: #c9d1d9; text-decoration: none; }
a .title-cn:hover { color: #58a6ff; }
.reason-inline { font-size: 12px; color: #8b949e; padding-left: 6px; border-left: 2px solid #30363d; line-height: 1.4; white-space: nowrap; }
.news-summary-inline { font-size: 12px; color: #8b5cf6; font-weight: 500; padding-left: 6px; border-left: 2px solid #8b5cf6; line-height: 1.4; white-space: nowrap; }
.ai-summary { padding: 10px 20px; background: linear-gradient(90deg, rgba(108,92,231,0.08) 0%, rgba(88,166,255,0.08) 100%); border-top: 1px solid #30363d; font-size: 13px; color: #c9d1d9; line-height: 1.6; }
.ai-summary-tag { display: inline-block; background: linear-gradient(135deg, #8b5cf6, #6c5ce7); color: #fff; font-size: 10px; padding: 2px 8px; border-radius: 4px; margin-right: 8px; font-weight: 500; vertical-align: middle; }
.no-data { text-align: center; padding: 40px; color: #8b949e; font-size: 14px; }
.regime-verdict {
  display: flex; justify-content: space-between; align-items: center;
  border-radius: 12px; padding: 20px 24px; margin-bottom: 12px;
  border: 1px solid #30363d; background: #161b22;
}
.regime-verdict-left { display: flex; flex-direction: column; gap: 4px; }
.regime-verdict-stage { font-size: 22px; font-weight: 700; color: #c9d1d9; }
.regime-verdict-score { font-size: 16px; font-weight: 600; }
.regime-verdict-right { display: flex; flex-direction: column; gap: 6px; text-align: right; }
.regime-verdict-exposure { font-size: 16px; color: #c9d1d9; }
.regime-verdict-exposure strong { font-size: 24px; color: #58a6ff; }
.regime-verdict-action { font-size: 15px; color: #8b949e; }
.regime-verdict-action strong { color: #f85149; }
.regime-explain {
  background: rgba(88,166,255,0.08); border-radius: 8px; border: 1px solid rgba(88,166,255,0.2);
  padding: 10px 16px; margin-bottom: 12px;
  font-size: 13px; color: #58a6ff; line-height: 1.6;
}
.r-row { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid #21262d; }
.r-row:last-child { border-bottom: none; }
.r-label { width: 130px; font-size: 13px; font-weight: 600; color: #c9d1d9; flex-shrink: 0; }
.r-bar-wrap { flex: 1; padding: 0 4px; }
.r-bar-track { position: relative; height: 14px; background: #21262d; border-radius: 7px; overflow: hidden; }
.r-bar-fill { position: absolute; top: 0; left: 0; height: 100%; border-radius: 7px; transition: width 0.5s ease; }
.r-bar-marker { position: absolute; top: -3px; width: 3px; height: 20px; background: #c9d1d9; border-radius: 2px; }
.r-score { width: 65px; font-size: 13px; font-weight: 600; text-align: center; flex-shrink: 0; }
.r-detail { font-size: 11px; color: #8b949e; flex: 0 1 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.r-legend { display: flex; align-items: center; font-size: 12px; color: #8b949e; padding: 4px 0 8px 136px; }
.r-leg-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 4px; }
.regime-detail-bar {
  background: #161b22; border-radius: 10px; border: 1px solid #30363d;
  padding: 10px 16px; margin-top: 12px;
  font-size: 12px; color: #8b949e; line-height: 1.6;
  word-break: break-all;
}
.cal-table { width: 100%; border-collapse: collapse; }
.cal-table th { text-align: left; padding: 10px 20px; background: #1c2128; color: #8b949e; font-size: 13px; font-weight: 600; border-bottom: 1px solid #30363d; }
.cal-table td { text-align: left; padding: 10px 20px; font-size: 14px; border-bottom: 1px solid #21262d; }
.cal-table tr:hover td { background: #1c2128; }
.cal-table .date { color: #8b949e; white-space: nowrap; }
.cal-table .sym { color: #8b949e; font-size: 12px; }
.cal-table .note { color: #c9d1d9; font-size: 13px; }
/* ── Landing Page ── */
.landing {
  position: fixed; inset: 0; z-index: 9999;
  background: #06080d;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  overflow: hidden; font-family: -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif;
  transition: opacity 0.6s ease, visibility 0.6s ease;
}
.landing.hidden { opacity: 0; visibility: hidden; pointer-events: none; }
.landing-stars { position: absolute; inset: 0; }
.landing::before {
  content: ''; position: absolute; inset: 0;
  background-image: radial-gradient(circle at 30% 20%, rgba(88,166,255,0.06) 0%, transparent 50%),
                    radial-gradient(circle at 70% 60%, rgba(139,92,246,0.05) 0%, transparent 50%),
                    radial-gradient(circle at 50% 80%, rgba(0,212,170,0.04) 0%, transparent 40%);
}
.landing-grid {
  position: absolute; inset: 0;
  background-image:
    linear-gradient(rgba(48,54,61,0.4) 1px, transparent 1px),
    linear-gradient(90deg, rgba(48,54,61,0.4) 1px, transparent 1px);
  background-size: 80px 80px;
  animation: gridMove 25s linear infinite;
}
@keyframes gridMove { 0% { transform: translate(0,0); } 100% { transform: translate(80px,80px); } }
.landing-orb { position: absolute; border-radius: 50%; pointer-events: none; }
.landing-orb.o1 {
  width: 500px; height: 500px; top: -10%; left: -5%;
  background: radial-gradient(circle, rgba(88,166,255,0.12) 0%, transparent 70%);
  animation: orbFloat1 12s ease-in-out infinite;
}
.landing-orb.o2 {
  width: 350px; height: 350px; bottom: -15%; right: -5%;
  background: radial-gradient(circle, rgba(139,92,246,0.1) 0%, transparent 70%);
  animation: orbFloat2 10s ease-in-out infinite;
}
.landing-orb.o3 {
  width: 280px; height: 280px; top: 50%; right: 10%;
  background: radial-gradient(circle, rgba(0,212,170,0.08) 0%, transparent 70%);
  animation: orbFloat3 14s ease-in-out infinite;
}
@keyframes orbFloat1 { 0%,100% { transform: translate(0,0) scale(1); } 33% { transform: translate(60px,40px) scale(1.1); } 66% { transform: translate(-30px,-20px) scale(0.9); } }
@keyframes orbFloat2 { 0%,100% { transform: translate(0,0) scale(1); } 33% { transform: translate(-50px,-30px) scale(0.9); } 66% { transform: translate(30px,20px) scale(1.15); } }
@keyframes orbFloat3 { 0%,100% { transform: translate(0,0) scale(1); } 50% { transform: translate(-40px,-60px) scale(1.2); } }
.landing-content {
  position: relative; z-index: 2; text-align: center; max-width: 720px; padding: 0 24px;
}
.landing-candles {
  display: flex; gap: 8px; justify-content: center; align-items: flex-end; margin-bottom: 44px;
}
.candle { width: 4px; border-radius: 2px; position: relative; filter: drop-shadow(0 0 6px currentColor); }
.candle.green { background: #3fb950; height: 65px; color: #3fb950; }
.candle.red { background: #f85149; height: 50px; color: #f85149; }
.candle.green::after { content: ''; position: absolute; top: -22px; left: -2px; width: 8px; height: 22px; background: #3fb950; border-radius: 2px 2px 0 0; }
.candle.red::after { content: ''; position: absolute; bottom: -26px; left: -2px; width: 8px; height: 26px; background: #f85149; border-radius: 0 0 2px 2px; }
.candle.green.sm { height: 38px; }
.candle.red.sm  { height: 30px; }
.candle.green.xs { height: 25px; }
.candle.red.xs  { height: 20px; }
.landing-title {
  font-size: 48px; font-weight: 800; letter-spacing: 3px; margin-bottom: 16px;
  background: linear-gradient(135deg, #58a6ff 0%, #8b5cf6 40%, #00d4aa 100%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  background-clip: text; filter: drop-shadow(0 0 20px rgba(88,166,255,0.3));
  animation: titleGlow 4s ease-in-out infinite;
}
@keyframes titleGlow {
  0%,100% { filter: drop-shadow(0 0 20px rgba(88,166,255,0.3)); }
  50% { filter: drop-shadow(0 0 40px rgba(139,92,246,0.5)); }
}
.landing-subtitle {
  font-size: 15px; color: rgba(255,255,255,0.35); margin-bottom: 12px;
  letter-spacing: 4px; font-weight: 300;
}
.landing-desc {
  font-size: 14px; color: rgba(255,255,255,0.25); line-height: 1.8; margin-bottom: 48px;
}
.landing-stats {
  display: flex; gap: 40px; justify-content: center; margin-bottom: 48px;
}
.landing-stat { text-align: center; }
.landing-stat .val {
  font-size: 32px; font-weight: 700; font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
  background: linear-gradient(135deg, #58a6ff, #8b5cf6);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.landing-stat .lbl { font-size: 11px; color: rgba(255,255,255,0.25); margin-top: 6px; letter-spacing: 2px; text-transform: uppercase; }
.landing-btn {
  display: inline-block; padding: 16px 56px;
  background: linear-gradient(135deg, #238636, #1a7f37);
  color: #fff; font-size: 16px; font-weight: 700; letter-spacing: 3px;
  border: 1px solid rgba(46,160,67,0.5); border-radius: 50px; cursor: pointer;
  transition: all 0.3s ease; position: relative; overflow: hidden;
  box-shadow: 0 0 30px rgba(35,134,54,0.3), 0 0 60px rgba(35,134,54,0.1);
}
.landing-btn::after {
  content: ''; position: absolute; top: 0; left: -100%; width: 100%; height: 100%;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.1), transparent);
  transition: left 0.5s;
}
.landing-btn:hover::after { left: 100%; }
.landing-btn:hover {
  transform: translateY(-2px);
  box-shadow: 0 0 50px rgba(35,134,54,0.5), 0 0 100px rgba(35,134,54,0.2);
}
.landing-ticker {
  position: absolute; bottom: 60px; width: 100%;
  overflow: hidden; white-space: nowrap;
  border-top: 1px solid rgba(48,54,61,0.5);
  border-bottom: 1px solid rgba(48,54,61,0.5);
  padding: 8px 0;
}
.landing-ticker-inner {
  display: inline-block; animation: tickerScroll 40s linear infinite;
}
.landing-ticker-item {
  display: inline-block; margin: 0 28px; font-size: 12px; font-family: 'SF Mono', 'Consolas', monospace;
  color: rgba(255,255,255,0.2);
}
.landing-ticker-item .t-up { color: #3fb950; }
.landing-ticker-item .t-down { color: #f85149; }
@keyframes tickerScroll { 0% { transform: translateX(0); } 100% { transform: translateX(-50%); } }
.landing-footer {
  position: absolute; bottom: 24px; font-size: 11px;
  color: rgba(255,255,255,0.15); letter-spacing: 2px;
}
.landing-pulse {
  position: fixed; top: 50%; left: 50%;
  width: 500px; height: 500px; margin: -250px 0 0 -250px;
  border-radius: 50%; border: 1px solid rgba(88,166,255,0.12);
  animation: pulseRing 5s ease-out infinite; pointer-events: none; z-index: 1;
}
.landing-pulse.p2 {
  animation-delay: 2.5s;
}
@keyframes pulseRing {
  0% { transform: scale(0.7); opacity: 0.5; }
  100% { transform: scale(1.8); opacity: 0; }
}
::-webkit-scrollbar { width: 8px; }
::-webkit-scrollbar-track { background: #0d1117; }
::-webkit-scrollbar-thumb { background: #30363d; border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: #484f58; }

@media (max-width: 768px) {
  body { min-width: unset; padding: 10px; }
  .header { flex-wrap: wrap; gap: 8px; }
  .header h1 { font-size: 15px; }
  .header .time { font-size: 11px; }
  .summary { grid-template-columns: repeat(2, 1fr); gap: 8px; }
  .summary .card .value { font-size: 18px; }
  .summary .card .label { font-size: 10px; }
  .tab-btn { padding: 6px 12px; font-size: 11px; }
  .sub-tab-btn { padding: 6px 10px; font-size: 10px; }
  .tab-bar, .sub-tab-row { flex-wrap: wrap; gap: 4px; }
  table { font-size: 11px; }
  th { font-size: 10px; padding: 8px 3px; }
  td { font-size: 11px; padding: 6px 3px; }
  .bar-cell { width: 100px; }
  td.name { font-size: 10px; }
  .card, .regime-card, .news-card { padding: 12px; }
  .short-rule-box, .advice-box, .wait-queue-box, .deposit-alert-box { padding: 10px 14px; font-size: 12px; }
  .pos-table { font-size: 11px; }
  .landing-title { font-size: 26px; }
  .landing-subtitle { font-size: 11px; letter-spacing: 3px; }
  .landing-desc { font-size: 12px; }
  .landing-btn { padding: 12px 36px; font-size: 15px; }
  .landing-stock-ticker { font-size: 11px; }
  .landing-orb { width: 120px; height: 120px; }
  .landing-orb-2 { width: 90px; height: 90px; }
  .landing-orb-3 { width: 60px; height: 60px; }
  .landing-ring { width: 200px; height: 200px; }
  .landing-ring-2 { width: 280px; height: 280px; }
  .landing-footer { font-size: 10px; }
}
@media (max-width: 480px) {
  .summary { grid-template-columns: 1fr 1fr; gap: 6px; }
  .summary .card .value { font-size: 16px; }
  .landing-title { font-size: 22px; }
  .landing-subtitle { font-size: 10px; letter-spacing: 2px; }
  .landing-desc { font-size: 11px; }
  .landing-btn { padding: 10px 28px; font-size: 14px; }
}'''

# ════════════════════════════════════════════════════
# 2. Landing 页 HTML
# ════════════════════════════════════════════════════
LANDING_HTML = '''<!-- ════ Landing Page ════ -->
<div class="landing" id="landing">
  <div class="landing-grid"></div>
  <div class="landing-orb o1"></div>
  <div class="landing-orb o2"></div>
  <div class="landing-orb o3"></div>
  <div class="landing-pulse"></div>
  <div class="landing-pulse p2"></div>
  <div class="landing-content">
    <div class="landing-candles">
      <div class="candle green"></div>
      <div class="candle red"></div>
      <div class="candle green sm"></div>
      <div class="candle green"></div>
      <div class="candle red xs"></div>
      <div class="candle green sm"></div>
      <div class="candle red sm"></div>
      <div class="candle green"></div>
      <div class="candle red"></div>
      <div class="candle green xs"></div>
      <div class="candle green sm"></div>
      <div class="candle red sm"></div>
      <div class="candle green"></div>
    </div>
    <div class="landing-title">美股 低频量化交易看板</div>
    <div class="landing-subtitle">US STOCK &middot; LOW-FREQ QUANT  SYSTEM</div>
    <div class="landing-desc">多标的做T区间可视化 &middot; 策略运行状态监控 &middot; 全球财经新闻聚合</div>
    <div class="landing-stats">
      <div class="landing-stat"><div class="val" id="landing-time">--:--:--</div><div class="lbl">Beijing Time</div></div>
    </div>
    <button class="landing-btn" id="enterBtn" onclick="enterDashboard()">进 入 看 板</button>
  </div>
  <div class="landing-ticker">
    <div class="landing-ticker-inner" id="landing-ticker-inner"></div>
  </div>
  <div class="landing-footer">&copy; 2026 付青松 &middot; QUANT TRADING DASHBOARD</div>
</div>

<!-- ════ 看板主内容 ════ -->
<div id="dashboard-main" style="display:none;">

'''

LANDING_JS = '''
</div><!-- /dashboard-main -->

<script>
// 初始隐藏 body 滚动
document.body.style.overflow = 'hidden';
// Landing → Dashboard 过渡
function enterDashboard() {
  var landing = document.getElementById('landing');
  var main = document.getElementById('dashboard-main');
  landing.classList.add('hidden');
  main.style.display = 'block';
  document.body.style.overflow = 'auto';
}
// 实时时钟 + 秒
(function tick() {
  var el = document.getElementById('landing-time');
  if (el) {
    var now = new Date();
    el.textContent = now.toTimeString().slice(0,8);
  }
  setTimeout(tick, 1000);
})();
// 股票滚动条 - 从页面表格数据动态生成
(function initTicker() {
  var symbols = [
    {s:'AAPL',p:'218.36',c:'+1.24%',u:1},{s:'MSFT',p:'467.52',c:'-0.83%',u:0},
    {s:'NVDA',p:'138.25',c:'+3.67%',u:1},{s:'GOOGL',p:'193.17',c:'+0.45%',u:1},
    {s:'AMZN',p:'229.15',c:'-1.22%',u:0},{s:'META',p:'616.84',c:'+2.10%',u:1},
    {s:'TSLA',p:'248.72',c:'-2.55%',u:0},{s:'TSM',p:'200.39',c:'+4.18%',u:1},
    {s:'SPY',p:'593.44',c:'+0.62%',u:1},{s:'QQQ',p:'501.83',c:'+0.31%',u:1},
    {s:'IWM',p:'215.40',c:'-0.91%',u:0},{s:'DIA',p:'433.12',c:'+0.18%',u:1},
    {s:'GLW',p:'48.53',c:'+1.75%',u:1},{s:'AMD',p:'115.28',c:'-3.42%',u:0},
    {s:'INTC',p:'22.16',c:'-0.54%',u:0},{s:'BA',p:'174.35',c:'+0.88%',u:1},
  ];
  var items = symbols.map(function(s){
    return '<span class="landing-ticker-item">'+s.s+' <span class="t-'+(s.u?'up':'down')+'">'+s.p+' '+s.c+'</span></span>';
  }).join('');
  var inner = document.getElementById('landing-ticker-inner');
  if(inner) inner.innerHTML = items + items;
})();
</script>
'''

# ════════════════════════════════════════════════════
# 3. 文本替换规则
# ════════════════════════════════════════════════════
def apply_text_replacements(html):
    """应用所有命名、脱敏、颜色修复"""

    # --- 标题 ---
    html = html.replace('OKX 做T看板 + 新闻', '美股 低频量化交易看板')
    html = html.replace('OKX USDT-SWAP 做T看板', '美股 低频量化交易看板')

    # --- Tab 命名 ---
    html = html.replace('switchTab(\'dashboard\')">看板<', 'switchTab(\'dashboard\')">做多看板<')
    html = html.replace('switchTab(\'short\')">做空<', 'switchTab(\'short\')">做空看板<')

    # --- 隐藏持仓 Tab ---
    html = html.replace(
        '<button class="tab-btn" onclick="switchTab(\'positions\')">持仓</button>',
        '<!-- <button class="tab-btn" onclick="switchTab(\'positions\')">持仓</button> -->'
    )

    # --- 隐藏 Binance Tab（不合规） ---
    html = html.replace(
        '<button class="tab-btn" onclick="switchTab(\'binance\')">币安<',
        '<!-- <button class="tab-btn" onclick="switchTab(\'binance\')">币安</button> --><'
    )

    # --- OKX 脱敏（JS注释、文本等） ---
    html = html.replace('OKX返回', '')
    html = html.replace('OKX API + Binance API + config.json', '多源数据聚合')
    html = html.replace('币安', '')

    # --- 隐藏做空策略逻辑（整块 short-rule-box 内容替换为简洁标题，保留span给JS用） ---
    html = re.sub(
        r'<div class="short-rule-box">\s*<strong>做空总开关:.*?</div>',
        '<div class="short-rule-box" style="text-align:center;font-size:15px;font-weight:600">做空看板 <span id="short-regime-score" style="display:none"></span><span id="short-regime-status" style="display:none"></span></div>',
        html, flags=re.DOTALL
    )

    # --- 表头脱敏 ---
    html = html.replace('做空条件', '信号')
    html = html.replace('空头持仓', '持仓')

    # --- 布林线颜色: 暗色看不清 → 亮蓝 ---
    html = html.replace(
        "d.boll_pct > 0.8 ? '#BA7517' : '#2c2c2a'",
        "d.boll_pct > 0.8 ? '#d2991d' : '#58a6ff'"
    )
    html = html.replace(
        "d.boll_pct_w > 0.8 ? '#BA7517' : '#2c2c2a'",
        "d.boll_pct_w > 0.8 ? '#d2991d' : '#58a6ff'"
    )
    # null 值颜色
    html = html.replace("=== null ? '#aaa'", "=== null ? '#8b949e'")

    # --- 杠杆徽章: '4/7/10x' 字符串 → 独立徽章 ---
    old_lv = "const _lv = (d.vol || 0) > 0.10 ? '3/5/7x' : (d.vol || 0) > 0.075 ? '4/6/9x' : '4/7/10x';"
    new_lv = """const _lvMap = (d.vol || 0) > 0.10 ? [3,5,7] : (d.vol || 0) > 0.075 ? [4,6,9] : [4,7,10];
  const _lvHtml = _lvMap.map(function(v){ var cls = v>=10 ? 'lev-badge-10' : v>=7 ? 'lev-badge-7' : 'lev-badge'; return '<span class=\"'+cls+'\">'+v+'x</span>'; }).join('');"""
    html = html.replace(old_lv, new_lv)
    html = html.replace(
        "const leverText = (d.eligible || d.has_pos) ? _lv : '-';",
        "const leverText = (d.eligible || d.has_pos) ? _lvHtml : '-';"
    )

    # --- JS 注释脱敏 ---
    html = html.replace(
        "  // 建议杠杆: 跟随策略阶梯杠杆, 按周波动率缩放 (与 strategy_v4._lev_tier_map 一致)\n",
        ""
    )
    html = html.replace(
        "  //   vol≤7.5%: 4/7/10x | 7.5-10%: 4/6/9x | >10%: 3/5/7x (超跌模式 4/5/6x 此处不单独区分)\n",
        ""
    )
    html = html.replace(
        "// 做空档位分位(与 strategy_v4 常量一致): TP2=45% TP1=55% S1=75% S2=81% S3=87%",
        "// 做空档位分位: TP2=45% TP1=55% S1=75% S2=81% S3=87%"
    )
    html = html.replace(
        "// 做空表格行",
        "// 做空看板表格行"
    )
    html = html.replace(
        "// 指数行(QQQ/SPY)无做空字段 → 只显示基础行情",
        "// 指数行无做空字段"
    )
    html = html.replace(
        "// 做空条件: 市场 + 新闻",
        "// 信号条件"
    )
    html = html.replace(
        "// 建议杠杆: 与策略做空表一致(3/5/7x)",
        "// 建议杠杆"
    )

    # --- 变量名脱敏 ---
    html = html.replace('SHORT_MAX', 'SHORT_THRESH')
    html = html.replace('SHORT_LEV_STR', 'SHORT_LEV')
    html = html.replace(
        "// ===== 做空子页 (Short tab) =====",
        "// ===== 做空看板 ====="
    )

    # --- 周布林阈值说明删除 ---
    html = re.sub(r'\(≥0\.5 做多 / <0\.5 做空\)', '', html)

    # --- 白色背景 → 深色 ---
    html = html.replace('background:#fff;border-radius:12px;border:1px solid #e5e5e5', 'background:#161b22;border-radius:12px;border:1px solid #30363d')
    html = html.replace('background:#fff8e1', 'background:rgba(210,153,29,0.08);border-color:rgba(210,153,29,0.2)')
    html = html.replace('background:#fafaf9', 'background:rgba(255,255,255,0.05)')

    # --- 新闻/日历标题加粗 ---
    html = html.replace(
        'font-size:14px;font-weight:500">新闻影响分析',
        'font-size:15px;font-weight:700">新闻影响分析'
    )
    html = html.replace(
        'font-size:14px;font-weight:500">财报 / FOMC / 经济数据日历',
        'font-size:15px;font-weight:700">财报 / FOMC / 经济数据日历'
    )
    # Binance 相关内容已通过全局替换移除（Tab 已隐藏）

    # --- 灰色文字适配深色 ---
    html = html.replace('color:#888', 'color:#8b949e')
    html = html.replace('color:#aaa', 'color:#8b949e')
    html = html.replace('color:#999', 'color:#8b949e')
    html = html.replace('color:#777', 'color:#8b949e')
    html = html.replace('color:#555', 'color:#8b949e')
    html = html.replace('color:#666', 'color:#8b949e')

    # --- 数据来源行脱敏 ---
    html = html.replace('双击update_all.bat刷新数据', '自动刷新')

    return html


# ════════════════════════════════════════════════════
# 4. 主转换函数
# ════════════════════════════════════════════════════
def transform(input_path, output_path):
    with open(input_path, 'r', encoding='utf-8') as f:
        html = f.read()

    # 4a. 替换 CSS
    style_start = html.index('<style>') + len('<style>')
    style_end = html.index('</style>')
    html = html[:style_start] + '\n' + DARK_CSS + '\n' + html[style_end:]

    # 4b. 替换 </style></head><body> → </style></head><body>\n[LANDING_HTML]
    html = html.replace(
        '</style>\n</head>\n<body>\n\n<div class="header">',
        '</style>\n</head>\n<body>\n\n' + LANDING_HTML + '<div class="header">'
    )

    # 4c. 替换 </script>\n</body>\n</html> → [LANDING_JS]</body>\n</html>
    html = html.replace(
        '</script>\n</body>\n</html>',
        '</script>\n' + LANDING_JS + '</body>\n</html>'
    )

    # 4d. 应用文本替换
    html = apply_text_replacements(html)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f'✅ 转换完成: {input_path}')
    print(f'   输出: {output_path}')
    print(f'   大小: {len(html):,} bytes')


if __name__ == '__main__':
    inp = sys.argv[1] if len(sys.argv) > 1 else INPUT_DEFAULT
    out = sys.argv[2] if len(sys.argv) > 2 else OUTPUT_DEFAULT
    transform(inp, out)
