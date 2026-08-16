import { defineConfig, loadEnv } from "@medusajs/framework/utils"

loadEnv(process.env.NODE_ENV || "development", process.cwd())

module.exports = defineConfig({
  admin: {
    disable: true
  },
  projectConfig: {
    databaseUrl: process.env.DATABASE_URL,
    databaseDriverOptions: {
      connection: { ssl: false }
    },
    redisUrl: process.env.REDIS_URL,
    http: {
      storeCors: process.env.STORE_CORS || "http://localhost:3000",
      adminCors: process.env.ADMIN_CORS || "http://localhost:9000,http://localhost:3001",
      authCors: process.env.AUTH_CORS || "http://localhost:3000,http://localhost:3001",
      jwtSecret: process.env.JWT_SECRET || "local-demo-jwt-secret-change-me",
      cookieSecret: process.env.COOKIE_SECRET || "local-demo-cookie-secret-change-me"
    }
  },
  modules: process.env.REDIS_URL
    ? [
        {
          resolve: "@medusajs/medusa/caching",
          options: {
            providers: [
              {
                resolve: "@medusajs/caching-redis",
                id: "caching-redis",
                is_default: true,
                options: {
                  redisUrl: process.env.REDIS_URL,
                  prefix: "searchops:commerce:",
                  ttl: 300
                }
              }
            ]
          }
        }
      ]
    : []
})
