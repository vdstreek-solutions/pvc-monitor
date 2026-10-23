import os
import smtplib
import asyncio
import logging
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


# ── Config ───────────────────────────────────────────────────────────────────

def get_config() -> dict:
    return {
        "smtp_host":            os.getenv("SMTP_HOST", ""),
        "smtp_port":            int(os.getenv("SMTP_PORT", "587")),
        "smtp_user":            os.getenv("SMTP_USER", ""),
        "smtp_use_tls":         os.getenv("SMTP_USE_TLS", "true").lower() != "false",
        "smtp_from":            os.getenv("SMTP_FROM", ""),
        "alert_to":             [a.strip() for a in os.getenv("ALERT_EMAIL_TO", "").split(",") if a.strip()],
        "age_threshold_days":   int(os.getenv("ALERT_SNAPSHOT_AGE_DAYS", "7")),
        "check_interval_hours": float(os.getenv("ALERT_CHECK_INTERVAL_HOURS", "24")),
    }

def is_configured(cfg: dict | None = None) -> bool:
    cfg = cfg or get_config()
    return bool(cfg["smtp_host"] and cfg["alert_to"] and cfg["smtp_from"])


# ── In-memory state ──────────────────────────────────────────────────────────

_state: dict = {
    "last_check": None,  # datetime
    "last_alert": None,  # datetime
    "last_count": None,  # int
    "last_error": None,  # str
}

_state_orphans: dict = {
    "last_check": None,
    "last_alert": None,
    "last_count": None,
    "last_error": None,
}

def get_status() -> dict:
    cfg = get_config()
    return {
        # Snapshot alert fields (unchanged — Snapshots.jsx reads these top-level)
        "configured":           is_configured(cfg),
        "last_check":           _state["last_check"].isoformat() if _state["last_check"] else None,
        "last_alert":           _state["last_alert"].isoformat() if _state["last_alert"] else None,
        "last_old_snap_count":  _state["last_count"],
        "last_error":           _state["last_error"],
        "age_threshold_days":   cfg["age_threshold_days"],
        "check_interval_hours": cfg["check_interval_hours"],
        "alert_to":             cfg["alert_to"],
        # Orphan FCD alert state (nested so it doesn't clash with snapshot fields)
        "orphans": {
            "last_check": _state_orphans["last_check"].isoformat() if _state_orphans["last_check"] else None,
            "last_alert": _state_orphans["last_alert"].isoformat() if _state_orphans["last_alert"] else None,
            "last_count": _state_orphans["last_count"],
            "last_error": _state_orphans["last_error"],
        },
    }


# ── Email composition ────────────────────────────────────────────────────────

def _age_days(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days
    except Exception:
        return None


def _compose_email(cfg: dict, old_snaps: list[dict], test: bool = False) -> MIMEMultipart:
    threshold = cfg["age_threshold_days"]
    count = len(old_snaps)

    if test and count == 0:
        subject = "[PVC Monitor] Test alert — no old snapshots currently"
        intro = f"This is a test email from PVC Monitor. There are currently <strong>no snapshots older than {threshold} days</strong>."
    else:
        subject = f"[PVC Monitor] {count} snapshot{'s' if count != 1 else ''} older than {threshold} days"
        intro = (
            f"<strong>{count} FCD snapshot{'s' if count != 1 else ''}</strong> "
            f"{'are' if count != 1 else 'is'} older than <strong>{threshold} days</strong> "
            f"and may need to be cleaned up."
        )

    rows = ""
    for s in sorted(old_snaps, key=lambda x: x.get("created") or ""):
        age = _age_days(s.get("created"))
        age_str = f"{age}d" if age is not None else "?"
        age_color = "#c0392b" if (age or 0) > 30 else "#e67e22" if (age or 0) > 7 else "#333"
        created = s.get("created") or "—"
        created_short = created[:10] if len(created) >= 10 else created
        rows += f"""
        <tr>
          <td style="padding:7px 12px;border-bottom:1px solid #eee">{s.get('pvc_name') or '—'}</td>
          <td style="padding:7px 12px;border-bottom:1px solid #eee;font-size:12px;font-family:monospace">{s.get('pv_name') or '—'}</td>
          <td style="padding:7px 12px;border-bottom:1px solid #eee;font-size:11px;font-family:monospace">{s.get('snapshot_id') or '—'}</td>
          <td style="padding:7px 12px;border-bottom:1px solid #eee">{s.get('datastore') or '—'}</td>
          <td style="padding:7px 12px;border-bottom:1px solid #eee">{s.get('storage_class') or '—'}</td>
          <td style="padding:7px 12px;border-bottom:1px solid #eee">{created_short}</td>
          <td style="padding:7px 12px;border-bottom:1px solid #eee;font-weight:700;color:{age_color}">{age_str}</td>
        </tr>"""

    table_section = ""
    if rows:
        table_section = f"""
  <table style="width:100%;border-collapse:collapse;margin-top:20px;font-size:13px">
    <thead>
      <tr style="background:#f5f5f5;text-align:left">
        <th style="padding:9px 12px;border-bottom:2px solid #ddd">PVC Name</th>
        <th style="padding:9px 12px;border-bottom:2px solid #ddd">PV Name</th>
        <th style="padding:9px 12px;border-bottom:2px solid #ddd">Snapshot ID</th>
        <th style="padding:9px 12px;border-bottom:2px solid #ddd">Datastore</th>
        <th style="padding:9px 12px;border-bottom:2px solid #ddd">Storage Class</th>
        <th style="padding:9px 12px;border-bottom:2px solid #ddd">Created</th>
        <th style="padding:9px 12px;border-bottom:2px solid #ddd">Age</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>"""

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;color:#333;max-width:960px;margin:0 auto;padding:24px;background:#fafafa">
  <div style="background:#fff;border-radius:8px;padding:28px;border:1px solid #e0e0e0">
    <h2 style="margin:0 0 6px;color:{'#888' if test and count == 0 else '#c0392b'}">
      {'⚙ Test Alert' if test and count == 0 else '⚠ Snapshot Age Alert'}
    </h2>
    <p style="margin:0 0 16px;color:#555">{intro}</p>
    {table_section}
    <hr style="border:none;border-top:1px solid #eee;margin:24px 0">
    <p style="margin:0;font-size:11px;color:#aaa">
      Sent by PVC Monitor &nbsp;·&nbsp; threshold: {threshold} days
      &nbsp;·&nbsp; {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    </p>
  </div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["smtp_from"]
    msg["To"]      = ", ".join(cfg["alert_to"])
    msg.attach(MIMEText(html, "html"))
    return msg


# ── SMTP delivery ────────────────────────────────────────────────────────────

def _send_email(cfg: dict, msg: MIMEMultipart) -> None:
    host     = cfg["smtp_host"]
    port     = cfg["smtp_port"]
    user     = cfg["smtp_user"]
    password = os.getenv("SMTP_PASSWORD", "")
    use_tls  = cfg["smtp_use_tls"]

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=15) as server:
            if user and password:
                server.login(user, password)
            server.sendmail(cfg["smtp_from"], cfg["alert_to"], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.ehlo()
            if use_tls:
                server.starttls()
                server.ehlo()
            if user and password:
                server.login(user, password)
            server.sendmail(cfg["smtp_from"], cfg["alert_to"], msg.as_string())


# ── Check and alert ──────────────────────────────────────────────────────────

def check_and_alert(force: bool = False) -> dict:
    """
    Fetch FCD snapshots, filter those older than the threshold, send email.
    force=True always sends an email (used for test-alert endpoint).
    """
    import vcenter_client  # late import — avoids circular dependency at module load

    cfg = get_config()
    _state["last_check"] = datetime.now(timezone.utc)
    _state["last_error"] = None

    if not is_configured(cfg):
        _state["last_error"] = "SMTP not configured"
        return {"sent": False, "reason": "not_configured", "count": 0}

    try:
        snapshots = vcenter_client.get_snapshots()
    except Exception as exc:
        _state["last_error"] = str(exc)
        return {"sent": False, "reason": "fetch_error", "error": str(exc), "count": 0}

    threshold = cfg["age_threshold_days"]
    old_snaps = [
        s for s in snapshots
        if (_age_days(s.get("created")) or 0) >= threshold
    ]
    _state["last_count"] = len(old_snaps)

    if not old_snaps and not force:
        return {"sent": False, "reason": "no_old_snapshots", "count": 0}

    try:
        msg = _compose_email(cfg, old_snaps, test=force and not old_snaps)
        _send_email(cfg, msg)
        _state["last_alert"] = datetime.now(timezone.utc)
        return {"sent": True, "count": len(old_snaps), "recipients": cfg["alert_to"]}
    except Exception as exc:
        _state["last_error"] = str(exc)
        return {"sent": False, "reason": "smtp_error", "error": str(exc), "count": len(old_snaps)}


# ── Orphan FCD email + check ─────────────────────────────────────────────────

def _compose_orphan_email(cfg: dict, orphans: list[dict], test: bool = False) -> MIMEMultipart:
    count = len(orphans)

    if test and count == 0:
        subject = "[PVC Monitor] Test alert — no orphaned FCDs currently"
        intro   = "This is a test email from PVC Monitor. There are currently <strong>no orphaned FCDs</strong>."
    else:
        subject = f"[PVC Monitor] {count} orphaned FCD{'s' if count != 1 else ''} detected"
        intro   = (
            f"<strong>{count} FCD{'s' if count != 1 else ''}</strong> "
            f"{'are' if count != 1 else 'is'} present in vSphere but not claimed by any "
            "Kubernetes PersistentVolume. These may be leaked volumes (PV deleted with "
            "Retain policy) or pre-existing non-CSI disks."
        )

    rows = ""
    for o in sorted(orphans, key=lambda x: x.get("capacity_gb", 0), reverse=True):
        attached  = o.get("vm_name") and o["vm_name"] != "—"
        vm_style  = "color:#e67e22;font-weight:600" if attached else "color:#999"
        rows += f"""
        <tr>
          <td style="padding:7px 12px;border-bottom:1px solid #eee;font-family:monospace;font-size:11px">{o.get('fcd_id') or '—'}</td>
          <td style="padding:7px 12px;border-bottom:1px solid #eee">{o.get('fcd_name') or '—'}</td>
          <td style="padding:7px 12px;border-bottom:1px solid #eee">{o.get('datastore') or '—'}</td>
          <td style="padding:7px 12px;border-bottom:1px solid #eee;text-align:right">{o.get('capacity_gb', 0)} GB</td>
          <td style="padding:7px 12px;border-bottom:1px solid #eee;{vm_style}">{o.get('vm_name') or '—'}</td>
        </tr>"""

    table_section = ""
    if rows:
        total_gb = sum(o.get("capacity_gb", 0) for o in orphans)
        table_section = f"""
  <table style="width:100%;border-collapse:collapse;margin-top:20px;font-size:13px">
    <thead>
      <tr style="background:#f5f5f5;text-align:left">
        <th style="padding:9px 12px;border-bottom:2px solid #ddd">FCD ID</th>
        <th style="padding:9px 12px;border-bottom:2px solid #ddd">Name</th>
        <th style="padding:9px 12px;border-bottom:2px solid #ddd">Datastore</th>
        <th style="padding:9px 12px;border-bottom:2px solid #ddd;text-align:right">Size</th>
        <th style="padding:9px 12px;border-bottom:2px solid #ddd">VM Attached</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
    <tfoot>
      <tr style="background:#f9f9f9;font-weight:600">
        <td colspan="3" style="padding:9px 12px;border-top:2px solid #ddd">Total wasted capacity</td>
        <td style="padding:9px 12px;border-top:2px solid #ddd;text-align:right">{total_gb:.1f} GB</td>
        <td style="padding:9px 12px;border-top:2px solid #ddd"></td>
      </tr>
    </tfoot>
  </table>"""

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;color:#333;max-width:960px;margin:0 auto;padding:24px;background:#fafafa">
  <div style="background:#fff;border-radius:8px;padding:28px;border:1px solid #e0e0e0">
    <h2 style="margin:0 0 6px;color:{'#888' if test and count == 0 else '#c0392b'}">
      {'⚙ Test Alert' if test and count == 0 else '⚠ Orphaned FCD Alert'}
    </h2>
    <p style="margin:0 0 16px;color:#555">{intro}</p>
    {table_section}
    <hr style="border:none;border-top:1px solid #eee;margin:24px 0">
    <p style="margin:0;font-size:11px;color:#aaa">
      Sent by PVC Monitor &nbsp;·&nbsp; {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    </p>
  </div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["smtp_from"]
    msg["To"]      = ", ".join(cfg["alert_to"])
    msg.attach(MIMEText(html, "html"))
    return msg


def check_and_alert_orphans(force: bool = False) -> dict:
    """
    Fetch orphaned FCDs, send email if any are found.
    force=True always sends (used for test-alert endpoint).
    """
    import vcenter_client  # late import — avoids circular dependency at module load

    cfg = get_config()
    _state_orphans["last_check"] = datetime.now(timezone.utc)
    _state_orphans["last_error"] = None

    if not is_configured(cfg):
        _state_orphans["last_error"] = "SMTP not configured"
        return {"sent": False, "reason": "not_configured", "count": 0}

    try:
        orphans = vcenter_client.get_orphaned_fcds()
    except Exception as exc:
        _state_orphans["last_error"] = str(exc)
        return {"sent": False, "reason": "fetch_error", "error": str(exc), "count": 0}

    _state_orphans["last_count"] = len(orphans)

    if not orphans and not force:
        return {"sent": False, "reason": "no_orphans", "count": 0}

    try:
        msg = _compose_orphan_email(cfg, orphans, test=force and not orphans)
        _send_email(cfg, msg)
        _state_orphans["last_alert"] = datetime.now(timezone.utc)
        return {"sent": True, "count": len(orphans), "recipients": cfg["alert_to"]}
    except Exception as exc:
        _state_orphans["last_error"] = str(exc)
        return {"sent": False, "reason": "smtp_error", "error": str(exc), "count": len(orphans)}


# ── Background scheduler ─────────────────────────────────────────────────────

async def alert_scheduler() -> None:
    cfg = get_config()
    if not is_configured(cfg):
        logger.info("Alerting: SMTP not configured — scheduler will not run")
        return

    interval_sec = cfg["check_interval_hours"] * 3600
    logger.info(
        "Alerting: scheduler started — interval=%.1fh, threshold=%dd, to=%s",
        cfg["check_interval_hours"], cfg["age_threshold_days"], cfg["alert_to"],
    )

    while True:
        # Snapshot age alert
        try:
            result = check_and_alert()
            if result.get("sent"):
                logger.info("Alerting: snapshots — sent email for %d old snapshots", result.get("count", 0))
            elif result.get("reason") == "no_old_snapshots":
                logger.info("Alerting: snapshots — none older than %dd", cfg["age_threshold_days"])
            else:
                logger.warning("Alerting: snapshots — check result: %s", result)
        except Exception as exc:
            logger.exception("Alerting: snapshots — unexpected error: %s", exc)

        # Orphan FCD alert
        try:
            result = check_and_alert_orphans()
            if result.get("sent"):
                logger.info("Alerting: orphans — sent email for %d orphaned FCDs", result.get("count", 0))
            elif result.get("reason") == "no_orphans":
                logger.info("Alerting: orphans — no orphaned FCDs found")
            else:
                logger.warning("Alerting: orphans — check result: %s", result)
        except Exception as exc:
            logger.exception("Alerting: orphans — unexpected error: %s", exc)

        await asyncio.sleep(interval_sec)
