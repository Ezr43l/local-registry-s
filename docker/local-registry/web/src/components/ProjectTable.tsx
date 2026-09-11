import { Fragment, useState } from 'react'
import { formatBytes, type Project } from '../api'

type Orden = 'exclusive_bytes' | 'tags' | 'name'

/** La tabla de proyectos.
 *
 *  La barra separa exclusivo de compartido a propósito: es la distinción que
 *  hace útil la cifra. Sumar el tamaño de cada etiqueta contaría la misma capa
 *  base decenas de veces, así que aquí «exclusivo» significa exactamente
 *  «bytes que se liberan si borras este proyecto entero». */
export function ProjectTable({ projects }: { projects: Project[] }) {
  const [orden, setOrden] = useState<Orden>('exclusive_bytes')
  const [abierto, setAbierto] = useState<string | null>(null)

  const ordenados = [...projects].sort((a, b) =>
    orden === 'name' ? a.name.localeCompare(b.name) : b[orden] - a[orden],
  )
  const maximo = Math.max(...projects.map((p) => p.total_bytes), 1)

  return (
    <section className="panel">
      <header className="panel-head">
        <h2>Proyectos</h2>
        <div className="orden">
          {(['exclusive_bytes', 'tags', 'name'] as Orden[]).map((o) => (
            <button
              key={o}
              className={orden === o ? 'activo' : ''}
              onClick={() => setOrden(o)}
            >
              {o === 'exclusive_bytes' ? 'tamaño' : o === 'tags' ? 'versiones' : 'nombre'}
            </button>
          ))}
        </div>
      </header>

      <table>
        <thead>
          <tr>
            <th>Proyecto</th>
            <th className="num">Versiones</th>
            <th className="num">Exclusivo</th>
            <th className="num">Compartido</th>
            <th className="barra-col">Reparto</th>
          </tr>
        </thead>
        <tbody>
          {ordenados.map((p) => (
            <Fragment key={p.name}>
              <tr
                className="fila"
                onClick={() => setAbierto(abierto === p.name ? null : p.name)}
              >
                <td>
                  <span className="chevron">{abierto === p.name ? '▾' : '▸'}</span>
                  {p.name}
                </td>
                <td className="num">{p.tags}</td>
                <td className="num fuerte">{formatBytes(p.exclusive_bytes)}</td>
                <td className="num tenue">{formatBytes(p.shared_bytes)}</td>
                <td>
                  <div className="barra" title={`${formatBytes(p.total_bytes)} referenciados`}>
                    <div
                      className="barra-ex"
                      style={{ width: `${(p.exclusive_bytes / maximo) * 100}%` }}
                    />
                    <div
                      className="barra-sh"
                      style={{ width: `${(p.shared_bytes / maximo) * 100}%` }}
                    />
                  </div>
                </td>
              </tr>
              {abierto === p.name && (
                <tr className="detalle">
                  <td colSpan={5}>
                    <div className="etiquetas">
                      {p.tag_names.map((t) => (
                        <span key={t} className="etiqueta">{t}</span>
                      ))}
                    </div>
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>

      <p className="leyenda">
        <span className="punto punto-ex" /> exclusivo — se libera al borrar el proyecto
        <span className="punto punto-sh" /> compartido — capas que usan otros proyectos
      </p>
    </section>
  )
}
