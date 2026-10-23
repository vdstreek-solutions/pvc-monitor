import { useState, useEffect, useCallback, useRef } from 'react'

function readyBadge(ready) {
  return ready
    ? <span className="badge badge-green">Ready</span>
    : <span className="badge badge-yellow">Not Ready</span>
}

function useTabData(url) {
  const [data, setData]                 = useState([])
  const [loading, setLoading]           = useState(true)
  const [error, setError]               = useState(null)
  const [crdMissing, setCrdMissing]     = useState(false)
  const [notConfigured, setNotConfigured] = useState(false)
  const [spinning, setSpinning]         = useState(false)

  const fetch_ = useCallback(async () => {
    setSpinning(true)
    setError(null)
    setCrdMissing(false)
    setNotConfigured(false)
    try {
      const res = await fetch(url)
      if (res.status === 503) {
        const err = await res.json()
        const detail = err.detail || ''
        if (detail.toLowerCase().includes('not configured')) {
          setNotConfigured(true)
        } else {
          setCrdMissing(true)
        }
        setError(detail || 'Unavailable')
        return
      }
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || res.statusText)
      }
      setData(await res.json())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
      setSpinning(false)
    }
  }, [url])

  useEffect(() => { fetch_() }, [fetch_])
  return { data, loading, error, crdMissing, notConfigured, spinning, refresh: fetch_ }
}

export default function K8sSnapshots({ onCount }) {
  const onCountRef = useRef(onCount)
  onCountRef.current = onCount

  const [subTab, setSubTab] = useState('snapshots')
  const [search, setSearch] = useState('')

  const snaps    = useTabData('/api/k8s/volume-snapshots')
  const contents = useTabData('/api/k8s/volume-snapshot-contents')

  useEffect(() => {
    onCountRef.current(snaps.data.length)
  }, [snaps.data.length])

  const active   = subTab === 'snapshots' ? snaps : contents

  const filteredSnaps = snaps.data.filter(s =>
    !search ||
    (s.name         || '').toLowerCase().includes(search.toLowerCase()) ||
    (s.namespace    || '').toLowerCase().includes(search.toLowerCase()) ||
    (s.source_pvc   || '').toLowerCase().includes(search.toLowerCase())
  )

  const filteredContents = contents.data.filter(c =>
    !search ||
    (c.name             || '').toLowerCase().includes(search.toLowerCase()) ||
    (c.snapshot_handle  || '').toLowerCase().includes(search.toLowerCase()) ||
    (c.bound_snapshot   || '').toLowerCase().includes(search.toLowerCase())
  )

  const filtered = subTab === 'snapshots' ? filteredSnaps : filteredContents

  return (
    <>
      <div className="section-header">
        <div>
          <div className="section-title">Kubernetes Snapshots</div>
          <div className="section-subtitle">VolumeSnapshots and SnapshotContents via vSphere CSI</div>
        </div>
        <button className="btn" onClick={active.refresh} disabled={active.spinning}>
          {active.spinning ? <span className="spin-icon">↻</span> : '↻'} Refresh
        </button>
      </div>

      {!active.loading && !active.crdMissing && !active.error && (
        <div className="stats-bar">
          <div className="stat-card">
            <div className="stat-value">{snaps.data.length}</div>
            <div className="stat-label">VolumeSnapshots</div>
          </div>
          <div className="stat-card">
            <div className="stat-value">{snaps.data.filter(s => s.ready).length}</div>
            <div className="stat-label">Ready</div>
          </div>
          <div className="stat-card">
            <div className="stat-value">{contents.data.length}</div>
            <div className="stat-label">Snapshot Contents</div>
          </div>
          <div className="stat-card">
            <div className="stat-value">
              {new Set(snaps.data.map(s => s.namespace).filter(Boolean)).size}
            </div>
            <div className="stat-label">Namespaces</div>
          </div>
        </div>
      )}

      {/* Sub-tabs */}
      <div style={{ display: 'flex', gap: 0, marginBottom: 16, borderBottom: '1px solid var(--border)' }}>
        {[
          ['snapshots', `VolumeSnapshots (${snaps.data.length})`],
          ['contents',  `SnapshotContents (${contents.data.length})`],
        ].map(([id, label]) => (
          <button
            key={id}
            onClick={() => { setSubTab(id); setSearch('') }}
            style={{
              padding: '8px 16px',
              border: 'none',
              background: 'none',
              fontSize: 13,
              cursor: 'pointer',
              color: subTab === id ? 'var(--accent-hover)' : 'var(--text-secondary)',
              borderBottom: `2px solid ${subTab === id ? 'var(--accent-hover)' : 'transparent'}`,
              marginBottom: -1,
              fontWeight: subTab === id ? 500 : 400,
              transition: 'color .15s',
            }}
          >
            {label}
          </button>
        ))}
      </div>

      {active.notConfigured && (
        <div className="info-banner">
          <span style={{ fontSize: 16 }}>ℹ</span>
          <div>
            <strong>Kubernetes not configured</strong>
            For OIDC clusters set <span className="td-mono">K8S_SERVER</span> and <span className="td-mono">K8S_TOKEN</span> in
            your <span className="td-mono">.env</span> file. Alternatively set <span className="td-mono">KUBECONFIG_PATH</span> to
            a kubeconfig that contains a valid OIDC id-token.
            <br /><br />
            <span style={{ color: 'var(--text-secondary)' }}>Detail: {active.error}</span>
          </div>
        </div>
      )}

      {active.crdMissing && (
        <div className="info-banner">
          <span style={{ fontSize: 16 }}>ℹ</span>
          <div>
            <strong>VolumeSnapshot CRDs not installed</strong>
            Install the external snapshotter to enable VolumeSnapshot support:
            {' '}<span className="td-mono">kubectl apply -f https://…/snapshot.storage.k8s.io_v1.yaml</span>
            <br /><br />
            <span style={{ color: 'var(--text-secondary)' }}>Detail: {active.error}</span>
          </div>
        </div>
      )}

      {!active.crdMissing && active.error && (
        <div className="error-state">
          <span>⚠</span>
          <span>{active.error}</span>
        </div>
      )}

      {active.loading ? (
        <div className="loading-state">
          <div className="spinner" />
          <span>Loading {subTab === 'snapshots' ? 'VolumeSnapshots' : 'SnapshotContents'}…</span>
        </div>
      ) : !active.notConfigured && !active.crdMissing && !active.error && (
        <>
          <div className="filter-row">
            <input
              type="search"
              className="search-input"
              placeholder={
                subTab === 'snapshots'
                  ? 'Filter by name, namespace, or source PVC…'
                  : 'Filter by name or snapshot handle…'
              }
              value={search}
              onChange={e => setSearch(e.target.value)}
            />
            {search && <span className="filter-count">{filtered.length} result{filtered.length !== 1 ? 's' : ''}</span>}
          </div>

          <div className="table-container">
            <div className="table-scroll">
              {filtered.length === 0 ? (
                <div className="empty-state">
                  <div className="empty-state-icon">📷</div>
                  <div>{search ? 'No matching resources' : 'No snapshot resources found'}</div>
                </div>
              ) : subTab === 'snapshots' ? (
                <table>
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Namespace</th>
                      <th>Source PVC</th>
                      <th>Snapshot Class</th>
                      <th>Ready</th>
                      <th>Restore Size</th>
                      <th>Content</th>
                      <th>Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredSnaps.map((s, i) => (
                      <tr key={i}>
                        <td style={{ fontWeight: 500 }}>{s.name}</td>
                        <td><span className="badge badge-blue">{s.namespace}</span></td>
                        <td className="td-mono">{s.source_pvc || '—'}</td>
                        <td>{s.snapshot_class || '—'}</td>
                        <td>{readyBadge(s.ready)}</td>
                        <td>{s.restore_size || '—'}</td>
                        <td className="td-mono" style={{ fontSize: 11 }}>{s.content_name || '—'}</td>
                        <td className="td-mono">{s.created ? new Date(s.created).toLocaleDateString() : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Driver</th>
                      <th>Snapshot Handle</th>
                      <th>Bound Snapshot</th>
                      <th>Reclaim Policy</th>
                      <th>Ready</th>
                      <th>Restore Size</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredContents.map((c, i) => (
                      <tr key={i}>
                        <td style={{ fontWeight: 500 }}>{c.name}</td>
                        <td className="td-mono">{c.driver || '—'}</td>
                        <td className="td-mono">{c.snapshot_handle || '—'}</td>
                        <td className="td-mono">{c.bound_snapshot || '—'}</td>
                        <td>
                          {c.reclaim_policy === 'Retain'
                            ? <span className="badge badge-blue">{c.reclaim_policy}</span>
                            : <span className="badge badge-yellow">{c.reclaim_policy || '—'}</span>}
                        </td>
                        <td>{readyBadge(c.ready)}</td>
                        <td>{c.restore_size || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </>
      )}
    </>
  )
}
