// /config/custom_components/z2m_tracker/www/z2m-tracker-card.js
// Compact Home Assistant card summarising the Z2M Device Tracker app.
const Z2M_TRACKER_CARD_VERSION = "2.3.0";

const IEEE_RE = /^0x[0-9a-f]{16}$/i;
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
// Panel links stay inside Home Assistant: only a local path ("/app/...") is used, never a javascript: or outside URL.
const localPath = (v) => (typeof v === "string" && /^\/(?!\/)/.test(v) ? v : "");
const nameOf = (d) => {
  if (d.friendly_name && !IEEE_RE.test(d.friendly_name)) return d.friendly_name;
  const named = (d.names || []).filter((n) => n.name && !IEEE_RE.test(n.name));
  return named.length ? named[named.length - 1].name : d.ha_name || d.ieee;
};
const I18N = () => window.Z2MTrackerI18n;
const T = (key, vars, fallback) => I18N()?.t(key, vars, fallback) ?? fallback ?? key;
const KIND_ICON = { offline: "mdi:wifi-off", silent: "mdi:wifi-off", late: "mdi:timer-sand", frozen: "mdi:thermometer-alert", weak_signal: "mdi:signal-cellular-1", signal_drop: "mdi:signal-cellular-2", battery_low: "mdi:battery-alert-variant-outline", battery_soon: "mdi:battery-clock-outline", update: "mdi:chip", unidentified: "mdi:help-circle-outline", replacement: "mdi:link-variant", router_hopping: "mdi:swap-horizontal", slow_router: "mdi:router-wireless", maintenance_due: "mdi:wrench-clock", slow_commands: "mdi:timer-sand", missed_commands: "mdi:gesture-tap-button", reporting_config: "mdi:tune", naming: "mdi:rename", community_avoid: "mdi:thumb-down-outline" };

class Z2MTrackerCard extends HTMLElement {
  setConfig(config) {
    this._config = { max_items: 5, ...config };
    if (this._root) this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    this._lang = this._lang || (() => this._render());
    I18N()?.use(hass, this._lang);
    if (first) this._start();
  }

  connectedCallback() { if (this._hass && !this._timer) this._start(); }

  disconnectedCallback() {
    if (this._lang) I18N()?.forget(this._lang);
    clearInterval(this._timer); this._timer = null;
    if (this._unsub) { this._unsub.then((fn) => fn && fn()).catch(() => {}); this._unsub = null; }
  }

  getCardSize() { return 5; }
  getGridOptions() { return { columns: 6, min_columns: 4, rows: "auto" }; }
  static getStubConfig() { return {}; }
  // Home Assistant draws the visual editor from this schema (labels from the cards' translations).
  static getConfigForm() {
    return {
      schema: [
        { name: "title", selector: { text: {} } },
        { name: "max_items", selector: { number: { min: 1, max: 50, mode: "box" } } },
        { name: "show_community", selector: { boolean: {} } },
      ],
      computeLabel: (s) => T(`card.editor.${s.name}`, null, s.name),
      computeHelper: (s) => (s.name === "show_community" ? T("card.editor.show_community_help") : undefined),
    };
  }

  _start() {
    if (!this._root) {
      this._root = this.attachShadow({ mode: "open" });
      this._root.innerHTML = `<style>
        ha-card { overflow: hidden; }
        .head { display: flex; align-items: center; justify-content: space-between; padding: 16px 16px 4px; }
        .head h2 { margin: 0; font-size: var(--ha-card-header-font-size, 24px); font-weight: 400; color: var(--ha-card-header-color, var(--primary-text-color)); }
        .stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 4px; padding: 8px 12px 4px; }
        .stat { text-align: center; padding: 8px 4px; border-radius: 12px; cursor: pointer; }
        .stat:hover { background: rgba(var(--rgb-primary-text-color, 0, 0, 0), 0.05); }
        .stat .n { font-size: 24px; line-height: 1.2; color: var(--primary-text-color); }
        .stat .l { font-size: 12px; color: var(--secondary-text-color); }
        .stat.warn .n { color: var(--warning-color, #ffa600); }
        .stat.bad .n { color: var(--error-color, #db4437); }
        .section { padding: 8px 16px 0; font-size: 14px; font-weight: 500; color: var(--secondary-text-color); }
        .row { display: flex; align-items: center; gap: 12px; padding: 6px 16px; min-height: 40px; }
        .row ha-icon { --mdc-icon-size: 22px; color: var(--c, var(--state-icon-color)); flex: none; }
        .row .t { flex: 1; min-width: 0; }
        .row .t1 { color: var(--primary-text-color); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .row .t2 { font-size: 12px; color: var(--secondary-text-color); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .empty { padding: 8px 16px; color: var(--secondary-text-color); font-size: 14px; }
        .actions { display: flex; justify-content: flex-end; padding: 8px; border-top: 1px solid var(--divider-color); margin-top: 8px; }
        .actions a { color: var(--primary-color); text-decoration: none; font-weight: 500; font-size: 14px; padding: 8px 12px; border-radius: 18px; }
        .actions a:hover { background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.1); }
        .err { padding: 16px; color: var(--error-color); }
      </style><ha-card><div id="body"></div></ha-card>`;
    }
    this._load();
    if (!this._timer) this._timer = setInterval(() => this._load(), 60000);
    if (!this._unsub && this._hass?.connection) {
      this._unsub = this._hass.connection.subscribeMessage(() => this._load(), { type: "z2m_tracker/subscribe" }).catch(() => null);
    }
  }

  async _load() {
    try { this._data = await this._hass.callWS({ type: "z2m_tracker/devices" }); this._error = null; this._failed = false; }
    catch (err) { this._error = err?.message || null; this._failed = true; }
    this._render();
  }

  // Optional (show_community): the user's models other Zigbee Tracker users rate Avoid or Mixed, worst first.
  _community(cfg) {
    const models = Object.values(this._data?.community?.models || {});
    const order = { avoid: 0, mixed: 1 };
    const flagged = models.filter((m) => m.verdict in order).sort((a, b) => order[a.verdict] - order[b.verdict] || b.reports - a.reports);
    const good = models.filter((m) => m.verdict === "recommended").length;
    const row = (m) => {
      const color = m.verdict === "avoid" ? "var(--error-color, #db4437)" : "var(--warning-color, #ffa600)";
      const name = [m.vendor, m.model].filter(Boolean).join(" ");
      const facts = [T(`card.community.${m.verdict}`), T("card.community.reports", { n: m.reports }), T("card.community.you_have", { n: (m.ieees || []).length })].join(" · ");
      return `<div class="row" style="--c:${color}"><ha-icon icon="${m.verdict === "avoid" ? "mdi:thumb-down-outline" : "mdi:thumbs-up-down-outline"}"></ha-icon><div class="t"><div class="t1">${esc(name)}</div><div class="t2">${esc(facts)}</div></div></div>`;
    };
    const rows = flagged.slice(0, cfg.max_items).map(row).join("")
      + (flagged.length > cfg.max_items ? `<div class="empty">${esc(T("card.more", { n: flagged.length - cfg.max_items }))}</div>` : "");
    const goodRow = good ? `<div class="row" style="--c:var(--success-color, #43a047)"><ha-icon icon="mdi:thumb-up-outline"></ha-icon><div class="t"><div class="t1">${esc(T("card.community.recommended", { n: good }))}</div></div></div>` : "";
    return `<div class="section">${esc(T("card.community.title"))}</div>${rows}${goodRow}${rows || goodRow ? "" : `<div class="empty">${esc(T("card.community.none"))}</div>`}`;
  }

  _render() {
    const body = this._root?.getElementById("body");
    if (!body) return;
    const cfg = { ...this._config, title: this._config.title || T("card.summary.title"), panel: localPath(this._config.panel) || localPath(this._data?.panel) || "/app/local_z2m_tracker" };
    if (this._failed) { body.innerHTML = `<div class="head"><h2>${esc(cfg.title)}</h2></div><div class="err">${esc(this._error || T("card.unreachable"))}</div>`; return; }
    if (!this._data) { body.innerHTML = `<div class="head"><h2>${esc(cfg.title)}</h2></div><div class="empty">${esc(T("card.loading"))}</div>`; return; }
    const devices = this._data.devices.filter((d) => d.type !== "Coordinator" && d.status !== "removed");
    // Same rule as the network card and the Offline devices sensor: an unidentified device that drops off counts too.
    const offline = devices.filter((d) => d.availability === "offline").length;
    const all = devices.flatMap((d) => (d.issues || []).map((i) => ({ ...i, area: d.area })));
    const keys = new Set(all.map((i) => i.key));
    // Devices behind an offline router are covered by the router's issue.
    const issues = all.filter((i) => i.severity === "warning" && !(i.behind && keys.has(`offline:${i.behind}`)));
    const due = devices.filter((d) => {
      const est = d.metrics?.battery_estimate_days;
      const low = d.limits?.battery_percent ?? this._data.battery_alert_percent ?? 15;
      return d.battery_record && ((est !== null && est !== undefined && est <= 60) || d.battery_low === true || (typeof d.battery === "number" && (d.battery <= low || (d.battery <= 25 && (est === null || est === undefined)))));
    });
    const shopping = {};
    due.forEach((d) => { const r = d.battery_record || {}; const k = r.type || T("card.summary.unknown_type"); shopping[k] = (shopping[k] || 0) + (r.quantity || 1); });
    const buy = Object.entries(shopping).map(([t, n]) => `${n}× ${t}`).join(", ");
    body.innerHTML = `
      <div class="head"><h2>${esc(cfg.title)}</h2></div>
      <div class="stats">
        <div class="stat" data-go><div class="n">${devices.length}</div><div class="l">${esc(T("card.summary.active"))}</div></div>
        <div class="stat ${offline ? "bad" : ""}" data-go><div class="n">${offline}</div><div class="l">${esc(T("card.summary.offline"))}</div></div>
        <div class="stat ${issues.length ? "warn" : ""}" data-go><div class="n">${issues.length}</div><div class="l">${esc(T("card.summary.attention"))}</div></div>
        <div class="stat ${due.length ? "warn" : ""}" data-go><div class="n">${due.length}</div><div class="l">${esc(T("card.summary.batteries_due"))}</div></div>
      </div>
      <div class="section">${esc(T("card.summary.needs_attention"))}</div>
      ${issues.length ? issues.slice(0, cfg.max_items).map((i) => `<div class="row" style="--c:var(--warning-color, #ffa600)"><ha-icon icon="${KIND_ICON[i.kind] || "mdi:alert"}"></ha-icon><div class="t"><div class="t1">${esc(i.name)}</div><div class="t2">${esc(I18N()?.issueLabel(i) ?? i.label)}${i.area ? ` · ${esc(i.area)}` : ""}</div></div></div>`).join("")
        + (issues.length > cfg.max_items ? `<div class="empty">${esc(T("card.more", { n: issues.length - cfg.max_items }))}</div>` : "")
        : `<div class="empty">${esc(T("card.nothing"))}</div>`}
      ${buy ? `<div class="section">${esc(T("card.summary.to_buy"))}</div><div class="row" style="--c:var(--state-icon-color)"><ha-icon icon="mdi:battery-plus-outline"></ha-icon><div class="t"><div class="t1">${esc(buy)}</div><div class="t2">${esc(T("card.summary.to_buy_for", { n: due.length }))}</div></div></div>` : ""}
      ${cfg.show_community ? this._community(cfg) : ""}
      <div class="actions"><a href="${esc(cfg.panel)}">${esc(T("card.summary.open"))}</a></div>`;
    body.querySelectorAll("[data-go]").forEach((el) => el.addEventListener("click", () => { history.pushState(null, "", cfg.panel); window.dispatchEvent(new CustomEvent("location-changed")); }));
    body.querySelector(".actions a").addEventListener("click", (e) => { e.preventDefault(); history.pushState(null, "", cfg.panel); window.dispatchEvent(new CustomEvent("location-changed")); });
  }
}

if (!customElements.get("z2m-tracker-card")) {
  customElements.define("z2m-tracker-card", Z2MTrackerCard);
  window.customCards = window.customCards || [];
  window.customCards.push({ type: "z2m-tracker-card", name: "Zigbee Tracker summary", description: "Active, offline, issues and batteries due from the Zigbee Tracker app.", preview: false });
}
