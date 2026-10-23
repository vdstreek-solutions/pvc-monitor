import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import alerting
import auth
import k8s_client
import vcenter_client

logger = logging.getLogger(__name__)

# ── Session config ───────────────────────────────────────────────────────────

_SESSION_SECRET = os.getenv("SESSION_SECRET", "")
if not _SESSION_SECRET:
    _SESSION_SECRET = secrets.token_hex(32)
    logger.warning("SESSION_SECRET not set — using random key; sessions won't survive restarts")

_SESSION_SECURE = os.getenv("SESSION_SECURE", "false").lower() == "true"


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    auth.init_oauth()
    task = asyncio.create_task(alerting.alert_scheduler())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── App + middleware ─────────────────────────────────────────────────────────
#
# Middleware is applied in LIFO order: the LAST add_middleware call becomes
# the outermost wrapper (first to receive the request).
#
#   Request → SessionMiddleware → AuthMiddleware → CORSMiddleware → routes
#
# SessionMiddleware must be outermost so AuthMiddleware can read the session.

app = FastAPI(title="vCenter Monitor", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(auth.AuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=_SESSION_SECRET,
    https_only=_SESSION_SECURE,
    same_site="lax",
    max_age=86400 * 7,  # 7 days
)

app.include_router(auth.router)


# ── Process health (liveness probe — no external calls) ─────────────────────

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ── vSphere endpoints ────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return vcenter_client.check_connectivity()


@app.get("/api/persistent-volumes")
async def persistent_volumes():
    try:
        return vcenter_client.get_persistent_volumes()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/container-volumes")
async def container_volumes():
    try:
        return vcenter_client.get_container_volumes()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/snapshots")
async def snapshots():
    try:
        return vcenter_client.get_snapshots()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/orphaned-fcds")
async def orphaned_fcds():
    try:
        return vcenter_client.get_orphaned_fcds()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Overview (joined PVC + FCD) ──────────────────────────────────────────────

@app.get("/api/overview")
async def volume_overview():
    try:
        container_vols = vcenter_client.get_container_volumes()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    try:
        persistent_vols = vcenter_client.get_persistent_volumes()
    except Exception:
        persistent_vols = []

    vsphere_by_pv = {v["pv_name"].lower(): v for v in persistent_vols}

    result = []
    for pvc in container_vols:
        pv_name    = (pvc.get("pv_name") or "").lower()
        vsphere    = vsphere_by_pv.get(pv_name)
        pvc_status = pvc.get("status") or ""

        if pvc_status not in ("Bound",):
            usage_state = pvc_status.lower() if pvc_status else "unknown"
        elif vsphere and (vsphere.get("vm_name") or "—") not in ("—", ""):
            usage_state = "active"
        else:
            usage_state = "idle"

        result.append({
            "namespace":      pvc["namespace"],
            "pvc_name":       pvc["name"],
            "pv_name":        pvc.get("pv_name") or "—",
            "volume_handle":  pvc.get("volume_handle") or "—",
            "capacity":       pvc.get("capacity") or "—",
            "pvc_status":     pvc_status,
            "storage_class":  pvc.get("storage_class") or "—",
            "access_modes":   pvc.get("access_modes") or [],
            "pvc_created":    pvc.get("created"),
            "vsphere_found":  vsphere is not None,
            "vm_name":        (vsphere or {}).get("vm_name") or "—",
            "vm_power_state": (vsphere or {}).get("vm_power_state") or "—",
            "datastore":      (vsphere or {}).get("datastore") or "—",
            "usage_state":    usage_state,
        })

    return result


# ── Alert endpoints ──────────────────────────────────────────────────────────

@app.get("/api/alerts/status")
async def alerts_status():
    return alerting.get_status()


@app.post("/api/alerts/test")
async def alerts_test():
    try:
        result = alerting.check_and_alert(force=True)
        if result.get("reason") == "not_configured":
            raise HTTPException(status_code=503, detail="SMTP not configured")
        if result.get("reason") == "smtp_error":
            raise HTTPException(status_code=502, detail=result.get("error", "SMTP error"))
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/alerts/orphan-test")
async def alerts_orphan_test():
    try:
        result = alerting.check_and_alert_orphans(force=True)
        if result.get("reason") == "not_configured":
            raise HTTPException(status_code=503, detail="SMTP not configured")
        if result.get("reason") == "smtp_error":
            raise HTTPException(status_code=502, detail=result.get("error", "SMTP error"))
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Diagnostic endpoint ──────────────────────────────────────────────────────

@app.get("/api/debug/vsphere")
async def debug_vsphere():
    try:
        return vcenter_client.debug_vsphere_inventory()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Kubernetes endpoints ─────────────────────────────────────────────────────

@app.get("/api/k8s/health")
async def k8s_health():
    return k8s_client.check_k8s_connectivity()


@app.get("/api/k8s/persistent-volumes")
async def k8s_persistent_volumes():
    try:
        return k8s_client.get_k8s_persistent_volumes()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/k8s/persistent-volume-claims")
async def k8s_persistent_volume_claims():
    try:
        return k8s_client.get_k8s_persistent_volume_claims()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/k8s/volume-snapshots")
async def k8s_volume_snapshots():
    try:
        return k8s_client.get_k8s_volume_snapshots()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/k8s/volume-snapshot-contents")
async def k8s_volume_snapshot_contents():
    try:
        return k8s_client.get_k8s_volume_snapshot_contents()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Serve built React app in production ─────────────────────────────────────

_static_dir = os.path.join(os.path.dirname(__file__), "../frontend/dist")
if os.path.exists(_static_dir):
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
