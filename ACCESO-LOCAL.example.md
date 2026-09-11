# Configuración local de ejemplo

Este fichero documenta la forma de mantener accesos fuera del repositorio.
No guardes aquí valores de claves, tokens ni contraseñas.

```text
SECRETS_DIR=/ruta/privada/a/las/claves
NODES_CONFIG=node-a:host-a:key-a,node-b:host-b:key-b
PEER_KEY=/ruta/privada/a/las/claves/registry-peer.key
```

El fichero real de cada instalación debe permanecer fuera del repositorio o
estar ignorado por Git.
