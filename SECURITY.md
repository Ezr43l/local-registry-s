# Seguridad

## Versiones atendidas

Se mantiene la versión indicada en `VERSION`. Si alguna release anterior
continúa recibiendo correcciones de seguridad, se enumerará expresamente aquí
con su fecha de fin de soporte; en ausencia de esa lista, sólo está soportada la
versión actual.

## Comunicación responsable

No publiques secretos, topologías ni detalles explotables en una incidencia. En
`Ezr43l/local-registry-s`, utiliza **Security > Report a vulnerability** para
abrir un aviso privado de GitHub. Si ese canal no estuviera disponible, comunica
el hallazgo al propietario por un medio privado antes de compartir detalles.

## Límites del producto

- El acceso público OCI y el panel exigen HTTPS. El motor OCI interno sólo
  escucha en loopback dentro del contenedor. Las credenciales de aplicaciones
  diferencian descarga, publicación y borrado, con límites por repositorio.
- El login del panel es independiente de las credenciales de imágenes. La
  primera cuenta exige un código local de un solo uso. Las sesiones se revocan
  al cerrar sesión y las cookies sólo se envían mediante HTTPS.
- Los nodos verifican certificados y firmas de peticiones y respuestas. La
  réplica valida los manifiestos por su contenido, fija el digest de origen y
  comprueba el resultado antes de dar la copia por terminada.
- Los límites de conexiones, lectura, cuerpos e intentos de acceso protegen el
  trabajo del panel y de la puerta OCI. Los eventos no registran secretos.
- El código de incorporación incluye la identidad privada del grupo; concede
  confianza administrativa al nodo incorporado. No publicarlo ni incluirlo en
  plantillas. Un nodo comprometido requiere renovar la identidad del grupo.
- El panel y su API exigen la cuenta propietaria creada en la primera visita.
  Las mutaciones validan además un token CSRF vinculado a la sesión.
- El token de clúster debe tener entre 32 y 256 caracteres y no se usa para el
  acceso humano. Puede generarlo el asistente o montarse mediante
  `MAINTENANCE_CLUSTER_TOKEN_FILE` en instalaciones heredadas.
- Las operaciones de retención y garbage collection son irreversibles: valida
  siempre la previsualización y una copia de seguridad.
- La replicación SSH exige claves de host previamente verificadas y una clave
  privada local `0400` o `0600`; no desactives `StrictHostKeyChecking`.
- El proceso conserva UID 0 para leer secretos `root:root 0400` y mantener
  compatibilidad con volúmenes Registry existentes. El contenedor compensa ese
  límite con `cap-drop=ALL`, `no-new-privileges`, raíz de sólo lectura, `tmpfs`
  acotados, límite de procesos y sin socket Docker. No se debe retirar ese
  endurecimiento ni añadir el socket.
