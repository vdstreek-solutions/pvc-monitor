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

Optional Dex OIDC integration with group-based access control (`ALLOWED_GROUPS`). When a user's groups do not intersect with the allowed set, they see a styled 403 page explaining which groups are required.

## Architecture

```
┌─────────────────────────────────────────┐
│  React (Vite)  ── served by FastAPI     │
├─────────────────────────────────────────┤
│  FastAPI backend                        │
│  ├── pyVmomi  → vCenter SOAP API        │
│  ├── kubernetes-client → K8s API        │
│  └── authlib  → Dex OIDC               │
└─────────────────────────────────────────┘
```

Built as a single Docker image (multi-stage): Node 20 builds the React app, Python 3.12 serves it via FastAPI/uvicorn.

## Prerequisites

- vCenter with a service account that has read access to datastores and virtual machines
- Kubernetes cluster using the vSphere CSI driver (`csi.vsphere.vmware.com`)
- The external-snapshotter CRDs installed if you want snapshot data
- (Optional) Dex OIDC provider for authentication

## Configuration

All configuration is via environment variables. Copy `backend/.env.example` for local development.

### vCenter

| Variable | Description | Default |
|----------|-------------|---------|
| `VCENTER_HOST` | vCenter hostname or IP | — |
| `VCENTER_USER` | Service account UPN | — |
| `VCENTER_PASSWORD` | Service account password | — |
| `VCENTER_PORT` | vCenter HTTPS port | `443` |
| `VCENTER_VERIFY_SSL` | Verify TLS certificate | `true` |

### Kubernetes

| Variable | Description |
|----------|-------------|
| `K8S_SERVER` | API server URL (OIDC / external clusters) |
| `K8S_TOKEN` | Bearer token |
| `K8S_VERIFY_SSL` | Verify TLS (default `true`) |
| `KUBECONFIG_PATH` | Path to a kubeconfig file |
| `K8S_CONTEXT` | Context within the kubeconfig |

When none of the above are set the app uses in-cluster config (mounted ServiceAccount token), which is the default when deployed in Kubernetes.

### Email alerts

| Variable | Description | Default |
|----------|-------------|---------|
| `SMTP_HOST` | SMTP server hostname | — |
| `SMTP_PORT` | SMTP port | `587` |
| `SMTP_USE_TLS` | Use STARTTLS | `true` |
| `SMTP_FROM` | Sender address | — |
| `SMTP_USER` | SMTP username (optional) | — |
| `SMTP_PASSWORD` | SMTP password (optional) | — |
| `ALERT_EMAIL_TO` | Comma-separated recipients | — |
| `ALERT_SNAPSHOT_AGE_DAYS` | Age threshold for snapshot alerts | `7` |
| `ALERT_CHECK_INTERVAL_HOURS` | How often to run both alert checks | `24` |

Alerts are disabled when `SMTP_HOST` or `ALERT_EMAIL_TO` is empty. Both alert types (snapshot age and orphaned FCDs) use the same SMTP settings and run on the same interval. Each has a **Test Alert** button in the UI that triggers an immediate test email.

### Dex OIDC authentication

| Variable | Description | Default |
|----------|-------------|---------|
| `AUTH_ENABLED` | Enable authentication | `true` |
| `OIDC_ISSUER` | Dex issuer URL | — |
| `OIDC_CLIENT_ID` | OAuth2 client ID | — |
| `OIDC_CLIENT_SECRET` | OAuth2 client secret | — |
| `OIDC_REDIRECT_URI` | Full callback URL | — |
| `OIDC_SCOPES` | Requested scopes | `openid email profile groups` |
| `ALLOWED_GROUPS` | Comma-separated Dex groups allowed to log in | _(all users)_ |
| `SESSION_SECRET` | Cookie signing key — generate with `openssl rand -hex 32` | — |
| `SESSION_SECURE` | Set `Secure` flag on session cookie (use `true` behind HTTPS) | `false` |

Authentication is disabled when `OIDC_ISSUER` is empty. Set `AUTH_ENABLED=false` to disable it explicitly in development.

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
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in values
uvicorn main:app --reload --port 8000
```

```bash
# Frontend (separate terminal)
cd frontend
npm install
npm run dev                   # proxies /api/* to localhost:8000
```

Open http://localhost:5173.

To disable authentication locally set `AUTH_ENABLED=false` or leave `OIDC_ISSUER` empty in `.env`.

## Running tests

```bash
cd backend
pip install pytest
pytest test_alerting.py -v
```

## Docker

```bash
# Build
docker build -t pvc-monitor .

# Run
docker run -p 8000:8000 \
  -e VCENTER_HOST=vcenter.example.com \
  -e VCENTER_USER=svc@vsphere.local \
  -e VCENTER_PASSWORD=secret \
  -e VCENTER_VERIFY_SSL=false \
  -e AUTH_ENABLED=false \
  pvc-monitor
```

## Deploying to Kubernetes

```bash
# Put real credentials in a private values file (do not commit it) under `secret.data`,
# or create the Secret yourself and set `secret.existingSecret`.
helm upgrade --install pvc-monitor helm/pvc-monitor -n mdt-cps --create-namespace \
  --set gateway.parentRef.name=<gateway> \
  -f my-values.yaml
```

Traffic is exposed through the Gateway API: the chart creates a `ListenerSet` (HTTPS listener for `gateway.hostname`, attached to the Gateway in `gateway.parentRef`), an `HTTPRoute` and a cert-manager `Certificate`. The Gateway must allow ListenerSets. Set `gateway.enabled=false` to skip all of these.

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
| `GET` | `/auth/user` | Current session user info |

## Project structure

```
├── backend/
│   ├── main.py              # FastAPI app, all API endpoints
│   ├── vcenter_client.py    # pyVmomi — FCD catalog, snapshots, VM attachment, orphan detection
│   ├── k8s_client.py        # kubernetes-client — PVs, PVCs, VolumeSnapshots
│   ├── auth.py              # Dex OIDC middleware and routes
│   ├── alerting.py          # Email alert scheduler (snapshot age + orphaned FCDs)
│   ├── test_alerting.py     # pytest tests for alerting
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
