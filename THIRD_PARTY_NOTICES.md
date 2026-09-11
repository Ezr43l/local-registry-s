# Avisos de terceros

Local Registry se distribuye bajo Apache-2.0. La etiqueta OCI
`org.opencontainers.image.licenses=Apache-2.0` describe el código propio; no
reemplaza las licencias de los componentes de terceros incluidos en la imagen.
La imagen conserva el inventario y los textos aplicables bajo
`/usr/share/licenses/local-registry/`.

## Distribution

El binario `/bin/registry` se compila desde
`distribution/distribution` 3.1.1, commit
`9a8d98b679740cd514aa7e7d84d23d442a5ef54c`, bajo Apache-2.0. Esta distribución
lo modifica con el parche versionado
`docker/local-registry/patches/0001-inmemory-find-consumes-final-component.patch`
(SHA-256 `7231d9f29fc09d182a997c76dc7c8fc16729d644bec9d46a0164d897d32b080b`).
El parche cambia únicamente:

- `registry/storage/driver/inmemory/mfs.go`;
- `registry/storage/driver/inmemory/driver_test.go`.

La imagen incluye la licencia y `AUTHORS` originales, el parche aplicado, los
`go.mod`/`go.sum` efectivos y el listado de módulos enlazados en
`/usr/share/licenses/local-registry/distribution/` y
`/usr/share/licenses/local-registry/go/`. El código fuente correspondiente se
reconstruye con el commit anterior y el parche exacto del repositorio.

## Módulos Go

Las licencias, avisos, patentes y copyrights encontrados en el caché de módulos
usado por la compilación se copian, conservando sus rutas, en
`/usr/share/licenses/local-registry/go/module-cache/`. La licencia y el fichero
`PATENTS` del toolchain Go también se conservan. `BUILT-MODULES.txt`, generado
desde el binario final con `go version -m`, delimita los módulos realmente
enlazados; el directorio puede contener avisos adicionales de dependencias de
compilación o prueba.

## Panel web

El bundle servido desde `/app/web` incorpora React 19.1.0, React DOM 19.1.0 y
Scheduler 0.26.0 (MIT) y se genera con las dependencias fijadas en
`docker/local-registry/web/package-lock.json`. Para no perder avisos insertados
por herramientas de build, la imagen conserva el lockfile, el árbol `npm ls` y
todos los ficheros `LICENSE`, `NOTICE`, `COPYING`, `PATENTS` y `COPYRIGHT` del
árbol instalado en `/usr/share/licenses/local-registry/npm/`.

## Python y paquetes Alpine

La imagen base contiene CPython y pip; sus licencias se copian a
`/usr/share/licenses/local-registry/python/`. Para cada paquete APK instalado se
conserva la base original `/lib/apk/db/installed` y un manifiesto TSV con nombre,
versión, arquitectura, expresión de licencia, origen y commit de aports en
`/usr/share/licenses/local-registry/alpine/`. El inventario no virtual exacto que
se aceptó al construir la release también se conserva como
`RUNTIME-PACKAGES.lock`; el build se detiene si una actualización cambia ese
conjunto sin una revisión expresa.

Cada GitHub Release debe adjuntar `alpine-copyleft-sources.tar.gz` y
`alpine-copyleft-sources-manifest.tsv`. La puerta
`scripts/collect-alpine-copyleft-sources.sh` selecciona las familias recíprocas
enumeradas en `scripts/apk-installed-manifest.awk` —entre ellas AGPL, GPL, LGPL,
MPL, EPL, EUPL, CDDL y APSL— desde las bases APK exactas de AMD64 y ARM64,
extrae cada receta del commit aports registrado por el paquete y ejecuta
`abuild fetch verify`.
El archivo resultante incluye APKBUILD, parches/local files y todos los
distfiles verificados; la release no se publica si falta o cambia uno. No se
sustituye esa fuente acompañante por una promesa futura ni por enlaces sin
verificación.

Los textos canónicos de los identificadores SPDX inventariados están en
`third_party/spdx/`, fijados a `spdx/license-list-data` v3.28.0, commit
`c4a7237ec8f4654e867546f9f409749300f1bf4c`. Alpine también usa el literal
`Public-Domain`, que se conserva tal cual en el manifiesto y no se presenta como
un identificador SPDX. Los ficheros originales de cada módulo tienen prioridad
para sus copyrights y atribuciones concretas.

Este inventario es informativo y no altera los términos de ninguna licencia.
