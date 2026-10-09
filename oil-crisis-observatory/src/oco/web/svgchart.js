/* Minimal self-contained SVG line charts (no external libraries, works offline and under strict CSP).
   lineChart(el, {series:[{name,x:[iso dates],y:[numbers|null],color,width,opacity,dash}], ytitle, band:{x0,x1,y0,y1,label},
             vlines:[{x,label}], digits, zero:false, height})
   Gaps (null) break the line. Hover/touch shows a crosshair with values for the nearest date. Colours come from CSS vars. */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  // CSS custom properties are not reliable inside SVG presentation attributes, so colours go into inline style.
  const el = (tag, attrs, parent) => {
    const e = document.createElementNS(NS, tag); let style = "";
    for (const k in attrs) {
      const v = attrs[k];
      if ((k === "fill" || k === "stroke") && String(v).includes("var(")) style += `${k}:${v};`;
      else e.setAttribute(k, v);
    }
    if (style) e.setAttribute("style", (e.getAttribute("style") || "") + style);
    if (parent) parent.appendChild(e); return e;
  };
  const toT = s => Date.parse(String(s).slice(0, 10) + "T00:00:00Z");
  const fmtNum = (v, d) => v == null || !isFinite(v) ? "–" : Number(v).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
  const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

  function niceTicks(lo, hi, n) {
    if (!(hi > lo)) { hi = lo + 1; }
    const span = hi - lo, step0 = span / Math.max(1, n), mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= step0) || 10 * mag;
    const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
    return { ticks: out, step };
  }
  function timeTicks(t0, t1, width) {
    const days = (t1 - t0) / 864e5, maxTicks = Math.max(2, Math.floor(width / 80));
    const months = [1, 2, 3, 4, 6, 12, 24].find(m => days / 30.4 / m <= maxTicks) || 24;
    const d = new Date(t0); d.setUTCDate(1); d.setUTCHours(0, 0, 0, 0);
    while (d.getUTCMonth() % months) d.setUTCMonth(d.getUTCMonth() + 1);
    const out = [];
    while (d.getTime() <= t1) { if (d.getTime() >= t0) out.push(d.getTime()); d.setUTCMonth(d.getUTCMonth() + months); }
    return out.map(t => { const x = new Date(t); const m = x.getUTCMonth(); return { t, label: (m === 0 || months >= 12) ? String(x.getUTCFullYear()) : MONTHS[m] + (out.length < 5 ? " " + x.getUTCFullYear() : "") }; });
  }

  function render(host, cfg) {
    const toT = cfg.xnumeric ? (v => Number(v)) : (s => Date.parse(String(s).slice(0, 10) + "T00:00:00Z"));
    host.innerHTML = "";
    host.style.position = "relative";
    const W = Math.max(280, host.clientWidth || 600), H = cfg.height || host.clientHeight || 340;
    // legend layout first (wraps onto extra rows) so the plot area can reserve the space it needs
    const legendRows = []; { let row = [], w = 0; const maxW = W - 56 - 20;
      if (cfg.series.length > 1) cfg.series.forEach(s => { const sw = 34 + s.name.length * 6.3; if (row.length && w + sw > maxW) { legendRows.push(row); row = []; w = 0; } row.push([s, w]); w += sw; });
      if (row.length) legendRows.push(row); }
    const m = { l: 56, r: 24, t: 12, b: 30 + legendRows.length * 18 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    const pts = [];
    let tmin = Infinity, tmax = -Infinity, ymin = Infinity, ymax = -Infinity;
    cfg.series.forEach(s => s.x.forEach((x, i) => { const t = toT(x), y = s.y[i]; if (!isFinite(t)) return; tmin = Math.min(tmin, t); tmax = Math.max(tmax, t); if (y != null && isFinite(y)) { ymin = Math.min(ymin, y); ymax = Math.max(ymax, y); } }));
    if (cfg.band) { ymin = Math.min(ymin, cfg.band.y0); ymax = Math.max(ymax, cfg.band.y1); }
    if (cfg.zero) ymin = Math.min(0, ymin);
    if (!isFinite(tmin) || !isFinite(ymin)) { host.innerHTML = '<p class="nodata">No data available for this chart yet.</p>'; return; }
    const pad = (ymax - ymin) * 0.06 || 1; ymin -= pad; ymax += pad;
    const yt = niceTicks(ymin, ymax, Math.max(3, Math.floor(ph / 55)));
    ymin = Math.min(ymin, yt.ticks[0]); ymax = Math.max(ymax, yt.ticks[yt.ticks.length - 1]);
    const X = t => m.l + (t - tmin) / (tmax - tmin || 1) * pw, Y = v => m.t + (1 - (v - ymin) / (ymax - ymin || 1)) * ph;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": cfg.title || cfg.ytitle || "chart", style: "display:block;max-width:100%;height:auto;font-family:var(--body);" }, host);
    // grid + y axis
    const digits = cfg.digits ?? (yt.step < 0.1 ? 3 : yt.step < 1 ? 2 : yt.step < 10 ? 1 : 0);
    yt.ticks.forEach(v => { const y = Y(v); el("line", { x1: m.l, x2: m.l + pw, y1: y, y2: y, stroke: "var(--rule)", "stroke-width": 1 }, svg);
      const t = el("text", { x: m.l - 8, y: y + 4, "text-anchor": "end", "font-size": 11, fill: "var(--ink2)" }, svg); t.textContent = fmtNum(v, Math.min(digits, 3)); });
    if (cfg.ytitle) { const t = el("text", { x: 12, y: m.t + ph / 2, transform: `rotate(-90 12 ${m.t + ph / 2})`, "text-anchor": "middle", "font-size": 11, fill: "var(--ink2)" }, svg); t.textContent = cfg.ytitle; }
    const xticks = cfg.xnumeric ? niceTicks(tmin, tmax, Math.max(3, Math.floor(pw / 70))).ticks.map(t => ({ t, label: (cfg.xprefix || "") + t }))
                                : timeTicks(tmin, tmax, pw);
    xticks.forEach(k => { const x = X(k.t); el("line", { x1: x, x2: x, y1: m.t + ph, y2: m.t + ph + 4, stroke: "var(--ink2)" }, svg);
      const anchor = x > m.l + pw - 24 ? "end" : (x < m.l + 24 ? "start" : "middle");
      const t = el("text", { x, y: m.t + ph + 17, "text-anchor": anchor, "font-size": 11, fill: "var(--ink2)" }, svg); t.textContent = k.label; });
    el("line", { x1: m.l, x2: m.l + pw, y1: m.t + ph, y2: m.t + ph, stroke: "var(--ink2)", "stroke-width": 1 }, svg);
    // band
    if (cfg.band) { const b = cfg.band, x0 = X(Math.max(tmin, toT(b.x0))), x1 = X(Math.min(tmax, toT(b.x1)));
      el("rect", { x: Math.min(x0, x1), y: Y(b.y1), width: Math.abs(x1 - x0) || pw, height: Math.max(1, Y(b.y0) - Y(b.y1)), fill: "var(--muted)", opacity: 0.16 }, svg);
      if (b.label) { const t = el("text", { x: Math.min(x0, x1) + 4, y: m.t + 11, "font-size": 10.5, fill: "var(--ink2)" }, svg); t.textContent = b.label; } }
    (cfg.vlines || []).forEach(v => { const t = toT(v.x); if (t < tmin || t > tmax) return; const x = X(t);
      el("line", { x1: x, x2: x, y1: m.t, y2: m.t + ph, stroke: "var(--ink2)", "stroke-dasharray": "3 3", "stroke-width": 1 }, svg);
      if (v.label) { const tx = el("text", { x: x + 4, y: m.t + 11, "font-size": 10.5, fill: "var(--ink2)" }, svg); tx.textContent = v.label; } });
    // lines (break at nulls)
    cfg.series.forEach(s => {
      let d = "", pen = false;
      s.x.forEach((x, i) => { const y = s.y[i]; if (y == null || !isFinite(y)) { pen = false; return; } d += (pen ? "L" : "M") + X(toT(x)).toFixed(1) + " " + Y(y).toFixed(1); pen = true; });
      el("path", { d, fill: "none", stroke: s.color, "stroke-width": s.width || 2, opacity: s.opacity ?? 1, "stroke-linejoin": "round", "stroke-linecap": "round", ...(s.dash ? { "stroke-dasharray": s.dash } : {}) }, svg);
      const li = s.y.map((v, i) => v != null && isFinite(v) ? i : -1).filter(i => i >= 0).pop();
      if (li != null && !s.noDot) el("circle", { cx: X(toT(s.x[li])), cy: Y(s.y[li]), r: 4, fill: s.color, stroke: "var(--surface)", "stroke-width": 2 }, svg);
      pts.push(s);
    });
    // legend
    legendRows.forEach((row, ri) => { const ly = m.t + ph + 40 + ri * 18;
      row.forEach(([s, x0]) => { const lx = m.l + x0; el("line", { x1: lx, x2: lx + 16, y1: ly - 4, y2: ly - 4, stroke: s.color, "stroke-width": 3 }, svg);
        const t = el("text", { x: lx + 20, y: ly, "font-size": 11.5, fill: "var(--ink)" }, svg); t.textContent = s.name; }); });
    // hover
    const cross = el("line", { y1: m.t, y2: m.t + ph, stroke: "var(--ink2)", "stroke-width": 1, visibility: "hidden" }, svg);
    const tip = document.createElement("div"); tip.className = "svgtip"; tip.hidden = true; host.appendChild(tip);
    const idx = pts.map(s => s.x.map(toT));
    const hit = el("rect", { x: m.l, y: m.t, width: pw, height: ph, fill: "transparent", style: "cursor:crosshair" }, svg);
    function move(clientX) {
      const r = svg.getBoundingClientRect(), sx = (clientX - r.left) * (W / r.width);
      const t = tmin + (sx - m.l) / pw * (tmax - tmin);
      let best = null;
      pts.forEach((s, k) => { let bi = -1, bd = Infinity; idx[k].forEach((tt, i) => { const dd = Math.abs(tt - t); if (dd < bd && s.y[i] != null) { bd = dd; bi = i; } }); if (bi >= 0 && (!best || bd < best.bd)) best = { bd, t: idx[k][bi] }; });
      if (!best) return;
      const x = X(best.t); cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
      const day = cfg.xnumeric ? (cfg.xprefix || "") + best.t : new Date(best.t).toISOString().slice(0, 10);
      const rows = pts.map((s, k) => { const i = idx[k].indexOf(best.t); const v = i >= 0 ? s.y[i] : null; return `<div><span style="color:${s.color}">●</span> ${s.name}: <b>${fmtNum(v, cfg.digits ?? digits)}</b></div>`; }).join("");
      tip.innerHTML = `<div class="d">${day}</div>${rows}`; tip.hidden = false;
      const px = x * (r.width / W); tip.style.left = Math.min(Math.max(4, px + 10), r.width - tip.offsetWidth - 4) + "px"; tip.style.top = "8px";
    }
    hit.addEventListener("pointermove", e => move(e.clientX));
    hit.addEventListener("pointerdown", e => move(e.clientX));
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; });
  }

  const registry = [];
  window.lineChart = function (host, cfg) { registry.push([host, cfg]); try { render(host, cfg); } catch (e) { host.innerHTML = '<p class="nodata">Chart could not be drawn: ' + String(e).replace(/</g, "&lt;") + '</p>'; } };
  window.redrawCharts = function () { registry.forEach(([h, c]) => { try { render(h, c); } catch (e) {} }); };
  let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(window.redrawCharts, 150); });
})();
