// dtbot ダッシュボード(ビルド不要の ES モジュール)。
// 表示する文字列はすべて textContent で入れる(戦略やエラーの文言をHTMLとして解釈しない)。

const TOKEN_KEY = "dtbot-token";
const SESSION_OPEN_MIN = 9 * 60;
const SESSION_SPAN_MIN = 390; // 9:00〜15:30
const JST_OFFSET_SEC = 9 * 3600; // チャートは UTC で描くので、JST に見えるようにずらす

const yen = new Intl.NumberFormat("ja-JP", { maximumFractionDigits: 0 });
const price = new Intl.NumberFormat("ja-JP", { maximumFractionDigits: 1 });
const pct = new Intl.NumberFormat("ja-JP", { style: "percent", maximumFractionDigits: 1 });

const $ = (id) => document.getElementById(id);

const state = {
  token: "",
  snapshot: null,
  symbol: null,
  chart: null,
  candles: null,
  markers: null,
  fills: [],
  ws: null,
  wsRetry: 0,
  view: "live",
  backtestRun: null,
  btCharts: [],
};

// ---- 小道具 ------------------------------------------------------------------------------

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function signed(value) {
  if (value === null || value === undefined) return "—";
  const rounded = Math.round(value);
  if (rounded === 0) return "0円";
  return `${rounded > 0 ? "+" : "−"}${yen.format(Math.abs(rounded))}円`;
}

function tone(value) {
  if (!value) return "";
  return value > 0 ? "up" : "down";
}

function hhmm(iso) {
  return iso ? iso.slice(11, 16) : "";
}

function hhmmss(iso) {
  return iso ? iso.slice(11, 19) : "";
}

function minutesOfDay(iso) {
  const [hh, mm, ss] = iso.slice(11, 19).split(":").map(Number);
  return hh * 60 + mm + ss / 60;
}

function setNotice(message, kind = "warn") {
  const el = $("notice");
  if (!message) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.dataset.kind = kind;
  el.textContent = message;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "X-DTBot-Token": state.token, "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.detail || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return body;
}

// ---- トークン ----------------------------------------------------------------------------

function readToken() {
  const match = location.hash.match(/token=([^&]+)/);
  if (match) {
    sessionStorage.setItem(TOKEN_KEY, decodeURIComponent(match[1]));
    history.replaceState(null, "", location.pathname + location.search);
  }
  return sessionStorage.getItem(TOKEN_KEY) || "";
}

// ---- 状態の帯と操作 -----------------------------------------------------------------------

const MODE_LABELS = { demo: "デモ", paper: "ペーパー", live: "本番", backtest: "バックテスト", viewer: "閲覧のみ" };

function renderHeader(s) {
  const bar = $("statebar");
  bar.dataset.mode = s.mode;
  bar.dataset.state = s.risk ? s.risk.state : "viewer";
  $("mode").textContent = MODE_LABELS[s.mode] || s.mode;
  if (!s.risk) {
    $("state-label").textContent = "閲覧のみ";
    $("state-reason").textContent = "売買エンジンは動いていません。バックテストの結果を表示できます。";
    $("controls").hidden = true;
    return;
  }
  const r = s.risk;
  let label = r.label;
  if (r.state === "active" && !s.entries_allowed) label = "稼働中(新規エントリーの時間外)";
  if (r.flattening && r.state !== "halted") label += "・全決済中";
  $("state-label").textContent = label;
  $("state-reason").textContent = r.reason || "";
  $("state-reason").title = r.reason || "";
  $("controls").hidden = false;
  $("btn-pause").hidden = r.state !== "active";
  $("btn-resume").hidden = r.state !== "paused";
  $("btn-clear").hidden = r.state !== "halted";
  $("btn-halt").hidden = r.state === "halted";
  $("btn-flatten").hidden = r.state === "halted";
  $("clock").textContent = s.now ? `${s.now.slice(0, 10)} ${hhmmss(s.now)}` : "";
}

const CONFIRMS = {
  flatten: {
    title: "すべての建玉を決済しますか?",
    body: "未約定の新規注文を取り消し、すべての建玉を成行で返済します。新規エントリーも止まります(再開ボタンで戻せます)。",
    ok: "全決済する",
  },
  halt: {
    title: "緊急停止しますか?",
    body: "新規エントリーを止め、すべての建玉を成行で返済します。解除するまで新しい取引はしません。",
    ok: "緊急停止する",
  },
  clear_halt: {
    title: "緊急停止を解除しますか?",
    body: "解除しても、新規エントリーは止まったままです。原因を確かめてから、再開ボタンで再開してください。",
    ok: "解除する",
  },
};

function confirmAction(command) {
  const spec = CONFIRMS[command];
  if (!spec) return Promise.resolve(true);
  const dialog = $("confirm");
  $("confirm-title").textContent = spec.title;
  $("confirm-body").textContent = spec.body;
  $("confirm-ok").textContent = spec.ok;
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
  });
}

async function control(command, button) {
  if (!(await confirmAction(command))) return;
  button.disabled = true;
  try {
    const result = await api(`/api/control/${command}`, {
      method: "POST",
      body: JSON.stringify({ confirm: true, reason: "ダッシュボードからの操作" }),
    });
    setNotice("");
    render(result.state);
  } catch (error) {
    setNotice(`操作できませんでした: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

// ---- 立会の時間軸 ------------------------------------------------------------------------

function placeRibbon() {
  for (const el of document.querySelectorAll("[data-from]")) {
    el.style.setProperty("--from", el.dataset.from);
    el.style.setProperty("--to", el.dataset.to);
  }
  for (const el of document.querySelectorAll("[data-at]")) {
    el.style.setProperty("--at", el.dataset.at);
  }
}

function timeToOffset(hhmmText) {
  const [hh, mm] = hhmmText.split(":").map(Number);
  return hh * 60 + mm - SESSION_OPEN_MIN;
}

function renderRibbon(s) {
  if (!s.risk) return;
  const track = $("ribbon");
  track.dataset.phase = s.phase.code;
  $("mark-cutoff").style.setProperty("--at", timeToOffset(s.risk.limits.entry_cutoff));
  $("mark-flatten").style.setProperty("--at", timeToOffset(s.risk.limits.flatten_at));
  $("mark-cutoff").title = `${s.risk.limits.entry_cutoff} 以降は新規エントリーしません`;
  $("mark-flatten").title = `${s.risk.limits.flatten_at} にすべての建玉を成行で手仕舞いします`;
  const needle = $("needle");
  const offset = minutesOfDay(s.now) - SESSION_OPEN_MIN;
  if (s.phase.code === "holiday") {
    needle.hidden = true;
  } else {
    needle.hidden = false;
    needle.style.setProperty("--at", Math.max(0, Math.min(SESSION_SPAN_MIN, offset)).toFixed(2));
    $("needle-label").textContent = hhmm(s.now);
  }
  const phaseText = {
    holiday: "本日は休場日です。",
    pre_open: "寄り付き前です。9:00 から取引を始めます。",
    morning: "前場の取引中です。",
    lunch: "昼休みです。12:30 に後場が始まります。",
    afternoon: "後場の取引中です。",
    pre_close: "引け前の注文受付時間です(約定しません)。",
    closed: "本日の取引は終わりました。",
  }[s.phase.code];
  $("phase").textContent = `${phaseText} 新規エントリーは ${s.risk.limits.entry_cutoff} まで、${s.risk.limits.flatten_at} に全建玉を手仕舞いします。`;
}

// ---- 損益 --------------------------------------------------------------------------------

function renderPnl(s) {
  if (!s.pnl) return;
  const p = s.pnl;
  const net = $("pnl-net");
  net.textContent = signed(p.net);
  net.className = `pnl__net num ${tone(p.net)}`;
  const limit = s.risk.limits.max_daily_loss;
  $("budget-halt").textContent = `−${yen.format(limit)}円で緊急停止`;
  const usage = Math.min(Math.max(s.risk.loss_usage, 0), 1);
  const track = document.querySelector(".budget__track");
  if (p.net >= 0) track.dataset.profit = "";
  else delete track.dataset.profit;
  $("budget-fill").style.width = `${usage * 100}%`;
  const room = yen.format(Math.max(limit + p.net, 0));
  $("budget-caption").textContent =
    p.net >= 0
      ? `緊急停止まで、あと ${room}円の損失の余裕があります。`
      : `損失上限の ${pct.format(usage)} を使っています(緊急停止まで あと ${room}円)。`;
  setFigure("pnl-realized", signed(p.realized), tone(p.realized));
  setFigure("pnl-unrealized", signed(p.unrealized), tone(p.unrealized));
  setFigure("pnl-fees", `${yen.format(p.fees)}円`);
  setFigure("pnl-trades", p.trades ? `${p.trades}回(勝率 ${pct.format(p.win_rate)})` : "0回");
  setFigure("exposure", `${yen.format(s.risk.exposure)}円`);
  const f = s.feed;
  let feedText = "—";
  if (f && f.name) {
    if (f.last_message_age_sec === null) feedText = f.connected ? "受信待ち" : "未接続";
    else if (f.last_message_age_sec < 5) feedText = "受信中";
    else feedText = `${Math.round(f.last_message_age_sec)}秒前に受信`;
  }
  setFigure("feed", feedText, f && f.last_message_age_sec > 30 ? "up" : "");
}

function setFigure(id, text, cls = "") {
  const el = $(id);
  el.textContent = text;
  el.classList.remove("up", "down");
  if (cls) el.classList.add(cls);
}

// ---- 表 ----------------------------------------------------------------------------------

function fillTable(id, rows, emptyId) {
  const tbody = document.querySelector(`#${id} tbody`);
  tbody.replaceChildren(...rows);
  $(emptyId).hidden = rows.length > 0;
  document.querySelector(`#${id}`).hidden = rows.length === 0;
}

function sideCell(sideLabel, side) {
  return h("td", { class: side === "buy" ? "up" : "down" }, sideLabel);
}

function renderTables(s) {
  const names = Object.fromEntries((s.symbols || []).map((x) => [x.symbol, x.name]));
  fillTable(
    "positions",
    (s.positions || []).map((p) =>
      h(
        "tr",
        {},
        h("td", {}, `${p.symbol} ${p.name || ""}`),
        h("td", { class: `num ${p.qty > 0 ? "up" : "down"}` }, `${p.qty > 0 ? "買" : "売"} ${yen.format(Math.abs(p.qty))}`),
        h("td", { class: "num" }, price.format(p.avg_price)),
        h("td", { class: "num" }, p.last === null ? "—" : price.format(p.last)),
        h("td", { class: `num ${tone(p.unrealized)}` }, signed(p.unrealized)),
      ),
    ),
    "positions-empty",
  );
  fillTable(
    "orders",
    (s.orders || []).slice(0, 40).map((o) => {
      const kind = o.order_type === "market" ? "成行" : `指値 ${price.format(o.limit_price)}`;
      const effect = o.effect === "open" ? "新規" : "返済";
      return h(
        "tr",
        { dataset: o.active ? { active: "" } : {}, title: o.reason || o.tag || "" },
        h("td", { class: "t" }, hhmmss(o.created_at)),
        h("td", {}, `${o.symbol} ${names[o.symbol] || ""}`),
        h("td", { class: o.side === "buy" ? "up" : "down" }, `${effect}${o.side_label} ${kind}`),
        h("td", { class: "num" }, `${yen.format(o.filled_qty)} / ${yen.format(o.qty)}`),
        h("td", { class: `status status--${o.status}` }, o.status_label),
      );
    }),
    "orders-empty",
  );
  fillTable(
    "trips",
    (s.round_trips || []).map((t) =>
      h(
        "tr",
        {},
        h("td", { class: "t" }, hhmm(t.closed_at)),
        h("td", {}, t.symbol),
        sideCell(t.side_label, t.side),
        h("td", { class: "num" }, yen.format(t.qty)),
        h("td", { class: "num" }, `${price.format(t.entry_price)} → ${price.format(t.exit_price)}`),
        h("td", { class: `num ${tone(t.pnl)}` }, signed(t.pnl)),
      ),
    ),
    "trips-empty",
  );
  const log = $("log");
  log.replaceChildren(...(s.events || []).slice(0, 120).map(logItem));
}

function logItem(e) {
  return h("li", { dataset: { level: e.level } }, h("time", { datetime: e.time }, hhmmss(e.time)), h("span", {}, e.message));
}

// ---- チャート ----------------------------------------------------------------------------

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function chartOptions() {
  return {
    autoSize: true,
    layout: {
      background: { color: cssVar("--panel") },
      textColor: cssVar("--muted"),
      fontFamily: cssVar("--font-fixed"),
      attributionLogo: true,
    },
    grid: { vertLines: { color: cssVar("--line") }, horzLines: { color: cssVar("--line") } },
    rightPriceScale: { borderColor: cssVar("--line-strong") },
    timeScale: { borderColor: cssVar("--line-strong"), timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 },
    localization: { locale: "ja-JP" },
  };
}

function ensureChart() {
  if (state.chart || !window.LightweightCharts) return;
  const LC = window.LightweightCharts;
  state.chart = LC.createChart($("chart"), chartOptions());
  state.candles = state.chart.addSeries(LC.CandlestickSeries, {
    upColor: cssVar("--up"),
    downColor: cssVar("--down"),
    borderUpColor: cssVar("--up"),
    borderDownColor: cssVar("--down"),
    wickUpColor: cssVar("--up"),
    wickDownColor: cssVar("--down"),
    priceFormat: { type: "price", precision: 1, minMove: 0.1 },
  });
  state.markers = LC.createSeriesMarkers(state.candles, []);
}

function candle(bar) {
  return { time: bar.time + JST_OFFSET_SEC, open: bar.open, high: bar.high, low: bar.low, close: bar.close };
}

function renderMarkers() {
  if (!state.markers) return;
  const markers = state.fills
    .filter((f) => f.symbol === state.symbol)
    .map((f) => ({
      time: Math.floor((f.unix + JST_OFFSET_SEC) / 60) * 60,
      position: f.side === "buy" ? "belowBar" : "aboveBar",
      shape: f.side === "buy" ? "arrowUp" : "arrowDown",
      color: f.side === "buy" ? cssVar("--up") : cssVar("--down"),
      text: `${f.effect === "open" ? "新規" : "返済"}${f.side_label}`,
    }))
    .sort((a, b) => a.time - b.time);
  state.markers.setMarkers(markers);
}

async function loadBars(symbol) {
  ensureChart();
  if (!state.candles) return;
  try {
    const data = await api(`/api/bars?symbol=${encodeURIComponent(symbol)}`);
    const bars = data.bars.map(candle);
    if (data.current) bars.push(candle(data.current));
    state.candles.setData(bars);
    state.fills = data.fills;
    renderMarkers();
    state.chart.timeScale().scrollToRealTime();
  } catch (error) {
    setNotice(`チャートを読み込めませんでした: ${error.message}`, "error");
  }
}

function renderSymbols(s) {
  const box = $("symbols");
  const list = s.symbols || [];
  if (!state.symbol && list.length) {
    state.symbol = list[0].symbol;
    loadBars(state.symbol);
  }
  box.replaceChildren(
    ...list.map((x) =>
      h(
        "button",
        {
          type: "button",
          class: "symbol",
          "aria-pressed": x.symbol === state.symbol ? "true" : "false",
          onclick: () => {
            state.symbol = x.symbol;
            renderSymbols(state.snapshot);
            loadBars(x.symbol);
          },
        },
        `${x.symbol} ${x.name || ""}`,
        h("span", { class: "num" }, x.last === null ? "" : price.format(x.last)),
      ),
    ),
  );
}

// ---- 全体の描画 --------------------------------------------------------------------------

function render(s) {
  state.snapshot = s;
  renderHeader(s);
  if (!s.risk) return;
  renderRibbon(s);
  renderPnl(s);
  renderTables(s);
  renderSymbols(s);
}

// ---- WebSocket ---------------------------------------------------------------------------

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${location.host}/ws?token=${encodeURIComponent(state.token)}`);
  state.ws = ws;
  ws.addEventListener("open", () => {
    state.wsRetry = 0;
    setNotice("");
  });
  ws.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    switch (msg.type) {
      case "snapshot":
        render(msg.snapshot);
        break;
      case "bar":
        if (msg.bar.symbol === state.symbol && state.candles) state.candles.update(candle(msg.bar));
        break;
      case "tick":
        if (msg.symbol === state.symbol && msg.bar && state.candles) state.candles.update(candle(msg.bar));
        break;
      case "fill":
        state.fills.push(msg.fill);
        renderMarkers();
        break;
      case "event":
        $("log").prepend(logItem(msg.event));
        break;
      case "hello":
        render({ mode: msg.mode });
        break;
      default:
        break;
    }
  });
  ws.addEventListener("close", (event) => {
    if (event.code === 1008) {
      setNotice("ダッシュボードに接続できません。コンソールに表示された URL(#token= を含む)から開き直してください。", "error");
      return;
    }
    if (state.snapshot && state.snapshot.mode === "viewer") return;
    state.wsRetry += 1;
    const delay = Math.min(1000 * 2 ** (state.wsRetry - 1), 15000);
    setNotice("ボットとの接続が切れました。再接続しています…");
    setTimeout(connect, delay);
  });
}

// ---- バックテスト ------------------------------------------------------------------------

const SUMMARY_FIELDS = [
  ["net_pnl", "損益の合計", (v) => signed(v), (v) => tone(v)],
  ["return_pct", "資金に対する損益", (v) => pct.format(v), (v) => tone(v)],
  ["trades", "取引回数", (v) => `${v}回`],
  ["win_rate", "勝率", (v) => (v === null ? "—" : pct.format(v))],
  ["profit_factor", "プロフィットファクター", (v) => (v === null ? "—" : v.toFixed(2))],
  ["avg_trade", "1回あたりの平均", (v) => signed(v), (v) => tone(v)],
  ["max_drawdown", "最大ドローダウン", (v) => signed(v), () => "down"],
  ["sharpe", "シャープレシオ(年率)", (v) => (v === null ? "—" : v.toFixed(2))],
  ["trading_days", "取引日数", (v, s) => `${v}日(プラス ${s.positive_days}日)`],
  ["avg_holding_minutes", "平均保有時間", (v) => (v === null ? "—" : `${v.toFixed(0)}分`)],
  ["long_pnl", "買いの損益", (v, s) => `${signed(v)}(${s.long_trades}回)`, (v) => tone(v)],
  ["short_pnl", "売りの損益", (v, s) => `${signed(v)}(${s.short_trades}回)`, (v) => tone(v)],
];

async function loadBacktests() {
  try {
    const runs = await api("/api/backtests");
    $("runs-empty").hidden = runs.length > 0;
    $("runs").replaceChildren(
      ...runs.map((r) =>
        h(
          "li",
          {},
          h(
            "button",
            {
              type: "button",
              dataset: { runId: r.run_id },
              "aria-current": r.run_id === state.backtestRun ? "true" : "false",
              onclick: () => showBacktest(r.run_id),
            },
            h("span", { class: "run__title" }, `${r.start_date} 〜 ${r.end_date}`),
            h("span", { class: "run__sub" }, `${r.strategy}、${r.symbols.join("・")}、${signed(r.summary.net_pnl)}(${r.summary.trades}回)`),
          ),
        ),
      ),
    );
    if (!state.backtestRun && runs.length) showBacktest(runs[0].run_id);
  } catch (error) {
    setNotice(`バックテストの一覧を読み込めませんでした: ${error.message}`, "error");
  }
}

async function showBacktest(runId) {
  state.backtestRun = runId;
  for (const b of document.querySelectorAll("#runs button")) {
    b.setAttribute("aria-current", String(b.dataset.runId === runId));
  }
  let data;
  try {
    data = await api(`/api/backtests/${encodeURIComponent(runId)}`);
  } catch (error) {
    setNotice(`バックテストの結果を読み込めませんでした: ${error.message}`, "error");
    return;
  }
  $("bt-detail").hidden = false;
  $("bt-title").textContent = `${data.start_date} 〜 ${data.end_date} の結果`;
  const params = Object.entries(data.params).map(([k, v]) => `${k}=${v}`).join("、");
  $("bt-meta").textContent = `戦略 ${data.strategy}(${params})。銘柄 ${data.symbols.join("・")}。データ ${data.summary.sim?.source === "ticks" ? "ティック" : "1分足"}、スリッページ込み ${yen.format(data.summary.sim?.slippage_cost ?? 0)}円。`;
  const s = data.summary;
  $("bt-figures").replaceChildren(
    ...SUMMARY_FIELDS.map(([key, label, fmt, cls]) =>
      h("div", {}, h("dt", {}, label), h("dd", { class: `num ${cls ? cls(s[key]) : ""}` }, fmt(s[key], s))),
    ),
  );
  drawBacktestCharts(data);
  document.querySelector("#bt-trades tbody").replaceChildren(
    ...data.trades.map((t) =>
      h(
        "tr",
        {},
        h("td", { class: "t" }, `${t.opened_at.slice(5, 10)} ${hhmm(t.opened_at)}`),
        h("td", {}, t.symbol),
        sideCell(t.side_label, t.side),
        h("td", { class: "num" }, yen.format(t.qty)),
        h("td", { class: "num" }, `${price.format(t.entry_price)} → ${price.format(t.exit_price)}`),
        h("td", { class: "num" }, `${t.holding_minutes.toFixed(0)}分`),
        h("td", { class: `num ${tone(t.pnl)}` }, signed(t.pnl)),
      ),
    ),
  );
}

function drawBacktestCharts(data) {
  const LC = window.LightweightCharts;
  if (!LC) return;
  for (const c of state.btCharts) c.remove();
  state.btCharts = [];
  const equity = LC.createChart($("bt-equity"), chartOptions());
  const line = equity.addSeries(LC.BaselineSeries, {
    baseValue: { type: "price", price: 0 },
    topLineColor: cssVar("--up"),
    bottomLineColor: cssVar("--down"),
    topFillColor1: "transparent",
    topFillColor2: "transparent",
    bottomFillColor1: "transparent",
    bottomFillColor2: "transparent",
    lineWidth: 2,
    priceFormat: { type: "price", precision: 0, minMove: 1 },
  });
  const seen = new Set();
  const points = [];
  for (const [iso, value] of data.equity) {
    const t = Math.floor(Date.parse(iso) / 1000) + JST_OFFSET_SEC;
    if (seen.has(t)) points[points.length - 1].value = value;
    else {
      seen.add(t);
      points.push({ time: t, value });
    }
  }
  points.sort((a, b) => a.time - b.time);
  line.setData(points);
  equity.timeScale().fitContent();
  const daily = LC.createChart($("bt-daily"), { ...chartOptions(), timeScale: { ...chartOptions().timeScale, timeVisible: false } });
  const bars = daily.addSeries(LC.HistogramSeries, { priceFormat: { type: "price", precision: 0, minMove: 1 } });
  bars.setData(data.daily.map((d) => ({ time: d.date, value: d.pnl, color: d.pnl >= 0 ? cssVar("--up") : cssVar("--down") })));
  daily.timeScale().fitContent();
  state.btCharts.push(equity, daily);
}

function switchView(view) {
  state.view = view;
  $("view-live").hidden = view !== "live";
  $("view-backtest").hidden = view !== "backtest";
  $("tab-live").setAttribute("aria-pressed", String(view === "live"));
  $("tab-backtest").setAttribute("aria-pressed", String(view === "backtest"));
  if (view === "backtest") loadBacktests();
}

// ---- 起動 --------------------------------------------------------------------------------

async function main() {
  placeRibbon();
  state.token = readToken();
  $("tab-live").addEventListener("click", () => switchView("live"));
  $("tab-backtest").addEventListener("click", () => switchView("backtest"));
  $("btn-pause").addEventListener("click", (e) => control("pause", e.currentTarget));
  $("btn-resume").addEventListener("click", (e) => control("resume", e.currentTarget));
  $("btn-flatten").addEventListener("click", (e) => control("flatten", e.currentTarget));
  $("btn-halt").addEventListener("click", (e) => control("halt", e.currentTarget));
  $("btn-clear").addEventListener("click", (e) => control("clear_halt", e.currentTarget));
  if (!state.token) {
    setNotice("コンソールに表示された URL(#token= を含む)から開いてください。", "error");
    $("state-label").textContent = "トークンがありません";
    return;
  }
  try {
    const health = await fetch("/api/health").then((r) => r.json());
    $("version").textContent = `dtbot ${health.version}`;
    const snapshot = await api("/api/state");
    render(snapshot);
    if (snapshot.mode === "viewer") {
      switchView("backtest");
      $("tab-live").hidden = true;
      return;
    }
    connect();
  } catch (error) {
    if (error.status === 401) {
      sessionStorage.removeItem(TOKEN_KEY);
      setNotice("トークンが正しくありません。コンソールに表示された最新の URL から開き直してください。", "error");
    } else {
      setNotice(`ボットに接続できません: ${error.message}`, "error");
    }
  }
}

main();
