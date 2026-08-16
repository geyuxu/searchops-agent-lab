package lab.searchops;

import static lab.searchops.AiTestSupport.aiClient;
import static lab.searchops.AiTestSupport.baselineStrategy;
import static lab.searchops.AiTestSupport.elasticsearchHits;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.catchThrowable;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.atLeastOnce;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.mockingDetails;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import java.util.List;
import java.util.Map;
import lab.searchops.domain.AiRewriteStatus;
import lab.searchops.domain.ApiModels.EvaluationQuery;
import lab.searchops.domain.ApiModels.EvaluationRequest;
import lab.searchops.domain.ApiModels.Product;
import lab.searchops.domain.ApiModels.SearchOptions;
import lab.searchops.domain.ApiModels.SearchResponse;
import lab.searchops.domain.StrategyConfig;
import lab.searchops.api.StrategyController;
import lab.searchops.api.ToolGatewayController;
import lab.searchops.service.AiAdapterClient;
import lab.searchops.service.ElasticsearchGateway;
import lab.searchops.service.EvaluationService;
import lab.searchops.service.ProductSearchService;
import lab.searchops.service.SearchAnalyticsRepository;
import lab.searchops.service.SearchQueryCompiler;
import lab.searchops.service.StrategyService;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.mockito.invocation.Invocation;
import org.springframework.jdbc.core.JdbcTemplate;

/**
 * 候选策略配置评测（EvaluationRequest.strategy_config）的行为契约。
 *
 * <p>这条路径同时要满足两组互相拉扯的要求，任何一组破了都是重大回归：
 * <ul>
 *   <li><b>不传 strategy_config 时必须逐字节维持旧行为</b>——仍然评测当前已发布策略、
 *       persist 语义原样保留。仓库根 baselines/evaluation-v7-bm25-baseline-20260802.json
 *       （strategy v7，NDCG@10 0.4326）的可比性完全押在这一条上，它是本类里最重要的测试；</li>
 *   <li><b>传了 strategy_config 时必须用它编译查询，并且什么状态都不写</b>——不读写 strategy、
 *       不写 quality_metrics / evaluation_runs。否则一次参数扫描就能用没有版本号的候选数字
 *       覆盖掉真实基线（quality_metrics 唯一键是 (query_id, strategy_version)，
 *       ON CONFLICT DO UPDATE 覆盖不可逆）。</li>
 * </ul>
 *
 * <p>请求体那一层（缺省字段不会变成 400、响应键集合不变）由
 * {@link EvaluationCandidateWireContractTest} 单独把守。
 *
 * <p>全部纯 JVM，不启动 Spring 上下文、不启动 Testcontainers——沿用
 * {@link EvaluationAiToggleTest} 的取舍，不给 {@code mvn test} 增加容器启动代价。
 */
class EvaluationCandidateConfigTest {
    private final ProductSearchService search = mock(ProductSearchService.class);
    private final StrategyService strategies = mock(StrategyService.class);
    private final JdbcTemplate jdbc = mock(JdbcTemplate.class);
    private final ElasticsearchGateway elasticsearch = mock(ElasticsearchGateway.class);
    private final SearchAnalyticsRepository analytics = mock(SearchAnalyticsRepository.class);

    /** quality_metrics 的 INSERT 里 strategy_version 是第 3 个绑定参数（args[0] 是 SQL）。 */
    private static final int QUALITY_METRICS_VERSION_ARG = 3;

    /** evaluation_runs 的 INSERT 里 strategy_version 是第 2 个绑定参数。 */
    private static final int EVALUATION_RUNS_VERSION_ARG = 2;

    // ---------------------------------------------------------------------
    // 1. 不传 strategy_config：行为必须与新增该字段之前完全一致
    // ---------------------------------------------------------------------

    /**
     * 基线红线：缺省 strategy_config 时仍然评测**当前已发布策略**。
     *
     * <p>三个证据一起看才完整：走的是单参 search 重载（既有调用形状）、真的问过
     * StrategyService 当前版本、响应里回填的是真实版本号 7 而不是候选哨兵。
     * 少任何一条，baselines/ 里那份 v7 基线就不再是"同一件事"跑出来的。
     */
    @Test
    void withoutStrategyConfigTheRunStillEvaluatesThePublishedStrategy() {
        var result = publishedService().run(publishedRequest(false));

        assertThat(capturedPublishedOptions()).hasSize(2);
        verify(search, never()).search(any(), any());
        verify(strategies, atLeastOnce()).current();
        assertThat(result.strategyVersion()).isEqualTo(AiTestSupport.BASELINE_STRATEGY_VERSION);
        assertThat(result.strategySource()).isNull();
        assertThat(result.persistSkipped()).isNull();
        assertThat(result.queryCount()).isEqualTo(2);
    }

    /**
     * 缺省 strategy_config 且 persist=true：两张表照写，写的还是真实版本号。
     *
     * <p>make evaluate / data/scripts/evaluate.py 发的载荷正是 persist=true。
     * 如果新增的"候选评测强制不落库"闸门误伤了这条路径，质量基线表会静默停止更新，
     * 而调用方拿到的仍是 200 —— 属于最难发现的那类回归。
     */
    @Test
    void withoutStrategyConfigPersistTrueStillWritesBothTables() {
        publishedService().run(publishedRequest(true));

        var updates = jdbcUpdates();
        assertThat(updates).hasSize(3);

        var qualityMetrics = updatesTouching(updates, "quality_metrics");
        assertThat(qualityMetrics).hasSize(2).allSatisfy(update ->
                assertThat(update.getArguments()[QUALITY_METRICS_VERSION_ARG])
                        .isEqualTo(AiTestSupport.BASELINE_STRATEGY_VERSION));

        var runs = updatesTouching(updates, "evaluation_runs");
        assertThat(runs).hasSize(1);
        assertThat(runs.getFirst().getArguments()[EVALUATION_RUNS_VERSION_ARG])
                .isEqualTo(AiTestSupport.BASELINE_STRATEGY_VERSION);
    }

    /** 缺省 strategy_config 且 persist=false：一行都不写，这也是改动前的行为。 */
    @Test
    void withoutStrategyConfigPersistFalseWritesNothing() {
        var result = publishedService().run(publishedRequest(false));

        verifyNoInteractions(jdbc);
        // persist_skipped 只描述"被强制降级"，用户自己传 false 不算降级，必须保持 null（响应里省略）。
        assertThat(result.persistSkipped()).isNull();
    }

    // ---------------------------------------------------------------------
    // 2. 传了 strategy_config：用它编译查询，且不碰任何 strategy
    // ---------------------------------------------------------------------

    /**
     * 候选配置必须真的进到编译器里，而不是只影响响应上的标记。
     *
     * <p>用真实的 ProductSearchService + SearchQueryCompiler，只打桩 Elasticsearch：
     * 断言发给 ES 的查询体带着候选的 field_weights / rewrite_rules / blocked ids，
     * 且不含 baseline 的 title^4.0。这正是"评测一个尚未发布的配置"的全部意义。
     */
    @Test
    void candidateConfigIsTheOneCompiledIntoTheElasticsearchQuery() {
        wiredService().run(candidateRequest(candidateConfig(), false));

        var bodies = capturedElasticsearchBodies();
        assertThat(bodies).hasSize(2).allSatisfy(body -> assertThat(body.toString())
                .contains("title^17.0", "P9")
                .doesNotContain("title^4.0"));
        // 候选的 rewrite_rules 只命中第一条查询（smart tv -> 4k television）。
        assertThat(bodies.getFirst().toString()).contains("4k television");
    }

    /** 反面对照：同一批查询在缺省路径下编译出的是已发布策略（baseline）的权重。 */
    @Test
    void withoutStrategyConfigTheCompiledQueryStillUsesThePublishedWeights() {
        wiredService().run(publishedRequest(false));

        assertThat(capturedElasticsearchBodies()).hasSize(2).allSatisfy(body ->
                assertThat(body.toString())
                        .contains("title^4.0")
                        .doesNotContain("title^17.0", "4k television", "P9"));
    }

    /** 候选配置对象要原样交给 override 重载，中途不能被替换成已发布配置。 */
    @Test
    void candidateRunUsesTheOverrideOverloadWithTheExactConfigInstance() {
        var candidate = candidateConfig();

        candidateService().run(candidateRequest(candidate, false));

        ArgumentCaptor<StrategyConfig> captor = ArgumentCaptor.captor();
        verify(search, atLeastOnce()).search(any(), captor.capture());
        assertThat(captor.getAllValues()).hasSize(2).allMatch(config -> config == candidate);
        // 单参重载 = 已发布策略路径，候选评测一次都不该走。
        verify(search, never()).search(any());
    }

    /**
     * 候选评测不读取、不创建、不发布、不回滚任何 strategy 版本。
     *
     * <p>candidateService() 刻意**不打桩** strategies.current()：EvaluationService 一旦
     * 去问当前版本，拿到的会是 null 并在 .version() 上 NPE，测试立刻红。
     * 这比 verifyNoInteractions 更硬——它连"读一下但不用"都不允许。
     */
    @Test
    void candidateRunNeverAsksTheStrategyServiceForAnything() {
        candidateService().run(candidateRequest(candidateConfig(), false));

        verifyNoInteractions(strategies);
    }

    /**
     * 即使走完整的 ProductSearchService（override 路径里它仍会读一次 active 策略，
     * 这是既有代码形状），也绝不能出现任何写侧调用——候选评测是 DRY_RUN。
     */
    @Test
    void candidateRunNeverMutatesAnyStrategyVersion() {
        wiredService().run(candidateRequest(candidateConfig(), false));

        verify(strategies, never()).create(any(), any());
        verify(strategies, never()).submit(any(), any(), any());
        verify(strategies, never()).approve(any(), any(), any());
        verify(strategies, never()).publish(any(), any(), any());
        verify(strategies, never()).rollback(any(), any());
    }

    // ---------------------------------------------------------------------
    // 3. 候选配置 + persist=true：强制不落库
    // ---------------------------------------------------------------------

    /**
     * 最关键的一条防线：候选评测即使请求 persist=true 也一行都不写，并如实告知调用方。
     *
     * <p>候选配置没有版本号。若允许落库，它只能借用某个版本号写进 quality_metrics，
     * 于是 ON CONFLICT (query_id, strategy_version) DO UPDATE 会把同一版本下已发布策略的
     * 真实基线数字覆盖掉，且不可逆——一次 field_weights 扫描就能毁掉整张质量基线表。
     */
    @Test
    void candidateRunWithPersistTrueWritesNothingAndReportsTheDowngrade() {
        var result = candidateService().run(candidateRequest(candidateConfig(), true));

        verifyNoInteractions(jdbc);
        assertThat(result.persistSkipped()).isTrue();
        assertThat(result.strategyVersion()).isEqualTo(EvaluationService.CANDIDATE_STRATEGY_VERSION);
    }

    /** 候选评测 persist=false 时没有"被降级"这回事，persist_skipped 必须缺席。 */
    @Test
    void candidateRunWithPersistFalseDoesNotClaimADowngrade() {
        var result = candidateService().run(candidateRequest(candidateConfig(), false));

        verifyNoInteractions(jdbc);
        assertThat(result.persistSkipped()).isNull();
    }

    /** 工具网关入口（POST /tools/evaluations/candidate）同样受这道闸门保护。 */
    @Test
    void candidateToolEntryPointWithPersistTrueAlsoWritesNothing() {
        var result = candidateService().runCandidate(candidateRequest(candidateConfig(), true));

        verifyNoInteractions(jdbc);
        assertThat(result.persistSkipped()).isTrue();
    }

    // ---------------------------------------------------------------------
    // 4. 响应必须能区分"候选配置评测"与"已发布策略评测"
    // ---------------------------------------------------------------------

    /**
     * 下游（Agent、评测归档脚本）靠这两个标记判断一份成绩属于谁。
     *
     * <p>契约承诺两种判定方式永远同时成立：strategy_version == -1，
     * 以及 strategy_source == "candidate"；已发布评测两个标记都不出现。
     */
    @Test
    void resultDistinguishesCandidateEvaluationFromPublishedEvaluation() {
        var candidate = candidateService().run(candidateRequest(candidateConfig(), false));
        assertThat(candidate.strategyVersion()).isEqualTo(EvaluationService.CANDIDATE_STRATEGY_VERSION);
        assertThat(candidate.strategySource()).isEqualTo(EvaluationService.CANDIDATE_STRATEGY_SOURCE);

        var published = publishedService().run(publishedRequest(false));
        assertThat(published.strategyVersion()).isEqualTo(AiTestSupport.BASELINE_STRATEGY_VERSION);
        assertThat(published.strategySource()).isNull();
    }

    /** 哨兵常量本身就是对外契约的一部分，改值等于悄悄改协议。 */
    @Test
    void candidateMarkersAreTheDocumentedConstants() {
        assertThat(EvaluationService.CANDIDATE_STRATEGY_VERSION).isEqualTo(-1);
        assertThat(EvaluationService.CANDIDATE_STRATEGY_SOURCE).isEqualTo("candidate");
    }

    /**
     * "传了一个空配置"与"没传配置"是两件事：前者是一次真实的候选评测（用 baseline 默认权重），
     * 后者才是评测已发布策略。二者混淆会让 Agent 把已发布策略的成绩当成自己提案的成绩。
     */
    @Test
    void anEmptyCandidateConfigIsStillACandidateEvaluation() {
        var empty = new StrategyConfig(null, null, null, null, null, null, null);

        var result = candidateService().run(candidateRequest(empty, false));

        assertThat(result.strategyVersion()).isEqualTo(EvaluationService.CANDIDATE_STRATEGY_VERSION);
        assertThat(result.strategySource()).isEqualTo(EvaluationService.CANDIDATE_STRATEGY_SOURCE);
        verifyNoInteractions(strategies);
        // 空配置被补成 baseline 权重，但那是"候选地重跑一遍 baseline"，与已发布策略无关。
        assertThat(empty.fieldWeights()).containsEntry("title", 4.0);
    }

    // ---------------------------------------------------------------------
    // 5. 候选入口的前置校验
    // ---------------------------------------------------------------------

    /**
     * 候选端点缺 strategy_config 必须 400，而不是静默退化成"评测已发布策略"。
     *
     * <p>这是这条路径最危险的失败模式：Agent 少传一个字段，就会拿已发布策略的
     * NDCG@10 当作自己提案的自证，人类审批者看到的是一份来源错误的证据。
     */
    @Test
    void candidateEntryPointRejectsAMissingStrategyConfig() {
        var service = candidateService();
        var request = new EvaluationRequest(List.of(query(1, "smart tv")), 10, false, false);

        var thrown = catchThrowable(() -> service.runCandidate(request));

        assertThat(thrown).isInstanceOf(IllegalArgumentException.class);
        // ApiExceptionHandler 把 IllegalArgumentException 翻成 400 invalid_request，
        // message 原样透出，契约里写死了这句话。
        assertThat(thrown).hasMessageContaining("strategy_config is required for candidate evaluation");
        verifyNoInteractions(search);
        verifyNoInteractions(strategies);
        verifyNoInteractions(jdbc);
    }

    /** k 的口径不能被候选评测顺带放宽——基线只认 k=10。 */
    @Test
    void candidateRunStillRefusesAnyKOtherThanTen() {
        var service = candidateService();
        var request = new EvaluationRequest(List.of(query(1, "smart tv")), 20, false, false,
                candidateConfig());

        assertThat(catchThrowable(() -> service.runCandidate(request)))
                .isInstanceOf(IllegalArgumentException.class);
        verifyNoInteractions(jdbc);
    }

    /** 候选评测默认不走 AI：基线可比性对候选跑同样成立，否则前后对比没有意义。 */
    @Test
    void candidateRunKeepsAiOffByDefault() {
        var result = candidateService().run(candidateRequest(candidateConfig(), false));

        ArgumentCaptor<SearchOptions> captor = ArgumentCaptor.captor();
        verify(search, atLeastOnce()).search(captor.capture(), any());
        assertThat(captor.getAllValues()).allMatch(options -> !options.useAi());
        assertThat(result.useAi()).isFalse();
    }

    /**
     * 工具网关的 /evaluations/candidate 必须走带护栏的 runCandidate，而不是裸的 run。
     *
     * <p>直接调 run 的话，缺 strategy_config 会变成"静默评测已发布策略"——护栏形同虚设。
     */
    @Test
    void toolGatewayCandidateEndpointGoesThroughTheGuardedEntryPoint() {
        var evaluations = mock(EvaluationService.class);
        var controller = new ToolGatewayController(analytics, strategies,
                mock(StrategyController.class), evaluations);
        var request = candidateRequest(candidateConfig(), false);

        controller.evaluateCandidate(request);

        verify(evaluations).runCandidate(request);
        verify(evaluations, never()).run(any());
    }

    // ---------------------------------------------------------------------
    // 装配
    // ---------------------------------------------------------------------

    /** 已发布策略路径：打桩 strategies.current() 与单参 search 重载。 */
    private EvaluationService publishedService() {
        when(strategies.current()).thenReturn(baselineStrategy());
        when(search.search(any())).thenReturn(searchResponse());
        return new EvaluationService(search, strategies, jdbc);
    }

    /**
     * 候选路径：只打桩 override 重载，**不打桩** strategies.current()。
     * EvaluationService 若在候选路径上去读当前策略，会拿到 null 并直接 NPE。
     */
    private EvaluationService candidateService() {
        when(search.search(any(), any())).thenReturn(searchResponse());
        return new EvaluationService(search, strategies, jdbc);
    }

    /** 真实 ProductSearchService + SearchQueryCompiler，只把 Elasticsearch 和 AI 打桩到 HTTP 层。 */
    private EvaluationService wiredService() {
        when(strategies.current()).thenReturn(baselineStrategy());
        when(elasticsearch.search(any())).thenReturn(elasticsearchHits("P1", "P2"));
        var productSearch = new ProductSearchService(elasticsearch, new SearchQueryCompiler(),
                strategies, analytics, aiDisabled());
        return new EvaluationService(productSearch, strategies, jdbc);
    }

    /** AI 关闭的真实客户端：一次 HTTP 都不会发，评测保持纯 BM25。 */
    private AiAdapterClient aiDisabled() {
        return aiClient(false).client();
    }

    private List<SearchOptions> capturedPublishedOptions() {
        ArgumentCaptor<SearchOptions> captor = ArgumentCaptor.captor();
        verify(search, atLeastOnce()).search(captor.capture());
        return captor.getAllValues();
    }

    private List<Map<String, Object>> capturedElasticsearchBodies() {
        ArgumentCaptor<Map<String, Object>> captor = ArgumentCaptor.captor();
        verify(elasticsearch, atLeastOnce()).search(captor.capture());
        return captor.getAllValues();
    }

    /** JdbcTemplate 上真实发生过的 update 调用（打桩不算调用）。 */
    private List<Invocation> jdbcUpdates() {
        return mockingDetails(jdbc).getInvocations().stream()
                .filter(invocation -> "update".equals(invocation.getMethod().getName()))
                .toList();
    }

    private static List<Invocation> updatesTouching(List<Invocation> updates, String table) {
        return updates.stream()
                .filter(invocation -> String.valueOf(invocation.getArguments()[0]).contains(table))
                .toList();
    }

    private static EvaluationRequest publishedRequest(boolean persist) {
        return new EvaluationRequest(List.of(query(1, "smart tv"), query(2, "wireless mouse")),
                10, persist, false);
    }

    private static EvaluationRequest candidateRequest(StrategyConfig config, boolean persist) {
        return new EvaluationRequest(List.of(query(1, "smart tv"), query(2, "wireless mouse")),
                10, persist, false, config);
    }

    /** 与 baseline 处处不同的候选配置：权重、改写规则、屏蔽列表都能在编译产物里看见。 */
    private static StrategyConfig candidateConfig() {
        return new StrategyConfig(
                Map.of(), List.of(new StrategyConfig.RewriteRule("smart tv", "4k television")),
                List.of(), List.of("P9"), Map.of(), Map.of("title", 17.0), 0.0);
    }

    private static EvaluationQuery query(long id, String text) {
        return new EvaluationQuery(id, text, Map.of("P1", "E", "P2", "S"));
    }

    private static SearchResponse searchResponse() {
        return new SearchResponse("request-under-test", "smart tv", "smart tv", 2, 0, 10, 5,
                EvaluationService.CANDIDATE_STRATEGY_VERSION, List.of(product("P1"), product("P2")),
                Map.of(), false, AiRewriteStatus.NOT_REQUESTED, null, "notice");
    }

    private static Product product(String id) {
        return new Product(id, id + " title", "Acme", "desc", "bullet", "black", "us",
                "Electronics", 1999, "USD", 3, 120, "test", 1.5);
    }
}
