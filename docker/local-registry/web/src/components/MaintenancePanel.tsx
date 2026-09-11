import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  fetchMaintenance, formatBytes, formatWhen, runMaintenance, setMaintenanceCoordinator,
  type GcResult, type Maintenance, type RetentionResult, type Summary,
  type SyncResult, type Tarea, type WindowProgress, type WindowResult,
  type MaintenanceHistorySummary, type WindowStepProgress,
} from '../api'

type Estado = {
  cargando: Tarea | null
  resultado: string | null
  resultadoTipo: Tarea | null
  error: string | null
}

type AvisoMantenimiento = {
  titulo: string
  mensajes: string[]
  accion: string
  peligro?: boolean
  aceptacion?: string
}

const AVISOS: Record<Tarea, AvisoMantenimiento> = {
  gc: {
    titulo: 'Liberar espacio del clúster',
    mensajes: [
      'Se eliminarán los blobs que ya no referencia ninguna imagen, nodo a nodo.',
      'Sólo se detendrá un registro cada vez y se comprobará que vuelve antes de continuar.',
    ],
    accion: 'Liberar en el clúster',
    peligro: true,
    aceptacion: 'Entiendo que los blobs eliminados no se pueden recuperar desde el panel.',
  },
  retention: {
    titulo: 'Aplicar retención',
    mensajes: [
      'Se eliminarán las versiones antiguas según la retención configurada para cada rama.',
      'Las etiquetas protegidas y las imágenes sin fecha verificable se conservarán.',
    ],
    accion: 'Aplicar retención',
    peligro: true,
    aceptacion: 'Entiendo que las etiquetas eliminadas no se pueden restaurar desde el panel.',
  },
  sync: {
    titulo: 'Sincronizar registros',
    mensajes: [
      'Se copiarán las etiquetas ausentes y se corregirán los digests distintos entre los registros configurados.',
      'La fuente autoritativa configurada decidirá qué imagen prevalece si existe un conflicto.',
    ],
    accion: 'Sincronizar ahora',
  },
  window: {
    titulo: 'Ejecutar mantenimiento del clúster',
    mensajes: [
      'Se reservarán todos los nodos y se ejecutarán, por este orden, la retención, el GC secuencial y la sincronización.',
      'El panel mostrará el progreso de cada nodo y verificará al final que los registros coinciden.',
    ],
    accion: 'Ejecutar en el clúster',
    peligro: true,
    aceptacion: 'Entiendo que esta ventana puede eliminar etiquetas antiguas y blobs huérfanos.',
  },
}

/** Mantenimiento: las únicas acciones de la interfaz que pueden escribir. */
export function MaintenancePanel({
  maintenance, summary, csrfToken, onDone,
}: {
  maintenance: Maintenance
  summary: Summary
  csrfToken: string
  onDone: () => void
}) {
  const [st, setSt] = useState<Estado>({ cargando: null, resultado: null, resultadoTipo: null, error: null })
  const [progresoVentana, setProgresoVentana] = useState<WindowProgress>(maintenance.window_progress)
  const [confirmacion, setConfirmacion] = useState<Tarea | null>(null)
  const [coordinadorElegido, setCoordinadorElegido] = useState(maintenance.coordinator)
  const [guardandoCoordinador, setGuardandoCoordinador] = useState(false)
  const [mensajeCoordinador, setMensajeCoordinador] = useState<string | null>(null)
  const [errorCoordinador, setErrorCoordinador] = useState<string | null>(null)
  const [, setReloj] = useState(0)

  useEffect(() => {
    if (st.cargando !== 'window') setProgresoVentana(maintenance.window_progress)
  }, [maintenance.window_progress, st.cargando])

  useEffect(() => {
    setCoordinadorElegido(maintenance.coordinator)
  }, [maintenance.coordinator])

  useEffect(() => {
    const timer = window.setInterval(() => setReloj((valor) => valor + 1), 60_000)
    return () => window.clearInterval(timer)
  }, [])

  // La ventana es una petición síncrona, pero su estado se publica en paralelo
  // para que el usuario vea el paso real mientras el servidor trabaja.
  useEffect(() => {
    if (st.cargando !== 'window') return
    let activo = true
    const actualizar = async () => {
      try {
        const estado = await fetchMaintenance()
        if (activo) setProgresoVentana(estado.window_progress)
      } catch {
        // La respuesta final de la operación sigue siendo la fuente de verdad.
      }
    }
    void actualizar()
    const timer = window.setInterval(() => void actualizar(), 500)
    return () => {
      activo = false
      window.clearInterval(timer)
    }
  }, [st.cargando])

  const lanzar = async (what: Tarea, confirm: boolean) => {
    setSt({ cargando: what, resultado: null, resultadoTipo: null, error: null })
    try {
      const r = await runMaintenance(what, confirm, csrfToken)
      if (what === 'window' && r.action === 'window' && r.progress) {
        setProgresoVentana(r.progress)
      }
      setSt({ cargando: null, resultado: resumir(r), resultadoTipo: what, error: null })
      onDone()
    } catch (e) {
      setSt({ cargando: null, resultado: null, resultadoTipo: null, error: e instanceof Error ? e.message : 'La operación falló' })
    }
  }

  const cambiarCoordinador = async (coordinador: string) => {
    setGuardandoCoordinador(true)
    setMensajeCoordinador(null)
    setErrorCoordinador(null)
    try {
      const resultado = await setMaintenanceCoordinator(coordinador, csrfToken)
      setMensajeCoordinador(
        resultado.changed
          ? `${resultado.coordinator} coordina ahora el mantenimiento del clúster.`
          : `${resultado.coordinator} ya era el coordinador.`,
      )
      onDone()
    } catch (e) {
      setCoordinadorElegido(maintenance.coordinator)
      setErrorCoordinador(e instanceof Error ? e.message : 'No se pudo cambiar el coordinador')
    } finally {
      setGuardandoCoordinador(false)
    }
  }

  const bloqueado = !maintenance.cluster_ready
  const ocupado = st.cargando !== null || guardandoCoordinador || maintenance.in_progress || bloqueado
  const coordinacion = maintenance.cluster_mode && (
    <div className="coordinator-settings">
      <div>
        <span className="overview-label">Coordinación</span>
        <strong>Elige qué nodo dirige la ventana</strong>
        <p>El cambio se replica en todo el clúster. El nodo elegido conserva sus propios ajustes de mantenimiento y horario.</p>
      </div>
      <div className="coordinator-control">
        <label htmlFor="maintenance-coordinator">Nodo coordinador</label>
        <select
          id="maintenance-coordinator"
          value={coordinadorElegido}
          disabled={ocupado}
          onChange={(evento) => {
            setCoordinadorElegido(evento.target.value)
            setMensajeCoordinador(null)
            setErrorCoordinador(null)
          }}
        >
          {maintenance.cluster_nodes.map((nodo) => <option key={nodo}>{nodo}</option>)}
        </select>
        <button
          disabled={ocupado || coordinadorElegido === maintenance.coordinator}
          onClick={() => void cambiarCoordinador(coordinadorElegido)}
        >
          {guardandoCoordinador ? 'Guardando…' : 'Cambiar coordinador'}
        </button>
      </div>
      {mensajeCoordinador && <p className="coordinator-feedback" role="status">{mensajeCoordinador}</p>}
      {errorCoordinador && <p className="coordinator-error" role="alert">{errorCoordinador}</p>}
    </div>
  )

  if (!maintenance.enabled) {
    return (
      <section className="panel">
        <header className="panel-head"><h2>Mantenimiento</h2></header>
        <p className="panel-disabled">
          Las acciones están desactivadas en este nodo (<code>MAINTENANCE_ENABLED=0</code>).
        </p>
        {coordinacion}
      </section>
    )
  }

  const progresoActivo = progresoVentana.running || st.cargando === 'window'
  const pasoRetencion = pasoVisible(progresoVentana, 'retention', progresoActivo)
  const pasoGc = pasoVisible(progresoVentana, 'gc', progresoActivo)
  const pasoSync = pasoVisible(progresoVentana, 'sync', progresoActivo)
  const ultimo = maintenance.history.last_run
  const ultimaPrevisualizacion = maintenance.history.last_preview

  return (
    <section className="panel maintenance-panel">
      <header className="panel-head">
        <h2>{maintenance.cluster_mode ? 'Mantenimiento del clúster' : 'Mantenimiento del registro'}</h2>
        {progresoActivo
          ? <span className="chip chip-warn"><span className="status-dot" />en curso</span>
          : bloqueado
            ? <span className="chip chip-bad">requiere atención</span>
            : <span className="chip">disponible</span>}
      </header>
      <p className="panel-intro maintenance-intro">
        Una única ventana ordena la retención, libera espacio nodo a nodo y termina
        sincronizando los registros. Puedes previsualizarla sin modificar nada.
      </p>

      <div className="maintenance-overview">
        <article className={`maintenance-status-card ${bloqueado ? 'status-card-error' : ''}`}>
          <span className="overview-label">Estado del clúster</span>
          <strong>{bloqueado ? 'No preparado' : `${maintenance.cluster_nodes.length} ${maintenance.cluster_nodes.length === 1 ? 'nodo disponible' : 'nodos disponibles'}`}</strong>
          <div className="cluster-node-chips">
            {maintenance.cluster_nodes.map((nodo) => (
              <span className={`chip ${nodo === maintenance.coordinator ? 'chip-coordinator' : ''}`} key={nodo}>
                {nodo}{nodo === maintenance.coordinator ? ' · coordinador' : ''}
              </span>
            ))}
          </div>
          {bloqueado && <p>{maintenance.cluster_error || 'No se puede contactar con el coordinador del clúster.'}</p>}
        </article>

        <article className="maintenance-status-card">
          <span className="overview-label">Último mantenimiento real</span>
          {ultimo ? <HistorialMantenimiento resumen={ultimo} /> : (
            <div className="history-empty">
              <strong>Todavía no hay una ejecución registrada</strong>
              <p>La primera ejecución real quedará guardada aquí aunque reinicies el contenedor.</p>
            </div>
          )}
        </article>

        <article className="maintenance-status-card">
          <span className="overview-label">Programación</span>
          {maintenance.window_hour ? <>
            <strong>Todos los días · {maintenance.window_hour}</strong>
            <p>Zona {maintenance.timezone}{maintenance.window_next && <> · próxima {formatWhen(maintenance.window_next)}</>}</p>
            {!maintenance.window_gc && <span className="chip chip-warn">GC desactivado</span>}
          </> : <>
            <strong>Sin horario automático</strong>
            <p>No hay una hora de mantenimiento configurada en la plantilla.</p>
          </>}
        </article>
      </div>

      {coordinacion}

      <div className="maintenance-primary">
        <div>
          <span className="overview-label">Flujo recomendado</span>
          <h3>Ejecutar el mantenimiento completo</h3>
          <p>
            Reserva todos los nodos y ejecuta las tres fases en el orden seguro.
            El progreso se muestra en directo y al terminar se guarda la fecha y el resultado.
          </p>
        </div>
        <div className="maintenance-primary-actions">
          <button disabled={ocupado} onClick={() => void lanzar('window', false)}>
            {st.cargando === 'window' ? 'Preparando…' : 'Previsualizar'}
          </button>
          <button className="primary-danger" disabled={ocupado} onClick={() => setConfirmacion('window')}>
            Ejecutar mantenimiento
          </button>
        </div>
      </div>

      {progresoActivo && <VentanaProgreso progreso={progresoVentana} />}

      <div className="maintenance-phases" aria-label="Fases del mantenimiento">
        <FaseMantenimiento numero="1" titulo="Conservar versiones" paso={pasoRetencion}>
          {maintenance.keep_last > 0
            ? <>Conserva <strong>{maintenance.keep_last}</strong> versiones únicas por rama. Protegidas: {maintenance.protected_tags.map((tag) => <code key={tag}>{tag}</code>)}</>
            : <>La retención está apagada. Configura <code>KEEP_LAST</code> para activarla.</>}
        </FaseMantenimiento>
        <FaseMantenimiento numero="2" titulo="Liberar espacio" paso={pasoGc}>
          Este nodo muestra <strong>{formatBytes(summary.orphan_bytes)}</strong> en {summary.orphan_blobs} blobs huérfanos. El clúster libera un nodo cada vez.
        </FaseMantenimiento>
        <FaseMantenimiento numero="3" titulo="Sincronizar registros" paso={pasoSync}>
          Corrige etiquetas ausentes y digests distintos al final de la ventana.
        </FaseMantenimiento>
      </div>

      <details className="phase-actions">
        <summary>Acciones por fase</summary>
        <p>Úsalas para diagnosticar o repetir sólo una parte. Para el uso habitual, ejecuta el flujo completo.</p>
        <div className="phase-action-grid">
          <div>
            <strong>Retención</strong>
            <div className="mant-botones">
              <button disabled={ocupado || maintenance.keep_last <= 0} onClick={() => void lanzar('retention', false)}>
                {st.cargando === 'retention' ? 'Calculando…' : 'Previsualizar'}
              </button>
              <button className="peligro" disabled={ocupado || maintenance.keep_last <= 0} onClick={() => setConfirmacion('retention')}>Aplicar</button>
            </div>
          </div>
          <div>
            <strong>Espacio</strong>
            <div className="mant-botones">
              <button disabled={ocupado} onClick={() => void lanzar('gc', false)}>{st.cargando === 'gc' ? 'Calculando…' : 'Previsualizar'}</button>
              <button className="peligro" disabled={ocupado} onClick={() => setConfirmacion('gc')}>Liberar</button>
            </div>
          </div>
          <div>
            <strong>Sincronización</strong>
            <div className="mant-botones">
              <button disabled={ocupado || !maintenance.peers.length} onClick={() => void lanzar('sync', false)}>{st.cargando === 'sync' ? 'Comparando…' : 'Ver diferencias'}</button>
              <button disabled={ocupado || !maintenance.peers.length} onClick={() => setConfirmacion('sync')}>Sincronizar</button>
            </div>
          </div>
        </div>
      </details>

      {ultimaPrevisualizacion && (
        <div className="preview-history">
          <span className="overview-label">Última previsualización</span>
          <span>{formatDateTime(ultimaPrevisualizacion.finished_at)} · {formatRelative(ultimaPrevisualizacion.finished_at)}</span>
          <span>{ultimaPrevisualizacion.retention.count} etiquetas previstas · {ultimaPrevisualizacion.sync.missing_count + ultimaPrevisualizacion.sync.mismatch_count} diferencias</span>
        </div>
      )}

      {st.error && <p className="panel-disabled resultado-error">{st.error}</p>}
      {st.resultado && <>
        <div className="operation-result" aria-live="polite">
          <span className="overview-label">
            {st.resultadoTipo === 'window' ? 'Resultado de la ventana' : 'Resultado de la operación'}
          </span>
          <p>{st.resultado}</p>
        </div>
      </>}

      {confirmacion && (
        <DialogoConfirmacion
          key={confirmacion}
          aviso={AVISOS[confirmacion]}
          onClose={() => setConfirmacion(null)}
          onConfirm={() => {
            const tarea = confirmacion
            setConfirmacion(null)
            void lanzar(tarea, true)
          }}
        />
      )}
    </section>
  )
}

function FaseMantenimiento({
  numero, titulo, paso, children,
}: {
  numero: string
  titulo: string
  paso: WindowStepProgress | null
  children: ReactNode
}) {
  return (
    <article className={`maintenance-phase ${clasePaso(paso)}`}>
      <header>
        <span className="step-number">{numero}</span>
        <strong>{titulo}</strong>
        {paso && <span className="step-status">{textoPaso(paso)}</span>}
      </header>
      <p>{children}</p>
    </article>
  )
}

function HistorialMantenimiento({ resumen }: { resumen: MaintenanceHistorySummary }) {
  return (
    <div className="history-summary">
      <div className="history-result">
        <span className={`history-dot ${resumen.ok ? 'history-ok' : 'history-error'}`} />
        <strong>{resumen.ok ? 'Completado' : 'Con incidencias'}</strong>
      </div>
      <time dateTime={new Date(resumen.finished_at * 1000).toISOString()}>
        Finalizó el {formatDateTime(resumen.finished_at)}
      </time>
      <p>{formatRelative(resumen.finished_at)} · {resumen.trigger === 'scheduled' ? 'automático' : 'manual'} · coordinó {resumen.coordinator}</p>
      <div className="history-metrics">
        <span><strong>{formatDuration(resumen.duration_seconds)}</strong> de duración</span>
        <span><strong>{formatBytes(resumen.gc.freed_bytes)}</strong> liberados</span>
        <span><strong>{resumen.retention.count}</strong> etiquetas</span>
      </div>
    </div>
  )
}

function DialogoConfirmacion({
  aviso, onClose, onConfirm,
}: {
  aviso: AvisoMantenimiento
  onClose: () => void
  onConfirm: () => void
}) {
  const dialogo = useRef<HTMLDialogElement>(null)
  const [aceptado, setAceptado] = useState(false)

  useEffect(() => {
    const actual = dialogo.current
    if (actual && !actual.open) actual.showModal()
    return () => {
      if (actual?.open) actual.close()
    }
  }, [])

  return (
    <dialog
      ref={dialogo}
      className="dialogo"
      aria-labelledby="dialogo-titulo"
      onCancel={(evento) => {
        evento.preventDefault()
        onClose()
      }}
      onClick={(evento) => {
        if (evento.target === evento.currentTarget) onClose()
      }}
    >
      <div className="dialogo-contenido">
        <header>
          <h2 id="dialogo-titulo">{aviso.titulo}</h2>
          <button type="button" className="cerrar-dialogo" aria-label="Cerrar" onClick={onClose}>×</button>
        </header>
        <div className="dialogo-cuerpo">
          {aviso.mensajes.map((mensaje) => <p key={mensaje}>{mensaje}</p>)}
        </div>
        {aviso.aceptacion && (
          <label className="aceptacion-dialogo">
            <input
              type="checkbox"
              checked={aceptado}
              onChange={(evento) => setAceptado(evento.target.checked)}
              autoFocus
            />
            <span>{aviso.aceptacion}</span>
          </label>
        )}
        <footer>
          <button type="button" onClick={onClose}>Cancelar</button>
          <button
            type="button"
            className={`accion-dialogo ${aviso.peligro ? 'peligro' : ''}`}
            disabled={Boolean(aviso.aceptacion) && !aceptado}
            onClick={onConfirm}
            autoFocus={!aviso.aceptacion}
          >
            {aviso.accion}
          </button>
        </footer>
      </div>
    </dialog>
  )
}

function pasoVisible(progreso: WindowProgress, id: WindowStepProgress['id'], visible: boolean) {
  return visible ? progreso.steps.find((paso) => paso.id === id) ?? null : null
}

function clasePaso(paso: WindowStepProgress | null): string {
  if (!paso) return ''
  if (paso.status === 'running') return 'paso-en-curso'
  if (paso.status === 'completed') return 'paso-completado'
  if (paso.status === 'failed' || paso.status === 'blocked') return 'paso-error'
  if (paso.status === 'skipped') return 'paso-omitido'
  return 'paso-pendiente'
}

function textoPaso(paso: WindowStepProgress): string {
  if (paso.status === 'running') return 'en curso'
  if (paso.status === 'completed') return 'completado'
  if (paso.status === 'failed') return 'con incidencias'
  if (paso.status === 'blocked') return 'bloqueado'
  if (paso.status === 'skipped') return 'omitido'
  return 'pendiente'
}

function VentanaProgreso({ progreso }: { progreso: WindowProgress }) {
  const resueltos = progreso.steps.filter((paso) => !['pending', 'running'].includes(paso.status)).length
  const actual = progreso.steps.find((paso) => paso.status === 'running')
  const nodosGc = progreso.steps.find((paso) => paso.id === 'gc')?.nodes ?? []
  const liberado = nodosGc.reduce((total, nodo) => total + (nodo.freed_bytes || 0), 0)
  const duracion = progreso.started_at && progreso.finished_at
    ? formatDuration(progreso.finished_at - progreso.started_at)
    : null
  const titulo = progreso.running
    ? 'Mantenimiento en curso'
    : progreso.status === 'completed'
      ? 'Mantenimiento completado'
      : 'Mantenimiento terminado con incidencias'
  const detalle = actual?.detail || progreso.message

  return (
    <div className={`ventana-progreso progreso-${progreso.status}`} aria-live="polite">
      <div className="progreso-cabecera">
        <div className="progreso-titulo">
          <span className="progreso-icono">{progreso.running ? '…' : progreso.status === 'completed' ? '✓' : '!'}</span>
          <div>
            <strong>{titulo}</strong>
            <p>{detalle}</p>
          </div>
        </div>
        <span className="progreso-contador">{resueltos}/3 fases finalizadas</span>
      </div>
      <div className="progreso-lista">
        {progreso.steps.map((paso) => (
          <div className={`progreso-fase ${clasePaso(paso)}`} key={paso.id}>
            <div className="progreso-item">
              <span className="progreso-punto">{paso.status === 'completed' ? '✓' : paso.number}</span>
              <span>{paso.label}</span>
            </div>
            {paso.id === 'gc' && paso.nodes && (
              <div className="progreso-nodos">
                {paso.nodes.map((nodo) => (
                  <div className={`progreso-nodo nodo-${nodo.status}`} key={nodo.name}>
                    <span className="progreso-nodo-icono">{iconoEstado(nodo.status)}</span>
                    <strong>{nodo.name}</strong>
                    <span>{textoEstadoNodo(nodo.status)}</span>
                    {nodo.freed_bytes > 0 && <span>{formatBytes(nodo.freed_bytes)}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
      {!progreso.running && (duracion || liberado > 0) && (
        <div className="progreso-resumen">
          {liberado > 0 && <span><strong>{formatBytes(liberado)}</strong> liberados</span>}
          {duracion && <span>Duración: <strong>{duracion}</strong></span>}
        </div>
      )}
    </div>
  )
}

function iconoEstado(status: WindowStepProgress['status']): string {
  if (status === 'completed') return '✓'
  if (status === 'running') return '…'
  if (status === 'failed' || status === 'blocked') return '!'
  if (status === 'skipped') return '–'
  return '·'
}

function textoEstadoNodo(status: WindowStepProgress['status']): string {
  if (status === 'completed') return 'Completado'
  if (status === 'running') return 'En curso'
  if (status === 'failed') return 'Incidencia'
  if (status === 'skipped') return 'Omitido'
  return 'Pendiente'
}

function formatDuration(seconds: number): string {
  const minutos = Math.floor(seconds / 60)
  const resto = Math.max(0, seconds % 60)
  return minutos ? `${minutos} min ${resto} s` : `${resto} s`
}

function formatDateTime(epoch: number): string {
  return new Date(epoch * 1000).toLocaleString('es-ES', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

function formatRelative(epoch: number): string {
  const segundos = Math.max(0, Math.floor(Date.now() / 1000) - epoch)
  if (segundos < 60) return 'hace menos de un minuto'
  const minutos = Math.floor(segundos / 60)
  if (minutos < 60) return `hace ${minutos} ${minutos === 1 ? 'minuto' : 'minutos'}`
  const horas = Math.floor(minutos / 60)
  if (horas < 24) return `hace ${horas} ${horas === 1 ? 'hora' : 'horas'}`
  const dias = Math.floor(horas / 24)
  return `hace ${dias} ${dias === 1 ? 'día' : 'días'}`
}

function fecha(epoch: number): string {
  return epoch ? new Date(epoch * 1000).toLocaleDateString('es-ES') : 'sin fecha'
}

function resumir(r: GcResult | RetentionResult | SyncResult | WindowResult): string {
  if (r.error) return `OPERACIÓN BLOQUEADA — ${r.error}`

  if (r.action === 'gc') {
    const cab = r.dry_run
      ? 'PREVISUALIZACIÓN — no se ha borrado nada.'
      : `Liberados ${formatBytes(r.freed_bytes)} (de ${formatBytes(r.bytes_before)} a ${formatBytes(r.bytes_after)}).`
    const reg = r.dry_run ? '' : `\nRegistros disponibles al terminar: ${r.registry_back ? 'sí' : 'NO — revísalo'}`
    const nodos = r.node_results
      ? Object.entries(r.node_results).map(([nombre, resultado]) => {
          if (resultado.skipped) return `  ${nombre}: omitido por seguridad`
          if (resultado.error || !resultado.ok) return `  ${nombre}: incidencia`
          return `  ${nombre}: ${r.dry_run ? 'previsualización completada' : `${formatBytes(resultado.freed_bytes)} liberados`}`
        }).join('\n')
      : r.output
    return `${cab}${reg}\n\n${nodos || '(sin salida)'}`
  }

  if (r.action === 'sync') {
    const cab = r.dry_run
      ? `PREVISUALIZACIÓN — ${r.missing_count} etiquetas ausentes y ${r.mismatch_count} con digest distinto:`
      : `Copiadas ${r.copied.length} etiquetas.`
    const caidos = r.unreachable.length ? `\n⚠ Nodos que no responden: ${r.unreachable.join(', ')}` : ''
    const omit = r.skipped_by_retention
      ? `\n(${r.skipped_by_retention} no se reparten: la retención va a eliminarlas)` : ''
    const cuerpo = r.dry_run ? r.missing.slice(0, 40).map((m) => `  ${m}`).join('\n')
                             : r.copied.slice(0, 40).map((m) => `  ${m}`).join('\n')
    const mal = r.failed.length ? `\nFallaron ${r.failed.length}:\n${r.failed.slice(0, 10).map((m) => `  ${m}`).join('\n')}` : ''
    return `${cab}${caidos}${omit}\n${cuerpo || '  (nada que hacer: los registros están iguales)'}${mal}`
  }

  if (r.action === 'window') {
    const estado = r.ok === false ? 'MANTENIMIENTO TERMINADO CON INCIDENCIAS' :
      r.dry_run ? 'PREVISUALIZACIÓN' : 'MANTENIMIENTO COMPLETADO'
    return `${estado}. Consulta el resumen visual de las tres fases.`
  }

  if (r.enabled === false) return r.reason || 'Retención apagada.'
  if (r.blocked) return `RETENCIÓN BLOQUEADA — ${r.reason || 'faltan nodos configurados.'}`
  if (r.dry_run) {
    const lineas: string[] = []
    for (const c of r.candidates || []) {
      lineas.push(`${c.project} (${c.tags} etiquetas)`)
      for (const b of c.branches || []) {
        const cons = b.keeping.map((t) => `${t} [${fecha(b.dates[t])}]`).join(', ')
        lineas.push(`  rama ${b.branch}: conserva ${cons || '—'}`)
        if (b.removing.length) lineas.push(`    elimina ${b.removing.length}: ${b.removing.slice(0, 8).join(', ')}${b.removing.length > 8 ? '…' : ''}`)
      }
    }
    return `PREVISUALIZACIÓN — se eliminarían ${r.would_remove} etiquetas:\n${lineas.join('\n') || '  (nada que eliminar)'}`
  }
  return `Eliminadas ${r.removed?.length ?? 0} etiquetas.` +
    (r.failed?.length ? `\nFallaron ${r.failed.length}: ${r.failed.slice(0, 10).join(', ')}` : '') +
    `\n\n${r.note ?? ''}`
}
