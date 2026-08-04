# OKX 量化交易看板

OKX/币安双账户加密货币量化交易实时看板，支持做T区间可视化、策略状态监控、新闻舆情追踪。

## 技术栈

- **数据源**: OKX API v5 + Binance API + Google News RSS
- **后端生成**: Python (Flask + Gunicorn) — 定时调用交易所 API，计算多时间维度做T区间，生成自包含 HTML
- **前端**: 纯静态 HTML/CSS，零外部依赖，Chart.js 行情图表
- **部署**: GitHub Pages + GitHub Actions CI/CD，每5分钟自动更新

## 功能

- 多币种做T区间可视化（日线/4H/1H 多时间维度）
- 策略运行状态实时监控（心跳检测）
- 全球加密新闻舆情聚合
- 排行榜排序（做T溢价率、波动率、成交量）
- 响应式布局，手机/电脑均可访问

## 本地运行

```bash
pip install flask gunicorn requests urllib3
python dashboard_web.py
# 浏览器打开 http://localhost:8080
```

## 部署架构

```
本地/服务器                    GitHub Actions              GitHub Pages
┌─────────────┐    git push    ┌──────────────┐   部署    ┌──────────────┐
│ dashboard.py │ ────────────→ │ deploy.yml   │ ────────→ │ 静态站点     │
│ → HTML 生成  │               │ Pages 部署   │           │ 全球 CDN     │
└─────────────┘               └──────────────┘           └──────────────┘
```

## License

MIT
