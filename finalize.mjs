import fs from "node:fs/promises";
import path from "node:path";

const ROOT = path.dirname(new URL(import.meta.url).pathname);
const config = JSON.parse(await fs.readFile(path.join(ROOT, "config.json"), "utf8"));
const snapshot = JSON.parse(await fs.readFile(path.join(ROOT, "data", "snapshot.json"), "utf8"));
const analysis = JSON.parse(await fs.readFile(path.join(ROOT, "data", "analysis.json"), "utf8"));
const byId = new Map(snapshot.candidates.map((candidate) => [candidate.candidateId, candidate]));
const returnedIds = analysis.assessments.map((item) => item.candidateId);
const returnedIdSet = new Set(returnedIds);
const expectedIds = new Set(byId.keys());
if (returnedIds.length !== returnedIdSet.size) {
  throw new Error("Codex analysis contains duplicate candidateId values");
}
const missingIds = [...expectedIds].filter((id) => !returnedIdSet.has(id));
const unknownIds = [...returnedIdSet].filter((id) => !expectedIds.has(id));
if (missingIds.length || unknownIds.length) {
  throw new Error(`Incomplete Codex analysis; missing=${missingIds.join(",")}; unknown=${unknownIds.join(",")}`);
}

async function liveNoAsk(candidate) {
  const response = await fetch(`https://clob.polymarket.com/book?token_id=${candidate.noTokenId}`);
  if (!response.ok) throw new Error(`order book ${response.status}`);
  const book = await response.json();
  const asks = (book.asks || []).map((level) => ({ price: Number(level.price), size: Number(level.size) }))
    .sort((a, b) => a.price - b.price);
  return asks[0] || null;
}

const assessed = [];
for (const item of analysis.assessments) {
  const candidate = byId.get(item.candidateId);
  if (!candidate) continue;
  const ask = await liveNoAsk(candidate);
  if (!ask || ask.price < config.minNoAsk || ask.price > config.maxNoAsk) continue;
  const rawEdge = item.noWinProbability - ask.price;
  const edgeAfterBuffer = rawEdge - config.feeAndUncertaintyBuffer;
  assessed.push({ ...candidate, ...item, liveNoAsk: ask.price, liveNoAskSize: ask.size, rawEdge, edgeAfterBuffer });
}

const bestByEvent = new Map();
for (const item of assessed) {
  const current = bestByEvent.get(item.eventId);
  if (!current || item.noWinProbability > current.noWinProbability ||
      (item.noWinProbability === current.noWinProbability && item.edgeAfterBuffer > current.edgeAfterBuffer)) {
    bestByEvent.set(item.eventId, item);
  }
}
const ranked = [...bestByEvent.values()].sort((a, b) =>
  b.noWinProbability - a.noWinProbability || b.edgeAfterBuffer - a.edgeAfterBuffer);
const recommendations = ranked.filter((item) =>
  item.recommended && item.edgeAfterBuffer >= config.minEdgeAfterBuffer);

function pct(value) { return `${(value * 100).toFixed(1)}%`; }
function cents(value) { return `${(value * 100).toFixed(1)}c`; }
function money(value) { return Math.round(value).toLocaleString("en-US"); }

const selection = snapshot.selection || {
  offset: 0,
  rankStart: snapshot.topEvents.length ? 1 : null,
  rankEnd: snapshot.topEvents.length,
  isSupplement: false
};
const eventRange = selection.isSupplement
  ? `第${selection.rankStart}-${selection.rankEnd}`
  : `前${selection.rankEnd}`;
const titleSuffix = selection.isSupplement ? `｜补充${eventRange}` : "";

const lines = [
  `【天气 NO 策略｜${snapshot.targetDate}${titleSuffix}】`,
  `范围：流动性×24h成交量综合${eventRange}，排除香港；NO卖一 ${cents(config.minNoAsk)}–${cents(config.maxNoAsk)}`,
  `候选档位 ${snapshot.candidates.length} 个，独立评估 ${assessed.length} 个，推荐 ${recommendations.length} 个`,
  ""
];
if (!recommendations.length) {
  lines.push("今日没有同时满足胜率、价格和安全边际要求的盘口。宁可空仓。", "");
} else {
  recommendations.forEach((item, index) => {
    lines.push(
      `${index + 1}. ${item.eventTitle}｜${item.outcomeRange} NO`,
      `NO胜率 ${pct(item.noWinProbability)}（区间 ${pct(item.confidenceBand[0])}–${pct(item.confidenceBand[1])}）｜现价 ${cents(item.liveNoAsk)}｜缓冲后优势 ${pct(item.edgeAfterBuffer)}`,
      `依据：${item.weatherThesis}`,
      `最大风险：${item.keyRisk}`,
      `更新触发：${item.updateTriggers.join("；")}`,
      `市场：${item.eventSlug}`,
      ""
    );
  });
}
lines.push(`${eventRange}事件：`);
snapshot.topEvents.forEach((event) => lines.push(
  `${event.rank}. ${event.title}｜24h $${money(event.volume24hr)}｜流动性 $${money(event.liquidity)}`
));
lines.push("", `生成时间：${new Date().toLocaleString("zh-CN", { timeZone: config.timezone, hour12: false })}`,
  "仅为概率筛选，不自动下单；每个事件最多推荐一档。"
);

const message = lines.join("\n");
const report = { snapshot, analysis, assessed, recommendations, message, finalizedAt: new Date().toISOString() };
await fs.writeFile(path.join(ROOT, "data", "report.json"), JSON.stringify(report, null, 2));
await fs.writeFile(path.join(ROOT, "data", "message.txt"), message);

if (process.argv.includes("--dry-run")) {
  console.log(message);
  process.exit(0);
}
const webhook = process.env[config.feishuWebhookEnv];
if (!webhook) throw new Error(`${config.feishuWebhookEnv} is not set`);
const response = await fetch(webhook, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ msg_type: "text", content: { text: message } })
});
const body = await response.text();
if (!response.ok) throw new Error(`Feishu ${response.status}: ${body}`);
const parsed = JSON.parse(body);
if (parsed.code !== 0 && parsed.StatusCode !== 0) throw new Error(`Feishu rejected: ${body}`);
console.log(`Feishu sent: ${body}`);
