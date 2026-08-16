package lab.searchops;

import static lab.searchops.AiTestSupport.REWRITE_URL;
import static lab.searchops.AiTestSupport.aiClient;
import static lab.searchops.AiTestSupport.baselineStrategy;
import static lab.searchops.AiTestSupport.elasticsearchHits;
import static lab.searchops.AiTestSupport.options;
import static lab.searchops.AiTestSupport.rewriteResponse;
import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.atLeastOnce;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.requestTo;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withServerError;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withSuccess;

import com.fasterxml.jackson.annotation.JsonInclude;
import java.util.List;
import java.util.Map;
import lab.searchops.domain.AiRewriteStatus;
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
 * ProductSearchService 层面的 AI 可观测性测试：真实的 AiAdapterClient + 真实的
 * SearchQueryCompiler，只把 Elasticsearch 和 AI 适配器的 HTTP 打桩。
 *
 * <p>比 AiAdapterClientTest 高一层，验证这些状态确实一路传到了 API 响应里，
 * 而不是在 service 层被吃掉。
 */
class SearchAiStatusTest {
    private final ElasticsearchGateway elasticsearch = mock(ElasticsearchGateway.class);
    private final StrategyService strategies = mock(StrategyService.class);
    private final SearchAnalyticsRepository analytics = mock(SearchAnalyticsRepository.class);

    /**
     * 真实 provider 的改写必须真的打进 Elasticsearch 查询体。
     *
     * <p>只断言 ai_applied=true 不够：修复前的静默降级同样返回 200，
     * 唯一能证明"AI 生效了"的证据是发给 ES 的那段文本变了。
     */
    @Test
    void realProviderRewriteReachesElasticsearch() {
        var wiring = aiClient(true);
        wiring.server().expect(requestTo(REWRITE_URL)).andRespond(
                withSuccess(rewriteResponse("4k television", "langchain"), MediaType.APPLICATION_JSON));

        var response = searchService(wiring.client()).search(options("smart tv", true));

        assertThat(response.aiStatus()).isEqualTo(AiRewriteStatus.APPLIED);
        assertThat(response.aiApplied()).isTrue();
        assertThat(response.aiProvider()).isEqualTo("langchain");
        assertThat(response.effectiveQuery()).isEqualTo("4k television");
        assertThat(elasticsearchBodies().getLast().toString()).contains("4k television");
        assertThat(response.strategyVersion()).isEqualTo(AiTestSupport.BASELINE_STRATEGY_VERSION);
        wiring.server().verify();
    }

    /**
     * AI 调用成功但没改查询时，ai_applied 必须是 false。
     *
     * <p>旧语义下 mock provider 未命中规则也报 true，于是"AI 覆盖率"这类指标全是虚高的。
     * 同时确认 provider 仍被回报——这次调用是成功的，不是降级。
     */
    @Test
    void successfulButUnchangedRewriteIsNotReportedAsApplied() {
        var wiring = aiClient(true);
        wiring.server().expect(requestTo(REWRITE_URL)).andRespond(
                withSuccess(rewriteResponse("wireless mouse", "mock"), MediaType.APPLICATION_JSON));

        var response = searchService(wiring.client()).search(options("wireless mouse", true));

        assertThat(response.aiApplied()).isFalse();
        assertThat(response.aiStatus()).isEqualTo(AiRewriteStatus.NO_CHANGE);
        assertThat(response.aiProvider()).isEqualTo("mock");
    }

    /**
     * 降级必须"结果无声、原因可见"：发给 ES 的查询体与 use_ai=false 完全一致（排序不受影响），
     * 但 ai_status 说得出是哪一类故障。
     *
     * <p>这条同时锁住两件事——降级不改变检索结果，以及降级原因不再被折叠成同一个 false。
     */
    @Test
    void transportFailureFallsBackToExactlyTheAiFreeQuery() {
        var aiFreeWiring = aiClient(true);
        var aiFree = searchService(aiFreeWiring.client()).search(options("smart tv", false));

        var brokenWiring = aiClient(true);
        brokenWiring.server().expect(requestTo(REWRITE_URL)).andRespond(withServerError());
        var degraded = searchService(brokenWiring.client()).search(options("smart tv", true));

        var bodies = elasticsearchBodies();
        assertThat(bodies).hasSize(2);
        assertThat(bodies.get(1)).isEqualTo(bodies.get(0));
        assertThat(degraded.effectiveQuery()).isEqualTo(aiFree.effectiveQuery());
        assertThat(degraded.aiApplied()).isFalse();
        assertThat(degraded.aiStatus()).isEqualTo(AiRewriteStatus.TRANSPORT_ERROR);
        assertThat(aiFree.aiStatus()).isEqualTo(AiRewriteStatus.NOT_REQUESTED);
        assertThat(degraded.aiStatus()).isNotEqualTo(aiFree.aiStatus());
        assertThat(degraded.aiProvider()).isNull();
    }

    /** kill switch 关着时，响应里也要看得出是"被关掉了"，而不是"AI 没效果"。 */
    @Test
    void killSwitchSurfacesAsDisabledInTheResponse() {
        var wiring = aiClient(false);

        var response = searchService(wiring.client()).search(options("smart tv", true));

        assertThat(response.aiStatus()).isEqualTo(AiRewriteStatus.DISABLED);
        assertThat(response.aiApplied()).isFalse();
        assertThat(response.effectiveQuery()).isEqualTo("smart tv");
        // 没登记任何 HTTP 期望：真的发出请求就会抛 AssertionError，测试直接红。
        wiring.server().verify();
    }

    /**
     * 线格式回归。
     *
     * <p>application.yml 里 spring.jackson.default-property-inclusion=non_null，所以
     * ai_provider 为 null 时整个键会消失（不是 null 值）——前端和运维面板必须按"键可能不存在"
     * 来读。ai_status 则永远在。这里用同样的 inclusion 配置复现真实序列化行为。
     */
    @Test
    void aiStatusIsAlwaysSerialisedAndNullProviderIsOmitted() {
        var mapper = JsonMapper.builder()
                .changeDefaultPropertyInclusion(value ->
                        value.withValueInclusion(JsonInclude.Include.NON_NULL))
                .build();

        var degraded = mapper.writeValueAsString(new SearchResponse("r1", "smart tv", "smart tv",
                0, 0, 10, 3, 7, List.of(), Map.of(), false,
                AiRewriteStatus.TIMEOUT, null, "notice"));
        var applied = mapper.writeValueAsString(new SearchResponse("r2", "smart tv", "4k television",
                1, 0, 10, 3, 7, List.of(), Map.of(), true,
                AiRewriteStatus.APPLIED, "langchain", "notice"));

        assertThat(degraded).contains("\"ai_status\":\"TIMEOUT\"").doesNotContain("ai_provider");
        assertThat(applied).contains("\"ai_status\":\"APPLIED\"", "\"ai_provider\":\"langchain\"",
                "\"ai_applied\":true");
    }

    private ProductSearchService searchService(AiAdapterClient ai) {
        when(strategies.current()).thenReturn(baselineStrategy());
        when(elasticsearch.search(any())).thenReturn(elasticsearchHits("P1", "P2"));
        return new ProductSearchService(elasticsearch, new SearchQueryCompiler(), strategies,
                analytics, ai);
    }

    /** 发给 Elasticsearch 的所有查询体，按调用顺序。 */
    private List<Map<String, Object>> elasticsearchBodies() {
        ArgumentCaptor<Map<String, Object>> captor = ArgumentCaptor.captor();
        verify(elasticsearch, atLeastOnce()).search(captor.capture());
        return captor.getAllValues();
    }
}
