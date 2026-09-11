import { useEffect, useState, type FormEvent } from 'react'
import {
  fetchSettings, saveSettings, saveSetup,
  type SetupMember, type SetupPayload,
} from '../api'

function parseMembers(value: string): SetupMember[] {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => {
    const parts = line.split('|').map((part) => part.trim())
    if (parts.length !== 3) throw new Error(`Línea de nodo no válida: ${line}`)
    return { name: parts[0], registry_url: parts[1], panel_url: parts[2] }
  })
}

function formatMembers(members: SetupMember[]): string {
  return members.map((member) => (
    `${member.name} | ${member.registry_url} | ${member.panel_url}`
  )).join('\n')
}

function configuredNodeNames(value: string): string[] {
  const names: string[] = []
  const seen = new Set<string>()
  for (const line of value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)) {
    const parts = line.split('|').map((part) => part.trim())
    const name = parts.length === 3 ? parts[0] : ''
    if (name && !seen.has(name)) {
      seen.add(name)
      names.push(name)
    }
  }
  return names
}

export function SetupPanel({
  csrfToken, mode = 'initial', onCancel,
}: {
  csrfToken: string
  mode?: 'initial' | 'settings'
  onCancel?: () => void
}) {
  const editing = mode === 'settings'
  const [node, setNode] = useState('')
  const [members, setMembers] = useState('')
  const [enabled, setEnabled] = useState(false)
  const [keepLast, setKeepLast] = useState(0)
  const [hour, setHour] = useState('')
  const [gc, setGc] = useState(true)
  const [coordinator, setCoordinator] = useState('')
  const [protectedTags, setProtectedTags] = useState('latest')
  const [source, setSource] = useState('')
  const [timezone, setTimezone] = useState('UTC')
  const [enrollment, setEnrollment] = useState('')
  const [cacheTtl, setCacheTtl] = useState(300)
  const [leaseTtl, setLeaseTtl] = useState(7200)
  const [requireAllNodes, setRequireAllNodes] = useState(true)
  const [branchPattern, setBranchPattern] = useState('')
  const [corsOrigins, setCorsOrigins] = useState('')
  const [showEnrollment, setShowEnrollment] = useState(false)
  const [loading, setLoading] = useState(editing)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [enrollmentResult, setEnrollmentResult] = useState('')
  const nodeNames = configuredNodeNames(members)
  const coordinatorUnavailable = Boolean(coordinator && !nodeNames.includes(coordinator))
  const sourceUnavailable = Boolean(source && !nodeNames.includes(source))
  const unavailableSelection = coordinatorUnavailable || sourceUnavailable

  useEffect(() => {
    if (!editing) return
    void fetchSettings().then((configuration) => {
      setNode(configuration.node)
      setMembers(formatMembers(configuration.members))
      setTimezone(configuration.timezone)
      setEnrollment(configuration.enrollment_code)
      setCacheTtl(configuration.cache_ttl)
      setCorsOrigins(configuration.cors_allowed_origins)
      setEnabled(configuration.maintenance.enabled)
      setKeepLast(configuration.maintenance.keep_last)
      setHour(configuration.maintenance.hour)
      setGc(configuration.maintenance.gc)
      setCoordinator(configuration.maintenance.coordinator)
      setProtectedTags(configuration.maintenance.protected_tags)
      setSource(configuration.maintenance.sync_source)
      setLeaseTtl(configuration.maintenance.lease_ttl)
      setRequireAllNodes(configuration.maintenance.require_all_nodes)
      setBranchPattern(configuration.maintenance.branch_pattern)
      setError(null)
    }).catch((failure) => {
      setError(failure instanceof Error ? failure.message : 'No se pudo leer la configuración')
    }).finally(() => setLoading(false))
  }, [editing])

  const payload = (): SetupPayload => ({
    node: node.trim(),
    members: parseMembers(members),
    timezone: timezone.trim(),
    enrollment_code: enrollment.trim(),
    cache_ttl: cacheTtl,
    cors_allowed_origins: corsOrigins.trim(),
    maintenance: {
      enabled,
      keep_last: keepLast,
      hour: hour.trim(),
      gc,
      coordinator: coordinator.trim(),
      protected_tags: protectedTags.trim(),
      sync_source: source.trim(),
      lease_ttl: leaseTtl,
      require_all_nodes: requireAllNodes,
      branch_pattern: branchPattern.trim(),
    },
  })

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      if (editing) {
        await saveSettings(payload(), csrfToken)
      } else {
        const result = await saveSetup(payload(), csrfToken)
        setEnrollmentResult(result.enrollment_code)
      }
      setSaved(true)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'No se pudo guardar la configuración')
    } finally {
      setBusy(false)
    }
  }

  const copy = async (value: string) => { await navigator.clipboard.writeText(value) }

  if (saved) {
    return <main className="wrap setup-wrap">
      <header className="top">
        <div className="brand">
          <img className="brand-mark" src="/favicon.svg" alt="" />
          <div className="brand-copy">
            <h1>Local Registry configurado</h1>
            <p className="sub">El panel se está reiniciando con la configuración guardada.</p>
          </div>
        </div>
      </header>
      <section className="panel setup-result">
        <h2>Configuración guardada</h2>
        {enrollmentResult && <>
          <p>Usa este código al configurar los demás registros del mismo clúster.</p>
          <label>Código de incorporación<textarea readOnly value={enrollmentResult} rows={5} /></label>
          <button onClick={() => void copy(enrollmentResult)}>Copiar código</button>
        </>}
        <button className="refrescar" onClick={() => window.location.reload()}>Continuar al panel</button>
      </section>
    </main>
  }

  return <main className="wrap setup-wrap">
    <header className="top">
      <div className="brand">
        <img className="brand-mark" src="/favicon.svg" alt="" />
        <div className="brand-copy">
          <h1>{editing ? 'Configuración' : 'Configurar Local Registry'}</h1>
          <p className="sub">
            {editing
              ? 'Administra los valores operativos guardados en este nodo.'
              : 'Los datos se guardarán en el volumen del registro, no en la plantilla de Unraid.'}
          </p>
        </div>
      </div>
      {editing && <button type="button" onClick={onCancel}>Volver</button>}
    </header>

    {loading ? <div className="vacio"><span className="spinner" aria-hidden="true" /><p>Leyendo la configuración…</p></div> :
      <form className="setup-form" onSubmit={(event) => void submit(event)}>
        <section className="panel">
          <header className="panel-head"><h2>Este nodo</h2></header>
          <div className="setup-grid">
            <label>Nombre del nodo<input required value={node} onChange={(event) => setNode(event.target.value)} placeholder="node-a" /></label>
            <label>Zona horaria<input required value={timezone} onChange={(event) => setTimezone(event.target.value)} placeholder="Region/City" /></label>
            <label>Duración de la caché (segundos)<input type="number" min="0" max="86400" value={cacheTtl} onChange={(event) => setCacheTtl(Number(event.target.value))} /></label>
          </div>
        </section>

        <section className="panel">
          <header className="panel-head"><h2>Nodos del clúster</h2></header>
          <label>Nodos<textarea required rows={6} value={members} onChange={(event) => setMembers(event.target.value)} placeholder={'node-a | |\nnode-b | http://registry-b.example:5000 | http://registry-b.example:5001'} /></label>
          <p className="sub">Una línea por nodo: nombre | URL del registro | URL del panel. En la línea de este nodo, ambas URL pueden quedar vacías.</p>
          <label>Código de incorporación
            <div className="setup-secret-row">
              <input type={showEnrollment ? 'text' : 'password'} value={enrollment} onChange={(event) => setEnrollment(event.target.value)} autoComplete="off" placeholder="Vacío al crear el primer nodo" />
              <button type="button" onClick={() => setShowEnrollment((visible) => !visible)}>{showEnrollment ? 'Ocultar' : 'Mostrar'}</button>
              {editing && enrollment && <button type="button" onClick={() => void copy(enrollment)}>Copiar</button>}
            </div>
          </label>
          <p className="sub">Los nodos del mismo clúster deben usar el mismo código. Sustituirlo incorpora este nodo a otro clúster.</p>
        </section>

        <section className="panel">
          <header className="panel-head"><h2>Mantenimiento y retención</h2></header>
          <label className="check-row"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} /> Activar las acciones de mantenimiento</label>
          <div className="setup-grid">
            <label>Versiones por rama<input type="number" min="0" max="100000" value={keepLast} onChange={(event) => setKeepLast(Number(event.target.value))} /></label>
            <label>Hora diaria<input type="time" value={hour} onChange={(event) => setHour(event.target.value)} /></label>
            {editing ? <div className="setup-readonly">
              <span>Coordinador del clúster</span>
              <strong>{coordinator}</strong>
              <small>Se cambia únicamente desde la página principal, en Mantenimiento.</small>
              {coordinatorUnavailable && <span className="setup-field-error">Cambia primero el coordinador antes de retirar este nodo del clúster.</span>}
            </div> : <label>Coordinador inicial<select value={coordinator} onChange={(event) => setCoordinator(event.target.value)} aria-invalid={coordinatorUnavailable}>
              <option value="">Automático (primer nodo por nombre)</option>
              {coordinatorUnavailable && <option value={coordinator} disabled>{coordinator} (no disponible)</option>}
              {nodeNames.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
              {coordinatorUnavailable && <span className="setup-field-error">El coordinador seleccionado ya no figura en la lista de nodos.</span>}
            </label>}
          </div>
          <label>Etiquetas protegidas<input value={protectedTags} onChange={(event) => setProtectedTags(event.target.value)} placeholder="latest,stable" /></label>
          <label className="check-row"><input type="checkbox" checked={gc} onChange={(event) => setGc(event.target.checked)} /> Liberar blobs huérfanos durante la ventana</label>
          <label className="check-row"><input type="checkbox" checked={requireAllNodes} onChange={(event) => setRequireAllNodes(event.target.checked)} /> Bloquear la retención si algún nodo no responde</label>
        </section>

        <section className="panel">
          <header className="panel-head"><h2>Opciones avanzadas</h2></header>
          <div className="setup-grid">
            <label>Caducidad de la reserva (segundos)<input type="number" min="300" max="31536000" value={leaseTtl} onChange={(event) => setLeaseTtl(Number(event.target.value))} /></label>
            <label>Patrón personalizado de ramas<input value={branchPattern} onChange={(event) => setBranchPattern(event.target.value)} placeholder="Expresión regular opcional" /></label>
            <label>Fuente autoritativa en conflictos<select value={source} onChange={(event) => setSource(event.target.value)} aria-invalid={sourceUnavailable}>
              <option value="">Usar el coordinador (recomendado)</option>
              {sourceUnavailable && <option value={source} disabled>{source} (no disponible)</option>}
              {nodeNames.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
              {sourceUnavailable && <span className="setup-field-error">La fuente seleccionada ya no figura en la lista de nodos.</span>}
            </label>
          </div>
          <p className="sub">Sólo interviene si una misma etiqueta apunta a imágenes distintas. Las etiquetas ausentes se copian igualmente desde una réplica disponible.</p>
          <label>Orígenes CORS adicionales<input value={corsOrigins} onChange={(event) => setCorsOrigins(event.target.value)} placeholder="https://panel.example,https://admin.example" /></label>
          <p className="sub">Déjalo vacío para permitir únicamente el mismo origen. Separa varios orígenes con comas.</p>
        </section>

        {error && <p className="aviso" role="alert">{error}</p>}
        <div className="setup-actions">
          {editing && <button type="button" onClick={onCancel}>Cancelar</button>}
          <button className="refrescar" disabled={busy || unavailableSelection}>{busy ? 'Guardando…' : editing ? 'Guardar y reiniciar el panel' : 'Guardar y arrancar el panel'}</button>
        </div>
      </form>
    }
  </main>
}
