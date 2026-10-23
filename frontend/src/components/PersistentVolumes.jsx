import { useState, useEffect, useCallback, useRef } from 'react'

function powerBadge(state) {
  if (state === 'poweredOn')  return <span className="badge badge-green">On</span>
  if (state === 'poweredOff') return <span className="badge badge-gray">Off</span>
  return <span className="badge badge-yellow">{state}</span>
}

function k8sBadge(status) {
  if (status === 'Bound')    return <span className="badge badge-green">Bound</span>
  if (status === 'Released') return <span className="badge badge-blue">Released</span>
  if (status === 'Failed')   return <span className="badge badge-red">Failed</span>
  return <span className="badge badge-gray">{status || '—'}</span>
}

export default function PersistentVolumes({ onCount }) {
  const onCountRef = useRef(onCount)
  onCountRef.current = onCount

  const [data, setData]         = useState([])
  const [loading, setLoading]   = useState(true)
  const [error, setError]       = useState(null)
  const [search, setSearch]     = useState('')
  const [spinning, setSpinning] = useState(false)

  const fetchData = useCallback(async () => {
    setSpinning(true)
    setError(null)
    try {
      const res = await fetch('/api/persistent-volumes')
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
    (v.pv_name      || '').toLowerCase().includes(search.toLowerCase()) ||
    (v.claim        || '').toLowerCase().includes(search.toLowerCase()) ||
    (v.vm_name      || '').toLowerCase().includes(search.toLowerCase()) ||
    (v.datastore    || '').toLowerCase().includes(search.toLowerCase()) ||
    (v.storage_class|| '').toLowerCase().includes(search.toLowerCase())
  )

  const totalCapacity = data.reduce((sum, v) => sum + (v.capacity_gb || 0), 0)

  return (
    <>
      <div className="section-header">
        <div>
          <div className="section-title">Persistent Volumes</div>
          <div className="section-subtitle">
            vSphere VMDKs matched to Kubernetes PVs via PV name — only visible while a pod has the volume attached
          </div>
        </div>
        <button className="btn" onClick={fetchData} disabled={spinning}>
          {spinning ? <span className="spin-icon">↻</span> : '↻'} Refresh
        </button>
      </div>

      {!loading && !error && (
        <div className="stats-bar">
          <div className="stat-card">
            <div className="stat-value">{data.length}</div>
            <div className="stat-label">Attached Volumes</div>
          </div>
          <div className="stat-card">
            <div className="stat-value">{totalCapacity.toFixed(1)} GB</div>
            <div className="stat-label">Total Capacity</div>
          </div>
          <div className="stat-card">
            <div className="stat-value">{data.filter(v => v.vm_power_state === 'poweredOn').length}</div>
            <div className="stat-label">On Powered-On VMs</div>
          </div>
          <div className="stat-card">
            <div className="stat-value">{new Set(data.map(v => v.vm_name)).size}</div>
            <div className="stat-label">Node VMs</div>
          </div>
        </div>
      )}

      {error && (
        <div className="error-state">
          <span>⚠</span>
          <span>{error}</span>
        </div>
      )}

      {loading ? (
        <div className="loading-state">
          <div className="spinner" />
          <span>Loading persistent volumes…</span>
        </div>
      ) : !error && (
        <>
          <div className="filter-row">
            <input
              type="search"
              className="search-input"
              placeholder="Filter by PV name, claim, VM, datastore, or storage class…"
              value={search}
              onChange={e => setSearch(e.target.value)}
            />
            {search && <span className="filter-count">{filtered.length} result{filtered.length !== 1 ? 's' : ''}</span>}
          </div>

          <div className="table-container">
            <div className="table-scroll">
              {filtered.length === 0 ? (
                <div className="empty-state">
                  <div className="empty-state-icon">💾</div>
                  <div>
                    {search
                      ? 'No matching volumes'
                      : 'No vSphere CSI volumes found attached to any node VM'}
                  </div>
                  {!search && (
                    <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 8, maxWidth: 420, textAlign: 'center' }}>
                      Volumes only appear here while a pod has them mounted. If all pods are stopped the VMDKs will be detached from the node VMs.
                    </div>
                  )}
                </div>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>PV Name</th>
                      <th>PVC Claim</th>
                      <th>K8s Status</th>
                      <th>Capacity</th>
                      <th>Storage Class</th>
                      <th>Datastore</th>
                      <th>File Path</th>
                      <th>Node VM</th>
                      <th>VM Power</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((vol, i) => (
                      <tr key={i}>
                        <td className="td-mono" style={{ fontWeight: 500 }}>{vol.pv_name || '—'}</td>
                        <td className="td-mono">{vol.claim || '—'}</td>
                        <td>{k8sBadge(vol.k8s_status)}</td>
                        <td><strong>{vol.capacity_gb} GB</strong></td>
                        <td>{vol.storage_class || '—'}</td>
                        <td>{vol.datastore}</td>
                        <td className="td-mono" style={{ fontSize: 11 }}>{vol.file_path}</td>
                        <td style={{ fontWeight: 500 }}>{vol.vm_name}</td>
                        <td>{powerBadge(vol.vm_power_state)}</td>
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
