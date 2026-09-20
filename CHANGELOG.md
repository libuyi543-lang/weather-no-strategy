# 版本记录

本文件记录影响天气数据、研究结论或交易决策链路的版本。每个版本分别说明已完成内容、决策接入状态、验证结果和遗留问题，避免把“已经采集”误认为“已经用于决策”。

## 2026.08.09-ladder-1100-cutoff-catchup-v1

日期：2026-08-09
主题：修复 V4.1 因采集进程错过 11:00 对齐窗口而整日缺失

- 根因是 AI 研究循环按“当前时间减90秒后向下取整”选择5分钟槽位；当循环分别落在窗口两侧时，会从10:55直接跳到11:05，即使主采集器已保存完整11:00盘口，也不会再执行冻结。
- 新增11:00专用补执行：主采集器的标准11:00运行确认完成后，只要仍在11:12:00截止时间内且该城市尚未冻结，就读取11:00及以前的盘口、METAR、过程、模型与Ridge证据并写入候选。
- 补执行保持幂等；已有城市跳过，缺失城市可补齐，组合选择在全部候选处理后重新执行。
- 超过11:12:00禁止事后回填。2026-08-09已经缺失的日期继续保留为`MISSING_CAPTURE`，不使用事后信息污染前向样本。
- 本修复仅影响梯度shadow研究采集，不创建订单、action、fill或position，不改变V3/V4.1规则、NO策略或paper-only边界。

## 2026.08.06-ladder-v4-1-unlimited-city-shadow-v1

日期：2026-08-06
主题：V4.1 连续三桶全部合格城市前向 Shadow 队列

- 新建 V4.1 前向队列 `ladder_portfolio_v4_1_1100_5_15_5_all_eligible`，从 2026-08-07 开始；11:00 北京时间的所有合格城市都分别写入一条 shadow 组合。
- 权重仍为 `5 / 15 / 5`，每城净成本上限仍为 15 USDC（含 5% taker fee）；取消的只是“每日最多一城”限制，不新增每日总额上限。
- 旧 V4 的每日一城记录保留为旧规则历史，不再继续写入；不把两套规则混入已开始的前向样本。V3 和 NO 策略不变。
- 组合表增加 `selection_key`，以城市事件作为幂等键；收益、回撤与 Bootstrap 按目标日期聚合，不把同日多城误计成多个独立样本。
- 看板 V4 区更新为 V4.1，清楚标示“全部合格城市”、“15 USDC / 城市”与“无每日总额上限”；历史区仅作 44 个合格城市组合的探索基准，非样本外结论。
- 本次仍为严格 shadow-only：不创建订单、action、fill 或 position，不改变 paper-only、NO-only 与执行安全层。

## 2026.08.05-cma-meso-cutoff-research-v1

日期：2026-08-05
主题：CMA-MESO/ECMWF/CMA-GFS固定截点与新运行到达影子评估

- 新增`forecast_cutoff_snapshots`，按站点当地时间冻结07:30、10:00、11:00预测，并记录模式起报时刻、首次获取时间、到达延迟、版本哈希、最高温路径和缺报原因。
- 固定截点只允许读取`fetched_at_utc <= scheduled_at_utc`的预测；采集器晚启动时可以从已经落库的旧快照恢复，但禁止用截点后到达的数据补写，防止穿越。
- 每次模型版本首次到达另写`new_run`记录。供应商不公开起报时刻时只标记`forecast_content_change`，不把抓取时间伪装成模式运行时间。
- Open-Meteo实时对照增加`ecmwf_ifs` HRES 9 km和`cma_grapes_global`，保留`ecmwf_ifs025`用于分辨率对照；`CMA-MESO 3 km`在无凭据或无字段映射时明确记录`provider_unconfigured`。
- 新增配置驱动的`CmaMesoAdapter`。正式凭据必须在仓库外声明接口ID、响应行路径、有效时刻字段和2米温度字段；映射不完整时拒绝解析。
- 结算后自动回填MAE、偏差、整度桶命中和1°C内命中，报告同时给出独立目标日期数、城市偏差、缺报率及模式到达延迟。
- 本版本只完善shadow研究采集，不进入AI决策、NO规则、梯度选择或任何下单链路；30个独立日期前不得形成正式优劣结论。

## 2026.08.04-ladder-v4-dashboard-v1

日期：2026-08-04
主题：V4连续三桶前向研究接入本地看板

- 看板新增独立“V4 梯度”区和 `/api/ladder-v4` 只读接口，展示冻结规则、前向阶段、日期进度、选择/无交易数量、净成本、净PnL、净ROI、日期块bootstrap 5%下界、胜出腿分布和每日审计流水。
- 历史区明确分开显示15个独立日期的研究基准与规则选择稳健性结果；历史表现标记为非样本外证据，不能替代2026-08-05开始的前向验证。
- V4使用独立前端请求，不再等待较慢的总览或数据质量接口；V4接口异常时只显示本区失败状态，不阻塞其他看板模块。
- 桌面1440×1100和手机390×844均完成Chromium截图检查；手机摘要末项占满整行，未发现V4区块文字重叠或页面横向撑破。
- 全量测试171项通过，Python/JavaScript语法检查通过，SQLite `PRAGMA quick_check`返回`ok`。
- 本次仅接入观测界面；`shadow_only=true`，不创建订单、action、fill或position，也不改变NO策略、安全层和paper-only状态。

## 2026.08.04-ladder-forward-audit-v1

日期：2026-08-04
主题：V3/V4真正前向样本的数据质量与收益审计

- 新增只读脚本 `research/weather_ladder_forward_audit.py`，正式前向起点固定为2026-08-05，独立样本单位固定为目标日期。
- 每个到期日期检查七城事件分母、11:00候选完整性、V3/V4组合选择、捕获时间、真实VWAP名义成本、5%天气盘taker fee、15 USDC净成本上限和结构化拒绝原因；无合格候选保留为零交易日期。
- 已结算组合报告净ROI、净PnL、日期块bootstrap日均PnL 5%下界、最大回撤、连续亏损日期、中心/左右侧/三桶外胜出次数，以及市场锚四分类Brier和log score。
- 阶段规则写死：前10日期只检查数据质量；15日期仅中期报告且不改规则；30日期才进入预注册正式复核，任何结果都不自动授权实盘。
- 当前前向日期为0，状态为`NO_FORWARD_DATES_YET`；首个数据检查点是2026-08-05 11:12之后。
- 新增3项定向测试；全量测试169项通过，SQLite `PRAGMA quick_check`返回`ok`。
- 不改变V1/V2/V3/V4规则，不创建订单、action、fill或position；`enabled=false`、paper-only与NO策略安全层保持不变。

## 2026.08.04-ladder-microstructure-v2-shadow

日期：2026-08-04
主题：连续三桶逐腿归因与中心重仓 V2 前向对照

- 新增只读研究脚本 `research/weather_ladder_microstructure_research.py`，从固定截点之前的真实 order book 重建 VWAP，按目标日期分块 bootstrap；输出 JSON、Markdown及逐事件审计清单。
- 当前完整结算重算为 15 个独立日期、44 个 11:00 候选。旧口径遗漏了 2026-07-29 成都这一笔 `-1.69` 的已结算候选；同时按天气盘当前 5% taker fee 重算后，`1/3/1` 净 PnL `+14.192`、净 ROI `17.1%`、日期块5%下界 `+0.139/事件`。
- `1/3/1`逐腿归因（含费）：下侧腿成本10.969、payout 3、ROI `-72.6%`；中心腿成本60.542、payout 84、ROI `+38.7%`；上侧腿成本11.297、payout 10、ROI `-11.5%`。优势来自中心低估，不是三桶等权覆盖。
- 新增冻结对照 V2 `0.5/4/0.5`，与V1共享完全相同的进入条件和事件分母，只比较权重。按5% taker fee历史净 PnL `+26.637`、净 ROI `29.0%`、日期块5%下界 `+0.300/事件`。
- 新增可执行份额V3 `5/20/5`及单日组合规则 `ladder_portfolio_v3_1100_5_20_5_lowest_center_lead`：每条腿满足当前CLOB 5-share最低值，每天只选中心领先差最小的一城，净成本必须不高于15 USDC。历史15日期净 PnL `+70.455`、净 ROI `41.6%`、日期块5%下界 `+1.786/日`，30种结构/选择器Reality Check `p≈0.0451`。
- V3历史15天三桶覆盖恰为15/15（中心11、上侧4、下侧0），武汉贡献约58%的净PnL；结果过强且城市集中，明确标记为高过拟合风险。移除武汉后历史PnL仍为正，但禁止事后排除任何城市。
- 截点诊断：按每日一城V3组合口径重算，同一V3在11:10覆盖15个日期，净ROI `31.9%`、日期块5%下界`+0.515/日`；11:05净ROI `10.6%`、11:30净ROI `-2.0%`且下界均为负。优势不能解释为稳定的十分钟平滑窗口，固定截点仍保持11:00不变。
- 多重检验：扣费后11:00下8种权重的日期块Reality Check为`p≈0.0205`；8个截点×5种总权重为5的结构共40条规则为`p≈0.0162`。这只说明历史信号不容易由当前有限搜索随机产生，不能替代未来样本外验证。
- V2仍是历史发现，不是样本外证据。V1与V2都从2026-08-05开始同时前向记录；前10日期不调参，30日期前不得正式判断。
- 新增 `weather_ladder_frozen_variants` 保存V1/V2/V3三套权重、成本、结算payout和PnL，不创建action、fill、position或订单。
- 新增 `weather_ladder_frozen_portfolio_selections` 保存V3每日唯一选择、候选数量、资金上限及结算PnL；无合格候选也写零PnL记录，保留完整日期分母。
- 权重被明确标记为 `normalized_shadow_units`。Polymarket官方文档及2026-08-04实际中国天气token均显示 `min_order_size=5` shares，因此当前归一化三桶不能描述成三笔可执行的5-share总仓位。
- 过程门控仍未启用：基准候选可对齐天气为9个独立日期、28个事件；V3所选组合只有9个天气对齐日期，其中正升温8天盈利、非正趋势仅1天亏损，不能形成规则。冻结表与组合选择表显式保存已观测最高、中心距离、升温趋势、ensemble均值/离散度及预注册诊断标签，角色固定为diagnostic-only。

## 2026.08.04-ladder-fee-accounting-v1

日期：2026-08-04
主题：影子梯度研究统一接入天气盘 taker fee

- 费率固定为 `0.05`，公式为 `shares × 0.05 × price × (1-price)`；候选、变体、组合选择和结算统一保存名义成本、手续费、净成本，并以净成本做 15 USDC 组合上限。
- 不创建 action、fill、position 或订单；只影响研究成本和 hypothetical PnL。
- 回归测试：`166 passed`；研究报告已重生成至 `research/output/weather_ladder_microstructure_report.{json,md}`。

## 2026.08.04-ladder-deep-research-v1

日期：2026-08-04
主题：盘口选择器与METAR事件窗口的深入shadow研究

- 新增 `research/weather_ladder_selector_robustness.py`：对30条可执行结构/选择器做留一日期、滚动起点、前后半段、留城和选择重叠检查。
- 新增 `research/weather_ladder_event_window_research.py`：按METAR首次系统抓取时间重建10:00至15:30事件窗口，并测量5/10/15分钟后胜出桶价格反应；不写生产数据库。
- 新增研究笔记 `research/LADDER_DEEP_RESEARCH_2026-08-04.md`。新增独立V4前向shadow：`5/15/5 + 11:00 lowest_max_spread`；每天最多一城、净成本不超过15 USDC。滚动选择净ROI为负，尚无样本外正EV结论。
- 过程事件规则在9个独立日期上的最佳日期块5%下界仍为负，9规则Reality Check `p≈0.232`；过程数据继续保持diagnostic-only。
- 本版本不改变`enabled=false`、paper-only、NO-only及现有V1/V2/V3；V4只写冻结变体和组合选择表，不创建订单、action、fill或position。

## 2026.08.04-frozen-ladder-shadow-v1

日期：2026-08-04
主题：冻结 11:00 市场中心连续三桶 `1/3/1` 前向影子实验

- 新增独立表 `weather_ladder_frozen_candidates`，与原有每 5 分钟枚举全三桶的探索表隔离，避免继续调参污染前向样本。
- 固定规则版本为 `ladder_v1_1100_market_center_1_3_1`：本地 11:00 只读取该截点及之前的 order book，取 YES midpoint 最高的精确温度桶为中心，买入权重固定为相邻三桶 `1/3/1`。
- 固定进入门槛：中心领先第二档至少 `0.03`；完整 order-book 加权 VWAP 总成本严格大于 `1.50` 且不高于 `2.00` USDC；三腿最大 spread 不高于 `0.20`；盘口年龄不超过 10 分钟；每城事件每天只冻结一次。
- 每条记录保留中心/相邻市场、逐腿 VWAP 和深度、spread、盘口年龄、过程状态、Ridge payload、资格状态及结构化拒绝原因。当时无盘口也会写入拒绝记录，保留真实研究分母。过程数据和 Ridge 仅供后续门控诊断，不参与中心选择或权重调整。
- 结算按实际权重回填：中心桶胜出 payout=3，相邻桶胜出 payout=1，其余 payout=0；只有当时合格的影子候选计算 hypothetical PnL。
- 该实验不创建 action、fill、position 或订单；`enabled=false`、NO-only、paper-only 和全部执行安全层保持不变。为避免把当日历史发现混入前向样本，正式验证起点固定为 2026-08-05。
- 验证：中心选择、相邻桶、精确加权 VWAP、成本上下边界、gap/spread/depth/staleness、去重、V1/V2/V3加权结算、每日唯一选择与零交易副作用均有测试；全量166项通过。
- 统计门槛：前 10 个独立日期不调参；15 个日期只做中期报告；30 个日期才做第一次正式结论，并要求日期块保守 EV 下界大于 0、ROI 高于 5%，且结果不被单一城市或单日主导。

## 2026.08.04-ridge-research-collection-v1

日期：2026-08-04
主题：修复 Grok 失败或停用时 Ridge V3 概率停止采集

- 根因：Ridge 快照原先只在构建 Grok 决策上下文时生成；Grok 请求失败、熔断或停用后，普通研究快照和梯度影子数据仍继续写入，但 Ridge V3 概率会中断。
- 修复：每个研究采样点独立运行一次 Ridge，并把同一份点时 payload 和 `snapshot_id` 同时写入研究快照与梯度影子数据，不再依赖 Grok 是否成功返回。
- 降级：Ridge 计算失败不会中断盘口、METAR 和模型数据采集；该采样点明确写为 `unavailable`，梯度概率保持 `NULL`，禁止用当前模型伪造当时概率。
- 历史边界：2026-08-01 至 2026-08-03 已缺失的 Ridge V3 概率保持缺失，不做穿越式回填；修复只保证重新加载后的未来采样点持续采集。
- 交易边界：梯度策略仍为 research/shadow only；NO-only、paper-only 与现有安全门均未改变。

## 2026.07.30-grok-routing-v1

日期：2026-07-30
主题：AI 请求统一切换到 Grok

- 交易 AI 从 GPT 模型切换为 `grok-4.5`，推理强度保持 `medium`，移除 GPT fallback，避免继续消耗 GPT token。
- 观察 AI 保持 `grok-4.5 + medium`，与交易 AI 共用新的仓库外密钥文件。
- API 密钥不写入仓库、SQLite、prompt 或日志；`paperOnly=true`、NO-only、执行前重验证和全部安全门保持不变。
- BeeAPI最小连通性请求确认实际返回模型为`grok-4.5`；全量测试154项通过，交易AI守护进程已重启加载新路由。

## 2026.07.29-weather-research-data-v2

日期：2026-07-29
主题：结算标签、模型版本、ECMWF Ensemble与三桶梯度影子数据

### 新增数据

1. 新增`weather_resolution_labels`：把Polymarket官方结算结果按规则声明的精度物化为训练标签；开放式胜出档只标记为区间删失，不伪造精确温度。独立METAR日最高只作为审计值保存，不冒充官方结算源。
2. 新增`forecast_model_runs`：保存来源、模型、目标日、内容版本哈希及首次/最后发现时间。供应商未公开的模型运行时刻保持`NULL`；抓取时间不再冒充模型发布时间。
3. 新增`ensemble_forecasts`：每3小时采集Open-Meteo ECMWF IFS 0.25 ensemble，逐成员保存目标日最高温，同时保存成员数、均值、标准差、范围与10/50/90分位数，原始响应继续gzip归档。
4. 新增`weather_ladder_shadow_snapshots`：每个研究快照枚举全部相邻精确三档，保存5-share组合市价、组合成本、当时V3三档概率和edge，并在结算后回填组合命中与假设PnL。

### 决策接入状态

- deterministic ECMWF使用内容哈希表示数据版本，AI上下文明确显示模型运行时刻不可用。
- ECMWF ensemble的成员数、均值、离散度和分位数已进入`modelUpdates.ecmwfEnsemble`及后续研究快照，固定标记为`research_evidence_only`。
- 历史快照只回填当时真实盘口；当时不存在的V3概率保持`NULL`，不以当前模型反算历史概率。
- 三桶梯度仍为影子研究，不执行YES；现有NO-only、paper-only、安全内核、仓位和价格规则全部不变。

### 真实库验证快照（2026-07-29 23:25 CST）

- 结算标签203条：193条精确标签，10条开放式区间删失标签。
- 模型版本审计402条：Open-Meteo deterministic 195条、ECMWF ensemble 40条、Windy 167条；402条运行时刻全部为`NULL`，Windy声明字段仅保留为低可信元数据。
- 两轮ensemble共写入40个站点目标日样本，每个样本51个成员。
- 三桶影子组合10,479条，其中8,029条已结算；9,653条明确标记为历史缺少Ridge分布，未发生概率回填穿越。

### 验证

- 官方标签与METAR审计分离、ensemble成员不聚合丢失、运行时刻不可伪造、全相邻三桶枚举、未校准仅影子、结算PnL回填均有回归测试。
- Ridge V2.1使用新标签重训后仍为：MAE 0.4115°C、精确档68.42%、±1°C 89.47%，训练截止2026-07-28。
- 定向测试106项通过；`python3 -m pytest -q`全量154项通过，配置JSON、Python静态编译、Dashboard JavaScript语法和SQLite `quick_check`均通过。

## 2026.07.29-ridge-v21-v3-v1

日期：2026-07-29
主题：Ridge V2.1共享时间中心与Ridge V3研究概率层

### 修正内容

1. Ridge V2.1将原来19个互相独立的半小时模型合并为一个共享时间模型；截点、日内进度、太阳能进度及其与升温趋势的交互进入特征，旧V2 artifact继续作为兼容回退。
2. 每次运行检查最近已结算训练日；有新结算日时每天最多原子重训一次，目标日结果永不进入训练，失败保留上一份有效artifact。
3. 缓存身份包含最新METAR观测时间与两套预报抓取版本：同一观测可复用，新METAR或新模型版本立即重算。
4. 增加0.75°C无解释跳变约束；新观测日最高、预报模型更新、模型重训或天气过程状态改变可以解释并放行较大移动，快照记录全部限幅原因。
5. Ridge V3按截点保存有方向的逐日走步残差，经“已观测最高温物理下限 + 结算half-up取整”生成逐桶概率，概率和固定为1。
6. 校准检验严格按日期向前：当天只能使用更早OOS日期且同一截点的残差，输出50/80/90%覆盖率、Brier、log loss、头号桶平均置信度与命中率。
7. 少于30个独立OOS日期时状态固定为`insufficient_independent_oos_dates`；满30天后还必须通过覆盖误差和置信度差门槛。无论结果如何，当前V3仍为`research_evidence_only`和`authoritative=false`，不能绕过NO策略硬安全层。

### 当前真实结果

- 训练截止：2026-07-28；共享训练行912，逐日走步OOS日期5天。
- V2.1中心：MAE 0.4115°C，精确温度档68.42%，±1°C 89.47%。
- 向前校准：80%区间实际覆盖77.26%，90%区间实际覆盖85.71%。
- 桶概率：头号桶平均置信度75.66%，实际命中66.92%，仍表现出过度自信，因此不得直接用于下单授权。

### 验证

- 新增共享时间特征、目标日隔离、原子写入、非对称概率、概率归一、物理下限、稳定性限幅、新METAR重算和每日单次重训测试。
- Ridge定向测试：26项通过；全量测试149项通过。
- 真实数据库完成共享artifact训练及当前北京事件运行快照，V3概率和为1。

## 2026.07.29-paper-market-order-v1

日期：2026-07-29
主题：Paper请求按最新盘口模拟市价成交

### 修正内容

1. 新增 `paperMarketOrderExecution=true`：AI买入请求通过分析层校验后，按执行时最新订单簿的市价VWAP模拟成交。
2. 执行前发现新METAR、天气过程变化或普通对齐变化时继续刷新并记录审计字段，但不再因此把paper请求标记为 `REJECTED · 未成交`。
3. 分析方向、概率档和价格优势仍使用AI分析时的盘口校验；执行时价格变化作为paper市价单的实际成交成本记录，不重复否决已经通过的分析。
4. `FOLLOW + aligned + 非过早收敛`市场主档NO、现金保留、重复/反向仓位、最大仓位、交易时段和最新盘口可执行性仍是硬限制。

### 验证

- 新增“新METAR到达但paper市价单仍按最新价格成交”与关闭该模式后保持旧拒绝行为的回归测试。
- `python3 -m pytest -q`：139项通过。
- 配置JSON与Python静态编译通过。

## 2026.07.28-decision-freshness-v1

日期：2026-07-28
主题：过期AI分析失效、市场侧价格强校验与时间审计

### 修正内容

1. AI返回时若分析盘口已超过10分钟，整组回复作废，不写入决策周期、动作或尝试下单，并进入最新数据重分析。
2. 单并发模式改为每组临调用前读取上下文、返回后立即验证和持久化，避免七城上下文同时生成后串行等待。
3. 定时复核只把整点/半点作为审计锚点；每城真正调用AI前重新读取当前最新METAR和盘口，过期重试同样使用最新数据。
4. 所有NO买入在动作落库前，强制校验 `marketImpliedProbability` 与NO侧5-share可执行买价一致；YES价格或无关上限不得冒充NO价格。
5. Dashboard明确分开显示“数据时点”和“AI返回时点”，避免把旧盘口分析误读为返回时刻的实时判断。

### 2026-07-28重庆案例覆盖

- 15:30盘口上下文在16:22返回时会被判定超过10分钟并整体作废，不再产生34°C NO尝试记录。
- 34°C NO可执行价为0.27而AI填写0.78时，响应在动作落库前直接判定为价格侧字段不一致。

### 验证

- `python3 -m pytest -q`：138项通过。
- 配置JSON、Python静态编译和Dashboard JavaScript语法检查通过。

## 2026.07.28-ridge-v2-cutoff-v1

日期：2026-07-28
主题：Ridge V2 首次计算时间与 Dashboard 状态语义

### 修正内容

1. 将运行配置 `ridgeV2StartLocalMinutes` 从 420 恢复为 600，与实际存在的 10:00 至 19:00 模型 artifact 对齐。
2. 本地时间 10:00 前，Ridge V2 返回 `not_due`，原因为 `first Ridge V2 cutoff is 10:00 local`，不再尝试读取不存在的 07:00/07:30 artifact。
3. Dashboard 将 `not_due` 显示为“10:00后计算”，将真正的 `unavailable` 显示为“计算不可用”，并继续展示底层原因。

### 验证

- 增加 09:59 返回 `not_due` 的回归测试。
- `python3 -m pytest -q`：134 项通过。
- 配置 JSON、Dashboard JavaScript 和 Python 静态检查通过。

## 2026.07.27-tiered-no-sizing-v3

日期：2026-07-27
主题：方向未决错价入场与分级 NO 仓位

### 修正内容

1. NO-only Paper 新增 `BASE` 与 `STRONG` 两档仓位：BASE 目标 5 shares，STRONG 目标 10 shares。
2. `NO_OVERSHOOT` 可研究风险带为 `30_50` 的方向未决档，但必须以保守 NO 胜率下界验证真实可执行价格，而不能把方向不确定伪装成排除。
3. BASE 要求保守 NO 胜率下界至少高于执行价 10 个百分点；STRONG 要求至少高出 25 个百分点。
4. STRONG 额外要求至少 4 条反共识证据和 3 种独立新信息；已有 5-share BASE 仓位只有出现新可观测证据才可补到 10 shares。
5. 决策上下文新增 10-share NO 可执行价格与深度，动作审计新增 `sizing_tier`。
6. 执行前天气重验证、现金保留、真实深度、最大仓位以及 `FOLLOW + aligned + 非过早收敛`主档 NO 硬禁令保持不变。

### 成都案例覆盖

- 当目标档风险为 `30_50`、保守 NO 下界为 0.50、NO 可执行价为 0.12 时，价格优势为 0.38：允许 BASE；证据达到 STRONG 门槛时允许10-share目标仓位。
- 若相同风险带的NO价格为0.45，价格优势仅0.05：两档均拒绝。

### 验证

- `python3 -m pytest -q`：133 项通过。
- 配置、JSON Schema、数据库迁移和 Python 静态编译通过。

## 2026.07.27-execution-weather-guard-v2

日期：2026-07-27
主题：执行前天气重验证与市场主档 NO 硬门

### 修正内容

1. 执行前不再只刷新盘口，同时重新读取最新 METAR、天气过程状态、Ridge V2 和市场/天气对齐结果。
2. 分析后若出现新 METAR，旧决策不得直接成交，必须依据原失效条件重新分析。
3. 新天气过程导致 Ridge 或市场对齐结构变化时，旧决策不得直接成交。
4. 当 `mode=FOLLOW`、`weatherAlignment=aligned` 且 `marketPrematureConvergence=false` 时，禁止对市场主档买 NO。
5. 上述主档 NO 规则同时进入 Hermes 提示、NO-only 决策输出校验和执行安全内核，避免单层规则被绕过。
6. 动作审计新增执行时 METAR、过程快照、对齐结果和天气重验证状态字段。

### 对 2026-07-27 问题的覆盖

- 北京 34°C：14:30 新 METAR 在动作生成前已经到达，旧升温论点现在会被判定需要重新分析。
- 广州 32°C：Ridge 所有路径与市场主档均为 32°C，且没有过早收敛，新硬门会直接禁止购买 32°C NO。

### 验证

- `python3 -m pytest -q`：128 项通过。
- `weather_ai_agent.py` 静态编译通过，配置 JSON 校验通过。

## 2026.07.27-weather-data-v1

日期：2026-07-27
主题：高频 METAR、JAXA 遥感、CMA 权限探针、Open-Meteo 去重与影子消融

### 本次目标

在不改变 paper-only 风控规则的前提下，提高七个中国城市天气过程的观测频率和可审计性，并建立新数据源进入实时决策前的影子验证流程。

### 已完成

1. METAR/SPECI 快速采集
   - 新增七站一分钟批量轮询服务 `weather_metar_fast_collector.py`。
   - 新增 `fast_metar_reports` 表，保留观测时间、报文时间、接收时间、报文类型、原始报文和首次/最后抓取时间。
   - 主采集器读取最近三小时快速报文，用于主站趋势、周边站网络和天气过程分析。
   - 新增 LaunchAgent、安装脚本、状态报告和针对性测试。

2. JAXA P-Tree 官方遥感
   - 每 10 分钟采集 5 km L2 短波辐射、云光学厚度和 ISCCP 云类型点值。
   - 保存帧时间、数据年龄、空间/时间分辨率、质量状态、来源 URL 和内容哈希。
   - 将 JAXA 数值写入 `weatherProcess.remoteSensing` 和 `weatherProcess.solarHeating`。
   - 保留 NICT Himawari 真彩色云代理，作为并行消融对照，不替代 JAXA 官方产品。

3. CMA 数据接入准备
   - 新增 `weather_cma_probe.py`，探测自动站、雷达、闪电和辐射接口权限。
   - 凭据固定从仓库外 `~/.config/weather-market-monitor/cma_api.json` 读取，并要求权限不宽于 `600`。
   - 无权限时明确记录 `auth_required`，不把无授权伪装成缺测。

4. Open-Meteo 版本去重
   - 新增来源版本登记和内容哈希，仅在模型内容变化时写入新的有效预测版本。
   - Open-Meteo/ECMWF 继续作为辅助模型进入 `modelUpdates.ecmwf`，Meteoblue 仍为主模型。

5. 影子采集与消融框架
   - 新增 `shadow_source_snapshots`、`shadow_ablation_predictions` 及来源覆盖状态。
   - 固定比较 baseline、快速 METAR、JAXA SWR、JAXA cloud、CMA 自动站、CMA 雷达/闪电六个版本。
   - 评估目标包括 OOS MAE、精确档命中、Top-2 覆盖、Brier/log loss、市场领先分钟数和假设 5-share PnL。
   - 少于 30 个独立日期只报告覆盖率；30 日形成初步结论，60 日后才视为较稳健。

6. 实时决策链路
   - Hermes 决策上下文继续包含当前/上一条 METAR、站点网络、天气过程、Meteoblue、ECMWF、盘口、持仓和历史 lessons。
   - JAXA SWR、云光学厚度和云类型已通过 `weatherProcess` 进入 Hermes 决策输入。
   - 新来源目前只作为证据，不自动覆盖 Meteoblue，也不绕过 `marketAlignment` 和 paper 风控。

### 本次明确未做

- 未接入 Wunderground/Weather Company 同源观测。该项按本次要求暂缓。
- 未根据新来源自动训练或修改交易权重。必须先完成足够日期的影子消融。
- 未开放真实交易，系统继续保持 `paperOnly=true`。

### 2026-07-27 验证快照

| 项目 | 状态 | 验证结果 |
|---|---|---|
| METAR/SPECI 快采 | 已运行并进入过程层 | `fast_metar_reports` 已有真实报文；交易决策输入包含当前和上一条 METAR |
| JAXA SWR | 已采集并进入决策 | 七城最新交易决策输入均包含真实短波辐射值 |
| JAXA cloud | 已采集并进入决策 | 决策输入包含云光学厚度/云类型；部分无有效反演时保留空值或质量码 |
| CMA | 仅完成探针 | 当前为 `auth_required`，没有进入决策输入 |
| Open-Meteo/ECMWF | 已接入但运行异常 | 最新交易决策仍读取旧有效快照；后续请求持续出现 HTTP 429 |
| 影子消融 | 已开始积累 | 当前仅 1 个独立影子日期，尚无资格形成效果结论或调整决策权重 |
| Hermes 交易决策 | 正常执行定时复核 | 2026-07-27 最新七城定时周期均完成，输入中可审计到 JAXA 字段 |

### 已知问题与下一版本入口

1. 修复 Open-Meteo 实际请求节流。当前“内容变化才落库”已经存在，但没有阻止连续请求触发 429；需要把请求调度本身严格限制为每 3 小时一次，并增加退避。
2. 获得 CMA 正式权限后，再将 CMA 数据接入过程层和决策上下文；接入前保持 `auth_required`。
3. 连续积累至少 30 个独立结算日期，完成首次 walk-forward 消融，再决定哪些来源可以进入模型特征或确定性规则。
4. 为 ECMWF 增加独立的新鲜度字段和陈旧数据硬限制，避免旧快照在决策输入中看起来仍然有效。

### 主要文件

- `weather_metar_fast_collector.py`
- `weather_process_analyzer.py`
- `weather_market_monitor.py`
- `weather_cma_probe.py`
- `weather_shadow_research.py`
- `weather_ablation_evaluator.py`
- `weather_observer_agent.py`
- `weather_ai_agent.py`
- `README.md`
