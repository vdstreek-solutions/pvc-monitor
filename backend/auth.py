import os
import logging
from urllib.parse import quote
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, JSONResponse, HTMLResponse

logger = logging.getLogger(__name__)

AUTH_ENABLED       = os.getenv("AUTH_ENABLED", "true").lower() != "false"
OIDC_ISSUER        = os.getenv("OIDC_ISSUER", "").rstrip("/")
OIDC_CLIENT_ID     = os.getenv("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "")
OIDC_REDIRECT_URI  = os.getenv("OIDC_REDIRECT_URI", "")
OIDC_SCOPES        = os.getenv("OIDC_SCOPES", "openid email profile groups")
ALLOWED_GROUPS     = {g.strip() for g in os.getenv("ALLOWED_GROUPS", "").split(",") if g.strip()}

_SKIP_PREFIXES = ("/auth/", "/healthz")

oauth = OAuth()


def init_oauth() -> None:
    if not AUTH_ENABLED:
        logger.info("Auth: disabled via AUTH_ENABLED=false")
        return
    if not OIDC_ISSUER:
        logger.warning("Auth: OIDC_ISSUER not set — running without authentication")
        return
    # authlib's encode_client_secret_basic does raw base64("id:secret") with no
    # URL-encoding. Dex URL-decodes per RFC 6749 §2.3.1, so a bare % in the
    # secret (e.g. %ll) is rejected as invalid percent-encoding. Pre-encoding
    # here means Dex receives %25ll, decodes it back to %ll, and matches.
    oauth.register(
        name="dex",
        server_metadata_url=f"{OIDC_ISSUER}/.well-known/openid-configuration",
        client_id=OIDC_CLIENT_ID,
        client_secret=quote(OIDC_CLIENT_SECRET, safe=""),
        client_kwargs={
            "scope": OIDC_SCOPES,
            "token_endpoint_auth_method": "client_secret_basic",
        },
    )
    if ALLOWED_GROUPS:
        logger.info("Auth: Dex OIDC ready — issuer=%s allowed_groups=%s", OIDC_ISSUER, ALLOWED_GROUPS)
    else:
        logger.info("Auth: Dex OIDC ready — issuer=%s (all authenticated users allowed)", OIDC_ISSUER)


def auth_active() -> bool:
    return AUTH_ENABLED and bool(OIDC_ISSUER)


def _safe_next(url: str) -> str:
    """Only allow relative paths — prevents open redirect."""
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return "/"


# ── Middleware ───────────────────────────────────────────────────────────────

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not auth_active():
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        if not request.session.get("user"):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            next_url = request.url.path
            if request.url.query:
                next_url += "?" + request.url.query
            return RedirectResponse(url=f"/auth/login?next={next_url}")

        return await call_next(request)


# ── Routes ───────────────────────────────────────────────────────────────────

router = APIRouter()


@router.get("/auth/login")
async def login(request: Request):
    if not auth_active():
        return RedirectResponse(url="/")
    next_url = _safe_next(request.query_params.get("next", "/"))
    request.session["next"] = next_url
    redirect_uri = OIDC_REDIRECT_URI or str(request.url_for("callback"))
    return await oauth.dex.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback", name="callback")
async def callback(request: Request):
    try:
        token = await oauth.dex.authorize_access_token(request)
    except Exception as exc:
        logger.warning("Auth: token exchange failed: %s", exc)
        return RedirectResponse(url="/auth/login")

    userinfo = token.get("userinfo")
    if not userinfo:
        try:
            userinfo = await oauth.dex.userinfo(token=token)
        except Exception as exc:
            logger.warning("Auth: userinfo fetch failed: %s", exc)
            return RedirectResponse(url="/auth/login")

    user = {
        "sub":    userinfo.get("sub", ""),
        "email":  userinfo.get("email", ""),
        "name":   (
            userinfo.get("name")
            or userinfo.get("preferred_username")
            or userinfo.get("email", "")
        ),
        "groups": userinfo.get("groups", []),
    }

    # Group membership check — skip if ALLOWED_GROUPS is not configured
    if ALLOWED_GROUPS and not set(user["groups"]).intersection(ALLOWED_GROUPS):
        logger.warning(
            "Auth: access denied — %s groups=%s not in allowed=%s",
            user["email"], user["groups"], ALLOWED_GROUPS,
        )
        request.session["denied"] = {"email": user["email"], "groups": user["groups"]}
        return RedirectResponse(url="/auth/forbidden")

    request.session.pop("denied", None)
    request.session["user"] = user
    next_url = _safe_next(request.session.pop("next", "/"))
    logger.info("Auth: login — %s (%s)", user["name"], user["email"])
    return RedirectResponse(url=next_url)


@router.get("/auth/logout")
async def logout(request: Request):
    user = request.session.get("user", {})
    logger.info("Auth: logout — %s", user.get("email", "unknown"))
    request.session.clear()
    return RedirectResponse(url="/auth/login" if auth_active() else "/")


@router.get("/auth/forbidden")
async def forbidden(request: Request):
    denied  = request.session.pop("denied", {})
    email   = denied.get("email", "unknown")
    groups  = ", ".join(denied.get("groups", [])) or "none"
    allowed = ", ".join(sorted(ALLOWED_GROUPS)) if ALLOWED_GROUPS else "—"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Access Denied — PVC Monitor</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           background: #0d1117; color: #e6edf3; display: flex; align-items: center;
           justify-content: center; min-height: 100vh; margin: 0; }}
    .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
             padding: 40px; max-width: 480px; width: 100%; text-align: center; }}
    h1 {{ color: #f85149; margin: 0 0 8px; font-size: 22px; }}
    p  {{ color: #8b949e; margin: 8px 0; font-size: 14px; line-height: 1.6; }}
    .tag {{ display: inline-block; background: #21262d; border: 1px solid #30363d;
            border-radius: 4px; padding: 2px 8px; font-family: monospace; font-size: 12px;
            color: #e6edf3; }}
    a  {{ display: inline-block; margin-top: 24px; padding: 8px 20px; background: #21262d;
          border: 1px solid #30363d; border-radius: 6px; color: #8b949e;
          text-decoration: none; font-size: 13px; transition: color .15s, border-color .15s; }}
    a:hover {{ color: #e6edf3; border-color: #8b949e; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>&#x26D4; Access Denied</h1>
    <p>Signed in as <span class="tag">{email}</span></p>
    <p>You are not a member of any group that is allowed to access this application.</p>
    <p style="margin-top:20px">
      Your groups: <span class="tag">{groups}</span><br><br>
      Allowed groups: <span class="tag">{allowed}</span>
    </p>
    <a href="/auth/logout">Sign out and try a different account</a>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=403)


@router.get("/auth/user")
async def get_user(request: Request):
    if not auth_active():
        return {"authenticated": False, "auth_enabled": False}
    user = request.session.get("user")
    if not user:
        return JSONResponse({"authenticated": False, "auth_enabled": True})
    return {"authenticated": True, "auth_enabled": True, **user}
