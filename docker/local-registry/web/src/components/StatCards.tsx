import { formatBytes, type Summary } from '../api'

/** Las cifras de cabecera. Los huérfanos se destacan porque son la única de
 *  estas cifras sobre la que se puede actuar: es espacio recuperable. */
export function StatCards({ summary, diskAvailable }: { summary: Summary; diskAvailable: boolean }) {
  const pctHuerfano = summary.total_bytes
    ? (summary.orphan_bytes / summary.total_bytes) * 100
    : 0

  return (
    <div className="cards">
      <div className="card">
        <span className="card-label">Ocupación</span>
        <span className="card-value">{formatBytes(summary.total_bytes)}</span>
        <span className="card-note">
          {diskAvailable ? `${summary.blobs_on_disk} blobs en disco` : 'estimada (sin volumen)'}
        </span>
      </div>

      <div className="card">
        <span className="card-label">Proyectos</span>
        <span className="card-value">{summary.projects}</span>
        <span className="card-note">{summary.tags} etiquetas en total</span>
      </div>

      <div className={`card ${summary.orphan_bytes > 0 ? 'card-warn' : ''}`}>
        <span className="card-label">Recuperable</span>
        <span className="card-value">{formatBytes(summary.orphan_bytes)}</span>
        <span className="card-note">
          {summary.orphan_blobs} blobs sin referenciar · {pctHuerfano.toFixed(0)}% del total
        </span>
      </div>

      <div className={`card ${summary.missing_blobs > 0 ? 'card-bad' : ''}`}>
        <span className="card-label">Integridad</span>
        <span className="card-value">
          {summary.missing_blobs === 0 ? 'correcta' : summary.missing_blobs}
        </span>
        <span className="card-note">
          {summary.missing_blobs === 0
            ? 'nada referenciado falta del disco'
            : 'blobs referenciados que NO están en disco'}
        </span>
      </div>
    </div>
  )
}
