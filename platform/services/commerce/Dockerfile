FROM node:24.18.0-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM node:24.18.0-bookworm-slim
ENV NODE_ENV=production MEDUSA_FF_CACHING=true MEDUSA_DISABLE_TELEMETRY=true
WORKDIR /app
COPY package.json package-lock.json ./
COPY --from=build /app/node_modules ./node_modules
RUN npm prune --omit=dev --no-audit --no-fund && npm cache clean --force
COPY --from=build /app/.medusa/server ./
COPY --from=build /app/medusa-config.ts ./medusa-config.ts
COPY --from=build /app/tsconfig.json ./tsconfig.json
COPY --from=build /app/scripts ./scripts
EXPOSE 9000
CMD ["npm", "start"]
