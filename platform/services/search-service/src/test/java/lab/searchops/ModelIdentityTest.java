package lab.searchops;

import static lab.searchops.AiTestSupport.REWRITE_URL;
import static lab.searchops.AiTestSupport.aiClient;
import static lab.searchops.AiTestSupport.baselineStrategy;
import static lab.searchops.AiTestSupport.elasticsearchHits;
import static lab.searchops.AiTestSupport.options;
import static lab.searchops.AiTestSupport.rewriteResponse;
import static lab.searchops.AiTestSupport.rewriteResponseWithRawModel;
import static lab.searchops.RerankTestSupport.RERANK_URL;
import static lab.searchops.RerankTestSupport.candidates;
import static lab.searchops.RerankTestSupport.rerankClient;
import static lab.searchops.RerankTestSupport.rerankOptions;
import static lab.searchops.RerankTestSupport.rerankResponse;
import static lab.searchops.RerankTestSupport.rerankResponseWithRawModel;
import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.params.provider.Arguments.arguments;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.requestTo;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withException;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withServerError;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withSuccess;

import java.net.SocketTimeoutException;
import java.util.List;
import java.util.Map;
import java.util.stream.IntStream;
import java.util.stream.Stream;
import lab.searchops.domain.AiRerankStatus;
import lab.searchops.domain.AiRewriteStatus;
import lab.searchops.domain.ApiModels.EvaluationQuery;
import lab.searchops.domain.ApiModels.EvaluationRequest;
import lab.searchops.domain.ApiModels.Product;
import lab.searchops.domain.ApiModels.SearchOptions;
import lab.searchops.domain.ApiModels.SearchResponse;
import lab.searchops.service.AiAdapterClient;
import lab.searchops.service.AiAdapterClient.RerankResult;
import lab.searchops.service.ElasticsearchGateway;
import lab.searchops.service.EvaluationService;
import lab.searchops.service.ProductSearchService;
import lab.searchops.service.SearchAnalyticsRepository;
import lab.searchops.service.SearchQueryCompiler;
import lab.searchops.service.StrategyService;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.web.client.MockRestServiceServer;
import org.springframework.test.web.client.ResponseCreator;

/**
 * 模型身份（ai_model / rerank_model）的取证契约。
 *
 * <p>这个类守的东西与 {@link AiAdapterClientTest} / {@link RerankFallbackTest} 不同：那两个类
 * 保护的是"降级要能被区分出来"，这里保护的是"产物能自证是哪个模型跑出来的"。
 * 留出集上重排的 ΔNDCG@10（+0.0968 / +0.1207 / +0.1349，N=20/50/100）整个建立在模型身上，
 * 而重排结果的形状（候选集的一个排列）对模型身份完全不敏感——换个模型跑出另一组数字，
 * 产物里看不出任何差别。此前唯一的记录是人手写进 experiments/*.json 的 {@code ai} 块
 * （例如 experiments/rerank-holdout-n50.json 里的 {@code "model": "qwen3.7-flash-2026-07-15"}），
 * 那是注释不是证据：没有任何机制保证它与真正跑过的进程一致。
 *
 * <p>因此本类反复检查同一条红线：<b>宁可没有这一行，也不能有一行说不清来源的</b>。
 * 模型标识缺失时必须保持 null（键在响应里整体消失），绝不像 provider 那样兜底成占位值。
 *
 * <p>全部纯 JVM：不启动 Spring 上下文、不启动 Testcontainers，打桩只打到 HTTP 这一层，
 * 沿用 {@link AiTestSupport} / {@link RerankTestSupport} 的取舍。
 * 线格式那一层（键消失、既有产物形状不变、Jackson 的 primitive 陷阱）由
 * {@link ModelIdentityWireContractTest} 单独把守。
 */
class ModelIdentityTest {
    /** 留出集那几轮真正跑过的模型，见 experiments/rerank-holdout-n50.json 手写的 ai 块。 */
    private static final String REWRITE_MODEL = "qwen3.7-flash-2026-07-15";

    /**
     * 测试用的虚构标识，刻意与 REWRITE_MODEL 不同（并非某一轮真跑过的模型）：
     * 两个字段一旦错位赋值，必须当场暴露而不是"两边都对得上"。
     */
    private static final String RERANK_MODEL = "qwen3.7-plus-2026-07-15";

    /**
     * 这条红线的完整理由，作为断言失败信息挂在每一处"必须是 null"上。
     *
     * <p>写这么长是有意的：将来若有人为了"日志好看"给 model 加个兜底值，他会先读到这段话。
     */
    private static final String WHY_NULL_NOT_PLACEHOLDER =
            "模型标识缺失时必须保持 null，绝不能兜底成 \"unknown\" 或任何占位值。"
            + "provider 可以兜底，因为它只做观测；model 不行，因为它是要写进 experiments/ 归档、"
            + "用来回答\"这组数字是哪个模型跑出来的\"的证据。一个编造出来的占位值在归档里与真实"
            + "模型名长得一模一样，读的人无从分辨，等于把\"没有证据\"伪装成\"有证据\"——"
            + "而\"可复现\"这句话恰恰押在这一行上。缺了就让键消失，问题会在需要它的时候暴露。";

    private static final String WHY_NO_MODEL_ON_FALLBACK =
            "降级时 model 必须是 null：这一次调用没有产出任何结果，也就不存在\"哪个模型跑的\"。"
            + "若在这里留下上一次成功调用的残留值（或响应体里那个没被采纳的 model），"
            + "产物就会声称一组纯 BM25 的数字是某个 LLM 跑出来的——比没有记录更糟。";

    private final ElasticsearchGateway elasticsearch = mock(ElasticsearchGateway.class);
    private final StrategyService strategies = mock(StrategyService.class);
    private final SearchAnalyticsRepository analytics = mock(SearchAnalyticsRepository.class);
    private final ProductSearchService search = mock(ProductSearchService.class);
    private final JdbcTemplate jdbc = mock(JdbcTemplate.class);

    // ---------------------------------------------------------------------
    // 1. 适配器回报了 model：它必须原样一路到 SearchResponse
    // ---------------------------------------------------------------------

    /**
     * 改写链路：适配器回报什么模型就记什么模型，不做任何解释或改写。
     *
     * <p>参数化到几个形态各异的标识，是因为 Java 侧对模型名不该有任何认知——
     * 它只是一个要被原样归档的字符串。任何"只认识某些模型名"的逻辑都会在换模型时静默失效，
     * 与 docs/ai-handoff.md 承诺的"换模型不需改上游"直接冲突。
     */
    @ParameterizedTest
    @ValueSource(strings = {"qwen3.7-flash-2026-07-15", "gpt-5.2-mini", "deterministic-mock",
            "accounts/fireworks/models/llama-v4-70b", "claude-opus-5-20260514"})
    void rewriteReportsExactlyTheModelTheAdapterNamed(String model) {
        var wiring = aiClient(true);
        expectRewrite(wiring.server(), rewriteResponse("4k television", "langchain", model));

        var result = wiring.client().rewrite(options("smart tv", true));

        assertThat(result.model()).isEqualTo(model);
        assertThat(result.provider()).isEqualTo("langchain");
        assertThat(result.status()).isEqualTo(AiRewriteStatus.APPLIED);
        wiring.server().verify();
    }

    /** 重排链路同理——而这条链路上模型身份最要紧，全部收益都由它产生。 */
    @ParameterizedTest
    @ValueSource(strings = {"qwen3.7-flash-2026-07-15", "gpt-5.2-mini", "deterministic-mock",
            "accounts/fireworks/models/llama-v4-70b", "claude-opus-5-20260514"})
    void rerankReportsExactlyTheModelTheAdapterNamed(String model) {
        var wiring = rerankClient();
        expectRerank(wiring, rerankResponse("langchain-rerank", List.of("P03", "P01"), model));

        var result = rerank(wiring, candidates(3));

        assertThat(result.model()).isEqualTo(model);
        assertThat(result.provider()).isEqualTo("langchain-rerank");
        assertThat(result.status()).isEqualTo(AiRerankStatus.APPLIED);
        wiring.rerankServer().verify();
    }

    /**
     * 改写路径的端到端证据：模型标识确实到了 SearchResponse.ai_model，
     * 没有在 ProductSearchService 里被吃掉。
     *
     * <p>只测 RewriteResult 不够——EvaluationService 是从 SearchResponse 上观测模型的，
     * 中间少接一根线，整条取证链就断在这里，而且外部完全看不出来（响应仍然 200）。
     */
    @Test
    void theRewriteModelReachesTheSearchResponse() {
        var wiring = aiClient(true);
        expectRewrite(wiring.server(), rewriteResponse("4k television", "langchain", REWRITE_MODEL));

        var response = searchService(wiring.client()).search(options("smart tv", true));

        assertThat(response.aiModel()).isEqualTo(REWRITE_MODEL);
        assertThat(response.aiProvider()).isEqualTo("langchain");
        assertThat(response.aiStatus()).isEqualTo(AiRewriteStatus.APPLIED);
        // 这次请求没要求重排，重排那一半不能凭空出现一个模型名。
        assertThat(response.rerankModel()).isNull();
        assertThat(response.rerankStatus()).isEqualTo(AiRerankStatus.NOT_REQUESTED);
        wiring.server().verify();
    }

    /** 重排路径的端到端证据。use_ai=false，所以改写那台 server 一次都不该被碰。 */
    @Test
    void theRerankModelReachesTheSearchResponse() {
        var wiring = rerankClient();
        expectRerank(wiring, rerankResponse("langchain-rerank", List.of("P2", "P1"), RERANK_MODEL));

        var response = searchService(wiring.client()).search(searchOptions(false, true));

        assertThat(response.rerankModel()).isEqualTo(RERANK_MODEL);
        assertThat(response.rerankProvider()).isEqualTo("langchain-rerank");
        assertThat(response.rerankStatus()).isEqualTo(AiRerankStatus.APPLIED);
        assertThat(response.aiModel()).isNull();
        assertThat(response.aiStatus()).isEqualTo(AiRewriteStatus.NOT_REQUESTED);
        wiring.rerankServer().verify();
        wiring.rewriteServer().verify();
    }

    /**
     * 两条链路同时开时，两个模型标识不能互换位置。
     *
     * <p>ProductSearchService 是按位置把四个值（provider/model × 改写/重排）塞进
     * SearchResponse 的 20 参构造器的，写反了编译器一个字都不会说——而产物会把改写模型
     * 记成重排模型，正好把"重排收益归因到哪个模型"这个唯一重要的问题答反。
     * 两个常量刻意取成不同的值，就是为了让这种错位当场暴露。
     */
    @Test
    void theTwoModelsAreNeverCrossedInTheSearchResponse() {
        var wiring = rerankClient();
        wiring.rewriteServer().expect(requestTo(REWRITE_URL)).andRespond(withSuccess(
                rewriteResponse("4k television", "langchain", REWRITE_MODEL),
                MediaType.APPLICATION_JSON));
        expectRerank(wiring, rerankResponse("langchain-rerank", List.of("P2", "P1"), RERANK_MODEL));

        var response = searchService(wiring.client()).search(searchOptions(true, true));

        assertThat(response.aiModel()).isEqualTo(REWRITE_MODEL).isNotEqualTo(RERANK_MODEL);
        assertThat(response.rerankModel()).isEqualTo(RERANK_MODEL).isNotEqualTo(REWRITE_MODEL);
        assertThat(response.aiProvider()).isEqualTo("langchain");
        assertThat(response.rerankProvider()).isEqualTo("langchain-rerank");
        wiring.rewriteServer().verify();
        wiring.rerankServer().verify();
    }

    /**
     * 首尾空白被剪掉，模型名本身不变。
     *
     * <p>不剪的话，同一个模型会因为一次多余的空格在 EvaluationService 的 LinkedHashSet 里
     * 变成两个条目，产物看上去像是"中途换过模型"——一个纯粹由格式噪音伪造出来的脏信号。
     */
    @Test
    void surroundingWhitespaceIsTrimmedOffTheModelIdentity() {
        var wiring = aiClient(true);
        expectRewrite(wiring.server(),
                rewriteResponse("4k television", "langchain", "  " + REWRITE_MODEL + "  "));

        assertThat(wiring.client().rewrite(options("smart tv", true)).model())
                .isEqualTo(REWRITE_MODEL);
    }

    // ---------------------------------------------------------------------
    // 2. 适配器没回报 model：保持 null，绝不兜底
    // ---------------------------------------------------------------------

    /**
     * 本类最重要的一条：provider 缺失兜底成 "unknown"，model 缺失必须保持 null。
     *
     * <p>两个字段在同一个响应里同时缺席，一次断言里同时确认两种**刻意不同**的策略，
     * 这样"照着 provider 抄一个兜底"这种最自然的改法会立刻变红。理由见
     * {@link #WHY_NULL_NOT_PLACEHOLDER}。
     */
    @Test
    void aMissingRewriteModelStaysNullWhileTheProviderFallsBackToUnknown() {
        var wiring = aiClient(true);
        expectRewrite(wiring.server(), rewriteResponse("4k television", null));

        var result = wiring.client().rewrite(options("smart tv", true));

        assertThat(result.provider())
                .as("provider 缺失只影响观测，兜底成占位名是刻意的，这一半不要改")
                .isEqualTo("unknown");
        assertThat(result.model()).as(WHY_NULL_NOT_PLACEHOLDER).isNull();
        // 缺 model 绝不能拖垮整次调用：它是可选契约，第三方 provider 完全可以不声明。
        assertThat(result.status()).isEqualTo(AiRewriteStatus.APPLIED);
        assertThat(result.query()).isEqualTo("4k television");
    }

    /** 重排链路的同一条红线。 */
    @Test
    void aMissingRerankModelStaysNullWhileTheProviderFallsBackToUnknown() {
        var wiring = rerankClient();
        expectRerank(wiring, rerankResponse(null, List.of("P03", "P01")));

        var result = rerank(wiring, candidates(3));

        assertThat(result.provider()).isEqualTo("unknown");
        assertThat(result.model()).as(WHY_NULL_NOT_PLACEHOLDER).isNull();
        assertThat(result.status()).isEqualTo(AiRerankStatus.APPLIED);
        assertThat(result.candidateCount()).isEqualTo(3);
    }

    /** 端到端：适配器两条链路都没声明模型时，响应里两个 model 都是 null（键随之消失）。 */
    @Test
    void theSearchResponseCarriesNoModelWhenTheAdapterDeclaredNone() {
        var wiring = rerankClient();
        wiring.rewriteServer().expect(requestTo(REWRITE_URL)).andRespond(withSuccess(
                rewriteResponse("4k television", "langchain"), MediaType.APPLICATION_JSON));
        expectRerank(wiring, rerankResponse("langchain-rerank", List.of("P2", "P1")));

        var response = searchService(wiring.client()).search(searchOptions(true, true));

        assertThat(response.aiModel()).as(WHY_NULL_NOT_PLACEHOLDER).isNull();
        assertThat(response.rerankModel()).as(WHY_NULL_NOT_PLACEHOLDER).isNull();
        // 调用本身是成功的：provider 与状态都在，只有"哪个模型"这一条不知道。
        assertThat(response.aiStatus()).isEqualTo(AiRewriteStatus.APPLIED);
        assertThat(response.rerankStatus()).isEqualTo(AiRerankStatus.APPLIED);
        assertThat(response.aiProvider()).isEqualTo("langchain");
        assertThat(response.rerankProvider()).isEqualTo("langchain-rerank");
    }

    // ---------------------------------------------------------------------
    // 3. 空白 / 非标量与缺失同等对待
    // ---------------------------------------------------------------------

    /**
     * 空串、纯空格、制表符都必须与"没回报"同义。
     *
     * <p>放进归档的 {@code "ai_model": ""} 比没有这一行更糟：它是一个存在的键，
     * 读的人会以为字段填过了，只是显示不出来。{@code "\t"} 是 JSON 转义序列，
     * 解析出来是一个真的制表符。
     */
    @ParameterizedTest
    @ValueSource(strings = {"", " ", "   ", "\\t"})
    void aBlankRewriteModelIsTreatedExactlyLikeAMissingOne(String blank) {
        var wiring = aiClient(true);
        expectRewrite(wiring.server(), rewriteResponse("4k television", "langchain", blank));

        assertThat(wiring.client().rewrite(options("smart tv", true)).model())
                .as(WHY_NULL_NOT_PLACEHOLDER).isNull();
    }

    @ParameterizedTest
    @ValueSource(strings = {"", " ", "   ", "\\t"})
    void aBlankRerankModelIsTreatedExactlyLikeAMissingOne(String blank) {
        var wiring = rerankClient();
        expectRerank(wiring, rerankResponse("langchain-rerank", List.of("P03", "P01"), blank));

        assertThat(rerank(wiring, candidates(3)).model())
                .as(WHY_NULL_NOT_PLACEHOLDER).isNull();
    }

    /**
     * {@code "model": null} 与整个键缺席同义。
     *
     * <p>本仓库的适配器以 {@code response_model_exclude_none=True} 序列化，永远不会发出显式
     * null；但契约是对所有实现开放的，第三方按 OpenAPI 生成的服务端很自然会把可选字段写成
     * 显式 null。它绝不能被读成字符串 "null" ——那会是一个凭空捏造、且看起来还挺像模型名的证据。
     */
    @Test
    void anExplicitJsonNullModelIsTreatedLikeAMissingOne() {
        var rewriteWiring = aiClient(true);
        expectRewrite(rewriteWiring.server(),
                rewriteResponseWithRawModel("4k television", "langchain", "null"));
        var rerankWiring = rerankClient();
        expectRerank(rerankWiring,
                rerankResponseWithRawModel("langchain-rerank", List.of("P03", "P01"), "null"));

        var rewritten = rewriteWiring.client().rewrite(options("smart tv", true));
        var reranked = rerank(rerankWiring, candidates(3));

        assertThat(rewritten.model()).as(WHY_NULL_NOT_PLACEHOLDER).isNull();
        assertThat(reranked.model()).as(WHY_NULL_NOT_PLACEHOLDER).isNull();
        assertThat(rewritten.status()).isEqualTo(AiRewriteStatus.APPLIED);
        assertThat(reranked.status()).isEqualTo(AiRerankStatus.APPLIED);
    }

    /**
     * model 是个对象或数组时同样按"没回报"处理，而不是把 JSON 片段当成模型名记下来。
     *
     * <p>形如 {@code "model":{"name":"…","version":"…"}} 的响应完全可能出现在某个自定义适配器上。
     * 把它 toString 成一段 JSON 塞进归档，会得到一个既不是模型名、又无法与真实模型名区分的字符串。
     */
    @Test
    void aStructuredModelValueIsTreatedLikeAMissingOne() {
        var wiring = aiClient(true);
        expectRewrite(wiring.server(), rewriteResponseWithRawModel("4k television", "langchain",
                "{\"name\":\"qwen\",\"version\":\"3.7\"}"));

        assertThat(wiring.client().rewrite(options("smart tv", true)).model())
                .as(WHY_NULL_NOT_PLACEHOLDER).isNull();
    }

    // ---------------------------------------------------------------------
    // 4. 降级路径下不虚报模型
    // ---------------------------------------------------------------------

    /**
     * 改写的每一种降级都必须 model=null。
     *
     * <p>第三个 case 是最尖锐的：响应体里**明明白白写着** model，但因为缺少 rewritten_query
     * 这次调用被判为 INVALID_RESPONSE，检索退回了纯 BM25。此时记下那个 model，
     * 等于让一次没被采纳的调用给一组 BM25 数字签名。
     */
    @ParameterizedTest(name = "{0}")
    @MethodSource("rewriteDegradations")
    void noRewriteFallbackEverReportsAModel(String scenario, ResponseCreator responder,
            AiRewriteStatus expected) {
        var wiring = aiClient(true);
        wiring.server().expect(requestTo(REWRITE_URL)).andRespond(responder);

        var result = wiring.client().rewrite(options("smart tv", true));

        assertThat(result.status()).isEqualTo(expected);
        assertThat(result.status().fallback()).isTrue();
        assertThat(result.model()).as(WHY_NO_MODEL_ON_FALLBACK).isNull();
        assertThat(result.provider()).isNull();
        assertThat(result.query()).isEqualTo("smart tv");
    }

    static Stream<Arguments> rewriteDegradations() {
        return Stream.of(
                arguments("读超时", withException(new SocketTimeoutException("Read timed out")),
                        AiRewriteStatus.TIMEOUT),
                arguments("适配器返回 5xx", withServerError(), AiRewriteStatus.TRANSPORT_ERROR),
                arguments("响应里带着 model 却没有 rewritten_query",
                        withSuccess("{\"provider\":\"langchain\",\"model\":\"" + REWRITE_MODEL + "\"}",
                                MediaType.APPLICATION_JSON),
                        AiRewriteStatus.INVALID_RESPONSE));
    }

    /**
     * 重排的每一种降级都必须 model=null，理由同上，而且更要紧：
     * 重排降级的外在表现就是"200 + BM25 顺序"，与"重排跑了但没收益"完全同形。
     */
    @ParameterizedTest(name = "{0}")
    @MethodSource("rerankDegradations")
    void noRerankFallbackEverReportsAModel(String scenario, ResponseCreator responder,
            AiRerankStatus expected) {
        var wiring = rerankClient();
        var candidates = candidates(5);
        wiring.rerankServer().expect(requestTo(RERANK_URL)).andRespond(responder);

        var result = rerank(wiring, candidates);

        assertThat(result.status()).isEqualTo(expected);
        assertThat(result.status().fallback()).isTrue();
        assertThat(result.model()).as(WHY_NO_MODEL_ON_FALLBACK).isNull();
        assertThat(result.provider()).isNull();
        assertThat(result.candidateCount()).isNull();
        assertThat(result.products()).containsExactlyElementsOf(candidates);
    }

    static Stream<Arguments> rerankDegradations() {
        return Stream.of(
                arguments("读超时", withException(new SocketTimeoutException("Read timed out")),
                        AiRerankStatus.TIMEOUT),
                arguments("适配器返回 5xx", withServerError(), AiRerankStatus.TRANSPORT_ERROR),
                arguments("响应里带着 model 却是空排名",
                        withSuccess("{\"ranked_product_ids\":[],\"provider\":\"langchain-rerank\","
                                + "\"model\":\"" + RERANK_MODEL + "\"}", MediaType.APPLICATION_JSON),
                        AiRerankStatus.INVALID_RESPONSE));
    }

    /**
     * 同一个客户端上，一次成功之后再失败，第二次绝不能继承第一次的模型标识。
     *
     * <p>这是"残留值"这个失败模式最直接的构造：只要有人把 model 存成客户端上的一个字段
     * （比如为了打点方便），这条就会红。一轮 600 条查询的评测里，前一条成功、后一条超时是常态，
     * 残留值会让整份产物声称每一条都是那个模型跑的。
     */
    @Test
    void aFailedRewriteNeverInheritsTheModelOfTheLastSuccessfulCall() {
        var wiring = aiClient(true);
        wiring.server().expect(requestTo(REWRITE_URL)).andRespond(withSuccess(
                rewriteResponse("4k television", "langchain", REWRITE_MODEL),
                MediaType.APPLICATION_JSON));
        wiring.server().expect(requestTo(REWRITE_URL)).andRespond(withServerError());

        var succeeded = wiring.client().rewrite(options("smart tv", true));
        var degraded = wiring.client().rewrite(options("smart tv", true));

        assertThat(succeeded.model()).isEqualTo(REWRITE_MODEL);
        assertThat(degraded.model()).as(WHY_NO_MODEL_ON_FALLBACK).isNull();
        assertThat(degraded.status()).isEqualTo(AiRewriteStatus.TRANSPORT_ERROR);
        wiring.server().verify();
    }

    /** 重排链路的同一条：成功一次之后超时，模型标识必须归零。 */
    @Test
    void aFailedRerankNeverInheritsTheModelOfTheLastSuccessfulCall() {
        var wiring = rerankClient();
        var candidates = candidates(5);
        wiring.rerankServer().expect(requestTo(RERANK_URL)).andRespond(withSuccess(
                rerankResponse("langchain-rerank", List.of("P03", "P01"), RERANK_MODEL),
                MediaType.APPLICATION_JSON));
        wiring.rerankServer().expect(requestTo(RERANK_URL))
                .andRespond(withException(new SocketTimeoutException("Read timed out")));

        var succeeded = rerank(wiring, candidates);
        var degraded = rerank(wiring, candidates);

        assertThat(succeeded.model()).isEqualTo(RERANK_MODEL);
        assertThat(degraded.model()).as(WHY_NO_MODEL_ON_FALLBACK).isNull();
        assertThat(degraded.status()).isEqualTo(AiRerankStatus.TIMEOUT);
        wiring.rerankServer().verify();
    }

    /** 压根没调用的那些分支（开关关着 / 没请求 / 不适用）同样不能有模型标识。 */
    @Test
    void callsThatNeverHappenedReportNoModel() {
        var disabled = aiClient(false);
        var enabled = aiClient(true);

        assertThat(disabled.client().rewrite(options("smart tv", true)).model())
                .as(WHY_NO_MODEL_ON_FALLBACK).isNull();
        assertThat(enabled.client().rewrite(options("smart tv", false)).model())
                .as(WHY_NO_MODEL_ON_FALLBACK).isNull();

        var rerankWiring = rerankClient();
        var notRequested = rerankWiring.client().rerank(
                rerankWiring.client().rerankPlan(rerankOptions(false)), "smart tv", candidates(3), "r");
        var secondPage = rerankWiring.client().rerank(
                rerankWiring.client().rerankPlan(
                        rerankOptions("smart tv", 1, 10, "relevance", true, null)),
                "smart tv", candidates(3), "r");

        assertThat(notRequested.status()).isEqualTo(AiRerankStatus.NOT_REQUESTED);
        assertThat(notRequested.model()).as(WHY_NO_MODEL_ON_FALLBACK).isNull();
        assertThat(secondPage.status()).isEqualTo(AiRerankStatus.NOT_APPLICABLE);
        assertThat(secondPage.model()).as(WHY_NO_MODEL_ON_FALLBACK).isNull();
        // 一次 HTTP 都没发：两台 server 都没登记期望，发了就会抛 AssertionError。
        disabled.server().verify();
        enabled.server().verify();
        rerankWiring.rerankServer().verify();
    }

    /**
     * 一半降级时，只有真正跑成的那条链路留下模型标识。
     *
     * <p>"改写成功 + 重排超时"是真实评测里最常见的混合形态。两个字段必须各说各话——
     * 若降级的一半也填上另一半的模型，产物会声称一组没跑过重排的数字是重排模型排的。
     */
    @Test
    void aPartiallyDegradedSearchOnlyReportsTheModelThatActuallyRan() {
        var wiring = rerankClient();
        wiring.rewriteServer().expect(requestTo(REWRITE_URL)).andRespond(withSuccess(
                rewriteResponse("4k television", "langchain", REWRITE_MODEL),
                MediaType.APPLICATION_JSON));
        wiring.rerankServer().expect(requestTo(RERANK_URL))
                .andRespond(withException(new SocketTimeoutException("Read timed out")));

        var response = searchService(wiring.client()).search(searchOptions(true, true));

        assertThat(response.aiModel()).isEqualTo(REWRITE_MODEL);
        assertThat(response.aiStatus()).isEqualTo(AiRewriteStatus.APPLIED);
        assertThat(response.rerankModel()).as(WHY_NO_MODEL_ON_FALLBACK).isNull();
        assertThat(response.rerankStatus()).isEqualTo(AiRerankStatus.TIMEOUT);
        assertThat(response.rerankProvider()).isNull();
    }

    // ---------------------------------------------------------------------
    // 5. EvaluationService：整轮观测的汇总
    // ---------------------------------------------------------------------

    /**
     * 整轮只观测到一个模型 → 产物里就是那个模型名。
     *
     * <p>这是最常见的正常情况，也是 experiments/ 下那些结论唯一站得住的形态：
     * 一份产物 + 一个模型名 = "这组数字是它跑的"。
     */
    @Test
    void aRunThatObservedOneModelReportsExactlyThatModel() {
        var result = evaluationService(observed(REWRITE_MODEL, RERANK_MODEL),
                observed(REWRITE_MODEL, RERANK_MODEL)).run(request(2, true));

        assertThat(result.aiModel()).isEqualTo(REWRITE_MODEL);
        assertThat(result.rerankModel()).isEqualTo(RERANK_MODEL);
        assertThat(result.queryCount()).isEqualTo(2);
    }

    /**
     * 同一个模型被观测 N 次只出现一次。
     *
     * <p>留出集一轮是 600 条查询；用 List 累加会得到一行 600 个逗号分隔的重复串——
     * 产物当场变得不可读，diff 也没法看。LinkedHashSet 的去重就是为了这个。
     */
    @Test
    void repeatedObservationsOfTheSameModelAppearExactlyOnce() {
        var result = evaluationService(observed(REWRITE_MODEL, RERANK_MODEL),
                observed(REWRITE_MODEL, RERANK_MODEL), observed(REWRITE_MODEL, RERANK_MODEL),
                observed(REWRITE_MODEL, RERANK_MODEL)).run(request(4, true));

        assertThat(result.aiModel()).isEqualTo(REWRITE_MODEL).doesNotContain(",");
        assertThat(result.rerankModel()).isEqualTo(RERANK_MODEL).doesNotContain(",");
    }

    /**
     * 中途换过模型 → 全部列出，按观测顺序。
     *
     * <p>适配器是个可以被重启/改配置的独立进程，一轮 600 条查询要跑几分钟。
     * 中途换了模型而产物只记第一个，正是"产物声称的模型不是跑出数字的模型"这个缺陷的另一种形态。
     * 这里刻意不替读者挑一个：把两个都写上，让产物自己说"这轮不干净"。
     */
    @Test
    void aModelSwitchMidRunListsEveryObservedModelInObservationOrder() {
        var result = evaluationService(
                observed("qwen3.7-flash-2026-07-15", "rerank-a"),
                observed("qwen3.7-flash-2026-07-15", "rerank-a"),
                observed("gpt-5.2-mini", "rerank-b")).run(request(3, true));

        assertThat(result.aiModel()).isEqualTo("qwen3.7-flash-2026-07-15,gpt-5.2-mini");
        assertThat(result.rerankModel()).isEqualTo("rerank-a,rerank-b");
        // 逗号是"这轮不干净"的信号，下游（人或脚本）靠它一眼看出不能直接引用这组数字。
        assertThat(result.aiModel()).contains(",");
    }

    /**
     * 全程降级一次都没跑成 → null，而不是任何占位值。
     *
     * <p>与 use_ai=true 并列时，这个 null（键整体消失）正是"要求了 AI 但一次都没跑成"的信号。
     * 这一组数字实际上就是纯 BM25，绝不能因为请求里写了 use_ai=true 就在产物上盖一个模型名。
     */
    @Test
    void aFullyDegradedRunReportsNoModelAtAll() {
        var result = evaluationService(observed(null, null), observed(null, null))
                .run(request(2, true));

        assertThat(result.aiModel()).as(WHY_NULL_NOT_PLACEHOLDER).isNull();
        assertThat(result.rerankModel()).as(WHY_NULL_NOT_PLACEHOLDER).isNull();
        // 请求参数照常回显：两者并列才说得清"要求了 AI，但一次都没跑成"。
        assertThat(result.useAi()).isTrue();
    }

    /**
     * 空白观测不是证据。
     *
     * <p>这条守的是 EvaluationService 自己那一层：即使上游（换了实现的 AiAdapterClient、
     * 或别的 SearchResponse 来源）递过来一个空串，汇总也不能把它变成产物里的一个值。
     * 手工构造的 SearchResponse 在这里是刻意的——它是这一层唯一的入参形态。
     */
    @Test
    void blankObservationsNeverBecomeEvidence() {
        var result = evaluationService(observed("", "   "), observed("  ", ""))
                .run(request(2, true));

        assertThat(result.aiModel()).as(WHY_NULL_NOT_PLACEHOLDER).isNull();
        assertThat(result.rerankModel()).as(WHY_NULL_NOT_PLACEHOLDER).isNull();
    }

    /** 只有一条链路跑成时，另一条不能借用它的模型名。 */
    @Test
    void aRunWhereOnlyTheRerankRanReportsOnlyTheRerankModel() {
        var result = evaluationService(observed(null, RERANK_MODEL), observed(null, RERANK_MODEL))
                .run(request(2, true));

        assertThat(result.aiModel()).as(WHY_NULL_NOT_PLACEHOLDER).isNull();
        assertThat(result.rerankModel()).isEqualTo(RERANK_MODEL);
    }

    /**
     * 汇总时两个模型不能互换。
     *
     * <p>与 {@link #theTwoModelsAreNeverCrossedInTheSearchResponse} 同样的理由，
     * 只是这次错位会直接写进归档文件——重排的收益会被记到改写模型头上。
     */
    @Test
    void theTwoModelsAreNeverCrossedInTheEvaluationResult() {
        var result = evaluationService(observed(REWRITE_MODEL, RERANK_MODEL))
                .run(request(1, true));

        assertThat(result.aiModel()).isEqualTo(REWRITE_MODEL).isNotEqualTo(RERANK_MODEL);
        assertThat(result.rerankModel()).isEqualTo(RERANK_MODEL).isNotEqualTo(REWRITE_MODEL);
    }

    /**
     * 无 AI 的基线跑观测不到任何模型。
     *
     * <p>baselines/evaluation-v7-bm25-baseline-20260802.json 与 experiments/ 下的 BM25 基线
     * 都是这条路径产出的；这两个键必须保持 null（进而在 JSON 里整体消失），
     * 既有归档才仍然可以逐字节 diff。形状那一层由 ModelIdentityWireContractTest 接着守。
     */
    @Test
    void anAiFreeBaselineRunObservesNoModel() {
        var result = evaluationService(observed(null, null), observed(null, null))
                .run(request(2, false));

        assertThat(result.useAi()).isFalse();
        assertThat(result.aiModel()).isNull();
        assertThat(result.rerankModel()).isNull();
    }

    // ---------------------------------------------------------------------
    // 装配
    // ---------------------------------------------------------------------

    /** 真实的 ProductSearchService + SearchQueryCompiler，只把 Elasticsearch 与 AI 打桩到 HTTP。 */
    private ProductSearchService searchService(AiAdapterClient ai) {
        when(strategies.current()).thenReturn(baselineStrategy());
        when(elasticsearch.search(any())).thenReturn(elasticsearchHits("P1", "P2"));
        return new ProductSearchService(elasticsearch, new SearchQueryCompiler(), strategies,
                analytics, ai);
    }

    /** 打桩 ProductSearchService，按顺序吐出每条查询观测到的响应。 */
    private EvaluationService evaluationService(SearchResponse first, SearchResponse... rest) {
        when(strategies.current()).thenReturn(baselineStrategy());
        when(search.search(any())).thenReturn(first, rest);
        return new EvaluationService(search, strategies, jdbc);
    }

    private RerankResult rerank(RerankTestSupport.Wiring wiring, List<Product> candidates) {
        var plan = wiring.client().rerankPlan(rerankOptions(true));
        return wiring.client().rerank(plan, "smart tv", candidates, "request-under-test");
    }

    private void expectRewrite(MockRestServiceServer server, String body) {
        server.expect(requestTo(REWRITE_URL)).andRespond(withSuccess(body, MediaType.APPLICATION_JSON));
    }

    private void expectRerank(RerankTestSupport.Wiring wiring, String body) {
        wiring.rerankServer().expect(requestTo(RERANK_URL))
                .andRespond(withSuccess(body, MediaType.APPLICATION_JSON));
    }

    /** 一次搜索请求，两条 AI 链路可分别开关。 */
    private static SearchOptions searchOptions(boolean useAi, boolean useRerank) {
        return new SearchOptions("smart tv", "en-US", 0, 10, null, null, null, null, null,
                "relevance", useAi, "request-under-test", false, useRerank, null);
    }

    /**
     * 一条评测查询观测到的搜索响应；两个 model 原样写进去（含 null 与空白）。
     *
     * <p>其余分量跟着 model 走成一个自洽的组合（有模型 == 调用成功），只有空白那一组是
     * 刻意的病态输入——它要证明 EvaluationService 自己就会把空白挡在产物之外。
     */
    private static SearchResponse observed(String aiModel, String rerankModel) {
        return new SearchResponse("evaluation-1", "smart tv", "smart tv", 2, 0, 10, 5,
                AiTestSupport.BASELINE_STRATEGY_VERSION,
                List.of(RerankTestSupport.product("P1"), RerankTestSupport.product("P2")), Map.of(),
                aiModel != null, aiModel == null ? AiRewriteStatus.TIMEOUT : AiRewriteStatus.APPLIED,
                aiModel == null ? null : "langchain", aiModel,
                rerankModel != null,
                rerankModel == null ? AiRerankStatus.TIMEOUT : AiRerankStatus.APPLIED,
                rerankModel == null ? null : "langchain-rerank", rerankModel,
                rerankModel == null ? null : 50, "notice");
    }

    private static EvaluationRequest request(int queryCount, boolean useAi) {
        var queries = IntStream.rangeClosed(1, queryCount)
                .mapToObj(id -> new EvaluationQuery(id, "query " + id, Map.of("P1", "E", "P2", "S")))
                .toList();
        return new EvaluationRequest(queries, 10, false, useAi);
    }
}
