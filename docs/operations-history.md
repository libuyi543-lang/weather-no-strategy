# Weather NO Strategy

## 当前运行入口

现在的主链路是 `weather_edge_agent.py`，它读取监控 SQLite，给 Hermes/DeepSeek 一次完整的市场与天气快照，要求模型输出全桶概率分布，再由 Python 计算 edge、检查盘口与资金、记录 paper 成交。结算后会记录 AI 与市场的 Brier/log-loss，并调用独立复盘提示生成可验证的反馈；复盘失败会保留为 pending，下一轮自动重试。

Edge 代理只允许 paper 交易，默认覆盖上海、北京、广州、青岛、武汉、重庆、成都。配置在 `weather_edge_agent_config.json`，DeepSeek 密钥放在 `~/.config/weather-market-monitor/deepseek_weather.key`。先检查链路：

```bash
python3 weather_edge_agent.py --summary
python3 weather_edge_agent.py --calibration Beijing
python3 weather_edge_agent.py --dry-run
python3 weather_edge_agent.py --once
```

安装常驻任务：

```bash
./install_weather_edge_agent.sh install
```

旧的 `weather_ai_agent.py`、`weather_dual_strategy.py` 和相关 LaunchAgent 保留用于历史数据与回测兼容，不应与 Edge 代理同时运行，否则会产生相互独立的账本和重复 paper 订单。

Edge 不是固定时间无条件调用 AI。服务每 5 分钟检查一次轻量状态，只有盘口移动超过 `marketMoveTrigger`、观测/模型变化超过阈值、距结算阶段切换，或超过 `heartbeatReviewMinutes` 没有复核时才调用 Hermes；两次 AI 决策至少间隔 `minDecisionIntervalMinutes`。相关参数位于 `weather_edge_agent_config.json`。

AI 不直接决定仓位。它输出可审计的全桶概率、证据置信度、市场错误类型和失效条件；Python 按市场阶段把概率偏离收缩回市场先验，再以可执行盘口、费用、净 edge、资金和 fractional Kelly 计算最终纸面股数。原始 AI 概率与执行概率会一并落库，结算复盘可以区分预测问题和执行问题。

版本变更与当前接入状态见 [CHANGELOG.md](CHANGELOG.md)。
市场模式审计与交易模式选择见 [docs/market_mode_analysis.md](docs/market_mode_analysis.md)。

开源项目的适配研究和取舍记录在 [OPEN_SOURCE_RESEARCH.md](OPEN_SOURCE_RESEARCH.md)。当前只引入零传递依赖的
`python-metar==2.0.1` 和图像解码所需的 `Pillow`；WeatherBench-X 的评估方法以本地 SQLite 模块实现，避免把 Beam/xarray/JAX 等大型依赖
带入常驻采集器。

安装监控服务时会自动安装固定版本依赖：

```bash
./install_monitor.sh install
```

`weather_forecast_evaluator.py` 会持续生成 `data/weather_monitor/forecast_evaluations_latest.csv` 和
`forecast_calibration_latest.json`。Hermes Agent 只读取目标日期之前已经结算的事件，并按当地决策时点去重；少于
10 个事件日的城市/模型会标记为样本不足，不做自动偏差修正。

每日筛选 Polymarket 次日城市气温市场，只评估可成交 NO 卖一严格低于 92c 的盘口，不设最低价；由 Meteoblue 主模型和 ECMWF 辅助模型判断结算区间风险。不自动下单。

## 固定规则

- 目标日期：上海时区的次日
- 事件池：天气标签下的城市日最高/最低温市场
- 排除：香港、深圳全部市场
- 前 20：按 `sqrt(24h成交量 * 流动性)` 排序
- 价格：刷新后的 NO 卖一严格低于 92c，不设最低价
- 每个城市可选择多个不同温度档的 NO；BASE 目标 5 shares，STRONG 目标 10 shares
- 推荐门槛：方向未决时也允许研究明显错价，但必须用粗概率带对应的保守 NO 下界验证可执行价格优势
- 默认执行：每天上海时间 18:30

## 手动运行

```bash
./run.sh
```

只准备候选：

```bash
node prepare.mjs 2026-07-18
```

只准备指定排名区间（例如补充第 11-20 个事件）：

```bash
node prepare.mjs 2026-07-19 --event-offset=10 --event-count=10
```

日志位于 `logs/`，最新快照、分析和最终报告位于 `data/`。

安装或刷新每日任务：

```bash
./install.sh
```

若 Codex 登录失效或其他阶段失败，飞书会收到失败告警，不会静默跳过。

## 分层天气数据采集与 Windy 监控

`weather_market_monitor.py` 是独立研究采集器，不调用 AI、不发飞书，也不下单。当前频率分层为：主站
METAR/SPECI 每分钟一个七站批量请求；中国七城市场、周边站、雷达与卫星维持 5 分钟；完整城市、Windy、
Open-Meteo 与评估维持 30 分钟，其中 Open-Meteo 仅每 3 小时检查一次，并按内容哈希只记录真正变化的模型版本。

- 固定跟踪上海、首尔、北京、广州、东京、惠灵顿、台北、新加坡、青岛、武汉、釜山、重庆、吉隆坡、成都、马尼拉、米兰、卡拉奇、马德里和伦敦，并为每个城市选择日期最近的活跃最高温事件。
- 排除深圳与香港事件，不采集其盘口、预测或结算更新；历史已保存记录不删除。
- 保存事件全部温度档位的 YES/NO Gamma 价格、最佳买卖价及两侧前五档订单簿。
- 从结算规则识别实际气象站，并按站点坐标读取 Windy Meteoblue (`mblue`) Meteogram 一小时预测。
- 保存主站过去 3 小时的全部 METAR/SPECI，并在 250 km 范围建立带距离、方位和气象要素的周边站网络。
- 为中国 Agent 城市提取 RainViewer 雷达回波覆盖/距离/上风向变化，以及 NICT Himawari 真彩色云量代理；不可用或低质量状态会显式落库。
- 每 10 分钟读取 JAXA P-Tree 官方 5 km L2 短波辐射、云光学厚度和 ISCCP 云类型点值；保留 NICT 代理作为并行消融对照。
- 结合站点趋势、上风向梯度、雷达、卫星和模型辐射，识别海风、冷池、放晴、云雨带接近和剩余加热能力。
- 将 Windy 预测点转换到站点 IANA 时区，保存目标当地日期的预测最高温。
- 每3小时采集ECMWF IFS 0.25 ensemble，保留全部51个成员各自的目标日最高温，并计算成员均值、离散度、范围和10/50/90分位数；不先平均成员路径。
- 为每个forecast内容保存独立版本哈希和可信度审计。供应商未公开模型运行时刻时固定保存`NULL`，绝不把API抓取时间当成模型发布时间。
- 冻结07:30、10:00、11:00三个当地截点，并在每次模型版本首次到达时保存`new_run`快照；固定截点只使用当时已经获取的数据。对照同时包含ECMWF IFS HRES 9 km、ECMWF IFS 0.25度、CMA-GFS和待接入的CMA-MESO 3 km。
- 固定城市中已开始记录的事件会持续跟踪到目标当地日结束，避免日期切换造成日内断档。
- 市场结束后自动物化结算标签：精确胜出档可作为训练温度，开放式胜出档只保存区间删失状态；独立METAR日最高保留为审计值，不覆盖官方标签。

数据保存在 `data/weather_market_monitor.sqlite3`，SQLite 使用 WAL 模式。最新报告位于：

- `data/weather_monitor/status_latest.json`
- `data/weather_monitor/market_structure_latest.json`
- `data/weather_monitor/coverage_latest.csv`
- `data/weather_monitor/accuracy_latest.csv`
- `data/weather_monitor/daily_accuracy_latest.csv`
- `data/weather_monitor/daily_accuracy_latest.json`
- `data/weather_monitor/metar_fast_status_latest.json`
- `data/weather_monitor/cma_access_status_latest.json`
- `data/weather_monitor/forecast_cutoff_status_latest.json`
- `data/weather_monitor/forecast_cutoff_snapshots_latest.csv`
- `data/weather_monitor/shadow_ablation_latest.json`
- `data/weather_monitor/shadow_ablation_latest.md`

一分钟 METAR 服务安装与查看：

```bash
./install_metar_fast_collector.sh install
./install_metar_fast_collector.sh status
```

CMA 自动站、雷达、闪电和辐射运行只读权限探针；CMA-MESO还支持取得正式接口后的字段映射接入。凭据必须放在仓库外的
`~/.config/weather-market-monitor/cma_api.json` 且权限为 `600`；无授权时明确记录为 `auth_required`，不会伪装成缺测。

```bash
python3 weather_cma_probe.py --once
```

CMA-MESO映射必须包含`interfaceId`以及明确的响应字段，不能依赖猜测。示意结构如下，字段名需按获批接口的真实说明填写：

```json
{
  "userId": "...",
  "pwd": "...",
  "products": {
    "cma_meso": {
      "interfaceId": "...",
      "params": {
        "staId": "{stationId}",
        "date": "{targetDateCompact}"
      },
      "response": {
        "rowsPath": "DS",
        "validTimeField": "...",
        "temperatureField": "...",
        "runTimeField": "...",
        "temperatureUnit": "C",
        "timeZone": "UTC"
      }
    }
  }
}
```

影子采集会保存每个来源的状态、帧时间、数据年龄和版本哈希。消融实验固定比较 baseline、快速 METAR、
JAXA SWR、JAXA cloud、CMA 自动站与 CMA 雷达/闪电版本，并按目标日期 walk-forward；少于 30 个独立日期
只报告覆盖率，30 日后才能形成初步结论，60 日更稳健。
- `data/weather_monitor/resolutions_latest.csv`
- `data/weather_monitor/data_quality_latest.json`

采集器同时把每轮外部模型、ensemble成员、模型版本审计、结算标签、机场观测和原始响应写入 SQLite：`external_forecasts`、`ensemble_forecasts`、`forecast_model_runs`、`weather_resolution_labels`、`weather_observations`、`raw_weather_payloads`。过程层位于 `station_network_reports`、`remote_sensing_snapshots` 和 `weather_process_states`。原始 JSON 以 gzip 和 SHA-256 保存，便于之后重新解析而不依赖当时的网络响应；雷达/卫星只保存可审计特征、来源和质量状态，不把图片误当成精确气象量。

手动采集一次或只刷新报告：

```bash
python3 weather_market_monitor.py --once
python3 weather_market_monitor.py --report
```

安装常驻任务、查看状态或停止：

```bash
./install_monitor.sh install
./install_monitor.sh status
./install_monitor.sh stop
```

采样按 UTC 每小时 `:00` 和 `:30` 对齐；每轮保存 ECMWF 辅助模型、Open-Meteo 当前天气和 AviationWeather METAR，Meteoblue 则来自 Windy Meteogram 主链路。保存时同时记录站点当地日期、小时和 UTC 偏移。覆盖报告根据 IANA 时区计算，普通日期应有 48 槽，夏令时切换日会是 46 或 50 槽。首次启动前的时段会标记为不具备完整日资格，不会伪装成历史预测。

macOS 休眠或合盖期间无法联网采样，历史 Windy 预测也无法真实回填。两周实验期间需让机器保持开机、联网且不进入系统睡眠；缺口会保留在覆盖报告中。
# Windy Meteoblue hourly forecast

The monitor requests Windy's Meteoblue (`mblue`) Meteogram product at one-hour cadence and verifies both the response header and timestamp spacing. The Windy primary series is always saved as `mblue`; ECMWF is collected separately through Open-Meteo as a comparison model and is never substituted for the Meteoblue series.

Configure a short-lived Windy `userToken` outside this repository:

```sh
mkdir -p ~/.config/weather-market-monitor
chmod 700 ~/.config/weather-market-monitor
printf '%s\n' '<userToken>' > ~/.config/weather-market-monitor/windy_user_token
chmod 600 ~/.config/weather-market-monitor/windy_user_token
```

The token file is hot-reloaded before every collection. Do not put the Windy account password in the project, LaunchAgent, logs, or database. If the token is missing or expired, the run records an authentication error and does not write a forecast row.

### Windy Premium 自动续签

安装独立 Token 守护器：

```bash
./install_windy_token_keeper.sh install
```

守护器会打开一个只用于 Windy 的 Chrome 配置目录：
`~/.config/weather-market-monitor/chrome-profile`。首次安装后，需要在这个窗口中人工登录一次 Windy Premium；登录 Cookie 保存在专用目录，账号和密码不会写入项目。

守护器每 30 秒检查登录状态和 JWT 到期时间，在剩余 12 小时时通过已登录会话刷新页面获取新 Token，并每小时验证一次 Meteoblue 1 小时预报。新 Token 会以原子方式写入 `windy_user_token`，权限保持 `600`。采集器若收到 3 小时降级数据，会请求守护器立即续签并重试失败站点一次。

以下情况会通过 `FEISHU_WEATHER_NO_WEBHOOK` 告警，并在恢复后发送恢复通知：

- 专用浏览器尚未登录或登录会话失效；
- 页面 Token 不含 Premium 权限；
- Token 距离过期不足 2 小时且续签失败；
- Token 存在但 Meteoblue 1 小时预报验证失败；
- 守护器自身异常。

同一故障最多每 6 小时提醒一次，避免重复告警。查看状态：

```bash
./install_windy_token_keeper.sh status
```

## 独立天气看板

天气看板运行在 `http://127.0.0.1:8788/`，与 `8787` 的加密策略看板完全独立。它只读取天气监控 SQLite，展示固定城市的 Windy Meteoblue 最高温、相对上次采集的变化、预测温度档 YES 价格，以及城市级温度与盘口趋势图。同一城市跨日期的事件仍会在后台连续采集，但看板只显示日期最近的当前事件。

采集按 UTC 每小时 `:00` 和 `:30` 对齐，因此固定城市的整点时区会在当地每天 07:30 形成真实 Meteoblue 快照。看板 `FIXED CITY INDEX` 的“07:30 基准”只显示当天该快照，不用其他时刻的滚动预测替代。每日预测准确性也以每个城市当地时间 07:30 最近的快照作为标准预测。

安装、查看状态或停止：

```bash
./install_weather_dashboard.sh install
./install_weather_dashboard.sh status
./install_weather_dashboard.sh stop
```

## 已隔离的旧 10:00 NO Paper

旧的固定当地 10:00、只做 NO 的 paper 模块已经退出主系统，并归档在
`legacy/no-paper-10am/`。旧 LaunchAgent 和安装入口均已禁用，活跃 Hermes Agent 不导入旧代码、
不读取旧配置，也不查询 `weather_no_paper_*` 历史表。历史数据保留用于未来离线对照，不能从主目录重新启动。

## 历史模块

`weather_ai_agent.py`、`weather_dual_strategy.py` 及 `weather_outcome_reviewer.py` 仅用于读取旧账本和离线回测。不要安装它们对应的 LaunchAgent；实时决策、结算和反馈统一由 Edge 代理负责。
