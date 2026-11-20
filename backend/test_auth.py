"""Tests for start-up validation, the auth middleware, the session and the 403 page."""
import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import auth
import main
from settings import ConfigError

BACKEND = Path(__file__).resolve().parent


class FakeDex:
    """Stands in for the authlib client: returns the given ID token claims."""

    def __init__(self, userinfo=None, error=None):
        self.userinfo = userinfo
        self.error = error
        self.kwargs = None

    async def authorize_access_token(self, request, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return {"access_token": "a", "userinfo": self.userinfo}


@pytest.fixture
def client():
    return TestClient(main.app, base_url="https://testserver", follow_redirects=False)


def sign_in(client, monkeypatch, userinfo):
    dex = FakeDex(userinfo)
    monkeypatch.setattr(auth, "oauth", SimpleNamespace(dex=dex))
    return client.get("/auth/callback"), dex


ALLOWED_USER = {"sub": "1", "email": "user@example.test", "name": "User", "groups": ["ops", "other"]}


# ── Start-up configuration ───────────────────────────────────────────────────

class TestValidateSettings:
    def test_valid_settings_pass(self):
        auth.validate_settings()

    @pytest.mark.parametrize("name", ["OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "SESSION_SECRET",
                                      "ALLOWED_GROUPS"])
    def test_missing_setting_refuses_start(self, monkeypatch, name):
        monkeypatch.setattr(auth, name, set() if name == "ALLOWED_GROUPS" else "")
        with pytest.raises(ConfigError, match=name):
            auth.validate_settings()

    def test_any_user_wildcard_warns(self, monkeypatch, caplog):
        monkeypatch.setattr(auth, "ALLOWED_GROUPS", {"*"})
        monkeypatch.setattr(auth, "ALLOW_ANY_USER", True)
        with caplog.at_level(logging.WARNING):
            auth.validate_settings()
        assert "ALLOWED_GROUPS=*" in caplog.text

    def test_wildcard_cannot_be_mixed_with_groups(self, monkeypatch):
        monkeypatch.setattr(auth, "ALLOWED_GROUPS", {"*", "ops"})
        with pytest.raises(ConfigError, match="cannot be combined"):
            auth.validate_settings()

    def test_http_issuer_refused(self, monkeypatch):
        monkeypatch.setattr(auth, "OIDC_ISSUER", "http://issuer.test")
        with pytest.raises(ConfigError, match="https"):
            auth.validate_settings()

    def test_http_issuer_on_localhost_allowed(self, monkeypatch):
        monkeypatch.setattr(auth, "OIDC_ISSUER", "http://localhost:5556/dex")
        auth.validate_settings()

    @pytest.mark.parametrize("secret", ["short", "change-me-generate-with-openssl-rand-hex-32"])
    def test_weak_session_secret_refused(self, monkeypatch, secret):
        monkeypatch.setattr(auth, "SESSION_SECRET", secret)
        with pytest.raises(ConfigError, match="SESSION_SECRET"):
            auth.validate_settings()

    def test_auth_disabled_needs_nothing_and_warns(self, monkeypatch, caplog):
        monkeypatch.setattr(auth, "AUTH_ENABLED", False)
        monkeypatch.setattr(auth, "OIDC_ISSUER", "")
        monkeypatch.setattr(auth, "SESSION_SECRET", "")
        with caplog.at_level(logging.WARNING):
            auth.validate_settings()
        assert "AUTH_ENABLED=false" in caplog.text


def _run_main_import(env_overrides: dict, cwd: Path, code: str = "import main") -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "SYSTEMROOT")}
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=60,
    )


def test_start_refused_when_auth_enabled_without_issuer(tmp_path):
    result = _run_main_import(
        {"PYTHONPATH": str(BACKEND), "AUTH_ENABLED": "true", "OIDC_ISSUER": "",
         "OIDC_CLIENT_ID": "x", "OIDC_CLIENT_SECRET": "x", "SESSION_SECRET": "s" * 40},
        cwd=tmp_path,
    )
    assert result.returncode != 0
    assert "Configuration error" in result.stderr and "OIDC_ISSUER" in result.stderr


def test_start_refused_without_allowed_groups(tmp_path):
    result = _run_main_import(
        {"PYTHONPATH": str(BACKEND), "AUTH_ENABLED": "true", "OIDC_ISSUER": "https://issuer.test",
         "OIDC_CLIENT_ID": "x", "OIDC_CLIENT_SECRET": "x", "SESSION_SECRET": "s" * 40},
        cwd=tmp_path,
    )
    assert result.returncode != 0
    assert "Configuration error" in result.stderr and "ALLOWED_GROUPS" in result.stderr


def test_invalid_number_refused_at_start(tmp_path):
    result = _run_main_import(
        {"PYTHONPATH": str(BACKEND), "AUTH_ENABLED": "false", "ALERT_CHECK_INTERVAL_HOURS": "0.5"},
        cwd=tmp_path,
    )
    assert result.returncode != 0
    assert "ALERT_CHECK_INTERVAL_HOURS" in result.stderr


def test_settings_from_dotenv_reach_every_module(tmp_path):
    """load_dotenv() runs before auth.py and the clients read the environment."""
    for source in BACKEND.glob("*.py"):
        if not source.name.startswith(("test_", "conftest")):
            (tmp_path / source.name).write_text(source.read_text())
    (tmp_path / ".env").write_text(
        "AUTH_ENABLED=true\nOIDC_ISSUER=https://issuer.test\nOIDC_CLIENT_ID=c\n"
        "OIDC_CLIENT_SECRET=s\nSESSION_SECRET=" + "k" * 40 + "\nALLOWED_GROUPS=ops\n"
        "K8S_SERVER=https://k8s.test\nVCENTER_HOST=vc.test\n"
    )
    result = _run_main_import(
        {"PYTHONPATH": str(tmp_path)}, cwd=tmp_path,
        code="import main, auth, k8s_client, vcenter_client; "
             "print(auth.auth_active(), auth.OIDC_ISSUER, k8s_client.K8S_SERVER, vcenter_client.VCENTER_HOST)",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["True", "https://issuer.test", "https://k8s.test", "vc.test"]


# ── Middleware ───────────────────────────────────────────────────────────────

def _asgi_get(app, **scope_overrides) -> int:
    """Send one GET through the ASGI app with a hand-built scope; return the status."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 1234), "root_path": "", "path": "/api/alerts/status",
        "raw_path": b"/api/alerts/status", "query_string": b"",
        "headers": [(b"host", b"app.test")],
    }
    scope.update(scope_overrides)
    statuses = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    asyncio.run(app(scope, receive, send))
    return statuses[0]


class TestMiddleware:
    def test_api_requires_session(self, client):
        assert client.get("/api/alerts/status").status_code == 401

    def test_forged_scheme_does_not_skip_auth(self):
        # A scheme like this makes request.url.path read "/healthz".
        assert _asgi_get(main.app, scheme="http://x/healthz?") == 401
        assert _asgi_get(main.app, scheme="http://x/auth/?") == 401
        assert _asgi_get(main.app, headers=[(b"host", b"x/healthz")]) == 401

    @pytest.mark.parametrize("path", ["/healthzx", "/auth/../index.html", "/auth/other"])
    def test_public_paths_match_exactly(self, client, path):
        response = client.get(path)
        assert response.status_code == 307
        assert response.headers["location"].startswith("/auth/login?next=")

    def test_healthz_is_public(self, client):
        assert client.get("/healthz").status_code == 200

    def test_next_keeps_the_whole_query(self, client):
        response = client.get("/some/page?a=1&b=2")
        assert response.headers["location"] == "/auth/login?next=%2Fsome%2Fpage%3Fa%3D1%26b%3D2"

    @pytest.mark.parametrize("value", ["//evil.test", "/\\evil.test", "/a\r\nb", "https://evil.test"])
    def test_safe_next_rejects_offsite(self, value):
        assert auth._safe_next(value) == "/"


# ── Login, session lifetime, groups ──────────────────────────────────────────

class TestSession:
    def test_login_sets_host_prefixed_secure_cookie(self, client, monkeypatch):
        response, dex = sign_in(client, monkeypatch, ALLOWED_USER)
        assert response.status_code == 307
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("__Host-session=")
        assert "secure" in cookie.lower() and "path=/" in cookie.lower()
        assert client.get("/api/alerts/status").status_code == 200

    def test_claims_pinned_to_issuer_and_audience(self, client, monkeypatch):
        _, dex = sign_in(client, monkeypatch, ALLOWED_USER)
        options = dex.kwargs["claims_options"]
        assert options["iss"]["values"][0] == "https://issuer.test"
        assert options["aud"]["value"] == "pvc-monitor"

    def test_only_matched_groups_are_stored(self, client, monkeypatch):
        sign_in(client, monkeypatch, ALLOWED_USER)
        assert client.get("/auth/user").json()["groups"] == ["ops"]

    def test_session_expires_after_absolute_lifetime(self, client, monkeypatch):
        sign_in(client, monkeypatch, ALLOWED_USER)
        later = time.time() + 12 * 3600 + 5
        monkeypatch.setattr(auth.time, "time", lambda: later)
        assert client.get("/api/alerts/status").status_code == 401
        assert client.get("/auth/user").json()["authenticated"] is False

    def test_session_without_login_time_is_rejected(self, client, monkeypatch):
        request = SimpleNamespace(session={"user": {"email": "x"}})
        assert auth.current_user(request) is None
        assert request.session == {}

    def test_denied_login_ends_earlier_session(self, client, monkeypatch):
        sign_in(client, monkeypatch, ALLOWED_USER)
        sign_in(client, monkeypatch, {**ALLOWED_USER, "groups": ["other"]})
        assert client.get("/api/alerts/status").status_code == 401

    @pytest.mark.parametrize("groups", ["ops", None, [["ops"]], ["ops", 1], {"ops": True}])
    def test_groups_claim_must_be_list_of_strings(self, client, monkeypatch, groups):
        response, _ = sign_in(client, monkeypatch, {**ALLOWED_USER, "groups": groups})
        assert response.headers["location"] == "/auth/forbidden"

    def test_wildcard_admits_any_authenticated_user(self, client, monkeypatch):
        monkeypatch.setattr(auth, "ALLOWED_GROUPS", {"*"})
        monkeypatch.setattr(auth, "ALLOW_ANY_USER", True)
        response, _ = sign_in(client, monkeypatch, {**ALLOWED_USER, "groups": []})
        assert response.headers["location"] == "/"
        assert client.get("/api/alerts/status").status_code == 200

    def test_missing_id_token_is_refused(self, client, monkeypatch):
        response, _ = sign_in(client, monkeypatch, None)
        assert response.status_code == 400 and "location" not in response.headers
        assert client.get("/api/alerts/status").status_code == 401


class TestLogout:
    def test_logout_is_a_post_that_ends_the_session(self, client, monkeypatch):
        sign_in(client, monkeypatch, ALLOWED_USER)
        response = client.post("/auth/logout", headers={"Sec-Fetch-Site": "same-origin"})
        assert response.status_code == 303 and response.headers["location"] == "/auth/login"
        assert client.get("/api/alerts/status").status_code == 401

    def test_get_does_not_log_out(self, client, monkeypatch):
        sign_in(client, monkeypatch, ALLOWED_USER)
        assert client.get("/auth/logout").status_code == 405
        assert client.get("/api/alerts/status").status_code == 200

    @pytest.mark.parametrize("headers", [{"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.test"}])
    def test_cross_site_logout_refused(self, client, monkeypatch, headers):
        sign_in(client, monkeypatch, ALLOWED_USER)
        assert client.post("/auth/logout", headers=headers).status_code == 403
        assert client.get("/api/alerts/status").status_code == 200

    def test_forbidden_page_signs_out_with_a_form_post(self, client):
        text = client.get("/auth/forbidden").text
        assert '<form method="post" action="/auth/logout">' in text
        assert 'href="/auth/logout"' not in text


class TestSignInFailedPage:
    def test_failed_exchange_shows_page_with_retry_link(self, client, monkeypatch):
        monkeypatch.setattr(auth, "oauth", SimpleNamespace(dex=FakeDex(error=Exception("issuer mismatch"))))
        response = client.get("/auth/callback")
        assert response.status_code == 400
        assert "location" not in response.headers  # no redirect loop back to the login
        assert "Sign-in failed" in response.text
        assert 'href="/auth/login?next=%2F"' in response.text
        assert "issuer mismatch" not in response.text
        assert response.headers["content-security-policy"] == auth._FORBIDDEN_CSP

    def test_retry_link_keeps_next_and_is_escaped(self, client, monkeypatch):
        class Login:
            async def authorize_redirect(self, request, redirect_uri):
                from starlette.responses import RedirectResponse
                return RedirectResponse("https://issuer.test/auth")

            async def authorize_access_token(self, request, **kwargs):
                raise Exception("bad state")

        monkeypatch.setattr(auth, "oauth", SimpleNamespace(dex=Login()))
        client.get("/auth/login", params={"next": '/x?a="<b>&c=1'})
        response = client.get("/auth/callback")
        assert "<b>" not in response.text and '"<' not in response.text
        assert "/auth/login?next=%2Fx%3Fa%3D%22%3Cb%3E%26c%3D1" in response.text


# ── 403 page and log lines ───────────────────────────────────────────────────

class TestForbiddenPage:
    def test_claims_are_escaped(self, client, monkeypatch):
        sign_in(client, monkeypatch, {"sub": "t", "email": "a<i>@example.invalid", "groups": ["<i>g</i>"]})
        response = client.get("/auth/forbidden")
        assert response.status_code == 403
        assert "&lt;i&gt;g&lt;/i&gt;" in response.text
        assert "a&lt;i&gt;@example.invalid" in response.text
        assert "<i>" not in response.text

    def test_page_keeps_its_own_csp(self, client):
        csp = client.get("/auth/forbidden").headers["content-security-policy"]
        assert csp == auth._FORBIDDEN_CSP

    def test_allowed_groups_shown_only_right_after_denial(self, client, monkeypatch):
        sign_in(client, monkeypatch, {**ALLOWED_USER, "groups": ["other"]})
        assert "ops" in client.get("/auth/forbidden").text
        assert "ops" not in client.get("/auth/forbidden").text
        fresh = TestClient(main.app, base_url="https://testserver")
        assert "ops" not in fresh.get("/auth/forbidden").text


def test_untrusted_text_cannot_split_log_lines(client, monkeypatch, caplog):
    monkeypatch.setattr(auth, "oauth", SimpleNamespace(dex=FakeDex(error=Exception("b\nFAKE c"))))
    with caplog.at_level(logging.WARNING, logger="auth"):
        client.get("/auth/callback")
    messages = [r.getMessage() for r in caplog.records if r.name == "auth"]
    assert len(messages) == 1
    assert "\n" not in messages[0] and "b\\nFAKE c" in messages[0]


def test_pkce_enabled_in_authorization_url(monkeypatch):
    """authlib adds an S256 code challenge when the client is registered by init_oauth."""
    from authlib.integrations.starlette_client import OAuth

    registry = OAuth()
    monkeypatch.setattr(auth, "oauth", registry)
    auth.init_oauth()
    dex = registry.dex

    async def metadata():
        return {"authorization_endpoint": "https://issuer.test/auth", "issuer": "https://issuer.test"}

    monkeypatch.setattr(dex, "load_server_metadata", metadata)
    rv = asyncio.run(dex.create_authorization_url("https://app.test/auth/callback"))
    assert "code_challenge=" in rv["url"] and "code_challenge_method=S256" in rv["url"]
    assert rv["code_verifier"]


@pytest.mark.parametrize("claims, ok", [
    ({}, True),
    ({"aud": "other-client"}, False),
    ({"iss": "https://other-issuer.test"}, False),
])
def test_id_token_claims_pinned(claims, ok):
    """auth.claims_options() as authlib applies it to an ID token."""
    from joserfc.errors import InvalidClaimError
    from authlib.oidc.core import CodeIDToken

    now = int(time.time())
    payload = {"iss": "https://issuer.test", "sub": "1", "aud": "pvc-monitor",
               "exp": now + 300, "iat": now, "nonce": "n", **claims}
    token = CodeIDToken(payload, {"alg": "RS256"}, auth.claims_options(), {"nonce": "n", "client_id": "pvc-monitor"})
    if ok:
        token.validate()
    else:
        with pytest.raises(InvalidClaimError):
            token.validate()


# ── ID token validation through authlib ──────────────────────────────────────

class TestIdTokenValidation:
    """The registered authlib client validates a real signed ID token.

    Only the metadata download is replaced (the JWKS travels in the metadata,
    which authlib reads before any jwks_uri); signature, issuer, audience and
    nonce checks run in authlib and joserfc with auth.claims_options().
    """

    @pytest.fixture
    def idp(self, monkeypatch):
        from authlib.integrations.starlette_client import OAuth
        from joserfc import jwt
        from joserfc.jwk import RSAKey

        key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
        registry = OAuth()
        monkeypatch.setattr(auth, "oauth", registry)
        auth.init_oauth()
        dex = registry.dex
        dex.server_metadata.update({
            "issuer": "https://issuer.test",
            "authorization_endpoint": "https://issuer.test/auth",
            "token_endpoint": "https://issuer.test/token",
            "jwks": {"keys": [key.as_dict(private=False)]},
            "id_token_signing_alg_values_supported": ["RS256"],
            "_loaded_at": time.time(),  # authlib then skips the download
        })

        def validate(signing_key=key, **claims):
            now = int(time.time())
            payload = {"iss": "https://issuer.test", "sub": "1", "aud": "pvc-monitor",
                       "exp": now + 300, "iat": now, "nonce": "n-1", **claims}
            token = {"access_token": "a", "token_type": "Bearer",
                     "id_token": jwt.encode({"alg": "RS256", "kid": "k1"}, payload, signing_key)}
            return asyncio.run(dex.parse_id_token(token, nonce="n-1", claims_options=auth.claims_options()))

        return validate

    def test_valid_token_accepted(self, idp):
        assert idp()["sub"] == "1"

    def test_bad_signature_refused(self, idp):
        from joserfc.errors import BadSignatureError
        from joserfc.jwk import RSAKey

        other = RSAKey.generate_key(2048, parameters={"kid": "k1"})  # same kid, other key
        with pytest.raises(BadSignatureError):
            idp(signing_key=other)

    @pytest.mark.parametrize("claims", [
        {"iss": "https://other-issuer.test"},
        {"aud": "other-client"},
        {"nonce": "other-nonce"},
    ])
    def test_wrong_claim_refused(self, idp, claims):
        from joserfc.errors import InvalidClaimError
        with pytest.raises(InvalidClaimError):
            idp(**claims)
