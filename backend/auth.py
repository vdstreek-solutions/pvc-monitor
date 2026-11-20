import html
import logging
import os
import time
from urllib.parse import quote, urlsplit

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, JSONResponse, HTMLResponse

from settings import ConfigError, env_bool, env_int, env_list, env_str

logger = logging.getLogger(__name__)

AUTH_ENABLED       = env_bool("AUTH_ENABLED", True)
OIDC_ISSUER        = env_str("OIDC_ISSUER").rstrip("/")
OIDC_CLIENT_ID     = env_str("OIDC_CLIENT_ID")
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "")  # used as given
OIDC_REDIRECT_URI  = env_str("OIDC_REDIRECT_URI")
OIDC_SCOPES        = env_str("OIDC_SCOPES", "openid email profile groups")
ALLOWED_GROUPS     = set(env_list("ALLOWED_GROUPS"))
# ALLOWED_GROUPS=* lets every user the identity provider authenticates in.
ALLOW_ANY_USER     = ALLOWED_GROUPS == {"*"}

SESSION_SECRET        = os.getenv("SESSION_SECRET", "")
SESSION_SECURE        = env_bool("SESSION_SECURE", True)
SESSION_MAX_AGE_HOURS = env_int("SESSION_MAX_AGE_HOURS", 12, minimum=1)
# The __Host- prefix makes browsers reject the cookie unless it is Secure, has
# Path=/ and no Domain, so a sibling host cannot plant or overwrite it.
SESSION_COOKIE        = "__Host-session" if SESSION_SECURE else "session"

# Values that appear in documentation and must never be used as a real key.
_SECRET_PLACEHOLDERS = {"change-me-generate-with-openssl-rand-hex-32"}

# Paths served without a session. Matched exactly, never by prefix.
_PUBLIC_PATHS = {
    "/healthz",
    "/auth/login",
    "/auth/callback",
    "/auth/logout",
    "/auth/forbidden",
    "/auth/user",
}

# Cap on the number of claimed groups kept for the 403 page (cookie size).
_MAX_DENIED_GROUPS = 20

oauth = OAuth()


def validate_settings() -> None:
    """Refuse to start when authentication is enabled but not fully configured."""
    if not AUTH_ENABLED:
        logger.warning("Auth: AUTH_ENABLED=false, every route is served without login")
        return
    missing = [name for name, value in (
        ("OIDC_ISSUER", OIDC_ISSUER),
        ("OIDC_CLIENT_ID", OIDC_CLIENT_ID),
        ("OIDC_CLIENT_SECRET", OIDC_CLIENT_SECRET),
        ("SESSION_SECRET", SESSION_SECRET),
        ("ALLOWED_GROUPS", ALLOWED_GROUPS),
    ) if not value]
    if missing:
        raise ConfigError(
            f"AUTH_ENABLED is true but {', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set. "
            "Set AUTH_ENABLED=false to run without login (local development only)."
            + (" ALLOWED_GROUPS takes group names, or * for every authenticated user."
               if "ALLOWED_GROUPS" in missing else "")
        )
    if "*" in ALLOWED_GROUPS and not ALLOW_ANY_USER:
        raise ConfigError("ALLOWED_GROUPS=* cannot be combined with group names")
    if ALLOW_ANY_USER:
        logger.warning("Auth: ALLOWED_GROUPS=*, every user the identity provider authenticates is allowed in")
    issuer = urlsplit(OIDC_ISSUER)
    local = issuer.hostname in ("localhost", "127.0.0.1", "::1")
    if issuer.scheme != "https" and not (issuer.scheme == "http" and local):
        raise ConfigError("OIDC_ISSUER must be an https:// URL (http:// is accepted only for localhost)")
    if len(SESSION_SECRET) < 32 or SESSION_SECRET in _SECRET_PLACEHOLDERS:
        raise ConfigError(
            "SESSION_SECRET must be at least 32 characters and not the example value; "
            "generate one with: openssl rand -hex 32"
        )


def init_oauth() -> None:
    if not AUTH_ENABLED:
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
            "code_challenge_method": "S256",  # PKCE
        },
    )
    logger.info("Auth: OIDC ready, issuer=%s allowed_groups=%s", OIDC_ISSUER, sorted(ALLOWED_GROUPS))


def auth_active() -> bool:
    return AUTH_ENABLED


def claims_options() -> dict:
    """Pin the ID token issuer and audience to the configured values."""
    return {
        "iss": {"essential": True, "values": [OIDC_ISSUER, OIDC_ISSUER + "/"]},
        "aud": {"essential": True, "value": OIDC_CLIENT_ID},
    }


def current_user(request: Request) -> dict | None:
    """Signed-in user, or None (and a cleared session) once the login is too old."""
    user = request.session.get("user")
    if not user:
        return None
    login_at = request.session.get("login_at")
    now = time.time()
    if (
        not isinstance(login_at, (int, float))
        or now - login_at > SESSION_MAX_AGE_HOURS * 3600
        or login_at > now + 60
    ):
        request.session.clear()
        return None
    return user


def _string_list(value) -> list[str]:
    """The groups claim as a list of strings; anything else counts as no groups."""
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return []


def _safe_next(url: str) -> str:
    """Only allow relative paths, which prevents an open redirect."""
    if (
        url
        and url.startswith("/")
        and not url.startswith("//")
        and "\\" not in url
        and not any(ord(ch) < 32 or ord(ch) == 127 for ch in url)
    ):
        return url
    return "/"


# ── Middleware ───────────────────────────────────────────────────────────────

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not auth_active():
            return await call_next(request)

        # scope["path"] is the value the router matches on; request.url.path is
        # rebuilt from the Host and scheme and can differ from it.
        path = request.scope["path"]
        if path in _PUBLIC_PATHS or current_user(request):
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        next_url = path
        query = request.scope.get("query_string", b"").decode("latin-1")
        if query:
            next_url += "?" + query
        return RedirectResponse(url="/auth/login?next=" + quote(next_url, safe=""))


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
    if not auth_active():
        return RedirectResponse(url="/")
    try:
        token = await oauth.dex.authorize_access_token(request, claims_options=claims_options())
    except Exception as exc:
        logger.warning("Auth: token exchange failed: %r", str(exc))
        return _sign_in_failed(request)

    # Only claims from a validated ID token are trusted.
    userinfo = token.get("userinfo")
    if not userinfo:
        logger.warning("Auth: token response has no validated ID token")
        return _sign_in_failed(request)

    email = userinfo.get("email") or ""
    name = userinfo.get("name") or userinfo.get("preferred_username") or email
    groups = _string_list(userinfo.get("groups"))
    matched = sorted(set(groups) & ALLOWED_GROUPS)

    if not ALLOW_ANY_USER and not matched:
        logger.warning(
            "Auth: access denied, %r groups=%r not in allowed=%s",
            email, groups, sorted(ALLOWED_GROUPS),
        )
        # End any earlier session before recording the denial.
        request.session.clear()
        request.session["denied"] = {"email": email, "groups": groups[:_MAX_DENIED_GROUPS]}
        return RedirectResponse(url="/auth/forbidden")

    next_url = _safe_next(request.session.get("next", "/"))
    request.session.clear()
    request.session["user"] = {
        "sub":    userinfo.get("sub") or "",
        "email":  email,
        "name":   name,
        "groups": matched,  # only the groups that grant access
    }
    request.session["login_at"] = int(time.time())
    logger.info("Auth: login %r (%r)", name, email)
    return RedirectResponse(url=next_url)


def require_same_origin(request: Request) -> None:
    """Refuse a state-changing request that a browser sends from another site."""
    # Browsers send Sec-Fetch-Site; older ones at least send Origin on POST.
    # Non-browser clients send neither and cannot be driven cross-site.
    site = request.headers.get("sec-fetch-site")
    origin = request.headers.get("origin")
    if site is not None and site != "same-origin":
        raise HTTPException(status_code=403, detail="Cross-site request refused")
    if site is None and origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="Cross-site request refused")


@router.post("/auth/logout", dependencies=[Depends(require_same_origin)])
async def logout(request: Request):
    user = request.session.get("user") or {}
    logger.info("Auth: logout %r", user.get("email", "unknown"))
    request.session.clear()
    # 303 so the browser follows with a GET instead of repeating the POST.
    return RedirectResponse(url="/auth/login" if auth_active() else "/", status_code=303)


# The pages carry their own inline <style>, so they get their own policy.
# No form-action: the sign-out form's redirect chain ends at the identity provider.
_FORBIDDEN_CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"


def _page(title: str, heading: str, details: str, link_url: str, link_text: str, status_code: int,
          post: bool = False) -> HTMLResponse:
    """A small stand-alone page. title, heading and link_text are fixed text;
    details is markup the caller has escaped; link_url is escaped here.
    post=True renders the link as a button that POSTs to link_url."""
    if post:
        action = f'<form method="post" action="{html.escape(link_url)}"><button type="submit">{link_text}</button></form>'
    else:
        action = f'<a href="{html.escape(link_url)}">{link_text}</a>'
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{title} — PVC Monitor</title>
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
    a, button {{ display: inline-block; margin-top: 24px; padding: 8px 20px; background: #21262d;
          border: 1px solid #30363d; border-radius: 6px; color: #8b949e; font: inherit;
          text-decoration: none; font-size: 13px; cursor: pointer; transition: color .15s, border-color .15s; }}
    a:hover, button:hover {{ color: #e6edf3; border-color: #8b949e; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{heading}</h1>{details}
    {action}
  </div>
</body>
</html>"""
    return HTMLResponse(content=page, status_code=status_code, headers={"Content-Security-Policy": _FORBIDDEN_CSP})


def _sign_in_failed(request: Request) -> HTMLResponse:
    """Shown instead of redirecting to the login again, which can loop when the
    issuer is misconfigured. The retry link keeps the page the user asked for."""
    next_url = _safe_next(request.session.get("next", "/"))
    return _page(
        "Sign-in failed", "Sign-in failed",
        "\n    <p>The sign-in could not be completed. If this keeps happening, the identity "
        "provider settings may be wrong; the server log has the details.</p>",
        "/auth/login?next=" + quote(next_url, safe=""), "Try again", 400,
    )


@router.get("/auth/forbidden")
async def forbidden(request: Request):
    denied = request.session.pop("denied", None)
    if isinstance(denied, dict):
        email   = html.escape(str(denied.get("email") or "unknown"))
        groups  = html.escape(", ".join(_string_list(denied.get("groups"))) or "none")
        allowed = html.escape(", ".join(sorted(ALLOWED_GROUPS)) or "—")
        details = f"""
    <p>Signed in as <span class="tag">{email}</span></p>
    <p>You are not a member of any group that is allowed to access this application.</p>
    <p style="margin-top:20px">
      Your groups: <span class="tag">{groups}</span><br><br>
      Allowed groups: <span class="tag">{allowed}</span>
    </p>"""
    else:
        details = "\n    <p>You do not have access to this application.</p>"
    return _page("Access Denied", "&#x26D4; Access Denied", details,
                 "/auth/logout", "Sign out and try a different account", 403, post=True)


@router.get("/auth/user")
async def get_user(request: Request):
    if not auth_active():
        return {"authenticated": False, "auth_enabled": False}
    user = current_user(request)
    if not user:
        return JSONResponse({"authenticated": False, "auth_enabled": True})
    return {"authenticated": True, "auth_enabled": True, **user}
