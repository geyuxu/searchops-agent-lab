package lab.searchops.service;

import java.net.SocketTimeoutException;
import java.net.http.HttpTimeoutException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeoutException;
import lab.searchops.config.RerankProperties;
import lab.searchops.config.SearchProperties;
import lab.searchops.domain.AiRerankStatus;
import lab.searchops.domain.AiRewriteStatus;
import lab.searchops.domain.ApiModels.Product;
import lab.searchops.domain.ApiModels.SearchOptions;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.MediaType;
import org.springframework.http.converter.HttpMessageConversionException;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import tools.jackson.core.JacksonException;
import tools.jackson.databind.JsonNode;

@Component
public class AiAdapterClient {
    private static final Logger log = LoggerFactory.getLogger(AiAdapterClient.class);

    /** 适配器没有回报 provider 名时的占位值，仅用于观测，不参与任何判断。 */
    static final String UNKNOWN_PROVIDER = "unknown";

    // 下面这组上限来自适配器的 pydantic 契约（services/ai-adapter/app/models.py，
    // StrictModel 是 extra="forbid" 的严格模型）。Java 侧必须自己裁剪：任何一条超限
    // 都会让整次调用被 422 拒掉，于是"某个商品描述特别长"这种数据问题会伪装成 AI 故障。
    private static final int MAX_QUERY_CHARS = 500;
    private static final int MAX_PRODUCT_ID_CHARS = 128;
    private static final int MAX_TITLE_CHARS = 2000;
    private static final int MAX_BRAND_CHARS = 500;
    private static final int MAX_DESCRIPTION_CHARS = 5000;
    private static final int MAX_REQUEST_ID_CHARS = 128;

    private final RestClient client;
    private final RestClient rerankClient;
    private final SearchProperties properties;
    private final RerankProperties rerankProperties;

    /** 重排的并发预算，见 RerankProperties.maxConcurrency 与 AiRerankStatus.OVERLOADED。 */
    private final Semaphore rerankBudget;

    @Autowired
    public AiAdapterClient(@Qualifier("aiRestClient") RestClient client,
            @Qualifier("aiRerankRestClient") RestClient rerankClient,
            SearchProperties properties, RerankProperties rerankProperties) {
        this.client = client;
        this.rerankClient = rerankClient;
        this.properties = properties;
        this.rerankProperties = rerankProperties;
        this.rerankBudget = new Semaphore(rerankProperties.maxConcurrency());
    }

    /**
     * 兼容构造器：改写与重排共用同一个 RestClient，重排配置取默认值。
     *
     * <p>保留它是为了让所有既有构造点（尤其是只关心改写降级的单元测试）源码不变。
     * 生产装配走上面带 {@code @Autowired} 的四参构造器，重排因此拥有独立的超时预算。
     */
    public AiAdapterClient(RestClient client, SearchProperties properties) {
        this(client, client, properties, RerankProperties.defaults());
    }

    public RewriteResult rewrite(SearchOptions options) {
        // 为什么拆成两个 if：原实现是 `!aiEnabled() || !useAi()` 的短路或，两种"没调用"
        // 在响应里完全无法区分，运维分不清是 kill switch 关着还是调用方没请求 AI。
        if (!properties.aiEnabled()) {
            return notCalled(options, AiRewriteStatus.DISABLED);
        }
        if (!options.useAi()) {
            return notCalled(options, AiRewriteStatus.NOT_REQUESTED);
        }

        var filters = new LinkedHashMap<String, Object>();
        if (options.brand() != null) filters.put("brand", options.brand());
        if (options.category() != null) filters.put("category", options.category());
        if (options.priceMin() != null) filters.put("price_min", options.priceMin());
        if (options.priceMax() != null) filters.put("price_max", options.priceMax());

        try {
            var response = client.post().uri("/ai/query-rewrite")
                    .contentType(MediaType.APPLICATION_JSON)
                    .header("X-Request-ID", options.requestId())
                    .body(Map.of(
                            "query", options.query(),
                            "locale", options.locale(),
                            "filters", filters,
                            "request_id", options.requestId()))
                    .retrieve().body(JsonNode.class);

            // 为什么只校验 rewritten_query：原实现要求 provider 必须等于 "mock"，
            // 任何真实 provider（langchain、openai…）都会在这里抛异常并被下面的 catch
            // 静默降级成 BM25——docs/ai-handoff.md 承诺的"换真实 provider 无需改 Java"因此不成立。
            // provider 名现在只做记录、向上传递，不再作为准入条件。
            var rewritten = response == null ? "" : response.path("rewritten_query").asText("");
            if (rewritten.isBlank()) {
                throw new InvalidAiResponseException(
                        "AI adapter response is missing a usable rewritten_query");
            }
            var provider = response.path("provider").asText("");
            if (provider.isBlank()) {
                // 契约里 provider 是必填项，但它不影响改写结果本身，因此只告警不降级。
                log.warn("AI adapter omitted provider name (request_id={})", options.requestId());
                provider = UNKNOWN_PROVIDER;
            }

            // 为什么比较归一化后的文本：SearchQueryCompiler 会把查询 trim + 折叠空白 + 转小写，
            // 只有大小写/空白差异的"改写"在编译后完全等价，对外声称改写过属于噪音。
            var changed = !normalize(rewritten).equals(normalize(options.query()));
            var status = changed ? AiRewriteStatus.APPLIED : AiRewriteStatus.NO_CHANGE;
            log.debug("AI rewrite {} by provider {} (request_id={})", status, provider,
                    options.requestId());
            return new RewriteResult(rewritten, changed, provider, status);
        } catch (RuntimeException exception) {
            // 原实现把所有 RuntimeException 折叠成一个 "fallback" 字符串，超时和协议错误
            // 无法分辨；真实 LLM 最常见的失败恰恰是超时，必须能单独统计。
            var status = classify(exception);
            log.warn("AI rewrite fell back to BM25 (status={}, request_id={}): {}",
                    status, options.requestId(), exception.toString());
            return new RewriteResult(options.query(), false, null, status);
        }
    }

    /**
     * 决定本次请求要不要走重排、以及要取多深的候选。
     *
     * <p>为什么把判定放在这里而不是 ProductSearchService：候选深度会改变发给 Elasticsearch
     * 的 size，而"是否调用"和"取多深"必须由同一套条件决定。分散到两处早晚会出现
     * "多取了 50 条却没调用重排"（白白放大 ES 负载）或"调用了重排却只有 10 条候选"
     * （重排无从发挥）这类不一致。
     */
    public RerankPlan rerankPlan(SearchOptions options) {
        var window = Math.max(1, options.size());
        // 顺序与 rewrite() 一致：先看 kill switch，再看调用方是否要求。
        // 这样"开关关着"永远压过"没请求"，运维一眼能看出是被关掉的。
        if (!properties.aiEnabled()) {
            return RerankPlan.skip(AiRerankStatus.AI_DISABLED, window);
        }
        if (!rerankProperties.enabled()) {
            return RerankPlan.skip(AiRerankStatus.DISABLED, window);
        }
        if (!options.useRerank()) {
            return RerankPlan.skip(AiRerankStatus.NOT_REQUESTED, window);
        }
        // page>0 不重排：重排改变的是 top-N 内部的顺序，而分页是按 BM25 顺序切片的。
        // 若第 2 页也重排，同一个候选会因为两次调用（LLM 不保证确定性）而重复出现或消失，
        // 分页语义会当场崩坏。只重排第一页 ⇒ 对外的分页契约完全不变，第 2 页起逐字节等同
        // 于改动前。代价见 ProductSearchService.search 的注释：第 1 页与第 2 页之间可能重复
        // 或遗漏若干条。这是本次刻意接受的取舍——衡量重排价值的窗口就是 top-10。
        if (options.page() > 0) {
            return RerankPlan.skip(AiRerankStatus.NOT_APPLICABLE, window);
        }
        // 非 relevance 排序时用相关性重排等于推翻用户显式选择的排序方式。
        if (!SearchQueryCompiler.isRelevanceSort(options.sort())) {
            return RerankPlan.skip(AiRerankStatus.NOT_APPLICABLE, window);
        }
        return new RerankPlan(true, rerankProperties.effectiveDepth(options.rerankDepth(), window),
                window, null);
    }

    /**
     * 对 BM25 候选集做一次重排。
     *
     * <p><b>永不改变文档集合</b>：无论适配器返回什么（少返、多返、集外 id、超时、乱码），
     * 返回的 products 都是入参 candidates 的一个排列。这条不变量由
     * {@link RerankOrdering#reorder} 构造并断言，理由见那里的类注释。
     *
     * @param plan       {@link #rerankPlan(SearchOptions)} 的结果
     * @param query      用来判定相关性的查询文本（调用方传实际检索用的 effective query）
     * @param candidates BM25 顺序的候选集
     */
    public RerankResult rerank(RerankPlan plan, String query, List<Product> candidates,
            String requestId) {
        var safeCandidates = candidates == null ? List.<Product>of() : candidates;
        if (plan == null || !plan.active()) {
            return unchanged(safeCandidates,
                    plan == null ? AiRerankStatus.NOT_REQUESTED : plan.skipStatus());
        }
        // 适配器契约要求 query 非空、candidates 至少 1 条；不满足时调用必然 422，
        // 与其换一个"传输失败"的假故障，不如如实记成"不适用"。
        var text = truncate(normalizeQuery(query), MAX_QUERY_CHARS);
        if (text.isBlank() || safeCandidates.isEmpty()) {
            return unchanged(safeCandidates, AiRerankStatus.NOT_APPLICABLE);
        }
        var payloadCandidates = candidatePayload(safeCandidates);
        if (payloadCandidates.isEmpty()) {
            return unchanged(safeCandidates, AiRerankStatus.NOT_APPLICABLE);
        }
        if (!rerankBudget.tryAcquire()) {
            log.warn("Rerank skipped: concurrency budget of {} exhausted (request_id={})",
                    rerankProperties.maxConcurrency(), requestId);
            return unchanged(safeCandidates, AiRerankStatus.OVERLOADED);
        }

        try {
            var response = rerankClient.post().uri("/ai/rerank")
                    .contentType(MediaType.APPLICATION_JSON)
                    .header("X-Request-ID", requestId)
                    .body(Map.of(
                            "query", text,
                            "candidates", payloadCandidates,
                            "request_id", truncate(blankToUnknown(requestId), MAX_REQUEST_ID_CHARS)))
                    .retrieve().body(JsonNode.class);

            var rankedIds = rankedIds(response);
            if (rankedIds.isEmpty()) {
                // 空排名不是"没变化"，而是响应不可用：合法的重排至少要点名一个候选。
                throw new InvalidAiResponseException(
                        "AI adapter response is missing a usable ranked_product_ids");
            }
            var provider = response.path("provider").asText("");
            if (provider.isBlank()) {
                log.warn("AI adapter omitted provider name on rerank (request_id={})", requestId);
                provider = UNKNOWN_PROVIDER;
            }

            var reordered = RerankOrdering.reorder(safeCandidates, rankedIds, Product::productId);
            // 判定口径是"实际返回的那一页"，不是整个候选集：见 AiRerankStatus.NO_CHANGE 的注释。
            var changed = !window(reordered, plan.window()).equals(window(safeCandidates, plan.window()));
            var status = changed ? AiRerankStatus.APPLIED : AiRerankStatus.NO_CHANGE;
            log.debug("Rerank {} by provider {} over {} candidates (request_id={})",
                    status, provider, payloadCandidates.size(), requestId);
            return new RerankResult(reordered, status, provider, payloadCandidates.size());
        } catch (IllegalStateException exception) {
            // 排列不变量被破坏 —— 这是本服务自己的缺陷，不是模型的锅，因此不能记成
            // INVALID_RESPONSE（会把排查引向适配器）。结果强制退回 BM25 顺序：
            // 用户宁可拿到没优化过的排序，也不能拿到一个丢了文档的排序。
            log.error("Rerank ordering invariant violated, falling back to BM25 (request_id={})",
                    requestId, exception);
            return unchanged(safeCandidates, AiRerankStatus.INTERNAL_ERROR);
        } catch (RuntimeException exception) {
            var status = switch (failureKind(exception)) {
                case TIMEOUT -> AiRerankStatus.TIMEOUT;
                case INVALID -> AiRerankStatus.INVALID_RESPONSE;
                case TRANSPORT -> AiRerankStatus.TRANSPORT_ERROR;
            };
            log.warn("Rerank fell back to BM25 order (status={}, candidates={}, request_id={}): {}",
                    status, payloadCandidates.size(), requestId, exception.toString());
            return unchanged(safeCandidates, status);
        } finally {
            rerankBudget.release();
        }
    }

    /** 候选集原样返回（顺序即 BM25 顺序），只带上本次的状态。 */
    private RerankResult unchanged(List<Product> candidates, AiRerankStatus status) {
        return new RerankResult(candidates, status, null, null);
    }

    private List<Map<String, Object>> candidatePayload(List<Product> candidates) {
        var payload = new ArrayList<Map<String, Object>>(candidates.size());
        for (var product : candidates) {
            var id = product.productId();
            // 契约要求 product_id 长度 1..128。截断 id 会破坏身份（模型返回的 id 将对不上号），
            // 所以这里只能整条跳过；被跳过的候选不会消失，它会由 RerankOrdering 按原序补回末尾。
            if (id == null || id.isBlank() || id.length() > MAX_PRODUCT_ID_CHARS) continue;
            var candidate = new LinkedHashMap<String, Object>();
            candidate.put("product_id", id);
            candidate.put("title", truncate(nullToEmpty(product.title()), MAX_TITLE_CHARS));
            candidate.put("brand", truncate(nullToEmpty(product.brand()), MAX_BRAND_CHARS));
            candidate.put("description",
                    truncate(nullToEmpty(product.description()), MAX_DESCRIPTION_CHARS));
            // score 在非 relevance 排序下可能为 null；契约要求是数字。
            candidate.put("bm25_score", product.score() == null ? 0.0 : product.score());
            payload.add(candidate);
            if (payload.size() >= RerankProperties.MAX_DEPTH) break;
        }
        return payload;
    }

    private List<String> rankedIds(JsonNode response) {
        var ids = new ArrayList<String>();
        if (response == null) return ids;
        var node = response.path("ranked_product_ids");
        if (!node.isArray()) return ids;
        node.forEach(entry -> {
            if (entry.isTextual()) ids.add(entry.asText());
        });
        return ids;
    }

    /** 实际返回给调用方的那一页 id 列表，用来判断重排是否真的改变了用户可见的顺序。 */
    private List<String> window(List<Product> products, int size) {
        return products.stream().limit(Math.max(0, size)).map(Product::productId).toList();
    }

    private RewriteResult notCalled(SearchOptions options, AiRewriteStatus status) {
        return new RewriteResult(options.query(), false, null, status);
    }

    private AiRewriteStatus classify(RuntimeException exception) {
        return switch (failureKind(exception)) {
            case TIMEOUT -> AiRewriteStatus.TIMEOUT;
            case INVALID -> AiRewriteStatus.INVALID_RESPONSE;
            case TRANSPORT -> AiRewriteStatus.TRANSPORT_ERROR;
        };
    }

    /**
     * 降级原因的分类，改写与重排共用。
     *
     * <p>抽出来是因为两条链路必须对"什么算超时"给出完全一致的答案；各写一份迟早会分叉，
     * 于是同一次网络故障在两个字段里显示成不同原因。
     */
    private FailureKind failureKind(RuntimeException exception) {
        if (exception instanceof InvalidAiResponseException) {
            return FailureKind.INVALID;
        }
        if (hasCause(exception, JacksonException.class)
                || hasCause(exception, HttpMessageConversionException.class)) {
            return FailureKind.INVALID;
        }
        if (hasCause(exception, HttpTimeoutException.class)
                || hasCause(exception, SocketTimeoutException.class)
                || hasCause(exception, TimeoutException.class)) {
            // JdkClientHttpRequestFactory 的读超时会被 RestClient 包成
            // ResourceAccessException，真正的超时类型藏在 cause 链里，必须逐层查看。
            return FailureKind.TIMEOUT;
        }
        return FailureKind.TRANSPORT;
    }

    private enum FailureKind { TIMEOUT, TRANSPORT, INVALID }

    private static String nullToEmpty(String value) {
        return value == null ? "" : value;
    }

    private static String blankToUnknown(String value) {
        return value == null || value.isBlank() ? "unknown" : value;
    }

    private static String truncate(String value, int max) {
        return value.length() <= max ? value : value.substring(0, max);
    }

    private String normalizeQuery(String value) {
        return value == null ? "" : String.join(" ", value.trim().split("\\s+"));
    }

    private boolean hasCause(Throwable error, Class<? extends Throwable> type) {
        for (var cause = error; cause != null; cause = cause.getCause()) {
            if (type.isInstance(cause)) return true;
            if (cause.getCause() == cause) break; // 防御自引用 cause 造成的死循环
        }
        return false;
    }

    private String normalize(String value) {
        return value == null ? "" : String.join(" ", value.trim().split("\\s+")).toLowerCase();
    }

    /**
     * @param query    实际交给查询编译器的文本；降级时等于原始查询
     * @param applied  查询是否真的被 AI 改写（归一化后不等于原始查询）
     * @param provider 适配器回报的 provider 名；未真正调用或调用失败时为 null
     * @param status   本次调用的确定性结果，降级时即降级原因
     */
    public record RewriteResult(String query, boolean applied, String provider,
            AiRewriteStatus status) {}

    /**
     * 本次请求的重排计划：要不要调用、取多深的候选、这一页多大。
     *
     * @param active     是否真的要调用重排。false 时检索深度必须保持原样（等于 size），
     *                   否则会白白放大 Elasticsearch 的 size 却没人使用多出来的候选。
     * @param depth      BM25 候选深度 N，仅在 active 时有意义
     * @param window     实际返回给调用方的条数，用来判定 APPLIED / NO_CHANGE
     * @param skipStatus 不调用时的原因；active 为 true 时是 null
     */
    public record RerankPlan(boolean active, int depth, int window, AiRerankStatus skipStatus) {
        static RerankPlan skip(AiRerankStatus status, int window) {
            return new RerankPlan(false, window, window, status);
        }
    }

    /**
     * @param products       重排后的文档，**保证是入参候选集的一个排列**；降级时即原 BM25 顺序
     * @param status         本次重排的确定性结果，降级时即降级原因，永不为 null
     * @param provider       适配器回报的 provider 名；未调用或调用失败时为 null
     * @param candidateCount 实际发给适配器的候选条数；未发起调用时为 null
     */
    public record RerankResult(List<Product> products, AiRerankStatus status, String provider,
            Integer candidateCount) {

        /** 顺序是否真的变了（等价于 status == APPLIED），给响应里的 rerank_applied 用。 */
        public boolean applied() {
            return status == AiRerankStatus.APPLIED;
        }
    }

    /** 适配器可达但响应不符合契约（响应体为空、缺少 rewritten_query）。 */
    static class InvalidAiResponseException extends RuntimeException {
        InvalidAiResponseException(String message) {
            super(message);
        }
    }
}
