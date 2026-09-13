# Changelog

## v1.2.14

- Restaurado en el paquete compartido el proceso que comprueba y publica la imagen
  pública para servidores Linux AMD64 y ARM64.
- La plantilla pública utiliza el canal `stable`, que se mueve únicamente después de
  publicar y verificar una nueva versión estable.
- La publicación puede obtener las recetas de Alpine desde su GitLab o desde el espejo
  oficial de GitHub, evitando que una caída temporal de uno bloquee la versión.
- No cambia el funcionamiento del registro, del panel ni del mantenimiento coordinado.

## v1.2.13

- El mantenimiento manual conserva en pantalla el resultado de las tres fases al
  finalizar, incluido el nodo y el motivo exacto si la operación queda bloqueada.
- Los errores devueltos por la API se muestran con su explicación real en lugar de
  reducirse al número de respuesta HTTP.
- Se ha regularizado la activación del mantenimiento en los tres nodos del clúster y
  se han validado una simulación y una ejecución real completas.

## v1.2.12

- Actualizado el contrato reproducible de Alpine a `xz-libs 5.8.4-r0`, la revisión
  corregida que entrega el repositorio y que exige la imagen final.
- La puerta de ausencia de variables de plantilla usa una condición explícita compatible
  con `errexit`, ShellCheck y `actionlint`.
- La auditoría documenta que el valor vacío de `_auth_status` representa una sesión
  ausente y no una contraseña incrustada, sin relajar el resto de reglas de Bandit.

## v1.2.11

- Cambiar el coordinador deja de quedar bloqueado cuando otro nodo tiene el
  mantenimiento desactivado: la operación conserva las comprobaciones de
  conectividad, topología y exclusión, pero no ejecuta mantenimiento.
- El botón aplica el cambio directamente y muestra junto al selector tanto la
  confirmación como cualquier error devuelto por el clúster.
- El selector continúa disponible aunque el coordinador tenga el mantenimiento
  pausado, para que siempre sea posible trasladar el rol a otro nodo.
- El preflight de GitHub Actions expresa la ausencia de variables en la plantilla como
  una condición explícita compatible con `errexit` y `actionlint`.

## v1.2.10

- El coordinador sólo se puede cambiar desde la página principal; la sección
  de configuración muestra el valor efectivo sin permitir una segunda
  selección contradictoria.
- Cada cambio de coordinador se guarda también en la configuración persistente
  de todos los nodos, además del estado operativo compartido.
- El origen automático de sincronización usa realmente el coordinador. La
  excepción manual se identifica como fuente autoritativa para conflictos y
  queda dentro de las opciones avanzadas.

## v1.2.9

- El coordinador y el origen de sincronización se seleccionan entre los nodos
  definidos, en lugar de aceptar texto libre.
- Los desplegables conservan los modos automáticos y bloquean el guardado si se
  elimina de la topología un nodo previamente seleccionado.

## v1.2.8

- La sección **Configuración** adopta la paleta, tipografía, paneles y botones
  del dashboard, y delimita claramente todos los campos editables.
- Corrige tres referencias CSS inexistentes que hacían que los controles se
  confundieran visualmente con el fondo.

## v1.2.7

- El asistente inicial y la nueva sección **Configuración** permiten consultar
  y modificar desde la WebUI todos los valores operativos que antes ocupaban la
  plantilla de Unraid.
- La configuración persistida por la aplicación prevalece sobre variables
  heredadas de plantillas antiguas, incluido el secreto interno del clúster.
- La plantilla conserva únicamente puertos y almacenamiento, que pertenecen al
  ciclo de vida del contenedor y no pueden cambiarse desde su interior.

## v1.2.6

- Los nodos sin cuenta adoptan automáticamente la identidad propietaria ya
  existente en el clúster mediante la API interna autenticada. Si encuentran
  identidades distintas, no sobrescriben ninguna.
- El acceso usa el mismo portal visual de dos paneles que el resto de la suite.

## v1.2.5

- El panel completo queda protegido por una cuenta propietaria creada desde la
  WebUI y por sesiones HTTP con cookie `HttpOnly` y verificación CSRF.
- Las acciones de mantenimiento dejan de pedir o aceptar un token
  administrativo: se autorizan con la sesión iniciada.
- El código de incorporación sólo transporta el secreto interno del clúster;
  los códigos heredados siguen siendo válidos al añadir un nodo.

## v1.2.4

- El token administrativo dispone ahora de un botón explícito para validarlo y
  habilitar las acciones de mantenimiento, con confirmación visible y soporte
  para la tecla Intro.
- La validación no ejecuta ninguna tarea ni guarda el token fuera de la memoria
  de la pestaña.

## v1.2.3

- Restaura el icono Moby de Local Registry sin depender de que el futuro
  repositorio público ya exista.
- La imagen incorpora las etiquetas de DockerMan para conservar en Unraid las
  acciones WebUI y Editar incluso cuando el contenedor se recrea manualmente.

## v1.2.2

- La plantilla Unraid pasa de 23 campos a tres conexiones Docker: puerto OCI,
  puerto del panel y almacenamiento persistente.
- El primer arranque se configura desde la WebUI: nodo, pares, retención,
  coordinación, horario y zona horaria quedan bajo
  `/var/lib/registry/.local-registry`.
- Los tokens administrativo y de clúster se generan con permisos `0600`. El
  primer nodo entrega un código de incorporación para los demás miembros y no
  se necesita un volumen de secretos separado.
- Se mantiene la compatibilidad con despliegues existentes que ya proporcionan
  `NODE_NAME`, `PEERS` y las variables de mantenimiento.

## v1.2.1 — 2026-09-07

Corrección de identidad y preparación del canal compartido.

- El futuro repositorio público adopta el nombre de producto `local-registry-s`; se
  actualizan imagen, plantilla, metadatos OCI, CI, release y documentación.
- Local Registry mantiene su arquitectura de un único contenedor con Registry y panel
  integrados.
- El árbol compartible no incorpora evidencias de laboratorio, topología, credenciales ni
  valores de una instalación particular.
- Se actualiza el inventario reproducible de Alpine con las revisiones de
  `apk-tools`, `libapk` y `libuuid` instaladas durante la construcción.
- El contrato de ejecución conserva literalmente las rutas internas de Docker
  cuando se lanza desde Git Bash en Windows.

## v1.2.0 — 2026-08-30

Historial persistente y nueva experiencia de mantenimiento del clúster.

- Licencia Apache-2.0 adoptada para código y documentación, incluida en la imagen y declarada
  en OCI; la release pública exige `LICENSE_SPDX=Apache-2.0`.
- Todas las conexiones salientes se limitan a HTTP(S), vuelven a validar esquema y host en el
  límite de transporte y rechazan credenciales embebidas y fragmentos.
- La recolección ejecuta `/bin/registry` mediante argumentos fijos y sin shell; Bandit cubre
  todas las severidades y el laboratorio multinodo autentica también su token administrativo.
- El laboratorio multinodo conserva literalmente sus rutas HTTP al ejecutarse desde Git Bash,
  evitando que la conversión automática de rutas de MSYS altere los endpoints enviados a Docker.
- Las acciones de release están fijadas por commit, las herramientas por digest y la
  publicación exige CI correcta, dos arquitecturas escaneadas, SBOM y procedencia verificable.
- La aplicación guarda por separado el último mantenimiento real y la última previsualización dentro del volumen persistente.
- El panel muestra la fecha y hora exactas de la última ejecución, su antigüedad, duración, origen manual o automático, coordinador, espacio liberado y resultado.
- El progreso de las fases sólo se resalta mientras la ventana está activa; un éxito anterior ya no deja permanentemente verde la sección.
- El coordinador se puede elegir desde cualquier portal y el cambio se replica de forma atómica a todos los nodos con reserva distribuida y reversión ante fallos.
- El planificador queda preparado en todos los nodos, pero sólo ejecuta quien sea coordinador en el momento programado; cambiarlo no exige reiniciar contenedores.
- Rediseño completo de la sección: estado, historial y horario separados; mantenimiento completo como flujo principal y acciones individuales dentro de un bloque avanzado.
- Los resultados manuales se presentan como información de la aplicación, sin bloques de código ni avisos nativos del navegador.
- La instalación limpia queda separada del Registry que todavía no existe: Compose construye desde código y la plantilla pública apunta al canal compartido versionado.
- La plantilla Unraid deja de contener marcadores y utiliza valores neutros sin nodos, IP, rutas ni secretos del entorno original.
- El mantenimiento queda apagado por defecto y, al activarlo, todas sus peticiones exigen un token administrativo distinto del secreto HMAC del clúster.
- Los secretos directo/fichero son excluyentes y se rechazan ficheros ilegibles, no UTF-8 o desproporcionados.
- Los ficheros de secretos se abren sin seguir enlaces y se validan mediante su
  descriptor; el arranque comprueba toda la configuración del panel antes de
  iniciar procesos y sale con código 64 si no es válida.
- Compose y la plantilla pública dejan de pasar secretos directos: montan un
  directorio `ro` y sólo configuran rutas `MAINTENANCE_*_TOKEN_FILE`. El
  despliegue remoto entrega valores por stdin en ficheros `root:root 0400` y
  revierte secretos y contenedor si el nuevo Registry no queda disponible.
- El build fija sus bases por digest, usa lockfiles para npm y el inventario APK,
  ejecuta 31 pruebas dentro de la imagen y funciona en ARM64 y AMD64 sin
  ejecutar `esbuild` bajo emulación. Un cambio en cualquier paquete Alpine no
  virtual exige revisar y actualizar expresamente el bloqueo auditado.
- El servidor consume de forma segura el cuerpo de los POST rechazados para que una conexión HTTP/1.1 persistente no contamine la petición siguiente.
- Vite se actualiza a `7.3.6`; `npm audit` no detecta vulnerabilidades conocidas en el árbol actual.
- Compose y la plantilla activan filesystem de sólo lectura, `tmpfs`, eliminación de capacidades, `no-new-privileges` y límite de procesos.
- Los `tmpfs` usan el mismo contrato en Compose, Unraid y despliegue (`/run`
  `0755`, `/tmp` `1777`, ambos `nosuid`/`noexec`) y se comprueban contra el
  contenedor real. El espejo valida referencias/nodos antes de escribir y ya no
  permite desactivar la identidad SSH del host.
- El renderizador abre la plantilla sin seguir enlaces, limita su tamaño,
  rechaza DTD/entidades y destinos ambiguos, y sólo procesa una raíz y un
  `Repository` únicos.
- El despliegue remoto exige identidades ya registradas en `known_hosts` para
  todas sus conexiones SSH y rechaza también DTD/entidades en una plantilla
  instalada antes de reutilizar cualquiera de sus valores.
- El hook de Docker valida puertos, nombres y literales IPv6 completos antes de
  escribir `daemon.json`; una dirección comprimida mal formada ya no puede
  dejar una configuración que impida arrancar el daemon.
- Distribution deja de recibir las variables `REGISTRY_*` internas del panel y
  no intenta exportar telemetría salvo configuración explícita.
- Se añaden cabeceras CSP, permisos, anti-frame y no-cache para la API, además de metadatos OCI y workflows de CI/release con SBOM y procedencia.
- El motor OCI integrado se actualiza de Distribution `2.8.3` al commit exacto
  de `3.1.1` y se reconstruye como `3.1.1-secure.2` con Go `1.27.0`,
  `x/crypto 0.55.0`, `x/net 0.58.0`, `x/text 0.41.0` y gRPC `1.83.2`.
- Un parche fijado por SHA-256 corrige el recorrido de rutas del driver
  `inmemory` cuando se repiten componentes y añade regresiones explícitas; no
  se omite ni reintenta ningún test de Distribution.
- La reconstrucción ejecuta primero todas las pruebas cortas de Distribution y
  elimina los hallazgos altos/críticos que contenía su binario publicado. CI y
  release aplican Trivy `0.74.0` sin `--ignore-unfixed` ni excepciones globales.
- La imagen conserva inventarios y avisos de Distribution, Go, npm, Python y
  APK. Cada release incorpora un manifiesto AMD64/ARM64 y un archivo reproducible
  con las recetas, parches y distfiles copyleft de Alpine verificados por
  `abuild`; la publicación falla si no puede generarlos. La obtención usa blobs
  parciales, comprobación de objetos, reintentos limitados y publicación
  transaccional para tolerar cortes sin aceptar revisiones distintas.

## v1.1.0 — 2026-08-30

Mantenimiento coordinado para instalaciones con varios registros.

- Una ventana iniciada desde cualquier panel se delega en un único coordinador.
- Reserva distribuida con caducidad para impedir ejecuciones manuales o programadas solapadas.
- Comprobación previa de topología, paneles, registros y configuración antes de borrar nada.
- Garbage collection secuencial por nodo, verificando que cada registro vuelve a servir antes de detener el siguiente.
- Retención global, sincronización única y comprobación final de deriva dentro de la misma ejecución.
- Órdenes internas firmadas con HMAC, fecha y nonce mediante un secreto externo común; no se incorporan claves a la imagen.
- Progreso por nodo, coordinador y estado del clúster visibles desde cualquiera de los portales.
- Planificador activo únicamente en el coordinador aunque todos los nodos tengan configurada la misma hora.
- Nuevas variables genéricas `PANEL_PEERS`, `MAINTENANCE_COORDINATOR`, `MAINTENANCE_CLUSTER_TOKEN` y `MAINTENANCE_LEASE_TTL`.

## v1.0.0 — 2026-08-26

Primera versión estable independiente del entorno original.

- Plantilla de Unraid y Docker Compose genéricos, sin nodos, IPs, rutas ni claves incrustados.
- Retención opcional de versiones únicas por rama y protección de etiquetas configuradas.
- Ventana de mantenimiento segura con retención, GC controlado y sincronización en orden.
- Progreso real de las tres fases, estados finales claros y presentación visual sin JSON técnico.
- Panel con versión visible, favicon local, textos de mantenimiento dependientes de la plantilla y controles más claros.
- Sistema visual unificado con el panel de IP flotante: paleta oscura, tipografía monoespaciada, jerarquía, tablas, tarjetas y estados coherentes.
- Confirmaciones de mantenimiento integradas en la aplicación, sin avisos nativos del navegador y con aceptación explícita para acciones irreversibles.
- Conservación del icono Moby, las imágenes existentes y el volumen persistente durante los despliegues.

## v1.0.0-rc6 — 2026-08-26

Ajuste visual del mantenimiento manual.

- El resultado de la ventana deja de mostrar JSON técnico y se presenta mediante el resumen visual de fases.
- Los tres pasos quedan separados en tarjetas independientes.
- Los estados completados usan una señal azul y neutra; el verde queda reservado para indicadores de éxito concretos.

## v1.0.0-rc5 — 2026-08-26

Ajuste de la release candidate: el estado final también marca como incidencia
una recolección cuyo registro no vuelve a estar disponible.

## v1.0.0-rc4 — 2026-08-26

Cuarta release candidate, con progreso visible y verificable para la ventana de
mantenimiento.

- El backend publica el paso actual y el estado de cada una de las tres fases.
- La interfaz resalta el bloque activo y muestra un resumen final de éxito,
  omisiones o incidencias.
- La ventana conserva el orden retención → liberación de espacio → sincronización.

## v1.0.0-rc3 — 2026-08-26

Tercera release candidate, con los últimos ajustes de presentación del panel.

- Favicon local en SVG, sin margen interior, para aprovechar todo el espacio del navegador.
- Mensaje inferior de mantenimiento dependiente de la hora configurada en la plantilla.
- Separación visual entre la información de la ventana y sus acciones.

## v1.0.0-rc2 — 2026-08-26

Segunda release candidate, centrada en presentar mejor la información sin
cambiar las operaciones del registro.

- La interfaz muestra la versión real de la imagen desplegada.
- Rediseño visual ligero del panel, las tarjetas, las tablas y el mantenimiento.
- Acciones con nombres más claros: previsualizar, aplicar retención y liberar
  espacio.
- Pie de página simplificado, con acceso limpio a la API JSON.

## v1.0.0-rc1 — 2026-08-26

Primera release candidate independiente del entorno original. La promoción a `v1.0.0` queda pendiente de la validación en producción.

- Contenedor único con registro Docker/OCI, panel web y volumen persistente.
- Plantilla de Unraid genérica, conservando el icono Moby y los recursos del
  panel existentes.
- Configuración neutral: sin nombres de nodos, IPs, rutas personales, zona
  horaria regional ni repositorios privados incrustados.
- Añadido Docker Compose para instalación en servidores Docker normales.
- Retención opcional por rama que cuenta versiones únicas por digest; conserva
  tags protegidos y no elimina imágenes con fecha desconocida.
- Retención bloqueada por defecto cuando un par configurado no responde.
- Recolección segura: falla cerrada si no se identifica el proceso del registro,
  detiene el registro durante GC y lo levanta de nuevo incluso si GC falla.
- Bloqueo común para impedir que dos operaciones de mantenimiento se pisen.
- Ventana diaria configurable por zona horaria y ordenada como retención,
  recolección y sincronización.
- Sincronización reparada para corregir también tags existentes con digest distinto.
- Borrado protegido contra cambios concurrentes del tag entre el plan y el DELETE.
- API destructiva protegida contra peticiones cross-site mediante cabecera
  personalizada y CORS cerrado por defecto.
- Rutas HTTP codificadas, lectura de cabeceras case-insensitive y protección
  contra escape del directorio de la SPA.
- Scripts de construcción, réplica y despliegue parametrizados para no depender
  de una topología, cuenta, clave o proveedor concretos.

## Historial de desarrollo

Las versiones `v0.x` pertenecen al desarrollo previo y describen decisiones que
se consolidan en `v1.0.0`: panel integrado, almacenamiento persistente,
retención, garbage collection, análisis de blobs y sincronización por API.
Los detalles específicos del entorno de desarrollo no forman parte de esta
distribución independiente.
