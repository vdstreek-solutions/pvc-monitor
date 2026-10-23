import ssl
import os
import re
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
from dotenv import load_dotenv

# Matches snapshot delta disks: basename-000001.vmdk, basename-0001.vmdk, etc.
_SNAPSHOT_DELTA_RE = re.compile(r'-\d{4,6}\.vmdk$', re.IGNORECASE)


def _delta_to_base(file_path: str) -> str | None:
    """
    If file_path is a snapshot delta VMDK (ends in -NNNNNN.vmdk), return the
    base path (ends in .vmdk).  Otherwise return None.

    Example: "[ds] fcd/_x/f799081d951647d58572810601d0d2b8-000001.vmdk"
          -> "[ds] fcd/_x/f799081d951647d58572810601d0d2b8.vmdk"
    """
    if _SNAPSHOT_DELTA_RE.search(file_path):
        return _SNAPSHOT_DELTA_RE.sub('.vmdk', file_path)
    return None


def _nodash(uuid: str) -> str:
    """Return a UUID string with all dashes removed.

    vSphere stores FCDs with UUID-without-dashes as the backing filename:
      volumeHandle  f799081d-9516-47d5-8572-810601d0d2b8   (K8s, with dashes)
      VMDK filename f799081d951647d58572810601d0d2b8.vmdk  (vSphere, no dashes)
    Substring-matching the no-dash form finds both the base disk and any delta.
    """
    return uuid.replace('-', '')

load_dotenv()

VCENTER_HOST = os.getenv("VCENTER_HOST", "")
VCENTER_USER = os.getenv("VCENTER_USER", "")
VCENTER_PASSWORD = os.getenv("VCENTER_PASSWORD", "")
VCENTER_PORT = int(os.getenv("VCENTER_PORT", "443"))
VCENTER_VERIFY_SSL = os.getenv("VCENTER_VERIFY_SSL", "false").lower() == "true"


def _get_connection():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    if not VCENTER_VERIFY_SSL:
        context.verify_mode = ssl.CERT_NONE
    return SmartConnect(
        host=VCENTER_HOST,
        user=VCENTER_USER,
        pwd=VCENTER_PASSWORD,
        port=VCENTER_PORT,
        sslContext=context,
    )


def check_connectivity() -> dict:
    try:
        si = _get_connection()
        about = si.RetrieveContent().about
        Disconnect(si)
        return {
            "connected": True,
            "vcenter_host": VCENTER_HOST,
            "version": about.version,
            "build": about.build,
            "full_name": about.fullName,
        }
    except Exception as exc:
        return {
            "connected": False,
            "vcenter_host": VCENTER_HOST,
            "error": str(exc),
        }


VSPHERE_CSI_DRIVER = "csi.vsphere.vmware.com"


def _get_vsphere_pv_maps() -> tuple[dict, dict]:
    """
    Build lookup maps for matching vSphere FCDs to Kubernetes PVs.

    Returns:
        term_to_pv  – any search term (lower) → PV dict.
                      Indexed by: PV name AND PVC name so that FCD display names
                      set to either convention are found.
        handle_to_pv – volumeHandle (lower) → PV dict, for UUID-based matching.

    Deduplication key: volume_handle (used in `seen` sets throughout).
    """
    import k8s_client
    pvs = k8s_client.get_k8s_persistent_volumes()
    term_to_pv: dict = {}
    handle_to_pv: dict = {}
    for pv in pvs:
        if pv.get("driver") != VSPHERE_CSI_DRIVER:
            continue
        pv_name = (pv.get("name") or "").lower()
        handle = (pv.get("volume_handle") or "").lower()
        # claim_ref is "namespace/pvc-name" — extract just the PVC name
        claim_ref = pv.get("claim_ref") or ""
        pvc_name = claim_ref.split("/")[-1].lower() if "/" in claim_ref else ""

        if pv_name:
            term_to_pv[pv_name] = pv
        # Index by PVC name too; don't overwrite if PV name already claimed the slot
        if pvc_name and pvc_name not in term_to_pv:
            term_to_pv[pvc_name] = pv
        if handle:
            handle_to_pv[handle] = pv
    return term_to_pv, handle_to_pv


def _pv_key(pv: dict) -> str:
    """Canonical dedup key for a PV — volume_handle, falling back to PV name."""
    return (pv.get("volume_handle") or pv.get("name") or "").lower()


def _ds_label(ds) -> str:
    """Safe datastore label that never triggers a permission-gated property fetch."""
    try:
        return ds.name
    except Exception:
        try:
            return ds._moId
        except Exception:
            return str(ds)


def _build_vm_attachment_index(content) -> tuple[dict, dict]:
    """
    Scan every VM and return two lookup dicts:
        obj_id_index   – backingObjectId (lower) → vm_info
        filepath_index – fileName (lower)        → vm_info

    filepath_index is keyed by BOTH the actual fileName AND (when the disk is a
    snapshot delta) the derived base path, so FCD catalog entries can match VMs
    that currently have a snapshot active on a CSI volume.
    """
    obj_id_index: dict = {}
    filepath_index: dict = {}
    try:
        vm_view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        for vm in vm_view.view:
            try:
                if not vm.config:
                    continue
                for device in vm.config.hardware.device:
                    if not isinstance(device, vim.vm.device.VirtualDisk):
                        continue
                    backing = device.backing
                    ds_ref = getattr(backing, "datastore", None)
                    vm_info = {
                        "vm_name": vm.name,
                        "vm_power_state": str(vm.runtime.powerState),
                        "file_path": getattr(backing, "fileName", "") or "N/A",
                        "datastore": ds_ref.name if ds_ref else "N/A",
                        "capacity_gb": round(device.capacityInKB / 1024 / 1024, 2),
                    }
                    obj_id = (getattr(backing, "backingObjectId", None) or "").lower()
                    if obj_id:
                        obj_id_index[obj_id] = vm_info
                    fp = (getattr(backing, "fileName", None) or "").lower()
                    if fp:
                        filepath_index[fp] = vm_info
                        # When the active disk is a snapshot delta, also index
                        # by the base path so the FCD catalog lookup still hits.
                        base = _delta_to_base(fp)
                        if base:
                            filepath_index[base] = vm_info
            except Exception:
                continue
        vm_view.Destroy()
    except Exception:
        pass
    return obj_id_index, filepath_index


def _scan_fcd_catalog(content, term_to_pv: dict, handle_to_pv: dict,
                      seen: set, obj_id_index: dict, filepath_index: dict) -> list[dict]:
    """
    Iterate the FCD catalog across all accessible datastores and match each FCD
    to a K8s PV.  Matching order:
      1. FCD display name == PV name  (CSI driver default)
      2. FCD display name == PVC name (some CSI configurations)
      3. FCD UUID         == volumeHandle

    Annotates each result with attached node VM data from the index.
    Skips datastores where the account lacks System.View / StorageViews.View.
    """
    volumes: list = []
    try:
        vsom = content.vStorageObjectManager
        ds_view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.Datastore], True
        )
        for ds in ds_view.view:
            ds_id = _ds_label(ds)
            try:
                refs = vsom.ListVStorageObject(ds)
            except Exception:
                continue
            for ref in refs:
                try:
                    obj = vsom.RetrieveVStorageObject(ref, ds)
                    cfg = obj.config
                    fcd_name = (getattr(cfg, "name", None) or "").lower()
                    fcd_id = (cfg.id.id or "").lower()

                    pv = term_to_pv.get(fcd_name) or handle_to_pv.get(fcd_id)
                    if not pv:
                        continue

                    key = _pv_key(pv)
                    if key in seen:
                        continue
                    seen.add(key)

                    fcd_backing = getattr(cfg, "backing", None)
                    fcd_file_path = (
                        getattr(fcd_backing, "filePath", "") if fcd_backing else ""
                    ) or ""

                    vm_info = (
                        obj_id_index.get(fcd_id)
                        or obj_id_index.get(_nodash(fcd_id))
                        or filepath_index.get(fcd_file_path.lower())
                    )

                    volumes.append({
                        "volume_id": pv.get("volume_handle") or cfg.id.id,
                        "pv_name": pv["name"],
                        "claim": pv.get("claim_ref") or "—",
                        "capacity_gb": (
                            (vm_info or {}).get("capacity_gb")
                            or round(getattr(cfg, "capacityInMB", 0) / 1024, 2)
                        ),
                        "datastore": (vm_info or {}).get("datastore") or ds_id,
                        "file_path": (vm_info or {}).get("file_path") or fcd_file_path or "N/A",
                        "vm_name": (vm_info or {}).get("vm_name") or "—",
                        "vm_power_state": (vm_info or {}).get("vm_power_state") or "—",
                        "storage_class": pv.get("storage_class") or "—",
                        "k8s_status": pv.get("status") or "—",
                    })
                except Exception:
                    continue
        try:
            ds_view.Destroy()
        except Exception:
            pass
    except Exception:
        pass
    return volumes


def get_persistent_volumes() -> list[dict]:
    term_to_pv, handle_to_pv = _get_vsphere_pv_maps()
    if not term_to_pv:
        return []

    si = _get_connection()
    try:
        content = si.RetrieveContent()
        obj_id_index, filepath_index = _build_vm_attachment_index(content)
        return _scan_fcd_catalog(
            content, term_to_pv, handle_to_pv,
            set(), obj_id_index, filepath_index,
        )
    finally:
        Disconnect(si)


def debug_vsphere_inventory() -> dict:
    """
    Diagnostic: all VMDKs on VMs, all accessible FCDs, and K8s PV search terms.
    GET /api/debug/vsphere
    """
    import k8s_client
    pvs = k8s_client.get_k8s_persistent_volumes()
    vsphere_pvs = []
    for p in pvs:
        if p.get("driver") != VSPHERE_CSI_DRIVER:
            continue
        claim_ref = p.get("claim_ref") or ""
        pvc_name = claim_ref.split("/")[-1] if "/" in claim_ref else ""
        vsphere_pvs.append({
            "pv_name": p["name"],
            "pvc_name": pvc_name,
            "volume_handle": p.get("volume_handle"),
            "status": p.get("status"),
        })

    si = _get_connection()
    try:
        content = si.RetrieveContent()

        all_vmdks = []
        try:
            vm_view = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.VirtualMachine], True
            )
            for vm in vm_view.view:
                try:
                    if not vm.config:
                        continue
                    for device in vm.config.hardware.device:
                        if not isinstance(device, vim.vm.device.VirtualDisk):
                            continue
                        all_vmdks.append({
                            "vm": vm.name,
                            "label": device.deviceInfo.label,
                            "file_path": getattr(device.backing, "fileName", ""),
                            "backing_obj_id": getattr(device.backing, "backingObjectId", None),
                            "capacity_gb": round(device.capacityInKB / 1024 / 1024, 2),
                        })
                except Exception:
                    continue
            vm_view.Destroy()
        except Exception:
            pass

        all_fcds = []
        fcd_errors = []
        try:
            vsom = content.vStorageObjectManager
            ds_view = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.Datastore], True
            )
            for ds in ds_view.view:
                ds_id = _ds_label(ds)
                try:
                    refs = vsom.ListVStorageObject(ds)
                except Exception as e:
                    fcd_errors.append(f"ListVStorageObject on {ds_id}: {type(e).__name__}")
                    continue
                for ref in refs:
                    try:
                        obj = vsom.RetrieveVStorageObject(ref, ds)
                        cfg = obj.config
                        backing = getattr(cfg, "backing", None)
                        all_fcds.append({
                            "fcd_id": cfg.id.id,
                            "fcd_name": getattr(cfg, "name", None),
                            "datastore": ds_id,
                            "capacity_mb": getattr(cfg, "capacityInMB", 0),
                            "file_path": getattr(backing, "filePath", None) if backing else None,
                        })
                    except Exception as e:
                        fcd_errors.append(f"RetrieveVStorageObject on {ds_id}: {type(e).__name__}")
            try:
                ds_view.Destroy()
            except Exception:
                pass
        except Exception as e:
            fcd_errors.append(f"vStorageObjectManager: {type(e).__name__}: {e}")

        return {
            "k8s_vsphere_pvs": vsphere_pvs,
            "vsphere_vmdks": all_vmdks,
            "vsphere_fcds": all_fcds,
            "fcd_errors": fcd_errors,
        }
    finally:
        Disconnect(si)


def get_snapshots() -> list[dict]:
    """
    Return FCD-level snapshots for vSphere CSI volumes.

    Kubernetes VolumeSnapshots backed by csi.vsphere.vmware.com are implemented
    as FCD snapshots, NOT VM snapshots.  The delta VMDKs are stored under the
    fcd/ directory on the datastore (e.g. fcd/_003c/<uuid>-000001.vmdk) and
    vm.snapshot on node VMs remains None.

    The correct vSphere API is vsom.RetrieveSnapshotInfo(fcd_id, datastore).
    """
    term_to_pv, handle_to_pv = _get_vsphere_pv_maps()
    if not term_to_pv and not handle_to_pv:
        return []

    search_terms = set(term_to_pv.keys())

    si = _get_connection()
    try:
        content = si.RetrieveContent()
        all_snapshots = []
        try:
            vsom = content.vStorageObjectManager
            ds_view = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.Datastore], True
            )
            for ds in ds_view.view:
                ds_id = _ds_label(ds)
                try:
                    refs = vsom.ListVStorageObject(ds)
                except Exception:
                    continue
                for ref in refs:
                    try:
                        obj = vsom.RetrieveVStorageObject(ref, ds)
                        cfg = obj.config
                        fcd_name = (getattr(cfg, "name", None) or "").lower()
                        fcd_id   = (cfg.id.id or "").lower()

                        pv = term_to_pv.get(fcd_name) or handle_to_pv.get(fcd_id)
                        if not pv:
                            continue

                        # Query FCD-level snapshots
                        try:
                            snap_info = vsom.RetrieveSnapshotInfo(ref, ds)
                        except Exception:
                            continue
                        if not snap_info or not getattr(snap_info, "snapshots", None):
                            continue

                        claim_ref = pv.get("claim_ref") or ""
                        pvc_name  = claim_ref.split("/")[-1] if "/" in claim_ref else claim_ref

                        for snap in snap_info.snapshots:
                            try:
                                snap_id = snap.id.id if hasattr(snap.id, "id") else str(snap.id)
                            except Exception:
                                snap_id = "—"
                            try:
                                created = snap.createTime.isoformat() if getattr(snap, "createTime", None) else None
                            except Exception:
                                created = None
                            all_snapshots.append({
                                "pv_name":       pv["name"],
                                "pvc_name":      pvc_name or "—",
                                "volume_handle": pv.get("volume_handle") or "—",
                                "snapshot_id":   snap_id,
                                "description":   getattr(snap, "description", "") or "",
                                "created":       created,
                                "datastore":     ds_id,
                                "storage_class": pv.get("storage_class") or "—",
                            })
                    except Exception:
                        continue
            try:
                ds_view.Destroy()
            except Exception:
                pass
        except Exception:
            pass
        return all_snapshots
    finally:
        Disconnect(si)


def get_orphaned_fcds() -> list[dict]:
    """
    FCDs in the vSphere catalog that have no corresponding Kubernetes PV.

    A K8s PV's spec.csi.volumeHandle is the FCD UUID. Any FCD whose UUID is
    not in that set is either:
      - A CSI volume whose PV was deleted (Retain policy → FCD stays on disk)
      - A pre-existing / non-CSI FCD (check fcd_name for clues)
      - A genuinely leaked volume

    Raises RuntimeError when K8s is not configured, because without the list of
    claimed handles we cannot determine which FCDs are orphaned.
    """
    import k8s_client

    pvs = k8s_client.get_k8s_persistent_volumes()   # raises RuntimeError if not configured
    claimed = {
        pv["volume_handle"].lower()
        for pv in pvs
        if pv.get("driver") == VSPHERE_CSI_DRIVER and pv.get("volume_handle")
    }

    si = _get_connection()
    try:
        content = si.RetrieveContent()
        obj_id_index, filepath_index = _build_vm_attachment_index(content)

        orphans: list[dict] = []
        try:
            vsom = content.vStorageObjectManager
            ds_view = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.Datastore], True
            )
            for ds in ds_view.view:
                ds_id = _ds_label(ds)
                try:
                    refs = vsom.ListVStorageObject(ds)
                except Exception:
                    continue
                for ref in refs:
                    try:
                        obj = vsom.RetrieveVStorageObject(ref, ds)
                        cfg = obj.config
                        fcd_id = (cfg.id.id or "").lower()

                        if fcd_id in claimed:
                            continue  # claimed by a K8s PV — not orphaned

                        fcd_backing  = getattr(cfg, "backing", None)
                        fcd_file_path = (
                            getattr(fcd_backing, "filePath", "") if fcd_backing else ""
                        ) or ""

                        vm_info = (
                            obj_id_index.get(fcd_id)
                            or obj_id_index.get(_nodash(fcd_id))
                            or filepath_index.get(fcd_file_path.lower())
                        )

                        try:
                            ds_name = ds.name
                        except Exception:
                            ds_name = ds_id

                        orphans.append({
                            "fcd_id":        cfg.id.id,
                            "fcd_name":      getattr(cfg, "name", "") or "—",
                            "datastore":     ds_name,
                            "capacity_gb":   round(getattr(cfg, "capacityInMB", 0) / 1024, 2),
                            "file_path":     fcd_file_path or "—",
                            "vm_name":       (vm_info or {}).get("vm_name") or "—",
                            "vm_power_state": (vm_info or {}).get("vm_power_state") or "—",
                        })
                    except Exception:
                        continue
            try:
                ds_view.Destroy()
            except Exception:
                pass
        except Exception:
            pass

        return orphans
    finally:
        Disconnect(si)


def get_container_volumes() -> list[dict]:
    """
    Return PVCs backed by the vSphere CSI driver, sourced from Kubernetes.

    CNS is not required. The standalone vSphere CSI driver provisions FCDs
    directly; PVCs are the Kubernetes view of those volumes.
    """
    import k8s_client

    pvs = k8s_client.get_k8s_persistent_volumes()
    vsphere_pv_map = {
        pv["name"]: pv
        for pv in pvs
        if pv.get("driver") == VSPHERE_CSI_DRIVER
    }
    if not vsphere_pv_map:
        return []

    pvcs = k8s_client.get_k8s_persistent_volume_claims()
    result = []
    for pvc in pvcs:
        pv_name = pvc.get("volume_name") or ""
        if pv_name not in vsphere_pv_map:
            continue
        pv = vsphere_pv_map[pv_name]
        result.append({
            "name": pvc["name"],
            "namespace": pvc["namespace"],
            "pv_name": pv_name,
            "volume_handle": pv.get("volume_handle") or "—",
            "capacity": pvc.get("capacity") or pv.get("capacity") or "—",
            "status": pvc.get("status") or "—",
            "storage_class": pvc.get("storage_class") or "—",
            "access_modes": pvc.get("access_modes") or [],
            "created": pvc.get("created"),
        })
    return result
