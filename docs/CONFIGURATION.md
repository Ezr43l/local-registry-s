# Configuración de Local Registry

Local Registry separa las conexiones que debe crear Docker de la configuración
funcional de la aplicación. Ningún ejemplo contiene nombres, direcciones, rutas
o credenciales de una instalación concreta.

## Valores que permanecen en la plantilla

Estos tres valores pertenecen al contenedor y no pueden modificarse desde su
interior:

| Valor | Función |
| --- | --- |
| Puerto del registro | Publica el puerto OCI interno `5000` en el host. |
| Puerto del panel | Publica la WebUI interna `5001` en el host. |
| Almacenamiento | Monta el volumen persistente en `/var/lib/registry`. |

## Valores gestionados desde la aplicación

El asistente inicial y la sección **Configuración** usan el mismo contrato. Los
cambios se guardan en el volumen persistente y reinician únicamente el panel
integrado para cargar los nuevos valores; el contenedor y el Registry no se
recrean.

| Variable interna | Campo de la WebUI | Valor inicial neutral |
| --- | --- | --- |
| `NODE_NAME` | Nombre del nodo | Lo introduce el usuario. |
| `PEERS` | URL OCI de los otros nodos | Ninguna. |
| `PANEL_PEERS` | URL del panel de los otros nodos | Ninguna. |
| `CACHE_TTL` | Duración de la caché | `300` segundos. |
| `TZ` | Zona horaria | `UTC`. |
| `MAINTENANCE_ENABLED` | Activar mantenimiento | Desactivado. |
| `KEEP_LAST` | Versiones conservadas por rama | `0`, sin eliminación automática. |
| `PROTECTED_TAGS` | Etiquetas que nunca elimina la retención | `latest`. |
| `MAINTENANCE_HOUR` | Hora de la ventana diaria | Vacía; sólo ejecución manual. |
| `MAINTENANCE_GC` | Recolectar blobs durante la ventana | Activado. |
| `MAINTENANCE_COORDINATOR` | Nodo coordinador | Primer miembro por nombre. |
| `MAINTENANCE_LEASE_TTL` | Caducidad de la reserva distribuida | `7200` segundos. |
| `RETENTION_REQUIRE_ALL_NODES` | Bloquear retención si falta un nodo | Activado. |
| `RETENTION_BRANCH_PATTERN` | Expresión regular personalizada de ramas | Vacía. |
| `SYNC_SOURCE` | Fuente autoritativa de sincronización | Coordinador. |
| `CORS_ALLOWED_ORIGINS` | Orígenes web adicionales | Ninguno; sólo mismo origen. |

El secreto `MAINTENANCE_CLUSTER_TOKEN` no se presenta como texto ni vuelve a la
plantilla. La aplicación lo administra mediante el **código de incorporación**:
el primer nodo genera uno y los demás lo reutilizan. Desde Configuración puede
consultarse, copiarse o sustituirse para incorporar el nodo a otro clúster.

## Campos retirados de la plantilla antigua

| Campo antiguo | Motivo |
| --- | --- |
| Directorio de secretos | La cuenta, el secreto del clúster y el estado viven con permisos restrictivos dentro del volumen persistente. |
| `MAINTENANCE_ADMIN_TOKEN_FILE` | El login y la sesión del portal sustituyeron el token administrativo. |
| `MAINTENANCE_CLUSTER_TOKEN_FILE` | La aplicación genera y guarda el secreto; el usuario utiliza el código de incorporación. |
| `STATS_ENABLED` | El panel debe permanecer disponible para iniciar sesión y modificar la configuración. |

Las rutas internas `REGISTRY_URL`, `REGISTRY_DATA`, `REGISTRY_CONFIG`,
`WEB_ROOT`, `PORT`, `MAINTENANCE_FLAG`, `APP_STATE_FILE` y
`LOCAL_REGISTRY_CONFIG_FILE` son parte del contrato de la imagen. No describen
el entorno del usuario y no se muestran como ajustes funcionales.
