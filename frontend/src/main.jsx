import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './App.css'
import App from './App.jsx'

// Redirect to OIDC login on any 401 response from the API.
const _fetch = window.fetch.bind(window)
window.fetch = async (...args) => {
  const res = await _fetch(...args)
  if (res.status === 401) {
    const next = encodeURIComponent(window.location.pathname + window.location.search)
    window.location.href = `/auth/login?next=${next}`
    return new Promise(() => {})  // never resolves — prevents downstream error handling
  }
  return res
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
