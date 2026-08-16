export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params
  const base = process.env.SEARCH_SERVICE_URL || "http://localhost:8080"
  try {
    const upstream = await fetch(`${base}/api/v1/products/${encodeURIComponent(id)}`, { cache: "no-store" })
    return new Response(await upstream.arrayBuffer(), {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") || "application/json" }
    })
  } catch (error) {
    return Response.json({ error: "product_unavailable", message: error instanceof Error ? error.message : "Product unavailable" }, { status: 503 })
  }
}

