import type { NextConfig } from "next"
import path from "node:path"

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: path.resolve(process.cwd(), "../.."),
  transpilePackages: ["@searchops/ui"],
  typedRoutes: false,
  turbopack: { root: path.resolve(process.cwd(), "../..") },
  experimental: { useTypeScriptCli: true }
}

export default nextConfig
