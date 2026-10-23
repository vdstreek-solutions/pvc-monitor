import { useState, useEffect, useCallback, useRef } from 'react'

const STATE_META = {
  active:   { label: 'Active',   className: 'badge-green',  desc: 'Bound and mounted on a node VM' },
  idle:     { label: 'Idle',     className: 'badge-yellow', desc: 'Bound but no pod is using it' },
  released: { label: 'Released', className: 'badge-red',    desc: 'PVC released — safe to delete' },
  lost:     { label: 'Lost',     className: 'badge-red',    desc: 'PVC lost — backing storage gone' },
  failed:   { label: 'Failed',   className: 'badge-red',    desc: 'PVC failed to provision' },
  pending:  { label: 'Pending',  className: 'badge-blue',   desc: 'Waiting for storage to be provisioned' },
  unknown:  { label: 'Unknown',  className: 'badge-gray',   desc: '' },
}

function usageBadge(state) {
  const meta = STATE_META[state] || STATE_META.unknown
  return <span className={`badge ${meta.className}`}>{meta.label}</span>
}

function powerBadge(state) {
  if (state === 'poweredOn')  return <span className="badge badge-green">On</span>
  if (state === 'poweredOff') return <span className="badge badge-gray">Off</span>
  if (!state || state === '—') return null
  return <span className="badge badge-yellow">{state}</span>
}

const FILTERS = [
  { id: 'all',      label: 'All' },
  { id: 'active',   label: 'Active' },
  { id: 'idle',     label: 'Idle' },
  { id: 'unused',   label: 'Needs Attention' },
]

const UNUSED_STATES = new Set(['released', 'lost', 'failed', 'pending', 'unknown'])

export default function Overview({ onCount }) {
  const onCountRef = useRef(onCount)
  onCountRef.current = onCount

  const [data, setData]           = useState([])
  const [loading, setLoading]     = useState(true)
  const [error, setError]         = useState(null)
  const [notConfigured, setNotConfigured] = useState(false)
  const [search, setSearch]       = useState('')
  const [filter, setFilter]       = useState('all')
  const [spinning, setSpinning]   = useState(false)

  const fetchData = useCallback(async () => {
    setSpinning(true)
    setError(null)
    setNotConfigured(false)
    try {
      const res = await fetch('/api/overview')
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
      onCountRef.current(json.filter(v => UNUSED_STATES.has(v.usage_state)).length)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
      setSpinning(false)
    }
  }, [])

  useEffect(() => { fetchData() }, [fetchData])

  const afterFilter = data.filter(v => {
    if (filter === 'active')  return v.usage_state === 'active'
    if (filter === 'idle')    return v.usage_state === 'idle'
    if (filter === 'unused')  return UNUSED_STATES.has(v.usage_state)
    return true
  })

  const filtered = afterFilter.filter(v =>
    !search ||
    (v.pvc_name      || '').toLowerCase().includes(search.toLowerCase()) ||
    (v.namespace     || '').toLowerCase().includes(search.toLowerCase()) ||
    (v.pv_name       || '').toLowerCase().includes(search.toLowerCase()) ||
    (v.vm_name       || '').toLowerCase().includes(search.toLowerCase()) ||
    (v.storage_class || '').toLowerCase().includes(search.toLowerCase())
  )

  const activeCount  = data.filter(v => v.usage_state === 'active').length
  const idleCount    = data.filter(v => v.usage_state === 'idle').length
  const unusedCount  = data.filter(v => UNUSED_STATES.has(v.usage_state)).length

  return (
    <>
      <div className="section-header">
        <div>
          <div className="section-title">Volume Overview</div>
          <div className="section-subtitle">
            All vSphere CSI PVCs with their vCenter FCD status — spot unused volumes at a glance
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
            <strong>Kubernetes not configured</strong> — cannot load PVC list.
            <br />
            <span style={{ color: 'var(--text-secondary)' }}>{error}</span>
          </div>
        </div>
      )}

      {!notConfigured && error && (
        <div className="error-state"><span>⚠</span><span>{error}</span></div>
      )}

      {!loading && !notConfigured && !error && (
        <div className="stats-bar">
          <div className="stat-card">
            <div className="stat-value">{data.length}</div>
            <div className="stat-label">Total PVCs</div>
          </div>
          <div className="stat-card">
            <div className="stat-value" style={{ color: 'var(--success)' }}>{activeCount}</div>
            <div className="stat-label">Active</div>
          </div>
          <div className="stat-card">
            <div className="stat-value" style={{ color: 'var(--warning)' }}>{idleCount}</div>
            <div className="stat-label">Idle</div>
          </div>
          <div className="stat-card">
            <div
              className="stat-value"
              style={{ color: unusedCount > 0 ? 'var(--danger, #e05252)' : undefined }}
            >
              {unusedCount}
            </div>
            <div className="stat-label">Needs Attention</div>
          </div>
        </div>
      )}

      {loading ? (
        <div className="loading-state">
          <div className="spinner" />
          <span>Loading volume overview…</span>
        </div>
      ) : !notConfigured && !error && (
        <>
          {/* Filter + search row */}
          <div className="filter-row" style={{ gap: 8, flexWrap: 'wrap' }}>
            <div style={{ display: 'flex', gap: 0, background: 'var(--surface)', borderRadius: 6, border: '1px solid var(--border)', overflow: 'hidden' }}>
              {FILTERS.map(f => (
                <button
                  key={f.id}
                  onClick={() => setFilter(f.id)}
                  style={{
                    padding: '5px 14px',
                    border: 'none',
                    borderRight: '1px solid var(--border)',
                    background: filter === f.id ? 'var(--accent)' : 'transparent',
                    color: filter === f.id ? '#fff' : 'var(--text-secondary)',
                    fontSize: 12,
                    fontWeight: filter === f.id ? 600 : 400,
                    cursor: 'pointer',
                    transition: 'background .15s, color .15s',
                  }}
                >
                  {f.label}
                  {f.id === 'unused' && unusedCount > 0 && (
                    <span style={{
                      marginLeft: 5,
                      background: 'var(--danger, #e05252)',
                      color: '#fff',
                      borderRadius: 10,
                      padding: '1px 6px',
                      fontSize: 10,
                      fontWeight: 700,
                    }}>{unusedCount}</span>
                  )}
                </button>
              ))}
            </div>
            <input
              type="search"
              className="search-input"
              style={{ flex: 1, minWidth: 200 }}
              placeholder="Filter by PVC, namespace, PV, VM, or storage class…"
              value={search}
              onChange={e => setSearch(e.target.value)}
            />
            {search && (
              <span className="filter-count">
                {filtered.length} result{filtered.length !== 1 ? 's' : ''}
              </span>
            )}
          </div>

          <div className="table-container">
            <div className="table-scroll">
              {filtered.length === 0 ? (
                <div className="empty-state">
                  <div className="empty-state-icon">✓</div>
                  <div>{search ? 'No matching volumes' : 'No volumes in this category'}</div>
                </div>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>State</th>
                      <th>Namespace</th>
                      <th>PVC Name</th>
                      <th>Capacity</th>
                      <th>Storage Class</th>
                      <th>Bound PV</th>
                      <th>vSphere Node VM</th>
                      <th>VM Power</th>
                      <th>Datastore</th>
                      <th>Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((vol, i) => (
                      <tr
                        key={i}
                        style={{
                          background: UNUSED_STATES.has(vol.usage_state)
                            ? 'rgba(224, 82, 82, 0.06)'
                            : vol.usage_state === 'idle'
                              ? 'rgba(230, 180, 40, 0.06)'
                              : undefined,
                        }}
                      >
                        <td>{usageBadge(vol.usage_state)}</td>
                        <td><span className="badge badge-blue">{vol.namespace}</span></td>
                        <td style={{ fontWeight: 500 }}>{vol.pvc_name}</td>
                        <td><strong>{vol.capacity}</strong></td>
                        <td>{vol.storage_class}</td>
                        <td className="td-mono" style={{ fontSize: 11 }}>{vol.pv_name}</td>
                        <td style={{ fontWeight: vol.vm_name !== '—' ? 500 : undefined, color: vol.vm_name === '—' ? 'var(--text-secondary)' : undefined }}>
                          {vol.vm_name}
                        </td>
                        <td>{powerBadge(vol.vm_power_state)}</td>
                        <td>{vol.datastore !== '—' ? vol.datastore : <span style={{ color: 'var(--text-secondary)' }}>—</span>}</td>
                        <td className="td-mono">
                          {vol.pvc_created ? new Date(vol.pvc_created).toLocaleDateString() : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>

          {/* Legend */}
          <div style={{ display: 'flex', gap: 20, marginTop: 12, flexWrap: 'wrap' }}>
            {Object.entries(STATE_META).filter(([k]) => k !== 'unknown').map(([k, m]) => (
              <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--text-secondary)' }}>
                <span className={`badge ${m.className}`} style={{ fontSize: 11 }}>{m.label}</span>
                {m.desc}
              </div>
            ))}
          </div>
        </>
      )}
    </>
  )
}
