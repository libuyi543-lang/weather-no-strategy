# 验证记录

验证日期：2026-09-20。以下是公开发布副本的本地验证，GitHub 上的持续结果以 Actions 为准。

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| 全仓库 Python 单元测试 | `python3 -m unittest discover -v` | 270 项通过 |
| 当前 Edge 专项 | `python3 -m unittest test_weather_edge_agent -v` | 8 项通过 |
| 前端构建 | `cd hermes_gui && npm ci && npm run build` | 本地及 GitHub Actions 通过 |

全仓库测试包括历史 Agent、双策略、数据采集、评测、时间截点、看板及离线研究，不应全部归为当前 Edge 功能。

Edge 专项覆盖：重复温度桶拒绝、低 edge 拒绝、已有仓位拒绝、单次最多一笔指令、按价格分桶校准、概率向市场收缩、仓位上限、冷却与价格变化触发。

这些结果证明特定代码行为通过了自动化检查，不证明模型概率准确、实时数据源始终可用、没有所有类型的故障，或策略能够盈利。

没有发布的指标：独立事件日数、完整样本外 Brier/log-loss 对照、净收益、回撤和真实用户规模。配置中的 `marketBaselineBrier` 是参数，不是本仓库可复现的实验结论。

公开验证：[GitHub Actions 全量测试与构建](https://github.com/libuyi543-lang/weather-no-strategy/actions/runs/35484252450)。Linux / Python 3.11 的 270 项测试通过；本地干净虚拟环境也通过全量测试。两个浏览器页面已检查关键导航与交互。
