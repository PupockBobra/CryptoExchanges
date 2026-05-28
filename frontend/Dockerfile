FROM node:22-alpine AS base
WORKDIR /app
COPY package.json package-lock.json* ./
# Use npm install on first run (no lock file yet); subsequent runs use ci
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

FROM base AS dev
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

FROM base AS build
# Vite replaces import.meta.env.VITE_* at build time — must be injected here
ARG VITE_API_URL
ARG VITE_ARBI_THRESHOLD=0.3
ENV VITE_API_URL=$VITE_API_URL
ENV VITE_ARBI_THRESHOLD=$VITE_ARBI_THRESHOLD
COPY . .
RUN npm run build

FROM nginx:alpine AS prod
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
