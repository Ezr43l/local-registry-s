import { useCallback, useEffect, useState } from 'react'
import {
  APP_VERSION, fetchAll, fetchAuth, fetchSetup, formatWhen, logout,
  type AuthStatus, type SetupStatus, type Snapshot,
} from './api'
import { StatCards } from './components/StatCards'
import { ProjectTable } from './components/ProjectTable'
import { DriftPanel } from './components/DriftPanel'
import { MaintenancePanel } from './components/MaintenancePanel'
import { SetupPanel } from './components/SetupPanel'
import { AuthPanel } from './components/AuthPanel'

export default function App() {
  const [datos, setDatos] = useState<Snapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [cargando, setCargando] = useState(false)
  const [setup, setSetup] = useState<SetupStatus | null>(null)
  const [auth, setAuth] = useState<AuthStatus | null>(null)
  const [view, setView] = useState<'dashboard' | 'settings'>('dashboard')

  const cargar = useCallback(async (refresh = false) => {
    setCargando(true)
    try {
      setDatos(await fetchAll(refresh))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'No se pudo consultar el servicio')
    } finally {
      setCargando(false)
    }
  }, [])

  const iniciarPanel = useCallback(() => {
    void fetchSetup().then((status) => {
      setSetup(status)
      if (!status.required) void cargar()
    }).catch((failure) => setError(failure instanceof Error ? failure.message : 'No se pudo consultar el servicio'))
  }, [cargar])

  useEffect(() => {
    void fetchAuth().then((status) => {
      setAuth(status)
      if (status.authenticated) iniciarPanel()
    }).catch((failure) => setError(failure instanceof Error ? failure.message : 'No se pudo consultar el acceso'))
  }, [iniciarPanel])

  if (auth && !auth.authenticated) {
    return <AuthPanel setupRequired={auth.setup_required} onAuthenticated={(status) => {
      setAuth(status)
      iniciarPanel()
    }} />
  }

  if (setup?.required && auth?.session) return <SetupPanel csrfToken={auth.session.csrf_token} />

  if (error && !datos) {
    return (
      <main className="wrap">
        <div className="vacio">
          <span className="vacio-icon" aria-hidden="true">!</span>
          <h1>No hay datos</h1>
          <p>{error}</p>
          <button onClick={() => void cargar()}>Reintentar</button>
        </div>
      </main>
    )
  }

  if (!auth || !setup || !datos) {
    return <main className="wrap"><div className="vacio"><span className="spinner" aria-hidden="true" /><p>Leyendo el registro…</p></div></main>
  }

  if (view === 'settings' && auth.session) {
    return <SetupPanel csrfToken={auth.session.csrf_token} mode="settings" onCancel={() => setView('dashboard')} />
  }

  return (
    <main className="wrap">
      <header className="top">
        <div className="brand">
          <img className="brand-mark" src="/favicon.svg" alt="" aria-hidden="true" />
          <div className="brand-copy">
            <div className="brand-line">
              <h1>Registro de imágenes</h1>
              <span className="version">{APP_VERSION}</span>
            </div>
            <p className="sub">
              nodo <strong>{datos.node}</strong><span className="dot">·</span>{datos.registry}
            </p>
          </div>
        </div>
        <div className="top-actions">
          <span className="chip chip-ok"><span className="status-dot" /> operativo</span>
          <span className="session-user">{auth.session?.display_name}</span>
          <button className="refrescar" disabled={cargando} onClick={() => void cargar(true)}>
            <span className="button-icon" aria-hidden="true">↻</span>
            {cargando ? 'midiendo…' : 'Actualizar'}
          </button>
          <button onClick={() => setView('settings')}>Configuración</button>
          <button onClick={() => void logout().then(() => {
            setDatos(null)
            setSetup(null)
            setView('dashboard')
            setAuth({ setup_required: false, authenticated: false, session: null })
          })}>Salir</button>
        </div>
      </header>

      <div className="snapshot-meta">
        <span>Última medición: <strong>{formatWhen(datos.generated_at)}</strong></span>
        <span>{datos.took_ms} ms</span>
      </div>

      {error && <p className="aviso">Mostrando los datos de la última medición correcta. {error}</p>}

      {!datos.disk_available && (
        <p className="aviso">
          El volumen del registro no está montado: las cifras salen de lo que
          declaran los manifiestos y no se pueden detectar blobs huérfanos.
        </p>
      )}

      <StatCards summary={datos.summary} diskAvailable={datos.disk_available} />
      <ProjectTable projects={datos.projects} />
      <DriftPanel drift={datos.drift} />
      <MaintenancePanel
        maintenance={datos.maintenance}
        summary={datos.summary}
        csrfToken={auth.session?.csrf_token || ''}
        onDone={() => void cargar(true)}
      />

      <footer className="pie">
        <div className="pie-brand">
          <strong>Local Registry</strong>
          <span className="version version-footer">{APP_VERSION}</span>
          <span className="pie-note">· panel integrado del registro</span>
        </div>
        <div className="pie-links">
          <a href="api/all" target="_blank" rel="noreferrer">Ver API JSON</a>
          <a href="https://discord.gg/8MAT6ZGJTW" target="_blank" rel="noreferrer">Soporte · Unraides</a>
        </div>
      </footer>
    </main>
  )
}
