# 双策略 AI 框架

## 主流程

```text
现有天气/市场采集器
        |
        v
SQLite 时点数据库
        |
        v
Python 确定性候选层
  - 三桶中心与连续档检查
  - 单桶 NO 价格/概率预筛
  - 新信息状态哈希去重
        |
        v
Hermes + gpt-5.6-sol (medium)
  - THREE_BUCKET_REVIEW
  - SINGLE_NO_REVIEW
        |
        v
严格 JSON Schema
        |
        v
Python 风控复核
  - 时间窗、盘口深度、edge
  - 现金预留、城市/组合敞口
  - 仓位与重复加仓限制
        |
        v
独立 Paper Ledger -> 官方结算
```

## 文件职责

- `weather_dual_strategy.py`：双策略编排、候选生成、风控、paper 落账和结算。
- `weather_dual_strategy.schema.json`：Hermes 输出契约。
- `weather_dual_strategy_config.json`：模型、时间窗、份额映射和风险阈值。
- `weather_ai_agent.py`：复用其数据上下文、Hermes 运行桥和 Ridge 研究输入。
- `hermes_weather_bridge.py`：无工具、无会话膨胀的 Hermes oneshot 调用。
- `test_weather_dual_strategy.py`：双策略核心边界测试。

## 三桶策略

Python 先选出最可能的中心档，并确认上下各有一个连续可交易档。Hermes 不预测任意仓位，只输出：

- `COLD` -> 下/中/上 = `10/15/5`
- `NEUTRAL` -> 下/中/上 = `5/20/5`
- `HOT` -> 下/中/上 = `5/15/10`

10:30 开始复核，11:15 后不再新进场，12:00 后不再产生三桶候选。第一版只进一次并持有到结算。

## 单桶 NO

当前关闭三桶新入场，先完善单桶 NO。Python 把盘口可用的全部精确档交给 Hermes，并标记 Ridge 中心与受支持的封顶路径。AI 只允许三种买入理由：`NO_CEILING` 表示现有天气数据和条件支持最高温达不到目标档下边界；`NO_OVERSHOOT` 表示明显会超过目标档上边界；`NO_MARKET_TAIL_REJECTION` 表示新鲜、独立的天气与市场证据证明市场明显高估了该温度档。Ridge 中心和受支持的封顶路径都是可推翻的强先验：只有 AI 明确 `REJECT` 对应路径、说明推翻原因，并给出至少三条、覆盖两类信息的证据时才可买该桶 NO。Python 使用保守概率下界计算 edge：BASE 至少 0.10，STRONG 至少 0.25。

同一市场最多一次 BASE 和一次 STRONG 升级。升级必须有不同的状态哈希、至少四条支持事实和三类新信息。

## 运行

初始化数据库表，不调用 AI：

```bash
python3 weather_dual_strategy.py --init-only
```

执行一次 paper 检查：

```bash
python3 weather_dual_strategy.py --once
```

常驻循环：

```bash
python3 weather_dual_strategy.py --loop
```

配置已启用，但仍严格为 `paperOnly=true`。`--once` 会调用 Hermes 并只向独立 paper ledger 写入模拟交易；不会发送真实订单。
