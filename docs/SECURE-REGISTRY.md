# Acceso seguro al registro

Local Registry mantiene un único contenedor con el motor de imágenes, una puerta
HTTPS independiente y el panel. Si se reinicia el panel, la puerta sigue autorizando
las descargas. El motor interno escucha sólo en `127.0.0.1:5050` dentro del contenedor.
No publiques ese puerto.

## Primer arranque

1. Conserva el volumen persistente al recrear el contenedor.
2. Abre `https://SERVIDOR:5001`. La identidad inicial es privada: antes de confiar
   en el certificado, comprueba su huella con el administrador del servidor.
3. Obtén el código inicial desde la consola del contenedor, con
   `cat /var/lib/registry/.local-registry/security/bootstrap-code`.
4. Crea la cuenta propietaria y completa el asistente. El código inicial se consume.
5. En Configuración, añade las IP/nombres con los que se accederá al nodo y la
   dirección flotante, si existe. No incluyas protocolo ni puerto en esos nombres.
6. Limita las redes autorizadas a administración, clientes y pares. Usa `/32`
   para una sola IP IPv4 y `/128` para una IPv6. Las redes privadas iniciales
   permiten completar la instalación; redúcelas a tu instalación real.

## Clientes Docker

El puerto 5000 no permite acceso anónimo. Crea una credencial por cliente o aplicación:

| Permisos | Uso |
|---|---|
| Descargar | Servidores que únicamente ejecutan imágenes |
| Descargar y publicar | Equipos de construcción y publicación |
| Descargar, publicar y borrar | Herramientas de mantenimiento autorizadas |

Puedes limitar cada credencial a un repositorio, un espacio como `equipo/*`, o todos
con `*`. El catálogo completo requiere acceso a todos los repositorios; una credencial
limitada sigue pudiendo descargar sus imágenes sin consultar el catálogo global.

Guarda el token al crearlo. El panel sólo conserva su hash. Para sustituirlo, crea
otra credencial, actualiza el cliente y revoca la anterior. El mantenimiento y la
réplica usan una identidad interna que no se debe entregar a clientes externos.

Descarga el certificado público del grupo desde Configuración e instálalo como
`/etc/docker/certs.d/REGISTRO:PUERTO/ca.crt` en cada equipo Linux que use Docker.
Después ejecuta:

```bash
docker login REGISTRO:PUERTO --username USUARIO_DE_APLICACION
```

Introduce el token cuando Docker solicite la contraseña. No lo escribas como argumento
del comando ni en plantillas. No uses `--insecure` ni desactives la verificación TLS.
Zero Launcher prepara el certificado y una credencial de publicación propia cuando
administra el Local Registry instalado en ese mismo servidor.

## Grupo de nodos

Todos los pares deben usar direcciones HTTPS y la misma identidad de grupo. El primer
nodo genera un código de incorporación con el secreto y la autoridad de certificados
del grupo. Este código concede confianza administrativa: compártelo por un medio
seguro, sólo con administradores. Al incorporar un nodo, su cuenta inicial se sustituye
por la cuenta verificada del grupo.

Las credenciales se modifican en el coordinador y se distribuyen con peticiones y
respuestas firmadas. Si falta un nodo, el panel avisa y reintenta la distribución
cuando vuelva. Una revocación no se debe considerar completada en un nodo que siga
desconectado; mantenlo aislado hasta que reciba la actualización.

## Migración desde una instalación HTTP

La actualización cambia el protocolo y la autenticación. Antes de cambiar la imagen:

1. Conserva una copia privada de la configuración, cuentas y registros. Conserva las
   imágenes anteriores según la política existente, sin borrarlas manualmente.
2. Identifica los motores Docker, publicadores, scripts y consultas directas a 5000/5001.
3. Prepara una identidad de certificados común, certificados para las direcciones
   reales de cada nodo y credenciales de clientes con el menor permiso necesario.
4. Instala certificados y credenciales en los clientes y convierte las URLs de pares
   a HTTPS. No actives la exigencia de credenciales antes de preparar a los consumidores.
5. Prueba primero un nodo y comprueba descarga, publicación, mantenimiento en simulación,
   réplica, login y revocación. Después instala la misma imagen en los demás.
6. Retira las entradas de estos registros de `insecure-registries`. Docker permite
   recargar su configuración sin reiniciar todos los contenedores; verifica el resultado.

`configure-docker-client.py` es una utilidad para hosts Linux con Python 3.9 o posterior.
Recibe `--config /RUTA/PRIVADA/cliente.json`, con permisos `0600`. El formato es:

```json
{
  "ca_file": "/srv/local-registry/.local-registry/security/ca.pem",
  "endpoints": {
    "registry.example:5000": {
      "username": "USUARIO_GENERADO",
      "password": "TOKEN_GENERADO_DE_AL_MENOS_32_CARACTERES"
    }
  },
  "firewall": {
    "ports": [5000, 5001],
    "policy_file": "/srv/local-registry/.local-registry/security/policy.json"
  }
}
```

La utilidad conserva las demás opciones de Docker, instala el certificado, añade
la credencial y retira únicamente estos endpoints de la lista insegura. Si existe
un almacén externo de contraseñas, utiliza `docker login` en lugar de escribirlo.
El bloque `firewall` es opcional; filtra esos puertos en IPv4 e IPv6, incluidas las
conexiones publicadas por Docker. `--watch` conserva el filtro al cambiar las redes
desde la app; su ejecución duplicada está protegida por un bloqueo local.

En sistemas donde `/etc` y `/root` no persisten, instala el hook de arranque junto a
la utilidad y guarda en `client-config-path` la ruta del JSON privado sobre el volumen
de datos. El hook espera a que ese volumen esté disponible. No guardes tokens en una
partición compartida ni publiques el fichero privado.

## Sesiones, registros y copias

El cierre de sesión revoca la sesión utilizada. Las sesiones anteriores a esta
actualización dejan de ser válidas: vuelve a iniciar sesión con la misma cuenta.
El panel y la puerta registran accesos o rechazos sin contraseñas, tokens, cookies
ni cuerpos. La plantilla limita el tamaño y la rotación de los registros Docker.

El volumen contiene material privado de certificados y credenciales de servicio.
Protege y cifra sus copias según tu política. Si se expone el secreto del grupo,
renueva su identidad y las credenciales; borrar una copia publicada no revoca secretos.
