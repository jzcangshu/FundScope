FROM node:22-bookworm-slim AS frontend-build

WORKDIR /build/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

FROM caddy:2.8-alpine

COPY --from=frontend-build /build/frontend/dist /srv
