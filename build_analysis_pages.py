# -*- coding: utf-8 -*-
"""渲染六步分析 .md → 美观静态 HTML 子页面; 生成 analysis_index.json; 提取财务指标回填 config.json.

- 输入: base = WORKSPACE_ROOT; analysis dir = base/analysis/港美股/{美股,港股,A股}
- 输出:
  - console_root/deploy/analysis/{sym}.html  (默认为 WORKSPACE_ROOT 外的 deploy, 可 --outdir 覆盖)
  - {outdir}/../analysis_index.json          (供 dashboard.py 判 has_analysis)
  - 提取的财务指标写回 config.json (m1/m2/m3), 仅当分析文件能解析出数值
命令行:
  python build_analysis_pages.py [--outdir PATH] [--write-config]
"""
import os, sys, re, json, glob, math, hashlib, argparse, html as htmllib

BASE = os.environ.get("WORKSPACE_ROOT", r"C:\Users\15949\WorkBuddy\xiaocai")
ANALYSIS_ROOT = os.path.join(BASE, "analysis", "港美股")
CFG = os.path.join(BASE, "watchlist_us", "config.json")
# 云端: 输出到仓库内 analysis/(随 git add 提交); 本地: 输出到同级 deploy 仓库 analysis/
if os.environ.get("CLOUD_MODE") == "1":
    DEFAULT_OUT = os.path.join(BASE, "analysis")
else:
    DEFAULT_OUT = os.path.join(os.path.dirname(BASE), "2026-08-04-21-02-03", "deploy", "analysis")

PURE_A = {"300308.SZ","603986.SS","688836.SS","688825.SS","601138.SS","600519.SS",
          "600309.SS","600031.SS","300274.SZ","600406.SS","300124.SZ"}

# ---------- 轻量 Markdown → HTML 渲染 ----------
def inline(tx):
    tx = htmllib.escape(tx)
    # 粗体 **x** → <b>
    tx = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', tx)
    # 斜体 *x*
    tx = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<i>\1</i>', tx)
    # 行内代码 `x`
    tx = re.sub(r'`([^`]+)`', r'<code>\1</code>', tx)
    # 链接
    tx = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" target="_blank">\1</a>', tx)
    return tx

def md_line(tx, in_list, in_table):
    """处理单行。返回 (html, list_type) ; list_type 为 'ul'/'ol'/'' 表示当前所处列表类型。
    空白行: 保持 in_list(列表可能跨空行继续); 遇到 非列表行/换列表类型 由 render_md 负责闭合。"""
    s = tx.strip()
    if not s:
        return '', in_list
    if s.startswith(('```', '~~~')):
        return '<pre>' + htmllib.escape(s[3:]) + '</pre>', in_list
    # 标题
    m = re.match(r'^(#{1,6})\s+(.*)$', s)
    if m:
        lvl = len(m.group(1))
        return f'<h{lvl + 1}>{inline(m.group(2))}</h{lvl + 1}>', ''
    # 分隔线
    if re.match(r'^-{3,}$', s) or re.match(r'^\*{3,}$', s):
        return '<hr>', ''
    # 无序列表
    if re.match(r'^[-*+]\s+', s):
        out = ''
        if in_list != 'ul':
            if in_list:
                out = '</' + in_list + '>'
            out += '<ul>'
        out += '<li>' + inline(re.sub(r'^[-*+]\s+', '', s)) + '</li>'
        return out, 'ul'
    # 有序列表
    if re.match(r'^\d+\.\s+', s):
        out = ''
        if in_list != 'ol':
            if in_list:
                out = '</' + in_list + '>'
            out += '<ol>'
        out += '<li>' + inline(re.sub(r'^\d+\.\s+', '', s)) + '</li>'
        return out, 'ol'
    # 表格
    if s.startswith('|') and '|' in s[1:]:
        cells = [c.strip() for c in s.strip('|').split('|')]
        if all(re.fullmatch(r':?-{2,}:?', c) for c in cells):
            return '', ''  # 分隔行
        if in_table:
            return '<tr>' + ''.join(f'<td>{inline(c)}</td>' for c in cells) + '</tr>', ''
        return '<div class="tblwrap"><table><tr>' + ''.join(f'<th>{inline(c)}</th>' for c in cells) + '</tr>', ''
    # 普通段落
    return '<p>' + inline(s) + '</p>', ''

def render_md(text):
    title = ''
    m = re.search(r'^#\s+(.+)$', text, re.M)
    if m:
        title = m.group(1).strip().replace('六步分析报告', '').strip('（）() ')
    body = []
    in_list = ''
    in_table = False
    for ln in text.splitlines():
        stripped = ln.strip()
        h, new_list = md_line(ln, in_list, in_table)
        # 表格闭合: 遇到非表格内容关闭
        if in_table and not stripped.startswith('|'):
            body.append('</table></div>')
            in_table = False
        # 列表闭合: 上一元素仍处列表, 而新行为非列表(标题/段落/表格/分割线/代码) → 先补闭合
        if in_list and new_list == '' and stripped != '' and not stripped.startswith(('```', '~~~')):
            body.append('</' + in_list + '>')
            in_list = ''
        body.append(h)
        in_list = new_list
        if stripped.startswith('|') and '|' in stripped[1:]:
            cells = [c.strip() for c in stripped.strip('|').split('|')]
            if not all(re.fullmatch(r':?-{2,}:?', c) for c in cells):
                in_table = True
    # 文件末尾残留列表/表格补闭合
    if in_list:
        body.append('</' + in_list + '>')
    if in_table:
        body.append('</table></div>')
    return title, '\n'.join(body)

PAGE_CSS = '''
:root{--bg:#12151b;--card:#1a1f2a;--card2:#20263;->;--txt:#e6e8ef}*/
*{box-sizing:border-box;margin:0;padding:0}
body{background:#12151b;color:#e6e8ef;font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;line-height:1.7;padding:24px 16px 80px;text-align:left}
.wrap{max-width:960px;margin:0 auto;text-align:left}
.toolbar{position:sticky;top:0;background:rgba(18,21,27,.92);backdrop-filter:blur(6px);padding:10px 0;margin-bottom:20px;z-index:10;border-bottom:1px solid #2a2f3c;text-align:left;display:flex;justify-content:space-between;align-items:center;gap:10px}
.back{display:inline-block;color:#8ab4f8;text-decoration:none;font-size:14px;padding:6px 14px;border:1px solid #334;border-radius:8px;transition:.2s}
.back:hover{background:#1f2633}
.shot{background:#1f2633;color:#e6e8ef;border:1px solid #334;border-radius:8px;padding:6px 14px;font-size:14px;cursor:pointer;transition:.2s;font-family:inherit}
.shot:hover{background:#2a3242}
h1{font-size:26px;font-weight:700;color:#fff;margin:14px 0 6px;text-align:left}
h2{font-size:20px;color:#8ab4f8;margin:26px 0 12px;padding-left:10px;border-left:4px solid #8ab4f8;text-align:left}
h3{font-size:16px;color:#e6e8ef;margin:18px 0 8px;text-align:left}
h4{font-size:14px;color:#aab3c5;margin:14px 0 6px;text-align:left}
p{margin:8px 0;color:#c7ccd8;text-align:left}
hr{border:none;border-top:1px solid #2a2f3c;margin:22px 0}
b,strong{color:#fff}
ul,ol{margin:8px 0 8px 22px;color:#c7ccd8;text-align:left}
ul ul, ul ol, ol ul, ol ol{margin-left:12px}
li{margin:4px 0;text-align:left}
td,th{text-align:left}
.tblwrap{max-width:100%;overflow-x:auto}
table{width:100%;border-collapse:collapse;margin:14px 0;background:#1a1f2a;border-radius:10px;overflow:hidden;table-layout:auto}
th{background:#232a3a;color:#fff;font-weight:600;padding:10px 12px;font-size:13px}
td{padding:9px 12px;border-top:1px solid #262d3d;color:#c7ccd8;font-size:13px;white-space:normal;word-break:break-word}
tr:nth-child(even){background:#1e2532}
code{background:#222a3a;color:#f0b06a;padding:2px 6px;border-radius:4px;font-size:12px}
pre{background:#0d1014;padding:14px;border-radius:8px;overflow-x:auto;margin:10px 0;color:#aee}
.meta{color:#8892a6;font-size:12px;margin-bottom:8px}
a{color:#8ab4f8}
.hook{color:#aab3c5;font-size:13.5px;border-left:3px solid #3b4a6b;padding:6px 12px;margin:10px 0;background:#171c26;border-radius:0 8px 8px 0}
.badge{display:inline-block;padding:5px 16px;border-radius:999px;font-size:15px;font-weight:700;letter-spacing:1px}
.badge-detail{color:#8892a6;font-size:13px;margin-left:8px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:10px;margin:14px 0}
.card{background:#1a1f2a;border:1px solid #262d3d;border-radius:10px;padding:10px 12px}
.card .k{color:#8892a6;font-size:12px}
.card .v{color:#fff;font-size:17px;font-weight:600;margin-top:3px;white-space:nowrap}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px;margin:6px 0 4px}
.chart-card{background:#1a1f2a;border:1px solid #262d3d;border-radius:10px;padding:12px 14px}
.ctitle{color:#aab3c5;font-size:13px;margin-bottom:8px}
svg.chart{width:100%;height:auto;display:block}
details.sec{background:#1a1f2a;border:1px solid #262d3d;border-radius:10px;margin:10px 0;padding:0 16px}
details.sec summary{cursor:pointer;padding:13px 0;color:#e6e8ef;font-size:15px;font-weight:600;list-style:none}
details.sec summary::-webkit-details-marker{display:none}
details.sec summary::before{content:'▸ ';color:#8ab4f8}
details.sec[open] summary::before{content:'▾ '}
details.sec[open]{padding-bottom:6px}
.tl{background:linear-gradient(135deg,#1c2536,#1a1f2a);border:1px solid #2b3a55;border-left:4px solid #8ab4f8;border-radius:10px;padding:13px 16px;margin:16px 0;color:#dbe1ee;font-size:14px}
'''.replace('->;', '')
# 修正上面的临时占位
PAGE_CSS = PAGE_CSS.replace('--card2:#20263;->;--txt:#e6e8ef}*/', '--card2:#20263a;--txt:#e6e8ef}')

SHARE_JS = '''
async function loadH2C(){
  if (window.html2canvas) return window.html2canvas;
  const urls = ['https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js',
                'https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js',
                'https://unpkg.com/html2canvas@1.4.1/dist/html2canvas.min.js'];
  for (const u of urls) {
    try {
      await new Promise((res, rej) => { const s = document.createElement('script'); s.src = u; s.onload = res; s.onerror = rej; document.head.appendChild(s); });
      if (window.html2canvas) return window.html2canvas;
    } catch(e) {}
  }
  throw new Error('截图库加载失败(检查网络)');
}
async function shareShot(btn){
  const orig = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '生成中…';
  try {
    const h2c = await loadH2C();
    const target = document.querySelector('.wrap') || document.body;
    const bg = getComputedStyle(document.body).backgroundColor || '#12151b';
    const canvas = await h2c(target, { backgroundColor: bg, useCORS: true, scale: Math.min(2, window.devicePixelRatio || 1) });
    const blob = await new Promise(res => canvas.toBlob(res, 'image/png'));
    const fname = 'analysis_' + (location.pathname.split('/').pop() || 'report').replace('.html','') + '_' + new Date().toISOString().slice(0,10) + '.png';
    const file = new File([blob], fname, { type: 'image/png' });
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      await navigator.share({ files: [file], title: document.title });
    } else {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = fname; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 8000);
    }
    btn.innerHTML = '✓ 已生成';
  } catch(e) {
    btn.innerHTML = '✗ 失败';
    alert('截图失败: ' + (e.message || e));
  }
  setTimeout(() => { btn.innerHTML = orig; btn.disabled = false; }, 1500);
}
'''

def make_page(sym, name, html_body, tag='', back_tab='dashboard'):
    return f'''<!DOCTYPE html>
<!-- build:{RENDER_VER}|{tag} -->
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{htmllib.escape(sym)} {htmllib.escape(name)} 六步分析</title>
<style>{PAGE_CSS}</style></head>
<body><div class="wrap">
<div class="toolbar"><a class="back" href="../index.html#{back_tab}" onclick="if (history.length > 1) {{ history.back(); return false; }}">&larr; 返回看板</a><button class="shot" onclick="shareShot(this)">📸 截图分享</button></div>
<h1>{htmllib.escape(name)} <span style="color:#8892a6;font-size:16px">{htmllib.escape(sym)}</span></h1>
{html_body}
<script>{SHARE_JS}</script>
</div></body></html>'''

# ---------- 财务指标提取 (最佳努力, 返回 (m1, m2, m3)) ----------
def extract_metrics(text):
    """返回 (m1, m2, m3). m1/m3 为带单位的字符串, m2 为数值(同比%, 可负)供红绿着色. 提取不到传 None."""
    roe, yoy, meta = None, None, None

    # ---- m1 ROE: 优先表格里加权/扣非加权/平均ROE, 取其后紧跟的 % 数值 ----
    m = re.search(r'(?:扣非加权|扣非|加权|平均|核心)?\s*ROE[^%\d\n]{0,12}?([\d.]+)\s*%(?:\s*\()?', text)
    if not m:
        m = re.search(r'ROIC[^%\d\n]{0,12}?([\d.]+)\s*%', text)
    if m:
        roe = m.group(1) + '%'

    # ---- m2 净利/EPS 同比: 保留正负号, 排除年数(>500) ----
    # 优先 扣非归母净利/归母净利/核心净利 的最近一个带符号 % 
    m2 = None
    m = re.search(r'(?:扣非归母净利润|扣非归母净利|归母净利润|归母净利|核心净利润|核心净利|净利润)\s*(?:\(Q\d\))?[^\n%]{0,18}?([-+]?[\d.]+)%', text)
    if m:
        v = float(m.group(1))
        if abs(v) <= 300:
            m2 = v
    if m2 is None:
        m = re.search(r'(?:EPS|摊薄EPS|每股收益)[^\n%]{0,18}?([-+]?[\d.]+)%', text)
        if m:
            v = float(m.group(1))
            if abs(v) <= 300:
                m2 = v
    yoy = m2

    # ---- m3 净现比 / FCF利润率: 净现比直接取比值(0.x~5), 排除年份 ----
    m = re.search(r'净现比[^=\d]*?[=≈]\s*([\d.]+)', text)
    if not m:
        m = re.search(r'净现比[^=\d]{0,16}?([12]?[.][\d]+)', text) or re.search(r'净现比[^=\d]{0,16}?([\d.]+)', text)
    if m:
        v = float(m.group(1))
        if 0 <= v <= 20 and not (v > 900):
            meta = f'{v:.2f}'
    if meta is None:
        m = re.search(r'(?:FCF利润率|自由现金流利润|FCF Margin|FCF margin|FCF)[^\n%]{0,12}?([-+]?[\d.]+)\s*%', text)
        if m:
            meta = m.group(1) + '%'
    if meta is None:
        m = re.search(r'经营现金流[^/]*?/[/]?\s*[^\d]{0,10}?([\d.]+)', text)
        if m and float(m.group(1)) <= 20:
            meta = f'{float(m.group(1)):.2f}'

    return roe, yoy, meta


# ============================================================
# 图表化改造 (v2-charts): md 数据提取 + 构建时 SVG 图表 + 折叠排版
# ============================================================
RENDER_VER = 'v2-charts12'

def _num(s):
    try:
        return float(str(s).replace(',', ''))
    except Exception:
        return None

def _fmtv(v):
    if v is None:
        return '-'
    return f'{v:,.10g}'

def split_sections(text):
    """按 '^## ' 切分为 [(title, body)]; 首元素为第一个 ## 之前的头部."""
    parts = re.split(r'(?m)^##\s+', text)
    secs = [('', parts[0])]
    for p in parts[1:]:
        nl = p.find('\n')
        title = (p[:nl] if nl >= 0 else p).strip()
        secs.append((title, p[nl + 1:] if nl >= 0 else ''))
    return secs

def extract_tldr(text):
    """提取并移除 '**一句话结论：** ...' 段落. 返回 (tldr, new_text)."""
    m = re.search(r'\*\*一句话结论[：:]\*\*\s*(.+?)(?=\n\s*\n|\n-{3,}|$)', text, re.S)
    if not m:
        return None, text
    t = m.group(1).strip().rstrip('*').strip()
    return (t or None), text[:m.start()] + text[m.end():]

def extract_hook(text):
    """提取并移除 '**核心问题...：** ...' 行. 返回 (hook, new_text)."""
    m = re.search(r'^(?:>\s*)?\*\*核心问题.*$', text, re.M)
    if not m:
        return None, text
    s = m.group(0).lstrip('> ').strip()
    s2 = re.sub(r'^\*\*核心问题[^：:]*[：:]\s*\*\*\s*', '', s)
    if s2 == s:
        s2 = re.sub(r'^\*\*核心问题[^：:]*[：:]\s*', '', s)
    s = s2.strip().rstrip('*').strip()
    new_text = text[:m.start()] + text[m.end():]
    return (s or None), new_text

def extract_meta_date(text):
    m = re.search(r'(\d{4}年\d{1,2}月\d{1,2}日)', text)
    return m.group(1) if m else None

def extract_rating(text):
    """综合评级 → (主评级, 补充说明)."""
    m = re.search(r'综合评级[：:]\s*\**\s*([^\n*]{2,60})', text)
    if not m:
        return None, None
    s = m.group(1).strip().rstrip('。*').strip()
    mm = re.match(r'^([^（(]{1,10})[（(](.*)$', s, re.S)
    if mm:
        return mm.group(1).strip(), mm.group(2).strip().rstrip('）)').strip()
    return s[:12], None

def rating_kind(main):
    if any(k in main for k in ('买入', '增持', '加仓')):
        return 'pos'
    if any(k in main for k in ('减持', '卖出', '回避')):
        return 'neg'
    return 'hold'

def extract_header_metrics(text):
    """头部指标 best-effort → dict(px, pe, pb, div, mcap, cur). 货币符号优先从价格行自身判定."""
    head = text.split('\n## ')[0] if '\n## ' in text else text
    out = {}
    cur = ''
    # 价格行: 捕获货币前缀与单位后缀, 从行本身判定币种 (避免头部"H股参考 XX港元"等对比文字误触发)
    m = re.search(r'(?:当前股价|收盘价|现价|股价)\s*\**\s*[：:]\s*\**\s*(?:≈)?((?:US)?(?:HK)?[¥￥₩\$]?)\s*([\d,]+(?:\.\d+)?)(元|港元)?', head)
    if m:
        out['px'] = _num(m.group(2))
        pre, unit = m.group(1) or '', m.group(3) or ''
        if 'HK' in pre or unit == '港元':
            cur = 'HK$'
        elif '¥' in pre or '￥' in pre or unit == '元':
            cur = '¥'
        elif '₩' in pre:
            cur = '₩'
        elif '$' in pre:
            cur = '$'
    if not cur:
        # 兜底: 全文启发 (仅当价格行无线索时)
        if re.search(r'HK\$', head):
            cur = 'HK$'
        elif '₩' in head:
            cur = '₩'
        elif '¥' in head or '￥' in head:
            cur = '¥'
        elif '$' in head:
            cur = '$'
        elif '元' in head:
            cur = '¥'
    out['cur'] = cur
    m = re.search(r'PE[\s(（]*TTM[\s)）]*[^%\d\n-]{0,10}([\d.]+)', head)
    if m:
        out['pe'] = m.group(1)
    m = re.search(r'PB[^\d\n%-]{0,10}([\d.]+)', head)
    if m:
        out['pb'] = m.group(1)
    m = re.search(r'股息率[^\d\n%]{0,10}([\d.]+)(?:\s*%)?', head)
    if m:
        out['div'] = m.group(1)
    m = re.search(r'(?:总)?市值\s*\**\s*[：:]\s*\**\s*(?:≈)?\s*(?:US)?(?:HK)?[¥￥₩\$]?\s*([\d,.]+)\s*(万亿|亿)', head)
    if m:
        out['mcap'] = f'{_fmtv(_num(m.group(1)))}{m.group(2)}'
    return out

def _radar_rows(seg):
    out = []
    for ln in seg.splitlines():
        m = re.match(r'^\|\s*([^|\-\s][^|]*?)\s*\|\s*\*{0,2}(\d(?:\.\d)?)\*{0,2}\s*\|', ln)
        if m:
            d = m.group(1).strip().strip('*').strip()
            v = float(m.group(2))
            if d not in ('维度', '项目', '轴') and 0 < v <= 5 and len(out) < 6 and all(d != o[0] for o in out):
                out.append((d, v))
    return out

def extract_radar(text):
    """六维雷达评分表 → [(维度, 0-5分)]; 无则 []."""
    for _t, body in split_sections(text):
        if '六维' not in body and '雷达' not in body:
            continue
        rows = _radar_rows(body)
        if rows:
            return rows
    return []

def _dcf_rows(seg):
    out = []
    for ln in seg.splitlines():
        if not ln.strip().startswith('|'):
            continue
        cells = [c.strip() for c in ln.strip().strip('|').split('|')]
        if len(cells) < 3 or not cells[0]:
            continue
        c0 = cells[0].replace('*', '')
        scen = next((k for k in ('悲观', '中性', '乐观') if k in c0), None)
        if not scen or any(scen == s for s, _, _ in out):
            continue
        val = pct = None
        for c in cells[1:]:
            cc = c.replace('*', '').strip()
            if re.fullmatch(r'[-+]\d+(?:\.\d+)?%', cc):
                pct = cc  # 较现价%惯例在右侧, 取最后一个匹配
                continue
            if '%' not in cc and '→' not in cc:
                mm = (re.fullmatch(r'[≈约~]?\s*(?:US)?(?:HK)?[¥￥₩\$]?\s*([\d,]+(?:\.\d+)?)\s*(?:元|港元|美元)?', cc)
                      or re.fullmatch(r'[≈约~]?\s*(?:US)?(?:HK)?[¥￥₩\$]?\s*([\d,]+(?:\.\d+)?)\s*[–~-]\s*[\d,]+(?:\.\d+)?\s*(?:元|港元|美元)?', cc)
                      or re.fullmatch(r'[≈约~]?\s*(?:US)?(?:HK)?[¥￥₩\$]?\s*([\d,]+(?:\.\d+)?)\s*(?:元|港元|美元)?\s*[（(]\$[\d,]+(?:\.\d+)?[）)]', cc))
                if mm:
                    v = _num(mm.group(1))
                    if v and 0.5 < v < 1000000:
                        val = v  # 每股价值列惯例在右侧, 取最后一个纯数值单元格(区间取下限)
        if val is not None:
            out.append((scen, val, pct))
        if len(out) >= 3:
            break
    return out

def _dcf_rows_wide(text):
    """横向参数表 fallback: | 参数 | 悲观 | 中性 | 乐观 | ... | 每股价值 | v1 | v2 | v3 |"""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s.startswith('|') or '每股' not in s:
            continue
        # 向上找最近的表头行(含三情景关键词)
        hdr = None
        for j in range(i - 1, max(i - 12, -1), -1):
            sj = lines[j].strip()
            if sj.startswith('|') and ('悲观' in sj or '中性' in sj or '乐观' in sj):
                hdr = sj
                break
            if not sj.startswith('|'):
                break
        if not hdr:
            continue
        hcells = [c.replace('*', '').strip() for c in hdr.strip().strip('|').split('|')]
        order = {}
        for k, hc in enumerate(hcells):
            for scen in ('悲观', '中性', '乐观'):
                if scen in hc and scen not in order:
                    order[scen] = k
        if len(order) < 3:
            continue
        vcells = [c.replace('*', '').strip() for c in s.strip().strip('|').split('|')]
        out = []
        for scen in ('悲观', '中性', '乐观'):
            k = order[scen]
            if k >= len(vcells):
                continue
            cc = vcells[k]
            mm = (re.fullmatch(r'[≈约~]?\s*(?:US)?(?:HK)?[¥￥₩\$]?\s*([\d,]+(?:\.\d+)?)\s*(?:元|港元|美元)?', cc)
                  or re.fullmatch(r'[≈约~]?\s*(?:US)?(?:HK)?[¥￥₩\$]?\s*([\d,]+(?:\.\d+)?)\s*[–~-]\s*[\d,]+(?:\.\d+)?\s*(?:元|港元|美元)?', cc))
            if mm:
                v = _num(mm.group(1))
                if v and 0.5 < v < 1000000:
                    out.append((scen, v, None))
        if len(out) >= 2:
            return out
    return []

def extract_dcf(text):
    """DCF三情景表 → [(悲观/中性/乐观, 每股价值, 较现价%)]; 无则 []."""
    cands = []
    for m in re.finditer(r'DCF[^\n]{0,24}情景', text):
        cands.append(m.start())
    for m in re.finditer(r'三情景', text):
        cands.append(m.start())
    # 表头行定位(每股价值/每股估值/每股内在价值 所在表头)
    for m in re.finditer(r'^\|[^\n]*(?:每股价值|每股估值|每股内在价值)[^\n]*\|', text, re.M):
        cands.append(m.start())
    for start in sorted(set(cands)):
        seg = text[start:start + 4000].split('\n## ')[0]
        rows = _dcf_rows(seg)
        if len(rows) >= 2:
            return rows
    return _dcf_rows_wide(text)

def extract_trend(text):
    """近5年趋势 → [(年份, 营收亿, 净利亿)]; 无则 []. 表格优先, A股叙述格式兜底."""
    m = re.search(r'近[5五]年趋势', text)
    if not m:
        return []
    seg = text[m.start():m.start() + 3000]
    seg = re.split(r'\n#{2,3}\s', seg)[0]

    def cellnum(c):
        mm = re.search(r'[≈约]?\s*(?:US)?(?:HK)?[¥￥₩\$]?\s*([\d,]+(?:\.\d+)?)\s*(万亿|亿)?', c)
        if not mm:
            return None
        v = _num(mm.group(1))
        if not v:
            return None
        return v * (10000 if mm.group(2) == '万亿' else 1)

    rows = []
    for ln in seg.splitlines():
        if not ln.strip().startswith('|'):
            continue
        cells = [c.strip() for c in ln.strip().strip('|').split('|')]
        if len(cells) < 3:
            continue
        m0 = re.search(r'(?:FY)?(\d{4}|\d{2})(E?)', cells[0])
        if not m0:
            continue
        y = m0.group(1)
        if len(y) == 2:
            y = '20' + y
        y += m0.group(2)
        r1, r2 = cellnum(cells[1]), cellnum(cells[2])
        if r1 and r2 and y not in [r[0] for r in rows]:
            rows.append((y, r1, r2))
    if len(rows) >= 3:
        return rows
    rows = []
    for m2 in re.finditer(r'(20\d{2})年\s*([\d,]+(?:\.\d+)?)\s*亿[^；\n/]{0,30}/\s*([\d,]+(?:\.\d+)?)\s*亿', seg):
        y, r1, r2 = m2.group(1), _num(m2.group(2)), _num(m2.group(3))
        if r1 and r2 and y not in [r[0] for r in rows]:
            rows.append((y, r1, r2))
    return rows if len(rows) >= 3 else []


# ---------- SVG 生成 (暗色主题, viewBox 自适应) ----------
def _svg_open(w, h):
    return f'<svg class="chart" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" role="img">'

def svg_radar(scores):
    n = max(len(scores), 3)
    cx, cy, R = 160, 152, 94
    angs = [-math.pi / 2 + i * 2 * math.pi / n for i in range(n)]

    def pt(a, r):
        return f'{cx + r * math.cos(a):.1f},{cy + r * math.sin(a):.1f}'

    rings = ''.join(
        f'<polygon points="{" ".join(pt(a, R * k / 5) for a in angs)}" fill="none" stroke="#262d3d" stroke-width="1"/>'
        for k in range(1, 6))
    axes = ''.join(
        f'<line x1="{cx}" y1="{cy}" x2="{cx + R * math.cos(a):.1f}" y2="{cy + R * math.sin(a):.1f}" stroke="#262d3d" stroke-width="1"/>'
        for a in angs)
    dp = ' '.join(pt(angs[i], R * min(scores[i][1], 5) / 5) for i in range(len(scores)))
    dots = ''.join(
        f'<circle cx="{cx + R * min(scores[i][1], 5) / 5 * math.cos(angs[i]):.1f}" cy="{cy + R * min(scores[i][1], 5) / 5 * math.sin(angs[i]):.1f}" r="2.5" fill="#8ab4f8"/>'
        for i in range(len(scores)))
    labels = []
    for i, (d, v) in enumerate(scores):
        a = angs[i]
        lx, ly = cx + (R + 12) * math.cos(a), cy + (R + 12) * math.sin(a)
        anchor = 'middle' if abs(math.cos(a)) < 0.35 else ('start' if math.cos(a) > 0 else 'end')
        dy = 4 + (6 if math.sin(a) > 0.5 else (-6 if math.sin(a) < -0.5 else 0))
        labels.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" dy="{dy}" font-size="12" fill="#aab3c5">{htmllib.escape(d)}'
            f'<tspan x="{lx:.1f}" dy="14" fill="#8ab4f8" font-weight="600">{v:g}</tspan></text>')
    return (_svg_open(320, 300).replace('viewBox="0 0 320 300"', 'viewBox="-55 0 375 300"') + rings + axes
            + f'<polygon points="{dp}" fill="rgba(138,180,248,.22)" stroke="#8ab4f8" stroke-width="1.5"/>'
            + dots + ''.join(labels) + '</svg>')

def svg_dcf_bars(dcf, px, cur):
    W = 470
    rowh = 50
    H = rowh * len(dcf) + 42
    x0 = 58
    bw = W - x0 - 80
    hi = max([v for _, v, _ in dcf] + ([px] if px else [])) * 1.08

    def X(v):
        return x0 + bw * v / hi

    rows = []
    for i, (nm, v, pct) in enumerate(dcf):
        y = 34 + i * rowh
        color = '#3fb950'
        if px is not None:
            color = '#f85149' if v > px * 1.02 else ('#e3b341' if v > px * 0.98 else '#3fb950')
        elif pct and pct.startswith('+'):
            color = '#f85149'
        rows.append(
            f'<text x="{x0 - 8}" y="{y + 16}" text-anchor="end" font-size="12.5" fill="#c7ccd8">{nm}</text>'
            f'<rect x="{x0}" y="{y}" width="{X(v) - x0:.1f}" height="22" rx="4" fill="{color}" opacity="0.75"/>'
            f'<text x="{X(v) + 6:.1f}" y="{y + 15}" font-size="12" fill="{color}">{cur}{_fmtv(v)}</text>'
            + (f'<text x="{x0 - 8}" y="{y + 32}" text-anchor="end" font-size="10.5" fill="#8892a6">{pct}</text>' if pct else ''))
    pxline = ''
    if px:
        xx = X(px)
        anchor = 'end' if xx > W - 96 else 'middle'
        tx = xx - 4 if anchor == 'end' else xx
        pxline = (f'<line x1="{xx:.1f}" y1="26" x2="{xx:.1f}" y2="{H - 8}" stroke="#e3b341" stroke-width="1.2" stroke-dasharray="4 3"/>'
                  f'<text x="{tx:.1f}" y="18" text-anchor="{anchor}" font-size="11" fill="#e3b341">现价 {cur}{_fmtv(px)}</text>')
    return _svg_open(W, H) + ''.join(rows) + pxline + '</svg>'

def svg_price_ladder(px, buy, sell, dcf, cur):
    vals = [buy, sell] + [v for _, v, _ in dcf] + ([px] if px else [])
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.05 or 1
    lo, hi = lo - pad, hi + pad
    W, H = 470, 128
    x0, x1 = 18, W - 18
    bw = x1 - x0

    def X(v):
        return x0 + bw * (v - lo) / (hi - lo)

    zones = (f'<rect x="{x0}" y="34" width="{X(buy) - x0:.1f}" height="30" fill="rgba(63,185,80,.16)"/>'
             f'<rect x="{X(buy):.1f}" y="34" width="{X(sell) - X(buy):.1f}" height="30" fill="rgba(139,147,167,.10)"/>'
             f'<rect x="{X(sell):.1f}" y="34" width="{x1 - X(sell):.1f}" height="30" fill="rgba(248,81,73,.16)"/>'
             f'<line x1="{x0}" y1="34" x2="{x1}" y2="34" stroke="#262d3d"/>'
             f'<line x1="{x0}" y1="64" x2="{x1}" y2="64" stroke="#262d3d"/>')

    def zone_cap(a, b, txt):
        if X(b) - X(a) > 52:
            return f'<text x="{(X(a) + X(b)) / 2:.1f}" y="53" text-anchor="middle" font-size="11" fill="#8892a6">{txt}</text>'
        return ''

    caps = zone_cap(lo, buy, '买入区') + zone_cap(buy, sell, '持有区') + zone_cap(sell, hi, '减仓区')

    def tick(v, name, color, ty):
        return (f'<line x1="{X(v):.1f}" y1="30" x2="{X(v):.1f}" y2="70" stroke="{color}" stroke-width="1.4"/>'
                f'<text x="{X(v):.1f}" y="{ty}" text-anchor="middle" font-size="11" fill="{color}">{name} {cur}{_fmtv(v)}</text>')

    marks = tick(buy, '买入', '#3fb950', 24) + tick(sell, '减仓', '#f85149', 24)
    if px:
        marks += tick(px, '现价', '#e3b341', 10)
    dmarks = ''
    for nm, v, _ in dcf:
        xx = X(v)
        dmarks += (f'<path d="M {xx:.1f} 88 l 5 6 l -5 6 l -5 -6 Z" fill="#8ab4f8" opacity="0.85"/>'
                   f'<text x="{xx:.1f}" y="114" text-anchor="middle" font-size="10" fill="#8ab4f8">{nm}≈{cur}{_fmtv(v)}</text>')
    return _svg_open(W, H) + zones + caps + marks + dmarks + '</svg>'

def svg_trend(rows, cur):
    W, H = 470, 244
    x0, xb = 14, W - 14
    top, bottom = 44, H - 26
    maxv = max(r[1] for r in rows) * 1.16
    minv = min(min(r[2] for r in rows), 0)
    n = len(rows)
    gw = (xb - x0) / n
    barw = min(26, gw * 0.30)
    zero = top + (bottom - top) * (0.74 if minv < 0 else 1.0)  # 零轴: 有负净利时上移留出下方负值区
    zline = f'<line x1="{x0}" y1="{zero:.1f}" x2="{xb}" y2="{zero:.1f}" stroke="#262d3d"/>' if minv < 0 else ''
    bars = []
    for i, (y, rev, prof) in enumerate(rows):
        gxc = x0 + gw * (i + 0.5)
        h1 = (zero - top) * rev / maxv
        x1 = gxc - barw - 1.5
        x2 = gxc + 1.5
        rrect = f'<rect x="{x1:.1f}" y="{zero - h1:.1f}" width="{barw:.1f}" height="{h1:.1f}" rx="2.5" fill="#8ab4f8" opacity="0.85"/>'
        rlab = f'<text x="{x1 + barw / 2:.1f}" y="{zero - h1 - 4:.1f}" text-anchor="middle" font-size="9" fill="#8ab4f8">{_fmtv(rev)}</text>'
        if prof >= 0:
            h2 = (zero - top) * prof / maxv
            prect = f'<rect x="{x2:.1f}" y="{zero - h2:.1f}" width="{barw:.1f}" height="{max(h2, 0.5):.1f}" rx="2.5" fill="#3fb950" opacity="0.85"/>'
            plab = f'<text x="{x2 + barw / 2:.1f}" y="{zero - h2 - 4:.1f}" text-anchor="middle" font-size="9" fill="#3fb950">{_fmtv(prof)}</text>'
        else:
            h2 = (bottom - zero) * min(1.0, abs(prof) / abs(minv))
            prect = f'<rect x="{x2:.1f}" y="{zero:.1f}" width="{barw:.1f}" height="{max(h2, 2):.1f}" rx="2.5" fill="#f85149" opacity="0.75"/>'
            plab = f'<text x="{x2 + barw / 2:.1f}" y="{zero + h2 + 10:.1f}" text-anchor="middle" font-size="9" fill="#f85149">{_fmtv(prof)}</text>'
        bars.append(rrect + prect + rlab + plab
                    + f'<text x="{gxc:.1f}" y="{H - 8}" text-anchor="middle" font-size="10.5" fill="#8892a6">{y}</text>')
    legend = (f'<rect x="{W - 168}" y="14" width="10" height="10" rx="2" fill="#8ab4f8"/>'
              f'<text x="{W - 154}" y="23" font-size="11" fill="#aab3c5">营收(亿{cur})</text>'
              f'<rect x="{W - 88}" y="14" width="10" height="10" rx="2" fill="#3fb950"/>'
              f'<text x="{W - 74}" y="23" font-size="11" fill="#aab3c5">净利(亿{cur})</text>')
    return _svg_open(W, H) + zline + ''.join(bars) + legend + '</svg>'

def chart_card(title, svg):
    return f'<div class="chart-card"><div class="ctitle">{htmllib.escape(title)}</div>{svg}</div>'

def build_charts(px, buy, sell, radar, dcf, trend, cur):
    cards = []
    if radar:
        cards.append(chart_card('六维雷达（0–5分）', svg_radar(radar)))
    if len(dcf) >= 2:
        cards.append(chart_card('DCF三情景 每股价值', svg_dcf_bars(dcf, px, cur)))
    if buy and sell and 0 < buy < sell:
        cards.append(chart_card('操作区间阶梯', svg_price_ladder(px, buy, sell, dcf, cur)))
    if len(trend) >= 3:
        cards.append(chart_card('近5年 营收/净利趋势', svg_trend(trend, cur)))
    if not cards:
        return ''
    return '<div class="charts">' + ''.join(cards) + '</div>'

def build_page_body(sym, name, text, buy, sell):
    """图表化页面主体: 徽章+指标卡+SVG图表+一句话结论+核心结论(展开)+其余章节(折叠)."""
    tldr, text = extract_tldr(text)
    hook, text = extract_hook(text)
    secs = split_sections(text)
    rating_main, rating_detail = extract_rating(text)
    metrics = extract_header_metrics(text)
    px, cur = metrics.get('px'), metrics.get('cur') or ''
    radar = extract_radar(text)
    dcf = extract_dcf(text)
    trend = extract_trend(text)
    parts = []
    # meta 行
    meta_bits = []
    d = extract_meta_date(text)
    if d:
        meta_bits.append(f'分析日期：{d}')
    if px:
        meta_bits.append(f'分析日收盘：{cur}{px:g}')
    if meta_bits:
        parts.append('<div class="meta">' + ' ｜ '.join(meta_bits) + '</div>')
    if hook:
        parts.append(f'<div class="hook">{inline(hook)}</div>')
    # 评级徽章
    if rating_main:
        k = rating_kind(rating_main)
        color = {'pos': '#f85149', 'neg': '#3fb950', 'hold': '#e3b341'}[k]
        bg = {'pos': 'rgba(248,81,73,.14)', 'neg': 'rgba(63,185,80,.14)', 'hold': 'rgba(227,179,65,.14)'}[k]
        dtxt = f'<span class="badge-detail">{htmllib.escape(rating_detail)}</span>' if rating_detail else ''
        parts.append(f'<div style="margin:10px 0 2px"><span class="badge" style="color:{color};background:{bg};border:1px solid {color}55">{htmllib.escape(rating_main)}</span>{dtxt}</div>')
    # 指标卡
    cards = []

    def add_card(k, v):
        cards.append(f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div></div>')

    if px:
        add_card('收盘价', f'{cur}{px:g}')
    if buy:
        add_card('买入线', f'{cur}{_num(buy):g}')
    if sell:
        add_card('减仓线', f'{cur}{_num(sell):g}')
    if metrics.get('pe'):
        add_card('PE (TTM)', metrics['pe'])
    if metrics.get('pb'):
        add_card('PB', metrics['pb'])
    if metrics.get('div'):
        add_card('股息率', metrics['div'] + '%')
    if metrics.get('mcap'):
        add_card('市值', metrics['mcap'])
    if cards:
        parts.append('<div class="cards">' + ''.join(cards) + '</div>')
    # 图表区
    ch = build_charts(px, _num(buy), _num(sell), radar, dcf, trend, cur)
    if ch:
        parts.append(ch)
    # 一句话结论
    if tldr:
        parts.append(f'<div class="tl"><b>一句话结论</b>：{inline(tldr)}</div>')
    # 章节: 结论展开, 其余折叠
    for title, body in secs[1:]:
        rendered = render_md(body)[1]
        if not rendered.strip():
            continue
        h2 = f'<h2>{inline(title)}</h2>'
        is_concl = ('结论先行' in title) or ('核心结论' in title)
        if is_concl:
            parts.append(h2 + rendered)
        else:
            parts.append(f'<details class="sec"><summary>{inline(title)}</summary>{rendered}</details>')
    return '\n'.join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--outdir', default=DEFAULT_OUT)
    ap.add_argument('--write-config', action='store_true', help='把提取的财务指标写回 config.json')
    args = ap.parse_args()

    # 预先扫描所有分析文件: basename → (name, code, date, file)
    files = {}   # code_basename -> {name,date,file,code}
    for dname in ("美股", "港股", "A股"):
        d = os.path.join(ANALYSIS_ROOT, dname)
        if not os.path.isdir(d):
            continue
        for f in glob.glob(os.path.join(d, "*六步分析法-*.md")):
            fn = os.path.basename(f)
            m = re.match(r'(.+?)(?:-(.+?))?-六步分析法-(\d{8})\.md', fn)
            if not m:
                continue
            name, code, date = m.group(1), (m.group(2) or m.group(1)), m.group(3)
            key = code
            if key not in files or date > files[key]['date']:
                files[key] = {'name': name, 'file': f, 'date': date}

    # 以 config 条目为驱动: 每个 symbol 用 (中文名 或 代码basename) 匹配分析文件
    cfg = json.load(open(CFG, encoding='utf-8')) if os.path.exists(CFG) else {}
    entries = []  # (symbol, name, analysis_path_or_None, cfg_entry)
    cfg_syms = set()
    for group in ('stocks', 'hk_stocks'):
        for s in cfg.get(group, []):
            sym = s.get('symbol', '')
            if not sym:
                continue
            cfg_syms.add(sym)
            name = s.get('name', '')
            # 1) 按 sym 直接匹配 basename(纯大写/数字文件, 如 9984T -> 9984.T)
            match = files.get(sym)
            # 2) 按 sym 去掉后缀的 basename 匹配 (00700.HK -> 00700)
            if not match:
                base = sym.split('.')[0]
                match = files.get(base)
            # 3) 按中文名匹配 (文件名含中文名)
            if not match and name:
                for k, v in files.items():
                    if name and (name in v['name'] or v['name'] in name):
                        match = v
                        break
            entries.append((sym, name, match, s, group))

    os.makedirs(args.outdir, exist_ok=True)
    index = {}
    n_ok = n_skip = 0
    for sym, name, match, s, group in entries:
        if not match:
            continue
        text = open(match['file'], encoding='utf-8').read()
        buy = s.get('buy') if isinstance(s, dict) else None
        sell = s.get('sell') if isinstance(s, dict) else None
        # 缓存标记: md 内容 + config 买卖价 未变 → 跳过重写 (下次财报更新 md 变化时自动重建)
        tag = 'md:' + hashlib.sha1((text + '|' + str(buy) + '|' + str(sell)).encode('utf-8')).hexdigest()[:10]
        dest = os.path.join(args.outdir, f'{sym}.html')
        if os.path.exists(dest):
            try:
                with open(dest, encoding='utf-8') as fh:
                    if f'<!-- build:{RENDER_VER}|{tag} -->' in fh.read(200):
                        n_skip += 1
                        index[sym] = f'analysis/{sym}.html'
                        continue
            except Exception:
                pass
        body = build_page_body(sym, match['name'], text, buy, sell)
        # 返回看板: 与 dashboard 的 tab 归属一致 (A股tab=CNY计价或market=A股; 港股=hk_stocks组其余; 美股=stocks组)
        _s = s if isinstance(s, dict) else {}
        if group == 'hk_stocks' and (_s.get('ccy') == 'CNY' or _s.get('market') == 'A股' or _s.get('tab') == 'A股'):
            _back = 'a'
        elif group == 'hk_stocks':
            _back = 'hk'
        else:
            _back = 'dashboard'
        page = make_page(sym, match['name'], body, tag, _back)
        open(dest, 'w', encoding='utf-8').write(page)
        n_ok += 1
        index[sym] = f'analysis/{sym}.html'
        print(f'  {sym:12} {match["name"]}  {len(page)}B')
    print(f'分析子页面: 生成 {n_ok}, 缓存跳过 {n_skip}')

    idx_f = os.path.join(os.path.dirname(args.outdir), 'analysis_index.json')
    json.dump(index, open(idx_f, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    # 同时写入脚本同目录(本地 okx/, 云端 deploy/ 即 dashboard.py 的 SCRIPT_DIR)
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.normpath(_script_dir) != os.path.normpath(os.path.dirname(idx_f)):
        json.dump(index, open(os.path.join(_script_dir, 'analysis_index.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'写分析索引: {len(index)} 个 -> {idx_f}')

    if args.write_config and os.path.exists(CFG):
        updated = 0
        for group in ('stocks', 'hk_stocks'):
            for s in cfg.get(group, []):
                sym = s.get('symbol', '')
                match = None
                for sm, nm, ma, _se, _gr in entries:
                    if sm == sym:
                        match = ma; break
                if not match:
                    continue
                text = open(match['file'], encoding='utf-8').read()
                m1, m2, m3 = extract_metrics(text)
                # 每次运行都以最新提取为准覆盖(未提取到则写 None, 避免残留旧错值)
                s['m1'], s['m2'], s['m3'] = m1, m2, m3
                updated += 1
        json.dump(cfg, open(CFG, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        print(f'回写 config.json 财务指标: {updated} 只股更新')

if __name__ == '__main__':
    main()