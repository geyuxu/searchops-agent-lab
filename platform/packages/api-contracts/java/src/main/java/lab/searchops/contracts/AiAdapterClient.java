package lab.searchops.contracts;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import tools.jackson.databind.ObjectMapper;

public final class AiAdapterClient {
    private final URI baseUri;
    private final Duration timeout;
    private final HttpClient http;
    private final ObjectMapper json;

    public AiAdapterClient(URI baseUri, Duration timeout) {
        this(baseUri, timeout, HttpClient.newBuilder().connectTimeout(timeout).build(), new ObjectMapper());
    }

    AiAdapterClient(URI baseUri, Duration timeout, HttpClient http, ObjectMapper json) {
        this.baseUri = baseUri;
        this.timeout = timeout;
        this.http = http;
        this.json = json;
    }

    public HealthResponse health() { return get("/ai/health", HealthResponse.class); }
    public QueryRewriteResponse rewrite(QueryRewriteRequest value) { return post("/ai/query-rewrite", value, value.request_id(), QueryRewriteResponse.class); }
    public RerankResponse rerank(RerankRequest value) { return post("/ai/rerank", value, value.request_id(), RerankResponse.class); }
    public StrategySuggestResponse suggestStrategy(StrategySuggestRequest value) { return post("/ai/strategy-suggest", value, value.request_id(), StrategySuggestResponse.class); }

    private <T> T get(String path, Class<T> type) {
        var request = HttpRequest.newBuilder(baseUri.resolve(path)).timeout(timeout).GET().build();
        return send(request, type);
    }

    private <T> T post(String path, Object value, String requestId, Class<T> type) {
        var request = HttpRequest.newBuilder(baseUri.resolve(path)).timeout(timeout)
                .header("content-type", "application/json").header("x-request-id", requestId)
                .POST(HttpRequest.BodyPublishers.ofString(json.writeValueAsString(value))).build();
        return send(request, type);
    }

    private <T> T send(HttpRequest request, Class<T> type) {
        try {
            var response = http.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() < 200 || response.statusCode() >= 300) {
                throw new AiAdapterException(response.statusCode(), response.body());
            }
            return json.readValue(response.body(), type);
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            throw new AiAdapterException(0, "AI adapter call interrupted", exception);
        } catch (java.io.IOException exception) {
            throw new AiAdapterException(0, "AI adapter unavailable", exception);
        }
    }

    public record HealthResponse(String status, String provider, boolean llm_required) {}
    public record QueryRewriteRequest(String query, String locale, Map<String, Object> filters, String request_id) {}
    public record QueryRewriteResponse(String original_query, String rewritten_query, Map<String, Object> extracted_filters, double confidence, String explanation, String provider, long latency_ms) {}
    public record Candidate(String product_id, String title, String brand, String description, double bm25_score) {}
    public record RerankRequest(String query, List<Candidate> candidates, String request_id) {}
    public record Score(String product_id, double score) {}
    public record RerankResponse(List<String> ranked_product_ids, List<Score> scores, String explanation, String provider, long latency_ms) {}
    public record StrategySuggestRequest(List<Map<String, Object>> query_metrics, Map<String, Object> current_strategy, List<Object> evidence, String request_id) {}
    public record ProposedChange(String operation, String path, Object value, String reason) {}
    public record StrategySuggestResponse(List<ProposedChange> proposed_changes, String expected_impact, List<String> evidence_refs, double confidence, String risk_level, boolean requires_approval, String provider, long latency_ms) {}

    public static final class AiAdapterException extends RuntimeException {
        public final int status;
        public AiAdapterException(int status, String message) { super(message); this.status = status; }
        public AiAdapterException(int status, String message, Throwable cause) { super(message, cause); this.status = status; }
    }
}

