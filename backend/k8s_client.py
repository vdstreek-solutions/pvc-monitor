import logging

from kubernetes import client, config
from kubernetes.client import ApiClient, Configuration
from kubernetes.client.rest import ApiException

from settings import Unavailable, env_bool, env_str

logger = logging.getLogger(__name__)

KUBECONFIG_PATH = env_str("KUBECONFIG_PATH")
K8S_CONTEXT     = env_str("K8S_CONTEXT") or None
K8S_SERVER      = env_str("K8S_SERVER")
K8S_TOKEN       = env_str("K8S_TOKEN")
K8S_VERIFY_SSL  = env_bool("K8S_VERIFY_SSL", True)
K8S_CA_FILE     = env_str("K8S_CA_FILE")  # CA bundle for K8S_SERVER
VSPHERE_DRIVER  = "csi.vsphere.vmware.com"
SNAPSHOT_GROUP  = "snapshot.storage.k8s.io"
SNAPSHOT_VER    = "v1"

# (connect, read) timeout in seconds for every API call.
REQUEST_TIMEOUT = (10, 60)
# Objects per list page; only one page of full objects is held at a time.
PAGE_SIZE = 250

if K8S_SERVER and K8S_TOKEN and not K8S_VERIFY_SSL:
    logger.warning(
        "Kubernetes: K8S_VERIFY_SSL=false, the API server certificate is NOT verified "
        "and K8S_TOKEN can be intercepted"
    )


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
        if K8S_CA_FILE:
            cfg.ssl_ca_cert = K8S_CA_FILE
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
        logger.warning("Kubernetes: no usable configuration: %r", str(exc))
        raise Unavailable("Kubernetes is not configured") from exc


def _list_typed(list_call, **kwargs):
    """Yield every item of a typed list call, one page at a time.

    Any error, including an expired continue token (410), propagates: callers
    must never work with a partial list.
    """
    token = None
    while True:
        page = list_call(limit=PAGE_SIZE, _continue=token, _request_timeout=REQUEST_TIMEOUT, **kwargs)
        yield from page.items or []
        token = page.metadata._continue if page.metadata else None
        if not token:
            return


def _list_custom(custom: client.CustomObjectsApi, plural: str):
    """Yield every item of a cluster-wide custom object list, one page at a time."""
    token = None
    while True:
        page = custom.list_cluster_custom_object(
            group=SNAPSHOT_GROUP, version=SNAPSHOT_VER, plural=plural,
            limit=PAGE_SIZE, _continue=token, _request_timeout=REQUEST_TIMEOUT,
        )
        yield from page.get("items") or []
        token = (page.get("metadata") or {}).get("continue")
        if not token:
            return


def check_k8s_connectivity() -> dict:
    try:
        with _get_api_client() as api:
            ver = client.VersionApi(api).get_code(_request_timeout=REQUEST_TIMEOUT)
        return {
            "connected": True,
            "server_version": f"v{ver.major}.{ver.minor}",
            "git_version": ver.git_version,
        }
    except Unavailable as exc:
        return {"connected": False, "configured": False, "error": str(exc)}
    except Exception:
        logger.exception("Kubernetes: connectivity check failed")
        return {"connected": False, "configured": True, "error": "connection failed, see the server log"}


def get_k8s_persistent_volumes() -> list[dict]:
    """All PVs. Raises if the list cannot be read completely: the result
    decides which vSphere disks count as claimed."""
    result = []
    with _get_api_client() as api:
        for pv in _list_typed(client.CoreV1Api(api).list_persistent_volume):
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
                "capacity": (pv.spec.capacity or {}).get("storage", "N/A"),
                "access_modes": pv.spec.access_modes or [],
                "reclaim_policy": pv.spec.persistent_volume_reclaim_policy,
                "status": pv.status.phase if pv.status else None,
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
    result = []
    with _get_api_client() as api:
        v1 = client.CoreV1Api(api)
        for pvc in _list_typed(v1.list_persistent_volume_claim_for_all_namespaces):
            requested = (pvc.spec.resources.requests or {}).get("storage", "N/A") if pvc.spec.resources else "N/A"
            actual    = ((pvc.status.capacity if pvc.status else None) or {}).get("storage", requested)
            result.append({
                "name": pvc.metadata.name,
                "namespace": pvc.metadata.namespace,
                "status": pvc.status.phase if pvc.status else None,
                "volume_name": pvc.spec.volume_name or "—",
                "capacity": actual,
                "access_modes": pvc.spec.access_modes or [],
                "storage_class": pvc.spec.storage_class_name or "N/A",
                "created": pvc.metadata.creation_timestamp.isoformat() if pvc.metadata.creation_timestamp else None,
            })
    return result


def get_k8s_volume_snapshots() -> list[dict]:
    result = []
    with _get_api_client() as api:
        try:
            for snap in _list_custom(client.CustomObjectsApi(api), "volumesnapshots"):
                meta   = snap.get("metadata") or {}
                spec   = snap.get("spec") or {}
                status = snap.get("status") or {}
                result.append({
                    "name": meta.get("name"),
                    "namespace": meta.get("namespace"),
                    "source_pvc": (spec.get("source") or {}).get("persistentVolumeClaimName"),
                    "snapshot_class": spec.get("volumeSnapshotClassName"),
                    "content_name": status.get("boundVolumeSnapshotContentName"),
                    "ready": status.get("readyToUse", False),
                    "restore_size": status.get("restoreSize"),
                    "created": meta.get("creationTimestamp"),
                })
        except ApiException as exc:
            if exc.status in (404, 405):
                raise Unavailable("VolumeSnapshot CRD not installed") from exc
            raise
    return result


def get_k8s_volume_snapshot_contents() -> list[dict]:
    result = []
    with _get_api_client() as api:
        try:
            for content in _list_custom(client.CustomObjectsApi(api), "volumesnapshotcontents"):
                meta     = content.get("metadata") or {}
                spec     = content.get("spec") or {}
                status   = content.get("status") or {}
                snap_ref = spec.get("volumeSnapshotRef") or {}
                result.append({
                    "name": meta.get("name"),
                    "driver": spec.get("driver"),
                    "snapshot_handle": status.get("snapshotHandle") or (spec.get("source") or {}).get("snapshotHandle"),
                    "bound_snapshot": f"{snap_ref.get('namespace')}/{snap_ref.get('name')}" if snap_ref.get("name") else None,
                    "reclaim_policy": spec.get("deletionPolicy"),
                    "ready": status.get("readyToUse", False),
                    "restore_size": status.get("restoreSize"),
                    "created": meta.get("creationTimestamp"),
                })
        except ApiException as exc:
            if exc.status in (404, 405):
                raise Unavailable("VolumeSnapshotContent CRD not installed") from exc
            raise
    return result
