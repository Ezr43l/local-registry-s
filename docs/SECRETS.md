# Credenciales de Local Registry

Desde `v1.2.5`, una instalación nueva no pide secretos en la plantilla de
Unraid ni en `.env`. La primera visita crea una cuenta propietaria local y el
asistente genera `cluster-token` para firmar la coordinación entre nodos.

Los valores son diferentes, aleatorios y quedan en el volumen persistente:

```text
/var/lib/registry/.local-registry/secrets/cluster-token
/var/lib/registry/.local-registry/auth.json
```

El directorio usa modo `0700` y los ficheros `0600`. Los valores no aparecen en
`docker inspect`, la plantilla, Compose ni el repositorio.

## Instalación aislada

Crea la cuenta propietaria y completa el asistente sin código de incorporación.
El código generado puede descartarse si el Registry siempre será aislado.

## Clúster

Configura primero un nodo sin código de incorporación y guarda el código que
muestra la WebUI. En cada nodo adicional,
describe la misma lista de miembros y pega ese código. El código transporta las
credenciales internas necesarias y deja de ser necesario al finalizar el alta.

No publiques el código ni lo guardes en una plantilla. Trátalo como una
credencial mientras se incorporan nodos.

## Backup y restauración

Las credenciales forman parte de `/var/lib/registry`; por tanto, el backup del
volumen debe incluir `.local-registry`. Restaura el volumen completo con el
contenedor detenido y conserva sus permisos. Restaurar sólo los blobs sin la
configuración obligaría a una nueva instalación y rompería la coordinación con
los nodos existentes.

## Compatibilidad heredada

Las instalaciones anteriores pueden seguir proporcionando
`MAINTENANCE_CLUSTER_TOKEN_FILE` o la variable histórica directa. Ese valor
explícito mantiene prioridad para evitar cambios inesperados durante una
actualización. El antiguo token administrativo ya no autoriza acciones.

El script privado `deploy-registry.sh` mantiene su flujo de ficheros, permisos y
rollback para despliegues existentes. Una instalación pública nueva no necesita
ese directorio ni esos campos adicionales.
