import { SOURCES, sourceFor, portalUrl, filterFilings, isUsdPoint } from "./explorer.mjs";

const CARDS = [["revenue", "Revenue"], ["net_income", "Net income"], ["free_cash_flow", "Free cash flow"], ["diluted_eps", "Diluted EPS"]];
const TABS = {
  income: [["revenue", "Revenue"], ["gross_profit", "Gross profit"], ["operating_income", "Operating income"], ["net_income", "Net income"], ["diluted_eps", "Diluted EPS ($)"]],
  cash: [["operating_cash_flow", "Operating cash flow"], ["capital_expenditures", "Capital expenditures"], ["free_cash_flow", "Free cash flow"], ["share_repurchases", "Share buybacks"], ["dividends_paid", "Dividends paid"]],
  balance: [["cash_and_equivalents", "Cash and equivalents"], ["total_assets", "Total assets"], ["total_liabilities", "Total liabilities"], ["long_term_debt", "Long-term debt"], ["stockholders_equity", "Shareholders' equity"]],
};
const METRICS = [...new Set([...CARDS, ...Object.values(TABS).flat()].map(([code]) => code))];
const PER_SHARE = new Set(["diluted_eps"]);
const state = { company: null, years: [], values: {}, tab: "income", scale: 1e9, filings: [], filingOffset: 0 };
const $ = (id) => document.getElementById(id);
const el = (tag, text, attrs = {}) => {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
};

try { $("key").value = localStorage.getItem("xbrl_api_key") || ""; } catch {}
$("key").addEventListener("change", () => { try { localStorage.setItem("xbrl_api_key", $("key").value); } catch {} });

async function api(path) {
  const headers = $("key").value ? { Authorization: "Bearer " + $("key").value } : {};
  const response = await fetch(path, { headers });
  if (!response.ok) {
    let detail = response.statusText;
    try { const body = await response.json(); detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail); } catch {}
    throw new Error(response.status === 401 ? "The API needs a key. Enter it in the API key box." : `${response.status}: ${detail}`);
  }
  return response.json();
}
const fail = (error) => { $("status").textContent = error.message; };

let timer, searchVersion = 0, selectionVersion = 0, detailVersion = 0;
for (const source of SOURCES) $("market").append(el("option", source.region, { value: source.code }));
const coverage = document.querySelector(".coverage");
$("company").after(coverage);
function renderSources() {
  const sources = SOURCES.filter((s) => !$("market").value || s.code === $("market").value);
  $("source-cards").replaceChildren(...sources.map((source) => {
    const card = el("article", null, { class: "source-card" });
    const link = el("a", `Open ${source.name} ↗`, { href: portalUrl(source, $("q").value), target: "_blank", rel: "noopener noreferrer" });
    card.append(el("span", source.mode, { class: "badge" }), el("h3", source.region), el("p", source.markets, { class: "faint" }), el("p", source.hint, { class: "faint" }), link);
    return card;
  }));
  $("coverage-help").textContent = $("q").value.trim() ? `Looking for “${$("q").value.trim()}”? ${sources.length === 1 && sources[0].code !== "sec" ? "Open the source below and enter the company name there." : "Continue at a source below. EDGAR accepts the query in the link; enter it on the other portals."}` : "Choose a region to see its filing source and acquisition requirements.";
}
function updateMarket() {
  const source = SOURCES.find((s) => s.code === $("market").value);
  $("exchange").value = "";
  $("exchange").disabled = !!source && !source.exchanges.length;
  $("exchange").placeholder = $("exchange").disabled ? "Exchange metadata unavailable" : "All exchanges";
  const exchanges = source ? source.exchanges : [...new Set(SOURCES.flatMap((s) => s.exchanges))];
  $("exchange-options").replaceChildren(...exchanges.map((name) => el("option", name, { value: name })));
  renderSources();
  searchCompanies();
}
async function searchCompanies() {
  clearTimeout(timer);
  ++selectionVersion; ++detailVersion;
  if (state.company) history.replaceState(null, "", location.pathname + location.search);
  state.company = null;
  $("company").hidden = true;
  $("filing-detail").hidden = true;
  $("empty").hidden = false;
  const version = ++searchVersion, query = $("q").value.trim();
  $("results").replaceChildren();
  $("status").textContent = "";
  renderSources();
  if (!query) { $("search-status").textContent = ""; return; }
  $("search-status").textContent = "Searching imported companies…";
  try {
    const params = new URLSearchParams({ q: query, limit: "100" });
    if ($("market").value) params.set("source", $("market").value);
    if ($("exchange").value.trim()) params.set("exchange", $("exchange").value.trim());
    const data = await api("/v1/companies?" + params);
    if (version !== searchVersion) return;
    $("search-status").textContent = data.items.length ? `${data.count}${data.count === data.limit ? "+" : ""} warehouse result${data.count === 1 ? "" : "s"}. Select a company to explore. ${data.count === data.limit ? "Refine the company name or exchange to narrow the results." : ""}` : `No imported company matches “${query}” with these filters. This does not mean the stock is unlisted. Use the regional filing sources below.`;
    $("results").replaceChildren(...data.items.map((company) => {
      const button = el("button", null, { type: "button", class: "result" });
      const label = el("span");
      label.append(el("span", company.tickers.join(" / ") || "Company", { class: "symbol" }), el("span", company.name, { class: "company-name" }));
      const meta = el("span", null, { class: "faint" });
      meta.append(el("span", company.exchanges.filter(Boolean).join(" / ") || "Exchange not supplied"), el("br"), el("span", `Company ID ${company.cik}`));
      button.append(label, meta);
      button.addEventListener("click", () => select(company));
      return button;
    }));
  } catch (error) {
    if (version !== searchVersion) return;
    $("search-status").textContent = "Search could not complete. Check API access or try again.";
    fail(error);
  }
}
$("search-form").addEventListener("submit", (event) => { event.preventDefault(); searchCompanies(); });
$("q").addEventListener("input", () => {
  ++searchVersion;
  clearTimeout(timer);
  $("results").replaceChildren();
  $("search-status").textContent = "";
  timer = setTimeout(searchCompanies, 250);
});
$("market").addEventListener("change", updateMarket);
$("exchange").addEventListener("input", () => { ++searchVersion; clearTimeout(timer); timer = setTimeout(searchCompanies, 250); });
document.querySelectorAll("[data-query]").forEach((button) => button.addEventListener("click", () => { $("q").value = button.dataset.query; searchCompanies(); }));
updateMarket();

async function select(company) {
  const version = ++selectionVersion;
  ++detailVersion;
  $("filing-detail").hidden = true;
  state.company = company;
  $("q").value = company.tickers[0] || company.name;
  renderSources();
  state.filings = [];
  state.filingOffset = 0;
  state.years = []; state.values = {};
  $("empty").hidden = true;
  $("company").hidden = false;
  $("name").textContent = company.name;
  $("ticker").textContent = [company.tickers.join(" / "), company.exchanges.filter(Boolean).join(" / "), `Company ID ${company.cik}`].filter(Boolean).join(" · ");
  $("source").textContent = "";
  $("company-status").textContent = "Loading financials and filing history…";
  $("filing-status").textContent = "Loading filings…";
  $("filings").querySelector("tbody").replaceChildren();
  $("filing-count").textContent = "";
  $("more-filings").hidden = true;
  $("filing-form").value = ""; $("filing-year").value = ""; $("filing-text").value = "";
  history.replaceState(null, "", "#" + company.cik);
  renderCards(); renderChart(); renderTable();
  const params = new URLSearchParams({ metrics: METRICS.join(","), limit: "10000" });
  params.append("period_type", "annual"); params.append("period_type", "instant");
  const results = await Promise.allSettled([api(`/v1/series/${company.cik}?${params}`), loadFilings(version)]);
  if (version !== selectionVersion) return;
  if (results[0].status === "fulfilled") {
    const points = results[0].value.items;
    // The current financial presentation is USD-only; other units stay in filing records.
    buildYears(points.filter(isUsdPoint));
    $("company-status").textContent = state.years.length ? "Annual financials in USD. Other reported units remain available in filing records." : "No mapped USD annual financials are available. You can still browse the filing library below.";
    renderCards(); renderChart(); renderTable();
  } else {
    $("company-status").textContent = "Financial summary unavailable: " + results[0].reason.message;
  }
  if (results[1].status === "rejected") $("filing-status").textContent = "Filings unavailable: " + results[1].reason.message;
  $("company").scrollIntoView({ block: "start" });
}

async function loadFilings(version = selectionVersion) {
  const company = state.company, offset = state.filingOffset;
  $("more-filings").disabled = true;
  try {
    const page = await api(`/v1/companies/${company.cik}/filings?limit=100&offset=${offset}`);
    if (version !== selectionVersion) return;
    state.filings.push(...page.items);
    state.filingOffset += page.items.length;
    $("more-filings").hidden = page.items.length < 100 || state.filingOffset > 10000;
    updateFilingFilters(); renderFilings();
  } finally { if (version === selectionVersion) $("more-filings").disabled = false; }
}
function updateFilingFilters() {
  for (const [id, values, title] of [
    ["filing-form", [...new Set(state.filings.map((f) => f.form))].sort(), "All types"],
    ["filing-year", [...new Set(state.filings.map((f) => String(f.filed_date).slice(0, 4)))].sort().reverse(), "All years"],
  ]) {
    const chosen = $(id).value;
    $(id).replaceChildren(el("option", title, { value: "" }), ...values.map((v) => el("option", v, { value: v })));
    $(id).value = chosen;
  }
}
$("more-filings").addEventListener("click", async () => {
  try { await loadFilings(); } catch (error) { $("filing-status").textContent = "Could not load more filings: " + error.message; }
});
for (const id of ["filing-form", "filing-year", "filing-text"]) $(id).addEventListener("input", renderFilings);

// Annual values by fiscal year; balance-sheet instants join on the fiscal year's end date.
function buildYears(points) {
  const values = {}, yearEnd = {};
  for (const p of points) {
    if (p.period_type !== "annual" || !p.fiscal_year) continue;
    (values[p.metric] ||= {})[p.fiscal_year] = Number(p.value);
    yearEnd[p.period_end] = p.fiscal_year;
  }
  for (const p of points) {
    const year = yearEnd[p.period_end];
    if (p.period_type === "instant" && year) (values[p.metric] ||= {})[year] = Number(p.value);
  }
  const years = [...new Set(Object.values(values).flatMap((v) => Object.keys(v).map(Number)))].sort();
  state.years = years.slice(-5);
  state.values = values;
  const size = Math.max(0, ...state.years.map((y) => Math.abs(values.revenue?.[y] ?? values.total_assets?.[y] ?? 0)));
  state.scale = size >= 1e10 ? 1e9 : 1e6;
}

const value = (code, year) => state.values[code]?.[year];
function money(code, v, withUnit = true) {
  if (v === undefined) return "–";
  if (PER_SHARE.has(code)) return (withUnit ? "$" : "") + v.toFixed(2);
  const scaled = v / state.scale;
  const text = Math.abs(scaled).toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  return (v < 0 ? "−" : "") + (withUnit ? "$" + text + (state.scale === 1e9 ? "B" : "M") : text);
}

function renderCards() {
  const [last, prev] = [state.years.at(-1), state.years.at(-2)];
  $("cards").replaceChildren(...CARDS.map(([code, label]) => {
    const card = el("div", null, { class: "card" });
    const now = value(code, last), before = value(code, prev);
    card.append(el("div", label, { class: "label" }), el("div", money(code, now), { class: "value" }));
    if (now !== undefined && before) {
      const change = (now / before - 1) * 100 * Math.sign(before);
      card.append(el("div", `${change >= 0 ? "+" : "−"}${Math.abs(change).toFixed(1)}% vs FY${prev}`, { class: change >= 0 ? "up" : "down", style: "font-size:13px" }));
    } else {
      card.append(el("div", " ", { style: "font-size:13px" }));
    }
    return card;
  }));
}

function renderChart() {
  const svg = $("chart"), ns = "http://www.w3.org/2000/svg";
  svg.replaceChildren();
  const width = svg.clientWidth || 840, height = 160, top = 22, bottom = 24;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const bars = state.years.map((year) => ({ year, v: value("revenue", year) }));
  $("chart-label").textContent = `Revenue, last ${bars.length} years`;
  const max = Math.max(0, ...bars.map((b) => b.v ?? 0));
  if (!max) return;
  const slot = width / bars.length;
  const add = (tag, attrs, text) => {
    const node = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text !== undefined) node.textContent = text;
    svg.append(node);
  };
  bars.forEach((b, i) => {
    const x = i * slot, centre = x + slot / 2;
    add("text", { x: centre, y: height - 6, "text-anchor": "middle" }, `FY${String(b.year).slice(2)}`);
    if (b.v === undefined || b.v <= 0) return;
    const h = (b.v / max) * (height - top - bottom);
    add("rect", { x: x + slot * 0.18, y: height - bottom - h, width: slot * 0.64, height: h, rx: 3, fill: "var(--bar)" });
    add("text", { x: centre, y: height - bottom - h - 6, "text-anchor": "middle" }, money("revenue", b.v, false));
  });
}
window.addEventListener("resize", () => { if (state.company) renderChart(); });

const dateText = (iso) => iso ? new Date(iso + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "";

function renderFilings() {
  const filings = filterFilings(state.filings, { form: $("filing-form").value, year: $("filing-year").value, text: $("filing-text").value });
  $("filing-count").textContent = `${state.filings.length} loaded`;
  $("filing-status").textContent = `${filings.length} of ${state.filings.length} loaded filings shown. Filters apply to loaded pages.${$("more-filings").hidden ? "" : " Load more to include older reports."}${state.filingOffset > 10000 ? " API paging limit reached; use a source portal for older reports." : ""}`;
  $("filings").querySelector("tbody").replaceChildren(...filings.map((filing) => {
    const row = el("tr"), report = el("td"), actions = el("td");
    report.append(el("strong", filing.form + (filing.amendment ? " · amended" : "")), el("small", filing.accession));
    const source = sourceFor(filing.accession);
    if (source?.code === "sec") {
      const href = `https://www.sec.gov/Archives/edgar/data/${state.company.cik}/${filing.accession.replaceAll("-", "")}/${filing.accession}-index.html`;
      actions.append(el("a", "Original filing ↗", { href, target: "_blank", rel: "noopener noreferrer" }));
    } else if (source) {
      actions.append(el("a", `${source.name} portal ↗`, { href: source.url, target: "_blank", rel: "noopener noreferrer" }));
    }
    const view = el("button", "View record", { type: "button" });
    view.addEventListener("click", () => viewFiling(filing)); actions.append(view);
    row.append(report, el("td", dateText(filing.period_end_date) || "Not supplied"), el("td", dateText(filing.filed_date)), actions);
    return row;
  }));
  if (!filings.length) {
    const row = el("tr"); row.append(el("td", state.filings.length ? "No filings match these filters." : "No filings have been imported for this company.", { colspan: "4" }));
    $("filings").querySelector("tbody").append(row);
  }
  const latest = state.filings[0];
  $("source").textContent = latest ? `Latest loaded report · ${latest.form} · ${dateText(latest.filed_date)}` : "No loaded reports";
}
async function viewFiling(filing) {
  const version = ++detailVersion;
  $("filing-detail").hidden = false;
  $("detail-title").textContent = `${filing.form} · ${filing.accession}`;
  $("detail-status").textContent = "Loading warehouse record…";
  $("detail-content").replaceChildren();
  $("filing-detail").scrollIntoView({ block: "start" });
  try {
    const params = new URLSearchParams({ cik: state.company.cik, filed_date: filing.filed_date });
    const record = await api(`/v1/filings/${encodeURIComponent(filing.accession)}?${params}`);
    if (version !== detailVersion) return;
    $("detail-status").textContent = "Warehouse extraction summary. Original reports are available through the filing source.";
    for (const [key, value] of Object.entries(record)) {
      $("detail-content").append(el("dt", key.replaceAll("_", " ")), el("dd", value === null ? "Not supplied" : typeof value === "object" ? JSON.stringify(value) : String(value)));
    }
  } catch (error) { if (version === detailVersion) $("detail-status").textContent = "Record unavailable: " + error.message; }
}
$("close-detail").addEventListener("click", () => { ++detailVersion; $("filing-detail").hidden = true; });

document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
  state.tab = b.dataset.tab;
  document.querySelectorAll(".tabs button").forEach((x) => (x.classList.toggle("on", x === b), x.setAttribute("aria-selected", String(x === b))));
  renderTable();
}));

function spark(values) {
  const known = values.filter((v) => v !== undefined);
  if (known.length < 2) return el("span");
  const min = Math.min(...known), max = Math.max(...known), ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("width", "64"); svg.setAttribute("height", "22"); svg.setAttribute("aria-hidden", "true");
  const line = document.createElementNS(ns, "polyline");
  line.setAttribute("points", values.map((v, i) => v === undefined ? null : `${2 + i * 15},${19 - ((v - min) / (max - min || 1)) * 16}`).filter(Boolean).join(" "));
  line.setAttribute("fill", "none"); line.setAttribute("stroke", "var(--bar)"); line.setAttribute("stroke-width", "1.5");
  svg.append(line);
  return svg;
}

function renderTable() {
  $("unit").textContent = `USD ${state.scale === 1e9 ? "billions" : "millions"}`;
  const table = $("table");
  table.replaceChildren();
  const head = el("tr");
  head.append(el("th", ""), ...state.years.map((y) => el("th", `FY${y}`)), el("th", "Trend"));
  table.appendChild(el("thead")).append(head);
  const body = table.appendChild(el("tbody"));
  for (const [code, label] of TABS[state.tab]) {
    const row = el("tr");
    const values = state.years.map((y) => value(code, y));
    row.append(el("td", label));
    for (const v of values) row.append(el("td", money(code, v, false), v < 0 ? { class: "neg" } : {}));
    row.appendChild(el("td")).append(spark(values));
    body.append(row);
  }
}

(async () => {
  const wanted = decodeURIComponent(location.hash.slice(1));
  if (!wanted) return;
  try {
    const match = (await api("/v1/companies?q=" + encodeURIComponent(wanted))).items[0];
    if (match) select(match); else { $("q").value = wanted; searchCompanies(); }
  } catch (e) { fail(e); }
})();
