package lab.searchops;

import static lab.searchops.AiTestSupport.baselineStrategy;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import java.lang.reflect.RecordComponent;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;
import java.util.stream.Stream;
import lab.searchops.domain.AiRerankStatus;
import lab.searchops.domain.AiRewriteStatus;
import lab.searchops.domain.ApiModels.EvaluationQuery;
import lab.searchops.domain.ApiModels.EvaluationRequest;
import lab.searchops.domain.ApiModels.EvaluationResult;
import lab.searchops.domain.ApiModels.Product;
import lab.searchops.domain.ApiModels.SearchResponse;
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
 * 四个模型身份字段（SearchResponse.ai_model / rerank_model、
 * EvaluationResult.ai_model / rerank_model）在**线格式**上的契约。
 *
 * <p>与 {@link ModelIdentityTest} 分工：那边测模型标识在服务内部怎么流动，
 * 这边测外部看到的字节——键名、缺省时键是否消失、既有归档产物的形状有没有被新增字段改掉。
 *
 * <p>为什么这一层必须单独守：这四个字段的全部意义是"被写进 experiments/ 当证据"，
 * 而证据是按 JSON 读的。键名写错、或者缺省时冒出一个 {@code "ai_model": null}，
 * 都会让下游脚本与半年后的读者拿到与预期不同的东西，而 Java 侧的行为测试一条都不会红。
 *
 * <p>沿用 {@link EvaluationCandidateWireContractTest} 的做法，用 ApplicationContextRunner 拿
 * Spring Boot 真正装配出来的 JsonMapper：Spring MVC 序列化响应体用的正是这个 bean，
 * 手搓 mapper 测不出线上行为（尤其是 default-property-inclusion=non_null 这条）。
 */
class ModelIdentityWireContractTest {
    /**
     * 归档基线 baselines/evaluation-v7-bm25-baseline-20260802.json
     * （sha256 e8d25d46…，不可再生资产）的顶层键集合。
     */
    private static final List<String> ARCHIVED_BASELINE_KEYS = List.of(
            "run_id", "strategy_version", "query_count", "precision10", "recall10",
            "mrr10", "ndcg10", "zero_result_rate", "queries", "generated_at");

    /**
     * 今天一次**无 AI** 评测的完整顶层键集合。
     *
     * <p>= 归档键 + use_ai。use_ai 是更早那次"评测 AI 开关"改动加上的唯一一个常驻键
     * （primitive boolean，non_null inclusion 管不到它），此后不该再多出任何常驻键。
     * 这里用"恰好等于"而不是"包含"来断言：新增可选字段一旦忘了做成可空类型，
     * 无 AI 产物就会长出第 12 个键，而这正是"既有归档仍可逐字节 diff"这个前提作废的时刻。
     */
    private static final List<String> AI_FREE_RESULT_KEYS =
            Stream.concat(ARCHIVED_BASELINE_KEYS.stream(), Stream.of("use_ai")).toList();

    private static final String REWRITE_MODEL = "qwen3.7-flash-2026-07-15";
    private static final String RERANK_MODEL = "qwen3.7-plus-2026-07-15";

    // ---------------------------------------------------------------------
    // 1. Jackson 陷阱：四个字段必须是对象类型
    // ---------------------------------------------------------------------

    /**
     * 四个新字段都必须是 String（对象类型），不能是 primitive。
     *
     * <p>本仓库出过一次真实回归：Jackson 3 把 FAIL_ON_NULL_FOR_PRIMITIVES 的默认值从 false
     * 翻成了 true（Spring Boot 4.1 的 use-jackson2-defaults 默认为 false，不恢复旧语义），
     * 于是缺省的 primitive 字段不再静默填零值，而是抛 MismatchedInputException 变成 400——
     * data/scripts/evaluate.py 那个只带 queries/k/persist 的载荷当场跑不起来。
     * 同一个陷阱在响应侧的形态是：primitive 无法表达"没有观测到模型"，
     * 只能填一个零值，于是"没有证据"又一次被伪装成"有证据"。
     *
     * <p>这条断言便宜到近乎白送，但它把"为什么是 String 而不是别的"这个决定钉在了测试里。
     */
    @Test
    void allFourModelFieldsAreNullableReferenceTypes() {
        for (var owner : List.of(SearchResponse.class, EvaluationResult.class)) {
            for (var name : List.of("aiModel", "rerankModel")) {
                var type = componentType(owner, name);
                assertThat(type.isPrimitive())
                        .as("%s.%s 必须是对象类型：primitive 表达不了\"没有观测到模型\"，"
                                + "而且 Jackson 3 的 FAIL_ON_NULL_FOR_PRIMITIVES 默认为 true，"
                                + "缺省的 primitive 会直接把请求/回读打成 400", owner.getSimpleName(), name)
                        .isFalse();
                assertThat(type).isEqualTo(String.class);
            }
        }
    }

    // ---------------------------------------------------------------------
    // 2. SearchResponse：缺省时两个键整体消失
    // ---------------------------------------------------------------------

    /**
     * 适配器没声明模型时，两个键在响应 JSON 里**整体缺席**，而不是出现一个 null 值。
     *
     * <p>{@code "ai_model": null} 与键不存在对读者是两回事：前者看起来像"字段填过了，
     * 只是没显示出来"。前端与运维面板必须按"键可能不存在"来读，这条就是那个约定。
     */
    @Test
    void searchResponseOmitsBothModelKeysWhenNoModelWasObserved() {
        withBootJsonMapper(mapper -> {
            var json = mapper.writeValueAsString(searchResponse(null, null));

            assertThat(json).doesNotContain("ai_model").doesNotContain("rerank_model");
            // 状态与 provider 的既有行为不受影响：状态永远在，provider 兜底成占位名。
            assertThat(json).contains("\"ai_status\":\"APPLIED\"", "\"ai_provider\":\"unknown\"");
        });
    }

    /** 有模型时用契约里那两个键名回报——键名就是下游脚本与归档读取的入口，改名即破坏契约。 */
    @Test
    void searchResponseUsesTheContractedModelKeyNames() {
        withBootJsonMapper(mapper -> {
            var json = mapper.writeValueAsString(searchResponse(REWRITE_MODEL, RERANK_MODEL));

            assertThat(json).contains("\"ai_model\":\"" + REWRITE_MODEL + "\"",
                    "\"rerank_model\":\"" + RERANK_MODEL + "\"");
        });
    }

    /**
     * 改动前那条构造路径（14 参兼容构造器）产出的响应里，两个键同样缺席。
     *
     * <p>既有构造点一个字都没改就是靠它。这条一旦红，说明"不带 AI 的响应形状逐字节不变"
     * 这句话已经不成立了——而 explain / preview 这些路径正是走它。
     */
    @Test
    void theLegacySearchResponseConstructorProducesNoModelKeys() {
        withBootJsonMapper(mapper -> {
            var legacy = new SearchResponse("r1", "smart tv", "smart tv", 0, 0, 10, 3, 7,
                    List.of(), Map.of(), false, AiRewriteStatus.NOT_REQUESTED, null, "notice");

            assertThat(legacy.aiModel()).isNull();
            assertThat(legacy.rerankModel()).isNull();
            assertThat(mapper.writeValueAsString(legacy))
                    .doesNotContain("ai_model").doesNotContain("rerank_model");
        });
    }

    // ---------------------------------------------------------------------
    // 3. EvaluationResult：既有归档产物的形状一个字节都没变
    // ---------------------------------------------------------------------

    /**
     * 无 AI 评测的响应键集合必须**恰好**还是归档时那一套（+ use_ai）。
     *
     * <p>baselines/evaluation-v7-bm25-baseline-20260802.json 与 experiments/ 下的 BM25 基线
     * 都是这条路径产出的，它们是不可再生资产：新增字段若在这里多留下哪怕一个键，
     * 新旧产物就不再能直接 diff，"基线可比"这个前提随之作废。
     */
    @Test
    void anAiFreeEvaluationResultKeepsExactlyTheArchivedKeySet() {
        withBootJsonMapper(mapper -> {
            var json = mapper.readTree(mapper.writeValueAsString(evaluationResult(false, null, null)));

            assertThat(json.propertyNames())
                    .as("无 AI 评测的产物形状不得因为新增模型身份字段而改变")
                    .containsExactlyInAnyOrderElementsOf(AI_FREE_RESULT_KEYS);
            assertThat(json.propertyNames()).doesNotContain("ai_model", "rerank_model");
        });
    }

    /** 观测到模型时，两个键带着观测值出现在产物顶层——这才是"产物能自证是谁跑的"。 */
    @Test
    void anEvaluationResultCarriesTheObservedModelsUnderTheContractedNames() {
        withBootJsonMapper(mapper -> {
            var json = mapper.readTree(
                    mapper.writeValueAsString(evaluationResult(true, REWRITE_MODEL, RERANK_MODEL)));

            assertThat(json.get("ai_model").asString()).isEqualTo(REWRITE_MODEL);
            assertThat(json.get("rerank_model").asString()).isEqualTo(RERANK_MODEL);
            assertThat(json.propertyNames()).containsAll(ARCHIVED_BASELINE_KEYS);
        });
    }

    /**
     * 一轮里观测到多个模型时，产物里就是逗号连起来的那一串。
     *
     * <p>它是"这轮不干净"的信号，必须原样出现在 JSON 里让人看见，而不是被折叠成一个值。
     */
    @Test
    void multipleObservedModelsAreVisibleInTheArtifact() {
        withBootJsonMapper(mapper -> {
            var json = mapper.readTree(mapper.writeValueAsString(
                    evaluationResult(true, "model-a,model-b", "rerank-a,rerank-b")));

            assertThat(json.get("ai_model").asString()).isEqualTo("model-a,model-b");
            assertThat(json.get("rerank_model").asString()).isEqualTo("rerank-a,rerank-b");
        });
    }

    /**
     * 归档产物（没有这两个键）必须还能被读回来。
     *
     * <p>这是 primitive 陷阱在响应侧的直接回归测试：若这两个分量是 primitive，
     * 缺键的既有归档文件一旦被反序列化（契约客户端、回读脚本、对拍工具）就会抛
     * MismatchedInputException——正是 EvaluationRequest 上那次 400 回归的同一个机制。
     */
    @Test
    void anArchivedResultWithoutModelKeysCanStillBeReadBack() {
        withBootJsonMapper(mapper -> {
            var archived = mapper.writeValueAsString(evaluationResult(false, null, null));
            assertThat(archived).doesNotContain("ai_model").doesNotContain("rerank_model");

            assertThatCode(() -> mapper.readValue(archived, EvaluationResult.class))
                    .as("既有归档里没有 ai_model / rerank_model 这两个键；"
                            + "缺省必须落到 null，而不是反序列化失败")
                    .doesNotThrowAnyException();

            var reread = mapper.readValue(archived, EvaluationResult.class);
            assertThat(reread.aiModel()).isNull();
            assertThat(reread.rerankModel()).isNull();
            assertThat(reread.useAi()).isFalse();
        });
    }

    /** 带模型的产物回读后两个值原样还在——写得出、读得回，证据链才闭合。 */
    @Test
    void anArtifactWithModelKeysRoundTripsUnchanged() {
        withBootJsonMapper(mapper -> {
            var written = mapper.writeValueAsString(evaluationResult(true, REWRITE_MODEL, RERANK_MODEL));

            var reread = mapper.readValue(written, EvaluationResult.class);

            assertThat(reread.aiModel()).isEqualTo(REWRITE_MODEL);
            assertThat(reread.rerankModel()).isEqualTo(RERANK_MODEL);
        });
    }

    // ---------------------------------------------------------------------
    // 装配
    // ---------------------------------------------------------------------

    /**
     * 用真实的 EvaluationService 产出 EvaluationResult，而不是手搓一个 record：
     * 手搓只能测出"理论形状"，测不出汇总逻辑真正填了什么。
     */
    private EvaluationResult evaluationResult(boolean useAi, String aiModel, String rerankModel) {
        var search = mock(ProductSearchService.class);
        var strategies = mock(StrategyService.class);
        when(strategies.current()).thenReturn(baselineStrategy());
        when(search.search(any())).thenReturn(observed(aiModel, rerankModel));
        return new EvaluationService(search, strategies, mock(JdbcTemplate.class))
                .run(new EvaluationRequest(List.of(new EvaluationQuery(1, "smart tv",
                        Map.of("P1", "E"))), 10, false, useAi));
    }

    /** 一次两条链路都成功的搜索响应，两个模型标识按参数填入（null == 适配器没声明）。 */
    private static SearchResponse searchResponse(String aiModel, String rerankModel) {
        return new SearchResponse("r1", "smart tv", "4k television", 1, 0, 10, 3, 7,
                List.of(product("P1")), Map.of(), true, AiRewriteStatus.APPLIED, "unknown", aiModel,
                true, AiRerankStatus.APPLIED, "unknown", rerankModel, 50, "notice");
    }

    /** 评测里每条查询观测到的响应。 */
    private static SearchResponse observed(String aiModel, String rerankModel) {
        return new SearchResponse("evaluation-1", "smart tv", "smart tv", 1, 0, 10, 3,
                AiTestSupport.BASELINE_STRATEGY_VERSION, List.of(product("P1")), Map.of(),
                aiModel != null, aiModel == null ? AiRewriteStatus.NOT_REQUESTED : AiRewriteStatus.APPLIED,
                aiModel == null ? null : "langchain", aiModel,
                rerankModel != null,
                rerankModel == null ? AiRerankStatus.NOT_REQUESTED : AiRerankStatus.APPLIED,
                rerankModel == null ? null : "langchain-rerank", rerankModel,
                rerankModel == null ? null : 50, "notice");
    }

    private static Product product(String id) {
        return new Product(id, id + " title", "Acme", "desc", "bullet", "black", "us",
                "Electronics", 1999, "USD", 3, 120, "test", 1.5);
    }

    private static Class<?> componentType(Class<?> owner, String component) {
        return Arrays.stream(owner.getRecordComponents())
                .filter(candidate -> candidate.getName().equals(component))
                .findFirst()
                .map(RecordComponent::getType)
                .orElseThrow(() -> new AssertionError(
                        owner.getSimpleName() + " 上没有分量 " + component + "，模型身份字段被删掉或改名了"));
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
