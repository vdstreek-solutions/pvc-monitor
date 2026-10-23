import { useState, useEffect, useCallback, useRef } from 'react'

function phaseBadge(phase) {
  if (phase === 'Bound')    return <span className="badge badge-green">Bound</span>
  if (phase === 'Pending')  return <span className="badge badge-yellow">Pending</span>
  if (phase === 'Lost')     return <span className="badge badge-red">Lost</span>
  return <span className="badge badge-gray">{phase || '—'}</span>
}

export default function ContainerVolumes({ onCount }) {
  const onCountRef = useRef(onCount)
  onCountRef.current = onCount

  const [data, setData]           = useState([])
  const [loading, setLoading]     = useState(true)
  const [error, setError]         = useState(null)
  const [notConfigured, setNotConfigured] = useState(false)
  const [search, setSearch]       = useState('')
  const [spinning, setSpinning]   = useState(false)

  const fetchData = useCallback(async () => {
    setSpinning(true)
    setError(null)
    setNotConfigured(false)
    try {
      const res = await fetch('/api/container-volumes')
      if (res.status === 503) {
        const err = await res.json()
        setNotConfigured(true)
        setError(err.detail || 'Kubernetes not configured')
        onCountRef.current(0)
        return
      }
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || res.statusText)
      }
      const json = await res.json()
      setData(json)
      onCountRef.current(json.length)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
      setSpinning(false)
    }
  }, [])

  useEffect(() => { fetchData() }, [fetchData])

  const filtered = data.filter(v =>
    !search ||
    (v.name           || '').toLowerCase().includes(search.toLowerCase()) ||
    (v.namespace      || '').toLowerCase().includes(search.toLowerCase()) ||
    (v.pv_name        || '').toLowerCase().includes(search.toLowerCase()) ||
    (v.storage_class  || '').toLowerCase().includes(search.toLowerCase()) ||
    (v.volume_handle  || '').toLowerCase().includes(search.toLowerCase())
  )

  const boundCount    = data.filter(v => v.status === 'Bound').length
  const namespaces    = new Set(data.map(v => v.namespace)).size

  return (
    <>
      <div className="section-header">
        <div>
          <div className="section-title">Container Volumes</div>
          <div className="section-subtitle">
            Kubernetes PVCs backed by the vSphere CSI driver (<span style={{ fontFamily: 'monospace' }}>csi.vsphere.vmware.com</span>)
          </div>
        </div>
        <button className="btn" onClick={fetchData} disabled={spinning}>
          {spinning ? <span className="spin-icon">↻</span> : '↻'} Refresh
        </button>
      </div>

      {notConfigured && (
        <div className="info-banner">
          <span style={{ fontSize: 16 }}>ℹ</span>
          <div>
            <strong>Kubernetes not configured</strong>
            Set <span className="td-mono">K8S_SERVER</span> and <span className="td-mono">K8S_TOKEN</span> in
            your secret, or ensure the pod is running with a ServiceAccount that has PVC read access.
            <br /><br />
            <span style={{ color: 'var(--text-secondary)' }}>Detail: {error}</span>
          </div>
        </div>
      )}

      {!notConfigured && error && (
        <div className="error-state">
          <span>⚠</span>
          <span>{error}</span>
        </div>
      )}

      {!loading && !notConfigured && !error && (
        <div className="stats-bar">
          <div className="stat-card">
            <div className="stat-value">{data.length}</div>
            <div className="stat-label">Total PVCs</div>
          </div>
          <div className="stat-card">
            <div className="stat-value">{boundCount}</div>
            <div className="stat-label">Bound</div>
          </div>
          <div className="stat-card">
            <div className="stat-value">{data.length - boundCount}</div>
            <div className="stat-label">Pending / Lost</div>
          </div>
          <div className="stat-card">
            <div className="stat-value">{namespaces}</div>
            <div className="stat-label">Namespaces</div>
          </div>
        </div>
      )}

      {loading ? (
        <div className="loading-state">
          <div className="spinner" />
          <span>Loading container volumes…</span>
        </div>
      ) : !notConfigured && !error && (
        <>
          <div className="filter-row">
            <input
              type="search"
              className="search-input"
              placeholder="Filter by PVC name, namespace, PV, or storage class…"
              value={search}
              onChange={e => setSearch(e.target.value)}
            />
            {search && <span className="filter-count">{filtered.length} result{filtered.length !== 1 ? 's' : ''}</span>}
          </div>

          <div className="table-container">
            <div className="table-scroll">
              {filtered.length === 0 ? (
                <div className="empty-state">
                  <div className="empty-state-icon">📦</div>
                  <div>{search ? 'No matching volumes' : 'No vSphere CSI PVCs found'}</div>
                </div>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>Namespace</th>
                      <th>PVC Name</th>
                      <th>Status</th>
                      <th>Bound PV</th>
                      <th>Capacity</th>
                      <th>Storage Class</th>
                      <th>Access Modes</th>
                      <th>Volume Handle</th>
                      <th>Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((vol, i) => (
                      <tr key={i}>
                        <td><span className="badge badge-blue">{vol.namespace}</span></td>
                        <td style={{ fontWeight: 500 }}>{vol.name}</td>
                        <td>{phaseBadge(vol.status)}</td>
                        <td className="td-mono">{vol.pv_name || '—'}</td>
                        <td><strong>{vol.capacity}</strong></td>
                        <td>{vol.storage_class}</td>
                        <td className="td-mono">{(vol.access_modes || []).join(', ')}</td>
                        <td className="td-mono" style={{ fontSize: 11 }}>{vol.volume_handle || '—'}</td>
                        <td className="td-mono">
                          {vol.created ? new Date(vol.created).toLocaleDateString() : '—'}
                        </td>
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
