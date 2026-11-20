import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Read backend/.env once, before any module below reads the environment.
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from fastapi import Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.datastructures import MutableHeaders  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402

from settings import ConfigError, Unavailable, env_bool, env_list  # noqa: E402

try:
    import alerting  # noqa: E402
    import auth  # noqa: E402
    import k8s_client  # noqa: E402
    import vcenter_client  # noqa: E402

    auth.validate_settings()
    alerting.get_config()
    ENABLE_DEBUG_ENDPOINT = env_bool("ENABLE_DEBUG_ENDPOINT", False)
    TRUSTED_HOSTS = env_list("TRUSTED_HOSTS")
except ConfigError as exc:
    raise SystemExit(f"Configuration error: {exc}") from None

logger = logging.getLogger(__name__)


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


# ── Helpers ──────────────────────────────────────────────────────────────────

# The built SPA loads only its own scripts, styles and assets.
# frontend/nginx.conf sends the same policy for the SPA; keep both in step.
_CSP = "default-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}


class _SecurityHeaders:
    """Pure ASGI wrapper around the whole app, including Starlette's error
    handler, so the headers are also on unhandled 500 responses. A header the
    response already sets (the auth pages' own CSP) is kept."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _TrustedHostMiddleware(TrustedHostMiddleware):
    """TrustedHostMiddleware that leaves /healthz open: kubelet probes send the pod IP as Host."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/healthz":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


def _call(fn, *args):
    """Run a backend call; return a fixed message to the client and log the detail."""
    try:
        return fn(*args)
    except Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))  # text written by this app
    except Exception:
        logger.exception("%s failed", fn.__name__)
        raise HTTPException(status_code=500, detail="Internal error, see the server log")


# Minimum time between two forced alert e-mails of the same kind.
ALERT_TEST_MIN_INTERVAL_SECONDS = 60
_last_alert_test: dict[str, float] = {}


def _alert_post_guard(kind: str):
    async def guard(request: Request) -> None:
        auth.require_same_origin(request)
        # Runs on the event loop, so check-and-set needs no lock.
        now = time.monotonic()
        last = _last_alert_test.get(kind)
        if last is not None and now - last < ALERT_TEST_MIN_INTERVAL_SECONDS:
            wait = int(ALERT_TEST_MIN_INTERVAL_SECONDS - (now - last)) + 1
            raise HTTPException(status_code=429, detail=f"Try again in {wait} seconds")
        _last_alert_test[kind] = now
    return Depends(guard)


def _alert_response(result: dict) -> dict:
    if result.get("reason") == "not_configured":
        raise HTTPException(status_code=503, detail="SMTP not configured")
    if result.get("reason") == "smtp_error":
        raise HTTPException(status_code=502, detail=result["error"])
    return result


# ── Routes ───────────────────────────────────────────────────────────────────
#
# Handlers that call vCenter, Kubernetes or SMTP are plain `def`: FastAPI runs
# them in its thread pool, so a slow scan does not block /healthz or others.

def _register_routes(app: FastAPI) -> None:
    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    # vSphere
    @app.get("/api/health")
    def health():
        return _call(vcenter_client.check_connectivity)  # 503 while another scan holds the lock

    @app.get("/api/persistent-volumes")
    def persistent_volumes():
        return _call(vcenter_client.get_persistent_volumes)

    @app.get("/api/container-volumes")
    def container_volumes():
        return _call(vcenter_client.get_container_volumes)

    @app.get("/api/snapshots")
    def snapshots():
        return _call(vcenter_client.get_snapshots)

    @app.get("/api/orphaned-fcds")
    def orphaned_fcds():
        return _call(vcenter_client.get_orphaned_fcds)

    # Overview (joined PVC + FCD)
    @app.get("/api/overview")
    def volume_overview():
        container_vols = _call(vcenter_client.get_container_volumes)

        try:
            persistent_vols = vcenter_client.get_persistent_volumes()
        except Exception:
            logger.exception("overview: vSphere scan failed, showing Kubernetes data only")
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

    # Alerts
    @app.get("/api/alerts/status")
    def alerts_status():
        return alerting.get_status()

    @app.post("/api/alerts/test", dependencies=[_alert_post_guard("snapshots")])
    def alerts_test():
        return _alert_response(_call(alerting.check_and_alert, True))

    @app.post("/api/alerts/orphan-test", dependencies=[_alert_post_guard("orphans")])
    def alerts_orphan_test():
        return _alert_response(_call(alerting.check_and_alert_orphans, True))

    # Diagnostics: lists every virtual disk visible to the vCenter account.
    if ENABLE_DEBUG_ENDPOINT:
        logger.warning("ENABLE_DEBUG_ENDPOINT=true, /api/debug/vsphere is available")

        @app.get("/api/debug/vsphere")
        def debug_vsphere():
            return _call(vcenter_client.debug_vsphere_inventory)

    # Kubernetes
    @app.get("/api/k8s/health")
    def k8s_health():
        return k8s_client.check_k8s_connectivity()

    @app.get("/api/k8s/persistent-volumes")
    def k8s_persistent_volumes():
        return _call(k8s_client.get_k8s_persistent_volumes)

    @app.get("/api/k8s/persistent-volume-claims")
    def k8s_persistent_volume_claims():
        return _call(k8s_client.get_k8s_persistent_volume_claims)

    @app.get("/api/k8s/volume-snapshots")
    def k8s_volume_snapshots():
        return _call(k8s_client.get_k8s_volume_snapshots)

    @app.get("/api/k8s/volume-snapshot-contents")
    def k8s_volume_snapshot_contents():
        return _call(k8s_client.get_k8s_volume_snapshot_contents)


# ── App + middleware ─────────────────────────────────────────────────────────
#
# Middleware is applied in LIFO order: the LAST add_middleware call becomes
# the outermost wrapper (first to receive the request).
#
#   Request → security headers → (Starlette error handler) → TrustedHost → Session → Auth → routes
#
# SessionMiddleware must wrap AuthMiddleware so AuthMiddleware can read the session.
# The security headers wrap the stack Starlette builds, outside its error handler.

def create_app() -> FastAPI:
    # The SPA is served from the same origin, so no CORS and no API docs pages.
    app = FastAPI(
        title="vCenter Monitor", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )

    session_secret = auth.SESSION_SECRET
    if not session_secret:  # only possible with AUTH_ENABLED=false
        session_secret = secrets.token_hex(32)

    app.add_middleware(auth.AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie=auth.SESSION_COOKIE,
        https_only=auth.SESSION_SECURE,
        same_site="lax",
        max_age=auth.SESSION_MAX_AGE_HOURS * 3600,
    )
    if TRUSTED_HOSTS:
        app.add_middleware(_TrustedHostMiddleware, allowed_hosts=TRUSTED_HOSTS)
    build_stack = app.build_middleware_stack
    app.build_middleware_stack = lambda: _SecurityHeaders(build_stack())

    app.include_router(auth.router)
    _register_routes(app)

    # Serve the built React app in production.
    static_dir = os.path.join(os.path.dirname(__file__), "../frontend/dist")
    if os.path.exists(static_dir):
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


app = create_app()
