# syntax=docker/dockerfile:1
# WorkplaceOps UI: Vite build served by nginx.
# Build: docker build --build-arg VITE_API_BASE_URL=http://localhost:8000 -t workplaceops-ui frontend
FROM node:22-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Public API base URL baked into the bundle at build time. Never pass secrets as VITE_* values.
ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
