# Weather Market Data Collection Architecture

当前系统的目标是持续积累天气市场、天气观测、模型预测和梯度交易研究数据。AI 分析、Observer 分析和 Paper 交易目前关闭。

```mermaid
flowchart LR
    subgraph Sources[外部数据源]
        PM[Polymarket\n市场/盘口/成交条件]
        METAR[aviationweather.gov\nMETAR / SPECI]
        WINDY[Windy Meteoblue\n逐小时模型预测]
        MODEL[Open-Meteo / ECMWF\n模型与集合预测]
        SAT[卫星与遥感\nHimawari / JAXA]
    end

    subgraph Collectors[采集服务]
        MON[weather-market-monitor\n盘口 + 主天气采集\n快: 5min / 完整: 30min]
        FAST[weather-metar-fast-collector\n快速 METAR\n约 60sec]
        TOKEN[windy-token-keeper\nWindy token 保活]
    end

    subgraph Store[SQLite 数据账本\ndata/weather_market_monitor.sqlite3]
        MS[(market_snapshots\nPolymarket 盘口快照)]
        OBS[(weather_observations\n天气观测)]
        FM[(fast_metar_reports\n快速 METAR/SPECI)]
        WF[(windy_forecasts\nWindy Meteoblue 预测)]
        EX[(external_forecasts / ensemble_forecasts\n其他模型预测)]
        CUT[(forecast_cutoff_snapshots\n07:30/10:00/11:00 + new_run)]
        RS[(remote_sensing_snapshots\n卫星/遥感/天气过程)]
    end

    subgraph Derived[衍生研究数据]
        RIDGE[(weather_ridge_v2_snapshots\nRidge 特征/路径/分布)]
        RESEARCH[(weather_ai_research_snapshots\n统一研究快照)]
        LADDER[(weather_ladder_shadow_snapshots\n梯度交易 shadow\n组合价格/edge/命中/PnL)]
    end

    DASH[Dashboard :8788\n状态/数据质量/研究结果]
    REPORT[data/weather_monitor\nlatest 报告与状态文件]
    AI[weather-ai-agent\n当前: AI 关闭\n仅执行衍生数据写入]
    OBSERVER[weather-observer-agent\n当前: 关闭]

    PM --> MON
    METAR --> MON
    METAR --> FAST
    WINDY --> MON
    MODEL --> MON
    SAT --> MON
    TOKEN -. token .-> MON

    MON --> MS
    MON --> OBS
    MON --> WF
    MON --> EX
    EX --> CUT
    MON --> RS
    FAST --> FM

    MS --> AI
    OBS --> AI
    FM --> AI
    WF --> AI
    EX --> AI
    RS --> AI
    AI --> RIDGE
    AI --> RESEARCH
    AI --> LADDER

    MS --> DASH
    OBS --> DASH
    WF --> DASH
    RIDGE --> DASH
    LADDER --> DASH
    MON --> REPORT
```

## 当前采集频率

| 数据 | 采集服务 | 当前频率 | 主要表 |
|---|---|---:|---|
| Polymarket 盘口 | `weather-market-monitor` | 快速 5 分钟，完整 30 分钟 | `market_snapshots` |
| METAR / SPECI | `weather-metar-fast-collector` | 约 60 秒 | `fast_metar_reports` |
| 主天气观测 | `weather-market-monitor` | 快速/完整周期 | `weather_observations` |
| Windy Meteoblue | `weather-market-monitor` | 完整周期约 30 分钟 | `windy_forecasts` |
| Open-Meteo / ECMWF | `weather-market-monitor` | 默认约 180 分钟 | `external_forecasts`, `ensemble_forecasts` |
| CMA-GFS / GRAPES Global | `weather-market-monitor` | 默认约 180 分钟 | `external_forecasts` |
| CMA-MESO 3 km | `weather-market-monitor` | 获得正式接口后每 30 分钟探测 | `external_forecasts` |
| 模型冻结截点 | `weather-market-monitor` | 每 5 分钟检查，固定 07:30/10:00/11:00 | `forecast_cutoff_snapshots` |
| 卫星/遥感 | `weather-market-monitor` | 随采集周期，部分源有单独间隔 | `remote_sensing_snapshots` |
| Ridge / 梯度 shadow | `weather-ai-agent` 数据模式 | 约 5 分钟研究快照 | `weather_ridge_v2_snapshots`, `weather_ladder_shadow_snapshots` |

## 现在运行与暂停的部分

- 运行：盘口、主天气、快速 METAR、Windy Meteoblue、模型预测、遥感、Ridge 和 ladder shadow 数据积累。
- 暂停：AI 决策、AI lesson、Observer AI、Paper 交易和日报。
- 看板只读取 SQLite 和 latest 状态文件，不参与数据采集。

## 数据流的核心关系

1. `market_snapshots` 是交易市场状态的原始时间序列。
2. `weather_observations`、`fast_metar_reports`、`windy_forecasts` 和模型预测描述真实天气与预测信息。
3. `weather_ridge_v2_snapshots` 将天气信息整理成可比较的路径/分布特征。
4. `weather_ladder_shadow_snapshots` 把相邻温度档组合成梯度交易候选，并记录价格、edge、命中和假设 PnL。
5. 研究和交易前应按 `event_id` / `target_date` 分组，不把同一市场内的快照当成独立样本。
