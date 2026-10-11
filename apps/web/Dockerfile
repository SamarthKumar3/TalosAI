FROM node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS dependencies

WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json ./
COPY apps/web/vendor ./vendor
RUN npm ci

FROM node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS builder

ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY --from=dependencies /app/node_modules ./node_modules
COPY apps/web ./
RUN npm run build

FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6 AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    NODE_OPTIONS=--max-old-space-size=256
WORKDIR /app
RUN apk upgrade --no-cache \
    && apk add --no-cache libstdc++ ca-certificates \
    && addgroup --system --gid 1001 talos \
    && adduser --system --uid 1001 --ingroup talos talos
COPY --from=dependencies /usr/local/bin/node /usr/local/bin/node
COPY --from=builder --chown=talos:talos /app/public ./public
COPY --from=builder --chown=talos:talos /app/.next/standalone ./
COPY --from=builder --chown=talos:talos /app/.next/static ./.next/static
USER talos
EXPOSE 3000
CMD ["node", "server.js"]
