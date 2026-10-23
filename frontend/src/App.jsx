import { useState, useEffect, useCallback } from 'react'

function K8sWheelIcon({ size = 20 }) {
  const cx = 50, cy = 50
  const ringR = 44
  const hubR = 8
  const dotR = 5.5
  const angles = Array.from({ length: 7 }, (_, i) =>
    ((90 + (360 / 7) * i) * Math.PI) / 180
  )
  return (
    <svg
      width={size} height={size} viewBox="0 0 100 100"
      fill="none" xmlns="http://www.w3.org/2000/svg"
      style={{ display: 'inline-block', verticalAlign: 'middle' }}
    >
      <circle cx={cx} cy={cy} r={ringR} stroke="currentColor" strokeWidth="6" />
      {angles.map((a, i) => {
        const sx = cx + (hubR + 3) * Math.cos(a),  sy = cy - (hubR + 3) * Math.sin(a)
        const ex = cx + (ringR - dotR - 2) * Math.cos(a), ey = cy - (ringR - dotR - 2) * Math.sin(a)
        const dx = cx + ringR * Math.cos(a), dy = cy - ringR * Math.sin(a)
        return (
          <g key={i}>
            <line x1={sx} y1={sy} x2={ex} y2={ey} stroke="currentColor" strokeWidth="5" strokeLinecap="round" />
            <circle cx={dx} cy={dy} r={dotR} fill="currentColor" />
          </g>
        )
      })}
      <circle cx={cx} cy={cy} r={hubR} fill="currentColor" />
    </svg>
  )
}
import Overview from './components/Overview'
import PersistentVolumes from './components/PersistentVolumes'
import ContainerVolumes from './components/ContainerVolumes'
import Snapshots from './components/Snapshots'
import OrphanedFCDs from './components/OrphanedFCDs'
import K8sVolumes from './components/K8sVolumes'
import K8sSnapshots from './components/K8sSnapshots'

const TABS = [
  { id: 'overview',           label: 'Overview',           group: 'main'    },
  { id: 'persistent-volumes', label: 'Persistent Volumes', group: 'vsphere' },
  { id: 'container-volumes',  label: 'Container Volumes',  group: 'vsphere' },
  { id: 'snapshots',          label: 'Snapshots',          group: 'vsphere' },
  { id: 'orphaned-fcds',      label: 'Orphaned FCDs',      group: 'vsphere' },
  { id: 'k8s-volumes',        label: 'K8s Volumes',        group: 'k8s'     },
  { id: 'k8s-snapshots',      label: 'K8s Snapshots',      group: 'k8s'     },
]

function useHealth(url) {
  const [health, setHealth] = useState({ status: 'checking' })
  useEffect(() => {
    const check = async () => {
      try {
        const res  = await fetch(url)
        const data = await res.json()
        setHealth({ status: data.connected ? 'connected' : 'disconnected', ...data })
      } catch {
        setHealth({ status: 'disconnected', error: 'Cannot reach backend' })
      }
    }
    check()
  }, [url])
  return health
}

function useUser() {
  const [user, setUser] = useState(null)
  useEffect(() => {
    fetch('/auth/user').then(r => r.json()).then(setUser).catch(() => {})
  }, [])
  return user
}

export default function App() {
  const [activeTab, setActiveTab] = useState('overview')
  const [counts, setCounts] = useState({})

  const vcHealth  = useHealth('/api/health')
  const k8sHealth = useHealth('/api/k8s/health')
  const user      = useUser()

  const makeCount = useCallback(
    id => n => setCounts(c => ({ ...c, [id]: n })),
    []
  )

  return (
    <div>
      <header className="header">
        <span className="header-logo"><K8sWheelIcon size={20} /></span>
        <span className="header-title">vCenter Monitor</span>
        <HealthBadge health={vcHealth}  label="vSphere" />
        <HealthBadge health={k8sHealth} label="K8s"     />
        <UserBadge user={user} />
      </header>

      <nav className="tabs">
        {TABS.map((tab, i) => {
          const prevGroup = i > 0 ? TABS[i - 1].group : tab.group
          const count = counts[tab.id]
          // Overview and orphaned-fcds badges are red when > 0 (needs attention)
          const isAlert = (tab.id === 'overview' || tab.id === 'orphaned-fcds') && count > 0
          return (
            <button
              key={tab.id}
              className={`tab${activeTab === tab.id ? ' active' : ''}${tab.group !== prevGroup ? ' tab-group-start' : ''}`}
              onClick={() => setActiveTab(tab.id)}
            >
              {tab.label}
              {count != null && (
                <span
                  className="tab-count"
                  style={isAlert ? { background: 'var(--danger, #e05252)', color: '#fff' } : undefined}
                >
                  {count}
                </span>
              )}
            </button>
          )
        })}
      </nav>

      <main className="content">
        {activeTab === 'overview'           && <Overview          onCount={makeCount('overview')} />}
        {activeTab === 'persistent-volumes' && <PersistentVolumes onCount={makeCount('persistent-volumes')} />}
        {activeTab === 'container-volumes'  && <ContainerVolumes  onCount={makeCount('container-volumes')} />}
        {activeTab === 'snapshots'          && <Snapshots         onCount={makeCount('snapshots')} />}
        {activeTab === 'orphaned-fcds'      && <OrphanedFCDs      onCount={makeCount('orphaned-fcds')} />}
        {activeTab === 'k8s-volumes'        && <K8sVolumes        onCount={makeCount('k8s-volumes')} />}
        {activeTab === 'k8s-snapshots'      && <K8sSnapshots      onCount={makeCount('k8s-snapshots')} />}
      </main>
    </div>
  )
}

function UserBadge({ user }) {
  if (!user || !user.auth_enabled) return null
  if (!user.authenticated) {
    return (
      <a href="/auth/login" className="status-badge not-configured" style={{ textDecoration: 'none' }}>
        Sign in
      </a>
    )
  }
  const initial = (user.name || user.email || '?')[0].toUpperCase()
  return (
    <div className="header-user">
      <span className="header-user-avatar">{initial}</span>
      <span className="header-user-name">{user.name || user.email}</span>
      <a href="/auth/logout" className="header-signout">Sign out</a>
    </div>
  )
}

function HealthBadge({ health, label }) {
  if (health.status === 'checking') {
    return (
      <span className="status-badge checking">
        <span className="status-dot pulse" /> {label}: connecting…
      </span>
    )
  }
  if (health.status === 'connected') {
    const detail = label === 'vSphere'
      ? `${health.vcenter_host} — ${health.full_name}`
      : health.git_version
    return (
      <span className="status-badge connected">
        <span className="status-dot" /> {label}: {detail}
      </span>
    )
  }
  if (health.configured === false) {
    return (
      <span className="status-badge not-configured">
        {label}: not configured
      </span>
    )
  }
  return (
    <span className="status-badge disconnected">
      <span className="status-dot" /> {label}: {health.error || 'disconnected'}
    </span>
  )
}
