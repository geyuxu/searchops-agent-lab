package lab.searchops;

import static lab.searchops.AiTestSupport.REWRITE_URL;
import static lab.searchops.AiTestSupport.aiClient;
import static lab.searchops.AiTestSupport.baselineStrategy;
import static lab.searchops.AiTestSupport.elasticsearchHits;
import static lab.searchops.AiTestSupport.rewriteResponse;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.catchThrowable;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.atLeastOnce;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.requestTo;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withSuccess;

import java.util.List;
import java.util.Map;
import lab.searchops.domain.AiRewriteStatus;
import lab.searchops.domain.ApiModels.EvaluationQuery;
import lab.searchops.domain.ApiModels.EvaluationRequest;
import lab.searchops.domain.ApiModels.Product;
import lab.searchops.domain.ApiModels.SearchOptions;
import lab.searchops.domain.ApiModels.SearchResponse;
import lab.searchops.service.AiAdapterClient;
import lab.searchops.service.ElasticsearchGateway;
import lab.searchops.service.EvaluationService;
import lab.searchops.service.ProductSearchService;
import lab.searchops.service.SearchAnalyticsRepository;
import lab.searchops.service.SearchQueryCompiler;
import lab.searchops.service.StrategyService;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;

/**
 * 评测入口的 AI 开关测试。
 *
 * <p>两条互相冲突的要求必须同时成立：
 * <ul>
 *   <li>不传 use_ai 时行为与修复前逐字节一致——现有 200 条查询的基线
 *       （strategy v7, NDCG@10 0.4326）是在无 AI 条件下产生的，必须仍能复现；</li>
 *   <li>传 use_ai=true 时确实会调用 AI 适配器——修复前 EvaluationService 把 useAi
 *       写死成 false，AI_ENABLED=true 也量不到任何 Recall@10/NDCG@10 变化。</li>
 * </ul>
 *
 * <p>请求体那一层的默认值由 {@link EvaluationRequestWireContractTest} 单独把守。
 */
class EvaluationAiToggleTest {
    private final ProductSearchService search = mock(ProductSearchService.class);
    private final StrategyService strategies = mock(StrategyService.class);
    private final JdbcTemplate jdbc = mock(JdbcTemplate.class);
    private final ElasticsearchGateway elasticsearch = mock(ElasticsearchGateway.class);
    private final SearchAnalyticsRepository analytics = mock(SearchAnalyticsRepository.class);

    /** 默认（无 AI）的评测请求必须把 SearchOptions.useAi 传成 false。 */
    @Test
    void defaultRunKeepsSearchOptionsAiFree() {
        var result = evaluationService().run(request(false));

        assertThat(capturedSearchOptions()).hasSize(2).allMatch(options -> !options.useAi());
        assertThat(result.useAi()).isFalse();
    }

    /**
     * 修复的正面证据：use_ai=true 时每一条查询的 SearchOptions.useAi 都必须是 true。
     *
     * <p>修复前这里是写死的 false（EvaluationService 构造 SearchOptions 的第 11 个实参），
     * 配合 AiAdapterClient 的 {@code !aiEnabled() || !useAi()} 短路，
     * 导致 make evaluate 永远测的是纯 BM25。
     */
    @Test
    void runWithUseAiPropagatesTheFlagToEveryQuery() {
        var result = evaluationService().run(request(true));

        assertThat(capturedSearchOptions()).hasSize(2).allMatch(SearchOptions::useAi);
        assertThat(result.useAi()).isTrue();
    }

    /**
     * EvaluationResult.use_ai 会被落到 data/processed/evaluation-latest.json。
     *
     * <p>evaluation_runs 表没有这一列（DB 变更延后），所以这个回显是区分
     * "基线跑"和"AI 候选跑"的唯一标记，丢了就没法做前后对比。
     */
    @Test
    void resultEchoesTheAiFlagForOfflineComparison() {
        assertThat(evaluationService().run(request(false)).useAi()).isFalse();
        assertThat(evaluationService().run(request(true)).useAi()).isTrue();
    }

    /** 既有调用方用的单参 runOne 必须保持无 AI，否则工具网关的既有契约行为会悄悄改变。 */
    @Test
    void legacyRunOneOverloadStaysAiFree() {
        evaluationService().runOne(query(1, "smart tv"));

        assertThat(capturedSearchOptions()).hasSize(1).allMatch(options -> !options.useAi());
    }

    /** 新的双参 runOne 支撑 /evaluations/query?use_ai=true 与工具网关的同名参数。 */
    @Test
    void runOneOverloadForwardsTheAiFlag() {
        evaluationService().runOne(query(1, "smart tv"), true);

        assertThat(capturedSearchOptions()).hasSize(1).allMatch(SearchOptions::useAi);
    }

    /**
     * HTTP 层证据：use_ai=true 的评测真的向 AI 适配器发了请求，且改写结果进了检索。
     *
     * <p>前面几条只证明布尔值传下去了；这一条用真实的 ProductSearchService +
     * AiAdapterClient 串起来，确认这条链路上没有第二个地方再把它按回 false。
     */
    @Test
    void evaluationWithAiActuallyCallsTheAdapter() {
        var wiring = aiClient(true);
        wiring.server().expect(requestTo(REWRITE_URL)).andRespond(
                withSuccess(rewriteResponse("4k television", "langchain"), MediaType.APPLICATION_JSON));

        var result = fullyWiredService(wiring.client())
                .run(new EvaluationRequest(List.of(query(1, "smart tv")), 10, false, true));

        wiring.server().verify();
        assertThat(result.useAi()).isTrue();
        assertThat(elasticsearchBodies().getLast().toString()).contains("4k television");
    }

    /**
     * HTTP 层的反面证据：默认评测一次 AI 请求都不发，即使 AI_ENABLED=true。
     *
     * <p>MockRestServiceServer 没登记任何期望，一旦发出请求就会抛 AssertionError
     * （Error 不会被 AiAdapterClient 的 catch RuntimeException 吞掉）。
     * 这正是基线可复现的物理保证。
     */
    @Test
    void defaultEvaluationNeverTouchesTheAdapterEvenWhenAiIsEnabled() {
        var wiring = aiClient(true);

        var result = fullyWiredService(wiring.client())
                .run(new EvaluationRequest(List.of(query(1, "smart tv")), 10, false, false));

        wiring.server().verify();
        assertThat(result.useAi()).isFalse();
        assertThat(elasticsearchBodies().getLast().toString()).contains("smart tv");
    }

    /** k 必须仍然只接受 10——这是基线口径的一部分，不能被 AI 改动顺带放宽。 */
    @Test
    void baselineEndpointStillRefusesAnyKOtherThanTen() {
        var service = evaluationService();
        var request = new EvaluationRequest(List.of(query(1, "smart tv")), 20, false, false);

        assertThat(catchThrowable(() -> service.run(request)))
                .isInstanceOf(IllegalArgumentException.class);
    }

    /** 只打桩 ProductSearchService，用来捕获传下去的 SearchOptions。 */
    private EvaluationService evaluationService() {
        when(strategies.current()).thenReturn(baselineStrategy());
        when(search.search(any())).thenReturn(searchResponse());
        return new EvaluationService(search, strategies, jdbc);
    }

    /** 真实的 ProductSearchService + AiAdapterClient，只打桩 Elasticsearch 和 AI 的 HTTP。 */
    private EvaluationService fullyWiredService(AiAdapterClient ai) {
        when(strategies.current()).thenReturn(baselineStrategy());
        when(elasticsearch.search(any())).thenReturn(elasticsearchHits("P1", "P2"));
        var productSearch = new ProductSearchService(elasticsearch, new SearchQueryCompiler(),
                strategies, analytics, ai);
        return new EvaluationService(productSearch, strategies, jdbc);
    }

    private List<SearchOptions> capturedSearchOptions() {
        ArgumentCaptor<SearchOptions> captor = ArgumentCaptor.captor();
        verify(search, atLeastOnce()).search(captor.capture());
        return captor.getAllValues();
    }

    private List<Map<String, Object>> elasticsearchBodies() {
        ArgumentCaptor<Map<String, Object>> captor = ArgumentCaptor.captor();
        verify(elasticsearch, atLeastOnce()).search(captor.capture());
        return captor.getAllValues();
    }

    private static EvaluationRequest request(boolean useAi) {
        return new EvaluationRequest(List.of(query(1, "smart tv"), query(2, "wireless mouse")),
                10, false, useAi);
    }

    private static EvaluationQuery query(long id, String text) {
        return new EvaluationQuery(id, text, Map.of("P1", "E", "P2", "S"));
    }

    private static SearchResponse searchResponse() {
        return new SearchResponse("request-under-test", "smart tv", "smart tv", 2, 0, 10, 5,
                AiTestSupport.BASELINE_STRATEGY_VERSION, List.of(product("P1"), product("P2")),
                Map.of(), false, AiRewriteStatus.NOT_REQUESTED, null, "notice");
    }

    private static Product product(String id) {
        return new Product(id, id + " title", "Acme", "desc", "bullet", "black", "us",
                "Electronics", 1999, "USD", 3, 120, "test", 1.5);
    }
}
