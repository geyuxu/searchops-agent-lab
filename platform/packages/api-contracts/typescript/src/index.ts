export type QueryRewriteRequest = { query: string; locale: string; filters: Record<string, unknown>; request_id: string }
export type QueryRewriteResponse = { original_query: string; rewritten_query: string; extracted_filters: Record<string, unknown>; confidence: number; explanation: string; provider: string; latency_ms: number }
export type Candidate = { product_id: string; title?: string; brand?: string; description?: string; bm25_score?: number }
export type RerankRequest = { query: string; candidates: Candidate[]; request_id: string }
export type RerankResponse = { ranked_product_ids: string[]; scores: { product_id: string; score: number }[]; explanation: string; provider: string; latency_ms: number }
export type StrategySuggestRequest = { query_metrics: Record<string, unknown>[]; current_strategy: Record<string, unknown>; evidence: (Record<string, unknown> | string)[]; request_id: string }
export type StrategySuggestResponse = { proposed_changes: { operation: string; path: string; value: unknown; reason: string }[]; expected_impact: string; evidence_refs: string[]; confidence: number; risk_level: "low" | "medium" | "high"; requires_approval: true; provider: string; latency_ms: number }

export class AiAdapterError extends Error {
  constructor(public status: number, message: string, public requestId?: string) { super(message) }
}

export class AiAdapterClient {
  constructor(private readonly baseUrl: string, private readonly timeoutMs = 400, private readonly fetcher: typeof fetch = fetch) {}

  health() { return this.request<{ status: string; provider: string; llm_required: boolean }>("/ai/health") }
  rewrite(payload: QueryRewriteRequest) { return this.request<QueryRewriteResponse>("/ai/query-rewrite", payload) }
  rerank(payload: RerankRequest) { return this.request<RerankResponse>("/ai/rerank", payload) }
  suggestStrategy(payload: StrategySuggestRequest) { return this.request<StrategySuggestResponse>("/ai/strategy-suggest", payload) }

  private async request<T>(path: string, payload?: unknown): Promise<T> {
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs)
    try {
      const response = await this.fetcher(`${this.baseUrl.replace(/\/$/, "")}${path}`, {
        method: payload === undefined ? "GET" : "POST",
        headers: { "content-type": "application/json", ...(payload && typeof payload === "object" && "request_id" in payload ? { "x-request-id": String((payload as { request_id: unknown }).request_id) } : {}) },
        body: payload === undefined ? undefined : JSON.stringify(payload),
        signal: controller.signal
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok) throw new AiAdapterError(response.status, body.message || `AI adapter returned ${response.status}`, response.headers.get("x-request-id") || undefined)
      return body as T
    } finally { clearTimeout(timeout) }
  }
}

