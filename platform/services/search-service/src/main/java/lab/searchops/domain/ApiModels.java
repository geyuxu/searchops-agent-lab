package lab.searchops.domain;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.UUID;

public final class ApiModels {
    private ApiModels() {}

    public record Product(
            @JsonProperty("product_id") String productId,
            String title,
            String brand,
            String description,
            @JsonProperty("bullet_point") String bulletPoint,
            String color,
            String locale,
            String category,
            @JsonProperty("price_cents") int priceCents,
            String currency,
            int inventory,
            @JsonProperty("placeholder_hue") int placeholderHue,
            String provenance,
            Double score) {}

    public record FacetBucket(String key, long count) {}

    public record SearchResponse(
            @JsonProperty("request_id") String requestId,
            @JsonProperty("original_query") String originalQuery,
            @JsonProperty("effective_query") String effectiveQuery,
            long total,
            int page,
            int size,
            @JsonProperty("latency_ms") long latencyMs,
            @JsonProperty("strategy_version") int strategyVersion,
            List<Product> products,
            Map<String, List<FacetBucket>> facets,
            @JsonProperty("ai_applied") boolean aiApplied,
            @JsonProperty("data_notice") String dataNotice) {}

    public record SearchOptions(
            String query,
            String locale,
            int page,
            int size,
            String brand,
            String category,
            Integer priceMin,
            Integer priceMax,
            Boolean inStock,
            String sort,
            boolean useAi,
            String requestId,
            boolean logRequest) {}

    public record Strategy(
            UUID id,
            int version,
            String name,
            String status,
            StrategyConfig config,
            @JsonProperty("created_by") String createdBy,
            @JsonProperty("created_at") OffsetDateTime createdAt,
            @JsonProperty("approved_by") String approvedBy,
            @JsonProperty("approved_at") OffsetDateTime approvedAt,
            @JsonProperty("published_at") OffsetDateTime publishedAt,
            @JsonProperty("supersedes_version") Integer supersedesVersion) {}

    public record CreateStrategyRequest(
            @NotBlank @Size(max = 200) String name,
            @NotBlank @Size(max = 200) String actor,
            @JsonProperty("request_id") @NotBlank @Size(max = 128) String requestId,
            @NotNull @Valid StrategyConfig config) {}

    public record TransitionRequest(
            @NotBlank @Size(max = 200) String actor,
            @JsonProperty("request_id") @NotBlank @Size(max = 128) String requestId) {}

    public record ApprovalResponse(Strategy strategy, @JsonProperty("approval_token") String approvalToken) {}

    public record PublishRequest(
            @NotBlank @Size(max = 200) String actor,
            @JsonProperty("request_id") @NotBlank @Size(max = 128) String requestId,
            @JsonProperty("approval_token") @NotBlank String approvalToken) {}

    public record RollbackRequest(
            @JsonProperty("target_version") @Min(1) int targetVersion,
            @NotBlank @Size(max = 200) String actor,
            @JsonProperty("request_id") @NotBlank @Size(max = 128) String requestId,
            @JsonProperty("approval_token") @NotBlank String approvalToken) {}

    public record PreviewRequest(
            @NotBlank @Size(max = 500) String query,
            String locale,
            @Min(1) @Max(50) int size,
            @NotNull @Valid StrategyConfig config) {}

    public record PreviewResponse(
            SearchResponse current,
            SearchResponse proposed,
            @JsonProperty("changed_positions") List<Map<String, Object>> changedPositions,
            @JsonProperty("dry_run") boolean dryRun) {}

    public record EvaluationQuery(
            @JsonProperty("query_id") long queryId,
            @NotBlank String query,
            @NotNull Map<String, String> judgments) {}

    public record EvaluationRequest(
            @Size(min = 1, max = 10000) List<@Valid EvaluationQuery> queries,
            @Min(1) @Max(100) int k,
            boolean persist) {}

    public record QueryMetric(
            @JsonProperty("query_id") long queryId,
            String query,
            @JsonProperty("precision10") double precision10,
            @JsonProperty("recall10") double recall10,
            @JsonProperty("mrr10") double mrr10,
            @JsonProperty("ndcg10") double ndcg10,
            @JsonProperty("zero_result") boolean zeroResult) {}

    public record EvaluationResult(
            @JsonProperty("run_id") UUID runId,
            @JsonProperty("strategy_version") int strategyVersion,
            @JsonProperty("query_count") int queryCount,
            @JsonProperty("precision10") double precision10,
            @JsonProperty("recall10") double recall10,
            @JsonProperty("mrr10") double mrr10,
            @JsonProperty("ndcg10") double ndcg10,
            @JsonProperty("zero_result_rate") double zeroResultRate,
            List<QueryMetric> queries,
            @JsonProperty("generated_at") OffsetDateTime generatedAt) {}

    public record ErrorResponse(
            String error,
            String message,
            @JsonProperty("request_id") String requestId,
            OffsetDateTime timestamp) {}
}

