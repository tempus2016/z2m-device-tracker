// /config/custom_components/z2m_tracker/www/z2m-tracker-cards.js
// Battery, health and network cards for the Z2M Device Tracker app.
// All three read the same z2m_tracker/devices websocket data, fetched once and shared.
(() => {
  const IEEE_RE = /^0x[0-9a-f]{16}$/i;
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const nameOf = (d) => {
    if (d.friendly_name && !IEEE_RE.test(d.friendly_name)) return d.friendly_name;
    const named = (d.names || []).filter((n) => n.name && !IEEE_RE.test(n.name));
    return named.length ? named[named.length - 1].name : d.ha_name || d.ieee;
  };
  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const netLabel = (bt) => (bt === "zigbee2mqtt" ? "Main" : String(bt).replace("zigbee2mqtt_", "").replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()));
  const KIND_ICON = { offline: "mdi:wifi-off", silent: "mdi:wifi-off", frozen: "mdi:thermometer-alert", weak_signal: "mdi:signal-cellular-1", signal_drop: "mdi:signal-cellular-2", battery_low: "mdi:battery-alert-variant-outline", battery_soon: "mdi:battery-clock-outline", update: "mdi:chip", unidentified: "mdi:help-circle-outline", replacement: "mdi:link-variant", router_hopping: "mdi:swap-horizontal" };
  const C_BAD = "var(--error-color, #db4437)";
  const C_WARN = "var(--warning-color, #ffa600)";
  const C_OK = "var(--success-color, #43a047)";

  // "Today 03:00", "Yesterday 14:10", "3 days ago"
  const when = (iso, lang) => {
    if (!iso) return null;
    const t = new Date(iso);
    if (Number.isNaN(t.getTime())) return null;
    const now = new Date();
    const day = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const days = Math.round((day(now) - day(t)) / 86400000);
    const time = t.toLocaleTimeString(lang || undefined, { hour: "2-digit", minute: "2-digit" });
    if (days <= 0) return `Today ${time}`;
    if (days === 1) return `Yesterday ${time}`;
    if (days < 30) return `${days} days ago`;
    return t.toLocaleDateString(lang || undefined, { day: "numeric", month: "short", year: "numeric" });
  };
  const ageDays = (iso) => {
    const t = iso ? new Date(iso).getTime() : NaN;
    return Number.isNaN(t) ? null : (Date.now() - t) / 86400000;
  };

  // ------------------------------------------------------------ shared data

  const STORE = { hass: null, data: null, error: null, cards: new Set(), unsub: null, timer: null, loading: null };
  const notify = () => STORE.cards.forEach((c) => c._render());
  const load = () => {
    if (!STORE.hass) return Promise.resolve();
    if (STORE.loading) return STORE.loading;
    STORE.loading = STORE.hass
      .callWS({ type: "z2m_tracker/devices" })
      .then((data) => { STORE.data = data; STORE.error = null; })
      .catch((err) => { STORE.error = err?.message || "The Zigbee Tracker app isn't reachable"; })
      .finally(() => { STORE.loading = null; notify(); });
    return STORE.loading;
  };
  const attach = (card, hass) => {
    const first = !STORE.hass;
    STORE.hass = hass;
    STORE.cards.add(card);
    if (first || (!STORE.data && !STORE.loading)) load();
    if (!STORE.timer) STORE.timer = setInterval(load, 60000);
    if (!STORE.unsub && hass.connection) {
      STORE.unsub = hass.connection.subscribeMessage(() => load(), { type: "z2m_tracker/subscribe" }).catch(() => null);
    }
  };
  const detach = (card) => {
    STORE.cards.delete(card);
    if (STORE.cards.size) return;
    clearInterval(STORE.timer); STORE.timer = null;
    if (STORE.unsub) { STORE.unsub.then((fn) => fn && fn()).catch(() => {}); STORE.unsub = null; }
  };

  const go = (path) => { history.pushState(null, "", path); window.dispatchEvent(new CustomEvent("location-changed")); };

  // ------------------------------------------------------------- base class

  const BASE_CSS = `
    :host { display: block; }
    ha-card { overflow: hidden; height: 100%; }
    .head { display: flex; align-items: center; gap: 8px; padding: 16px 16px 8px; }
    .head h2 { flex: 1; min-width: 0; margin: 0; font-size: var(--ha-card-header-font-size, 24px); font-weight: 400; color: var(--ha-card-header-color, var(--primary-text-color)); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .pill { flex: none; font-size: 12px; font-weight: 500; padding: 2px 10px; border-radius: 12px; color: var(--c); background: color-mix(in srgb, var(--c) 14%, transparent); }
    .dot { flex: none; display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--secondary-text-color); }
    .dot::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--c); }
    .row { display: flex; align-items: center; gap: 12px; padding: 6px 16px; min-height: 44px; cursor: pointer; }
    .row:hover { background: rgba(var(--rgb-primary-text-color, 0, 0, 0), 0.04); }
    .row:focus-visible { outline: 2px solid var(--primary-color); outline-offset: -2px; }
    .row ha-icon { --mdc-icon-size: 22px; color: var(--c, var(--state-icon-color)); flex: none; }
    .t { flex: 1; min-width: 0; }
    .t1 { color: var(--primary-text-color); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .t2 { font-size: 12px; color: var(--secondary-text-color); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .empty { padding: 8px 16px 16px; color: var(--secondary-text-color); font-size: 14px; }
    .err { padding: 0 16px 16px; color: var(--error-color); }
    .more { display: block; padding: 4px 16px 12px; color: var(--primary-color); font-size: 14px; font-weight: 500; text-decoration: none; cursor: pointer; }
    .more:hover { text-decoration: underline; }
    .pad { height: 8px; }
  `;

  class Z2MBaseCard extends HTMLElement {
    setConfig(config) {
      this._config = { ...this.constructor.defaults, ...config };
      this._render();
    }

    set hass(hass) {
      this._hass = hass;
      if (this.isConnected) attach(this, hass);
    }

    connectedCallback() { if (this._hass) attach(this, this._hass); this._render(); }
    disconnectedCallback() { detach(this); }

    getGridOptions() { return { columns: 6, min_columns: 4, rows: "auto" }; }

    get _panel() { return this._config?.panel || STORE.data?.panel || "/app/local_z2m_tracker"; }

    _devices() {
      return (STORE.data?.devices || []).filter((d) => d.type !== "Coordinator" && d.status !== "removed");
    }

    _shell(title, extraCss) {
      if (!this._root) {
        this._root = this.attachShadow({ mode: "open" });
        this._root.innerHTML = `<style>${BASE_CSS}${extraCss || ""}</style><ha-card><div id="body"></div></ha-card>`;
        this._root.addEventListener("click", (e) => this._click(e));
        this._root.addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && e.target.closest?.("[data-ieee],[data-panel]")) { e.preventDefault(); this._click(e); } });
      }
      const body = this._root.getElementById("body");
      if (STORE.error && !STORE.data) { body.innerHTML = `<div class="head"><h2>${esc(title)}</h2></div><div class="err">${esc(STORE.error)}</div>`; return null; }
      if (!STORE.data) { body.innerHTML = `<div class="head"><h2>${esc(title)}</h2></div><div class="empty">Loading…</div>`; return null; }
      return body;
    }

    _click(e) {
      const el = e.target.closest?.("[data-ieee],[data-panel]");
      if (!el) return;
      e.preventDefault();
      go(el.dataset.ieee ? `${this._panel}/${encodeURIComponent(el.dataset.ieee)}` : this._panel);
    }

    _row(ieee, icon, color, t1, t2, tail = "") {
      return `<div class="row" role="link" tabindex="0" data-ieee="${esc(ieee)}" style="--c:${color}"><ha-icon icon="${esc(icon)}"></ha-icon><div class="t"><div class="t1">${esc(t1)}</div>${t2 ? `<div class="t2">${esc(t2)}</div>` : ""}</div>${tail}</div>`;
    }

    _more(n) {
      return n > 0 ? `<a class="more" role="link" tabindex="0" data-panel href="${esc(this._panel)}">and ${n} more</a>` : `<div class="pad"></div>`;
    }
  }

  // --------------------------------------------------------------- editor

  const LABELS = {
    title: "Title",
    max_items: "Rows to show",
    show_all: "Show every battery device",
    show_info: "Include \"worth a look\" items",
    show_weak_spots: "List single-point-of-failure routers",
  };
  const HELPERS = {
    show_all: "Ignores the row limit.",
    show_info: "Off shows only devices that need attention.",
  };

  class Z2MCardEditor extends HTMLElement {
    set hass(hass) { this._hass = hass; if (this._form) this._form.hass = hass; }
    setConfig(config) { this._config = config; this._render(); }
    _render() {
      if (!this._form) {
        this._form = document.createElement("ha-form");
        this._form.computeLabel = (s) => LABELS[s.name] || s.name;
        this._form.computeHelper = (s) => HELPERS[s.name];
        this._form.addEventListener("value-changed", (e) => {
          this._config = e.detail.value;
          this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: this._config }, bubbles: true, composed: true }));
        });
        this.appendChild(this._form);
      }
      this._form.hass = this._hass;
      this._form.data = { ...this.constructor.defaults, ...this._config };
      this._form.schema = this.constructor.schema;
    }
  }
  const TITLE = { name: "title", selector: { text: {} } };
  const MAX = { name: "max_items", selector: { number: { min: 1, max: 50, mode: "box" } } };

  // -------------------------------------------------------------- battery

  class BatteryEditor extends Z2MCardEditor {}
  BatteryEditor.schema = [TITLE, MAX, { name: "show_all", selector: { boolean: {} } }];

  class Z2MTrackerBatteryCard extends Z2MBaseCard {
    static getConfigElement() { return document.createElement("z2m-tracker-battery-card-editor"); }
    static getStubConfig() { return { title: "Batteries", max_items: 8 }; }
    getCardSize() { return 1 + Math.min(this._shown || this._config?.max_items || 8, 12); }

    _render() {
      const cfg = this._config || Z2MTrackerBatteryCard.defaults;
      const body = this._shell(cfg.title, `
        .bar { flex: none; width: 72px; height: 6px; border-radius: 3px; background: var(--divider-color, rgba(0,0,0,.12)); overflow: hidden; }
        .bar i { display: block; height: 100%; border-radius: 3px; background: var(--c); }
        .pct { flex: none; width: 40px; text-align: right; font-size: 14px; color: var(--primary-text-color); font-variant-numeric: tabular-nums; }
      `);
      if (!body) return;
      const low = Number(STORE.data.battery_alert_percent ?? 15);
      const devs = this._devices()
        .filter((d) => d.battery_record || isNum(d.battery) || d.battery_low === true)
        .map((d) => ({ d, pct: isNum(d.battery) ? d.battery : null }))
        .sort((a, b) => (a.pct ?? (a.d.battery_low === true ? -1 : 101)) - (b.pct ?? (b.d.battery_low === true ? -1 : 101)) || nameOf(a.d).localeCompare(nameOf(b.d)));
      const isLow = (x) => x.d.battery_low === true || (x.pct !== null && x.pct <= low);
      const lowCount = devs.filter(isLow).length;
      const limit = cfg.show_all ? devs.length : Math.max(1, Number(cfg.max_items) || 8);
      const shown = devs.slice(0, limit);
      this._shown = shown.length;
      const rows = shown.map((x) => {
        const { d, pct } = x;
        const color = isLow(x) ? C_BAD : pct !== null && pct <= Math.max(low * 2, 30) ? C_WARN : C_OK;
        const est = d.metrics?.battery_estimate_days;
        const t2 = [d.battery_record?.label, isNum(est) ? `about ${plural(Math.max(0, Math.round(est)), "day")} left` : null, d.area].filter(Boolean).join(" · ");
        const icon = isLow(x) ? "mdi:battery-alert-variant-outline" : pct === null ? "mdi:battery-unknown" : `mdi:battery${pct >= 95 ? "" : `-${Math.max(10, Math.round(pct / 10) * 10)}`}`;
        const tail = `<div class="bar" aria-hidden="true"><i style="width:${pct === null ? (d.battery_low === true ? 4 : 0) : Math.max(2, Math.min(100, pct))}%"></i></div><div class="pct">${pct === null ? (d.battery_low === true ? "Low" : "–") : `${Math.round(pct)}%`}</div>`;
        return this._row(d.ieee, icon, color, nameOf(d), t2 || (pct === null && d.battery_low !== true ? "No reading yet" : ""), tail);
      }).join("");
      body.innerHTML = `
        <div class="head"><h2>${esc(cfg.title)}</h2>${lowCount ? `<span class="pill" style="--c:${C_WARN}">${lowCount} low</span>` : ""}</div>
        ${devs.length ? rows + this._more(devs.length - shown.length) : `<div class="empty">No battery devices found.</div>`}`;
    }
  }
  Z2MTrackerBatteryCard.defaults = BatteryEditor.defaults = { title: "Batteries", max_items: 8, show_all: false };

  // --------------------------------------------------------------- health

  class HealthEditor extends Z2MCardEditor {}
  HealthEditor.schema = [TITLE, MAX, { name: "show_info", selector: { boolean: {} } }];

  class Z2MTrackerHealthCard extends Z2MBaseCard {
    static getConfigElement() { return document.createElement("z2m-tracker-health-card-editor"); }
    static getStubConfig() { return { title: "Zigbee health", max_items: 5 }; }
    getCardSize() { return 3 + Math.min(this._shown ?? 3, 10); }

    _render() {
      const cfg = this._config || Z2MTrackerHealthCard.defaults;
      const body = this._shell(cfg.title, `
        .big { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px; padding: 0 12px 8px; }
        .big div { text-align: center; padding: 8px 4px; border-radius: 12px; cursor: pointer; }
        .big div:hover { background: rgba(var(--rgb-primary-text-color, 0, 0, 0), 0.04); }
        .big b { display: block; font-size: 28px; font-weight: 400; line-height: 1.2; color: var(--c, var(--primary-text-color)); }
        .big small { font-size: 12px; color: var(--secondary-text-color); }
      `);
      if (!body) return;
      const devs = this._devices();
      const warn = (d) => (d.issues || []).some((i) => i.severity === "warning");
      const attention = devs.filter(warn);
      const look = devs.filter((d) => !warn(d) && (d.issues || []).length);
      const fine = devs.length - attention.length - look.length;
      const byIeee = new Map(devs.map((d) => [d.ieee, d]));
      const issues = devs
        .flatMap((d) => (d.issues || []).map((i) => ({ ...i, device: d })))
        .filter((i) => cfg.show_info !== false || i.severity === "warning")
        .sort((a, b) => (a.severity === "warning" ? 0 : 1) - (b.severity === "warning" ? 0 : 1) || nameOf(a.device).localeCompare(nameOf(b.device)));
      const limit = Math.max(1, Number(cfg.max_items) || 5);
      const shown = issues.slice(0, limit);
      this._shown = shown.length;
      const rows = shown.map((i) => {
        const d = byIeee.get(i.ieee) || i.device;
        const detail = [i.title || i.label, i.detail].filter(Boolean).join(". ").replace(/\.\./g, ".");
        return this._row(d.ieee, KIND_ICON[i.kind] || "mdi:alert-circle-outline", i.severity === "warning" ? C_BAD : C_WARN, nameOf(d), detail);
      }).join("");
      body.innerHTML = `
        <div class="head"><h2>${esc(cfg.title)}</h2></div>
        <div class="big">
          <div data-panel role="link" tabindex="0" style="${attention.length ? `--c:${C_BAD}` : ""}"><b>${attention.length}</b><small>Need attention</small></div>
          <div data-panel role="link" tabindex="0" style="${look.length ? `--c:${C_WARN}` : ""}"><b>${look.length}</b><small>Worth a look</small></div>
          <div data-panel role="link" tabindex="0"><b>${fine}</b><small>Fine</small></div>
        </div>
        ${issues.length ? rows + this._more(issues.length - shown.length) : `<div class="empty">Nothing needs attention.</div>`}`;
    }
  }
  Z2MTrackerHealthCard.defaults = HealthEditor.defaults = { title: "Zigbee health", max_items: 5, show_info: true };

  // -------------------------------------------------------------- network

  class NetworkEditor extends Z2MCardEditor {}
  NetworkEditor.schema = [TITLE, { name: "show_weak_spots", selector: { boolean: {} } }];

  class Z2MTrackerNetworkCard extends Z2MBaseCard {
    static getConfigElement() { return document.createElement("z2m-tracker-network-card-editor"); }
    static getStubConfig() { return { title: "Zigbee network" }; }
    getCardSize() { return 4 + 3 * Math.max(1, (STORE.data?.networks || []).length); }

    _render() {
      const cfg = this._config || Z2MTrackerNetworkCard.defaults;
      const body = this._shell(cfg.title, `
        .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 4px; padding: 0 12px 8px; }
        .grid > div { display: flex; flex-direction: column; align-items: center; gap: 2px; padding: 8px 4px; border-radius: 12px; cursor: pointer; text-align: center; }
        .grid > div:hover { background: rgba(var(--rgb-primary-text-color, 0, 0, 0), 0.04); }
        .grid ha-icon { --mdc-icon-size: 22px; color: var(--c, var(--state-icon-color)); }
        .grid b { font-size: 22px; font-weight: 400; line-height: 1.2; color: var(--primary-text-color); }
        .grid small { font-size: 12px; color: var(--secondary-text-color); }
        .sub { padding: 8px 16px 2px; font-size: 14px; font-weight: 500; color: var(--secondary-text-color); }
        .kv { padding: 0 16px 8px; }
        .kv div { display: flex; justify-content: space-between; gap: 12px; padding: 6px 0; font-size: 14px; border-top: 1px solid var(--divider-color); }
        .kv div:first-child { border-top: 0; }
        .kv span { color: var(--secondary-text-color); }
        .kv b { font-weight: 400; color: var(--c, var(--primary-text-color)); text-align: right; }
        .row { min-height: 40px; }
      `);
      if (!body) return;
      const lang = this._hass?.locale?.language;
      const devs = this._devices();
      const routers = devs.filter((d) => d.type === "Router").length;
      const offline = devs.filter((d) => d.availability === "offline").length;
      const nets = STORE.data.networks || [];
      const weak = nets.flatMap((n) => (n.single_points || []).map((p) => ({ ...p, topic: n.topic })));
      const states = nets.map((n) => n.state).filter(Boolean);
      const status = !states.length ? "" : states.every((s) => s === "online") ? `<span class="dot" style="--c:${C_OK}">Online</span>` : states.every((s) => s === "offline") ? `<span class="dot" style="--c:${C_BAD}">Offline</span>` : `<span class="dot" style="--c:${C_WARN}">${states.filter((s) => s === "offline").length} offline</span>`;
      const stat = (icon, n, label, color) => `<div data-panel role="link" tabindex="0" style="${n && color ? `--c:${color}` : ""}"><ha-icon icon="${icon}"></ha-icon><b>${n}</b><small>${label}</small></div>`;

      const channel = (n) => {
        if (!n.channel) return ["Not known yet", ""];
        if (n.wifi === "overlap") return [`${n.channel} · overlaps Wi-Fi`, C_BAD];
        if (n.wifi === "near") return [`${n.channel} · close to Wi-Fi`, C_WARN];
        if (n.wifi === "clear") return [`${n.channel} · clear of Wi-Fi`, ""];
        return [`${n.channel} · Wi-Fi not set`, ""];
      };
      const backup = (n) => {
        const b = n.backup;
        if (!b) return ["Folder not found", ""];
        if (!b.exists) return ["Missing", C_BAD];
        if (b.error) return ["Unreadable", C_BAD];
        if (!b.has_key) return ["No network key", C_BAD];
        const age = ageDays(b.date);
        return [when(b.date, lang) || "Unknown date", age !== null && age > 2 ? C_WARN : ""];
      };
      const scan = (n) => (n.scanning ? ["Scanning now", ""] : n.scanned_at ? [when(n.scanned_at, lang), ageDays(n.scanned_at) > 7 ? C_WARN : ""] : ["Never", C_WARN]);
      const kv = (label, [value, color]) => `<div><span>${label}</span><b style="${color ? `--c:${color}` : ""}">${esc(value)}</b></div>`;
      const sections = nets.map((n) => `
        ${nets.length > 1 ? `<div class="sub">${esc(netLabel(n.topic))} network${n.state === "offline" ? " · offline" : ""}</div>` : ""}
        <div class="kv">${kv("Channel", channel(n))}${kv("Coordinator backup", backup(n))}${kv("Last scan", scan(n))}</div>`).join("");
      const weakRows = cfg.show_weak_spots !== false && weak.length
        ? `<div class="sub">Single points of failure</div>` + weak.map((p) => this._row(p.ieee, "mdi:alert", C_WARN, p.name || p.ieee,
            `Only route for ${[p.routers ? plural(p.routers, "router") : null, p.devices ? plural(p.devices, "device") : null].filter(Boolean).join(" and ") || "part of the mesh"}${nets.length > 1 ? ` · ${netLabel(p.topic)}` : ""}`)).join("") + `<div class="pad"></div>`
        : "";

      body.innerHTML = `
        <div class="head"><h2>${esc(cfg.title)}</h2>${status}</div>
        <div class="grid">
          ${stat("mdi:zigbee", devs.length, "Devices")}
          ${stat("mdi:router-wireless", routers, "Routers")}
          ${stat("mdi:lan-disconnect", offline, "Offline", C_BAD)}
          ${stat("mdi:alert", weak.length, weak.length === 1 ? "Weak spot" : "Weak spots", C_WARN)}
        </div>
        ${sections || `<div class="empty">No Zigbee2MQTT network seen yet.</div>`}
        ${weakRows}`;
    }
  }
  Z2MTrackerNetworkCard.defaults = NetworkEditor.defaults = { title: "Zigbee network", show_weak_spots: true };

  // ------------------------------------------------------------- register

  const CARDS = [
    ["z2m-tracker-battery-card", Z2MTrackerBatteryCard, BatteryEditor, "Zigbee Tracker batteries", "Battery devices, lowest first, with the app's low-battery threshold."],
    ["z2m-tracker-health-card", Z2MTrackerHealthCard, HealthEditor, "Zigbee Tracker health", "How many devices need attention, are worth a look or are fine, and the top issues."],
    ["z2m-tracker-network-card", Z2MTrackerNetworkCard, NetworkEditor, "Zigbee Tracker network", "Devices, routers, offline, weak spots, channel, coordinator backup and last scan."],
  ];
  window.customCards = window.customCards || [];
  for (const [type, Card, Editor, name, description] of CARDS) {
    if (customElements.get(type)) continue;
    customElements.define(type, Card);
    customElements.define(`${type}-editor`, Editor);
    window.customCards.push({ type, name, description, preview: true });
  }
})();
