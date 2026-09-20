import fs from "node:fs/promises";
import path from "node:path";

const ROOT = path.dirname(new URL(import.meta.url).pathname);
const config = JSON.parse(await fs.readFile(path.join(ROOT, "config.json"), "utf8"));
const fmt = new Intl.DateTimeFormat("en-CA", {
  timeZone: config.timezone,
  year: "numeric",
  month: "2-digit",
  day: "2-digit"
});

function dateInTimezone(offsetDays = 0) {
  const date = new Date(Date.now() + offsetDays * 86400000);
  return fmt.format(date);
}

async function getJson(url) {
  const response = await fetch(url, { headers: { accept: "application/json" } });
  if (!response.ok) throw new Error(`${response.status} ${url}`);
  return response.json();
}

function excluded(event) {
  const text = `${event.title} ${event.slug}`;
  return config.excludeCityPatterns.some((pattern) => text.includes(pattern));
}

function isDailyTemperatureEvent(event, targetDate) {
  return event.eventDate === targetDate &&
    /^(Highest|Lowest) temperature in .+ on /i.test(event.title) &&
    !excluded(event);
}

function parseArray(value) {
  return Array.isArray(value) ? value : JSON.parse(value);
}

async function noOrderBook(market) {
  const noTokenId = parseArray(market.clobTokenIds)[1];
  const book = await getJson(`https://clob.polymarket.com/book?token_id=${noTokenId}`);
  const asks = (book.asks || []).map((level) => ({
    price: Number(level.price),
    size: Number(level.size)
  })).sort((a, b) => a.price - b.price);
  const bids = (book.bids || []).map((level) => ({
    price: Number(level.price),
    size: Number(level.size)
  })).sort((a, b) => b.price - a.price);
  return { noTokenId, bestNoAsk: asks[0] || null, bestNoBid: bids[0] || null };
}

const args = process.argv.slice(2);
const targetDate = args.find((arg) => !arg.startsWith("--")) || dateInTimezone(1);

function integerOption(name, fallback) {
  const prefix = `--${name}=`;
  const raw = args.find((arg) => arg.startsWith(prefix))?.slice(prefix.length);
  if (raw === undefined) return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(`${name} must be a non-negative integer`);
  }
  return value;
}

const eventOffset = integerOption("event-offset", 0);
const eventCount = integerOption("event-count", config.topEventCount);
if (eventCount === 0) throw new Error("event-count must be greater than zero");
const endpoint = new URL("https://gamma-api.polymarket.com/events");
endpoint.search = new URLSearchParams({
  limit: "100",
  active: "true",
  closed: "false",
  tag_slug: "weather",
  order: "volume24hr",
  ascending: "false"
});

const events = (await getJson(endpoint)).filter((event) => isDailyTemperatureEvent(event, targetDate));
for (const event of events) {
  event.selectionScore = Math.sqrt(Math.max(Number(event.volume24hr) || 0, 1) *
    Math.max(Number(event.liquidity) || 0, 1));
}
const rankedEvents = events.sort((a, b) => b.selectionScore - a.selectionScore);
const topEvents = rankedEvents.slice(eventOffset, eventOffset + eventCount);

const candidates = [];
for (const event of topEvents) {
  const books = await Promise.all(event.markets.filter((market) => market.acceptingOrders)
    .map(async (market) => ({ market, book: await noOrderBook(market) })));
  for (const { market, book } of books) {
    if (!book.bestNoAsk) continue;
    if (book.bestNoAsk.price < config.minNoAsk || book.bestNoAsk.price > config.maxNoAsk) continue;
    candidates.push({
      candidateId: `${event.id}:${market.id}`,
      eventId: event.id,
      marketId: market.id,
      eventSlug: event.slug,
      eventTitle: event.title,
      outcomeRange: market.groupItemTitle,
      question: market.question,
      targetDate,
      resolutionSource: market.resolutionSource || event.resolutionSource,
      rules: market.description,
      volume24hr: Number(event.volume24hr) || 0,
      liquidity: Number(event.liquidity) || 0,
      selectionScore: event.selectionScore,
      noTokenId: book.noTokenId,
      bestNoAsk: book.bestNoAsk.price,
      bestNoAskSize: book.bestNoAsk.size,
      bestNoBid: book.bestNoBid?.price ?? null,
      fetchedAt: new Date().toISOString()
    });
  }
}

const snapshot = {
  targetDate,
  generatedAt: new Date().toISOString(),
  config,
  selection: {
    offset: eventOffset,
    requestedCount: eventCount,
    rankStart: topEvents.length ? eventOffset + 1 : null,
    rankEnd: topEvents.length ? eventOffset + topEvents.length : null,
    availableEventCount: rankedEvents.length,
    isSupplement: eventOffset > 0
  },
  topEvents: topEvents.map((event, index) => ({
    rank: eventOffset + index + 1,
    id: event.id,
    slug: event.slug,
    title: event.title,
    volume24hr: Number(event.volume24hr) || 0,
    liquidity: Number(event.liquidity) || 0,
    selectionScore: event.selectionScore
  })),
  candidates
};
await fs.writeFile(path.join(ROOT, "data", "snapshot.json"), JSON.stringify(snapshot, null, 2));

const prompt = `你是一个专门评估 Polymarket 城市天气 NO 盘口的审慎分析员。分析日期为 ${targetDate}。\n\n` +
`目标不是推导合理温度范围，也不是从盘口价格反推概率，而是对每个候选独立估计 P(NO 最终结算成功)。` +
`只分析输入中的候选，必须为每个 candidateId 返回一条 assessment。\n\n` +
`对每个候选执行：\n` +
`1. 从市场规则提取精确站点、最高/最低温、精度、修订截止条件。\n` +
`2. 获取最新机场 METAR/TAF（适用时）、Wunderground/官方预报、当地官方气象机构，以及至少 ECMWF、GFS、ICON/当地模型。\n` +
`3. 使用该站近期实际日极值与 Wunderground/Poly 已结算结果校准数据源偏差；不得用市中心站替代机场站。\n` +
`4. 用第一性原理评估云量、风向、海风、降水/对流触发、边界层混合、地形和峰值时段。\n` +
`5. 输出独立 NO 胜率和合理置信区间。recommended 只有在你认为数据质量足够、NO 胜率具有实质置信度且扣除价格/费用仍有余量时才为 true。\n` +
`6. 不得因为 NO 买价在 80-92c 就自动给高胜率；盘口只作为事后比较，不作为气象证据。\n` +
`7. 这是提前一天的预测；如果关键 TAF 尚未覆盖全天或对流不确定性过高，要扩大区间并降低置信度。\n` +
`8. 控制执行预算：按站点分组，同一事件的多个候选必须共享一次数据采集和天气分布，不得逐档重复抓取。优先批量请求 METAR/TAF 和数值模型；历史校准最多使用最近 3 个完整日。网页或接口受阻时记录数据限制，不得反复重试、搜索替代页面或扩展为长期回测。不要检查本地项目源码；获得上述核心证据后立即生成全部 assessments。\n\n` +
`输入快照：\n${JSON.stringify(snapshot, null, 2)}\n`;
await fs.writeFile(path.join(ROOT, "data", "prompt.txt"), prompt);
console.log(JSON.stringify({
  targetDate,
  eventOffset,
  requestedEvents: eventCount,
  selectedEvents: topEvents.length,
  candidates: candidates.length
}));
