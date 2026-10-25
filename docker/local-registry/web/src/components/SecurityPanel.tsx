import { useEffect, useState } from 'react'
import { fetchSecurity, updateSecurity, type SecuritySettings } from '../api'

const lines = (value: string) => value.split(/[\n,]+/).map((part) => part.trim()).filter(Boolean)

export function SecurityPanel({ csrfToken }: { csrfToken: string }) {
  const [settings, setSettings] = useState<SecuritySettings | null>(null)
  const [names, setNames] = useState('')
  const [networks, setNetworks] = useState('')
  const [name, setName] = useState('')
  const [role, setRole] = useState('pull')
  const [repos, setRepos] = useState('*')
  const [issued, setIssued] = useState<{ username: string; token: string } | null>(null)
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const reload = async () => {
    const next = await fetchSecurity()
    setSettings(next)
    setNames(next.tls_names.join('\n'))
    setNetworks(next.allowed_networks.join('\n'))
  }
  useEffect(() => { void reload().catch((e) => setError(String(e.message))) }, [])

  const changeClient = async (payload: Record<string, unknown>) => {
    setBusy(true); setError(''); setIssued(null); setMessage('')
    try {
      const result = await updateSecurity('api/security/clients', payload, csrfToken)
      if (result.username && result.token) setIssued({ username: result.username, token: result.token })
      setMessage(result.replication_errors?.length
        ? `Guardado en el coordinador. Falta confirmar en: ${result.replication_errors.join('; ')}`
        : 'Credenciales actualizadas en todos los nodos disponibles.')
      setName('')
      await reload()
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }

  return <>
    <section className="panel">
      <header className="panel-head"><h2>Conexiones seguras y redes autorizadas</h2></header>
      <p className="sub">El registro de imágenes y el panel usan HTTPS. Esta configuración pertenece a este nodo; las credenciales de aplicaciones se comparten con el grupo.</p>
      {error && <p className="aviso" role="alert">{error}</p>}
      {settings && <>
        <div className="setup-grid">
          <label>Direcciones de acceso de este nodo<textarea rows={5} value={names} onChange={(e) => setNames(e.target.value)} placeholder={'registry.example\n192.0.2.10'} /><small>Una IP o nombre por línea, sin protocolo ni puerto. Incluye la IP flotante si se utiliza. El certificado se renueva al guardar.</small></label>
          <label>Redes que pueden conectar<textarea rows={5} value={networks} onChange={(e) => setNetworks(e.target.value)} placeholder={'192.0.2.0/24\n198.51.100.25/32'} /><small>Una red por línea. /32 autoriza una sola IP IPv4; /128, una IPv6. Incluye administración, clientes Docker y nodos del grupo. Retirar tu red puede cortar el acceso.</small></label>
        </div>
        <p className="sub">Instala el certificado público en los clientes antes de usar sus credenciales. Comprueba la huella por un medio de confianza.</p>
        <p className="security-fingerprint"><strong>Huella SHA-256:</strong> <code>{settings.ca_fingerprint}</code></p>
        <div className="security-actions">
          <a className="security-certificate" href="api/security/ca" download="local-registry-ca.crt">Descargar certificado público</a>
          <button type="button" disabled={busy} onClick={() => {
            setBusy(true); setError('')
            void updateSecurity('api/security', { tls_names: lines(names), allowed_networks: lines(networks) }, csrfToken)
              .then(() => setMessage('Guardado. El panel se reinicia con el certificado actualizado; vuelve a cargar la página.'))
              .catch((e) => setError(String(e.message))).finally(() => setBusy(false))
          }}>Guardar conexiones seguras</button>
        </div>
      </>}
    </section>
    <section className="panel">
      <header className="panel-head"><h2>Aplicaciones con acceso a las imágenes</h2></header>
      <p className="sub">Cada cliente utiliza su propio usuario y token con <code>docker login</code>. El acceso al panel no sustituye estas credenciales. La réplica y el mantenimiento disponen de una identidad interna independiente.</p>
      {settings?.clients.map((client) => <div className="security-client" key={client.id}>
        <div><strong>{client.name}</strong><p className="sub"><code>{client.username}</code> · {client.permissions.map((permission) => ({ pull: 'Descargar', push: 'Publicar', delete: 'Borrar', admin: 'Administrar' })[permission] || permission).join(', ')}<br />Repositorios: {client.repositories.join(', ')}</p></div>
        <button type="button" disabled={busy} onClick={() => { if (window.confirm(`¿Revocar el acceso de ${client.name} en todos los nodos?`)) void changeClient({ action: 'delete', id: client.id }) }}>Revocar acceso</button>
      </div>)}
      {!settings?.clients.length && <p className="sub">Todavía no hay aplicaciones externas autorizadas.</p>}
      <div className="setup-grid">
        <label>Nombre de la aplicación<input value={name} onChange={(e) => setName(e.target.value)} placeholder="Servidor Docker, publicación de imágenes…" /></label>
        <label>Permisos<select value={role} onChange={(e) => setRole(e.target.value)}><option value="pull">Sólo descargar</option><option value="push">Descargar y publicar</option><option value="delete">Descargar, publicar y borrar</option></select></label>
        <label>Repositorios autorizados<input value={repos} onChange={(e) => setRepos(e.target.value)} placeholder="*, equipo/*, mi-app" /><small>Separa por comas. * permite todos; equipo/* limita a ese espacio; mi-app permite sólo ese repositorio.</small></label>
      </div>
      <button type="button" disabled={busy || !name.trim()} onClick={() => void changeClient({ action: 'create', name: name.trim(), permissions: role === 'pull' ? ['pull'] : role === 'push' ? ['pull', 'push'] : ['pull', 'push', 'delete'], repositories: lines(repos) })}>Crear credencial</button>
      {issued && <div className="security-issued" role="status">
        <p>Guarda este token ahora: no se volverá a mostrar.</p>
        <div className="setup-grid"><label>Usuario<input readOnly value={issued.username} /></label><label>Token<input readOnly value={issued.token} /></label></div>
        <button type="button" onClick={() => void navigator.clipboard.writeText(`Usuario: ${issued.username}\nToken: ${issued.token}`)}>Copiar credencial</button>
        <button type="button" onClick={() => setIssued(null)}>Ocultar</button>
      </div>}
      {message && <p className="aviso" role="status">{message}</p>}
    </section>
  </>
}
