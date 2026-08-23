# DLR web image: build the SPA, then serve it with Nginx behind /api proxy.
FROM node:22-alpine AS build

ARG TARGETPLATFORM=unknown
ARG TARGETARCH=unknown

WORKDIR /app

COPY web/package.json web/package-lock.json ./
RUN printf 'Installing locked web dependencies for %s (%s)\n' "$TARGETPLATFORM" "$TARGETARCH" \
    && npm ci --include=optional \
    && node --input-type=module -e 'import("rollup").then(() => console.log("Rollup native binding resolved"))'

COPY web/ ./
RUN npm run build

FROM nginx:alpine

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY docker/nginx-account.conf /etc/nginx/nginx-account.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
