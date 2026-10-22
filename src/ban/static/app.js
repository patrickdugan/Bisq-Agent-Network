"use strict";
const byId = (id) => document.getElementById(id);
const state = { currency: "PHP" };
function node(tag, text, className) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = String(text);
  if (className) e.className = className;
  return e;
}
function clear(id) { byId(id).replaceChildren(); }
async function request(path) {
  const response = await fetch(path, { cache: "no-store", credentials: "same-origin" });
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || data.error || "Request failed");
  return data;
}
function safeText(value, fallback) {
  return value === undefined || value === null || value === "" ? fallback : String(value);
}
function item(containerId, title, value, meta, kind) {
  const e = node("div", undefined, kind || "offer");
  const head = node("div", undefined, "headline");
  head.append(node("span", title), node("span", value, "price"));
  e.append(head, node("p", meta));
  byId(containerId).append(e);
}
function renderOffers(data) {
  clear("offers");
  if (!data.offers.length) item("offers", "No visible offers", "", "Review the market in Bisq.");
  for (const offer of data.offers) {
    const price = offer.fiat_per_btc ? offer.fiat_per_btc + " " + data.currency + " / BTC" : "Dynamic quote";
    const detail = offer.maker_direction + " BTC · " + offer.payment_methods.join(", ") +
      " · " + offer.amount_display + " · Availability unverified";
    item("offers", offer.nickname, price, detail);
  }
}
function renderMatches(data) {
  clear("matches");
  if (!data.offers.length) item("matches", "No matching sellers", "", "Try another permitted payment method.");
  for (const offer of data.offers) {
    const btc = offer.indicative_btc === null ? "Dynamic price" : offer.indicative_btc + " BTC";
    item("matches", offer.nickname, btc, offer.price_note + " · " + offer.payment_methods.join(", "));
  }
}
function renderPlan(data) {
  clear("plan");
  for (const task of data.tasks) {
    item("plan", "P" + task.priority + " · " + task.action.replaceAll("_", " "), "HUMAN",
      task.rationale + " " + task.next_step, "task");
  }
}
function renderLedger(data) {
  clear("ledger");
  item("ledger", "Total recorded events", String(data.total_events), data.caveat);
  const kinds = Object.entries(data.by_event);
  for (const [kind, count] of kinds) item("ledger", kind.replaceAll("_", " "), count, "Operator-reported event");
}
async function refresh() {
  byId("status").textContent = "Loading…";
  const currency = state.currency;
  try {
    const [health, market, report, plan, ledger] = await Promise.all([
      request("/api/health"),
      request("/api/offers?currency=" + encodeURIComponent(currency)),
      request("/api/report?currency=" + encodeURIComponent(currency)),
      request("/api/plan?currency=" + encodeURIComponent(currency)),
      request("/api/metrics")
    ]);
    byId("sourceBadge").textContent = health.source === "SYNTHETIC_FIXTURE" ? "SYNTHETIC DEMO DATA" : "LOCAL BISQ 2 NODE";
    byId("total").textContent = report.total_offers;
    byId("sellers").textContent = report.btc_seller_offers;
    byId("buyers").textContent = report.btc_buyer_offers;
    byId("rails").textContent = Object.keys(report.seller_payment_method_coverage).length;
    renderOffers(market);
    renderPlan(plan);
    renderLedger(ledger);
    byId("status").textContent = "Updated " + new Date(market.fetched_at).toLocaleTimeString() + " · Read only";
  } catch (error) {
    byId("status").textContent = "Data unavailable: " + error.message;
  }
}
byId("refresh").addEventListener("click", refresh);
byId("currency").addEventListener("change", (event) => {
  state.currency = event.target.value;
  refresh();
});
byId("quoteForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const amount = byId("amount").value;
  const payment = byId("payment").value;
  try {
    const result = await request("/api/quote?currency=" + encodeURIComponent(state.currency) +
      "&amount=" + encodeURIComponent(amount) + "&payment=" + encodeURIComponent(payment));
    renderMatches(result);
  } catch (error) {
    clear("matches");
    item("matches", "Quote unavailable", "", error.message);
  }
});
refresh();
