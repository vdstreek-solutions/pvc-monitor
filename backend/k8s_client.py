import os
from kubernetes import client, config
from kubernetes.client import ApiClient, Configuration
from kubernetes.client.rest import ApiException
from dotenv import load_dotenv

load_dotenv()

KUBECONFIG_PATH = os.getenv("KUBECONFIG_PATH", "")
K8S_CONTEXT     = os.getenv("K8S_CONTEXT", "") or None
K8S_SERVER      = os.getenv("K8S_SERVER", "")
K8S_TOKEN       = os.getenv("K8S_TOKEN", "")
K8S_VERIFY_SSL  = os.getenv("K8S_VERIFY_SSL", "true").lower() == "true"
VSPHERE_DRIVER  = "csi.vsphere.vmware.com"
SNAPSHOT_GROUP  = "snapshot.storage.k8s.io"
SNAPSHOT_VER    = "v1"


def _get_api_client() -> ApiClient:
    """
    Build an ApiClient with credentials explicitly bound.

    Priority:
      1. K8S_SERVER + K8S_TOKEN  — direct bearer token; most reliable for OIDC clusters
         where kubeconfig exec/OIDC plugins are unavailable server-side.
      2. KUBECONFIG_PATH         — load a specific kubeconfig file (supports OIDC via
         oauthlib if the id-token / refresh-token are present).
      3. In-cluster config       — service account mounted inside a pod.
      4. ~/.kube/config          — default kubeconfig as last resort.
    """
    # Option 1: explicit server + bearer token (best for OIDC in a backend context)
    if K8S_SERVER and K8S_TOKEN:
        cfg = Configuration()
        cfg.host                    = K8S_SERVER
        cfg.api_key["authorization"] = K8S_TOKEN
        cfg.api_key_prefix["authorization"] = "Bearer"
        cfg.verify_ssl              = K8S_VERIFY_SSL
        if not K8S_VERIFY_SSL:
            cfg.ssl_ca_cert = None
        return ApiClient(configuration=cfg)

    # Options 2-4: kubeconfig-based (OIDC token refresh handled by oauthlib)
    try:
        if KUBECONFIG_PATH:
            return config.new_client_from_config(
                config_file=KUBECONFIG_PATH,
                context=K8S_CONTEXT,
            )
        try:
            cfg = Configuration()
            config.load_incluster_config(client_configuration=cfg)
            return ApiClient(configuration=cfg)
        except config.ConfigException:
            return config.new_client_from_config(context=K8S_CONTEXT)
    except config.ConfigException as exc:
        raise RuntimeError(f"K8s not configured: {exc}") from exc


def check_k8s_connectivity() -> dict:
    try:
        with _get_api_client() as api:
            ver = client.VersionApi(api).get_code()
        return {
            "connected": True,
            "server_version": f"v{ver.major}.{ver.minor}",
            "git_version": ver.git_version,
        }
    except RuntimeError as exc:
        return {"connected": False, "configured": False, "error": str(exc)}
    except Exception as exc:
        return {"connected": False, "configured": True, "error": str(exc)}


def get_k8s_persistent_volumes() -> list[dict]:
    with _get_api_client() as api:
        pvs = client.CoreV1Api(api).list_persistent_volume()
    result = []
    for pv in pvs.items:
        if pv.spec.csi:
            driver       = pv.spec.csi.driver
            volume_handle = pv.spec.csi.volume_handle
            fs_type      = pv.spec.csi.fs_type or "ext4"
        elif pv.spec.vsphere_volume:
            driver       = "kubernetes.io/vsphere-volume"
            volume_handle = pv.spec.vsphere_volume.volume_path
            fs_type      = pv.spec.vsphere_volume.fs_type or "ext4"
        else:
            driver       = _pv_plugin_name(pv)
            volume_handle = None
            fs_type      = None

        result.append({
            "name": pv.metadata.name,
            "driver": driver,
            "volume_handle": volume_handle,
            "capacity": pv.spec.capacity.get("storage", "N/A"),
            "access_modes": pv.spec.access_modes or [],
            "reclaim_policy": pv.spec.persistent_volume_reclaim_policy,
            "status": pv.status.phase,
            "storage_class": pv.spec.storage_class_name or "N/A",
            "claim_ref": (
                f"{pv.spec.claim_ref.namespace}/{pv.spec.claim_ref.name}"
                if pv.spec.claim_ref else None
            ),
            "fs_type": fs_type,
            "created": pv.metadata.creation_timestamp.isoformat() if pv.metadata.creation_timestamp else None,
        })
    return result


def _pv_plugin_name(pv) -> str:
    """Best-effort plugin name for non-CSI, non-vSphere PVs."""
    spec = pv.spec
    if spec.nfs:           return "nfs"
    if spec.host_path:     return "hostPath"
    if spec.iscsi:         return "iscsi"
    if spec.fc:            return "fc"
    if spec.rbd:           return "rbd"
    if spec.aws_elastic_block_store: return "awsEBS"
    if spec.azure_disk:    return "azureDisk"
    if spec.gce_persistent_disk:     return "gcePD"
    return "unknown"


def get_k8s_persistent_volume_claims() -> list[dict]:
    with _get_api_client() as api:
        v1   = client.CoreV1Api(api)
        pvcs = v1.list_persistent_volume_claim_for_all_namespaces()

    result = []
    for pvc in pvcs.items:
        requested = (pvc.spec.resources.requests or {}).get("storage", "N/A")
        actual    = (pvc.status.capacity or {}).get("storage", requested)
        result.append({
            "name": pvc.metadata.name,
            "namespace": pvc.metadata.namespace,
            "status": pvc.status.phase,
            "volume_name": pvc.spec.volume_name or "—",
            "capacity": actual,
            "access_modes": pvc.spec.access_modes or [],
            "storage_class": pvc.spec.storage_class_name or "N/A",
            "created": pvc.metadata.creation_timestamp.isoformat() if pvc.metadata.creation_timestamp else None,
        })
    return result


def get_k8s_volume_snapshots() -> list[dict]:
    with _get_api_client() as api:
        custom = client.CustomObjectsApi(api)
        try:
            raw = custom.list_cluster_custom_object(
                group=SNAPSHOT_GROUP, version=SNAPSHOT_VER, plural="volumesnapshots"
            )
        except ApiException as exc:
            if exc.status in (404, 405):
                raise RuntimeError("VolumeSnapshot CRD not installed") from exc
            raise

    result = []
    for snap in raw.get("items", []):
        meta   = snap.get("metadata", {})
        spec   = snap.get("spec", {})
        status = snap.get("status", {})
        result.append({
            "name": meta.get("name"),
            "namespace": meta.get("namespace"),
            "source_pvc": spec.get("source", {}).get("persistentVolumeClaimName"),
            "snapshot_class": spec.get("volumeSnapshotClassName"),
            "content_name": status.get("boundVolumeSnapshotContentName"),
            "ready": status.get("readyToUse", False),
            "restore_size": status.get("restoreSize"),
            "created": meta.get("creationTimestamp"),
        })
    return result


def get_k8s_volume_snapshot_contents() -> list[dict]:
    with _get_api_client() as api:
        custom = client.CustomObjectsApi(api)
        try:
            raw = custom.list_cluster_custom_object(
                group=SNAPSHOT_GROUP, version=SNAPSHOT_VER, plural="volumesnapshotcontents"
            )
        except ApiException as exc:
            if exc.status in (404, 405):
                raise RuntimeError("VolumeSnapshotContent CRD not installed") from exc
            raise

    result = []
    for content in raw.get("items", []):
        meta     = content.get("metadata", {})
        spec     = content.get("spec", {})
        status   = content.get("status", {})
        snap_ref = spec.get("volumeSnapshotRef", {})
        result.append({
            "name": meta.get("name"),
            "driver": spec.get("driver"),
            "snapshot_handle": status.get("snapshotHandle") or spec.get("source", {}).get("snapshotHandle"),
            "bound_snapshot": f"{snap_ref.get('namespace')}/{snap_ref.get('name')}" if snap_ref.get("name") else None,
            "reclaim_policy": spec.get("deletionPolicy"),
            "ready": status.get("readyToUse", False),
            "restore_size": status.get("restoreSize"),
            "created": meta.get("creationTimestamp"),
        })
    return result
