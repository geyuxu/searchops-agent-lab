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
            // 候选集重排是否真的改变了本页顺序。与 ai_applied 一样，只有"确实产生了差异"
            // 才是 true；调用成功但顺序没变是 false + rerank_status=NO_CHANGE。
            @JsonProperty("rerank_applied") boolean rerankApplied,
            // 本次重排的确定性结果；降级时即降级原因，永不为 null。与 ai_status 分开两个字段，
            // 理由见 AiRerankStatus 的类注释——同一次请求可以是"改写成功 + 重排超时"。
            @JsonProperty("rerank_status") AiRerankStatus rerankStatus,
            // 重排实际生效的 provider 名；未调用或调用失败时为 null（该键被整体省略）。
            @JsonProperty("rerank_provider") String rerankProvider,
            // 实际送去重排的候选条数（BM25 取回的 N，可能少于配置的深度）。未调用时省略。
            // 没有它就无法从响应侧确认 rerank_depth 到底生效了没有。
            @JsonProperty("rerank_candidates") Integer rerankCandidates,
            @JsonProperty("data_notice") String dataNotice) {

        /**
         * 兼容构造器：不带任何重排信息 == 本次请求没有走重排链路。
         *
         * <p>保留它是为了让既有构造点源码不变；重排字段填成"未请求"的中性值。
         */
        public SearchResponse(String requestId, String originalQuery, String effectiveQuery,
                long total, int page, int size, long latencyMs, int strategyVersion,
                List<Product> products, Map<String, List<FacetBucket>> facets, boolean aiApplied,
                AiRewriteStatus aiStatus, String aiProvider, String dataNotice) {
            this(requestId, originalQuery, effectiveQuery, total, page, size, latencyMs,
                    strategyVersion, products, facets, aiApplied, aiStatus, aiProvider,
                    false, AiRerankStatus.NOT_REQUESTED, null, null, dataNotice);
        }
    }

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
            boolean logRequest,
            // 是否请求候选集重排。与 useAi 完全独立：可以只开改写、只开重排、或两者都开。
            boolean useRerank,
            // 候选深度 N 的请求级覆盖；null 表示用服务端配置的默认值。
            Integer rerankDepth) {

        /** 兼容构造器：不带重排参数 == 不重排。既有构造点（含 explain / preview）行为不变。 */
        public SearchOptions(String query, String locale, int page, int size, String brand,
                String category, Integer priceMin, Integer priceMax, Boolean inStock, String sort,
                boolean useAi, String requestId, boolean logRequest) {
            this(query, locale, page, size, brand, category, priceMin, priceMax, inStock, sort,
                    useAi, requestId, logRequest, false, null);
        }

        /**
         * 换一个检索深度的副本，其余分量原样。
         *
         * <p>重排需要先取 N 条再截断到 size，而 SearchQueryCompiler 只认 options.size()。
         * 与其给编译器加一个"候选深度"参数（那会让所有不重排的调用都要传一个多余的值，
         * 且很容易出现深度与是否重排不一致），不如在这里造一个只改 size 的副本：
         * 不重排时压根不会调用它，编译出的查询体因此与改动前逐字节相同。
         */
        public SearchOptions withSize(int newSize) {
            return new SearchOptions(query, locale, page, newSize, brand, category, priceMin,
                    priceMax, inStock, sort, useAi, requestId, logRequest, useRerank, rerankDepth);
        }
    }

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
            @JsonProperty("use_ai") Boolean useAi,
            // 候选策略配置（可选）。传了就用它编译检索查询，评测一个**尚未发布**的配置；
            // 缺省（null）时行为与新增此字段之前逐字节一致——评测当前已发布策略。
            //
            // 为什么需要它：以前想扫描 field_weights 之类的参数，只能真的发布/回滚几十次，
            // 把 strategy 版本历史和审计流全污染；Agent 也无法对自己的提案跑整轮离线评测。
            //
            // 类型上是对象（而非 primitive），Jackson 3 的 FAIL_ON_NULL_FOR_PRIMITIVES
            // 陷阱（见上面 use_ai 的注释）不适用；缺省即 null，null 就是"用已发布策略"。
            // 注意：候选配置评测**强制不落库**，理由见 EvaluationService.run 的注释。
            @JsonProperty("strategy_config") @Valid StrategyConfig strategyConfig,
            // 评测是否走候选集重排路径。
            //
            // 为什么是独立字段而不是复用 use_ai：
            // 1) 改写与重排是两条可以独立开关的链路，复用一个开关就没法只测其中一条；
            // 2) 更要命的是归因——若 use_ai 同时打开改写和重排，测出来的 ΔNDCG@10 是两者
            //    的合力，无法回答"重排本身值不值得"这个唯一重要的问题。留出集基线
            //    （NDCG@10 0.4720）是无 AI 条件下产生的，只有 use_rerank 单独可控，
            //    "只开重排"这一组才能与它直接相减。
            //
            // 与 use_ai 同样用包装类型 Boolean：Jackson 3 的 FAIL_ON_NULL_FOR_PRIMITIVES
            // 默认为 true，primitive 字段缺省会 400，而既有评测脚本的 payload 里没有这个键。
            // 默认 false，因此所有既有基线复现链路一个字节都不受影响。
            @JsonProperty("use_rerank") Boolean useRerank,
            // 候选深度 N 的覆盖；null 表示用服务端配置的默认值（lab.search.rerank.depth）。
            @JsonProperty("rerank_depth") @Min(1) @Max(200) Integer rerankDepth) {
        public EvaluationRequest {
            useAi = Boolean.TRUE.equals(useAi);
            useRerank = Boolean.TRUE.equals(useRerank);
            // rerankDepth 保持 null 语义：null == 用服务端默认深度。
            // strategyConfig 保持 null 语义：null == 评测当前已发布策略。
            // 这里刻意不填默认值，"没传候选配置"与"传了一个空候选配置"必须是两件事
            // ——后者会被 StrategyConfig 的紧凑构造器补成 baseline 权重，是一次真实的候选评测。
        }

        /**
         * 四参构造器：不带候选配置 == 评测当前已发布策略。
         *
         * <p>保留它是为了让所有既有调用点（EvaluationService.runOne、既有测试）源码不变，
         * 新增的第 5 个分量对它们完全不可见。
         */
        public EvaluationRequest(List<EvaluationQuery> queries, int k, boolean persist, Boolean useAi) {
            this(queries, k, persist, useAi, null, false, null);
        }

        /** 五参构造器：带候选配置但不重排。同样只为让既有调用点源码不变。 */
        public EvaluationRequest(List<EvaluationQuery> queries, int k, boolean persist,
                Boolean useAi, StrategyConfig strategyConfig) {
            this(queries, k, persist, useAi, strategyConfig, false, null);
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
            // 已发布策略评测时是真实版本号；候选配置评测时是哨兵
            // {@link lab.searchops.service.EvaluationService#CANDIDATE_STRATEGY_VERSION}(-1)，
            // 与 SearchResponse 对候选配置检索用的版本号是同一个哨兵。
            @JsonProperty("strategy_version") int strategyVersion,
            // "candidate" 表示这一轮评测跑的是请求里带来的候选配置，不是任何已发布版本。
            // 已发布策略评测时为 null——受 spring.jackson.default-property-inclusion=non_null
            // 影响该键会被整体省略，因此既有响应（以及 data/processed/evaluation-latest.json
            // 与 baselines/ 下的归档）的形状一个字节都没变。
            @JsonProperty("strategy_source") String strategySource,
            // 仅在"请求要求落库、但因为是候选配置评测而被强制忽略"时为 true，其余情况为 null（省略）。
            // 让调用方能看出自己的 persist=true 没有生效，而不是以为数据已经写进去了。
            @JsonProperty("persist_skipped") Boolean persistSkipped,
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
            // 回显本次评测是否开启了重排，以及实际用的候选深度。同理：没有这两个标记，
            // 一份 experiments/*.json 就无法自证它是哪一组条件跑出来的。
            //
            // 为什么是包装类型：重排关闭时填 null，受 non_null inclusion 影响这两个键会
            // 整体消失，于是既有产物（baselines/、experiments/ 下的归档）的 JSON 形状
            // 与新增这两个字段之前逐字节一致，新旧结果仍可直接 diff。
            @JsonProperty("use_rerank") Boolean useRerank,
            @JsonProperty("rerank_depth") Integer rerankDepth,
            @JsonProperty("generated_at") OffsetDateTime generatedAt) {}

    public record ErrorResponse(
            String error,
            String message,
            @JsonProperty("request_id") String requestId,
            OffsetDateTime timestamp) {}
}

