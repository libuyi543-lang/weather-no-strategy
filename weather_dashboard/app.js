const $ = (id) => document.getElementById(id);

const els = {
  clock: $("clock"), updatedAt: $("updatedAt"), autoBtn: $("autoBtn"), refreshBtn: $("refreshBtn"),
  cityCount: $("cityCount"), forecastCoverage: $("forecastCoverage"), changedCount: $("changedCount"),
  priceChangedCount: $("priceChangedCount"), averageTemp: $("averageTemp"), modelFreshness: $("modelFreshness"),
  cityRows: $("cityRows"), citySearch: $("citySearch"), sortSelect: $("sortSelect"), changedOnly: $("changedOnly"),
  changeFeed: $("changeFeed"), changeStatus: $("changeStatus"), detailTitle: $("detailTitle"), detailMeta: $("detailMeta"),
  detailStats: $("detailStats"), forecastChart: $("forecastChart"), profileChart: $("profileChart"),
  priceChart: $("priceChart"), priceLegend: $("priceLegend"), bucketGrid: $("bucketGrid"), bucketUpdated: $("bucketUpdated"),
  forecastChartValue: $("forecastChartValue"), profileChartValue: $("profileChartValue"), priceChartValue: $("priceChartValue"),
  accuracyDate: $("accuracyDate"), accuracyFilter: $("accuracyFilter"), accuracyHit: $("accuracyHit"),
  accuracyMiss: $("accuracyMiss"), accuracyPending: $("accuracyPending"), accuracyMae: $("accuracyMae"), accuracyRows: $("accuracyRows"),
  accuracyQuick: $("accuracyQuick"), accuracyQuickHint: $("accuracyQuickHint"),
  qualityRun: $("qualityRun"), qualitySlot: $("qualitySlot"), qualityComplete: $("qualityComplete"),
  qualityModels: $("qualityModels"), qualityMetar: $("qualityMetar"), qualityCurrent: $("qualityCurrent"),
  qualityRaw: $("qualityRaw"), qualityRows: $("qualityRows"),
  accuracyToggle: $("accuracyToggle"), accuracyBody: $("accuracyBody"), accuracyCollapsedSummary: $("accuracyCollapsedSummary"),
  aiAgentStatus: $("aiAgentStatus"), aiUpdatedAt: $("aiUpdatedAt"), aiLatestMode: $("aiLatestMode"),
  aiLatestCity: $("aiLatestCity"), aiCyclesToday: $("aiCyclesToday"), aiModeCounts: $("aiModeCounts"),
  aiFillsToday: $("aiFillsToday"), aiAvailableCash: $("aiAvailableCash"), aiOpenPositions: $("aiOpenPositions"),
  aiRealizedPnl: $("aiRealizedPnl"), aiCityFilter: $("aiCityFilter"), aiModeFilter: $("aiModeFilter"),
  aiCycleCount: $("aiCycleCount"), aiCycleRows: $("aiCycleRows"), aiDetailTitle: $("aiDetailTitle"),
  aiDetailMeta: $("aiDetailMeta"), aiCapPath: $("aiCapPath"), aiPrimaryPath: $("aiPrimaryPath"),
  aiWarmPath: $("aiWarmPath"), aiMarketLeader: $("aiMarketLeader"), aiStateAssessment: $("aiStateAssessment"),
  aiMarketAssessment: $("aiMarketAssessment"), aiTemperatureThesis: $("aiTemperatureThesis"),
  aiUncertainty: $("aiUncertainty"), aiNextReview: $("aiNextReview"), aiActionRows: $("aiActionRows"),
  aiFillCount: $("aiFillCount"), aiFillRows: $("aiFillRows"),
  aiPaperBreakdown: $("aiPaperBreakdown"),
  dualStrategyStatus: $("dualStrategyStatus"), dualStrategyUpdated: $("dualStrategyUpdated"), dualReviewsToday: $("dualReviewsToday"),
  dualReviewMeta: $("dualReviewMeta"), dualFillsToday: $("dualFillsToday"), dualOpenCost: $("dualOpenCost"), dualOpenPositions: $("dualOpenPositions"),
  dualRealizedPnl: $("dualRealizedPnl"), dualStrategyCards: $("dualStrategyCards"), dualReviewCount: $("dualReviewCount"), dualActionCount: $("dualActionCount"),
  dualReviewRows: $("dualReviewRows"), dualActionRows: $("dualActionRows"), dualNoBuyCount: $("dualNoBuyCount"), dualNoBuyRows: $("dualNoBuyRows"),
  ladderV4State: $("ladderV4State"), ladderV4Updated: $("ladderV4Updated"),
  ladderV4Dates: $("ladderV4Dates"), ladderV4Checkpoint: $("ladderV4Checkpoint"),
  ladderV4Selections: $("ladderV4Selections"), ladderV4Unresolved: $("ladderV4Unresolved"),
  ladderV4Roi: $("ladderV4Roi"), ladderV4WinDates: $("ladderV4WinDates"),
  ladderV4Pnl: $("ladderV4Pnl"), ladderV4Cost: $("ladderV4Cost"),
  ladderV4LowerBound: $("ladderV4LowerBound"), ladderV4Phase: $("ladderV4Phase"),
  ladderV4Progress: $("ladderV4Progress"), ladderV4Today: $("ladderV4Today"),
  ladderV4TodayMeta: $("ladderV4TodayMeta"), ladderV4HistoryDates: $("ladderV4HistoryDates"),
  ladderV4HistoryRoi: $("ladderV4HistoryRoi"), ladderV4HistoryPnl: $("ladderV4HistoryPnl"),
  ladderV4HistoryLower: $("ladderV4HistoryLower"), ladderV4HistoryOutcomes: $("ladderV4HistoryOutcomes"),
  ladderV4Robustness: $("ladderV4Robustness"), ladderV4RecordCount: $("ladderV4RecordCount"),
  ladderV4Rows: $("ladderV4Rows"),
};

const COLORS = ["#416b83", "#a4483f", "#34745a", "#8a6c32", "#765a82", "#557a42", "#8b5d46"];
const state = {
  overview: null, accuracy: null, quality: null, ai: null, dual: null, ladderV4: null, selectedEvent: null, selectedAiCycle: null,
  detail: null, auto: true, timer: null, activeMarkets: new Set(),
  accuracyExpanded: localStorage.getItem("weather-dashboard:accuracy-expanded") === "true",
};

function setClock() {
  const time = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date());
  els.clock.textContent = `北京时间 ${time}`;
}

function formatTime(raw, withDate = true) {
  if (!raw) return "--";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return String(raw);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", month: withDate ? "2-digit" : undefined, day: withDate ? "2-digit" : undefined,
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
}

function peakTime(raw) {
  if (!raw) return "--";
  const match = String(raw).match(/T(\d{2}:\d{2})/);
  return match ? match[1] : raw;
}

function num(value, digits = 2) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "--";
}

function temperature(value, digits = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${parsed.toFixed(digits)}°` : "--";
}

function percent(value, digits = 1) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(digits)}%` : "--";
}

function cents(value, digits = 1) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(digits)}c` : "--";
}

function money(value, signed = false) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "--";
  const sign = signed && parsed > 0 ? "+" : "";
  return `${sign}${parsed.toFixed(2)} USDC`;
}

function deltaMeta(value, suffix = "") {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || Math.abs(parsed) < 0.0005) return { text: "→ 0", cls: "flat" };
  return parsed > 0
    ? { text: `↑ +${parsed.toFixed(suffix === "%" ? 3 : 2)}${suffix}`, cls: "up" }
    : { text: `↓ ${parsed.toFixed(suffix === "%" ? 3 : 2)}${suffix}`, cls: "down" };
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function sparkline(points = []) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("sparkline");
  svg.setAttribute("viewBox", "0 0 84 30");
  const values = points.map((point) => Number(point.value)).filter(Number.isFinite);
  if (!values.length) return svg;
  const min = Math.min(...values), max = Math.max(...values), spread = max - min || 1;
  const coords = values.map((value, index) => {
    const x = values.length === 1 ? 42 : 3 + index * 78 / (values.length - 1);
    const y = 26 - (value - min) / spread * 22;
    return [x, y];
  });
  const path = document.createElementNS(svg.namespaceURI, "path");
  path.setAttribute("d", coords.map(([x, y], index) => `${index ? "L" : "M"}${x},${y}`).join(" "));
  svg.append(path);
  const last = coords.at(-1);
  const circle = document.createElementNS(svg.namespaceURI, "circle");
  circle.setAttribute("cx", last[0]); circle.setAttribute("cy", last[1]); circle.setAttribute("r", "2.5");
  svg.append(circle);
  return svg;
}

function renderSummary(data) {
  const s = data.summary;
  els.cityCount.textContent = s.city_count;
  els.forecastCoverage.textContent = `${s.forecasted_count}/${s.city_count} 有 Meteoblue`;
  els.changedCount.textContent = s.changed_count;
  els.priceChangedCount.textContent = s.price_changed_count;
  els.averageTemp.textContent = temperature(s.average_max_c, 1);
  els.modelFreshness.textContent = s.latest_model_update ? `模型 ${formatTime(s.latest_model_update)}` : "等待模型数据";
  els.updatedAt.textContent = data.latest_run ? `采集 ${formatTime(data.latest_run.completed_at_utc)}` : "暂无采集";
}

function qualityStatus(status) {
  const value = String(status || "missing");
  return { text: value === "ok" ? "OK" : value === "missing" ? "缺失" : "异常", cls: value === "ok" ? "ok" : value === "missing" ? "missing" : "error" };
}

function renderQuality() {
  const data = state.quality || { summary: {}, cities: [] };
  const summary = data.summary || {};
  els.qualityRun.textContent = data.run_id ? `RUN ${data.run_id}` : "尚无 run";
  els.qualitySlot.textContent = data.slot_utc ? `采集 ${formatTime(data.slot_utc)}` : "--";
  els.qualityComplete.textContent = `${summary.complete_cities || 0}/${summary.cities || 0}`;
  els.qualityModels.textContent = `${summary.external_models_ok || 0}/${summary.external_models_expected || 0}`;
  els.qualityMetar.textContent = `${summary.metar_ok || 0}/${summary.cities || 0}`;
  els.qualityCurrent.textContent = `${summary.open_meteo_current_ok || 0}/${summary.cities || 0}`;
  els.qualityRaw.textContent = summary.raw_payloads_ok ?? "--";
  els.qualityRows.replaceChildren();
  const cities = data.cities || [];
  if (!cities.length) {
    const tr = el("tr"); const td = el("td", "loading-cell", "暂无质量记录"); td.colSpan = 6; tr.append(td); els.qualityRows.append(tr); return;
  }
  cities.forEach((city) => {
    const tr = el("tr");
    const cityCell = el("td");
    cityCell.append(el("strong", "quality-city", city.city || "--"), el("small", "quality-station", `${city.station_id || "--"} · ${city.target_date || "--"}`));
    tr.append(cityCell);
    const mblue = el("td", "quality-temp");
    mblue.append(el("strong", "", Number.isFinite(Number(city.meteoblue_max_c)) ? temperature(city.meteoblue_max_c, 2) : "--"), el("small", "", "Meteoblue")); tr.append(mblue);
    const models = el("td", "model-stack");
    (city.external_models || []).forEach((model) => {
      const status = qualityStatus(model.status); const item = el("span", `model-chip ${status.cls}`);
      item.title = `${model.model}: ${model.status}${model.error ? ` · ${model.error}` : ""}`;
      item.append(el("b", "", model.model.split("_")[0].toUpperCase()), document.createTextNode(Number.isFinite(Number(model.max_c)) ? ` ${Number(model.max_c).toFixed(1)}°` : " --")); models.append(item);
    });
    tr.append(models);
    const obs = el("td", "observation-cell");
    const metar = city.observations?.metar || {}; const current = city.observations?.open_meteo_current || {};
    obs.append(el("strong", "", `METAR ${temperature(metar.temperature_c, 1)}`), el("small", "", `日高 ${temperature(metar.daily_max_c ?? current.daily_max_c, 1)} · 当前 ${temperature(current.temperature_c, 1)}`)); tr.append(obs);
    tr.append(el("td", "raw-count", `${city.raw_payload_count || 0}/3`));
    const stateCell = el("td"); const quality = qualityStatus(city.complete ? "ok" : "incomplete"); stateCell.append(el("span", `quality-badge ${quality.cls}`, city.complete ? "完整" : "需检查")); tr.append(stateCell);
    els.qualityRows.append(tr);
  });
}

function ladderStatus(status) {
  const labels = {
    NOT_STARTED: ["8月5日开始", "pending"],
    NOT_DUE: ["11:12后检查", "pending"],
    MISSING_CAPTURE: ["采集缺失", "error"],
    SELECTED_SHADOW: ["已选中", "ok"],
    NO_ELIGIBLE_SHADOW: ["无合格候选", "pending"],
  };
  const [text, cls] = labels[String(status || "")] || [String(status || "未知"), "pending"];
  return { text, cls };
}

function ladderPhase(phase) {
  return {
    DATA_QUALITY_ONLY: "前10日 · 只检查数据",
    WAIT_FOR_INTERIM: "等待15日中期报告",
    INTERIM_ONLY_NO_RULE_CHANGES: "中期观察 · 不改规则",
    FORMAL_REVIEW_DUE: "30日预注册复核",
  }[phase] || phase || "--";
}

function setMetricTone(node, value) {
  node.classList.remove("positive", "negative");
  const parsed = Number(value);
  if (Number.isFinite(parsed) && parsed !== 0) node.classList.add(parsed > 0 ? "positive" : "negative");
}

function renderLadderV4() {
  const data = state.ladderV4 || {};
  const summary = data.summary || {};
  const status = data.status || {};
  const currentState = ladderStatus(status.current_status);
  els.ladderV4State.textContent = currentState.text;
  els.ladderV4State.className = `system-state ${currentState.cls}`;
  els.ladderV4Updated.textContent = data.generated_at_utc ? `更新 ${formatTime(data.generated_at_utc)}` : "--";
  els.ladderV4Dates.textContent = summary.completed_dates ?? 0;
  els.ladderV4Checkpoint.textContent = `下一检查点 ${status.next_checkpoint_dates ?? 10} 日 · 还差 ${status.dates_remaining ?? 10}`;
  els.ladderV4Selections.textContent = `${summary.selected_dates ?? 0} / ${summary.no_eligible_dates ?? 0}`;
  els.ladderV4Unresolved.textContent = `${summary.unresolved_dates ?? 0} 个待结算`;
  els.ladderV4Roi.textContent = summary.net_roi == null ? "--" : percent(summary.net_roi);
  els.ladderV4WinDates.textContent = `${summary.profitable_dates ?? 0} 个盈利日期`;
  els.ladderV4Pnl.textContent = money(summary.total_net_pnl_usdc, true);
  els.ladderV4Cost.textContent = `累计净成本 ${money(summary.total_net_cost_usdc)}`;
  els.ladderV4LowerBound.textContent = summary.bootstrap_p05_net_pnl_per_date == null ? "--" : money(summary.bootstrap_p05_net_pnl_per_date, true);
  setMetricTone(els.ladderV4Roi, summary.net_roi);
  setMetricTone(els.ladderV4Pnl, summary.total_net_pnl_usdc);
  setMetricTone(els.ladderV4LowerBound, summary.bootstrap_p05_net_pnl_per_date);
  els.ladderV4Phase.textContent = ladderPhase(status.phase);
  const checkpoint = Number(status.next_checkpoint_dates) || 10;
  const completed = Number(summary.completed_dates) || 0;
  els.ladderV4Progress.style.width = `${Math.min(100, completed / checkpoint * 100)}%`;

  const current = data.current;
  if (current?.selection_status === "SELECTED_SHADOW") {
    const buckets = (current.legs || []).map((leg) => `${leg.outcomeRange} ×${num(leg.weight, 0)}`).join(" / ");
    const cities = current.cities?.length ? current.cities.join(" / ") : (current.city || "--");
    const count = current.selection_count || 1;
    els.ladderV4Today.textContent = `${cities} · ${count} 个影子组合${buckets ? ` · ${buckets}` : ""}`;
    els.ladderV4TodayMeta.textContent = `合计净成本 ${money(current.selected_cost_usdc)} · ${current.eligible_candidates || 0} 个合格城市 · ${current.completed ? "已结算" : "等待结算"}`;
  } else if (current?.selection_status === "NO_ELIGIBLE_SHADOW") {
    els.ladderV4Today.textContent = "今日无合格候选";
    els.ladderV4TodayMeta.textContent = `${current.candidate_count || 0} 个城市候选已纳入分母`;
  } else {
    els.ladderV4Today.textContent = currentState.text;
    els.ladderV4TodayMeta.textContent = status.current_status === "NOT_STARTED"
      ? `前向起点 ${data.rule?.forward_start_date || "2026-08-05"}`
      : "当日11:00冻结一次，不创建订单或仓位";
  }

  const history = data.historical || {};
  const metrics = history.metrics || {};
  const outcomes = history.outcome_legs || {};
  const lower = metrics.date_block_bootstrap_pnl_per_event?.p05;
  els.ladderV4HistoryDates.textContent = metrics.independent_dates ?? "--";
  els.ladderV4HistoryRoi.textContent = percent(metrics.roi);
  els.ladderV4HistoryPnl.textContent = money(metrics.total_pnl, true);
  els.ladderV4HistoryLower.textContent = Number.isFinite(Number(lower)) ? `${money(lower, true)} / 组合` : "--";
  const outcomeTotal = Object.values(outcomes).reduce((sum, value) => sum + (Number(value) || 0), 0);
  els.ladderV4HistoryOutcomes.textContent = outcomeTotal
    ? `中心 ${outcomes.center || 0} · 上侧 ${outcomes.upper || 0} · 下侧 ${outcomes.lower || 0} · 三桶外 ${outcomes.outside || 0}`
    : "44 个历史合格城市组合；这是无城市上限的探索基准。";
  const loocv = history.selection_pipeline_leave_one_date_out?.roi;
  const rolling = history.selection_pipeline_rolling_origin?.roi;
  els.ladderV4Robustness.textContent = Number.isFinite(Number(loocv)) || Number.isFinite(Number(rolling))
    ? `规则选择流程：留一日期 ${percent(loocv)} · 严格滚动 ${percent(rolling)}；历史结果不能替代前向验证。`
    : "历史基准仅说明所有合格城市同时纳入时的结果，不能替代 V4.1 前向验证。";

  els.ladderV4Rows.replaceChildren();
  const records = data.records || [];
  els.ladderV4RecordCount.textContent = `${records.length} 个城市组合记录`;
  if (!records.length) {
    const tr = el("tr"); const td = el("td", "loading-cell", "前向样本尚未开始"); td.colSpan = 7; tr.append(td); els.ladderV4Rows.append(tr); return;
  }
  records.forEach((record) => {
    const tr = el("tr");
    tr.append(el("td", "ladder-date", record.target_date || "--"));
    const stateCell = el("td");
    const stateMeta = ladderStatus(record.selection_status);
    stateCell.append(el("strong", `ladder-badge ${stateMeta.cls}`, stateMeta.text), el("small", "", record.city || "--")); tr.append(stateCell);
    const legs = (record.legs || []).map((leg) => `${leg.outcomeRange} ×${num(leg.weight, 0)}`).join(" / ");
    tr.append(el("td", "ladder-legs", legs || "--"));
    tr.append(el("td", "ladder-cost", record.selected_cost_usdc == null ? "--" : money(record.selected_cost_usdc)));
    tr.append(el("td", "ladder-candidates", `${record.eligible_candidates || 0}/${record.candidate_count || 0}`));
    tr.append(el("td", "ladder-resolution", record.completed ? (record.winning_range || "无交易") : "待结算"));
    const pnl = el("td", "ladder-pnl", record.completed ? money(record.hypothetical_pnl_usdc, true) : "--");
    setMetricTone(pnl, record.hypothetical_pnl_usdc); tr.append(pnl);
    els.ladderV4Rows.append(tr);
  });
}

function filteredCities() {
  const query = els.citySearch.value.trim().toLocaleLowerCase();
  const changedOnly = els.changedOnly.checked;
  const rows = [...(state.overview?.cities || [])].filter((city) => {
    if (query && !`${city.city} ${city.station_name}`.toLocaleLowerCase().includes(query)) return false;
    if (changedOnly && Math.abs(city.forecast.delta_c || 0) < 0.005 && Math.abs(city.market.price_delta || 0) < 0.0005) return false;
    return true;
  });
  const sort = els.sortSelect.value;
  if (sort === "temp") rows.sort((a, b) => (b.forecast.raw_c ?? -999) - (a.forecast.raw_c ?? -999));
  else if (sort === "change") rows.sort((a, b) => Math.abs(b.forecast.delta_c || 0) - Math.abs(a.forecast.delta_c || 0));
  else if (sort === "price") rows.sort((a, b) => Math.abs(b.market.price_delta || 0) - Math.abs(a.market.price_delta || 0));
  else rows.sort((a, b) => a.rank - b.rank);
  return rows;
}

function renderCities() {
  els.cityRows.replaceChildren();
  const cities = filteredCities();
  if (!cities.length) {
    const row = el("tr"); const cell = el("td", "loading-cell", "没有符合条件的城市"); cell.colSpan = 9; row.append(cell); els.cityRows.append(row); return;
  }
  cities.forEach((city) => {
    const row = el("tr", city.event_id === state.selectedEvent ? "selected" : "");
    row.dataset.eventId = city.event_id;
    const nameCell = el("td");
    const wrap = el("div", "city-name");
    wrap.append(el("span", "city-rank", String(city.rank).padStart(2, "0")));
    const labels = el("div"); labels.append(el("strong", "", city.city)); labels.append(el("small", "", `${city.target_date} · ${city.station_id || "--"}`));
    wrap.append(labels); nameCell.append(wrap); row.append(nameCell);

    const tempCell = el("td"); tempCell.append(el("span", "temp-value", temperature(city.forecast.display_c)));
    tempCell.append(el("small", "raw-value", Number.isFinite(Number(city.forecast.raw_c)) ? `${num(city.forecast.raw_c, 2)}°C raw` : "暂无预测")); row.append(tempCell);

    const baselineCell = el("td", "baseline-cell");
    const baseline = city.forecast.baseline_0730;
    baselineCell.append(el("span", "baseline-value", baseline ? temperature(baseline.display_c) : "--"));
    baselineCell.append(el("small", "raw-value", baseline ? `${num(baseline.raw_c, 2)}°C · 当地 07:30` : "等待 07:30 采样"));
    row.append(baselineCell);

    const tempDelta = deltaMeta(city.forecast.delta_c, "°"); row.append(el("td", `delta ${tempDelta.cls}`, tempDelta.text));
    const bucketCell = el("td"); bucketCell.append(el("span", "pill", city.market.predicted_bucket || "--")); row.append(bucketCell);
    row.append(el("td", "", percent(city.market.predicted_yes)));
    const priceDelta = deltaMeta(city.market.price_delta, "%");
    priceDelta.text = Number.isFinite(Number(city.market.price_delta)) ? `${Number(city.market.price_delta) >= 0 ? "↑ +" : "↓ "}${(Number(city.market.price_delta) * 100).toFixed(1)}pp` : "--";
    row.append(el("td", `delta ${priceDelta.cls}`, priceDelta.text));
    const sparkCell = el("td"); sparkCell.append(sparkline(city.forecast.spark)); row.append(sparkCell);
    row.append(el("td", "", peakTime(city.forecast.peak_local)));
    row.addEventListener("click", () => selectCity(city.event_id));
    els.cityRows.append(row);
  });
}

function renderChangeFeed() {
  els.changeFeed.replaceChildren();
  const changes = [];
  (state.overview?.cities || []).forEach((city) => {
    if (Math.abs(city.forecast.delta_c || 0) >= 0.005) changes.push({
      city: city.city, time: city.forecast.slot_utc, kind: "预测", value: city.forecast.delta_c,
      text: `最高温 ${city.forecast.delta_c > 0 ? "上调" : "下调"} ${Math.abs(city.forecast.delta_c).toFixed(2)}°C，现为 ${city.forecast.raw_c.toFixed(2)}°C`,
    });
    if (Math.abs(city.market.price_delta || 0) >= 0.0005) changes.push({
      city: city.city, time: city.market.slot_utc, kind: "盘口", value: city.market.price_delta,
      text: `${city.market.predicted_bucket || "预测档"} YES ${city.market.price_delta > 0 ? "上涨" : "下跌"} ${Math.abs(city.market.price_delta * 100).toFixed(1)}pp`,
    });
  });
  changes.sort((a, b) => new Date(b.time || 0) - new Date(a.time || 0));
  els.changeStatus.textContent = `${changes.length} 条`;
  if (!changes.length) {
    els.changeFeed.append(el("div", "change-empty", "当前还没有 Meteoblue 修订记录。下一轮采集后，温度或盘口变化会出现在这里。"));
    return;
  }
  changes.slice(0, 24).forEach((change) => {
    const item = el("div", "change-item");
    const head = el("div", "change-item-head"); head.append(el("strong", "", `${change.city} · ${change.kind}`)); head.append(el("time", "", formatTime(change.time)));
    item.append(head, el("p", "", change.text)); els.changeFeed.append(item);
  });
}

function renderAccuracy() {
  const payload = state.accuracy || { summary: {}, records: [] };
  const summary = payload.summary || {};
  els.accuracyHit.textContent = summary.accurate ?? 0;
  els.accuracyMiss.textContent = summary.inaccurate ?? 0;
  els.accuracyPending.textContent = summary.pending ?? 0;
  els.accuracyMae.textContent = Number.isFinite(Number(summary.mean_absolute_error_c))
    ? `${Number(summary.mean_absolute_error_c).toFixed(2)}°C`
    : "--";
  const resolved = Number(summary.resolved || 0);
  const accurate = Number(summary.accurate || 0);
  els.accuracyQuick.textContent = resolved ? `${Math.round(accurate / resolved * 100)}%` : "待结算";
  els.accuracyQuickHint.textContent = resolved
    ? `${accurate}/${resolved} 命中 · ${summary.pending || 0} 待结算`
    : `${summary.pending || 0} 个城市等待结算`;
  els.accuracyCollapsedSummary.textContent = resolved
    ? `${accurate}/${resolved} 命中 · MAE ${Number.isFinite(Number(summary.mean_absolute_error_c)) ? `${Number(summary.mean_absolute_error_c).toFixed(2)}°C` : "--"}`
    : `${summary.pending || 0} 个城市待结算`;

  const selectedDate = els.accuracyDate.value;
  const dates = [...new Set((payload.records || []).map((row) => row.target_date).filter(Boolean))].sort().reverse();
  els.accuracyDate.replaceChildren();
  const allOption = el("option", "", "全部日期"); allOption.value = "all"; els.accuracyDate.append(allOption);
  dates.forEach((date) => { const option = el("option", "", date); option.value = date; els.accuracyDate.append(option); });
  els.accuracyDate.value = dates.includes(selectedDate) ? selectedDate : "all";

  const dateFilter = els.accuracyDate.value;
  const statusFilter = els.accuracyFilter.value;
  const rows = (payload.records || []).filter((row) =>
    (dateFilter === "all" || row.target_date === dateFilter)
    && (statusFilter === "all" || row.accuracy === statusFilter)
  );
  els.accuracyRows.replaceChildren();
  if (!rows.length) {
    const tr = el("tr"); const td = el("td", "loading-cell", "该筛选条件下暂无记录"); td.colSpan = 6; tr.append(td); els.accuracyRows.append(tr); return;
  }
  rows.forEach((record) => {
    const tr = el("tr");
    const cityCell = el("td"); const city = el("div", "accuracy-city"); city.append(el("strong", "", record.city), el("small", "", `${record.target_date} · ${record.station_id}`)); cityCell.append(city); tr.append(cityCell);
    tr.append(el("td", "", Number.isFinite(Number(record.forecast_max_c)) ? `${Number(record.forecast_max_c).toFixed(2)}°C` : "--"));
    tr.append(el("td", "", record.winning_range || "待结算"));
    const errorCell = el("td"); const meter = el("span", "error-meter");
    let errorText = "--", errorValue = null;
    if (record.difference_kind === "exact" && Number.isFinite(Number(record.absolute_error_c))) {
      errorValue = Number(record.absolute_error_c); errorText = `${errorValue.toFixed(2)}°C`;
    } else if (record.difference_kind === "minimum_to_range" && Number.isFinite(Number(record.minimum_distance_to_winning_range_c))) {
      errorValue = Number(record.minimum_distance_to_winning_range_c); errorText = `至少 ${errorValue.toFixed(2)}°C`;
    }
    if (errorValue !== null) { const bar = el("i"); bar.style.setProperty("--error-width", `${Math.min(errorValue * 12, 46)}px`); meter.append(bar); }
    meter.append(document.createTextNode(errorText)); errorCell.append(meter); tr.append(errorCell);
    const statusCell = el("td");
    const labels = { accurate: "准确", inaccurate: "不准", pending: "待结算" };
    statusCell.append(el("span", `accuracy-result ${record.accuracy || "pending"}`, labels[record.accuracy] || "待结算")); tr.append(statusCell);
    const offset = Number(record.sample_offset_from_0730);
    tr.append(el("td", "", Number.isFinite(offset) ? `${offset >= 0 ? "+" : ""}${offset.toFixed(1)}h` : "--"));
    els.accuracyRows.append(tr);
  });
}

function setAccuracyExpanded(expanded, persist = true) {
  state.accuracyExpanded = Boolean(expanded);
  els.accuracyBody.hidden = !state.accuracyExpanded;
  els.accuracyToggle.setAttribute("aria-expanded", String(state.accuracyExpanded));
  const verb = state.accuracyExpanded ? "收起" : "展开";
  els.accuracyToggle.title = `${verb}每日预测准确性`;
  els.accuracyToggle.setAttribute("aria-label", `${verb}每日预测准确性`);
  if (persist) localStorage.setItem("weather-dashboard:accuracy-expanded", String(state.accuracyExpanded));
}

function modeLabel(mode) {
  return String(mode || "UNKNOWN").toUpperCase();
}

function ridgeStatusLabel(ridge) {
  if (ridge.status === "ok") return `${ridge.primaryBucketC}°C 主路径`;
  if (ridge.status === "not_due") return "10:00后计算";
  if (ridge.status === "unavailable") return "计算不可用";
  return "未计算";
}

function actionLabel(action) {
  const labels = { observe: "观察", buy: "买入", sell: "卖出", hold: "持有", reject: "拒绝", skip: "跳过" };
  const value = String(action || "--").toLowerCase();
  return labels[value] || String(action || "--").toUpperCase();
}

function cycleExecutionSummary(cycle) {
  const actions = cycle.actions || [];
  const fills = actions.flatMap((action) => action.fills || []);
  if (fills.length) return { text: `${fills.length} 笔成交`, cls: "filled" };
  const rejected = actions.filter((action) => action.rejection_reason || action.executed_action === "reject");
  if (rejected.length) return { text: `${rejected.length} 笔拒绝`, cls: "rejected" };
  const executed = actions.map((action) => actionLabel(action.executed_action)).filter((value) => value !== "--");
  return { text: executed.length ? [...new Set(executed)].join(" / ") : "无动作", cls: "observed" };
}

function ridgePath(ridge, kind) {
  if (!ridge || ridge.status !== "ok") return ridge?.reason || "尚未到计算时点";
  const value = kind === "cap" ? ridge.cappingPathC : kind === "warm" ? ridge.warmTailPathC : ridge.primaryPathC;
  const bucket = kind === "cap" ? ridge.cappingBucketC : kind === "warm" ? ridge.warmTailBucketC : ridge.primaryBucketC;
  return Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)}°C → ${bucket}°C` : "--";
}

function renderAiSummary() {
  const payload = state.ai || { summary: {}, cycles: [] };
  const summary = payload.summary || {};
  const run = payload.latest_run || {};
  els.aiAgentStatus.textContent = payload.paper_only ? `PAPER · ${run.status || "未知"}` : (run.status || "未知");
  els.aiAgentStatus.className = `system-state ${run.status === "completed" ? "ok" : run.status === "failed" ? "error" : "pending"}`;
  els.aiUpdatedAt.textContent = payload.generated_at_utc ? `数据 ${formatTime(payload.generated_at_utc)}` : "--";
  els.aiLatestMode.textContent = modeLabel(summary.latest_mode);
  els.aiLatestMode.className = `mode-text mode-${modeLabel(summary.latest_mode).toLowerCase()}`;
  els.aiLatestCity.textContent = summary.latest_city ? `${summary.latest_city} · ${formatTime(summary.latest_decision_at_utc)}` : "尚无决策";
  els.aiCyclesToday.textContent = summary.cycles_today ?? 0;
  els.aiModeCounts.textContent = Object.entries(summary.mode_counts || {}).map(([key, value]) => `${key} ${value}`).join(" · ") || "暂无模式统计";
  els.aiFillsToday.textContent = summary.fills_today ?? 0;
  els.aiAvailableCash.textContent = money(summary.available_cash_usdc);
  els.aiOpenPositions.textContent = `${summary.open_positions || 0} 个持仓 · 成本 ${money(summary.open_cost_basis_usdc)}`;
  els.aiRealizedPnl.textContent = money(summary.realized_pnl_usdc, true);
  els.aiRealizedPnl.className = Number(summary.realized_pnl_usdc) > 0 ? "positive" : Number(summary.realized_pnl_usdc) < 0 ? "negative" : "";
  renderNoPaperBreakdown(payload.paper_breakdown || []);
  renderAiRecentFills(payload.recent_fills || []);

  const selectedCity = els.aiCityFilter.value;
  const cities = [...new Set((payload.cycles || []).map((cycle) => cycle.city).filter(Boolean))].sort();
  els.aiCityFilter.replaceChildren();
  const all = el("option", "", "全部城市"); all.value = "all"; els.aiCityFilter.append(all);
  cities.forEach((city) => { const option = el("option", "", city); option.value = city; els.aiCityFilter.append(option); });
  els.aiCityFilter.value = cities.includes(selectedCity) ? selectedCity : "all";
}

function renderNoPaperBreakdown(rows) {
  const labels = {
    NO_OVERSHOOT: ["越档 NO", "升温路径穿过目标档"],
    NO_CEILING: ["封顶 NO", "天气过程封在目标档下方"],
    NO_MARKET_TAIL_REJECTION: ["尾部否定 NO", "实时机制否定市场尾部"],
  };
  els.aiPaperBreakdown.replaceChildren();
  rows.forEach((item) => {
    const [title, description] = labels[item.entry_type] || [item.entry_type, "NO Paper"];
    const article = el("article", "no-paper-item");
    const head = el("div", "no-paper-head");
    head.append(el("strong", "", title), el("code", "", item.entry_type));
    const pnl = el("b", Number(item.realized_pnl_usdc) > 0 ? "positive" : Number(item.realized_pnl_usdc) < 0 ? "negative" : "", money(item.realized_pnl_usdc, true));
    const settled = Number(item.settled || 0);
    const wins = Number(item.wins || 0);
    article.append(
      head,
      el("p", "", description),
      pnl,
      el("small", "", `${item.signals || 0} 笔信号 · 已结算 ${settled} · ${wins}胜${Math.max(0, settled - wins)}负 · 未结算成本 ${money(item.open_cost_usdc)}`),
    );
    els.aiPaperBreakdown.append(article);
  });
}

function filteredAiCycles() {
  return (state.ai?.cycles || []).filter((cycle) =>
    (els.aiCityFilter.value === "all" || cycle.city === els.aiCityFilter.value)
    && (els.aiModeFilter.value === "all" || modeLabel(cycle.market_decision_mode) === els.aiModeFilter.value)
  );
}

function renderAiCycles() {
  const cycles = filteredAiCycles();
  els.aiCycleCount.textContent = `显示 ${cycles.length} / ${state.ai?.cycles?.length || 0} 次分析`;
  els.aiCycleRows.replaceChildren();
  if (!cycles.length) {
    const row = el("tr"); const cell = el("td", "loading-cell", "该筛选条件下暂无 AI 决策"); cell.colSpan = 6; row.append(cell); els.aiCycleRows.append(row);
    renderAiDecision(null); return;
  }
  if (!cycles.some((cycle) => cycle.cycle_id === state.selectedAiCycle)) state.selectedAiCycle = cycles[0].cycle_id;
  cycles.forEach((cycle) => {
    const row = el("tr", cycle.cycle_id === state.selectedAiCycle ? "selected" : "");
    row.tabIndex = 0; row.setAttribute("role", "button"); row.setAttribute("aria-label", `查看 ${cycle.city} 的 AI 决策`);
    const city = el("td", "ai-city-cell"); city.append(el("strong", "", `${formatTime(cycle.trigger_slot_utc || cycle.decision_trigger_time_utc)} · ${cycle.city}`), el("small", "", `返回 ${formatTime(cycle.analyzed_at_utc)} · #${cycle.cycle_id}`)); row.append(city);
    row.append(el("td", "trigger-cell", cycle.decision_trigger_type || "--"));
    const modeCell = el("td"); modeCell.append(el("span", `mode-badge mode-${modeLabel(cycle.market_decision_mode).toLowerCase()}`, modeLabel(cycle.market_decision_mode))); row.append(modeCell);
    const ridge = cycle.ridge_v2 || {}; const ridgeCell = el("td", "ridge-cell");
    ridgeCell.append(el("strong", "", ridgeStatusLabel(ridge)), el("small", "", ridge.status === "ok" ? `${ridge.cappingBucketC} / ${ridge.primaryBucketC} / ${ridge.warmTailBucketC}°C` : ridge.reason || "--")); row.append(ridgeCell);
    const requested = (cycle.actions || []).map((action) => actionLabel(action.requested_action)).filter(Boolean);
    row.append(el("td", "request-cell", requested.length ? [...new Set(requested)].join(" / ") : "无请求"));
    const execution = cycleExecutionSummary(cycle); const executionCell = el("td"); executionCell.append(el("span", `execution-badge ${execution.cls}`, execution.text)); row.append(executionCell);
    const select = () => { state.selectedAiCycle = cycle.cycle_id; renderAiCycles(); renderAiDecision(cycle); };
    row.addEventListener("click", select); row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); } });
    els.aiCycleRows.append(row);
  });
  renderAiDecision(cycles.find((cycle) => cycle.cycle_id === state.selectedAiCycle) || cycles[0]);
}

function renderAiDecision(cycle) {
  if (!cycle) {
    els.aiDetailTitle.textContent = "没有符合条件的决策"; els.aiDetailMeta.textContent = "--";
    [els.aiCapPath, els.aiPrimaryPath, els.aiWarmPath, els.aiMarketLeader].forEach((node) => { node.textContent = "--"; });
    [els.aiStateAssessment, els.aiMarketAssessment, els.aiTemperatureThesis, els.aiUncertainty].forEach((node) => { node.textContent = "暂无决策"; });
    els.aiNextReview.textContent = ""; els.aiActionRows.replaceChildren(); return;
  }
  const ridge = cycle.ridge_v2 || {}; const alignment = cycle.market_alignment || {}; const leader = alignment.marketLeader || {};
  els.aiDetailTitle.textContent = `${cycle.city} · ${modeLabel(cycle.market_decision_mode)}`;
  els.aiDetailMeta.textContent = `数据 ${formatTime(cycle.trigger_slot_utc || cycle.decision_trigger_time_utc)} · 返回 ${formatTime(cycle.analyzed_at_utc)} · ${cycle.decision_trigger_type || "未知触发"} · cycle #${cycle.cycle_id}`;
  els.aiCapPath.textContent = ridgePath(ridge, "cap"); els.aiPrimaryPath.textContent = ridgePath(ridge, "primary"); els.aiWarmPath.textContent = ridgePath(ridge, "warm");
  els.aiMarketLeader.textContent = leader.outcomeRange ? `${leader.outcomeRange} · YES ${cents(leader.yesExecutableBuyPrice5 ?? leader.yesMidpoint)}` : "暂无市场主档";
  els.aiStateAssessment.textContent = cycle.state_assessment || cycle.weather_process_assessment || "暂无判断";
  els.aiMarketAssessment.textContent = cycle.market_consensus_assessment || alignment.reason || "暂无判断";
  els.aiTemperatureThesis.textContent = cycle.temperature_thesis || "暂无判断";
  els.aiUncertainty.textContent = cycle.uncertainty_assessment || "暂无判断";
  els.aiNextReview.textContent = cycle.next_review_reason ? `下次复核：${cycle.next_review_reason}` : "";
  renderAiActions(cycle.actions || []);
}

function renderAiActions(actions) {
  els.aiActionRows.replaceChildren();
  if (!actions.length) { const row = el("tr"); const cell = el("td", "loading-cell", "AI 未提出动作"); cell.colSpan = 6; row.append(cell); els.aiActionRows.append(row); return; }
  actions.forEach((action) => {
    const row = el("tr");
    row.append(el("td", "action-request", actionLabel(action.requested_action)));
    const market = el("td", "action-market"); market.append(el("strong", "", action.outcome_range ? `${action.outcome_range} · ${action.outcome_side || "--"}` : "无目标盘口"), el("small", "", action.entry_type || "NONE")); row.append(market);
    row.append(el("td", "", action.requested_shares != null && Number.isFinite(Number(action.requested_shares)) ? `${num(action.requested_shares, 2)} / 执行 ${action.executed_shares != null ? num(action.executed_shares, 2) : "--"}` : "--"));
    const actionCost = Number(action.notional_usdc || 0) + Number(action.fee_usdc || 0);
    row.append(el("td", "", action.execution_price != null && Number.isFinite(Number(action.execution_price)) ? `${cents(action.execution_price)} · ${money(actionCost)}` : "--"));
    const fills = action.fills || []; const actual = fills.length ? `${fills.length} 笔成交 · ${fills.reduce((sum, fill) => sum + Number(fill.shares || 0), 0).toFixed(2)} shares` : `${actionLabel(action.executed_action)} · 未成交`;
    const actualCell = el("td"); actualCell.append(el("span", `execution-badge ${fills.length ? "filled" : action.rejection_reason ? "rejected" : "observed"}`, actual)); row.append(actualCell);
    const reason = action.rejection_reason || action.thesis || "无补充理由"; const reasonCell = el("td", "action-reason", reason);
    if (action.key_risk) reasonCell.title = `关键风险：${action.key_risk}`; row.append(reasonCell);
    els.aiActionRows.append(row);
  });
}

function renderAiRecentFills(fills) {
  const rows = fills.slice(0, 16);
  els.aiFillCount.textContent = `${rows.length} / ${fills.length} 笔`;
  els.aiFillRows.replaceChildren();
  if (!rows.length) { const row = el("tr"); const cell = el("td", "loading-cell", "尚无 Paper 成交"); cell.colSpan = 7; row.append(cell); els.aiFillRows.append(row); return; }
  rows.forEach((fill) => {
    const row = el("tr");
    row.append(el("td", "fill-time", formatTime(fill.filled_at_utc)));
    row.append(el("td", "", fill.city || "--"));
    row.append(el("td", `fill-side side-${String(fill.side || "unknown").toLowerCase()}`, `${fill.side || "--"} ${fill.outcome_side || ""}`.trim()));
    const rangeCell = el("td");
    rangeCell.append(el("span", "", fill.outcome_range || (fill.fill_type === "settlement" ? "结算" : fill.market_id || "--")));
    if (fill.entry_type) rangeCell.append(el("small", "fill-entry-type", fill.entry_type));
    row.append(rangeCell);
    row.append(el("td", "", fill.shares != null ? num(fill.shares, 2) : "--"));
    row.append(el("td", "", fill.price != null ? cents(fill.price) : "--"));
    const pnl = el("td", Number(fill.realized_pnl_usdc) > 0 ? "positive" : Number(fill.realized_pnl_usdc) < 0 ? "negative" : "", money(fill.realized_pnl_usdc, true)); row.append(pnl);
    els.aiFillRows.append(row);
  });
}

function renderDualStrategy() {
  const payload = state.dual || { summary: {}, strategies: [], reviews: [], actions: [] };
  const summary = payload.summary || {};
  const run = payload.latest_run || {};
  els.dualStrategyStatus.textContent = payload.paper_only ? `PAPER · ${run.status || "等待"}` : "LIVE 禁止";
  els.dualStrategyStatus.className = `system-state ${payload.paper_only ? (run.status === "running" ? "ok" : "pending") : "error"}`;
  els.dualStrategyUpdated.textContent = payload.generated_at_utc ? formatTime(payload.generated_at_utc) : "--";
  els.dualReviewsToday.textContent = summary.reviews_today ?? 0;
  els.dualReviewMeta.textContent = `三桶 ${summary.three_bucket_reviews_today ?? 0} · 单桶 ${summary.single_no_reviews_today ?? 0}`;
  els.dualFillsToday.textContent = summary.fills_today ?? 0;
  els.dualOpenCost.textContent = money(summary.open_cost_usdc);
  els.dualOpenPositions.textContent = `${summary.open_positions ?? 0} 个未平仓`;
  els.dualRealizedPnl.textContent = money(summary.realized_pnl_usdc, true);
  els.dualRealizedPnl.className = Number(summary.realized_pnl_usdc) < 0 ? "negative" : Number(summary.realized_pnl_usdc) > 0 ? "positive" : "";
  els.dualStrategyCards.replaceChildren();
  (payload.strategies || []).forEach((strategy) => {
    const card = el("article", "dual-strategy-card");
    const title = el("div", "dual-card-title"); title.append(el("strong", "", strategy.label || strategy.strategy_type), el("code", "", strategy.strategy_type)); card.append(title);
    const grid = el("div", "dual-card-metrics");
    [["今日复核", strategy.reviews ?? 0], ["今日成交", strategy.fills ?? 0], ["未平仓", money(strategy.open_cost_usdc)], ["已实现", money(strategy.realized_pnl_usdc, true)]].forEach(([label, value]) => { const item = el("div"); item.append(el("span", "", label), el("b", "", String(value))); grid.append(item); });
    card.append(grid); els.dualStrategyCards.append(card);
  });
  const noBuys = (payload.fills || []).filter((fill) => fill.strategy_type === "SINGLE_NO" && fill.fill_type === "paper_buy");
  const noCities = [...new Set(noBuys.map((fill) => fill.city).filter(Boolean))];
  els.dualNoBuyCount.textContent = noBuys.length ? `${noBuys.length} 笔 · ${noCities.join("、")}` : "暂无 NO 买入";
  els.dualNoBuyRows.replaceChildren();
  if (!noBuys.length) { const row = el("tr"); const cell = el("td", "loading-cell", "当前没有实际执行的单桶 NO Paper 买入"); cell.colSpan = 7; row.append(cell); els.dualNoBuyRows.append(row); }
  noBuys.slice(0, 20).forEach((fill) => { const row = el("tr"); const allInCost = Number(fill.notional_usdc || 0) + Number(fill.fee_usdc || 0); row.append(el("td", "fill-time", formatTime(fill.filled_at_utc)), el("td", "dual-city", fill.city || "--"), el("td", "", fill.outcome_range || fill.market_id || "--"), el("td", "", fill.thesis || "--"), el("td", "", num(fill.shares, 2)), el("td", "", cents(fill.price)), el("td", "", money(allInCost))); els.dualNoBuyRows.append(row); });
  els.dualReviewCount.textContent = `${(payload.reviews || []).length} 条`;
  els.dualReviewRows.replaceChildren();
  const reviews = (payload.reviews || []).slice(0, 12);
  if (!reviews.length) { const row = el("tr"); const cell = el("td", "loading-cell", "尚无双策略复核"); cell.colSpan = 4; row.append(cell); els.dualReviewRows.append(row); }
  reviews.forEach((review) => { const row = el("tr"); row.append(el("td", "fill-time", formatTime(review.reviewed_at_utc)), el("td", "", review.city || "--"), el("td", "", review.trigger_type || "--")); const status = el("td", review.status === "completed" ? "positive" : "negative", review.status || "--"); row.append(status); els.dualReviewRows.append(row); });
  els.dualActionCount.textContent = `${(payload.actions || []).length} 条`;
  els.dualActionRows.replaceChildren();
  const actions = (payload.actions || []).slice(0, 12);
  if (!actions.length) { const row = el("tr"); const cell = el("td", "loading-cell", "尚无双策略动作"); cell.colSpan = 5; row.append(cell); els.dualActionRows.append(row); }
  actions.forEach((action) => { const row = el("tr"); row.append(el("td", "dual-city", action.city || "--"), el("td", "", `${action.strategy_type === "THREE_BUCKET" ? "三桶 YES" : "单桶 NO"} · ${action.outcome_range || action.market_id || "--"}`), el("td", "", action.outcome_side || "--"), el("td", "", action.executed_shares != null ? num(action.executed_shares, 2) : (action.requested_shares != null ? `申请 ${num(action.requested_shares, 2)}` : "--"))); const value = action.executed_action || "--"; const cell = el("td", value === "BUY" ? "positive" : value === "REJECTED" ? "negative" : "", value); row.append(cell); els.dualActionRows.append(row); });
}

function svgNode(name, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
}

function lineChart(container, series, options = {}) {
  container.replaceChildren(); container.classList.remove("empty-chart");
  const validSeries = series.map((item) => ({ ...item, points: item.points.filter((p) => Number.isFinite(Number(p.value)) && p.time) })).filter((item) => item.points.length);
  if (!validSeries.length) { container.classList.add("empty-chart"); container.textContent = options.empty || "等待历史样本"; return; }
  const width = Math.max(container.clientWidth || 500, 320), height = options.height || 230;
  const margin = { top: 14, right: 14, bottom: 28, left: 42 };
  const allPoints = validSeries.flatMap((item) => item.points);
  const times = allPoints.map((p) => new Date(p.time).getTime()).filter(Number.isFinite);
  let xMin = Math.min(...times), xMax = Math.max(...times); if (xMin === xMax) { xMin -= 1800000; xMax += 1800000; }
  const values = allPoints.map((p) => Number(p.value));
  let yMin = options.yMin ?? Math.min(...values), yMax = options.yMax ?? Math.max(...values);
  if (yMin === yMax) { yMin -= options.pad ?? 0.5; yMax += options.pad ?? 0.5; }
  else if (options.yMin === undefined) { const pad = (yMax - yMin) * .12; yMin -= pad; yMax += pad; }
  const x = (time) => margin.left + (new Date(time).getTime() - xMin) / (xMax - xMin) * (width - margin.left - margin.right);
  const y = (value) => margin.top + (yMax - value) / (yMax - yMin) * (height - margin.top - margin.bottom);
  const svg = svgNode("svg", { class: "chart-svg", viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none" });
  const crosshairX = svgNode("line", { class: "chart-crosshair", y1: margin.top, y2: height - margin.bottom });
  const crosshairY = svgNode("line", { class: "chart-crosshair", x1: margin.left, x2: width - margin.right });
  const focusDot = svgNode("circle", { class: "chart-focus-dot", r: 5 });
  for (let i = 0; i <= 4; i += 1) {
    const value = yMin + (yMax - yMin) * i / 4, yy = y(value);
    svg.append(svgNode("line", { class: "chart-gridline", x1: margin.left, x2: width - margin.right, y1: yy, y2: yy }));
    const label = svgNode("text", { class: "chart-axis", x: margin.left - 7, y: yy + 3, "text-anchor": "end" }); label.textContent = options.yFormat ? options.yFormat(value) : value.toFixed(1); svg.append(label);
  }
  [xMin, (xMin + xMax) / 2, xMax].forEach((time, index) => {
    const label = svgNode("text", { class: "chart-axis", x: x(time), y: height - 7, "text-anchor": index === 0 ? "start" : index === 2 ? "end" : "middle" });
    label.textContent = formatTime(new Date(time).toISOString(), false); svg.append(label);
  });
  validSeries.forEach((item, seriesIndex) => {
    const color = item.color || COLORS[seriesIndex % COLORS.length];
    const path = svgNode("path", { class: "chart-line", stroke: color, d: item.points.map((p, index) => `${index ? "L" : "M"}${x(p.time)},${y(Number(p.value))}`).join(" ") }); svg.append(path);
    item.points.forEach((point) => {
      const px = x(point.time), py = y(Number(point.value));
      const dot = svgNode("circle", { class: "chart-dot", cx: px, cy: py, r: 3, fill: "#fbfbf9", stroke: color });
      const hit = svgNode("circle", { class: "chart-hit-dot", cx: px, cy: py, r: 10, tabindex: 0 });
      hit._chartPoint = { ...point, name: item.name, color, px, py };
      svg.append(dot, hit);
    });
  });
  svg.append(crosshairX, crosshairY, focusDot);

  const tooltip = el("div", "chart-tooltip");
  tooltip.hidden = true;
  const tooltipTime = el("time", "chart-tooltip-time");
  const tooltipLine = el("div", "chart-tooltip-line");
  const tooltipSwatch = el("i", "chart-tooltip-swatch");
  const tooltipName = el("span", "chart-tooltip-name");
  const tooltipValue = el("strong", "chart-tooltip-value");
  tooltipLine.append(tooltipSwatch, tooltipName, tooltipValue);
  tooltip.append(tooltipTime, tooltipLine);

  const hideTooltip = () => {
    tooltip.hidden = true;
    crosshairX.classList.remove("visible"); crosshairY.classList.remove("visible"); focusDot.classList.remove("visible");
  };
  const showTooltip = (target, event) => {
    const point = target._chartPoint;
    if (!point) return;
    tooltipTime.textContent = `北京时间 ${formatTime(point.time)}`;
    tooltipName.textContent = point.name;
    tooltipValue.textContent = options.tooltipFormat ? options.tooltipFormat(Number(point.value)) : String(point.value);
    tooltipSwatch.style.background = point.color;
    crosshairX.setAttribute("x1", point.px); crosshairX.setAttribute("x2", point.px);
    crosshairY.setAttribute("y1", point.py); crosshairY.setAttribute("y2", point.py);
    focusDot.setAttribute("cx", point.px); focusDot.setAttribute("cy", point.py); focusDot.setAttribute("stroke", point.color);
    crosshairX.classList.add("visible"); crosshairY.classList.add("visible"); focusDot.classList.add("visible");
    tooltip.hidden = false;
    const bounds = container.getBoundingClientRect();
    const anchorX = event?.clientX != null ? event.clientX - bounds.left : point.px / width * bounds.width;
    const anchorY = event?.clientY != null ? event.clientY - bounds.top : point.py / height * bounds.height;
    const left = Math.min(Math.max(anchorX + 14, 8), Math.max(8, bounds.width - tooltip.offsetWidth - 8));
    const above = anchorY - tooltip.offsetHeight - 12;
    const top = above >= 8 ? above : Math.min(anchorY + 14, bounds.height - tooltip.offsetHeight - 8);
    tooltip.style.left = `${left}px`; tooltip.style.top = `${Math.max(8, top)}px`;
  };
  svg.querySelectorAll(".chart-hit-dot").forEach((hit) => {
    hit.addEventListener("pointerenter", (event) => showTooltip(hit, event));
    hit.addEventListener("pointermove", (event) => showTooltip(hit, event));
    hit.addEventListener("pointerleave", hideTooltip);
    hit.addEventListener("focus", () => showTooltip(hit));
    hit.addEventListener("blur", hideTooltip);
  });
  container.append(svg, tooltip);
}

function statChip(label, value) {
  const chip = el("div", "stat-chip"); chip.append(el("span", "", label), el("strong", "", value)); return chip;
}

function renderDetail() {
  const detail = state.detail;
  if (!detail) return;
  const event = detail.event;
  const city = state.overview.cities.find((item) => item.event_id === event.event_id);
  els.detailTitle.textContent = `${event.city} · ${event.target_date}`;
  els.detailMeta.textContent = `${event.station_name || event.station_id} · ${event.timezone || "--"} · 结算 ${event.resolution_source || "--"}`;
  els.detailStats.replaceChildren();
  els.detailStats.append(
    statChip("Meteoblue", city?.forecast.raw_c != null ? `${num(city.forecast.raw_c, 2)}°C` : "--"),
    statChip("页面显示", temperature(city?.forecast.display_c)),
    statChip("预测档 YES", percent(city?.market.predicted_yes)),
  );

  els.forecastChartValue.textContent = detail.forecast_history.length ? `${detail.forecast_history.length} 个样本` : "--";
  lineChart(els.forecastChart, [{ name: "预测最高温", color: COLORS[0], points: detail.forecast_history.map((p) => ({ time: p.time, value: p.max_c })) }], {
    yFormat: (v) => `${v.toFixed(1)}°`, tooltipFormat: (v) => `${v.toFixed(2)}°C`, empty: "等待下一轮形成修订趋势",
  });

  const profilePoints = detail.hourly_profile.map((p) => ({ time: p.time_local || p.time_utc, value: p.temp_c }));
  const profileMax = profilePoints.length ? Math.max(...profilePoints.map((p) => Number(p.value))) : null;
  els.profileChartValue.textContent = Number.isFinite(profileMax) ? `峰值 ${profileMax.toFixed(2)}°C` : "--";
  lineChart(els.profileChart, [{ name: "逐小时温度", color: COLORS[2], points: profilePoints }], {
    yFormat: (v) => `${v.toFixed(0)}°`, tooltipFormat: (v) => `${v.toFixed(2)}°C`, empty: "暂无逐小时数据",
  });

  const sortedSeries = [...detail.price_history].sort((a, b) => (b.latest ?? -1) - (a.latest ?? -1));
  if (!state.activeMarkets.size) sortedSeries.slice(0, 5).forEach((item) => state.activeMarkets.add(item.market_id));
  renderPriceLegend(sortedSeries);
  renderPriceChart(sortedSeries);
  renderBuckets(detail.markets, city);
}

function renderPriceLegend(series) {
  els.priceLegend.replaceChildren();
  series.forEach((item, index) => {
    const button = el("button", state.activeMarkets.has(item.market_id) ? "active" : "");
    const dot = el("i"); dot.style.background = COLORS[index % COLORS.length];
    button.append(dot, document.createTextNode(`${item.bucket} ${percent(item.latest, 0)}`));
    button.addEventListener("click", () => {
      if (state.activeMarkets.has(item.market_id)) state.activeMarkets.delete(item.market_id); else state.activeMarkets.add(item.market_id);
      if (!state.activeMarkets.size) state.activeMarkets.add(item.market_id);
      renderPriceLegend(series); renderPriceChart(series);
    });
    els.priceLegend.append(button);
  });
}

function renderPriceChart(series) {
  const active = series.filter((item) => state.activeMarkets.has(item.market_id)).map((item, index) => ({
    name: item.bucket, color: COLORS[series.indexOf(item) % COLORS.length], points: item.points,
  }));
  els.priceChartValue.textContent = active.length ? `${active.length} 条曲线` : "--";
  lineChart(els.priceChart, active, { height: 280, yMin: 0, yMax: 1, yFormat: (v) => `${Math.round(v * 100)}%`, tooltipFormat: (v) => `${(v * 100).toFixed(2)}% · ${v.toFixed(4)}`, empty: "暂无盘口历史" });
}

function renderBuckets(markets, city) {
  els.bucketGrid.replaceChildren();
  els.bucketUpdated.textContent = city?.market.slot_utc ? `盘口 ${formatTime(city.market.slot_utc)}` : "--";
  markets.forEach((market) => {
    const predicted = market.bucket === city?.market.predicted_bucket;
    const mode = market.bucket === city?.market.mode_bucket;
    const card = el("div", `bucket${predicted ? " predicted" : ""}${mode ? " mode" : ""}`);
    card.append(el("span", "", market.bucket), el("strong", "", percent(market.yes)), el("small", "", `bid ${num(market.yes_bid, 3)} / ask ${num(market.yes_ask, 3)}`));
    els.bucketGrid.append(card);
  });
}

async function selectCity(eventId) {
  if (!eventId || state.selectedEvent === eventId && state.detail) return;
  state.selectedEvent = eventId; state.activeMarkets.clear(); renderCities();
  els.detailTitle.textContent = "读取城市详情...";
  try {
    const response = await fetch(`/api/city?event_id=${encodeURIComponent(eventId)}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.detail = await response.json(); renderDetail();
  } catch (error) {
    els.detailTitle.textContent = "城市详情加载失败"; els.detailMeta.textContent = String(error);
  }
}

async function refreshLadderV4() {
  try {
    const response = await fetch("/api/ladder-v4", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.ladderV4 = await response.json();
    renderLadderV4();
  } catch (error) {
    els.ladderV4State.textContent = "加载失败";
    els.ladderV4State.className = "system-state error";
    els.ladderV4Today.textContent = "V4 数据不可用";
    els.ladderV4TodayMeta.textContent = String(error);
  }
}

async function refresh() {
  els.refreshBtn.disabled = true; els.refreshBtn.textContent = "…";
  refreshLadderV4();
  try {
    const [response, accuracyResponse, qualityResponse, aiResponse, dualResponse] = await Promise.all([
      fetch("/api/overview", { cache: "no-store" }),
      fetch("/api/accuracy", { cache: "no-store" }),
      fetch("/api/data-quality", { cache: "no-store" }),
      fetch("/api/ai-system", { cache: "no-store" }),
      fetch("/api/dual-strategy", { cache: "no-store" }),
    ]);
    if (!response.ok || !accuracyResponse.ok || !qualityResponse.ok || !aiResponse.ok || !dualResponse.ok) throw new Error(`HTTP ${response.status}/${accuracyResponse.status}/${qualityResponse.status}/${aiResponse.status}/${dualResponse.status}`);
    state.overview = await response.json(); state.accuracy = await accuracyResponse.json(); state.quality = await qualityResponse.json(); state.ai = await aiResponse.json(); state.dual = await dualResponse.json();
    if (state.overview.error) throw new Error(state.overview.error);
    renderSummary(state.overview); renderQuality(); renderCities(); renderChangeFeed(); renderAccuracy(); renderAiSummary(); renderAiCycles(); renderDualStrategy();
    const fallback = state.overview.cities.find((city) => city.forecast.raw_c != null) || state.overview.cities[0];
    const target = state.selectedEvent && state.overview.cities.some((city) => city.event_id === state.selectedEvent) ? state.selectedEvent : fallback?.event_id;
    state.detail = null; state.selectedEvent = null; if (target) await selectCity(target);
  } catch (error) {
    els.cityRows.replaceChildren(); const row = el("tr"); const cell = el("td", "loading-cell", `加载失败：${error}`); cell.colSpan = 9; row.append(cell); els.cityRows.append(row);
  } finally {
    els.refreshBtn.disabled = false; els.refreshBtn.textContent = "↻";
  }
}

function scheduleAuto() {
  clearInterval(state.timer);
  if (state.auto) state.timer = setInterval(refresh, 60_000);
}

els.refreshBtn.addEventListener("click", refresh);
els.autoBtn.addEventListener("click", () => {
  state.auto = !state.auto; els.autoBtn.setAttribute("aria-pressed", String(state.auto)); els.autoBtn.textContent = state.auto ? "自动 60s" : "自动关闭"; scheduleAuto();
});
[els.citySearch, els.sortSelect, els.changedOnly].forEach((control) => control.addEventListener("input", renderCities));
[els.accuracyDate, els.accuracyFilter].forEach((control) => control.addEventListener("input", renderAccuracy));
[els.aiCityFilter, els.aiModeFilter].forEach((control) => control.addEventListener("input", renderAiCycles));
els.accuracyToggle.addEventListener("click", () => setAccuracyExpanded(!state.accuracyExpanded));
window.addEventListener("resize", () => { if (state.detail) renderDetail(); });

setAccuracyExpanded(state.accuracyExpanded, false); setClock(); setInterval(setClock, 1000); scheduleAuto(); refresh();
