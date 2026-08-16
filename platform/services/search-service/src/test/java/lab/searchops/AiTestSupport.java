package lab.searchops;

import java.util.Arrays;
import java.util.UUID;
import java.util.stream.Collectors;
import lab.searchops.config.SearchProperties;
import lab.searchops.domain.ApiModels.SearchOptions;
import lab.searchops.domain.ApiModels.Strategy;
import lab.searchops.domain.StrategyConfig;
import lab.searchops.service.AiAdapterClient;
import org.springframework.test.web.client.MockRestServiceServer;
import org.springframework.web.client.RestClient;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * AI 相关单元测试的公共装配。
 *
 * <p>刻意不启动 Spring 上下文、不启动 Testcontainers：这一批测试保护的是 AI 降级路径的
 * 分支判断，全部可以在纯 JVM 里跑完，不该拖慢 {@code mvn test}（现有的
 * StrategyWorkflowIntegrationTest 已经承担了一次 Postgres 容器启动的代价）。
 */
final class AiTestSupport {
    /** AI 适配器的假地址；MockRestServiceServer 按完整 URI 匹配，所以两边必须一致。 */
    static final String AI_BASE_URL = "http://ai-adapter.test";

    /** 与 AiAdapterClient 硬编码的路径一致；路径写错会让"没调用 AI"伪装成通过。 */
    static final String REWRITE_URL = AI_BASE_URL + "/ai/query-rewrite";

    static final int BASELINE_STRATEGY_VERSION = 7;

    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    private AiTestSupport() {}

    /**
     * 把 RestClient 接到 MockRestServiceServer 上，返回一个真实的 AiAdapterClient。
     *
     * <p>为什么不 mock AiAdapterClient：本次修复的全部逻辑（provider 不再硬校验、
     * 降级原因分类、改写是否真的发生）都在 AiAdapterClient 内部，mock 掉它等于什么都没测。
     * 打桩只打到 HTTP 这一层。
     */
    static Wiring aiClient(boolean aiEnabled) {
        var builder = RestClient.builder().baseUrl(AI_BASE_URL);
        var server = MockRestServiceServer.bindTo(builder).build();
        return new Wiring(new AiAdapterClient(builder.build(), properties(aiEnabled)), server);
    }

    static SearchProperties properties(boolean aiEnabled) {
        return new SearchProperties("http://elasticsearch.test", "products-read", AI_BASE_URL,
                aiEnabled, 5000, 1000, "test-secret", "/tmp/processed");
    }

    static SearchOptions options(String query, boolean useAi) {
        return new SearchOptions(query, "en-US", 0, 10, null, null, null, null, null,
                "relevance", useAi, "request-under-test", false);
    }

    static Strategy baselineStrategy() {
        return new Strategy(UUID.randomUUID(), BASELINE_STRATEGY_VERSION, "baseline", "PUBLISHED",
                StrategyConfig.baseline(), "test", null, null, null, null, null);
    }

    /** 适配器契约里的成功响应；provider 为 null 时整个字段缺席，用来测"provider 缺失"。 */
    static String rewriteResponse(String rewrittenQuery, String provider) {
        return rewriteResponse(rewrittenQuery, provider, null);
    }

    /**
     * 带模型标识的成功响应。
     *
     * <p>model 为 null 时整个 model 键缺席——这正是适配器侧的真实形状（/ai/* 路由以
     * {@code response_model_exclude_none=True} 序列化，见 services/ai-adapter/app/models.py
     * 里 ModelIdentity 的注释）。空串则会真的以 {@code "model":""} 出现在响应里：
     * "这个 provider 没声明模型"与"声明了一个空值"必须能分开测，否则
     * {@link lab.searchops.ModelIdentityTest} 里那条"空白等同缺失"就无从构造。
     */
    static String rewriteResponse(String rewrittenQuery, String provider, String model) {
        return rewriteResponseWithRawModel(rewrittenQuery, provider,
                model == null ? null : "\"%s\"".formatted(model));
    }

    /**
     * model 字段直接写入原始 JSON 片段（含引号或字面量），用来构造 {@code "model":null}、
     * {@code "model":{...}} 这类不守契约、但第三方适配器完全可能发出来的响应。
     */
    static String rewriteResponseWithRawModel(String rewrittenQuery, String provider, String rawModel) {
        var providerField = provider == null ? "" : ",\"provider\":\"%s\"".formatted(provider);
        var modelField = rawModel == null ? "" : ",\"model\":%s".formatted(rawModel);
        return "{\"rewritten_query\":\"%s\"%s%s,\"extracted_filters\":{},\"latency_ms\":12}"
                .formatted(rewrittenQuery, providerField, modelField);
    }

    static JsonNode elasticsearchHits(String... productIds) {
        var hits = Arrays.stream(productIds)
                .map(id -> ("{\"_score\":1.5,\"_source\":{\"product_id\":\"%s\",\"title\":\"%s title\","
                        + "\"brand\":\"Acme\",\"price_cents\":1999,\"inventory\":3}}").formatted(id, id))
                .collect(Collectors.joining(","));
        return MAPPER.readTree(("{\"hits\":{\"total\":{\"value\":%d},\"hits\":[%s]},"
                + "\"aggregations\":{\"brands\":{\"buckets\":[]},\"categories\":{\"buckets\":[]}}}")
                .formatted(productIds.length, hits));
    }

    record Wiring(AiAdapterClient client, MockRestServiceServer server) {}
}
