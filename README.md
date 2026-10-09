# 美股量化交易看板

美股多标的量化交易实时监控看板，支持做T区间可视化、策略状态监控、财经新闻聚合。

![构建状态](https://img.shields.io/github/actions/workflow/status/fuqingsong1/trade.us_stock/update.yml?label=%E6%9E%84%E5%BB%BA)
![最近更新](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Ffuqingsong1.github.io%2Ftrade.us_stock%2Flast_update.json&query=update_text&label=%E6%9C%80%E8%BF%91%E6%9B%B4%E6%96%B0&color=blue)

## 在线访问

**https://fuqingsong1.github.io/trade.us_stock**

## 技术栈

- **前端**: 纯静态 HTML/CSS/原生 JavaScript，零外部依赖，响应式布局
- **数据可视化**: 多时间维度技术指标展示
- **数据源**: OKX 行情(本地) / Yahoo Finance + 腾讯行情(云端兜底)
- **自动更新**: GitHub Actions 定时任务(北京时间 21:00-22:00 每小时 + 周末 22:30)，自动生成 HTML 并推送
- **部署**: GitHub Pages 静态托管，全球 CDN 加速

## 功能

- 多标的做T区间可视化（日线/4H/1H 多时间维度）
- 策略运行状态实时监控
- 全球财经新闻舆情聚合（DeepSeek LLM 分析）
- 排行榜排序（溢价率、波动率、成交量）
- 响应式布局，手机/电脑均可访问

## 自动更新架构

```
GitHub Actions (美国云服务器, 免代理)
  ├─ 定时 cron 触发 (workflow_dispatch 可手动触发)
  ├─ collect_calendar.py  → 财报/FOMC/经济数据日历
  ├─ news.py              → Google News 抓取 + DeepSeek 舆情分析
  ├─ dashboard.py         → 生成看板 (Yahoo 行情兜底, 不暴露账户)
  ├─ transform.py         → 深色主题着陆页 index.html
  └─ 自动 commit + push → GitHub Pages 更新
```

本地交易策略 (strategy_v4.py / strategy_binance.py) 仅在本地服务器运行，**不推送到本仓库**；云端仅生成展示数据，不涉及任何交易下单。

## GitHub Secrets 配置（仓库 Settings → Secrets and variables → Actions）

首次部署需配置以下密钥（全部加密存储，不会出现在代码中）：

| Secret | 用途 | 是否必需 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 新闻舆情 LLM 分析 | ✅ 必需 |
| `OKX_API_KEY` | OKX 只读行情（云端 Yahoo 兜底，OKX 密钥可留空） | 可选 |
| `OKX_API_SECRET` | 同上 | 可选 |
| `OKX_PASSPHRASE` | 同上 | 可选 |
| `BINANCE_API_KEY` | 币安行情（云端默认不可达，可留空） | 可选 |
| `BINANCE_API_SECRET` | 同上 | 可选 |

配置完成后，在 Actions 页可手动 "Run workflow" 立即触发一次更新。

## 手动更新（本地）

```bat
update_all.bat
```

## License

MIT
