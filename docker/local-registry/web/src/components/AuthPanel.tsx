import { useState, type FormEvent } from 'react'
import { APP_VERSION, createOwner, login, type AuthStatus } from '../api'

export function AuthPanel({
  setupRequired, onAuthenticated,
}: {
  setupRequired: boolean
  onAuthenticated: (status: AuthStatus) => void
}) {
  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const status = setupRequired
        ? await createOwner({
            username: username.trim(),
            display_name: displayName.trim(),
            password,
            password_confirmation: confirmation,
          })
        : await login(username.trim(), password)
      onAuthenticated(status)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'No se pudo iniciar sesión')
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="login-page">
      <section className="login-intro" aria-labelledby="login-product-title">
        <img src="/favicon.svg" alt="" aria-hidden="true" />
        <span className="login-eyebrow">REGISTRO OCI PRIVADO</span>
        <h1 id="login-product-title">Local Registry</h1>
        <p>
          Publica, replica y mantiene las imágenes Docker de tus servidores desde
          un panel común para todo el clúster.
        </p>
        <div className="login-feature">
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M12 3l7 3v5c0 4.6-2.8 8.1-7 10-4.2-1.9-7-5.4-7-10V6l7-3z" />
            <path d="M8.7 12.1l2.1 2.1 4.7-4.8" />
          </svg>
          <span>Una cuenta común para todos los nodos del clúster</span>
        </div>
      </section>
      <section className="login-panel" aria-labelledby="login-heading">
        <form className="login-form" onSubmit={(event) => void submit(event)}>
          <div className="login-symbol" aria-hidden="true">
            <svg viewBox="0 0 24 24" focusable="false">
              <circle cx="8" cy="15" r="4" />
              <path d="M11 12l8-8M16 7l3 3M14 9l2 2" />
            </svg>
          </div>
          <span className="login-eyebrow">ACCESO PRIVADO</span>
          <h2 id="login-heading">
            {setupRequired ? 'Crear la cuenta del clúster' : 'Entrar en Local Registry'}
          </h2>
          <p className="login-help">
            {setupRequired
              ? 'Esta primera cuenta se utilizará para acceder a todos los nodos.'
              : 'Identifícate para consultar el registro y ejecutar su mantenimiento.'}
          </p>
          <label>
            <span>Usuario</span>
            <input
              required
              autoFocus
              autoCapitalize="none"
              autoComplete="username"
              spellCheck={false}
              value={username}
              onChange={(event) => setUsername(event.target.value)}
            />
          </label>
          {setupRequired && (
            <label>
              <span>Nombre visible</span>
              <input
                required
                autoComplete="name"
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
              />
            </label>
          )}
          <label>
            <span>Contraseña</span>
            <input
              required
              minLength={12}
              maxLength={128}
              type="password"
              autoComplete={setupRequired ? 'new-password' : 'current-password'}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          {setupRequired && (
            <label>
              <span>Repetir contraseña</span>
              <input
                required
                minLength={12}
                maxLength={128}
                type="password"
                autoComplete="new-password"
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
              />
            </label>
          )}
          {error && <p className="aviso" role="alert">{error}</p>}
          <button className="login-submit" disabled={busy}>
            {busy ? 'Comprobando…' : setupRequired ? 'Crear cuenta y entrar' : 'Entrar'}
          </button>
          <span className="login-version">{APP_VERSION}</span>
          <a className="login-support" href="https://discord.gg/8MAT6ZGJTW" target="_blank" rel="noreferrer">Soporte en Discord · Unraides</a>
        </form>
      </section>
    </main>
  )
}
