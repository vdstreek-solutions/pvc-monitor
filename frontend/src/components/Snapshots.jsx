import { useState, useEffect, useCallback, useRef } from 'react'

function ageDays(iso) {
  if (!iso) return null
  return Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000)
}

function ageBadge(iso) {
  const d = ageDays(iso)
  if (d === null) return <span className="badge badge-gray">Unknown</span>
  if (d > 30)  return <span className="badge badge-red">{d}d old</span>
  if (d > 7)   return <span className="badge badge-yellow">{d}d old</span>
  if (d === 0) return <span className="badge badge-blue">Today</span>
  return <span className="badge badge-green">{d}d old</span>
}

function formatDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

function AlertPanel({ status, testResult }) {
  if (!status) return null

  const { configured, alert_to, age_threshold_days, check_interval_hours,
          last_check, last_alert, last_old_snap_count, last_error } = status

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
      <strong style={{ fontSize: 13, whiteSpace: 'nowrap' }}>Email Alerts</strong>
      <span className={`badge ${configured ? 'badge-green' : 'badge-gray'}`} style={{ fontSize: 11 }}>
        {configured ? 'Configured' : 'Not configured'}
      </span>

      <div style={{ flex: 1, fontSize: 12, color: 'var(--text-secondary)' }}>
        {configured ? (
          <>
            <span>Threshold: <strong style={{ color: 'var(--text)' }}>{age_threshold_days}d</strong></span>
            <span style={{ margin: '0 6px' }}>·</span>
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
                {last_old_snap_count != null && (
                  <span> ({last_old_snap_count} old)</span>
                )}
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
          fontSize: 12,
          fontWeight: 500,
          color: testResult.ok ? 'var(--success, #3cb464)' : 'var(--danger, #e05252)',
          whiteSpace: 'nowrap',
        }}>
          {testResult.ok ? '✓' : '✗'} {testResult.msg}
        </span>
      )}
    </div>
  )
}

export default function Snapshots({ onCount }) {
  const onCountRef = useRef(onCount)
  onCountRef.current = onCount

  const [data, setData]           = useState([])
  const [loading, setLoading]     = useState(true)
  const [error, setError]         = useState(null)
  const [search, setSearch]       = useState('')
  const [spinning, setSpinning]   = useState(false)
  const [alertStatus, setAlertStatus] = useState(null)
  const [testing, setTesting]     = useState(false)
  const [testResult, setTestResult] = useState(null)

  const fetchData = useCallback(async () => {
    setSpinning(true)
    setError(null)
    try {
      const res = await fetch('/api/snapshots')
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
      const res = await fetch('/api/alerts/test', { method: 'POST' })
      const data = await res.json()
      if (!res.ok) {
        setTestResult({ ok: false, msg: data.detail || 'Failed' })
      } else if (data.sent) {
        setTestResult({
          ok: true,
          msg: `Sent to ${(data.recipients || []).join(', ')} · ${data.count} old snapshot${data.count !== 1 ? 's' : ''}`,
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

  const filtered = data.filter(s =>
    !search ||
    (s.pvc_name    || '').toLowerCase().includes(search.toLowerCase()) ||
    (s.pv_name     || '').toLowerCase().includes(search.toLowerCase()) ||
    (s.snapshot_id || '').toLowerCase().includes(search.toLowerCase()) ||
    (s.description || '').toLowerCase().includes(search.toLowerCase()) ||
    (s.datastore   || '').toLowerCase().includes(search.toLowerCase())
  )

  const uniquePVCs = new Set(data.map(s => s.pvc_name)).size
  const oldSnaps   = data.filter(s => (ageDays(s.created) ?? 0) > 7).length

  return (
    <>
      <div className="section-header">
        <div>
          <div className="section-title">Snapshots</div>
          <div className="section-subtitle">
            FCD-level snapshots on vSphere CSI volumes (created via Kubernetes VolumeSnapshot)
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button
            className="btn"
            onClick={sendTestAlert}
            disabled={testing}
            title="Send a test alert email now"
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
          <div className="stat-card">
            <div className="stat-value">{data.length}</div>
            <div className="stat-label">Total Snapshots</div>
          </div>
          <div className="stat-card">
            <div className="stat-value">{uniquePVCs}</div>
            <div className="stat-label">PVCs with Snapshots</div>
          </div>
          <div className="stat-card">
            <div
              className="stat-value"
              style={{ color: oldSnaps > 0 ? 'var(--warning)' : undefined }}
            >
              {oldSnaps}
            </div>
            <div className="stat-label">Older than 7 Days</div>
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
          <span>Loading snapshots…</span>
        </div>
      ) : !error && (
        <>
          <div className="filter-row">
            <input
              type="search"
              className="search-input"
              placeholder="Filter by PVC, PV, snapshot ID, or datastore…"
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
                  <div className="empty-state-icon">📷</div>
                  <div>{search ? 'No matching snapshots' : 'No FCD snapshots found'}</div>
                </div>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>PVC Name</th>
                      <th>PV Name</th>
                      <th>Snapshot ID</th>
                      <th>Description</th>
                      <th>Storage Class</th>
                      <th>Datastore</th>
                      <th>Created</th>
                      <th>Age</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((snap, i) => (
                      <tr
                        key={i}
                        style={{
                          background: (ageDays(snap.created) ?? 0) > 30
                            ? 'rgba(224,82,82,0.05)'
                            : (ageDays(snap.created) ?? 0) > 7
                              ? 'rgba(230,180,40,0.05)'
                              : undefined,
                        }}
                      >
                        <td style={{ fontWeight: 500 }}>{snap.pvc_name}</td>
                        <td className="td-mono" style={{ fontSize: 11 }}>{snap.pv_name}</td>
                        <td className="td-mono" style={{ fontSize: 11 }}>{snap.snapshot_id}</td>
                        <td style={{ color: 'var(--text-secondary)' }}>{snap.description || '—'}</td>
                        <td>{snap.storage_class}</td>
                        <td>{snap.datastore}</td>
                        <td className="td-mono">{formatDate(snap.created)}</td>
                        <td>{ageBadge(snap.created)}</td>
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
