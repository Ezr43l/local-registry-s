# Parche de Docker Distribution 3.1.1

La imagen parte del tag oficial `v3.1.1`, commit
`9a8d98b679740cd514aa7e7d84d23d442a5ef54c`, y aplica antes de compilar el
parche versionado
`docker/local-registry/patches/0001-inmemory-find-consumes-final-component.patch`.
Su SHA-256 fijado en el Dockerfile es
`7231d9f29fc09d182a997c76dc7c8fc16729d644bec9d46a0164d897d32b080b`.

## Causa

`inmemory.dir.find` seguía descendiendo cuando ya había consumido el último
componente (`i == -1`). Si el directorio encontrado contenía otro hijo con el
mismo nombre, por ejemplo `/repeat/repeat`, devolvía el hijo en vez del padre
solicitado. Una operación posterior podía responder `Path not found` aun cuando
la ruta existía. La aleatoriedad de las rutas del test hacía intermitente el
síntoma observado en `TestDeleteOnlyDeletesSubpaths`; el mismo defecto también
se reprodujo en `TestDeleteFolder`.

El problema está registrado en el proyecto upstream como
[distribution/distribution#4451](https://github.com/distribution/distribution/issues/4451).
El driver `inmemory` no es sólo un helper: `cmd/registry/main.go` lo registra en
el binario, por lo que ocultar o reintentar el test habría dejado un defecto
alcanzable mediante configuración.

## Alcance y puertas

El parche detiene la recursión al consumir el último componente y añade dos
regresiones explícitas para componentes adyacentes iguales y para el contraste
`/prefix/prefix` frente a `/prefix/prefixsuffix`. No omite pruebas ni incorpora
reintentos. El build verifica el checksum, exige que el parche sólo modifique
`mfs.go` y `driver_test.go`, ejecuta `git diff --check` y mantiene una única
ejecución serial de `go test -p 1 -short ./...` antes de compilar. La
serialización evita falsos negativos en pruebas temporizadas del upstream
cuando BuildKit comparte CPU, sin omitir paquetes ni pruebas.

El binario resultante se identifica como `3.1.1-secure.2` y la imagen incluye el
checksum en `io.ezr43l.distribution.patch.sha256`.
