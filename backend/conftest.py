"""Shared test set-up: a fixed configuration, no .env file, no network."""
import os
import socket

import dotenv
import pytest

# A developer's backend/.env must not configure the application under test.
dotenv.load_dotenv = lambda *args, **kwargs: False

_PREFIXES = (
    "VCENTER_", "K8S_", "KUBECONFIG", "SMTP_", "ALERT_", "OIDC_", "SESSION_",
    "AUTH_", "ALLOWED_GROUPS", "TRUSTED_HOSTS", "ENABLE_DEBUG",
)
for _name in list(os.environ):
    if _name.startswith(_PREFIXES):
        del os.environ[_name]

os.environ.update({
    "AUTH_ENABLED": "true",
    "OIDC_ISSUER": "https://issuer.test",
    "OIDC_CLIENT_ID": "pvc-monitor",
    "OIDC_CLIENT_SECRET": "test-client-secret",
    "SESSION_SECRET": "s" * 40,
    "ALLOWED_GROUPS": "ops",
})


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("network access is disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    import alerting
    import vcenter_client
    vcenter_client._scan_cache.clear()
    for state in (alerting._state, alerting._state_orphans):
        state.update(last_check=None, last_alert=None, last_count=None, last_error=None)
    import main
    main._last_alert_test.clear()
    yield
