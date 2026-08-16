FROM node:24.18.0-alpine AS dependencies
WORKDIR /workspace/apps/operations-console
COPY apps/operations-console/package.json apps/operations-console/package-lock.json ./
COPY packages/ui /workspace/packages/ui
RUN npm ci

FROM node:24.18.0-alpine AS build
WORKDIR /workspace/apps/operations-console
ARG NEXT_PUBLIC_SEARCH_SERVICE_URL
ENV NEXT_PUBLIC_SEARCH_SERVICE_URL=$NEXT_PUBLIC_SEARCH_SERVICE_URL NEXT_TELEMETRY_DISABLED=1
COPY --from=dependencies /workspace/apps/operations-console/node_modules ./node_modules
COPY apps/operations-console .
COPY packages/ui /workspace/packages/ui
RUN npm run build

FROM node:24.18.0-alpine
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3001 HOSTNAME=0.0.0.0
WORKDIR /app
RUN addgroup -S nextjs && adduser -S nextjs -G nextjs
COPY --from=build /workspace/apps/operations-console/public ./apps/operations-console/public
COPY --from=build --chown=nextjs:nextjs /workspace/apps/operations-console/.next/standalone ./
COPY --from=build --chown=nextjs:nextjs /workspace/apps/operations-console/.next/static ./apps/operations-console/.next/static
USER nextjs
EXPOSE 3001
CMD ["node", "apps/operations-console/server.js"]
