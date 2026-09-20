# Hermes Weather · AI 天气研究与决策审计

**让 AI 给出概率，让代码守住执行边界。** 面向天气预测市场的多源采集、Agent 决策、纸面执行与结算评测系统。

[在线演示 →](https://libuyi543-lang.github.io/weather-no-strategy/) · [快速开始](docs/quickstart.md) · [架构与代码地图](docs/architecture.md) · [验证记录](docs/verification.md) · [English](README.en.md)

[![CI](https://github.com/libuyi543-lang/weather-no-strategy/actions/workflows/ci.yml/badge.svg)](https://github.com/libuyi543-lang/weather-no-strategy/actions/workflows/ci.yml)

![天气研究工作台，界面数字全部为合成示例](docs/images/weather-demo.png)

> 研究原型，仅支持 paper trading，未实现实盘执行。在线界面全部为合成示例；不代表实时行情、真实收益或已验证的预测优势。

## 这个项目解决什么问题

模型能说出一个判断，不代表这个判断有可用的时间截点、可成交报价或足够的证据。本项目把输入、判断、执行、结算拆开记录，便于回答：模型当时看到了什么？为什么被放行或拦截？结算后是否优于市场基线？

| 环节 | 已实现内容 | 可核验代码 |
| --- | --- | --- |
| 多源采集 | 天气模型、METAR/SPECI、市场及订单簿，保存时间与来源 | [采集器](weather_market_monitor.py) · [数据层](weather_data_store.py) |
| Agent 判断 | 全温度档概率分布、证据、置信度与失效条件 | [Edge Agent](weather_edge_agent.py) · [输出 Schema](weather_edge_agent.schema.json) |
| 规则执行 | 数据新鲜度、价格/深度、费用、仓位与资金检查 | [执行实现](weather_edge_agent.py) · [核心测试](test_weather_edge_agent.py) |
| 结算评测 | Brier / log-loss、市场基线、校准与独立反馈 | [评测模块](weather_forecast_evaluator.py) · [反馈 Schema](weather_edge_feedback.schema.json) |

当前 Edge 配置覆盖上海、北京、广州、青岛、武汉、重庆、成都 7 城。采集器还包含更广的观测城市范围；两者不是同一个口径。

## 3 分钟看懂

1. 打开[交互演示](https://libuyi543-lang.github.io/weather-no-strategy/)，查看总览与证据页的数据组织方式。
2. 阅读[架构与代码地图](docs/architecture.md)，追踪从外部数据到 paper 账本的边界。
3. 在本地运行测试，查看重复温度桶、低 edge、已有仓位、概率收缩和资金上限如何处理。

```bash
git clone https://github.com/libuyi543-lang/weather-no-strategy.git
cd weather-no-strategy
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python3 -m unittest discover -v
```

Python 3.11+，Node.js 22 用于界面构建。测试使用临时/内存数据及 mock，不需要你的 API Key 或本地历史数据库。

```bash
cd hermes_gui
npm ci
npm run dev
```

真实采集和 Agent 运行需要额外数据源权限、Hermes 运行时与模型配置，见[运行说明](docs/quickstart.md)。安装依赖不会自动启动服务。

## 真实状态

- 2026-09-20 本地全仓库 **270 项单元测试通过**；其中当前 Edge 专项 8 项。测试数不等于功能数，也不证明收益或预测优越性。
- 公开源码包含当前链路、历史策略与研究模块；[代码地图](docs/architecture.md)标明了边界。
- 原始数据库、密钥、通知地址和个人运行日志不随仓库发布。
- 尚未发布可复现的独立样本外绩效数据集，因此不宣称胜率、收益率或优于市场。

## 复用与反馈

适合研究 Agent 输出边界、时间截点、概率评测和决策审计。欢迎提交可复现的失败场景，尤其是过期证据、缺失观测、错误概率、盘口变化和反馈失败。[报告问题](https://github.com/libuyi543-lang/weather-no-strategy/issues/new/choose) · [贡献指南](CONTRIBUTING.md)

如果你也在做可评测的 Agent，欢迎 Star 保存这个实现，或通过 Issue 交流具体边界案例。

代码采用 [MIT](LICENSE)。外部 API、气象数据及第三方服务遵循各自条款，仓库许可不包含数据源授权。
