# syntax=docker/dockerfile:1.7

FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b

RUN apk add --no-cache \
      abuild=3.17.0-r0 \
      bash=5.3.9-r1 \
      coreutils=9.11-r0 \
      git=2.54.0-r0 \
      gzip=1.14-r3 \
      python3=3.14.7-r1 \
      tar=1.35-r5 \
    && adduser -D -h /home/source-builder source-builder

WORKDIR /repo
ENTRYPOINT ["bash", "scripts/collect-alpine-copyleft-sources.sh"]
