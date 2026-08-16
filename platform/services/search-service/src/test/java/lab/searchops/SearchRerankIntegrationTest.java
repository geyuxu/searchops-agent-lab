package lab.searchops;

import static lab.searchops.AiTestSupport.baselineStrategy;
import static lab.searchops.AiTestSupport.elasticsearchHits;
import static lab.searchops.RerankTestSupport.RERANK_URL;
import static lab.searchops.RerankTestSupport.ids;
import static lab.searchops.RerankTestSupport.legacyOptions;
import static lab.searchops.RerankTestSupport.productIds;
import static lab.searchops.RerankTestSupport.rerankClient;
import static lab.searchops.RerankTestSupport.rerankOptions;
import static lab.searchops.RerankTestSupport.rerankResponse;
import static lab.searchops.RerankTestSupport.reversed;
import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.atLeastOnce;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.requestTo;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withServerError;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withSuccess;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import lab.searchops.config.RerankProperties;
import lab.searchops.domain.AiRerankStatus;
import lab.searchops.domain.ApiModels.SearchResponse;
import lab.searchops.service.AiAdapterClient;
import lab.searchops.service.ElasticsearchGateway;
import lab.searchops.service.ProductSearchService;
import lab.searchops.service.SearchAnalyticsRepository;
import lab.searchops.service.SearchQueryCompiler;
import lab.searchops.service.StrategyService;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.http.MediaType;
import tools.jackson.databind.json.JsonMapper;

/**
 * ProductSearchService 层面的重排接入测试：真实的 AiAdapterClient + 真实的
 * SearchQueryCompiler + 真实的 RerankOrdering，只把 Elasticsearch 和 AI 适配器的 HTTP 打桩。
 *
 * <p>比 {@link RerankFallbackTest} 高一层，回答两个只有在这一层才能回答的问题：
 * <ul>
 *   <li>不走重排时，发给 Elasticsearch 的查询体是否与改动前逐字节相同（分页契约没被顺手改掉）；</li>
 *   <li>走重排时，"取 N 条 → 重排 → 截回 size 条"这条链路是否仍然一条文档都不丢。</li>
 * </ul>
 */
class SearchRerankIntegrationTest {
    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    private final ElasticsearchGateway elasticsearch = mock(ElasticsearchGateway.class);
    private final StrategyService strategies = mock(StrategyService.class);
    private final SearchAnalyticsRepository analytics = mock(SearchAnalyticsRepository.class);

    /**
     * 重排关闭时，编译出的 ES 查询体与改动前逐字节相同。
     *
     * <p>参照物是 13 参兼容构造器——它就是新增 use_rerank / rerank_depth 之前所有调用点的
     * 原样写法。两者编译出的查询体必须一个字节都不差：留出集基线 v7（NDCG@10 0.4720）
     * 与 baselines/ 下的归档都是在这条链路上产生的，它一旦漂移，"重排的净收益"就失去了
     * 可减的被减数。
     *
     * <p>同时确认 size 没有被候选深度顺手放大——不重排却多取 50 条，只会白白放大 ES 负载。
     */
    @Test
    void rerankOffCompilesByteIdenticalElasticsearchQuery() {
        var wiring = rerankClient();
        var service = searchService(wiring.client(), 20);

        var legacy = service.search(legacyOptions("smart tv", 0, 10));
        var flagOff = service.search(rerankOptions("smart tv", 0, 10, "relevance", false, null));

        var bodies = elasticsearchBodies();
        assertThat(bodies).hasSize(2);
        assertThat(json(bodies.get(1))).isEqualTo(json(bodies.get(0)));
        assertThat(bodies.get(0)).containsEntry("from", 0).containsEntry("size", 10);
        assertThat(flagOff.effectiveQuery()).isEqualTo(legacy.effectiveQuery());
        assertThat(ids(flagOff.products())).isEqualTo(ids(legacy.products()));
        assertNoRerankReported(flagOff, AiRerankStatus.NOT_REQUESTED);
        assertNoRerankReported(legacy, AiRerankStatus.NOT_REQUESTED);
        // 两台 mock server 都没登记期望：发出任何 AI 请求都会抛 AssertionError。
        wiring.rerankServer().verify();
        wiring.rewriteServer().verify();
    }

    /**
     * 重排关闭时发出的查询体，逐个键钉死。
     *
     * <p>上一条比的是"两条构造路径彼此一致"，只能证明自洽；这一条把实际发出的查询钉在
     * 一个明确的形状上，任何让不重排请求的查询体发生变化的改动都会在这里当场变红——
     * 这才是"与改动前一致"能被机器验证的那一半。
     *
     * <p>field_weights 用无序断言：StrategyConfig 的紧凑构造器做了 {@code Map.copyOf}，
     * 不可变 Map 的迭代顺序按 JVM 每次启动的盐值变化，钉死顺序会造成随机失败。
     */
    @Test
    @SuppressWarnings("unchecked")
    void rerankOffEmitsExactlyTheBaselineQueryShape() {
        var wiring = rerankClient();

        searchService(wiring.client(), 20)
                .search(rerankOptions("smart tv", 0, 10, "relevance", false, null));

        var body = elasticsearchBodies().getFirst();
        assertThat(body).containsOnlyKeys("from", "size", "track_total_hits", "query", "sort",
                "aggs");   // baseline 的 minimum_score 是 0，min_score 键不该出现
        assertThat(body).containsEntry("from", 0).containsEntry("size", 10)
                .containsEntry("track_total_hits", true);
        assertThat(body.get("sort"))
                .isEqualTo(List.of(Map.of("_score", "desc"), Map.of("product_id", "asc")));
        assertThat(body.get("aggs")).isEqualTo(Map.of(
                "brands", Map.of("terms", Map.of("field", "brand.keyword", "size", 30)),
                "categories", Map.of("terms", Map.of("field", "category.keyword", "size", 30))));

        var bool = (Map<String, Object>) ((Map<String, Object>) body.get("query")).get("bool");
        assertThat(bool).containsOnlyKeys("must");   // 无 filter、无 must_not
        var must = (List<Map<String, Object>>) bool.get("must");
        assertThat(must).hasSize(1);
        var multiMatch = (Map<String, Object>) must.getFirst().get("multi_match");
        assertThat(multiMatch).containsEntry("query", "smart tv")
                .containsEntry("type", "best_fields")
                .containsEntry("operator", "or")
                .containsEntry("fuzziness", "AUTO");
        assertThat((List<String>) multiMatch.get("fields")).containsExactlyInAnyOrder(
                "title^4.0", "brand^2.5", "bullet_point^1.5", "description^1.0", "category^1.2");
    }

    /**
     * page&gt;0 不重排：第 2 页起与重排关闭时逐字节一致。
     *
     * <p>重排只动第一页是刻意的取舍（见 ProductSearchService 的注释）：分页是按 BM25 顺序
     * 切片的，若第 2 页也重排，同一个候选会因为两次不确定的模型调用而重复出现或消失，
     * 分页语义当场崩坏。这条测试锁死"对外分页契约完全不变"这一半承诺。
     */
    @Test
    void secondPageIsByteIdenticalToRerankBeingOff() {
        var wiring = rerankClient();
        var service = searchService(wiring.client(), 20);

        var off = service.search(rerankOptions("smart tv", 1, 10, "relevance", false, null));
        var on = service.search(rerankOptions("smart tv", 1, 10, "relevance", true, 50));

        var bodies = elasticsearchBodies();
        assertThat(bodies).hasSize(2);
        assertThat(json(bodies.get(1))).isEqualTo(json(bodies.get(0)));
        assertThat(bodies.get(1)).containsEntry("from", 10).containsEntry("size", 10);
        assertThat(ids(on.products())).isEqualTo(ids(off.products()));
        // 请求要了重排，但这条链路对第 2 页不适用——与"没请求"必须可区分。
        assertNoRerankReported(on, AiRerankStatus.NOT_APPLICABLE);
        assertNoRerankReported(off, AiRerankStatus.NOT_REQUESTED);
        assertThat(on.rerankStatus()).isNotEqualTo(off.rerankStatus());
        wiring.rerankServer().verify();
    }

    /** 第一页开重排：检索深度放大到 N，重排之后再截回 size 条。 */
    @Test
    void firstPageWithRerankRetrievesTheCandidateDepthAndTruncatesBack() {
        var wiring = rerankClient();
        var service = searchService(wiring.client(), 20);
        expectRerank(wiring, rerankResponse("mock", reversed(productIds(20))));

        var response = service.search(rerankOptions("smart tv", 0, 10, "relevance", true, 20));

        assertThat(elasticsearchBodies().getLast())
                .containsEntry("from", 0).containsEntry("size", 20);
        assertThat(ids(response.products()))
                .containsExactly("P20", "P19", "P18", "P17", "P16", "P15", "P14", "P13", "P12",
                        "P11");
        assertThat(response.rerankApplied()).isTrue();
        assertThat(response.rerankStatus()).isEqualTo(AiRerankStatus.APPLIED);
        assertThat(response.rerankProvider()).isEqualTo("mock");
        assertThat(response.rerankCandidates()).isEqualTo(20);
        // 分页字段与总数不受重排影响。
        assertThat(response.page()).isZero();
        assertThat(response.size()).isEqualTo(10);
        assertThat(response.total()).isEqualTo(20);
        wiring.rerankServer().verify();
    }

    /**
     * 模型幻觉不会让本页少一条商品。
     *
     * <p>这是整条链路上"绝不丢文档"最终要兑现的地方：适配器返回的 id 一半是编的、
     * 还夹着重复，本页仍然是满员的 10 条互不相同的商品，且全部来自 BM25 候选集。
     * 一旦这里能少一条，故障形状就与"BM25 没召回"完全一致，归因会落到索引和查询编译上。
     */
    @Test
    void hallucinatedIdsNeverShrinkThePage() {
        var wiring = rerankClient();
        var service = searchService(wiring.client(), 20);
        var ranked = new ArrayList<String>(List.of("ghost-1", "P07", "P07", "ghost-2", "P03",
                "", "P07", "ghost-3"));
        expectRerank(wiring, rerankResponse("mock", List.copyOf(ranked)));

        var response = service.search(rerankOptions("smart tv", 0, 10, "relevance", true, 20));

        assertThat(response.products()).hasSize(10);
        assertThat(ids(response.products())).doesNotHaveDuplicates()
                .isSubsetOf(productIds(20))
                // 被点名且真实存在的两条排在最前，其余按原 BM25 序补齐。
                .startsWith("P07", "P03", "P01", "P02", "P04");
        assertThat(response.rerankStatus()).isEqualTo(AiRerankStatus.APPLIED);
    }

    /**
     * 适配器故障时请求照常成功，本页退回纯 BM25 顺序，原因写在 rerank_status 里。
     *
     * <p>"返回 200 + BM25 顺序"既是失败的表现也是"重排没收益"的表现，两者只能靠
     * rerank_status 区分——所以这条同时断言"结果与不重排一致"和"状态说得出原因"。
     */
    @Test
    void adapterFailureStillReturnsTheBm25Page() {
        var wiring = rerankClient();
        var service = searchService(wiring.client(), 20);
        wiring.rerankServer().expect(requestTo(RERANK_URL)).andRespond(withServerError());

        var degraded = service.search(rerankOptions("smart tv", 0, 10, "relevance", true, 20));
        var rerankFree = service.search(rerankOptions("smart tv", 0, 10, "relevance", false, null));

        assertThat(ids(degraded.products())).isEqualTo(ids(rerankFree.products()))
                .isEqualTo(productIds(10));
        assertThat(degraded.rerankStatus()).isEqualTo(AiRerankStatus.TRANSPORT_ERROR);
        assertThat(degraded.rerankApplied()).isFalse();
        assertThat(degraded.rerankProvider()).isNull();
        assertThat(degraded.rerankCandidates()).isNull();
        assertThat(degraded.rerankStatus().failure()).isTrue();
        // 降级不影响检索深度：候选照样取了 N 条，只是没人给它们重新排序。
        assertThat(elasticsearchBodies().getFirst()).containsEntry("size", 20);
    }

    /** 重排成功但没改变本页顺序时，rerank_applied 必须是 false（口径见 AiRerankStatus）。 */
    @Test
    void rerankThatDoesNotChangeThePageIsNotReportedAsApplied() {
        var wiring = rerankClient();
        var service = searchService(wiring.client(), 20);
        var ranked = new ArrayList<>(productIds(20));
        java.util.Collections.swap(ranked, 14, 19);   // 第 15 位与第 20 位对调，本页看不见
        expectRerank(wiring, rerankResponse("mock", List.copyOf(ranked)));

        var response = service.search(rerankOptions("smart tv", 0, 10, "relevance", true, 20));

        assertThat(response.rerankStatus()).isEqualTo(AiRerankStatus.NO_CHANGE);
        assertThat(response.rerankApplied()).isFalse();
        assertThat(response.rerankStatus().fallback()).isFalse();   // 调用是成功的，不是降级
        assertThat(ids(response.products())).isEqualTo(productIds(10));
        assertThat(response.rerankProvider()).isEqualTo("mock");
    }

    /** 全局 AI kill switch 关着时，重排一次 HTTP 都不发，且状态与"重排开关关着"可区分。 */
    @Test
    void globalKillSwitchSurfacesAsAiDisabledInTheResponse() {
        var wiring = rerankClient(false, RerankProperties.defaults());
        var service = searchService(wiring.client(), 20);

        var response = service.search(rerankOptions("smart tv", 0, 10, "relevance", true, 20));

        assertNoRerankReported(response, AiRerankStatus.AI_DISABLED);
        assertThat(response.rerankStatus()).isNotEqualTo(AiRerankStatus.DISABLED);
        assertThat(elasticsearchBodies().getLast()).containsEntry("size", 10);
        wiring.rerankServer().verify();
    }

    // ---------------------------------------------------------------------
    // helpers
    // ---------------------------------------------------------------------

    private ProductSearchService searchService(AiAdapterClient ai, int hitCount) {
        when(strategies.current()).thenReturn(baselineStrategy());
        when(elasticsearch.search(any()))
                .thenReturn(elasticsearchHits(productIds(hitCount).toArray(String[]::new)));
        return new ProductSearchService(elasticsearch, new SearchQueryCompiler(), strategies,
                analytics, ai);
    }

    private void expectRerank(RerankTestSupport.Wiring wiring, String body) {
        wiring.rerankServer().expect(requestTo(RERANK_URL))
                .andRespond(withSuccess(body, MediaType.APPLICATION_JSON));
    }

    /** 没有走重排时，四个重排字段必须是中性值，只有状态说得出是哪一种"没走"。 */
    private void assertNoRerankReported(SearchResponse response, AiRerankStatus expected) {
        assertThat(response.rerankStatus()).isEqualTo(expected);
        assertThat(response.rerankApplied()).isFalse();
        assertThat(response.rerankProvider()).isNull();
        assertThat(response.rerankCandidates()).isNull();
        assertThat(expected.failure()).isFalse();
    }

    /** 发给 Elasticsearch 的所有查询体，按调用顺序。 */
    private List<Map<String, Object>> elasticsearchBodies() {
        ArgumentCaptor<Map<String, Object>> captor = ArgumentCaptor.captor();
        verify(elasticsearch, atLeastOnce()).search(captor.capture());
        return captor.getAllValues();
    }

    private static String json(Map<String, Object> body) {
        return MAPPER.writeValueAsString(body);
    }
}
