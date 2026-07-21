# AI 商业化景气度追踪

个人自用的 AI 商业化景气度看板,通过 GitHub Actions **每日自动更新**,GitHub Pages 托管。

**看板地址**:https://silentdog918.github.io/ai-jingqidu-tracker/

## 数据内容

| 板块 | 来源 | 说明 |
|---|---|---|
| OpenRouter Token 消耗 | openrouter.ai 公开接口 | 每日各模型 Token 排行(滚动保留 45 天历史)、新上架模型、应用榜 |
| SDK 下载量 | npm registry / pypistats | openai、anthropic 等核心 SDK 的 91 天日下载量与周环比 |
| AI 产业链新闻 | Google News RSS | 算力、模型厂商、商业化、融资、中文要闻五个栏目 |
| 核心标的行情 | Yahoo Finance | EOD 收盘价、1/5/20 日涨跌、60 日走势(非实时) |
| 投资播客 | 各节目官方 RSS | BG2 / All-In / Invest Like the Best / Dwarkesh / Latent Space / 硅谷101 / OnBoard! |

## 运行机制

- `.github/workflows/update.yml`:每天 **06:45(新加坡时间)** 定时运行,也可在 Actions 页手动 Run workflow
- `scripts/update_data.py`:纯 Python 标准库抓取,生成 `data/*.json`;某一源失败时保留上一份数据,不影响其他源
- 页面 `index.html` 打开时读取 `data/*.json`,永远显示最近一次抓取的数据
- OpenRouter 的接口只提供最近几天数据,脚本会与仓库中的历史合并,靠每日提交滚动积累

## 改配置

编辑 [`config.json`](config.json) 即可更换:新闻关键词、npm/PyPI 包名、行情标的与分组、播客源。
改完 push 会自动触发一次数据更新。

## 免责

数据来自公开接口,可能滞后或有误,仅供个人参考,不构成任何投资建议。
