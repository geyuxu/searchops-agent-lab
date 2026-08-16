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
            // ai_applied 现在表示"查询确实被 AI 改写了"（改写结果 != 原始查询）。
            // 旧语义只表示"AI 调用没抛异常"，mock provider 未命中规则时也是 true，
            // 于是这个字段无法用来判断 AI 到底有没有影响检索。丢掉的"调用是否成功"
            // 信息没有消失，被 ai_status 完整接管（APPLIED / NO_CHANGE / 各类降级原因）。
            @JsonProperty("ai_applied") boolean aiApplied,
            // 本次 AI 改写的确定性结果；降级时即降级原因，永不为 null。
            @JsonProperty("ai_status") AiRewriteStatus aiStatus,
            // 实际生效的 provider 名（mock / langchain / …）。未调用或调用失败时为 null，
            // 受 spring.jackson.default-property-inclusion=non_null 影响该字段会被整体省略。
            @JsonProperty("ai_provider") String aiProvider,
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
            boolean persist,
            // 评测是否走 AI 改写路径。以前 EvaluationService 把 useAi 写死成 false，
            // 所以 make evaluate 永远量不到 AI 的 Recall@10/NDCG@10 变化。
            //
            // 必须保持默认 false：现有 200 条查询的基线（strategy v7, NDCG@10 0.4326）
            // 是在无 AI 条件下产生的，只有默认不变，新旧结果才可比。
            //
            // 为什么是包装类型 Boolean 而不是 primitive boolean：
            // Jackson 3 把 FAIL_ON_NULL_FOR_PRIMITIVES 的默认值从 false 翻转成了 true
            // （Spring Boot 4.1 的 use-jackson2-defaults 默认为 false，不恢复旧语义），
            // 因此 primitive 字段一旦缺省就不再静默填 false，而是抛 MismatchedInputException
            // 变成 400。而 data/scripts/evaluate.py 发的 payload 只有 queries/k/persist，
            // 用 primitive 会让整条基线复现链路当场断掉。
            // 用 Boolean + 紧凑构造器归一化，可使该字段真正可选，且不依赖全局 Jackson 配置。
            @JsonProperty("use_ai") Boolean useAi) {
        public EvaluationRequest {
            useAi = Boolean.TRUE.equals(useAi);
        }
    }

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
            // 回显本次评测是否开启了 AI。评测结果会被落到 data/processed/evaluation-latest.json，
            // 没有这个标记就无法区分基线跑和 AI 候选跑，对比毫无意义。
            @JsonProperty("use_ai") boolean useAi,
            @JsonProperty("generated_at") OffsetDateTime generatedAt) {}

    public record ErrorResponse(
            String error,
            String message,
            @JsonProperty("request_id") String requestId,
            OffsetDateTime timestamp) {}
}

