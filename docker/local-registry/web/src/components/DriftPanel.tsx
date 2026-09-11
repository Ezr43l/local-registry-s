import { useState } from 'react'
import type { Drift, DriftIssue } from '../api'

/** Deriva entre los registros.
 *
 *  Se compara el digest, no si la etiqueta existe: 'stable' y 'dev' son
 *  flotantes —están siempre y cambian de imagen en cada versión—, así que mirar
 *  solo la presencia daría por sincronizado justo lo que acaba de quedarse
 *  viejo.
 *
 *  Y se separan discrepancias de ausencias. Una etiqueta que significa cosas
 *  distintas según el nodo es un problema; una que solo está en el nodo donde se
 *  construye es lo normal —el histórico no se replica a propósito—. Mezclarlas
 *  hace que cientos de ausencias esperadas entierren la avería de verdad. */
export function DriftPanel({ drift }: { drift: Drift }) {
  const [verAusencias, setVerAusencias] = useState(false)

  return (
    <section className="panel">
      <header className="panel-head">
        <h2>Coherencia entre registros</h2>
        <span className={`chip ${drift.consistent ? 'chip-ok' : 'chip-bad'}`}>
          {drift.consistent
            ? 'coherente'
            : `${drift.mismatch_count} discrepancias`}
        </span>
      </header>

      {drift.consistent ? (
        <p className="ok">
          ✅ Ninguna etiqueta apunta a imágenes distintas entre {drift.nodes.join(', ')}.
        </p>
      ) : (
        <Tabla issues={drift.mismatches} nodes={drift.nodes} />
      )}

      <div className="ausencias">
        <button className="enlace" onClick={() => setVerAusencias(!verAusencias)}>
          {verAusencias ? '▾' : '▸'} {drift.missing_count} etiquetas no están en todos los nodos
        </button>
        <p className="leyenda-inline">
          Normalmente esperado: el histórico de versiones se queda donde se construye.
        </p>
        {verAusencias && <Tabla issues={drift.missing} nodes={drift.nodes} />}
      </div>
    </section>
  )
}

function Tabla({ issues, nodes }: { issues: DriftIssue[]; nodes: string[] }) {
  return (
    <div className="tabla-scroll">
      <table>
        <thead>
          <tr>
            <th>Proyecto</th>
            <th>Etiqueta</th>
            {nodes.map((n) => <th key={n} className="num">{n}</th>)}
          </tr>
        </thead>
        <tbody>
          {issues.map((i) => (
            <tr key={`${i.project}:${i.tag}`}>
              <td>{i.project}</td>
              <td><code>{i.tag}</code></td>
              {nodes.map((n) => {
                const v = i.nodes[n]
                return (
                  <td key={n} className="num">
                    {v
                      ? <span className="digest" title={v}>{v.slice(7, 15)}</span>
                      : <span className="falta">no está</span>}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
