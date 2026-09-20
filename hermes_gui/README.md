# Hermes Weather Console

公开交互演示使用完全合成的数据，展示总览、证据时间轴、模型对照和纸面执行信息的组织方式。它不连接实时 API，也不会下单。

```bash
npm ci
npm run dev
npm run build
```

真实数据的只读界面是 `weather_dashboard/server.py`，需要本地采集数据库。为避免将固定的 Agent 概率与真实价格混为一谈，公开演示不自动切换数据源。
