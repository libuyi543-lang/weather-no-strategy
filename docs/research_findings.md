# 研究结论汇总：中国 7 城最高温市场的 edge 排查

> 对象：Polymarket「当日最高温」分桶市场，上海、北京、广州、青岛、武汉、重庆、成都（结算站 ZSPD/ZBAA/ZGGG/ZSQD/ZHHH/ZUCK/ZUUU）。
> 时间：2026-07 ~ 2026-09。所有回测都不用未来函数；置信区间用日期块 bootstrap（独立单元 = 目标日期）。
> 状态：只做研究和纸面模拟，没有实盘。

## 一句话结论

**到 2026-09-24 为止，没有找到任何可以稳定变现的 edge。** 每条路线都被证伪了，或者样本外没通过。能确定的只有市场这一侧的事实：
中国 7 城的高温桶定价比所有公开气象源都准；做 taker 在所有价位上都亏；做 maker 被逆向选择吃掉；而且在 AWC 发布 METAR 之前，市场就已经完成了约 95% 的定价。

| 路线 | 结论 | 证据 |
|---|---|---|
| 多桶梯度结构 MIXE10 | 样本内 +8.4% ROI，**样本外失效**（ROI −10.7%） | [§1](#1-多桶梯度结构mixe10) |
| 气象模型 / 集合预报 vs 市场 | 所有源在所有时点都显著输给市场 | [§2](#2-模型与数据源能否比市场准) |
| 已记录的观测/过程数据增量 | 可交易增量 = 0 | [§2](#2-模型与数据源能否比市场准) |
| 结算口径套利 | METAR 日最高取整和官方结算几乎 100% 一致 | [§3](#3-结构性套利) |
| 价格分档偏差（taker） | 全部档位 EV 为负 ≈ 半个价差 + 手续费 | [§3](#3-结构性套利) |
| 结算前扫陈旧挂单 / 全板套利 | 触发太少、为负 | [§3](#3-结构性套利) |
| 做市（maker） | 所有成交模型都显著亏损 | [§4](#4-做市可行性) |
| 抢 METAR 速度 | 不可能：市场比我们的 METAR 链路早约 12 分钟 | [§5](#5-市场是否比-metar-更快) |
| 单变量信号（雷达） | 无效 | [§6](#6-单变量天气信号) |
| 单变量信号（青岛海风） | 样本内显著，**样本外未确认** | [§6](#6-单变量天气信号) |

---

## 1. 多桶梯度结构（MIXE10）

脚本：`research/weather_ladder_*.py`、`research/weather_mixe10_optimization_ideas.py`、`research/weather_dynamic_rebalance_backtest.py`
报告：`research/output/weather_ladder_*.md`、`weather_mixe10_optimization_ideas.md`、`weather_dynamic_rebalance_backtest.md`

- **结构**：过了冻结门槛（中心桶领先 ≥ 0.03、1/3/1 成本带 1.50–2.00）的日子，买 10 股中心桶 YES，再买两侧邻桶 NO 各 5 股；中心 YES 带两段式出场，NO 腿持有到结算。
- **样本内**（2026-07-19 ~ 08-23，210 个城-日）：+95.2 USDC，ROI +8.4%；相对单桶基线 S0 的配对日差 +3.41，5% 下界 +1.74。中心桶在门槛日内命中率 51.6%，入场均价 0.450。
- **入场时段**：09–12 点之间没有统计差异；13 点起显著变差（ROI −25.9%，中心命中率跌到 32%，逆向选择）。
- **变体**：NO 腿提前锁利显著有害；按连败降风险的闸门（GATE5）看起来改善了回撤，但全部优势来自唯一一个连败簇，证据等级 = n=1。
- **样本外**（08-24 ~ 09-18，用冻结引擎重跑）：**−43.0 USDC，ROI −10.7%**，正/负日期 4/10。入场价一样是 0.444，中心命中率却从 51.6% 掉到 37.5%。相对 S0 的优势也消失了。合并 44 个日期后不再显著。
- **教训**：一次回测结果高得离谱（+603），最后查出是引擎把注释吞掉了一行，导致变量读到了别的桶的死盘口。结果异常好时，先当 bug 查。

## 2. 模型与数据源能否比市场准

脚本：`weather_source_vs_market_bakeoff.py`、`weather_center_bucket_model_vs_market.py`、`weather_ensemble_bucket_research.py`、`weather_recorded_data_incremental_audit.py`、`weather_exact_high_model*.py`、`weather_physics_high_model.py`、`weather_ridge_feature_ablation.py`

- **点预测对决**（667 个城-日 × 09/11/13/15 时）：没有任何气象源在任何时点赢过市场隐含期望温度。市场 MAE 从 0.66 收紧到 0.31；原始 NWP 比市场差 2–3 倍，并且普遍有 −0.9 ~ −1.5 °C 的冷偏差（ECMWF −0.9、CMA-GRAPES −1.4、集合均值 −1.05）。自研 Ridge V2 最接近，但仍显著落后。
- **中心桶二元事件**（175 个城-日）：市场 log-loss 0.670 / Brier 0.239，显著优于所有模型形态。最好的站点校准版也只有 0.90–0.92。
- **ECMWF 51 成员集合 → 桶概率**：原始成员分数偏冷约 1 °C，离散度只有真实的一半。逐站校准后仍全面输给市场。
- **已记录过程数据的增量**（METAR 动量、上风温差、雷达回波、云量、辐射比等 9 个特征，做残差的留一日期检验）：09/11 点没有正增量，其中 6 组显著有害；15 点后改善只有 0.006–0.012 °C，而且这些特征同源、无法变现。

## 3. 结构性套利

脚本：`weather_spread_arb_research.py`、`weather_stale_sweep_backtest.py`、`weather_release_timing_research.py`、`weather_high_price_weather_gate_backtest.py`

- **结算口径**：中国 7 城 METAR 日最高取整和官方结算几乎完全一致，没有口径差可做。
- **价格分档（热门-冷门偏差）**：994 个已结算事件，在当地 09/11/14 时按 5 股卖盘 VWAP 加手续费买入 YES 或 NO，几乎所有档位 EV 都在 −0.02 ~ −0.06/股，约等于半个价差加手续费。唯一为正的格子是约 80 个格子里的多重比较噪声。
- **全板套利**（买齐所有桶合计 < $1）：210 个城-日里只有 4 个窗口，边际 2–3%。
- **结算前扫陈旧挂单**：217 个城-日只触发 5 天，ROI −11.7%。18 点后当日最高温取整仍有 16.9% 会翻转（傍晚确实会升温），所以市场 0.90–0.95 的要价是合理的。
- **对齐 ECMWF 发布窗口**：检验没有功效。数据本来 88% 就是新鲜的。

## 4. 做市可行性

脚本：`research/weather_maker_trades_fetch.py`（从 `data-api.polymarket.com/trades` 抓全部公开成交），`research/weather_maker_feasibility.py`
报告：`research/output/weather_maker_feasibility.md`、`weather_maker_feasibility_hold60.md`

- **数据**：378 个已结算事件 / 52 个日期，4,158 个市场，82.8 万笔公开成交。
- **规则**：每个 5 分钟盘口快照在 best bid/ask 各挂 5 股（JOIN），或者往里抢一档（PENNY）；挂单最多保留 300 秒；成交后持有到结算。排队位置不可知，所以同时给出三种成交模型：OPT（排在队首）、QUEUE（排在已有挂单之后）、CONS（只有价格穿过才算成交）。
- **结果**（每股 PnL，日期块 bootstrap 90% 区间全部低于 0）：

  | 模型 | JOIN | PENNY |
  |---|---|---|
  | OPT（乐观） | −0.0048 | −0.0083 |
  | QUEUE | −0.0116 | — |
  | CONS（保守） | −0.0127 | −0.0098 |

- **原因**：挂单时点的半价差约 +0.015–0.020，结算时的逆向选择约 0.02/股，把它吃光还倒亏。天气类 maker 返佣（taker 费的 25%，约 0.0012/股）补不回来。
- **稳健性**：挂单时间缩到 60 秒结论不变；前后半段一致；没有任何「价格档 × 方向」或城市显著为正。最亏的是 0.20–0.50 价位的买单（约 −0.02/股）。
- **关键**：逆向选择**并不集中在 METAR 更新之后**，所以「新 METAR 一来就撤单」这类速度防守修不好它。

## 5. 市场是否比 METAR 更快

脚本：`research/weather_pre_metar_leakage.py`
报告：`research/output/weather_pre_metar_leakage.json`

- **设计**：共 1,502 次 METAR 发布（41 个日期）。看的是「当前装着日最高温的那个桶」：下一份 METAR 让它出局（KILL，约 290 例），还是让它存活。比较两组在每段时间里的价格分离：上一份发布 → 观测时刻 O → AWC 接收时刻 R（约 O+6 分钟）→ 我们 1 分钟轮询拿到 F（约 R+6 分钟）→ F+15 分钟。
- **结果**：KILL 相对存活的超额价格变动里，**观测时刻 O 之前已完成 35%–69%，到 R 时已完成约 95%**，到我们拿到 METAR 时约 96%，之后没有可交易的余量。只有整点报的站在 O 之前完成 44%，半小时报的站 24%。
- **含义**：有人在用比 AWC 更快的数据源交易（大概率是国内站点分钟级实况）。任何「看到 METAR 再反应」的策略都来不及，AI 也解决不了延迟问题。
- **坑**：桶出局后 YES 买盘消失，中间价会变成 None。必须把空的买侧当 0、空的卖侧当 1，否则 KILL 样本会被全部过滤掉。

## 6. 单变量天气信号

脚本：`research/weather_single_signal_event_study.py`、`research/weather_qingdao_seabreeze_oos.py`
报告：`research/output/weather_single_signal_event_study.json`、`weather_qingdao_seabreeze_oos.json`

度量：在 11–16 点、且现温 ≥ 当日最高 −1 的前提下，比较信号触发后「结算落在当前最高温所在桶之上」的实际比率和触发时市场给的 P_up；另外算触发时买入上方各桶 NO 的可执行 PnL（taker，含手续费）。

- **雷达（RainViewer）**：回波进入 25 km（n=39）完全无效，交易显著亏。回波越近效果越强，但样本也越少（≤5 km 时 n=10），且都不显著。
- **青岛海风**（METAR 风向从非向岸转为 90–210°，风速 ≥ 6 kt，当天首次触发）：
  - 样本内（07-28 ~ 09-18，n=16）：实际再升温 12.5%，市场给 35%，gap −0.23；交易 +15.3 / 成本 125。
  - 冻结规则后用公开数据回补样本外（gamma + CLOB prices-history + IEM ZSQD METAR，04-27 ~ 09-23，n=19）：**gap −0.08，CI [−0.24, +0.08]；交易 +4.6 / 成本 145，CI [−10.8, +20.1]，未确认**。
  - 分段看：5–6 月（n=8）完全无效，实际再升温 62.5%，市场只给 53%；7 月（n=9）有效。但这个季节切分是看过数据之后才做的，属于分叉路径。
  - 就算为真，规模也极小（35 次触发合计 +20 / 成本 270）。而且这个信号本身来自 METAR，属于「比市场快」而不是「比 METAR 快」。
  - 唯一干净的验证方式：明年 7–9 月冻结参数，做纸面前向验证。
- 上海海风无效；把风向扇区放宽后效果消失，收窄后更强，这和物理规律一致。

## 7. Agent 系统本身的诊断

- 旧版 prompt 禁止 LLM 输出完整温度分布，却要求它判断「市场是否错价」。在这个架构下，这个目标本来就无法完成。
- 新版 `weather_edge_agent.py` 叠了四层保守机制：「默认市场是对的」、自报置信度、收缩系数、最小 edge 门槛。结果 AI 要偏离市场约 0.45 才会下单，而实测偏离中位数只有 0.04，所以数学上永远不会成交。唯一跑过的一天：110 次决策、0 笔成交。在 n=7 个城-日上，AI 的 Brier 0.377，市场 0.341。
- 当时喂给 LLM 的证据，正是 §2 证明可交易增量为零的过程数据。

## 剩下的方向

1. **更快的数据源**：国内站点分钟级地面实况。如果能拿到，§4 和 §5 的「来不及」需要重算。这是数据获取问题，不是策略问题。
2. **接受有效市场**：承认中国 7 城高温桶的定价是有效的，把精力移到别处。

---

## 复现

所有 `research/*.py` 都只读 `data/weather_market_monitor.sqlite3`（私有采集库，不公开）或公开 API，输出写到 `research/output/`。以下缓存体积过大、没有进仓库，但可以用脚本重建：

| 文件 | 大小 | 重建方式 |
|---|---|---|
| `research/output/maker_trades_cache.sqlite3` | ~500 MB | `python3 research/weather_maker_trades_fetch.py`（Polymarket data-api，公开） |
| `research/output/weather_maker_feasibility_fills.csv` | ~90 MB | `python3 research/weather_maker_feasibility.py` |
| `research/output/qingdao_seabreeze_oos_cache.json` | ~20 MB | `python3 research/weather_qingdao_seabreeze_oos.py`（gamma + CLOB + IEM，全公开，不依赖私有库） |
| `research/output/weather_dynamic_rebalance_backtest.json`、`weather_exact_high_model_v2_report.json` | 2–4 MB | 重跑对应脚本；同名 `.md` 摘要已在仓库里 |

`weather_qingdao_seabreeze_oos.py` 是唯一可以完全不依赖私有库、端到端复现的研究。
