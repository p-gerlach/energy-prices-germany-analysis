"""`oco ships`: a foreground local web server that shows live AIS vessel positions on a zoomable map.

* binds to 127.0.0.1 by default; `--lan` makes it reachable from a phone on the same Wi-Fi (the API key is
  never sent to the browser either way)
* holds one aisstream subscription for the configured areas; stops with Ctrl+C and nothing keeps running after
* serves a self-contained page (no CDN, no map tiles) and a JSON snapshot that the page polls every few seconds
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..settings import env, sources_config
from .ais import StreamStatus, VesselStore, run_stream

HERE = Path(__file__).resolve().parent.parent / "web"


def area_boxes(names: list[str]) -> tuple[list, list[str]]:
    cfg = sources_config().get("ais_live", {})
    areas = cfg.get("areas", {})
    names = names or cfg.get("default_areas", ["gulf", "red_sea"])
    if "world" in names:
        return [[[-90, -180], [90, 180]]], ["world"]
    unknown = [n for n in names if n not in areas]
    if unknown:
        raise KeyError(f"unknown area(s) {unknown}; configured: {sorted(areas)} or 'world'")
    return [areas[n]["box"] for n in names], names


def land_for_live(ctx) -> list:
    """Natural Earth land, simplified less than the overview map so harbours stay recognisable when zoomed."""
    try:
        from ..collectors.portwatch_geo import simplified_land
        return simplified_land(ctx, tolerance=0.02, min_area=0.002) or []
    except Exception:  # noqa: BLE001 — map still works without land (graticule only)
        return []


def build_live_page(land: list, areas: list[str], boxes: list, chokepoints: list, snapshot: dict | None = None) -> str:
    tpl = (HERE / "live_template.html").read_text(encoding="utf-8")
    js = (HERE / "livemap.js").read_text(encoding="utf-8")
    boot = json.dumps({"land": land, "areas": areas, "boxes": boxes, "cps": chokepoints, "snapshot": snapshot}).replace("</", "<\\/")
    return tpl.replace("/*__LIVEMAP__*/", js).replace("__BOOT__", boot)


def make_handler(page: bytes, store: VesselStore, status: StreamStatus, info: dict):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the terminal quiet
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8")
            if path == "/api/ships":
                snap = store.snapshot()
                body = json.dumps({"ships": snap, "status": status.state, "detail": status.detail, "since": status.connected_since,
                                   "msgs": store.n_messages, "last": None if store.last_message_at is None else round(time.time() - store.last_message_at),
                                   **info}, separators=(",", ":")).encode()
                return self._send(200, body, "application/json")
            return self._send(404, b"not found", "text/plain")
    return H


def serve(ctx, areas: list[str], port: int = 8765, lan: bool = False, echo=print, stop: threading.Event | None = None,
          connect=None, on_ready=None) -> None:
    key = env("AISSTREAM_API_KEY")
    if not key:
        raise RuntimeError("AISSTREAM_API_KEY is not set. Create a free key yourself at https://aisstream.io/authenticate "
                           "(sign in with GitHub, then Account -> API key), put it in .env, and run `oco ships` again. "
                           "The application never registers accounts on your behalf.")
    boxes, names = area_boxes(areas)
    cps = []
    try:
        from ..storage.warehouse import open_snapshot
        con = open_snapshot(ctx.paths)
        if con is not None:
            r = con.execute("SELECT value FROM meta WHERE key='geo.chokepoints'").fetchone()
            cps = json.loads(r[0])["items"] if r else []
            con.close()
    except Exception:  # noqa: BLE001
        cps = []
    page = build_live_page(land_for_live(ctx), names, boxes, cps).encode("utf-8")
    store, status = VesselStore(), StreamStatus()
    stop = stop or threading.Event()

    def stream_thread():
        asyncio.run(run_stream(ctx.policy, key, boxes, store, status, stop, connect=connect))
    t = threading.Thread(target=stream_thread, name="aisstream", daemon=True)
    t.start()
    host = "0.0.0.0" if lan else "127.0.0.1"
    httpd = ThreadingHTTPServer((host, port), make_handler(page, store, status, {"areas": names}))
    httpd.timeout = 1
    echo(f"[ships] live map on http://127.0.0.1:{port}  (areas: {', '.join(names)})")
    if lan:
        echo("[ships] --lan: reachable from other devices on this network at http://<this computer's IP>:%d" % port)
    echo("[ships] positions come only while this window runs. Stop with Ctrl+C.")
    if on_ready:
        on_ready()
    try:
        while not stop.is_set():
            httpd.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
        t.join(timeout=5)
        echo("[ships] stopped. No live positions are collected until you start it again.")


def write_snapshot(ctx, areas: list[str], out: Path, seconds: int = 300, echo=print, connect=None) -> Path:
    """Collect real positions for `seconds`, then write a self-contained page that shows them as a dated snapshot
    (never labelled live). The key is used only for the subscription and is never written to the page."""
    key = env("AISSTREAM_API_KEY")
    if not key:
        raise RuntimeError("AISSTREAM_API_KEY is not set (create it yourself at https://aisstream.io/authenticate).")
    boxes, names = area_boxes(areas)
    store, status, stop = VesselStore(stale_after_s=max(3600, seconds * 2)), StreamStatus(), threading.Event()
    t = threading.Thread(target=lambda: asyncio.run(run_stream(ctx.policy, key, boxes, store, status, stop, connect=connect)), daemon=True)
    t.start()
    echo(f"[ships] collecting real positions for {seconds} s in {', '.join(names)} …")
    end = time.time() + seconds
    while time.time() < end and status.state != "stopped":
        time.sleep(1)
    stop.set()
    t.join(timeout=10)
    if status.state == "stopped" and store.n_messages == 0:
        raise RuntimeError(f"no data received: {status.detail}")
    from datetime import datetime, timezone
    ships = store.snapshot()
    taken = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cps = []
    try:
        from ..storage.warehouse import open_snapshot
        con = open_snapshot(ctx.paths)
        if con is not None:
            r = con.execute("SELECT value FROM meta WHERE key='geo.chokepoints'").fetchone()
            cps = json.loads(r[0])["items"] if r else []
            con.close()
    except Exception:  # noqa: BLE001
        cps = []
    html = build_live_page(land_for_live(ctx), names, boxes, cps, snapshot={"taken": taken, "seconds": seconds, "ships": ships})
    html = html.replace("<title>Live Ship Map</title>", "<title>Ship Positions Snapshot</title>")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    echo(f"[ships] wrote {out}: {len(ships)} ships from {store.n_messages} messages, taken {taken}")
    return out
