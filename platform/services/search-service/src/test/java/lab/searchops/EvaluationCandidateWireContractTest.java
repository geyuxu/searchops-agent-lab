package lab.searchops;

import static lab.searchops.AiTestSupport.baselineStrategy;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import jakarta.validation.ConstraintViolation;
import jakarta.validation.Validation;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Consumer;
import lab.searchops.domain.AiRewriteStatus;
import lab.searchops.domain.ApiModels.EvaluationQuery;
import lab.searchops.domain.ApiModels.EvaluationRequest;
import lab.searchops.domain.ApiModels.EvaluationResult;
import lab.searchops.domain.ApiModels.Product;
import lab.searchops.domain.ApiModels.SearchResponse;
import lab.searchops.domain.StrategyConfig;
import lab.searchops.service.EvaluationService;
import lab.searchops.service.ProductSearchService;
import lab.searchops.service.StrategyService;
import org.junit.jupiter.api.Test;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.jackson.autoconfigure.JacksonAutoConfiguration;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.jdbc.core.JdbcTemplate;
import tools.jackson.databind.json.JsonMapper;

/**
 * strategy_config 这个新增字段在**线格式**上的契约。
 *
 * <p>与 {@link EvaluationCandidateConfigTest} 分开，因为守的东西不同：那边测配置在
 * EvaluationService 内部怎么流动，这边测**外部调用方**看到的字节——请求体少传字段会不会 400、
 * 响应的键集合有没有因为新增字段而改变。
 *
 * <p>用 ApplicationContextRunner 拿 Spring Boot 真正装配出来的 JsonMapper（沿用
 * {@link EvaluationRequestWireContractTest} 的做法）：Spring MVC 反序列化 @RequestBody
 * 用的正是这个 bean，手搓 mapper 测不出线上行为。
 */
class EvaluationCandidateWireContractTest {
    /** data/scripts/evaluate.py 逐字节的载荷形状——既没有 use_ai，也没有 strategy_config。 */
    private static final String EVALUATE_PY_PAYLOAD = """
            {"queries":[{"query_id":1,"query":"smart tv","judgments":{"P1":"E"}}],\
            "k":10,"persist":true}""";

    private static final String CANDIDATE_PAYLOAD = """
            {"queries":[{"query_id":1,"query":"smart tv","judgments":{"P1":"E"}}],\
            "k":10,"persist":false,"strategy_config":{\
            "field_weights":{"title":9.5},"minimum_score":0.25,\
            "rewrite_rules":[{"match":"smart tv","rewrite":"4k television"}],\
            "blocked_product_ids":["P9"]}}""";

    /** 归档基线 baselines/evaluation-v7-bm25-baseline-20260802.json 的顶层键集合。 */
    private static final List<String> ARCHIVED_BASELINE_KEYS = List.of(
            "run_id", "strategy_version", "query_count", "precision10", "recall10",
            "mrr10", "ndcg10", "zero_result_rate", "queries", "generated_at");

    // ---------------------------------------------------------------------
    // 请求体：缺省 strategy_config 绝不能变成 400
    // ---------------------------------------------------------------------

    /**
     * 基线红线（本仓库出过一次真实回归）：请求体省略 strategy_config 时必须正常反序列化成 null，
     * 而不是抛 MismatchedInputException。
     *
     * <p>Jackson 3 把 FAIL_ON_NULL_FOR_PRIMITIVES 默认值翻成了 true，所以只要新增字段被写成
     * primitive，evaluate.py 那个只带 queries/k/persist 的载荷就会 400，
     * 200 条查询的 v7 基线（NDCG@10 0.4326）当场跑不出来。这条测试同时锁死两件事：
     * 不报错，且缺省语义是 null == "评测当前已发布策略"。
     */
    @Test
    void payloadWithoutStrategyConfigDeserialisesToNullInsteadOfFailing() {
        withBootJsonMapper(mapper -> {
            assertThatCode(() -> mapper.readValue(EVALUATE_PY_PAYLOAD, EvaluationRequest.class))
                    .as("data/scripts/evaluate.py 不发 strategy_config；缺省必须落到 null，"
                            + "否则 POST /api/v1/evaluations/run 返回 400，基线跑不起来")
                    .doesNotThrowAnyException();

            var request = mapper.readValue(EVALUATE_PY_PAYLOAD, EvaluationRequest.class);
            assertThat(request.strategyConfig()).isNull();
            // 同一个载荷里其余字段的既有语义一并复查，避免新增分量把 record 的 creator 选错。
            assertThat(request.useAi()).isFalse();
            assertThat(request.k()).isEqualTo(10);
            assertThat(request.persist()).isTrue();
            assertThat(request.queries()).hasSize(1);
        });
    }

    /** 显式传 null 与压根不传同义：都是"评测当前已发布策略"。 */
    @Test
    void explicitNullStrategyConfigMeansThePublishedStrategy() {
        withBootJsonMapper(mapper -> {
            var payload = EVALUATE_PY_PAYLOAD.replace("\"persist\":true",
                    "\"persist\":true,\"strategy_config\":null");
            assertThat(mapper.readValue(payload, EvaluationRequest.class).strategyConfig()).isNull();
        });
    }

    /** 候选配置要被完整读出来——少读一个字段就等于评测了另一份配置。 */
    @Test
    void candidatePayloadIsFullyDeserialised() {
        withBootJsonMapper(mapper -> {
            var request = mapper.readValue(CANDIDATE_PAYLOAD, EvaluationRequest.class);
            var config = request.strategyConfig();

            assertThat(config).isNotNull();
            assertThat(config.fieldWeights()).containsExactly(Map.entry("title", 9.5));
            assertThat(config.minimumScore()).isEqualTo(0.25);
            assertThat(config.blockedProductIds()).containsExactly("P9");
            assertThat(config.rewriteRules()).singleElement()
                    .satisfies(rule -> {
                        assertThat(rule.match()).isEqualTo("smart tv");
                        assertThat(rule.rewrite()).isEqualTo("4k television");
                    });
            // 新增字段不得顺带改变 use_ai 的默认值，否则候选跑和基线跑不可比。
            assertThat(request.useAi()).isFalse();
        });
    }

    /**
     * {@code "strategy_config": {}} 与不传是两件事：空对象是一次真实的候选评测
     * （字段权重被补成 baseline 默认值），不传才是评测已发布策略。
     */
    @Test
    void emptyStrategyConfigObjectIsNotTheSameAsOmittingIt() {
        withBootJsonMapper(mapper -> {
            var payload = EVALUATE_PY_PAYLOAD.replace("\"persist\":true",
                    "\"persist\":true,\"strategy_config\":{}");

            var config = mapper.readValue(payload, EvaluationRequest.class).strategyConfig();

            assertThat(config).isNotNull();
            assertThat(config.fieldWeights()).containsEntry("title", 4.0)
                    .containsEntry("brand", 2.5).containsEntry("bullet_point", 1.5)
                    .containsEntry("description", 1.0).containsEntry("category", 1.2);
            assertThat(config.minimumScore()).isEqualTo(0.0);
        });
    }

    // ---------------------------------------------------------------------
    // 请求体：Bean Validation 仍然按契约拒绝越界的候选配置
    // ---------------------------------------------------------------------

    /** 候选配置的取值上限要真的生效（field_weights <= 20），否则 400 的契约是空话。 */
    @Test
    void outOfRangeCandidateFieldWeightIsRejectedByValidation() {
        var request = new EvaluationRequest(List.of(query()), 10, false, false,
                new StrategyConfig(null, null, null, null, null, Map.of("title", 25.0), null));

        assertThat(violations(request)).isNotEmpty();
    }

    /** brand_boosts <= 100 同理。 */
    @Test
    void outOfRangeCandidateBrandBoostIsRejectedByValidation() {
        var request = new EvaluationRequest(List.of(query()), 10, false, false,
                new StrategyConfig(null, null, null, null, Map.of("Acme", 150.0), null, null));

        assertThat(violations(request)).isNotEmpty();
    }

    /** 合法的候选配置必须干净通过，别把护栏收得太紧把正常提案也挡了。 */
    @Test
    void aLegalCandidateConfigPassesValidation() {
        var request = new EvaluationRequest(List.of(query()), 10, false, false,
                new StrategyConfig(null, null, null, List.of("P9"), Map.of("Acme", 2.0),
                        Map.of("title", 17.0), 0.25));

        assertThat(violations(request)).isEmpty();
    }

    // ---------------------------------------------------------------------
    // 响应：已发布策略评测的键集合一个字节都没变
    // ---------------------------------------------------------------------

    /**
     * 已发布策略评测的响应 JSON 里，两个新增标记键必须**整体缺席**。
     *
     * <p>application.yml 的 default-property-inclusion=non_null 让 null 字段消失，
     * 于是 baselines/ 归档与 data/processed/evaluation-latest.json 的形状不受"新增字段"影响。
     * 一旦有人把 strategy_source 改成常量字符串（比如 "published"），
     * 归档文件的键集合就变了，基线可比性的前提随之作废——这条测试就是那道闸门。
     */
    @Test
    void publishedEvaluationResponseKeepsTheArchivedKeySet() {
        withBootJsonMapper(mapper -> {
            var json = mapper.readTree(mapper.writeValueAsString(publishedResult()));

            assertThat(json.propertyNames()).doesNotContain("strategy_source", "persist_skipped");
            assertThat(json.propertyNames()).containsAll(ARCHIVED_BASELINE_KEYS);
            assertThat(json.get("strategy_version").asInt())
                    .isEqualTo(AiTestSupport.BASELINE_STRATEGY_VERSION);
        });
    }

    /** 候选评测的响应必须带上两个标记，下游按任一判定都成立。 */
    @Test
    void candidateEvaluationResponseCarriesBothMarkers() {
        withBootJsonMapper(mapper -> {
            var json = mapper.readTree(mapper.writeValueAsString(candidateResult(true)));

            assertThat(json.get("strategy_version").asInt())
                    .isEqualTo(EvaluationService.CANDIDATE_STRATEGY_VERSION);
            assertThat(json.get("strategy_source").asString())
                    .isEqualTo(EvaluationService.CANDIDATE_STRATEGY_SOURCE);
            // persist=true 被强制忽略时必须如实告知，否则调用方会以为数据写进去了。
            assertThat(json.get("persist_skipped").asBoolean()).isTrue();
            assertThat(json.propertyNames()).containsAll(ARCHIVED_BASELINE_KEYS);
        });
    }

    /** persist=false 的候选评测没有"被降级"这回事，persist_skipped 键同样缺席。 */
    @Test
    void candidateEvaluationWithoutPersistOmitsThePersistSkippedKey() {
        withBootJsonMapper(mapper -> {
            var json = mapper.readTree(mapper.writeValueAsString(candidateResult(false)));

            assertThat(json.propertyNames()).doesNotContain("persist_skipped");
            assertThat(json.get("strategy_source").asString())
                    .isEqualTo(EvaluationService.CANDIDATE_STRATEGY_SOURCE);
        });
    }

    // ---------------------------------------------------------------------
    // 装配
    // ---------------------------------------------------------------------

    /** 用真实的 EvaluationService 产出响应对象，避免手搓 record 测了个"理论形状"。 */
    private EvaluationResult publishedResult() {
        var search = mock(ProductSearchService.class);
        var strategies = mock(StrategyService.class);
        when(strategies.current()).thenReturn(baselineStrategy());
        when(search.search(any())).thenReturn(searchResponse());
        return new EvaluationService(search, strategies, mock(JdbcTemplate.class))
                .run(new EvaluationRequest(List.of(query()), 10, false, false));
    }

    private EvaluationResult candidateResult(boolean persist) {
        var search = mock(ProductSearchService.class);
        when(search.search(any(), any())).thenReturn(searchResponse());
        return new EvaluationService(search, mock(StrategyService.class), mock(JdbcTemplate.class))
                .runCandidate(new EvaluationRequest(List.of(query()), 10, persist, false,
                        StrategyConfig.baseline()));
    }

    /** 用真实的 Bean Validation 实现跑一遍——@Valid 级联到 StrategyConfig 才是契约里那个 400。 */
    private static Set<ConstraintViolation<EvaluationRequest>> violations(EvaluationRequest request) {
        try (var factory = Validation.buildDefaultValidatorFactory()) {
            return factory.getValidator().validate(request);
        }
    }

    private static EvaluationQuery query() {
        return new EvaluationQuery(1, "smart tv", Map.of("P1", "E"));
    }

    private static SearchResponse searchResponse() {
        return new SearchResponse("request-under-test", "smart tv", "smart tv", 1, 0, 10, 5,
                EvaluationService.CANDIDATE_STRATEGY_VERSION,
                List.of(new Product("P1", "P1 title", "Acme", "desc", "bullet", "black", "us",
                        "Electronics", 1999, "USD", 3, 120, "test", 1.5)),
                Map.of(), false, AiRewriteStatus.NOT_REQUESTED, null, "notice");
    }

    /**
     * 不启动 web 容器、不启动 Testcontainers，只跑 Jackson 的自动装配，
     * 但 spring.jackson.* 的解析路径与线上完全一致。
     */
    private void withBootJsonMapper(Consumer<JsonMapper> assertions) {
        new ApplicationContextRunner()
                .withConfiguration(AutoConfigurations.of(JacksonAutoConfiguration.class))
                // 与 application.yml 保持一致，否则测的是另一套 Jackson 配置。
                .withPropertyValues("spring.jackson.default-property-inclusion=non_null")
                .run(context -> assertions.accept(context.getBean(JsonMapper.class)));
    }
}
