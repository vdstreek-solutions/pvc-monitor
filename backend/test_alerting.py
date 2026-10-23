"""
Tests for alerting.py — run from the backend/ directory:
    pip install pytest
    pytest test_alerting.py -v
"""
import os
import smtplib
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, call
import alerting


def html_body(msg) -> str:
    """Decode the HTML part of a MIMEMultipart message."""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            return payload.decode("utf-8") if payload else ""
    return ""


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_snap(pvc_name: str, days_old: int) -> dict:
    created = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    return {
        "pvc_name":     pvc_name,
        "pv_name":      f"pv-{pvc_name}",
        "snapshot_id":  f"snap-{pvc_name}-001",
        "description":  "",
        "created":      created,
        "datastore":    "datastore1",
        "storage_class": "vsphere-sc",
        "volume_handle": f"uuid-{pvc_name}",
    }

SMTP_ENV = {
    "SMTP_HOST":               "smtp.example.com",
    "SMTP_PORT":               "587",
    "SMTP_USE_TLS":            "true",
    "SMTP_FROM":               "monitor@example.com",
    "SMTP_USER":               "",
    "SMTP_PASSWORD":           "",
    "ALERT_EMAIL_TO":          "ops@example.com",
    "ALERT_SNAPSHOT_AGE_DAYS": "7",
    "ALERT_CHECK_INTERVAL_HOURS": "24",
}

@pytest.fixture(autouse=True)
def reset_state():
    """Reset module-level state between tests."""
    alerting._state.update(last_check=None, last_alert=None, last_count=None, last_error=None)
    yield


# ── is_configured ─────────────────────────────────────────────────────────────

class TestIsConfigured:
    def test_all_required_set(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False):
            assert alerting.is_configured() is True

    def test_missing_smtp_host(self):
        env = {**SMTP_ENV, "SMTP_HOST": ""}
        with patch.dict(os.environ, env, clear=False):
            assert alerting.is_configured() is False

    def test_missing_smtp_from(self):
        env = {**SMTP_ENV, "SMTP_FROM": ""}
        with patch.dict(os.environ, env, clear=False):
            assert alerting.is_configured() is False

    def test_missing_alert_to(self):
        env = {**SMTP_ENV, "ALERT_EMAIL_TO": ""}
        with patch.dict(os.environ, env, clear=False):
            assert alerting.is_configured() is False

    def test_multiple_recipients(self):
        env = {**SMTP_ENV, "ALERT_EMAIL_TO": "a@b.com, c@d.com"}
        with patch.dict(os.environ, env, clear=False):
            cfg = alerting.get_config()
            assert cfg["alert_to"] == ["a@b.com", "c@d.com"]


# ── _compose_email ────────────────────────────────────────────────────────────

class TestComposeEmail:
    def _cfg(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False):
            return alerting.get_config()

    def test_subject_with_old_snaps(self):
        snaps = [make_snap("my-pvc", 10)]
        msg = alerting._compose_email(self._cfg(), snaps)
        assert "1 snapshot older than 7 days" in msg["Subject"]

    def test_subject_plural(self):
        snaps = [make_snap("pvc1", 10), make_snap("pvc2", 15)]
        msg = alerting._compose_email(self._cfg(), snaps)
        assert "2 snapshots older than 7 days" in msg["Subject"]

    def test_subject_test_no_snaps(self):
        msg = alerting._compose_email(self._cfg(), [], test=True)
        assert "Test alert" in msg["Subject"]
        assert "no old snapshots" in msg["Subject"].lower()

    def test_body_contains_pvc_name(self):
        snaps = [make_snap("important-pvc", 20)]
        msg = alerting._compose_email(self._cfg(), snaps)
        assert "important-pvc" in html_body(msg)

    def test_body_contains_age(self):
        snaps = [make_snap("pvc1", 12)]
        msg = alerting._compose_email(self._cfg(), snaps)
        assert "12d" in html_body(msg)

    def test_recipients_set(self):
        snaps = [make_snap("pvc1", 10)]
        msg = alerting._compose_email(self._cfg(), snaps)
        assert "ops@example.com" in msg["To"]

    def test_from_set(self):
        snaps = [make_snap("pvc1", 10)]
        msg = alerting._compose_email(self._cfg(), snaps)
        assert "monitor@example.com" in msg["From"]


# ── _send_email ───────────────────────────────────────────────────────────────

class TestSendEmail:
    def _cfg(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False):
            return alerting.get_config()

    def _msg(self):
        return alerting._compose_email(self._cfg(), [make_snap("pvc1", 10)])

    def test_starttls_path(self):
        cfg = {**self._cfg(), "smtp_port": 587, "smtp_use_tls": True}
        mock_server = MagicMock()
        with patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__ = lambda s: mock_server
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            alerting._send_email(cfg, self._msg())
        mock_server.starttls.assert_called_once()

    def test_ssl_path_uses_smtp_ssl(self):
        cfg = {**self._cfg(), "smtp_port": 465}
        mock_server = MagicMock()
        with patch("smtplib.SMTP_SSL") as mock_ssl, patch("smtplib.SMTP") as mock_plain:
            mock_ssl.return_value.__enter__ = lambda s: mock_server
            mock_ssl.return_value.__exit__ = MagicMock(return_value=False)
            alerting._send_email(cfg, self._msg())
        mock_ssl.assert_called_once()
        mock_plain.assert_not_called()

    def test_login_called_when_credentials_set(self):
        cfg = {**self._cfg(), "smtp_port": 587, "smtp_use_tls": True, "smtp_user": "user@example.com"}
        mock_server = MagicMock()
        with patch("smtplib.SMTP") as mock_smtp, \
             patch.dict(os.environ, {"SMTP_PASSWORD": "secret"}, clear=False):
            mock_smtp.return_value.__enter__ = lambda s: mock_server
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            alerting._send_email(cfg, self._msg())
        mock_server.login.assert_called_once_with("user@example.com", "secret")

    def test_no_login_when_no_credentials(self):
        cfg = {**self._cfg(), "smtp_port": 587, "smtp_use_tls": True, "smtp_user": ""}
        mock_server = MagicMock()
        with patch("smtplib.SMTP") as mock_smtp, \
             patch.dict(os.environ, {"SMTP_PASSWORD": ""}, clear=False):
            mock_smtp.return_value.__enter__ = lambda s: mock_server
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            alerting._send_email(cfg, self._msg())
        mock_server.login.assert_not_called()


# ── check_and_alert ───────────────────────────────────────────────────────────

class TestCheckAndAlert:
    def test_returns_not_configured_when_smtp_missing(self):
        env = {**SMTP_ENV, "SMTP_HOST": ""}
        with patch.dict(os.environ, env, clear=False):
            result = alerting.check_and_alert()
        assert result["sent"] is False
        assert result["reason"] == "not_configured"

    def test_state_last_error_set_when_not_configured(self):
        env = {**SMTP_ENV, "SMTP_HOST": ""}
        with patch.dict(os.environ, env, clear=False):
            alerting.check_and_alert()
        assert alerting._state["last_error"] == "SMTP not configured"

    def test_no_old_snaps_does_not_send(self):
        snaps = [make_snap("pvc1", 3)]  # 3 days — under 7-day threshold
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snaps), \
             patch("alerting._send_email") as mock_send:
            result = alerting.check_and_alert()
        assert result["sent"] is False
        assert result["reason"] == "no_old_snapshots"
        mock_send.assert_not_called()

    def test_old_snaps_triggers_email(self):
        snaps = [make_snap("pvc1", 10)]  # 10 days — over threshold
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snaps), \
             patch("alerting._send_email") as mock_send:
            result = alerting.check_and_alert()
        assert result["sent"] is True
        assert result["count"] == 1
        assert "ops@example.com" in result["recipients"]
        mock_send.assert_called_once()

    def test_only_old_snaps_included(self):
        snaps = [make_snap("old-pvc", 10), make_snap("new-pvc", 2)]
        captured = {}
        def capture_send(cfg, msg):
            captured["msg"] = msg
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snaps), \
             patch("alerting._send_email", side_effect=capture_send):
            result = alerting.check_and_alert()
        assert result["count"] == 1
        body = html_body(captured["msg"])
        assert "old-pvc" in body
        assert "new-pvc" not in body

    def test_force_sends_even_with_no_old_snaps(self):
        snaps = [make_snap("pvc1", 2)]  # under threshold
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snaps), \
             patch("alerting._send_email") as mock_send:
            result = alerting.check_and_alert(force=True)
        assert result["sent"] is True
        mock_send.assert_called_once()

    def test_force_with_no_snaps_at_all_sends_test_email(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=[]), \
             patch("alerting._send_email") as mock_send:
            result = alerting.check_and_alert(force=True)
        assert result["sent"] is True
        mock_send.assert_called_once()

    def test_smtp_error_captured(self):
        snaps = [make_snap("pvc1", 10)]
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snaps), \
             patch("alerting._send_email", side_effect=smtplib.SMTPException("Connection refused")):
            result = alerting.check_and_alert()
        assert result["sent"] is False
        assert result["reason"] == "smtp_error"
        assert "Connection refused" in result["error"]
        assert alerting._state["last_error"] == "Connection refused"

    def test_fetch_error_captured(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", side_effect=RuntimeError("vCenter unavailable")):
            result = alerting.check_and_alert()
        assert result["sent"] is False
        assert result["reason"] == "fetch_error"
        assert "vCenter unavailable" in result["error"]

    def test_last_check_updated(self):
        assert alerting._state["last_check"] is None
        snaps = [make_snap("pvc1", 2)]
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snaps), \
             patch("alerting._send_email"):
            alerting.check_and_alert()
        assert alerting._state["last_check"] is not None

    def test_last_alert_updated_on_send(self):
        snaps = [make_snap("pvc1", 10)]
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snaps), \
             patch("alerting._send_email"):
            alerting.check_and_alert()
        assert alerting._state["last_alert"] is not None

    def test_last_alert_not_updated_when_no_send(self):
        snaps = [make_snap("pvc1", 2)]
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snaps), \
             patch("alerting._send_email"):
            alerting.check_and_alert()
        assert alerting._state["last_alert"] is None

    def test_custom_threshold(self):
        env = {**SMTP_ENV, "ALERT_SNAPSHOT_AGE_DAYS": "14"}
        snaps = [make_snap("pvc1", 10)]  # 10 days — under 14-day threshold
        with patch.dict(os.environ, env, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snaps), \
             patch("alerting._send_email") as mock_send:
            result = alerting.check_and_alert()
        assert result["sent"] is False
        mock_send.assert_not_called()


# ── get_status ────────────────────────────────────────────────────────────────

class TestGetStatus:
    def test_status_when_configured(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False):
            status = alerting.get_status()
        assert status["configured"] is True
        assert status["age_threshold_days"] == 7
        assert status["check_interval_hours"] == 24.0
        assert "ops@example.com" in status["alert_to"]

    def test_status_when_not_configured(self):
        env = {**SMTP_ENV, "SMTP_HOST": ""}
        with patch.dict(os.environ, env, clear=False):
            status = alerting.get_status()
        assert status["configured"] is False

    def test_status_reflects_state(self):
        snaps = [make_snap("pvc1", 10)]
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snaps), \
             patch("alerting._send_email"):
            alerting.check_and_alert()
            status = alerting.get_status()
        assert status["last_check"] is not None
        assert status["last_alert"] is not None
        assert status["last_old_snap_count"] == 1
