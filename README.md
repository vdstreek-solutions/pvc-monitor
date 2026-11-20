# PVC Monitor

A web application for monitoring Kubernetes PersistentVolumes backed by the vSphere CSI driver. Provides a unified view of PVCs, their corresponding vSphere First Class Disks (FCDs), FCD-level snapshots, orphaned disks, and an overview page to spot unused or stale volumes at a glance.

## Features

| Tab | What it shows |
|-----|---------------|
| **Overview** | All vSphere CSI PVCs joined with vCenter FCD data — usage state: Active / Idle / Released / Pending |
| **Persistent Volumes** | vSphere FCDs matched to Kubernetes PVs — datastore, capacity, VM attachment, power state |
| **Container Volumes** | Kubernetes PVCs backed by the vSphere CSI driver — namespace, status, storage class, access modes |
| **Snapshots** | FCD-level snapshots created via the Kubernetes VolumeSnapshot API — age badges, old-snapshot email alerts |
| **Orphaned FCDs** | vSphere FCDs with no matching Kubernetes PV — leaked volumes or non-CSI disks, with email alerts |
| **K8s Volumes** | Raw Kubernetes PersistentVolumes and PVCs with status filter buttons |
| **K8s Snapshots** | Kubernetes VolumeSnapshot and VolumeSnapshotContent objects |

### Email alerts

Two independent alert types share the same SMTP configuration and run on the same check interval:

| Alert | Trigger | Subject line |
|-------|---------|--------------|
| **Snapshot age** | Any FCD snapshot older than `ALERT_SNAPSHOT_AGE_DAYS` (default 7 days) | `[PVC Monitor] N snapshots older than X days` |
| **Orphaned FCDs** | Any FCD present in vSphere but not claimed by a Kubernetes PV | `[PVC Monitor] N orphaned FCDs detected` |

Each alert tab has a **Test Alert** button that sends a one-off email immediately, even when there is nothing to report.

### Authentication

Login through an OIDC provider (for example Dex) is on by default, with group-based access control (`ALLOWED_GROUPS`, required; `*` admits every user the provider authenticates and logs a warning at start). The application refuses to start when login is enabled but not fully configured; running without login requires `AUTH_ENABLED=false`. When a user's groups do not intersect with the allowed set, they see a 403 page listing their groups and the required ones, right after the denied sign-in only. When the sign-in itself fails (for example a misconfigured issuer), a short error page with a retry link is shown instead of an automatic redirect to the login, which could loop; the detail is in the server log.

## Architecture

```
┌─────────────────────────────────────────┐
│  React (Vite)  ── served by FastAPI     │
├─────────────────────────────────────────┤
│  FastAPI backend                        │
│  ├── pyVmomi  → vCenter SOAP API        │
│  ├── kubernetes-client → K8s API        │
│  └── authlib  → OIDC provider (Dex)     │
└─────────────────────────────────────────┘
```

Built as a single Docker image (multi-stage): Node 26 builds the React app, Python 3.12 serves it via FastAPI/uvicorn.

## Prerequisites

- vCenter with a dedicated service account and a custom role, see [Required vCenter privileges](#required-vcenter-privileges)
- Kubernetes cluster using the vSphere CSI driver (`csi.vsphere.vmware.com`)
- The external-snapshotter CRDs installed if you want snapshot data
- An OIDC provider (for example Dex) for login, unless you run with `AUTH_ENABLED=false` for local development

### Required vCenter privileges

The service account is **not read-only**. According to the vSphere Web Services API reference, the First Class Disk calls this application makes (`ListVStorageObject`, `RetrieveVStorageObject`, `RetrieveSnapshotInfo`) require the `Datastore.FileManagement` privilege ("Low level file operations") on the datastore. That is the same privilege that deleting a First Class Disk or one of its snapshots requires, so whoever holds this account's password can delete disks and snapshots on those datastores. Treat the password accordingly.

Recommended set-up:

1. Create a dedicated account for this application. Do not use an administrator account.
2. Create a custom role that contains only **Datastore > Low level file operations** (`Datastore.FileManagement`). The system privileges every role carries (`System.Anonymous`, `System.View`, `System.Read`) are added automatically.
3. Assign that role to the account on the datastores (or the datastore folder) that the vSphere CSI driver uses, not on the vCenter root.
4. Assign the built-in **Read-only** role to the account on the VM folder that holds the Kubernetes node VMs, so the application can see which disks are attached.

Datastores where the account lacks `Datastore.FileManagement` are skipped without an error, so their disks do not appear in any view or alert.

## Configuration

All configuration is via environment variables. Copy `backend/.env.example` for local development.

### vCenter

| Variable | Description | Default |
|----------|-------------|---------|
| `VCENTER_HOST` | vCenter hostname (must match the certificate) | — |
| `VCENTER_USER` | Service account UPN | — |
| `VCENTER_PASSWORD` | Service account password | — |
| `VCENTER_PORT` | vCenter HTTPS port | `443` |
| `VCENTER_VERIFY_SSL` | Verify the certificate chain and hostname. `false` disables verification and logs a warning at start; the password can then be intercepted | `true` |
| `VCENTER_CA_FILE` | PEM bundle with the CA that signed the vCenter certificate (for example the VMCA root). Empty uses the system trust store | — |
| `VCENTER_CACHE_SECONDS` | How long a finished vCenter scan is reused. Only one scan runs at a time; requests that arrive during a scan wait for it (up to `VCENTER_SCAN_WAIT_SECONDS`) and get its result | `60` |
| `VCENTER_SCAN_WAIT_SECONDS` | How long a request waits for a running scan; after that it gets `503` "scan in progress" instead of queueing | `120` |

Every vCenter request has a 60-second socket timeout, and every session is logged out after use.

### Kubernetes

| Variable | Description |
|----------|-------------|
| `K8S_SERVER` | API server URL (OIDC / external clusters) |
| `K8S_TOKEN` | Bearer token |
| `K8S_CA_FILE` | CA bundle for `K8S_SERVER` (used with `K8S_SERVER`/`K8S_TOKEN` only) |
| `K8S_VERIFY_SSL` | Verify TLS for `K8S_SERVER` (default `true`; `false` logs a warning) |
| `KUBECONFIG_PATH` | Path to a kubeconfig file |
| `K8S_CONTEXT` | Context within the kubeconfig |

When none of the above are set the app uses in-cluster config (mounted ServiceAccount token), which is the default when deployed in Kubernetes.

List calls are paged and time out after 60 seconds. If the PersistentVolume list cannot be read completely, the request fails rather than working with a partial list, because that list decides which disks count as claimed.

### Email alerts

| Variable | Description | Default |
|----------|-------------|---------|
| `SMTP_HOST` | SMTP server hostname | — |
| `SMTP_PORT` | SMTP port | `587` |
| `SMTP_USE_TLS` | Use STARTTLS (port 465 always uses implicit TLS) | `true` |
| `SMTP_FROM` | Sender address | — |
| `SMTP_USER` | SMTP username (optional) | — |
| `SMTP_PASSWORD` | SMTP password (optional) | — |
| `ALERT_EMAIL_TO` | Comma-separated recipients | — |
| `ALERT_SNAPSHOT_AGE_DAYS` | Age threshold for snapshot alerts | `7` |
| `ALERT_CHECK_INTERVAL_HOURS` | How often to run both alert checks (minimum `1`) | `24` |

Alerts are disabled when `SMTP_HOST`, `SMTP_FROM` or `ALERT_EMAIL_TO` is empty. Both alert types (snapshot age and orphaned FCDs) use the same SMTP settings and run on the same interval. Each has a **Test Alert** button in the UI that triggers an immediate test email; each kind can be triggered at most once per minute.

The SMTP server certificate and hostname are always verified. For a relay with a private CA, add the CA to the image trust store or point the standard `SSL_CERT_FILE` variable at a bundle. Credentials are never sent without TLS: with `SMTP_USE_TLS=false` on a port other than 465, leave `SMTP_USER`/`SMTP_PASSWORD` empty (unauthenticated relay only). A snapshot whose creation time cannot be read is included in the alert with age "unknown".

Invalid numeric or boolean settings stop the application at start with a message naming the variable.

### OIDC authentication

| Variable | Description | Default |
|----------|-------------|---------|
| `AUTH_ENABLED` | Require login. `false` serves every route without login and logs a warning | `true` |
| `OIDC_ISSUER` | Issuer URL; must be `https://` (`http://` only for localhost). Required when `AUTH_ENABLED=true` | — |
| `OIDC_CLIENT_ID` | OAuth2 client ID. Required when `AUTH_ENABLED=true` | — |
| `OIDC_CLIENT_SECRET` | OAuth2 client secret. Required when `AUTH_ENABLED=true` | — |
| `OIDC_REDIRECT_URI` | Full callback URL; set it explicitly in production. The Helm chart sets `https://<gateway.hostname>/auth/callback` when empty | derived from the request |
| `OIDC_SCOPES` | Requested scopes | `openid email profile groups` |
| `ALLOWED_GROUPS` | Comma-separated groups allowed to log in. Required when `AUTH_ENABLED=true`; `*` allows every authenticated user and logs a warning at start | — |
| `SESSION_SECRET` | Cookie signing key, at least 32 characters; generate with `openssl rand -hex 32`. Required when `AUTH_ENABLED=true` | — |
| `SESSION_SECURE` | `Secure` flag on the session cookie; when `true` the cookie is named `__Host-session` | `true` |
| `SESSION_MAX_AGE_HOURS` | Absolute session lifetime, counted from login. After it the user signs in again, which re-checks `ALLOWED_GROUPS` | `12` |
| `TRUSTED_HOSTS` | Optional comma-separated list of accepted `Host` header values; other hosts get `400`. `/healthz` is exempt so probes keep working. The Helm chart sets `gateway.hostname` when empty | — |
| `FORWARDED_ALLOW_IPS` | Read by uvicorn: addresses (IPs or CIDRs) whose `X-Forwarded-*` headers are trusted. Set it to the reverse proxy or Gateway data plane so the app sees the original `https` scheme and client address; the Helm chart sets it from `forwardedAllowIps` | `127.0.0.1` |
| `ENABLE_DEBUG_ENDPOINT` | Register `/api/debug/vsphere`, which lists every virtual disk visible to the vCenter account | `false` |

With `AUTH_ENABLED=true` (the default) the application refuses to start unless `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `SESSION_SECRET` and `ALLOWED_GROUPS` are set. An empty `OIDC_ISSUER` does not disable login. `AUTH_ENABLED` is false for `false`, `0`, `no` or `off` in any case; the Helm chart recognises the same values.

The login uses PKCE (S256), and the ID token's issuer and audience must match `OIDC_ISSUER` and `OIDC_CLIENT_ID`. Only the groups that match `ALLOWED_GROUPS` are stored in the session cookie. The cookie is signed, not stored server-side: logout (a same-origin `POST /auth/logout`) clears it in the browser, and rotating `SESSION_SECRET` ends every session.

#### Dex client registration

Add a `staticClient` entry to your Dex configuration:

```yaml
staticClients:
  - id: pvc-monitor
    secret: <OIDC_CLIENT_SECRET>
    name: PVC Monitor
    redirectURIs:
      - https://pvc-monitor.example.com/auth/callback
```

For group-based access control, ensure your Dex connector is configured to return group membership (e.g. LDAP `groupSearch`, AD groups). The `groups` scope is requested by default.

## Running locally

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
cp .env.example .env          # fill in values
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

`backend/.env` is read once at start, before any setting is used.

```bash
# Frontend (separate terminal)
cd frontend
npm install
npm run dev                   # proxies /api, /auth and /healthz to 127.0.0.1:8000
```

Open http://localhost:3000. The Vite dev server and the docker-compose services listen on `127.0.0.1` only.

To run without login during local development, set `AUTH_ENABLED=false` in `.env` and keep the backend bound to `127.0.0.1`. To test the login flow locally, keep `AUTH_ENABLED=true` and set the five required OIDC, session and group values; `SESSION_SECURE=true` works on `http://localhost` because browsers treat localhost as a secure origin.

## Running tests

```bash
cd backend
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
pytest
```

The tests use fakes for vCenter, Kubernetes, SMTP and the identity provider, ignore `backend/.env` and need no network access.

## Docker

```bash
# Build
docker build -t pvc-monitor .

# Run
# Local trial without login: publish on loopback only.
docker run -p 127.0.0.1:8000:8000 \
  -e VCENTER_HOST=vcenter.example.com \
  -e VCENTER_USER=svc-pvc-monitor@vsphere.local \
  -e VCENTER_PASSWORD=secret \
  -e AUTH_ENABLED=false \
  pvc-monitor
```

## Deploying to Kubernetes

```bash
# Put real credentials in a private values file (do not commit it) under `secret.data`,
# or create the Secret yourself and set `secret.existingSecret`.
helm upgrade --install pvc-monitor helm/pvc-monitor -n pvc-monitor --create-namespace \
  --set gateway.parentRef.name=<gateway> \
  -f my-values.yaml
```

Traffic is exposed through the Gateway API: the chart creates a `ListenerSet` (HTTPS listener for `gateway.hostname`, attached to the Gateway in `gateway.parentRef`), an `HTTPRoute` and a cert-manager `Certificate`. The Gateway must allow ListenerSets. Set `gateway.enabled=false` to skip all of these.

With `gateway.enabled` and the chart's own Secret, the Deployment sets `TRUSTED_HOSTS` to `gateway.hostname` and `OIDC_REDIRECT_URI` to `https://<gateway.hostname>/auth/callback` unless `secret.data` sets them. With `secret.existingSecret` the chart cannot see your values and sets neither; put both in that Secret. Also set `forwardedAllowIps` to the Gateway data-plane addresses (it becomes `FORWARDED_ALLOW_IPS`); otherwise uvicorn ignores the `X-Forwarded-*` headers and the app does not see the original `https` scheme. The chart refuses to render an exposed release with `AUTH_ENABLED` set to any false spelling (`false`, `0`, `no`, `off`, any case), or with authentication on and `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `SESSION_SECRET` or `ALLOWED_GROUPS` empty.

The deployment uses the pod's ServiceAccount token for in-cluster Kubernetes access. The ClusterRole grants read-only access to:

- `persistentvolumes`, `persistentvolumeclaims`
- `volumesnapshots`, `volumesnapshotcontents` (snapshot.storage.k8s.io)

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | vCenter connectivity check |
| `GET` | `/api/persistent-volumes` | vSphere FCDs with K8s PV data |
| `GET` | `/api/container-volumes` | Kubernetes PVCs (CSI driver) |
| `GET` | `/api/snapshots` | FCD-level snapshots |
| `GET` | `/api/orphaned-fcds` | FCDs not claimed by any K8s PV |
| `GET` | `/api/overview` | Joined PVC + FCD usage overview |
| `GET` | `/api/k8s/health` | Kubernetes API connectivity check |
| `GET` | `/api/k8s/persistent-volumes` | Raw K8s PersistentVolumes |
| `GET` | `/api/k8s/persistent-volume-claims` | Raw K8s PersistentVolumeClaims |
| `GET` | `/api/k8s/volume-snapshots` | K8s VolumeSnapshot objects |
| `GET` | `/api/k8s/volume-snapshot-contents` | K8s VolumeSnapshotContent objects |
| `GET` | `/api/alerts/status` | Alert scheduler state (snapshots + orphans) |
| `POST` | `/api/alerts/test` | Send a test snapshot age alert email |
| `POST` | `/api/alerts/orphan-test` | Send a test orphaned FCD alert email |
| `GET` | `/api/debug/vsphere` | vSphere diagnostics; only when `ENABLE_DEBUG_ENDPOINT=true` |
| `GET` | `/auth/user` | Current session user info |
| `POST` | `/auth/logout` | End the session; same-origin check as the alert routes, answers `303` to the login |
| `GET` | `/healthz` | Liveness probe, no external calls, no login |

The `POST` routes refuse cross-site browser requests (`Sec-Fetch-Site` / `Origin` check); the two alert routes also answer `429` when the same kind was triggered less than a minute ago. Errors return a fixed message; the detail is written to the server log. Routes that scan vCenter, including `/api/health`, answer `503` "scan in progress" when another scan holds the lock longer than `VCENTER_SCAN_WAIT_SECONDS`. The interactive API pages (`/docs`, `/redoc`, `/openapi.json`) are disabled.

Every response from the application, including unhandled `500` errors, carries `Content-Security-Policy` (`default-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin` and `X-Frame-Options: DENY`. The sign-in error and 403 pages send their own, stricter policy that allows only their inline style. In the docker-compose set-up, nginx sends the same policy for the SPA it serves. The SPA is served from the same origin as the API, so no CORS headers are sent.

## Project structure

```
├── backend/
│   ├── main.py              # FastAPI app, all API endpoints
│   ├── settings.py          # Typed environment settings and start-up errors
│   ├── vcenter_client.py    # pyVmomi — FCD catalog, snapshots, VM attachment, orphan detection
│   ├── k8s_client.py        # kubernetes-client — PVs, PVCs, VolumeSnapshots
│   ├── auth.py              # OIDC middleware, session and routes
│   ├── alerting.py          # Email alert scheduler (snapshot age + orphaned FCDs)
│   ├── conftest.py          # Shared test set-up (no .env, no network)
│   ├── test_*.py            # pytest tests
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   └── src/
│       ├── App.jsx
│       └── components/
│           ├── Overview.jsx
│           ├── PersistentVolumes.jsx
│           ├── ContainerVolumes.jsx
│           ├── Snapshots.jsx
│           ├── OrphanedFCDs.jsx
│           ├── K8sVolumes.jsx
│           └── K8sSnapshots.jsx
├── helm/pvc-monitor/        # Helm chart (Deployment, RBAC, Secret, ListenerSet, HTTPRoute, ...)
└── Dockerfile
```
