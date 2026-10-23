import { useState, useEffect, useCallback, useRef } from 'react'

function phaseBadge(phase) {
  if (phase === 'Bound')     return <span className="badge badge-green">{phase}</span>
  if (phase === 'Pending')   return <span className="badge badge-yellow">{phase}</span>
  if (phase === 'Released')  return <span className="badge badge-blue">{phase}</span>
  if (phase === 'Failed')    return <span className="badge badge-red">{phase}</span>
  return <span className="badge badge-gray">{phase || '—'}</span>
}

function reclaimBadge(policy) {
  if (policy === 'Retain')  return <span className="badge badge-blue">{policy}</span>
  if (policy === 'Delete')  return <span className="badge badge-yellow">{policy}</span>
  return <span className="badge badge-gray">{policy || '—'}</span>
}

function useTabData(url) {
  const [data, setData]             = useState([])
  const [loading, setLoading]       = useState(true)
  const [error, setError]           = useState(null)
  const [notConfigured, setNotConfigured] = useState(false)
  const [spinning, setSpinning]     = useState(false)

  const fetch_ = useCallback(async () => {
    setSpinning(true)
    setError(null)
    setNotConfigured(false)
    try {
      const res = await fetch(url)
      if (res.status === 503) {
        const err = await res.json()
        setNotConfigured(true)
        setError(err.detail || 'K8s not configured')
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
  return { data, loading, error, notConfigured, spinning, refresh: fetch_ }
}

export default function K8sVolumes({ onCount }) {
  const onCountRef = useRef(onCount)
  onCountRef.current = onCount

  const [subTab, setSubTab]           = useState('pvs')
  const [search, setSearch]           = useState('')
  const [statusFilter, setStatusFilter] = useState('all')

  const pvs  = useTabData('/api/k8s/persistent-volumes')
  const pvcs = useTabData('/api/k8s/persistent-volume-claims')

  useEffect(() => {
    onCountRef.current(pvs.data.length + pvcs.data.length)
  }, [pvs.data.length, pvcs.data.length])

  const active = subTab === 'pvs' ? pvs : pvcs

  // Reset filters when switching sub-tabs
  const switchTab = (id) => { setSubTab(id); setSearch(''); setStatusFilter('all') }

  const afterStatus = (items) =>
    statusFilter === 'all' ? items : items.filter(v => (v.status || '') === statusFilter)

  const filteredPVs = afterStatus(pvs.data).filter(v =>
    !search ||
    v.name.toLowerCase().includes(search.toLowerCase()) ||
    (v.driver        || '').toLowerCase().includes(search.toLowerCase()) ||
    (v.volume_handle || '').toLowerCase().includes(search.toLowerCase()) ||
    (v.claim_ref     || '').toLowerCase().includes(search.toLowerCase())
  )

  const filteredPVCs = afterStatus(pvcs.data).filter(v =>
    !search ||
    v.name.toLowerCase().includes(search.toLowerCase()) ||
    (v.namespace    || '').toLowerCase().includes(search.toLowerCase()) ||
    (v.volume_name  || '').toLowerCase().includes(search.toLowerCase())
  )

  const filtered = subTab === 'pvs' ? filteredPVs : filteredPVCs

  // Unique statuses in the current sub-tab's full dataset (before status filter)
  const sourceData   = subTab === 'pvs' ? pvs.data : pvcs.data
  const statusCounts = sourceData.reduce((acc, v) => {
    const s = v.status || '—'
    acc[s] = (acc[s] || 0) + 1
    return acc
  }, {})

  return (
    <>
      <div className="section-header">
        <div>
          <div className="section-title">Kubernetes Volumes</div>
          <div className="section-subtitle">All PersistentVolumes and PVCs in the cluster</div>
        </div>
        <button className="btn" onClick={active.refresh} disabled={active.spinning}>
          {active.spinning ? <span className="spin-icon">↻</span> : '↻'} Refresh
        </button>
      </div>

      {!active.loading && !active.error && (
        <div className="stats-bar">
          <div className="stat-card">
            <div className="stat-value">{pvs.data.length}</div>
            <div className="stat-label">PersistentVolumes</div>
          </div>
          <div className="stat-card">
            <div className="stat-value">{pvcs.data.length}</div>
            <div className="stat-label">PVC Claims</div>
          </div>
          <div className="stat-card">
            <div className="stat-value">
              {pvs.data.filter(v => v.status === 'Bound').length}
            </div>
            <div className="stat-label">Bound PVs</div>
          </div>
          <div className="stat-card">
            <div className="stat-value">
              {new Set(pvcs.data.map(v => v.namespace)).size}
            </div>
            <div className="stat-label">Namespaces</div>
          </div>
        </div>
      )}

      {/* Sub-tabs */}
      <div style={{ display: 'flex', gap: 0, marginBottom: 12, borderBottom: '1px solid var(--border)' }}>
        {[['pvs', `PersistentVolumes (${pvs.data.length})`], ['pvcs', `Claims (${pvcs.data.length})`]].map(([id, label]) => (
          <button
            key={id}
            onClick={() => switchTab(id)}
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

      {/* Status filter buttons */}
      {!active.loading && !active.error && !active.notConfigured && sourceData.length > 0 && (
        <div style={{ display: 'flex', gap: 6, marginBottom: 12, flexWrap: 'wrap' }}>
          {[['all', 'All', sourceData.length], ...Object.entries(statusCounts).sort()].map(([val, label, count]) => {
            const c = val === 'all' ? sourceData.length : statusCounts[val] ?? 0
            const isActive = statusFilter === val
            const color =
              val === 'Bound'    ? 'var(--success, #3cb464)' :
              val === 'Pending'  ? 'var(--warning, #e6b428)' :
              val === 'Released' ? 'var(--accent)'           :
              val === 'Failed'   ? 'var(--danger, #e05252)'  :
              val === 'Lost'     ? 'var(--danger, #e05252)'  :
              'var(--text-secondary)'
            return (
              <button
                key={val}
                onClick={() => setStatusFilter(val)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 6,
                  padding: '4px 12px',
                  border: `1px solid ${isActive ? color : 'var(--border)'}`,
                  borderRadius: 20,
                  background: isActive ? `color-mix(in srgb, ${color} 12%, transparent)` : 'var(--surface)',
                  color: isActive ? color : 'var(--text-secondary)',
                  fontSize: 12,
                  fontWeight: isActive ? 600 : 400,
                  cursor: 'pointer',
                  transition: 'all .15s',
                }}
              >
                {val === 'all' ? 'All' : val}
                <span style={{
                  background: isActive ? color : 'var(--border)',
                  color: isActive ? '#fff' : 'var(--text-secondary)',
                  borderRadius: 10,
                  padding: '1px 6px',
                  fontSize: 10,
                  fontWeight: 700,
                  minWidth: 18,
                  textAlign: 'center',
                }}>
                  {c}
                </span>
              </button>
            )
          })}
        </div>
      )}

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

      {!active.notConfigured && active.error && (
        <div className="error-state">
          <span>⚠</span>
          <span>{active.error}</span>
        </div>
      )}

      {active.loading ? (
        <div className="loading-state">
          <div className="spinner" />
          <span>Loading {subTab === 'pvs' ? 'PersistentVolumes' : 'PVCs'}…</span>
        </div>
      ) : !active.notConfigured && !active.error && (
        <>
          <div className="filter-row">
            <input
              type="search"
              className="search-input"
              placeholder={subTab === 'pvs' ? 'Filter by name, volume handle, or claim…' : 'Filter by name or namespace…'}
              value={search}
              onChange={e => setSearch(e.target.value)}
            />
            {(search || statusFilter !== 'all') && (
              <span className="filter-count">{filtered.length} result{filtered.length !== 1 ? 's' : ''}</span>
            )}
          </div>

          <div className="table-container">
            <div className="table-scroll">
              {filtered.length === 0 ? (
                <div className="empty-state">
                  <div className="empty-state-icon">☸</div>
                  <div>{search ? 'No matching resources' : `No ${subTab === 'pvs' ? 'PersistentVolumes' : 'PVCs'} found in the cluster`}</div>
                </div>
              ) : subTab === 'pvs' ? (
                <table>
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Driver</th>
                      <th>Volume Handle</th>
                      <th>Capacity</th>
                      <th>Access Modes</th>
                      <th>Status</th>
                      <th>Reclaim Policy</th>
                      <th>Storage Class</th>
                      <th>Claim</th>
                      <th>FS Type</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredPVs.map((pv, i) => (
                      <tr key={i}>
                        <td style={{ fontWeight: 500 }}>{pv.name}</td>
                        <td className="td-mono">{pv.driver || '—'}</td>
                        <td className="td-mono">{pv.volume_handle || '—'}</td>
                        <td><strong>{pv.capacity}</strong></td>
                        <td className="td-mono">{(pv.access_modes || []).join(', ')}</td>
                        <td>{phaseBadge(pv.status)}</td>
                        <td>{reclaimBadge(pv.reclaim_policy)}</td>
                        <td>{pv.storage_class}</td>
                        <td className="td-mono">{pv.claim_ref || '—'}</td>
                        <td><span className="badge badge-gray">{pv.fs_type || '—'}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Namespace</th>
                      <th>Status</th>
                      <th>Volume</th>
                      <th>Capacity</th>
                      <th>Access Modes</th>
                      <th>Storage Class</th>
                      <th>Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredPVCs.map((pvc, i) => (
                      <tr key={i}>
                        <td style={{ fontWeight: 500 }}>{pvc.name}</td>
                        <td><span className="badge badge-blue">{pvc.namespace}</span></td>
                        <td>{phaseBadge(pvc.status)}</td>
                        <td className="td-mono">{pvc.volume_name}</td>
                        <td><strong>{pvc.capacity}</strong></td>
                        <td className="td-mono">{(pvc.access_modes || []).join(', ')}</td>
                        <td>{pvc.storage_class}</td>
                        <td className="td-mono">{pvc.created ? new Date(pvc.created).toLocaleDateString() : '—'}</td>
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
