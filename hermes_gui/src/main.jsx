import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  ArrowDownRight,
  ArrowRight,
  Check,
  CloudSun,
  Crosshair,
  Menu,
  Radar,
  RefreshCw,
  Satellite,
  Sun,
  Waves,
  Wind,
  X,
} from "lucide-react";
import "@fontsource-variable/archivo";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/600.css";
import "./styles.css";

const DEMO = {
  city: "Shanghai",
  station: "ZSPD",
  targetDate: "SEP 15, 2026",
  question: "Will Shanghai reach 32°C today?",
  current: 31.2,
  dewpoint: 24,
  wind: "SE 4.2 m/s",
  cloud: 32,
  pressure: 1005,
  market: 41,
  agent: 68,
  confidence: 72,
  expected: 32.3,
  peak: "14:30–15:40",
  close: "02H 18M",
  models: [
    { name: "ECMWF", value: 32.4 },
    { name: "GFS", value: 31.8 },
    { name: "ICON", value: 32.1 },
    { name: "MBLUE", value: 32.6 },
  ],
};

const timeline = [
  ["12:00", 30.4, 30.6, 30.5, 30.5, 30.4],
  ["12:30", 30.7, 30.8, 30.8, 30.9, 30.7],
  ["13:00", 31.0, 31.1, 31.0, 31.2, 31.0],
  ["13:30", 31.4, 31.3, 31.3, 31.5, 31.4],
  ["14:00", 31.8, 31.6, 31.7, 31.9, 31.8],
  ["14:30", null, 31.9, 32.0, 32.2, 32.1],
  ["15:00", null, 32.0, 32.1, 32.4, 32.3],
  ["15:30", null, 31.9, 32.0, 32.3, 32.2],
  ["16:00", null, 31.7, 31.8, 32.1, 32.0],
];

function useClock() {
  const [time, setTime] = useState(new Date());
  useEffect(() => {
    const timer = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(time);
}

function useWeatherData() {
  const [loading, setLoading] = useState(false);
  function refresh() { setLoading(true); window.setTimeout(() => setLoading(false), 250); }
  return { data: DEMO, source: "SYNTHETIC DEMO", loading, refresh };
}

function Header({ view, setView, source, loading, refresh }) {
  const time = useClock();
  const [menuOpen, setMenuOpen] = useState(false);
  return (
    <header className="site-header">
      <a className="wordmark" href="#top" onClick={() => setView("overview")}>
        HERMES<span>//</span>WEATHER
      </a>
      <div className="clock-block">
        <small>SHANGHAI · LOCAL TIME</small>
        <strong>{time}</strong>
      </div>
      <div className="header-actions">
        <span className="source-state"><i />{source}</span>
        <button className="refresh-button" onClick={refresh} aria-label="刷新数据">
          <RefreshCw size={15} className={loading ? "spin" : ""} />
        </button>
        <button
          className="menu-button"
          onClick={() => setMenuOpen((open) => !open)}
          aria-expanded={menuOpen}
        >
          <span>Console</span>{menuOpen ? <X size={17} /> : <Menu size={17} />}
        </button>
        {menuOpen && (
          <nav className="menu-popover">
            <button onClick={() => { setView("overview"); setMenuOpen(false); }}>01 / Overview</button>
            <button onClick={() => { setView("evidence"); setMenuOpen(false); }}>02 / Evidence view</button>
            <a href="#market-ladder" onClick={() => setMenuOpen(false)}>03 / Market ladder</a>
          </nav>
        )}
      </div>
    </header>
  );
}

function ParticleField() {
  const points = useMemo(() => Array.from({ length: 42 }, (_, index) => ({
    x: (index * 37) % 101,
    y: (index * 61) % 89,
    delay: (index % 8) * 0.35,
    blue: index % 6 === 0,
  })), []);
  return (
    <div className="particle-field" aria-hidden="true">
      {points.map((point, index) => (
        <i key={index} className={point.blue ? "blue" : ""} style={{ left: `${point.x}%`, top: `${point.y}%`, animationDelay: `${point.delay}s` }} />
      ))}
    </div>
  );
}

function ProbabilityCard({ data, setView }) {
  const edge = data.agent - data.market;
  return (
    <aside className="probability-card reveal delay-2">
      <div className="card-kicker"><span>MARKET · EXAMPLE</span><i /></div>
      <p>{data.city.toUpperCase()} · {data.station}</p>
      <h2>{data.question}</h2>
      <div className="probability-lines">
        <div><span>MARKET</span><strong>{data.market}<small>%</small></strong></div>
        <div className="agent-line"><span>HERMES</span><strong>{data.agent}<small>%</small></strong></div>
      </div>
      <div className="edge-line">
        <span>EXPECTED EDGE</span>
        <strong>+{edge}<small> PTS</small></strong>
      </div>
      <button className="open-model" onClick={() => setView("evidence")}>
        Open the evidence <ArrowRight size={15} />
      </button>
    </aside>
  );
}

function Hero({ data, setView }) {
  return (
    <section className="hero" id="top">
      <ParticleField />
      <div className="hero-inner">
        <div className="hero-topline">
          <div className="hero-copy reveal">
            <span className="index">(01) / WEATHER INTELLIGENCE</span>
            <p>Hermes reads model guidance, live observations and the market, then reduces the gap to one decision.</p>
            <div className="close-chip"><i /> MARKET CLOSES IN {data.close}</div>
          </div>
          <ProbabilityCard data={data} setView={setView} />
        </div>
        <h1><span>HERMES</span></h1>
      </div>
    </section>
  );
}

function Ticker({ data }) {
  const items = [
    ["ZSPD", `${data.current.toFixed(1)}°C`], ["DEWPOINT", `${data.dewpoint}°C`], ["WIND", data.wind],
    ["MODEL RANGE", "31.8–32.6°C"], ["EDGE", `+${data.agent - data.market} PTS`], ["ACTION", "BUY YES"],
  ];
  return (
    <div className="ticker" aria-label="示例天气摘要">
      <div className="ticker-track">
        {[...items, ...items].map(([label, value], index) => <span key={index}>{label} <b>{value}</b><i>◆</i></span>)}
      </div>
    </div>
  );
}

function ProcessRow({ icon: Icon, label, value, tone = "blue" }) {
  return <div className="process-row"><span><Icon size={17} />{label}</span><strong className={tone}>{value}</strong></div>;
}

function ForecastScale({ data }) {
  const min = 31.5;
  const max = 32.8;
  return (
    <div className="forecast-scale">
      <div className="scale-axis"><span>31.5°</span><span>32.0°</span><span>32.5°</span><span>32.8°</span></div>
      <div className="range-line" style={{ left: `${((31.8-min)/(max-min))*100}%`, width: `${((32.6-31.8)/(max-min))*100}%` }} />
      {data.models.map((model, index) => (
        <div className="model-row" key={model.name}>
          <span>{model.name}</span>
          <i style={{ left: `${((model.value-min)/(max-min))*100}%` }} />
          <strong>{model.value.toFixed(1)}°</strong>
        </div>
      ))}
    </div>
  );
}

function DeskChart() {
  const width = 720;
  const height = 252;
  const px = (index) => 42 + index * ((width - 70) / (timeline.length - 1));
  const py = (value) => 18 + (32.8 - value) * 105;
  const makePath = (series) => timeline.reduce((path, row, index) => {
    const value = row[series];
    if (value == null) return path;
    return `${path}${path ? " L" : "M"}${px(index)} ${py(value)}`;
  }, "");
  return (
    <svg className="desk-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="实况、模型与 Hermes 修正温度走势">
      {[31, 31.5, 32, 32.5].map((value) => (
        <g key={value}><line x1="42" x2="700" y1={py(value)} y2={py(value)} /><text x="2" y={py(value) + 4}>{value.toFixed(1)}°</text></g>
      ))}
      <line className="threshold" x1="42" x2="700" y1={py(32)} y2={py(32)} />
      <text className="threshold-label" x="590" y={py(32) - 7}>32°C THRESHOLD</text>
      <path className="series muted one" d={makePath(2)} />
      <path className="series muted two" d={makePath(3)} />
      <path className="series muted three" d={makePath(4)} />
      <path className="series hermes" d={makePath(5)} />
      <path className="series observed" d={makePath(1)} />
      <line className="now-line" x1={px(4)} x2={px(4)} y1="12" y2="211" />
      <text className="now-label" x={px(4) + 6} y="20">NOW</text>
      {timeline.map((row, index) => <text className="time-label" key={row[0]} x={px(index)} y="240" textAnchor="middle">{row[0]}</text>)}
    </svg>
  );
}

function ModelBook({ data }) {
  const average = data.models.reduce((sum, model) => sum + model.value, 0) / data.models.length;
  return (
    <section className="desk-panel model-book">
      <div className="desk-panel-head"><span>MODEL BOOK</span><small>12Z / TMAX</small></div>
      <div className="model-consensus"><span>CONSENSUS</span><strong>{average.toFixed(1)}°C</strong><small>RANGE 31.8–32.6</small></div>
      <div className="model-table">
        {data.models.map((model) => (
          <div key={model.name}><span>{model.name}</span><strong>{model.value.toFixed(1)}°</strong><small>{model.value >= 32 ? "ABOVE" : "BELOW"}</small></div>
        ))}
      </div>
      <div className="obs-strip"><span>OBSERVED</span><strong>{data.current.toFixed(1)}°C</strong><small>+0.6°C/H</small></div>
      <div className="weather-strip">
        <span>DP <b>{data.dewpoint}°</b></span><span>WIND <b>{data.wind}</b></span><span>CLOUD <b>{data.cloud}%</b></span><span>MSLP <b>{data.pressure}</b></span>
      </div>
    </section>
  );
}

function EvidenceStack({ data, setView }) {
  return (
    <section className="desk-panel evidence-stack">
      <div className="desk-panel-head"><span>EXAMPLE EVIDENCE</span><small>{data.confidence}% CONF</small></div>
      <div className="signal-list">
        <div><span><Sun size={14}/>Remaining heating</span><strong>HIGH</strong></div>
        <div><span><Radar size={14}/>Upstream radar</span><strong>CLEAR</strong></div>
        <div><span><CloudSun size={14}/>Cloud interference</span><strong>LOW</strong></div>
        <div><span><Waves size={14}/>Sea-breeze risk</span><strong className="warn">WATCH</strong></div>
      </div>
      <div className="correction-compact">
        <span>MODEL CORRECTION</span>
        <strong>31.9° <ArrowRight size={16}/> {data.expected.toFixed(1)}°</strong>
        <small>+0.4° / solar input + upstream trend</small>
      </div>
      <div className="invalidation-compact"><span>INVALIDATE IF</span><p>Trend &lt; +0.2°C/h or echo enters upstream 80 km.</p></div>
      <button className="inspect-button" onClick={() => setView("evidence")}>FULL EVIDENCE <ArrowRight size={14}/></button>
    </section>
  );
}

function MarketLadder({ data }) {
  const rows = [
    { bucket: "≤30°C", market: 4, fair: 2, edge: -2, ask: 5 },
    { bucket: "31°C", market: 27, fair: 20, edge: -7, ask: 29 },
    { bucket: "32°C", market: data.market, fair: data.agent, edge: data.agent - data.market, ask: data.market + 1, selected: true },
    { bucket: "33°C", market: 21, fair: 9, edge: -12, ask: 23 },
    { bucket: "≥34°C", market: 7, fair: 1, edge: -6, ask: 8 },
  ];
  return (
    <section className="desk-panel ladder-panel-compact" id="market-ladder">
      <div className="desk-panel-head"><span>MARKET LADDER</span><small>YES BOOK / FEE ADJ.</small></div>
      <table>
        <thead><tr><th>BUCKET</th><th>MARKET</th><th>HERMES</th><th>EDGE</th><th>ASK</th><th>STATE</th></tr></thead>
        <tbody>{rows.map((row) => <tr key={row.bucket} className={row.selected ? "selected" : ""}>
          <td>{row.bucket}</td><td>{row.market}%</td><td>{row.fair}%</td><td className={row.edge > 0 ? "positive" : ""}>{row.edge > 0 ? "+" : ""}{row.edge}%</td><td>{row.ask}¢</td><td>{row.selected ? "BUY YES" : "PASS"}</td>
        </tr>)}</tbody>
      </table>
    </section>
  );
}

function Overview({ data, setView }) {
  const edge = data.agent - data.market;
  return (
    <main className="trading-desk" id="top">
      <nav className="city-tabs" aria-label="监控城市">
        <button className="active"><span>{data.city}</span><b>{data.station}</b><i>+{edge}</i></button>
        <button><span>Beijing</span><b>ZBAA</b><i className="flat">WAIT</i></button>
        <button><span>Guangzhou</span><b>ZGGG</b><i>+11</i></button>
        <button><span>Qingdao</span><b>ZSQD</b><i className="flat">PASS</i></button>
        <button><span>Wuhan</span><b>ZHHH</b><i>+8</i></button>
        <div className="desk-status"><span><i/>SAMPLE DATA</span><b>14:06:12 CST</b></div>
      </nav>

      <section className="decision-bar">
        <div className="market-title">
          <span>POLYMARKET / DAILY HIGH / {data.targetDate}</span>
          <h1>{data.question}</h1>
          <small>CLOSES {data.close} · PAPER ONLY</small>
        </div>
        <div className="decision-metric"><span>YES ASK</span><strong>{data.market}<small>¢</small></strong><i>MARKET {data.market}%</i></div>
        <div className="decision-metric fair"><span>FAIR VALUE</span><strong>{data.agent}<small>¢</small></strong><i>HERMES {data.agent}%</i></div>
        <div className="decision-metric edge"><span>NET EDGE</span><strong>+{edge}<small>PT</small></strong><i>AFTER FEE +{edge - 2}</i></div>
        <div className="trade-action"><span>ACTION</span><strong>BUY YES</strong><small>5 SHARES · BASE</small></div>
      </section>

      <section className="desk-grid">
        <ModelBook data={data} />
        <section className="desk-panel intraday-panel">
          <div className="desk-panel-head">
            <span>INTRADAY TEMPERATURE</span>
            <div className="compact-legend"><i className="actual"/>ACTUAL <i className="model"/>MODELS <i className="agent"/>HERMES</div>
          </div>
          <DeskChart />
          <div className="chart-footer">
            <span>EXPECTED TMAX <b>{data.expected.toFixed(1)}°C</b></span>
            <span>PEAK WINDOW <b>{data.peak}</b></span>
            <span>THRESHOLD PROB. <b>{data.agent}%</b></span>
          </div>
        </section>
        <EvidenceStack data={data} setView={setView} />
      </section>

      <section className="desk-bottom-grid">
        <MarketLadder data={data} />
        <section className="desk-panel execution-panel">
          <div className="desk-panel-head"><span>EXECUTION</span><small>PAPER ENGINE</small></div>
          <dl>
            <div><dt>Best ask</dt><dd>{data.market + 1}¢</dd></div><div><dt>Depth</dt><dd>42 sh</dd></div>
            <div><dt>Fee</dt><dd>1.8¢</dd></div><div><dt>Max cost</dt><dd>$2.10</dd></div>
            <div><dt>Invalidation</dt><dd>15:00 CST</dd></div><div><dt>Gate</dt><dd className="positive">READY</dd></div>
          </dl>
          <div className="execution-note"><Check size={13}/> Quote, weather and cash checks passed</div>
        </section>
      </section>
    </main>
  );
}

function EvidenceChart() {
  const width = 760;
  const height = 330;
  const px = (index) => 44 + index * ((width - 78) / (timeline.length - 1));
  const py = (value) => 26 + (32.7 - value) * 150;
  const makePath = (series) => timeline.reduce((path, row, index) => {
    const value = row[series];
    if (value == null) return path;
    return `${path}${path ? " L" : "M"}${px(index)} ${py(value)}`;
  }, "");
  return (
    <div className="evidence-chart">
      <div className="chart-legend">
        <span className="actual">Actual</span><span>ECMWF</span><span>GFS</span><span>ICON</span><span className="agent">Hermes corrected</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="模型、实况与 Hermes 修正温度时间轴">
        {[30.5, 31, 31.5, 32, 32.5].map((value) => <g key={value}><line x1="44" x2="730" y1={py(value)} y2={py(value)} /><text x="4" y={py(value) + 4}>{value.toFixed(1)}°</text></g>)}
        <line className="threshold" x1="44" x2="730" y1={py(32)} y2={py(32)} />
        <text className="threshold-label" x="615" y={py(32) - 8}>MARKET THRESHOLD</text>
        <path className="series muted one" d={makePath(2)} /><path className="series muted two" d={makePath(3)} />
        <path className="series muted three" d={makePath(4)} /><path className="series hermes" d={makePath(5)} />
        <path className="series observed" d={makePath(1)} />
        <line className="now-line" x1={px(4)} x2={px(4)} y1="20" y2="286" />
        <text className="now-label" x={px(4) + 7} y="32">NOW</text>
        {timeline.map((row, index) => <text className="time-label" key={row[0]} x={px(index)} y="318" textAnchor="middle">{row[0]}</text>)}
      </svg>
    </div>
  );
}

function WeatherMap() {
  return (
    <div className="weather-map">
      <div className="map-grid" />
      <svg viewBox="0 0 600 430" aria-label="上海周边观测与上风向示意地图">
        <path className="coast" d="M366 0 C339 52 350 99 311 140 C280 174 299 215 257 246 C220 274 232 324 188 364 C160 389 151 411 144 430 L600 430 L600 0 Z" />
        <path className="river" d="M0 230 C87 204 128 240 204 225 C285 209 334 179 392 188 C456 198 506 239 600 210" />
        <g className="wind-arrows"><path d="M70 110 L160 157"/><path d="M112 84 L202 131"/><path d="M51 277 L141 324"/><path d="M213 72 L303 119"/></g>
        <g className="radar-rings"><circle cx="292" cy="217" r="44"/><circle cx="292" cy="217" r="84"/><circle cx="292" cy="217" r="126"/></g>
        <g className="stations">
          <circle cx="292" cy="217" r="8"/><text x="307" y="221">ZSPD · 31.2°</text>
          <circle cx="182" cy="151" r="5"/><text x="195" y="155">NANTONG · 31.0°</text>
          <circle cx="161" cy="291" r="5"/><text x="174" y="295">HANGZHOU · 31.4°</text>
          <circle cx="376" cy="322" r="5"/><text x="389" y="326">SHENGSI · 28.9°</text>
        </g>
      </svg>
      <div className="map-toolbar"><button className="active"><Radar size={15}/>RADAR</button><button><Satellite size={15}/>SAT</button><button><Wind size={15}/>WIND</button></div>
      <div className="map-stamp">121.805°E / 31.143°N<br />RANGE 250 KM</div>
    </div>
  );
}

function Evidence({ data, setView }) {
  return (
    <main className="evidence-page" id="top">
      <div className="evidence-titlebar">
        <button className="back-link" onClick={() => setView("overview")}>← BACK TO OVERVIEW</button>
        <span className="index">(02) / EVIDENCE VIEW</span>
        <span className="target-date">CASE / {data.city.toUpperCase()} · {data.station}</span>
      </div>
      <section className="evidence-intro">
        <div><span>EXAMPLE QUESTION</span><h1>{data.question}</h1></div>
        <div className="evidence-summary"><span>MARKET <b>{data.market}%</b></span><ArrowRight/><span>HERMES <b>{data.agent}%</b></span><strong>+{data.agent - data.market} PTS</strong></div>
      </section>

      <section className="evidence-workspace">
        <article className="map-column">
          <div className="workspace-head"><span>01 / WEATHER FIELD</span><small>UPDATED 14:06 CST</small></div>
          <WeatherMap />
          <div className="map-notes"><span><i className="good"/>NO UPSTREAM ECHO</span><span><i/>SE FLOW 4.2 M/S</span><span><i className="good"/>SOLAR INPUT ACTIVE</span></div>
        </article>
        <article className="timeline-column">
          <div className="workspace-head"><span>02 / MODEL → OBSERVED → CORRECTED</span><small>LOCAL TIME</small></div>
          <EvidenceChart />
          <div className="correction-callout"><span>MODEL CORRECTION</span><strong>31.9°C <ArrowRight size={20}/> 32.3°C</strong><small>+0.4°C / remaining heating</small></div>
        </article>
        <aside className="agent-column">
          <div className="workspace-head"><span>03 / AGENT EVIDENCE</span><small>{data.confidence}% CONF.</small></div>
          <div className="evidence-list">
            <h3>OBSERVATIONS</h3>
            {["Temperature rising 0.6°C / hour", "Solar radiation remains strong", "Upstream stations still warming", "No significant radar echoes upstream"].map((text) => <p key={text}><Check size={14}/>{text}</p>)}
            <p className="caution"><ArrowDownRight size={14}/>SE wind strengthening slightly</p>
          </div>
          <div className="assessment-list">
            <h3>ASSESSMENT</h3>
            <div><span>Remaining heating</span><b>HIGH</b></div>
            <div><span>Sea-breeze suppression</span><b>LOW</b></div>
            <div><span>Cloud interference</span><b>LOW</b></div>
          </div>
          <div className="invalidation"><h3>INVALIDATION</h3><p>Temperature trend falls below +0.2°C/h or radar echoes enter the 80 km upstream sector.</p></div>
        </aside>
      </section>

      <section className="market-decision">
        <div><span className="index">(04) / MARKET</span><h2>Evidence becomes a position.</h2></div>
        <div className="market-numbers"><span>YES ASK<strong>{data.market}¢</strong></span><span>FAIR VALUE<strong>{data.agent}¢</strong></span><span>EXPECTED EDGE<strong>+{data.agent - data.market}¢</strong></span></div>
        <div className="action-box"><span>ACTION</span><strong>BUY<br/>YES</strong><small>PAPER ONLY / {data.confidence}% CONFIDENCE</small></div>
      </section>
    </main>
  );
}

function App() {
  const [view, setView] = useState("overview");
  const { data, source, loading, refresh } = useWeatherData();
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "instant" });
  }, [view]);
  return (
    <>
      <Header view={view} setView={setView} source={source} loading={loading} refresh={refresh} />
      <div className="demo-banner">交互演示 · 全部数字、走势与决策均为合成示例，不代表真实运行表现。<a href="https://github.com/libuyi543-lang/weather-no-strategy">查看源码与测试 ↗</a></div>
      {view === "overview" ? <Overview data={data} setView={setView} /> : <Evidence data={data} setView={setView} />}
      {view === "evidence" && <footer><span>HERMES//WEATHER</span><p>Weather evidence for prediction markets.</p><small>RESEARCH SYSTEM · PAPER ONLY</small></footer>}
    </>
  );
}

createRoot(document.getElementById("root")).render(<App />);
