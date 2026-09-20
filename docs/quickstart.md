# 快速开始

## 无账号体验

在线演示或本地 `hermes_gui` 都只展示合成案例。运行 `npm ci`、`npm run dev`，按终端地址打开。点击 Console 菜单切换 Evidence view。城市和地图图层控件中的部分元素目前仅为布局示意。

## 离线验证

推荐 Python 3.11+，创建虚拟环境后执行：

```bash
pip install -r requirements-dev.txt
python3 -m unittest discover -v
python3 -m unittest test_weather_edge_agent -v
```

全量测试覆盖当前与历史模块。测试不需要历史数据库或模型密钥。

## 运行真实研究链路

1. 阅读并调整 `monitor_config.json` 与 `weather_edge_agent_config.json`。只有具备授权的来源才能启用；缺失或过期权限应保留为错误状态。
2. 准备所需凭据，存放到配置指定的仓库外路径。Windy、CMA 等数据源可能需要账号/单独授权。
3. 使用 `python3 weather_market_monitor.py --once` 初始化和采集。该命令会发起网络请求并创建本地数据库，结果取决于数据源可用性。
4. 安装并配置 Hermes CLI 与模型提供方。仓库配置是作者的运行示例，不保证其中模型名对所有账号可用。
5. 先执行 `python3 weather_edge_agent.py --dry-run` 查看上下文，再决定是否运行 `--once`。dry-run 需要已采集的数据，不调用 AI。`--once` 可能产生模型费用并写入 paper 账本。
6. 使用 `python3 weather_edge_agent.py --summary` 或 `--calibration Beijing` 检查本地记录。
7. `python3 weather_dashboard/server.py --host 127.0.0.1 --port 8788` 启动真实数据的只读看板。

macOS `install_*.sh` 属于可选的常驻运行工具，会安装 LaunchAgent。公开 plist 是模板，安装脚本通过 `scripts/render_launchagent.py` 注入当前目录与 Python 路径；可用 `WEATHER_PYTHON` 指定虚拟环境解释器。不要直接加载模板，不要同时启用历史策略与当前 Edge。公开发布未启动或验证任何常驻采集/交易服务。

## 常见问题

- 没有数据库：先完成真实采集，或者只运行离线测试与静态演示。
- API 无权限：保留缺失状态，检查数据源授权；不能用演示数字填补研究证据。
- 模型超时或格式错误：检查 Edge 配置、Hermes 运行时和日志；对应失败路径可在测试中查看。
- 想看收益：需要自己的带时间截点的采集、结算与样本外评估；静态页面不提供绩效证据。
