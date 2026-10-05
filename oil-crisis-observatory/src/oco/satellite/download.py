"""Raw Sentinel product download via the CDSE OData $value route with the user's FREE General User login.

* token from the official CDSE identity endpoint (client_id cdse-public); refreshed only via refresh_token
* bounded: per-run product cap, rolling 30-day download budget, total disk budget — exceeding any => stop/queue
* downloads go through the guarded client; redirects validated; Authorization only on approved download hosts
* NO processing services, NO paid extension, NO cloud fallback: if this route fails, the job is blocked
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from ..collectors.base import Context, NotConfigured
from ..http import AuthRejected
from ..settings import budgets, env
from ..storage.state import utcnow
from ..storage.warehouse import Warehouse

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
DOWNLOAD = "https://download.dataspace.copernicus.eu/odata/v1/Products({id})/$value"


class BudgetExceeded(Exception):
    pass


class CDSEAuth:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.user, self.pw = env("CDSE_USERNAME"), env("CDSE_PASSWORD")
        if not (self.user and self.pw):
            raise NotConfigured("CDSE raw downloads need YOUR free General User login (CDSE_USERNAME/CDSE_PASSWORD in .env). "
                                "Register yourself at https://dataspace.copernicus.eu/ — the app never registers for you.")
        self._access = None
        self._refresh = None
        self._exp = 0.0

    def secrets(self):
        return [self.user, self.pw] + ([self._access] if self._access else []) + ([self._refresh] if self._refresh else [])

    def token(self) -> str:
        if self._access and time.time() < self._exp - 60:
            return self._access
        with self.ctx.client("cdse_download", secrets=self.secrets()) as c:
            if self._refresh:
                data = {"grant_type": "refresh_token", "refresh_token": self._refresh, "client_id": "cdse-public"}
            else:
                data = {"grant_type": "password", "username": self.user, "password": self.pw, "client_id": "cdse-public"}
            r = c.post(TOKEN_URL, data=data)
        tok = r.json()
        if "access_token" not in tok:
            raise AuthRejected("CDSE token response lacks access_token")
        self._access, self._refresh = tok["access_token"], tok.get("refresh_token")
        self._exp = time.time() + float(tok.get("expires_in", 600))
        return self._access


def disk_usage_gb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total / 1e9


def check_budget(ctx: Context, size_bytes: int | None):
    b = budgets()
    used30 = ctx.state.downloaded_bytes("cdse_download", days=30)
    if size_bytes and used30 + size_bytes > b.download_gb * 1e9:
        raise BudgetExceeded(f"30-day download budget {b.download_gb} GB would be exceeded ({used30/1e9:.2f} GB used)")
    disk = disk_usage_gb(ctx.paths.data)
    if disk + (size_bytes or 0) / 1e9 > b.disk_gb:
        raise BudgetExceeded(f"disk budget {b.disk_gb} GB would be exceeded ({disk:.2f} GB used)")
    free = shutil.disk_usage(ctx.paths.data).free
    if size_bytes and free < 3 * size_bytes:
        raise BudgetExceeded("insufficient local free disk space (need 3x product size for extraction)")


def queue_product(ctx: Context, product_id: str, reason: str) -> int | None:
    return ctx.state.enqueue("cdse_download", {"product_id": product_id, "reason": reason}, dedupe_key=f"dl:{product_id}", max_attempts=4)


def download_product(ctx: Context, wh: Warehouse, product_id: str, auth: CDSEAuth | None = None) -> Path:
    row = wh.con.execute("SELECT product_name, size_bytes, local_path FROM satellite_scenes WHERE product_id=?", [product_id]).fetchone()
    if not row:
        raise KeyError(f"product {product_id} not in catalogue table; run catalogue refresh first")
    name, size, local = row
    if local and Path(local).exists():
        return Path(local)
    check_budget(ctx, size)
    auth = auth or CDSEAuth(ctx)
    dest = ctx.paths.satellite / "products" / f"{name or product_id}.zip"
    for attempt in range(2):
        try:
            with ctx.client("cdse_download", secrets=auth.secrets()) as c:
                r = c.get(DOWNLOAD.format(id=product_id), auth_header=f"Bearer {auth.token()}", stream_to=dest)
            break
        except AuthRejected:
            if attempt == 1:
                raise
            auth._access = None  # expired approved token: refresh once, never escalate entitlements
    ctx.state.add_download_bytes("cdse_download", r.size)
    wh.con.execute("UPDATE satellite_scenes SET download_status='downloaded', local_path=?, local_sha256=? WHERE product_id=?",
                   [str(dest), r.sha256, product_id])
    ctx.state.record_probe("cdse_download", "authenticated_download", True,
                           f"downloaded {name} ({r.size/1e6:.1f} MB) at {utcnow():%Y-%m-%d %H:%M} UTC")
    return dest


def process_queue(ctx: Context, wh: Warehouse, limit: int | None = None) -> list[dict]:
    limit = limit or int(budgets().extras.get("max_products_per_run", 2))
    out = []
    try:
        auth = CDSEAuth(ctx)
    except NotConfigured as e:
        for j in ctx.state.due_jobs("cdse_download", limit):
            ctx.state.finish_job(j["id"], False, str(e), blocked=True)
        return [{"status": "unconfigured", "message": str(e)}]
    for j in ctx.state.due_jobs("cdse_download", limit):
        pid = j["payload"]["product_id"]
        try:
            p = download_product(ctx, wh, pid, auth)
            ctx.state.finish_job(j["id"], True)
            out.append({"product_id": pid, "status": "downloaded", "path": str(p)})
        except BudgetExceeded as e:
            ctx.state.finish_job(j["id"], False, str(e), retry_in_min=24 * 60)
            out.append({"product_id": pid, "status": "budget", "message": str(e)})
            break
        except Exception as e:  # noqa: BLE001
            ctx.state.finish_job(j["id"], False, f"{type(e).__name__}: {str(e)[:300]}")
            out.append({"product_id": pid, "status": "failed", "message": f"{type(e).__name__}: {str(e)[:200]}"})
    return out
