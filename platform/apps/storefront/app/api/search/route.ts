export async function GET(request: Request) {
  const incoming = new URL(request.url)
  const base = process.env.SEARCH_SERVICE_URL || "http://localhost:8080"
  try {
    const upstream = await fetch(`${base}/api/v1/search?${incoming.searchParams}`, {
      cache: "no-store",
      headers: { "x-request-id": request.headers.get("x-request-id") || crypto.randomUUID() }
    })
    return new Response(await upstream.arrayBuffer(), {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") || "application/json" }
    })
  } catch (error) {
    return Response.json({ error: "search_unavailable", message: error instanceof Error ? error.message : "Search unavailable" }, { status: 503 })
  }
}

