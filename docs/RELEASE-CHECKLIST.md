# Publicación de Local Registry

El repositorio público contiene un snapshot limpio de la versión estable. El
desarrollo y su historial permanecen en el repositorio privado.

## Proceso

1. Actualizar `VERSION`, Compose, la plantilla Unraid y el paquete web.
2. Ejecutar todas las pruebas en un host Docker antes de publicar.
3. Crear un tag anotado cuyo nombre coincida exactamente con `VERSION`.
4. Construir y publicar manualmente la imagen para AMD64 y ARM64.
5. La versión exacta y el alias `stable` apuntan a la misma imagen.
6. Comprobar que la imagen puede descargarse sin credenciales.
7. Crear la release de GitHub con las fuentes de esa versión.

El repositorio público no ejecuta automatizaciones de GitHub. De este modo,
la publicación de una versión estable siempre es una decisión manual.

## Versión actual

- Versión: `v1.2.13`.
- Imagen: `ghcr.io/ezr43l/local-registry-s:v1.2.13`.
- Alias estable: `ghcr.io/ezr43l/local-registry-s:stable`.
- Plantilla Unraid: `unraid/my-Local-Registry.xml`.

La plantilla usa la versión exacta para que una instalación no cambie sin que
el usuario lo decida. Quien quiera seguir siempre la última versión estable
puede utilizar el alias `stable`.

## Comprobaciones obligatorias

- El repositorio no contiene secretos, direcciones privadas ni nombres del
  entorno de desarrollo.
- La imagen incluye la licencia Apache-2.0 y los avisos de terceros.
- Las pruebas de la aplicación y el análisis de vulnerabilidades terminan sin
  errores antes de crear el tag.
- La imagen publicada contiene variantes `linux/amd64` y `linux/arm64`.
