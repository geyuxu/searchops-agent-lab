FROM node:24.18.0-alpine AS dependencies
WORKDIR /workspace/apps/storefront
COPY apps/storefront/package.json apps/storefront/package-lock.json ./
COPY packages/ui /workspace/packages/ui
RUN npm ci

FROM node:24.18.0-alpine AS build
WORKDIR /workspace/apps/storefront
ARG NEXT_PUBLIC_SEARCH_SERVICE_URL
ARG NEXT_PUBLIC_COMMERCE_SERVICE_URL
ENV NEXT_PUBLIC_SEARCH_SERVICE_URL=$NEXT_PUBLIC_SEARCH_SERVICE_URL \
    NEXT_PUBLIC_COMMERCE_SERVICE_URL=$NEXT_PUBLIC_COMMERCE_SERVICE_URL \
    NEXT_TELEMETRY_DISABLED=1
COPY --from=dependencies /workspace/apps/storefront/node_modules ./node_modules
COPY apps/storefront .
COPY packages/ui /workspace/packages/ui
RUN npm run build

FROM node:24.18.0-alpine
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app
RUN addgroup -S nextjs && adduser -S nextjs -G nextjs
COPY --from=build /workspace/apps/storefront/public ./apps/storefront/public
COPY --from=build --chown=nextjs:nextjs /workspace/apps/storefront/.next/standalone ./
COPY --from=build --chown=nextjs:nextjs /workspace/apps/storefront/.next/static ./apps/storefront/.next/static
USER nextjs
EXPOSE 3000
CMD ["node", "apps/storefront/server.js"]
