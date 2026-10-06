/* Dashboard widgets, self-contained SVG (no libraries, no map tiles, works offline):
   worldMap(host, world, opts)   Equal Earth world map: land, IMF shipping lanes, chokepoint bubbles, Gulf ports,
                                 zoom/pan, hover tooltip, click to select. Redraw with .update(dayIndex, type).
   stackedBars(host, cfg)        daily stacked columns with totals and per-bar tooltip
   donut(host, cfg)              share of total with legend
   sparkline(values, w, h)       returns an SVG string
   Colours come from CSS custom properties so light and dark themes both work. */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs, parent) => {
    const e = document.createElementNS(NS, tag); let style = "";
    for (const k in attrs) { const v = attrs[k];
      if ((k === "fill" || k === "stroke") && String(v).includes("var(")) style += `${k}:${v};`; else e.setAttribute(k, v); }
    if (style) e.setAttribute("style", style);
    if (parent) parent.appendChild(e); return e;
  };
  const fmt = (v, d = 0) => v == null || !isFinite(v) ? "–" : Number(v).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
  const DAY = 864e5;
  window.dayToISO = (start, i) => new Date(Date.parse(start + "T00:00:00Z") + i * DAY).toISOString().slice(0, 10);
  window.isoToDay = (start, iso) => Math.round((Date.parse(iso + "T00:00:00Z") - Date.parse(start + "T00:00:00Z")) / DAY);

  // ---- shared aggregation -------------------------------------------------------------------------
  // mean over the 7 days ending at i (needs >= 4 reported days); type "all" sums the five vessel types
  function dayValue(cp, type, i) {
    const keys = type === "all" ? Object.keys(cp.s) : [type];
    let tot = 0;
    for (const k of keys) { const a = cp.s[k]; if (!a) return null; const v = a[i]; if (v == null) return null; tot += v; }
    return tot;
  }
  function mean7(cp, type, i) {
    let s = 0, n = 0;
    for (let j = Math.max(0, i - 6); j <= i; j++) { const v = dayValue(cp, type, j); if (v != null) { s += v; n++; } }
    return n >= 4 ? s / n : null;
  }
  function baseOf(cp, type) {
    if (type === "all") { let s = 0; for (const k in cp.base) { if (cp.base[k] == null) return null; s += cp.base[k]; } return s; }
    return cp.base[type];
  }
  function lastIndex(cp) {
    for (let i = cp.s.tanker.length - 1; i >= 0; i--) if (cp.s.tanker[i] != null) return i;
    return -1;
  }
  window.SHIP = { dayValue, mean7, baseOf, lastIndex };

  // diverging change classes (polarity): red = fewer transits than baseline, blue = more, grey = near normal
  const CHANGE = [
    { max: -0.5, cls: "dv-n2", label: "−50% or more" }, { max: -0.15, cls: "dv-n1", label: "−15 to −50%" },
    { max: 0.15, cls: "dv-0", label: "within ±15%" }, { max: 0.5, cls: "dv-p1", label: "+15 to +50%" }, { max: Infinity, cls: "dv-p2", label: "+50% or more" }];
  const changeClass = pct => pct == null ? "dv-na" : CHANGE.find(c => pct <= c.max).cls;
  window.SHIP.CHANGE = CHANGE;

  // ---- Equal Earth projection ---------------------------------------------------------------------
  const A1 = 1.340264, A2 = -0.081106, A3 = 0.000893, A4 = 0.003796, M = Math.sqrt(3) / 2, S = 170;
  function proj(lon, lat) {
    const l = lon * Math.PI / 180, th = Math.asin(M * Math.sin(lat * Math.PI / 180)), t2 = th * th, t6 = t2 * t2 * t2;
    return [S * l * Math.cos(th) / (M * (A1 + 3 * A2 * t2 + t6 * (7 * A3 + 9 * A4 * t2))), -S * th * (A1 + A2 * t2 + t6 * (A3 + A4 * t2))];
  }
  function pathOf(lines, closed) {
    let d = "";
    for (const pts of lines) {
      let pen = false, prev = null;
      for (const [lon, lat] of pts) {
        if (prev != null && Math.abs(lon - prev) > 180) pen = false;  // never draw across the antimeridian
        const [x, y] = proj(lon, Math.max(-85, Math.min(85, lat)));
        d += (pen ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1); pen = true; prev = lon;
      }
      if (closed) d += "Z";
    }
    return d;
  }

  window.worldMap = function (host, W, opts) {
    host.querySelectorAll(".worldsvg,.maptip").forEach(n => n.remove());  // keep the zoom controls in the host
    const [x0] = proj(-180, 0), [x1] = proj(180, 0), [, yTop] = proj(0, 80), [, yBot] = proj(0, -58);
    const home = { x: x0, y: yTop, w: x1 - x0, h: yBot - yTop };
    let vb = { ...home };
    const svg = el("svg", { viewBox: `${vb.x} ${vb.y} ${vb.w} ${vb.h}`, class: "worldsvg", role: "img",
      "aria-label": "World map of shipping chokepoints sized by daily vessel transits" });
    host.insertBefore(svg, host.firstChild);
    el("rect", { x: x0 - 50, y: yTop - 50, width: home.w + 100, height: home.h + 100, fill: "var(--water)" }, svg);
    const grat = [];
    for (let lon = -180; lon <= 180; lon += 30) { const l = []; for (let lat = -58; lat <= 80; lat += 2) l.push([lon, lat]); grat.push(l); }
    for (let lat = -30; lat <= 60; lat += 30) { const l = []; for (let lon = -180; lon <= 180; lon += 2) l.push([lon, lat]); grat.push(l); }
    el("path", { d: pathOf(grat, false), fill: "none", stroke: "var(--grat)", "stroke-width": 0.6, "vector-effect": "non-scaling-stroke" }, svg);
    // rings entirely south of the map edge (Antarctica) are dropped: its outline spans the antimeridian and would invert the fill
    const land = (W.land || []).filter(r => r.some(p => p[1] > -58));
    if (land.length) el("path", { d: pathOf(land, true), fill: "var(--land)", stroke: "var(--coast)", "stroke-width": 0.5, "vector-effect": "non-scaling-stroke", "fill-rule": "nonzero" }, svg);
    if (W.routes && W.routes.length) el("path", { d: pathOf(W.routes, false), fill: "none", stroke: "var(--lane)", "stroke-width": 1, "vector-effect": "non-scaling-stroke", "stroke-linejoin": "round", class: "lanes" }, svg);
    const gPorts = el("g", {}, svg), gCps = el("g", {}, svg), gLab = el("g", { class: "maplabels" }, svg);
    const tip = document.createElement("div"); tip.className = "svgtip maptip"; tip.hidden = true; host.appendChild(tip);

    const cps = W.cps.map(cp => { const [x, y] = proj(cp.lon, cp.lat); return { cp, x, y,
      c: el("circle", { cx: x, cy: y, r: 3, class: "bub", tabindex: 0, role: "button", "aria-label": cp.name }, gCps) }; });
    const ports = W.ports.map(p => { const [x, y] = proj(p.lon, p.lat); return { p, x, y,
      c: el("rect", { x: x - 2, y: y - 2, width: 4, height: 4, class: "port " + (p.group === "bypass" ? "port-bypass" : "port-gulf"), transform: `rotate(45 ${x} ${y})` }, gPorts) }; });
    let state = { i: W.n - 1, type: "all", sel: opts.selected };
    const k = () => vb.w / home.w;  // keep symbols a constant screen size while zoomed

    function label(x, y, text, strong) {
      const t = el("text", { x: x + 6 * k() + 2 * k(), y: y + 3.5 * k(), "font-size": (strong ? 11 : 10) * k(), class: strong ? "maplab strong" : "maplab" }, gLab);
      t.textContent = text;
    }
    function draw() {
      gLab.innerHTML = "";
      const s = k();
      cps.forEach(o => {
        const v = mean7(o.cp, state.type, state.i), b = baseOf(o.cp, state.type);
        const pct = v != null && b ? v / b - 1 : null;
        o.v = v; o.pct = pct;
        o.c.setAttribute("r", (v == null ? 2.5 : Math.max(2.5, Math.sqrt(v) * 1.55)) * s);
        o.c.setAttribute("class", "bub " + changeClass(pct) + (o.cp.slug === state.sel ? " sel" : ""));
        o.c.setAttribute("stroke-width", (o.cp.slug === state.sel ? 2.5 : 1.2) * s);
        if (o.cp.screen || o.cp.slug === state.sel) label(o.x + Math.sqrt(v || 1) * 1.55 * s - 6 * s, o.y, o.cp.name.replace(/ (Strait|Canal)$/, "").replace(/^Strait of /, ""), o.cp.slug === state.sel);
      });
      const showPorts = state.type === "all" || state.type === "tanker";
      ports.forEach(o => {
        const a = o.p.export; let sum = 0, n = 0;
        for (let j = Math.max(0, state.i - 6); j <= state.i; j++) if (a[j] != null) { sum += a[j]; n++; }
        o.v = n >= 4 ? sum / n : null;
        const r = (o.v == null ? 2 : Math.max(2, Math.sqrt(o.v) * 0.5)) * s;
        o.c.setAttribute("x", o.x - r); o.c.setAttribute("y", o.y - r); o.c.setAttribute("width", 2 * r); o.c.setAttribute("height", 2 * r);
        o.c.setAttribute("stroke-width", 1 * s);
        o.c.style.display = showPorts ? "" : "none";
      });
    }
    function showTip(evt, html) {
      tip.innerHTML = html; tip.hidden = false;
      const r = host.getBoundingClientRect();
      let x = evt.clientX - r.left + 14, y = evt.clientY - r.top + 14;
      if (x + tip.offsetWidth > r.width - 4) x = evt.clientX - r.left - tip.offsetWidth - 14;
      if (y + tip.offsetHeight > r.height - 4) y = evt.clientY - r.top - tip.offsetHeight - 14;
      tip.style.left = Math.max(4, x) + "px"; tip.style.top = Math.max(4, y) + "px";
    }
    const typeName = t => ({ all: "all vessels", tanker: "tankers", container: "container ships", dry_bulk: "dry bulk carriers", general_cargo: "general cargo", roro: "ro-ro" }[t]);
    cps.forEach(o => {
      const html = () => `<div class="d">${dayToISO(W.start, state.i)} · 7-day mean</div><b>${o.cp.name}</b><div>${fmt(o.v, 1)} ${typeName(state.type)} per day</div>` +
        `<div>${o.pct == null ? "no " + W.base_label : (o.pct >= 0 ? "+" : "") + fmt(o.pct * 100, 0) + "% vs " + W.base_label}</div><div class="d">Click for details</div>`;
      o.c.addEventListener("pointermove", e => showTip(e, html()));
      o.c.addEventListener("pointerleave", () => { tip.hidden = true; });
      const pick = () => { state.sel = o.cp.slug; draw(); opts.onSelect && opts.onSelect(o.cp.slug); };
      o.c.addEventListener("click", pick);
      o.c.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
    });
    ports.forEach(o => {
      o.c.addEventListener("pointermove", e => showTip(e, `<div class="d">${dayToISO(W.start, state.i)} · 7-day mean</div><b>${o.p.name}</b>` +
        `<div>${o.p.group === "bypass" ? "Bypass port (outside the strait)" : "Inside the Gulf"}</div><div>Tanker exports ≈ ${fmt(o.v, 0)} thousand t/day</div>` +
        `<div>${W.base_label}: ${fmt(o.p.base, 0)} thousand t/day</div><div class="d">PortWatch estimate from AIS, not customs data</div>`));
      o.c.addEventListener("pointerleave", () => { tip.hidden = true; });
    });

    // zoom & pan (wheel, drag, pinch-free buttons)
    function setVB(n) {
      n.w = Math.min(home.w, Math.max(home.w / 12, n.w)); n.h = n.w * home.h / home.w;
      n.x = Math.min(home.x + home.w - n.w, Math.max(home.x, n.x)); n.y = Math.min(home.y + home.h - n.h, Math.max(home.y, n.y));
      vb = n; svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`); draw();
    }
    function zoomAt(f, cx, cy) { const w = vb.w * f, h = vb.h * f; setVB({ x: cx - (cx - vb.x) * f, y: cy - (cy - vb.y) * f, w, h }); }
    function toWorld(e) { const r = svg.getBoundingClientRect(); return [vb.x + (e.clientX - r.left) / r.width * vb.w, vb.y + (e.clientY - r.top) / r.height * vb.h]; }
    svg.addEventListener("wheel", e => { if (!e.ctrlKey && !e.metaKey && !opts.wheel) return; e.preventDefault(); const [cx, cy] = toWorld(e); zoomAt(e.deltaY > 0 ? 1.2 : 1 / 1.2, cx, cy); }, { passive: false });
    let drag = null;
    svg.addEventListener("pointerdown", e => { if (e.target.closest(".bub")) return; drag = { x: e.clientX, y: e.clientY, vb: { ...vb } }; svg.setPointerCapture(e.pointerId); svg.classList.add("dragging"); });
    svg.addEventListener("pointermove", e => { if (!drag) return; const r = svg.getBoundingClientRect();
      setVB({ ...drag.vb, x: drag.vb.x - (e.clientX - drag.x) / r.width * vb.w, y: drag.vb.y - (e.clientY - drag.y) / r.height * vb.h }); });
    const end = () => { drag = null; svg.classList.remove("dragging"); };
    svg.addEventListener("pointerup", end); svg.addEventListener("pointercancel", end);
    draw();
    return {
      update(i, type) { state.i = i; state.type = type; draw(); },
      select(slug) { state.sel = slug; draw(); },
      zoom(f) { zoomAt(f, vb.x + vb.w / 2, vb.y + vb.h / 2); },
      reset() { setVB({ ...home }); },
      focus(lon, lat, f) { const [x, y] = proj(lon, lat); const w = home.w / f; setVB({ x: x - w / 2, y: y - w * home.h / home.w / 2, w, h: w * home.h / home.w }); },
    };
  };

  // ---- stacked daily columns ----------------------------------------------------------------------
  window.stackedBars = function (host, cfg) {
    host.innerHTML = ""; host.style.position = "relative";
    const Wd = Math.max(280, host.clientWidth || 520), H = cfg.height || 300;
    const n = cfg.x.length, legendH = 18 * Math.ceil(cfg.series.length / Math.max(1, Math.floor((Wd - 60) / 120)));
    const m = { l: 40, r: 10, t: 22, b: 30 + legendH };
    const pw = Wd - m.l - m.r, ph = H - m.t - m.b;
    const totals = cfg.x.map((_, i) => { let s = 0, ok = false; cfg.series.forEach(se => { const v = se.y[i]; if (v != null) { s += v; ok = true; } }); return ok ? s : null; });
    const ymax0 = Math.max(1, ...totals.filter(v => v != null));
    const step0 = ymax0 / 4, mag = Math.pow(10, Math.floor(Math.log10(step0))), step = [1, 2, 2.5, 5, 10].map(z => z * mag).find(z => z >= step0);
    const ymax = Math.ceil(ymax0 / step) * step;
    const Y = v => m.t + ph - v / ymax * ph, bw = pw / n, gap = Math.max(2, bw * 0.28);
    const svg = el("svg", { viewBox: `0 0 ${Wd} ${H}`, width: Wd, height: H, role: "img", "aria-label": cfg.title || "stacked columns", style: "display:block;max-width:100%;height:auto;font-family:var(--body)" }, host);
    for (let v = 0; v <= ymax + 1e-9; v += step) { el("line", { x1: m.l, x2: m.l + pw, y1: Y(v), y2: Y(v), stroke: "var(--rule)" }, svg);
      const t = el("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end", "font-size": 11, fill: "var(--ink2)" }, svg); t.textContent = fmt(v); }
    const every = Math.ceil(n / Math.max(2, Math.floor(pw / 56)));
    cfg.x.forEach((d, i) => {
      const x = m.l + i * bw + gap / 2, w = bw - gap; let acc = 0;
      cfg.series.forEach((se, k) => { const v = se.y[i]; if (!v) return;
        const y0 = Y(acc), y1 = Y(acc + v); acc += v;
        const top = k === cfg.series.length - 1 || !cfg.series.slice(k + 1).some(z => z.y[i]);
        el("rect", { x, y: y1 + (top ? 0 : 1), width: w, height: Math.max(0.5, y0 - y1 - (top ? 0 : 1)), fill: se.color, rx: top ? Math.min(3, w / 3) : 0 }, svg); });
      if (totals[i] != null && (cfg.totals !== false) && w >= 14) { const t = el("text", { x: x + w / 2, y: Y(totals[i]) - 5, "text-anchor": "middle", "font-size": 10.5, "font-weight": 600, fill: "var(--ink)" }, svg); t.textContent = fmt(totals[i]); }
      if (i % every === 0) { const t = el("text", { x: x + w / 2, y: m.t + ph + 16, "text-anchor": "middle", "font-size": 11, fill: "var(--ink2)" }, svg);
        const dt = new Date(d + "T00:00:00Z"); t.textContent = dt.getUTCDate() + " " + ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][dt.getUTCMonth()]; }
    });
    el("line", { x1: m.l, x2: m.l + pw, y1: m.t + ph, y2: m.t + ph, stroke: "var(--ink2)" }, svg);
    let lx = m.l, ly = m.t + ph + 38;
    cfg.series.forEach(se => { const w = 22 + se.name.length * 6.4; if (lx + w > Wd - m.r) { lx = m.l; ly += 18; }
      el("rect", { x: lx, y: ly - 9, width: 10, height: 10, rx: 2, fill: se.color }, svg);
      const t = el("text", { x: lx + 14, y: ly, "font-size": 11.5, fill: "var(--ink)" }, svg); t.textContent = se.name; lx += w; });
    const tip = document.createElement("div"); tip.className = "svgtip"; tip.hidden = true; host.appendChild(tip);
    cfg.x.forEach((d, i) => {
      const hit = el("rect", { x: m.l + i * bw, y: m.t, width: bw, height: ph, fill: "transparent" }, svg);
      const show = () => { const r = svg.getBoundingClientRect(), sc = r.width / Wd;
        tip.innerHTML = `<div class="d">${d}</div>` + cfg.series.slice().reverse().map(se => `<div><span style="color:${se.color}">■</span> ${se.name}: <b>${fmt(se.y[i])}</b></div>`).join("") + `<div>Total: <b>${fmt(totals[i])}</b></div>`;
        tip.hidden = false; const px = (m.l + (i + 1) * bw) * sc + 6; tip.style.left = Math.min(px, r.width - tip.offsetWidth - 4) + "px"; tip.style.top = "4px"; };
      hit.addEventListener("pointermove", show); hit.addEventListener("pointerdown", show); hit.addEventListener("pointerleave", () => { tip.hidden = true; });
    });
  };

  // ---- donut --------------------------------------------------------------------------------------
  window.donut = function (host, cfg) {
    host.innerHTML = ""; host.style.position = "relative";
    const items = cfg.items.filter(it => it.value > 0), total = items.reduce((s, it) => s + it.value, 0);
    if (!total) { host.innerHTML = '<p class="nodata">No vessel transits reported for this period.</p>'; return; }
    const wrap = document.createElement("div"); wrap.className = "donutwrap"; host.appendChild(wrap);
    const R = 80, r = 50, svg = el("svg", { viewBox: "-90 -90 180 180", width: 180, height: 180, role: "img", "aria-label": cfg.title || "share", style: "flex:none;max-width:45%;height:auto" }, wrap);
    let a = -Math.PI / 2;
    items.forEach(it => {
      const f = it.value / total, a2 = a + f * 2 * Math.PI, large = f > 0.5 ? 1 : 0;
      const p = (rad, ang) => `${(rad * Math.cos(ang)).toFixed(2)} ${(rad * Math.sin(ang)).toFixed(2)}`;
      const d = f >= 0.9999 ? `M ${p(R, 0)} A ${R} ${R} 0 1 1 ${p(R, Math.PI)} A ${R} ${R} 0 1 1 ${p(R, 0)} M ${p(r, 0)} A ${r} ${r} 0 1 0 ${p(r, Math.PI)} A ${r} ${r} 0 1 0 ${p(r, 0)}`
        : `M ${p(R, a)} A ${R} ${R} 0 ${large} 1 ${p(R, a2)} L ${p(r, a2)} A ${r} ${r} 0 ${large} 0 ${p(r, a)} Z`;
      el("path", { d, fill: it.color, stroke: "var(--surface)", "stroke-width": 2, "fill-rule": "evenodd" }, svg); a = a2; });
    const c = el("text", { x: 0, y: -2, "text-anchor": "middle", "font-size": 22, "font-weight": 700, fill: "var(--ink)" }, svg); c.textContent = fmt(total / (cfg.days || 1), 1);
    const c2 = el("text", { x: 0, y: 16, "text-anchor": "middle", "font-size": 10.5, fill: "var(--ink2)" }, svg); c2.textContent = cfg.center || "";
    const ul = document.createElement("ul"); ul.className = "donutlegend"; wrap.appendChild(ul);
    ul.innerHTML = (items.some(it => it.ref != null) ? `<li class="hd"><span></span><span class="nm"></span><span class="pc">now</span><span class="ref">${cfg.refLabel || ""}</span></li>` : "") + items.map(it => `<li><span class="sw" style="background:${it.color}"></span><span class="nm">${it.name}</span><span class="pc">${fmt(it.value / total * 100, 0)}%</span>${it.ref != null ? `<span class="ref">${fmt(it.ref * 100, 0)}%</span>` : ""}</li>`).join("");
  };

  // ---- sparkline ----------------------------------------------------------------------------------
  window.sparkline = function (vals, w, h, ref) {
    const pts = vals.map((v, i) => [i, v]).filter(p => p[1] != null);
    if (pts.length < 2) return "";
    const all = pts.map(p => p[1]).concat(ref != null ? [ref] : []);
    const lo = Math.min(...all), hi = Math.max(...all), X = i => 1 + i / (vals.length - 1) * (w - 4), Y = v => h - 2 - (v - lo) / (hi - lo || 1) * (h - 4);
    const d = pts.map((p, k) => (k ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1)).join("");
    const last = pts[pts.length - 1];
    return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true" style="display:block">` +
      (ref != null ? `<line x1="0" x2="${w}" y1="${Y(ref).toFixed(1)}" y2="${Y(ref).toFixed(1)}" style="stroke:var(--muted)" stroke-dasharray="2 2"/>` : "") +
      `<path d="${d}" fill="none" style="stroke:var(--s1)" stroke-width="1.5" stroke-linejoin="round"/><circle cx="${X(last[0]).toFixed(1)}" cy="${Y(last[1]).toFixed(1)}" r="2.2" style="fill:var(--s1)"/></svg>`;
  };
})();
