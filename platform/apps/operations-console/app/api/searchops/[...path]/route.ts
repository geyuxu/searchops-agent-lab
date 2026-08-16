const service = process.env.SEARCH_SERVICE_URL || "http://localhost:8080"

async function forward(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params
  const incoming = new URL(request.url)
  const target = `${service}/api/v1/${path.map(encodeURIComponent).join("/")}${incoming.search}`
  const headers: Record<string, string> = {
    "content-type": request.headers.get("content-type") || "application/json",
    "x-request-id": request.headers.get("x-request-id") || crypto.randomUUID()
  }
  const idempotency = request.headers.get("idempotency-key")
  if (idempotency) headers["idempotency-key"] = idempotency
  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : await request.arrayBuffer(),
      cache: "no-store"
    })
    return new Response(await upstream.arrayBuffer(), {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") || "application/json" }
    })
  } catch (error) {
    return Response.json({ error: "searchops_unavailable", message: error instanceof Error ? error.message : "SearchOps unavailable" }, { status: 503 })
  }
}

export const GET = forward
export const POST = forward
export const PATCH = forward
export const DELETE = forward

