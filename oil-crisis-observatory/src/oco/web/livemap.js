/* Live ship map: canvas, Web Mercator, no tiles or libraries. Polls /api/ships from the local `oco ships` server.
   Ships glide from their previous to their newly reported position (never beyond the last report).
   Pan: drag. Zoom: wheel, pinch, double-tap, or the buttons. Tap/click a ship for its details. */
(function () {
  const BOOT = window.LIVE_BOOT;
  const R2D = 180 / Math.PI;
  const merc = (lon, lat) => [lon, -R2D * Math.log(Math.tan(Math.PI / 4 + Math.max(-84, Math.min(84, lat)) / R2D / 2))];
  const CLASSES = [["tanker", "Tankers"], ["cargo", "Cargo"], ["passenger", "Passenger"], ["fishing", "Fishing"],
    ["service", "Tugs & service"], ["military", "Military"], ["leisure", "Leisure"], ["high_speed", "High-speed"], ["other", "Other"], ["unknown", "Type not yet reported"]];
  let css = null;  // computed custom properties, refreshed once per frame (reading them per ship is slow)
  const cssVar = n => { if (!css) css = getComputedStyle(document.documentElement); return css.getPropertyValue(n).trim(); };
  const pal = {}; const colorOf = k => pal[k] || (pal[k] = cssVar("--k-" + k) || cssVar("--k-other"));

  const cv = document.getElementById("cv"), ctx = cv.getContext("2d");
  let W = 0, H = 0, dpr = 1;
  const view = { x: 50, y: -25, k: 40 };  // centre in projected units, pixels per unit
  const land = new Path2D();
  (BOOT.land || []).forEach(r => { r.forEach(([lo, la], i) => { const [x, y] = merc(lo, la); i ? land.lineTo(x, y) : land.moveTo(x, y); }); land.closePath(); });
  const ships = new Map();   // mmsi -> {d: data, fx, fy, tx, ty, t0}
  let selected = null, hidden = new Set(), query = "", needsDraw = true, lastSnap = null;

  function fit(boxes) {
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    boxes.forEach(([[la0, lo0], [la1, lo1]]) => { const a = merc(lo0, la0), b = merc(lo1, la1);
      x0 = Math.min(x0, a[0], b[0]); x1 = Math.max(x1, a[0], b[0]); y0 = Math.min(y0, a[1], b[1]); y1 = Math.max(y1, a[1], b[1]); });
    if (!isFinite(x0)) return;
    view.x = (x0 + x1) / 2; view.y = (y0 + y1) / 2; view.k = Math.min(W / (x1 - x0), H / (y1 - y0)) * 0.92;
  }
  const toScreen = (x, y) => [(x - view.x) * view.k + W / 2, (y - view.y) * view.k + H / 2];
  const toWorld = (sx, sy) => [(sx - W / 2) / view.k + view.x, (sy - H / 2) / view.k + view.y];

  function resize() {
    dpr = window.devicePixelRatio || 1; const r = cv.getBoundingClientRect(); W = r.width; H = r.height;
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); needsDraw = true;
  }
  function visible(s) {
    if (hidden.has(s.d.k)) return false;
    if (!query) return true;
    return (s.d.n || "").toLowerCase().includes(query) || String(s.d.m).includes(query) || String(s.d.imo || "").includes(query);
  }
  function pos(s, now) { const f = Math.min(1, (now - s.t0) / 1500), e = f < 1 ? 1 - Math.pow(1 - f, 3) : 1; return [s.fx + (s.tx - s.fx) * e, s.fy + (s.ty - s.fy) * e, f < 1]; }

  function draw(now) {
    css = null; for (const k in pal) delete pal[k];
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = cssVar("--water"); ctx.fillRect(0, 0, W, H);
    ctx.setTransform(dpr * view.k, 0, 0, dpr * view.k, dpr * (W / 2 - view.x * view.k), dpr * (H / 2 - view.y * view.k));
    ctx.fillStyle = cssVar("--land"); ctx.fill(land);
    ctx.lineWidth = 0.8 / view.k; ctx.strokeStyle = cssVar("--coast"); ctx.stroke(land);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    // subscribed areas
    ctx.setLineDash([5, 5]); ctx.strokeStyle = cssVar("--muted"); ctx.lineWidth = 1;
    (BOOT.boxes || []).forEach(([[la0, lo0], [la1, lo1]]) => { const a = toScreen(...merc(lo0, la0)), b = toScreen(...merc(lo1, la1)); ctx.strokeRect(Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.abs(b[0] - a[0]), Math.abs(b[1] - a[1])); });
    ctx.setLineDash([]);
    // chokepoint labels
    ctx.font = "600 11px " + cssVar("--body"); ctx.textBaseline = "middle";
    (BOOT.cps || []).forEach(c => { const [sx, sy] = toScreen(...merc(c.lon, c.lat)); if (sx < -50 || sy < -20 || sx > W + 50 || sy > H + 20) return;
      ctx.lineWidth = 3; ctx.strokeStyle = cssVar("--water"); ctx.fillStyle = cssVar("--ink2"); const t = c.name.replace(/ (Strait|Canal)$/, "").replace(/^Strait of /, "");
      ctx.strokeText(t, sx + 6, sy); ctx.fillText(t, sx + 6, sy); });
    // selected ship track
    if (selected && ships.has(selected)) { const s = ships.get(selected); const tr = s.d.tr || [];
      if (tr.length > 1) { ctx.beginPath(); tr.forEach(([la, lo], i) => { const [sx, sy] = toScreen(...merc(lo, la)); i ? ctx.lineTo(sx, sy) : ctx.moveTo(sx, sy); });
        ctx.strokeStyle = cssVar("--accent"); ctx.lineWidth = 2; ctx.setLineDash([4, 3]); ctx.stroke(); ctx.setLineDash([]); } }
    // ships
    let animating = false; const size = Math.max(4, Math.min(9, 3 + view.k / 40));
    for (const [m, s] of ships) {
      if (!visible(s)) continue;
      const [x, y, a] = pos(s, now); animating = animating || a;
      const [sx, sy] = toScreen(x, y); if (sx < -10 || sy < -10 || sx > W + 10 || sy > H + 10) continue;
      ctx.fillStyle = colorOf(s.d.k); ctx.strokeStyle = cssVar("--ship-ring"); ctx.lineWidth = 1;
      const heading = s.d.h ?? s.d.c, moving = (s.d.s || 0) >= 0.5 && heading != null;
      ctx.globalAlpha = s.d.a > 900 ? 0.45 : 1;   // no report for 15+ minutes: faded
      if (moving) { const r = heading / R2D; ctx.save(); ctx.translate(sx, sy); ctx.rotate(r);
        ctx.beginPath(); ctx.moveTo(0, -size * 1.4); ctx.lineTo(size * 0.75, size); ctx.lineTo(0, size * 0.55); ctx.lineTo(-size * 0.75, size); ctx.closePath(); ctx.fill(); ctx.stroke(); ctx.restore(); }
      else { ctx.beginPath(); ctx.arc(sx, sy, size * 0.55, 0, 2 * Math.PI); ctx.fill(); ctx.stroke(); }
      if (m === selected) { ctx.globalAlpha = 1; ctx.beginPath(); ctx.arc(sx, sy, size * 2.2, 0, 2 * Math.PI); ctx.strokeStyle = cssVar("--ink"); ctx.lineWidth = 2; ctx.stroke(); }
      ctx.globalAlpha = 1;
    }
    return animating;
  }
  function loop(now) { if (needsDraw || animatingNow) { animatingNow = draw(now); needsDraw = false; } requestAnimationFrame(loop); }
  let animatingNow = false;

  // ---------- data ----------
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmt = (v, d = 1) => v == null ? "–" : Number(v).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
  async function poll() {
    try {
      const r = await fetch("/api/ships", { cache: "no-store" }); const j = await r.json(); lastSnap = j;
      const now = performance.now(), seen = new Set();
      j.ships.forEach(d => { seen.add(d.m); const [x, y] = merc(d.lo, d.la); const s = ships.get(d.m);
        if (!s) ships.set(d.m, { d, fx: x, fy: y, tx: x, ty: y, t0: now - 2000 });
        else { const [cx, cy] = pos(s, now); if (x !== s.tx || y !== s.ty) { s.fx = cx; s.fy = cy; s.tx = x; s.ty = y; s.t0 = now; } s.d = d; } });
      for (const m of [...ships.keys()]) if (!seen.has(m)) ships.delete(m);
      status(j); counts(); if (selected) details(); needsDraw = true;
    } catch (e) { $("status").className = "pill bad"; $("status").textContent = "Viewer lost contact with oco ships — is it still running?"; }
    setTimeout(poll, 3000);
  }
  // snapshot pages (oco ships --snapshot) carry their positions inside the page and never poll
  function loadSnapshot(snap) {
    const now = performance.now();
    snap.ships.forEach(d => { const [x, y] = merc(d.lo, d.la); ships.set(d.m, { d, fx: x, fy: y, tx: x, ty: y, t0: now - 2000 }); });
    const el = $("status"); el.className = "pill warn";
    el.textContent = `Snapshot · ${snap.ships.length.toLocaleString("en-GB")} ships · ${snap.taken} · not live`;
    $("detail-status").textContent = `Real AIS positions collected for ${Math.round(snap.seconds / 60)} min up to ${snap.taken}. Ages under "Last report" are relative to that time. Run oco ships on your computer for the live map.`;
    counts(); needsDraw = true;
  }
  function status(j) {
    const el = $("status"), map = { connected: ["ok", "Live"], connecting: ["warn", "Connecting…"], reconnecting: ["warn", "Reconnecting"], stopped: ["bad", "Stopped"], starting: ["warn", "Starting"] };
    const [c, l] = map[j.status] || ["warn", j.status]; el.className = "pill " + c;
    el.textContent = `${l} · ${j.ships.length.toLocaleString("en-GB")} ships` + (j.last != null ? ` · last message ${j.last} s ago` : "");
    $("detail-status").textContent = j.detail || (j.status === "connected" && !j.ships.length ? "Connected. Waiting for the first position reports in your areas…" : "");
  }
  function counts() {
    const c = {}; for (const s of ships.values()) c[s.d.k] = (c[s.d.k] || 0) + 1;
    $("classes").innerHTML = CLASSES.filter(([k]) => c[k]).map(([k, l]) => `<label class="cls"><input type="checkbox" data-k="${k}" ${hidden.has(k) ? "" : "checked"}><span class="sw" style="background:var(--k-${k})"></span><span class="nm">${l}</span><span class="ct">${(c[k] || 0).toLocaleString("en-GB")}</span></label>`).join("") || '<p class="hint">No ships yet.</p>';
  }
  function details() {
    const s = ships.get(selected), box = $("panel");
    if (!s) { box.hidden = true; return; }
    const d = s.d, cls = (CLASSES.find(c => c[0] === d.k) || [0, d.k])[1];
    const row = (k, v) => v == null || v === "" ? "" : `<div class="r"><span>${k}</span><b>${v}</b></div>`;
    box.innerHTML = `<button type="button" class="close" id="close" aria-label="Close details">×</button>
      <div class="kicker"><span class="sw" style="background:var(--k-${d.k})"></span>${esc(cls)}</div>
      <h2>${esc(d.n || "Name not yet reported")}</h2>
      <div class="sub">${esc(d.tt)}${d.f ? " · flag " + esc(d.f) : ""}</div>
      <div class="grid">${row("Speed", d.s == null ? null : fmt(d.s) + " kn")}${row("Course", d.c == null ? null : fmt(d.c, 0) + "°")}${row("Heading", d.h == null ? null : fmt(d.h, 0) + "°")}
        ${row("Status", esc(d.nav))}${row("Destination", esc(d.d))}${row("ETA", esc(d.eta))}${row("Draught", d.dr == null ? null : fmt(d.dr) + " m")}
        ${row("Size", d.L ? fmt(d.L, 0) + " × " + fmt(d.B, 0) + " m" : null)}${row("MMSI", d.m)}${row("IMO", d.imo)}${row("Call sign", esc(d.cs))}
        ${row("Position", fmt(d.la, 4) + ", " + fmt(d.lo, 4))}${row("Last report", d.a < 90 ? d.a + " s ago" : Math.round(d.a / 60) + " min ago")}</div>
      <p class="hint">Destination and ETA are typed in by the crew and can be wrong. AIS identities can be spoofed.</p>
      <a href="https://www.marinetraffic.com/en/ais/details/ships/mmsi:${encodeURIComponent(d.m)}" target="_blank" rel="noopener">Look this ship up on MarineTraffic</a>`;
    box.hidden = false;
    $("close").onclick = () => { selected = null; box.hidden = true; needsDraw = true; };
  }

  // ---------- interaction ----------
  const pointers = new Map(); let pinch = null, downAt = null, lastTap = 0;
  function zoomAt(f, sx, sy) { const [wx, wy] = toWorld(sx, sy); view.k = Math.max(2, Math.min(20000, view.k * f)); const [nx, ny] = toWorld(sx, sy); view.x += wx - nx; view.y += wy - ny; needsDraw = true; }
  cv.addEventListener("wheel", e => { e.preventDefault(); const r = cv.getBoundingClientRect(); zoomAt(e.deltaY < 0 ? 1.25 : 0.8, e.clientX - r.left, e.clientY - r.top); }, { passive: false });
  cv.addEventListener("pointerdown", e => { cv.setPointerCapture(e.pointerId); pointers.set(e.pointerId, [e.clientX, e.clientY]); downAt = [e.clientX, e.clientY, performance.now()];
    if (pointers.size === 2) { const [a, b] = [...pointers.values()]; pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), k: view.k }; } });
  cv.addEventListener("pointermove", e => { if (!pointers.has(e.pointerId)) return; const prev = pointers.get(e.pointerId); pointers.set(e.pointerId, [e.clientX, e.clientY]);
    if (pointers.size === 2 && pinch) { const [a, b] = [...pointers.values()], r = cv.getBoundingClientRect(); const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
      zoomAt((pinch.k * d / pinch.d) / view.k, (a[0] + b[0]) / 2 - r.left, (a[1] + b[1]) / 2 - r.top); }
    else if (pointers.size === 1) { view.x -= (e.clientX - prev[0]) / view.k; view.y -= (e.clientY - prev[1]) / view.k; needsDraw = true; } });
  const up = e => { pointers.delete(e.pointerId); if (pointers.size < 2) pinch = null;
    if (downAt && pointers.size === 0 && Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) < 6 && performance.now() - downAt[2] < 400) tap(e);
    if (pointers.size === 0) downAt = null; };
  cv.addEventListener("pointerup", up); cv.addEventListener("pointercancel", up);
  function tap(e) {
    const r = cv.getBoundingClientRect(), sx = e.clientX - r.left, sy = e.clientY - r.top, now = performance.now();
    if (now - lastTap < 300) { zoomAt(2, sx, sy); lastTap = 0; return; } lastTap = now;
    let best = null, bd = 18;
    for (const [m, s] of ships) { if (!visible(s)) continue; const [x, y] = pos(s, now); const [px, py] = toScreen(x, y); const d = Math.hypot(px - sx, py - sy); if (d < bd) { bd = d; best = m; } }
    selected = best; details(); if (!best) $("panel").hidden = true; else keepVisible(ships.get(best)); needsDraw = true;
  }
  // on phones the details sheet covers the lower half: move the map so the chosen ship sits in the upper part
  function keepVisible(s) {
    if (!s || innerWidth > 720) return;
    const [, sy] = toScreen(s.tx, s.ty); if (sy < H * 0.4) return;
    view.y = s.ty + (H * 0.2) / view.k; view.x = s.tx;
  }
  $("zin").onclick = () => zoomAt(1.6, W / 2, H / 2); $("zout").onclick = () => zoomAt(1 / 1.6, W / 2, H / 2);
  $("zfit").onclick = () => { fit(BOOT.boxes); needsDraw = true; };
  $("classes").addEventListener("change", e => { const k = e.target.dataset.k; if (!k) return; e.target.checked ? hidden.delete(k) : hidden.add(k); needsDraw = true; });
  $("q").addEventListener("input", e => { query = e.target.value.trim().toLowerCase(); needsDraw = true; });
  $("q").addEventListener("keydown", e => { if (e.key !== "Enter") return; for (const [m, s] of ships) if (visible(s)) { selected = m; view.x = s.tx; view.y = s.ty; view.k = Math.max(view.k, 400); details(); keepVisible(s); needsDraw = true; break; } });
  $("togglefilters").onclick = () => { const f = $("filters"); f.classList.toggle("open"); $("togglefilters").setAttribute("aria-expanded", f.classList.contains("open")); };
  window.addEventListener("resize", () => { resize(); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => { needsDraw = true; });

  $("areas").textContent = BOOT.areas.join(", ");
  resize(); fit(BOOT.boxes); requestAnimationFrame(loop); if (BOOT.snapshot) loadSnapshot(BOOT.snapshot); else poll();
})();
