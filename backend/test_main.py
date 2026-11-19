"""Tests for the app wiring in main.py: routes, headers, errors, POST guards, threading."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

import alerting
import auth
import main
import vcenter_client
from settings import Unavailable


@pytest.fixture
def open_client(monkeypatch):
    """Client for an app with login disabled, so routes can be reached directly."""
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    return TestClient(main.app, base_url="https://testserver", follow_redirects=False)


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_framework_pages_disabled(open_client, path):
    assert open_client.get(path).status_code == 404


def test_debug_endpoint_off_by_default(open_client):
    assert open_client.get("/api/debug/vsphere").status_code == 404


def test_debug_endpoint_when_enabled(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    monkeypatch.setattr(main, "ENABLE_DEBUG_ENDPOINT", True)
    monkeypatch.setattr(vcenter_client, "debug_vsphere_inventory", lambda: {"ok": True})
    client = TestClient(main.create_app())
    assert client.get("/api/debug/vsphere").json() == {"ok": True}


def test_no_cors_headers(open_client):
    response = open_client.get("/healthz", headers={"Origin": "https://evil.test"})
    assert "access-control-allow-origin" not in response.headers
    preflight = open_client.options(
        "/api/alerts/test",
        headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in preflight.headers


def test_security_headers(open_client):
    headers = open_client.get("/healthz").headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"


def test_security_headers_on_unauthenticated_responses():
    client = TestClient(main.app, base_url="https://testserver")
    assert "content-security-policy" in client.get("/api/overview").headers


def test_unexpected_error_text_not_returned(open_client, monkeypatch, caplog):
    def boom():
        raise Exception("connect to secret-host.internal:443 failed")

    monkeypatch.setattr(vcenter_client, "get_orphaned_fcds", boom)
    response = open_client.get("/api/orphaned-fcds")
    assert response.status_code == 500
    assert "secret-host" not in response.text
    assert "secret-host" in caplog.text


def test_unavailable_returns_503_with_own_text(open_client, monkeypatch):
    def unavailable():
        raise Unavailable("Kubernetes is not configured")

    monkeypatch.setattr(vcenter_client, "get_container_volumes", unavailable)
    response = open_client.get("/api/container-volumes")
    assert response.status_code == 503
    assert response.json()["detail"] == "Kubernetes is not configured"


# ── POST alert routes ────────────────────────────────────────────────────────

@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake(force=False):
        calls.append(force)
        return {"sent": True, "count": 0, "recipients": []}

    monkeypatch.setattr(alerting, "check_and_alert", fake)
    monkeypatch.setattr(alerting, "check_and_alert_orphans", fake)
    return calls


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Origin": "https://evil.test"},
])
def test_cross_site_post_refused(open_client, sent, headers):
    assert open_client.post("/api/alerts/test", headers=headers).status_code == 403
    assert sent == []


def test_same_origin_post_allowed_then_rate_limited(open_client, sent):
    headers = {"Sec-Fetch-Site": "same-origin", "Origin": "https://testserver"}
    assert open_client.post("/api/alerts/test", headers=headers).status_code == 200
    assert open_client.post("/api/alerts/test", headers=headers).status_code == 429
    # The other alert kind has its own interval.
    assert open_client.post("/api/alerts/orphan-test", headers=headers).status_code == 200
    assert sent == [True, True]


def test_smtp_error_reported_without_detail(open_client, monkeypatch):
    monkeypatch.setattr(alerting, "check_and_alert", lambda force=False: {
        "sent": False, "reason": "smtp_error", "error": alerting._SMTP_ERROR, "count": 0,
    })
    response = open_client.post("/api/alerts/test")
    assert response.status_code == 502
    assert response.json()["detail"] == alerting._SMTP_ERROR


# ── Trusted hosts ────────────────────────────────────────────────────────────

def test_trusted_hosts(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    monkeypatch.setattr(main, "TRUSTED_HOSTS", ["app.test"])
    client = TestClient(main.create_app())
    assert client.get("/api/alerts/status", headers={"Host": "evil.test"}).status_code == 400
    assert client.get("/api/alerts/status", headers={"Host": "app.test"}).status_code == 200
    # Probes use the pod IP as Host.
    assert client.get("/healthz", headers={"Host": "10.0.0.7:8000"}).status_code == 200


# ── Blocking calls stay off the event loop ───────────────────────────────────

def test_healthz_answers_during_a_slow_scan(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)

    def slow_scan():
        time.sleep(1.5)
        return []

    monkeypatch.setattr(vcenter_client, "get_orphaned_fcds", slow_scan)
    with TestClient(main.app) as client:
        worker = threading.Thread(target=client.get, args=("/api/orphaned-fcds",))
        worker.start()
        time.sleep(0.2)
        started = time.monotonic()
        assert client.get("/healthz").status_code == 200
        elapsed = time.monotonic() - started
        worker.join()
    assert elapsed < 0.5
