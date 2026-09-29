# Phase 31: multi-stage, using Next's own `output: "standalone"` (next.config.mjs).
# A prior single-stage image shipped the entire npm CLI into production --
# including npm's own bundled internal dependencies (pacote, tar, sigstore,
# etc.), none of which this app ever calls at runtime -- and a Trivy scan of
# it found real, otherwise-unfixable-by-us CVEs in exactly that code. The
# runtime stage below has no npm at all: `next build` traces which production
# dependencies the built server actually imports and prunes everything else
# into .next/standalone, which is all this stage copies in.
FROM node:20-slim AS builder

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .
RUN npm run build

FROM node:20-slim AS runner

# Pick up the Debian security fixes published since this base image was built
# (mirrors backend/Dockerfile's own fix). Same base distro, same class of
# finding: libcap2/libgnutls30/libpcre2-8-0, all with published fixes.
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV NODE_ENV=production
ENV PORT=3000
# The standalone server binds to `localhost` unless told otherwise, which
# would be unreachable from outside the container despite the EXPOSE/publish.
ENV HOSTNAME=0.0.0.0

# node:20-slim bundles npm's own CLI regardless of what this Dockerfile
# installs -- standalone output only prunes THIS APP's dependency tree
# (.next/standalone/node_modules), never npm's own bundled internals at
# /usr/local/lib/node_modules/npm. This runner only ever runs `node
# server.js`; npm itself is dead weight here, and its own internal deps
# (pacote/tar/sigstore/etc, none of them this app's) are exactly what a scan
# of the earlier single-stage image found.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx /usr/local/lib/node_modules/corepack /usr/local/bin/corepack

# No `public/` directory exists in this app (yet) -- nothing to copy for it.
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

EXPOSE 3000

CMD ["node", "server.js"]
