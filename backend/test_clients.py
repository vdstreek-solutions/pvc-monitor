"""Tests for the vCenter and Kubernetes clients, using fakes only."""
import importlib
import ssl
import threading
import time
from types import SimpleNamespace

import pytest
from kubernetes.client import (
    V1ListMeta, V1ObjectMeta, V1PersistentVolume, V1PersistentVolumeList,
    V1PersistentVolumeSpec, V1CSIPersistentVolumeSource,
)
from kubernetes.client.rest import ApiException

import k8s_client
import vcenter_client


# ── vCenter TLS ──────────────────────────────────────────────────────────────

@pytest.fixture
def reload_vcenter(monkeypatch):
    """Re-read vcenter_client's settings from the environment; restore afterwards."""
    def reload(**env):
        for name in ("VCENTER_VERIFY_SSL", "VCENTER_CA_FILE"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("VCENTER_HOST", "vc.test")
        return importlib.reload(vcenter_client)

    yield reload
    monkeypatch.undo()
    importlib.reload(vcenter_client)


def capture_connect(monkeypatch, module):
    seen = {}

    def fake_connect(**kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(module, "SmartConnect", fake_connect)
    return seen


def test_vcenter_verifies_certificate_by_default(reload_vcenter, monkeypatch):
    module = reload_vcenter()
    seen = capture_connect(monkeypatch, module)
    module._get_connection()
    context = seen["sslContext"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert seen["httpConnectionTimeout"] == module.VCENTER_TIMEOUT_SECONDS


def test_vcenter_unverified_only_as_explicit_opt_out(reload_vcenter, monkeypatch, caplog):
    module = reload_vcenter(VCENTER_VERIFY_SSL="false")
    seen = capture_connect(monkeypatch, module)
    module._get_connection()
    assert seen["sslContext"].verify_mode == ssl.CERT_NONE
    assert "NOT verified" in caplog.text


def test_vcenter_ca_file_is_loaded(reload_vcenter, monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("not a certificate")
    module = reload_vcenter(VCENTER_CA_FILE=str(ca))
    capture_connect(monkeypatch, module)
    with pytest.raises(ssl.SSLError):  # the file is read, and rejected as invalid
        module._get_connection()


def test_vcenter_not_configured(monkeypatch):
    monkeypatch.setattr(vcenter_client, "VCENTER_HOST", "")
    result = vcenter_client.check_connectivity()
    assert result["connected"] is False
    assert result["error"] == "vCenter is not configured"


# ── vCenter session and view cleanup ─────────────────────────────────────────

def test_connectivity_failure_logs_out_and_hides_detail(monkeypatch):
    class FakeSI:
        def RetrieveContent(self):
            raise RuntimeError("fault from internal-host.example")

    logouts = []
    monkeypatch.setattr(vcenter_client, "VCENTER_HOST", "vc.test")
    monkeypatch.setattr(vcenter_client, "SmartConnect", lambda **kw: FakeSI())
    monkeypatch.setattr(vcenter_client, "Disconnect", logouts.append)
    result = vcenter_client.check_connectivity()
    assert result["connected"] is False
    assert "internal-host" not in result["error"]
    assert len(logouts) == 1


def test_container_view_destroyed_when_iteration_fails():
    destroyed = []

    class BadList:
        def __iter__(self):
            raise RuntimeError("permission fault")

    view = SimpleNamespace(view=BadList(), Destroy=lambda: destroyed.append(True))
    content = SimpleNamespace(
        rootFolder=object(),
        viewManager=SimpleNamespace(CreateContainerView=lambda *a: view),
    )
    vcenter_client._build_vm_attachment_index(content)
    assert destroyed == [True]


# ── One scan at a time, shared result ────────────────────────────────────────

def test_concurrent_callers_share_one_scan(monkeypatch):
    calls = []

    @vcenter_client._single_flight
    def scan():
        calls.append(1)
        time.sleep(0.2)
        return ["result"]

    results = []
    threads = [threading.Thread(target=lambda: results.append(scan())) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == [1]
    assert results == [["result"]] * 5


def test_failures_are_not_cached_and_cache_expires(monkeypatch):
    calls = []

    @vcenter_client._single_flight
    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first call fails")
        return len(calls)

    with pytest.raises(RuntimeError):
        flaky()
    assert flaky() == 2
    monkeypatch.setattr(vcenter_client, "VCENTER_CACHE_SECONDS", 0)
    assert flaky() == 3


def test_public_scans_use_the_shared_guard():
    for name in ("check_connectivity", "get_persistent_volumes", "get_snapshots",
                 "get_orphaned_fcds", "debug_vsphere_inventory"):
        assert hasattr(getattr(vcenter_client, name), "__wrapped__"), name


# ── Kubernetes paging ────────────────────────────────────────────────────────

def make_pv(name: str) -> V1PersistentVolume:
    return V1PersistentVolume(
        metadata=V1ObjectMeta(name=name),
        spec=V1PersistentVolumeSpec(
            csi=V1CSIPersistentVolumeSource(driver="csi.vsphere.vmware.com", volume_handle=f"h-{name}"),
            capacity={"storage": "1Gi"},
        ),
    )


class FakeCoreV1:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def list_persistent_volume(self, **kwargs):
        self.calls.append(kwargs)
        page = self.pages[len(self.calls) - 1]
        if isinstance(page, Exception):
            raise page
        return page


@pytest.fixture
def fake_core(monkeypatch):
    class FakeApi:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(k8s_client, "_get_api_client", lambda: FakeApi())

    def install(pages):
        core = FakeCoreV1(pages)
        monkeypatch.setattr(k8s_client.client, "CoreV1Api", lambda api: core)
        return core

    return install


def test_pv_list_follows_continue_tokens(fake_core):
    core = fake_core([
        V1PersistentVolumeList(items=[make_pv("a")], metadata=V1ListMeta(_continue="next")),
        V1PersistentVolumeList(items=[make_pv("b")], metadata=V1ListMeta()),
    ])
    pvs = k8s_client.get_k8s_persistent_volumes()
    assert [pv["name"] for pv in pvs] == ["a", "b"]
    assert pvs[1]["volume_handle"] == "h-b"
    assert core.calls[0]["_continue"] is None and core.calls[1]["_continue"] == "next"
    assert all(c["limit"] == k8s_client.PAGE_SIZE for c in core.calls)
    assert all(c["_request_timeout"] == k8s_client.REQUEST_TIMEOUT for c in core.calls)


def test_pv_list_raises_instead_of_returning_part(fake_core):
    fake_core([
        V1PersistentVolumeList(items=[make_pv("a")], metadata=V1ListMeta(_continue="next")),
        ApiException(status=410, reason="Expired"),
    ])
    with pytest.raises(ApiException):
        k8s_client.get_k8s_persistent_volumes()


def test_custom_object_list_follows_continue_tokens(monkeypatch):
    calls = []
    pages = [
        {"items": [{"metadata": {"name": "s1"}}], "metadata": {"continue": "t"}},
        {"items": [{"metadata": {"name": "s2"}}], "metadata": {}},
    ]

    class FakeCustom:
        def list_cluster_custom_object(self, **kwargs):
            calls.append(kwargs)
            return pages[len(calls) - 1]

    custom = FakeCustom()
    assert [i["metadata"]["name"] for i in k8s_client._list_custom(custom, "volumesnapshots")] == ["s1", "s2"]
    assert calls[1]["_continue"] == "t"


def test_k8s_ca_file_used_for_explicit_server(monkeypatch):
    monkeypatch.setattr(k8s_client, "K8S_SERVER", "https://k8s.test:6443")
    monkeypatch.setattr(k8s_client, "K8S_TOKEN", "token")
    monkeypatch.setattr(k8s_client, "K8S_CA_FILE", "/etc/ssl/k8s-ca.pem")
    api = k8s_client._get_api_client()
    assert api.configuration.ssl_ca_cert == "/etc/ssl/k8s-ca.pem"
    assert api.configuration.verify_ssl is True


# ── Real kubernetes client, only the HTTP layer replaced ─────────────────────

@pytest.fixture
def k8s_http(monkeypatch):
    """Route the real ApiClient's HTTP calls to canned JSON pages; record requests."""
    import json
    from urllib.parse import parse_qs, urlsplit
    from kubernetes.client import rest

    monkeypatch.setattr(k8s_client, "K8S_SERVER", "https://k8s.test:6443")
    monkeypatch.setattr(k8s_client, "K8S_TOKEN", "test-token")
    requests, responses = [], {}

    def fake_request(self, method, url, headers=None, body=None, post_params=None, _request_timeout=None):
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        requests.append({"path": parts.path, "query": query, "headers": headers, "timeout": _request_timeout})
        pages = responses[parts.path]
        payload = pages[query.get("continue", "")]
        raw = SimpleNamespace(status=200, reason="OK", data=json.dumps(payload).encode(),
                              headers={"content-type": "application/json"})
        return rest.RESTResponse(raw)

    monkeypatch.setattr(rest.RESTClientObject, "request", fake_request)
    return requests, responses


def test_real_client_pages_pv_list(k8s_http):
    requests, responses = k8s_http
    pv = {"metadata": {"name": "pv-{n}"}, "spec": {"csi": {"driver": "csi.vsphere.vmware.com", "volumeHandle": "h-{n}"},
                                                  "capacity": {"storage": "1Gi"}}, "status": {"phase": "Bound"}}
    item = lambda n: json_fill(pv, n)
    responses["/api/v1/persistentvolumes"] = {
        "": {"kind": "PersistentVolumeList", "apiVersion": "v1", "metadata": {"continue": "tok"}, "items": [item("a")]},
        "tok": {"kind": "PersistentVolumeList", "apiVersion": "v1", "metadata": {}, "items": [item("b")]},
    }
    pvs = k8s_client.get_k8s_persistent_volumes()
    assert [p["volume_handle"] for p in pvs] == ["h-a", "h-b"]
    assert requests[0]["query"]["limit"] == str(k8s_client.PAGE_SIZE)
    assert "continue" not in requests[0]["query"] and requests[1]["query"]["continue"] == "tok"
    assert requests[0]["timeout"] == k8s_client.REQUEST_TIMEOUT
    assert requests[0]["headers"]["authorization"] == "Bearer test-token"


def test_real_client_pages_custom_objects(k8s_http):
    requests, responses = k8s_http
    path = "/apis/snapshot.storage.k8s.io/v1/volumesnapshots"
    responses[path] = {
        "": {"items": [{"metadata": {"name": "s1"}}], "metadata": {"continue": "t"}},
        "t": {"items": [{"metadata": {"name": "s2"}}], "metadata": {}},
    }
    assert [s["name"] for s in k8s_client.get_k8s_volume_snapshots()] == ["s1", "s2"]
    assert requests[1]["query"]["continue"] == "t"


def test_real_client_version_check(k8s_http):
    requests, responses = k8s_http
    responses["/version/"] = {"": {"major": "1", "minor": "31", "gitVersion": "v1.31.0", "buildDate": "", "compiler": "gc",
                                     "gitCommit": "", "gitTreeState": "clean", "goVersion": "go", "platform": "linux/amd64"}}
    responses["/version"] = responses["/version/"]
    assert k8s_client.check_k8s_connectivity()["server_version"] == "v1.31"
    assert requests[0]["timeout"] == k8s_client.REQUEST_TIMEOUT


def json_fill(template, n):
    import json
    return json.loads(json.dumps(template).replace("{n}", n))


# ── Orphan scan end to end with a fake vCenter ───────────────────────────────

def test_orphan_scan_with_fake_vcenter(monkeypatch):
    def fcd(fcd_id):
        cfg = SimpleNamespace(id=SimpleNamespace(id=fcd_id), name=f"disk-{fcd_id}", capacityInMB=1024,
                              backing=SimpleNamespace(filePath=f"[ds1] fcd/{fcd_id}.vmdk"))
        return SimpleNamespace(config=cfg)

    fcds = {"claimed-1": fcd("claimed-1"), "orphan-1": fcd("orphan-1")}
    destroyed, logouts = [], []
    datastore = SimpleNamespace(name="ds1")

    def view_for(types):
        items = [datastore] if types == [vcenter_client.vim.Datastore] else []
        return SimpleNamespace(view=items, Destroy=lambda: destroyed.append(types[0]))

    content = SimpleNamespace(
        rootFolder=object(),
        viewManager=SimpleNamespace(CreateContainerView=lambda root, types, rec: view_for(types)),
        vStorageObjectManager=SimpleNamespace(
            ListVStorageObject=lambda ds: list(fcds),
            RetrieveVStorageObject=lambda ref, ds: fcds[ref],
        ),
    )
    monkeypatch.setattr(vcenter_client, "VCENTER_HOST", "vc.test")
    monkeypatch.setattr(vcenter_client, "SmartConnect", lambda **kw: SimpleNamespace(RetrieveContent=lambda: content))
    monkeypatch.setattr(vcenter_client, "Disconnect", logouts.append)
    monkeypatch.setattr(k8s_client, "get_k8s_persistent_volumes", lambda: [
        {"name": "pv", "driver": "csi.vsphere.vmware.com", "volume_handle": "CLAIMED-1"},
    ])

    orphans = vcenter_client.get_orphaned_fcds()
    assert [o["fcd_id"] for o in orphans] == ["orphan-1"]
    assert len(logouts) == 1 and len(destroyed) == 2  # VM view and datastore view

    def failing_list():
        raise ApiException(status=410, reason="Expired")

    vcenter_client._scan_cache.clear()
    monkeypatch.setattr(k8s_client, "get_k8s_persistent_volumes", failing_list)
    with pytest.raises(ApiException):
        vcenter_client.get_orphaned_fcds()
