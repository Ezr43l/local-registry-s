# syntax=docker/dockerfile:1.7

ARG NODE_IMAGE=node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32
ARG GO_IMAGE=golang:1.27.0-alpine3.24@sha256:4c9fe60190a2a3350ddc51de80d0224b8a6698d12bdfc999fee45ea9d6c46dbc
ARG PYTHON_IMAGE=python:3.12-alpine@sha256:d09d15e60962ca365d1cd544a48773bac9d33f2fb1b00f2aa0deec78ade7dc31
ARG APP_VERSION=v1.2.12
ARG DISTRIBUTION_PATCH_SHA256=7231d9f29fc09d182a997c76dc7c8fc16729d644bec9d46a0164d897d32b080b

FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS web
WORKDIR /web
ARG APP_VERSION
ENV VITE_APP_VERSION=$APP_VERSION
COPY docker/local-registry/web/package.json docker/local-registry/web/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY docker/local-registry/web/ ./
RUN npm run build
RUN set -eu; \
    license_root=/out/licenses/npm; \
    mkdir -p "$license_root/files"; \
    cp package.json package-lock.json "$license_root/"; \
    npm ls --all --json > "$license_root/dependency-tree.json"; \
    find node_modules -type f \
      \( -iname 'LICENSE*' -o -iname 'COPYING*' -o -iname 'NOTICE*' \
         -o -iname 'PATENTS*' -o -iname 'COPYRIGHT*' \) -print \
      | while IFS= read -r license_file; do \
          relative="${license_file#node_modules/}"; \
          destination="$license_root/files/$(dirname "$relative")"; \
          mkdir -p "$destination"; \
          cp "$license_file" "$destination/$(basename "$relative")"; \
        done; \
    test "$(find "$license_root/files" -type f | wc -l | tr -d ' ')" -gt 0

FROM --platform=$BUILDPLATFORM ${GO_IMAGE} AS registry-builder
ARG TARGETOS
ARG TARGETARCH
ARG TARGETVARIANT
ARG DISTRIBUTION_COMMIT=9a8d98b679740cd514aa7e7d84d23d442a5ef54c
ARG DISTRIBUTION_VERSION=3.1.1-secure.2
ARG DISTRIBUTION_PATCH_SHA256
ENV CGO_ENABLED=0 \
    GOTOOLCHAIN=local
COPY docker/local-registry/patches/0001-inmemory-find-consumes-final-component.patch /tmp/distribution.patch
RUN printf '%s  %s\n' "$DISTRIBUTION_PATCH_SHA256" /tmp/distribution.patch \
      | sha256sum -c -
RUN apk add --no-cache ca-certificates git \
    && git init /src \
    && git -C /src remote add origin https://github.com/distribution/distribution.git \
    && git -C /src fetch --depth=1 origin "$DISTRIBUTION_COMMIT" \
    && git -C /src checkout --detach FETCH_HEAD \
    && test "$(git -C /src rev-parse HEAD)" = "$DISTRIBUTION_COMMIT" \
    && git -C /src apply --check /tmp/distribution.patch \
    && git -C /src apply /tmp/distribution.patch \
    && test "$(git -C /src diff --name-only | wc -l | tr -d ' ')" = "2" \
    && test "$(git -C /src diff --name-only -- registry/storage/driver/inmemory/driver_test.go)" = \
      "registry/storage/driver/inmemory/driver_test.go" \
    && test "$(git -C /src diff --name-only -- registry/storage/driver/inmemory/mfs.go)" = \
      "registry/storage/driver/inmemory/mfs.go" \
    && git -C /src diff --check \
    && rm -rf /src/vendor
WORKDIR /src
RUN go mod edit -go=1.27.0 \
    && go get \
      golang.org/x/crypto@v0.55.0 \
      golang.org/x/net@v0.58.0 \
      golang.org/x/text@v0.41.0 \
      google.golang.org/grpc@v1.83.2 \
    && go mod tidy \
    && test "$(go list -m -f '{{.Version}}' golang.org/x/crypto)" = "v0.55.0" \
    && test "$(go list -m -f '{{.Version}}' golang.org/x/net)" = "v0.58.0" \
    && test "$(go list -m -f '{{.Version}}' golang.org/x/text)" = "v0.41.0" \
    && test "$(go list -m -f '{{.Version}}' google.golang.org/grpc)" = "v1.83.2" \
    && go test -p 1 -short ./...
RUN git tag --force v3.1.1 "$DISTRIBUTION_COMMIT" \
    && test "$(git rev-list -n 1 v3.1.1)" = "$DISTRIBUTION_COMMIT"
RUN GOOS="$TARGETOS" GOARCH="$TARGETARCH" GOARM="${TARGETVARIANT#v}" \
    go build -trimpath \
      -ldflags="-s -w -X github.com/distribution/distribution/v3/version.version=$DISTRIBUTION_VERSION -X github.com/distribution/distribution/v3/version.revision=$DISTRIBUTION_COMMIT -X github.com/distribution/distribution/v3/version.mainpkg=github.com/distribution/distribution/v3" \
      -o /out/registry ./cmd/registry \
    && license_root=/out/licenses \
    && mkdir -p "$license_root/distribution" "$license_root/go/module-cache" "$license_root/go/toolchain" \
    && go version -m /out/registry | tee "$license_root/go/BUILT-MODULES.txt" \
    && cp LICENSE AUTHORS go.mod go.sum "$license_root/distribution/" \
    && cp /tmp/distribution.patch "$license_root/distribution/APPLIED.patch" \
    && cp /usr/local/go/LICENSE /usr/local/go/PATENTS "$license_root/go/toolchain/" \
    && find /go/pkg/mod -type f \
      \( -iname 'LICENSE*' -o -iname 'COPYING*' -o -iname 'NOTICE*' \
         -o -iname 'PATENTS*' -o -iname 'COPYRIGHT*' \) -print \
      | while IFS= read -r license_file; do \
          relative="${license_file#/go/pkg/mod/}"; \
          destination="$license_root/go/module-cache/$(dirname "$relative")"; \
          mkdir -p "$destination"; \
          cp "$license_file" "$destination/$(basename "$relative")"; \
        done \
    && test "$(find "$license_root/go/module-cache" -type f | wc -l | tr -d ' ')" -gt 0

FROM ${PYTHON_IMAGE} AS runtime-base

ARG APP_VERSION
ARG BUILD_DATE=""
ARG VCS_REF="unknown"
ARG SOURCE_URL="https://github.com/Ezr43l/local-registry-s"
ARG LICENSE="Apache-2.0"
ARG DISTRIBUTION_COMMIT=9a8d98b679740cd514aa7e7d84d23d442a5ef54c
ARG DISTRIBUTION_VERSION=3.1.1-secure.2
ARG DISTRIBUTION_PATCH_SHA256

LABEL org.opencontainers.image.title="Local Registry" \
      org.opencontainers.image.description="Registro OCI local con panel de inventario y mantenimiento" \
      org.opencontainers.image.source="$SOURCE_URL" \
      org.opencontainers.image.version="$APP_VERSION" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.created="$BUILD_DATE" \
      org.opencontainers.image.licenses="$LICENSE" \
      net.unraid.docker.managed="dockerman" \
      net.unraid.docker.webui="http://[IP]:[PORT:5001]/" \
      net.unraid.docker.icon="https://www.docker.com/wp-content/uploads/2022/03/Moby-logo.png" \
      io.ezr43l.distribution.upstream.version="3.1.1" \
      io.ezr43l.distribution.upstream.revision="$DISTRIBUTION_COMMIT" \
      io.ezr43l.distribution.security-rebuild="$DISTRIBUTION_VERSION" \
      io.ezr43l.distribution.patch.sha256="$DISTRIBUTION_PATCH_SHA256" \
      io.ezr43l.licenses.notices="/usr/share/licenses/local-registry/THIRD_PARTY_NOTICES.md" \
      io.ezr43l.licenses.apk-manifest="/usr/share/licenses/local-registry/alpine/APK-PACKAGES.tsv"

COPY LICENSE /usr/share/licenses/local-registry/project/LICENSE
COPY THIRD_PARTY_NOTICES.md /usr/share/licenses/local-registry/THIRD_PARTY_NOTICES.md
COPY scripts/apk-installed-manifest.awk /usr/share/licenses/local-registry/apk-installed-manifest.awk
COPY third_party/spdx /usr/share/licenses/local-registry/spdx
COPY third_party/alpine-runtime-packages.lock /usr/share/licenses/local-registry/alpine/RUNTIME-PACKAGES.lock
COPY --from=registry-builder /out/licenses /usr/share/licenses/local-registry
COPY --from=web /out/licenses/npm /usr/share/licenses/local-registry/npm

RUN apk upgrade --no-cache \
    && apk add --no-cache ca-certificates tzdata \
    && apk info -v | sed '/^\./d' | sort > /tmp/runtime-packages.actual \
    && cmp /usr/share/licenses/local-registry/alpine/RUNTIME-PACKAGES.lock \
        /tmp/runtime-packages.actual \
    && rm /tmp/runtime-packages.actual \
    && mkdir -p /usr/share/licenses/local-registry/alpine \
        /usr/share/licenses/local-registry/python \
    && cp /lib/apk/db/installed \
        /usr/share/licenses/local-registry/alpine/installed.db \
    && awk -f /usr/share/licenses/local-registry/apk-installed-manifest.awk \
        /lib/apk/db/installed \
        > /usr/share/licenses/local-registry/alpine/APK-PACKAGES.tsv \
    && cp /usr/local/lib/python3.12/LICENSE.txt \
        /usr/share/licenses/local-registry/python/CPython-LICENSE.txt \
    && for license_file in /usr/local/lib/python3.12/site-packages/*.dist-info/LICENSE*; do \
         test -f "$license_file" || continue; \
         package_dir="$(basename "$(dirname "$license_file")")"; \
         mkdir -p "/usr/share/licenses/local-registry/python/$package_dir"; \
         cp "$license_file" "/usr/share/licenses/local-registry/python/$package_dir/"; \
       done

COPY --from=registry-builder /out/registry /bin/registry
COPY docker/local-registry/config.yml /etc/docker/registry/config.yml
COPY docker/local-registry/server.py /app/server.py
COPY LICENSE /app/LICENSE
COPY --chmod=0755 docker/local-registry/entrypoint.sh /entrypoint.sh

ENV APP_VERSION=$APP_VERSION \
    REGISTRY_URL=http://127.0.0.1:5000 \
    REGISTRY_DATA=/var/lib/registry \
    REGISTRY_CONFIG=/etc/docker/registry/config.yml \
    WEB_ROOT=/app/web \
    PORT=5001 \
    MAINTENANCE_FLAG=/run/registry-maintenance.flag \
    OTEL_TRACES_EXPORTER=none \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

FROM runtime-base AS tests
WORKDIR /src
COPY tests ./tests
COPY VERSION render-unraid-template.py ./
COPY unraid ./unraid
COPY docker/local-registry/Dockerfile ./docker/local-registry/Dockerfile
COPY docker/local-registry/patches ./docker/local-registry/patches
COPY docker/local-registry/web/package.json ./docker/local-registry/web/package.json
COPY docker/local-registry/web/src ./docker/local-registry/web/src
COPY docker/local-registry/server.py ./docker/local-registry/server.py
COPY docker/local-registry/entrypoint.sh ./docker/local-registry/entrypoint.sh
COPY THIRD_PARTY_NOTICES.md ./
COPY scripts ./scripts
COPY third_party ./third_party
COPY .env.example docker-compose.yml deploy-registry.sh docker-registry.sh mirror-registries.sh ./
RUN python3 -m unittest discover -s tests -v \
    && touch /tmp/local-registry-tests-passed

FROM runtime-base AS runtime
COPY --from=tests /tmp/local-registry-tests-passed /app/.tests-passed
COPY --from=web /web/dist /app/web

EXPOSE 5000 5001
VOLUME ["/var/lib/registry"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/v2/', timeout=4).read()"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["/entrypoint.sh"]
