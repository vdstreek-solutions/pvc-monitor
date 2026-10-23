import { useState, useEffect, useCallback } from 'react'

const FILTER_OPTS = [
  { val: 'all',         label: 'All'         },
  { val: 'vm-attached', label: 'VM Attached' },
  { val: 'unattached',  label: 'Unattached'  },
]

function formatDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

function AlertPanel({ status, testResult }) {
  if (!status) return null
  const { configured, alert_to, check_interval_hours, orphans = {} } = status
  const { last_check, last_alert, last_count, last_error } = orphans

  return (
    <div style={{
      background: configured ? 'rgba(60,180,100,0.06)' : 'var(--surface)',
      border: `1px solid ${configured ? 'rgba(60,180,100,0.3)' : 'var(--border)'}`,
      borderRadius: 8,
      padding: '12px 16px',
      marginBottom: 16,
      display: 'flex',
      alignItems: 'center',
      gap: 12,
      flexWrap: 'wrap',
    }}>
      <span style={{ fontSize: 15 }}>{configured ? '🔔' : '🔕'}</span>
      <strong style={{ fontSize: 13, whiteSpace: 'nowrap' }}>Orphan Alerts</strong>
      <span className={`badge ${configured ? 'badge-green' : 'badge-gray'}`} style={{ fontSize: 11 }}>
        {configured ? 'Configured' : 'Not configured'}
      </span>

      <div style={{ flex: 1, fontSize: 12, color: 'var(--text-secondary)' }}>
        {configured ? (
          <>
            <span>Every <strong style={{ color: 'var(--text)' }}>{check_interval_hours}h</strong></span>
            <span style={{ margin: '0 6px' }}>·</span>
            <span>To: <strong style={{ color: 'var(--text)' }}>{(alert_to || []).join(', ')}</strong></span>
            {last_check && (
              <>
                <span style={{ margin: '0 6px' }}>·</span>
                <span>Last check: {formatDate(last_check)}</span>
              </>
            )}
            {last_alert && (
              <>
                <span style={{ margin: '0 6px' }}>·</span>
                <span>Last alert: {formatDate(last_alert)}</span>
                {last_count != null && <span> ({last_count} orphaned)</span>}
              </>
            )}
            {last_error && (
              <span style={{ color: 'var(--danger, #e05252)', marginLeft: 8 }}>⚠ {last_error}</span>
            )}
          </>
        ) : (
          <span>
            Set <code>SMTP_HOST</code>, <code>SMTP_FROM</code>, and <code>ALERT_EMAIL_TO</code> to enable.
          </span>
        )}
      </div>

      {testResult && (
        <span style={{
          fontSize: 12, fontWeight: 500, whiteSpace: 'nowrap',
          color: testResult.ok ? 'var(--success, #3cb464)' : 'var(--danger, #e05252)',
        }}>
          {testResult.ok ? '✓' : '✗'} {testResult.msg}
        </span>
      )}
    </div>
  )
}

export default function OrphanedFCDs({ onCount }) {
  const [items, setItems]         = useState([])
  const [loading, setLoading]     = useState(true)
  const [error, setError]         = useState(null)
  const [spinning, setSpinning]   = useState(false)
  const [search, setSearch]       = useState('')
  const [filter, setFilter]       = useState('all')
  const [alertStatus, setAlertStatus] = useState(null)
  const [testing, setTesting]     = useState(false)
  const [testResult, setTestResult]   = useState(null)

  const fetchData = useCallback(async () => {
    setSpinning(true)
    setError(null)
    try {
      const res = await fetch('/api/orphaned-fcds')
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || res.statusText)
      }
      const json = await res.json()
      setItems(json)
      onCount?.(json.length)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
      setSpinning(false)
    }
  }, [])

  const fetchAlertStatus = useCallback(async () => {
    try {
      const res = await fetch('/api/alerts/status')
      setAlertStatus(res.ok ? await res.json() : null)
    } catch {
      setAlertStatus(null)
    }
  }, [])

  useEffect(() => {
    fetchData()
    fetchAlertStatus()
  }, [fetchData, fetchAlertStatus])

  const sendTestAlert = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      const res = await fetch('/api/alerts/orphan-test', { method: 'POST' })
      const data = await res.json()
      if (!res.ok) {
        setTestResult({ ok: false, msg: data.detail || 'Failed' })
      } else if (data.sent) {
        setTestResult({
          ok: true,
          msg: `Sent to ${(data.recipients || []).join(', ')} · ${data.count} orphaned FCD${data.count !== 1 ? 's' : ''}`,
        })
      } else {
        setTestResult({ ok: false, msg: data.reason || 'Not sent' })
      }
    } catch (e) {
      setTestResult({ ok: false, msg: e.message })
    } finally {
      setTesting(false)
      fetchAlertStatus()
    }
  }

  const totalGB    = items.reduce((s, i) => s + (i.capacity_gb || 0), 0)
  const vmAttached = items.filter(i => i.vm_name && i.vm_name !== '—').length

  const afterFilter = items.filter(i => {
    if (filter === 'vm-attached') return i.vm_name && i.vm_name !== '—'
    if (filter === 'unattached')  return !i.vm_name || i.vm_name === '—'
    return true
  })

  const q = search.toLowerCase()
  const visible = q
    ? afterFilter.filter(i =>
        (i.fcd_id    || '').toLowerCase().includes(q) ||
        (i.fcd_name  || '').toLowerCase().includes(q) ||
        (i.datastore || '').toLowerCase().includes(q) ||
        (i.vm_name   || '').toLowerCase().includes(q) ||
        (i.file_path || '').toLowerCase().includes(q)
      )
    : afterFilter

  return (
    <section>
      <div className="section-header">
        <div>
          <div className="section-title">Orphaned FCDs</div>
          <div className="section-subtitle">
            vSphere First Class Disks with no corresponding Kubernetes PersistentVolume — likely leaked volumes
            (PV deleted with Retain policy) or pre-existing non-CSI disks.
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button
            className="btn"
            onClick={sendTestAlert}
            disabled={testing}
            title="Send a test orphan alert email now"
          >
            {testing ? '…' : '✉ Test Alert'}
          </button>
          <button className="btn" onClick={fetchData} disabled={spinning}>
            {spinning ? <span className="spin-icon">↻</span> : '↻'} Refresh
          </button>
        </div>
      </div>

      <AlertPanel status={alertStatus} testResult={testResult} />

      {!loading && !error && (
        <div className="stats-bar">
          <div className="stat-card" style={items.length > 0 ? { borderColor: 'rgba(248,81,73,.5)' } : undefined}>
            <div className="stat-value" style={items.length > 0 ? { color: 'var(--danger)' } : undefined}>
              {items.length}
            </div>
            <div className="stat-label">Orphaned FCDs</div>
          </div>
          <div className="stat-card" style={totalGB > 0 ? { borderColor: 'rgba(210,153,34,.5)' } : undefined}>
            <div className="stat-value" style={totalGB > 0 ? { color: 'var(--warning)' } : undefined}>
              {totalGB.toFixed(1)} GB
            </div>
            <div className="stat-label">Wasted capacity</div>
          </div>
          <div
            className="stat-card"
            style={vmAttached > 0 ? { borderColor: 'rgba(210,153,34,.5)' } : undefined}
            title="VM-attached FCDs may still be in use outside of Kubernetes"
          >
            <div className="stat-value" style={vmAttached > 0 ? { color: 'var(--warning)' } : undefined}>
              {vmAttached}
            </div>
            <div className="stat-label">VM attached</div>
          </div>
        </div>
      )}

      {loading && (
        <div className="loading-state">
          <div className="spinner" />
          Loading orphaned FCDs…
        </div>
      )}

      {error && (
        <div className="error-state">
          <span>⚠</span>
          <span>{error}</span>
        </div>
      )}

      {!loading && !error && (
        <>
          {/* Filter pills */}
          <div style={{ display: 'flex', gap: 6, marginBottom: 12, flexWrap: 'wrap' }}>
            {FILTER_OPTS.map(({ val, label }) => {
              const count =
                val === 'all'         ? items.length :
                val === 'vm-attached' ? vmAttached   :
                items.length - vmAttached
              const isActive = filter === val
              const color =
                val === 'vm-attached' ? 'var(--warning)' :
                val === 'unattached'  ? 'var(--success)'  :
                'var(--text-secondary)'
              return (
                <button
                  key={val}
                  onClick={() => setFilter(val)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    padding: '4px 12px',
                    border: `1px solid ${isActive ? color : 'var(--border)'}`,
                    borderRadius: 20,
                    background: isActive ? `color-mix(in srgb, ${color} 12%, transparent)` : 'transparent',
                    color: isActive ? color : 'var(--text-secondary)',
                    fontSize: 12, fontWeight: isActive ? 600 : 400,
                    cursor: 'pointer', transition: 'all .15s',
                  }}
                >
                  {label}
                  <span style={{
                    background: 'var(--bg-primary)', padding: '0 5px', borderRadius: 8,
                    fontSize: 11, color: 'var(--text-secondary)', border: '1px solid var(--border)',
                  }}>
                    {count}
                  </span>
                </button>
              )
            })}
          </div>

          <div className="filter-row">
            <input
              type="search"
              className="search-input"
              placeholder="Search FCD ID, name, datastore, VM…"
              value={search}
              onChange={e => setSearch(e.target.value)}
              style={{ width: 340 }}
            />
            {search && (
              <span className="filter-count">
                {visible.length} result{visible.length !== 1 ? 's' : ''}
              </span>
            )}
          </div>

          {items.length === 0 ? (
            <div className="empty-state">
              <div className="empty-state-icon">✓</div>
              <div>No orphaned FCDs found — all vSphere FCDs are claimed by a Kubernetes PV.</div>
            </div>
          ) : visible.length === 0 ? (
            <div className="empty-state">
              <div className="empty-state-icon">🔍</div>
              <div>No FCDs match the current filter.</div>
            </div>
          ) : (
            <div className="table-container">
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>FCD ID</th>
                      <th>Name</th>
                      <th>Datastore</th>
                      <th>Size (GB)</th>
                      <th>File Path</th>
                      <th>VM Attached</th>
                      <th>Power</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map(item => {
                      const attached = item.vm_name && item.vm_name !== '—'
                      return (
                        <tr
                          key={item.fcd_id}
                          style={attached ? { background: 'rgba(210,153,34,.06)' } : undefined}
                        >
                          <td className="td-mono" style={{ fontSize: 11 }}>{item.fcd_id}</td>
                          <td>{item.fcd_name}</td>
                          <td>{item.datastore}</td>
                          <td>
                            <span className={`badge ${item.capacity_gb >= 100 ? 'badge-red' : item.capacity_gb >= 20 ? 'badge-yellow' : 'badge-blue'}`}>
                              {item.capacity_gb} GB
                            </span>
                          </td>
                          <td
                            className="td-mono"
                            style={{ fontSize: 11, maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                            title={item.file_path}
                          >
                            {item.file_path}
                          </td>
                          <td>
                            {attached
                              ? <span className="badge badge-yellow" title="Attached to a VM — may still be in use">{item.vm_name}</span>
                              : <span style={{ color: 'var(--text-secondary)' }}>—</span>}
                          </td>
                          <td>
                            {attached ? <PowerBadge state={item.vm_power_state} /> : <span style={{ color: 'var(--text-secondary)' }}>—</span>}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  )
}

function PowerBadge({ state }) {
  const s = (state || '').toLowerCase()
  if (s === 'poweredon')  return <span className="badge badge-green">on</span>
  if (s === 'poweredoff') return <span className="badge badge-red">off</span>
  if (s === 'suspended')  return <span className="badge badge-yellow">suspended</span>
  return <span className="badge badge-gray">{state || '—'}</span>
}
