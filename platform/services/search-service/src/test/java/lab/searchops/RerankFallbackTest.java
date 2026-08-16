package lab.searchops;

import static lab.searchops.RerankTestSupport.RERANK_URL;
import static lab.searchops.RerankTestSupport.candidates;
import static lab.searchops.RerankTestSupport.ids;
import static lab.searchops.RerankTestSupport.product;
import static lab.searchops.RerankTestSupport.productIds;
import static lab.searchops.RerankTestSupport.rerankClient;
import static lab.searchops.RerankTestSupport.rerankOptions;
import static lab.searchops.RerankTestSupport.rerankProperties;
import static lab.searchops.RerankTestSupport.rerankResponse;
import static lab.searchops.RerankTestSupport.reversed;
import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.params.provider.Arguments.arguments;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.content;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.header;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.jsonPath;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.method;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.requestTo;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withException;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withServerError;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withStatus;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withSuccess;

import java.io.IOException;
import java.net.ConnectException;
import java.net.SocketTimeoutException;
import java.net.UnknownHostException;
import java.net.http.HttpConnectTimeoutException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.atomic.AtomicReference;
import java.util.stream.Stream;
import lab.searchops.config.RerankProperties;
import lab.searchops.domain.AiRerankStatus;
import lab.searchops.domain.ApiModels.Product;
import lab.searchops.domain.ApiModels.SearchOptions;
import lab.searchops.service.AiAdapterClient.RerankResult;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;

/**
 * AiAdapterClient 重排链路的降级契约测试。
 *
 * <p>与 {@link RerankOrderingTest} 的分工：那边守的是纯函数的排列不变量，这边守的是
 * "不变量在真实调用链上依然成立"，以及每一种降级都<b>可区分、不改变文档集合、请求照常成功</b>。
 *
 * <p>为什么降级必须能分辨：重排失败的外在表现就是"返回 200 + BM25 顺序"，与
 * "重排跑了但没收益"完全同形。如果 TIMEOUT / TRANSPORT_ERROR / INVALID_RESPONSE /
 * OVERLOADED / INTERNAL_ERROR 塌缩成同一个 false，留出集上 +0.0968 的 ΔNDCG@10 到底是
 * 模型的功劳还是"这一组根本没跑起来"，事后无从对证。
 */
class RerankFallbackTest {

    // ---------------------------------------------------------------------
    // 1. 不变量：无论适配器返回什么，文档集合都不变
    // ---------------------------------------------------------------------

    /**
     * 恶意/畸形排名在整条调用链上依然只改顺序、不改集合。
     *
     * <p>这些 case 与 RerankOrderingTest 里的同名场景一一对应，区别是它们这次真的经过了
     * HTTP 编解码、JSON 解析与 {@code rankedIds()} 的过滤，覆盖的是"响应体是合法 JSON
     * 但内容是垃圾"这一类——它不会降级，会被当成一次成功的重排照单全收。
     */
    @ParameterizedTest(name = "{0}")
    @MethodSource("maliciousRankings")
    void maliciousRankingsNeverChangeTheDocumentSet(String scenario, List<String> rankedIds) {
        var wiring = rerankClient();
        var candidates = candidates(8);
        expectRerank(wiring, rerankResponse("mock", rankedIds));

        var result = rerank(wiring, candidates);

        assertThat(result.products()).hasSameSizeAs(candidates)
                .containsExactlyInAnyOrderElementsOf(candidates);
        assertThat(ids(result.products())).doesNotHaveDuplicates();
        assertThat(result.status()).isIn(AiRerankStatus.APPLIED, AiRerankStatus.NO_CHANGE);
        assertThat(result.status().failure()).isFalse();
        assertThat(result.provider()).isEqualTo("mock");
        assertThat(result.candidateCount()).isEqualTo(8);
        wiring.rerankServer().verify();
    }

    static Stream<Arguments> maliciousRankings() {
        var all = productIds(8);
        var oversized = new ArrayList<String>();
        oversized.addAll(all);
        oversized.addAll(all);
        oversized.addAll(List.of("ghost-1", "ghost-2", "ghost-3", "ghost-4"));
        return Stream.of(
                arguments("只返回部分 id", List.of("P05", "P01", "P03")),
                arguments("只返回一个 id", List.of("P08")),
                arguments("返回候选集外的 id", List.of("ghost-1", "P04", "P99")),
                arguments("返回的 id 全部在候选集外", List.of("x", "y", "z")),
                arguments("重复返回同一个 id", List.of("P02", "P02", "P02", "P02")),
                arguments("顺序完全颠倒", reversed(all)),
                arguments("返回数量超过候选数（20 条对 8 个候选）", List.copyOf(oversized)),
                arguments("id 被改坏了大小写与空白", List.of(" P01", "p02", "P03 ")));
    }

    /** 未被点名的候选按原 BM25 序补齐——模型只点名前 3 条是最常见的真实形态。 */
    @Test
    void candidatesTheModelDidNotMentionAreAppendedInBm25Order() {
        var wiring = rerankClient();
        var candidates = candidates(8);
        expectRerank(wiring, rerankResponse("mock", List.of("P05", "P08", "P02")));

        var result = rerank(wiring, candidates);

        assertThat(ids(result.products()))
                .containsExactly("P05", "P08", "P02", "P01", "P03", "P04", "P06", "P07");
        assertThat(result.status()).isEqualTo(AiRerankStatus.APPLIED);
    }

    /**
     * 因为不满足适配器契约而没进请求体的候选，依然要出现在结果里。
     *
     * <p>candidatePayload 会跳过 id 为空、或 id 超过 128 字符的候选（截断 id 会破坏身份）。
     * 跳过的只是"这条不参与排名"，绝不是"这条可以消失"。
     */
    @Test
    void candidatesSkippedFromTheRequestPayloadAreStillReturned() {
        var wiring = rerankClient();
        var candidates = List.of(product("P01"), product("", "blank id"),
                product("x".repeat(129), "oversized id"), product("P02"));
        expectRerank(wiring, rerankResponse("mock", List.of("P02", "P01")));

        var result = rerank(wiring, candidates);

        assertThat(result.candidateCount()).isEqualTo(2);
        assertThat(result.products()).hasSize(4).containsExactlyInAnyOrderElementsOf(candidates);
        assertThat(ids(result.products())).containsExactly("P02", "P01", "", "x".repeat(129));
    }

    // ---------------------------------------------------------------------
    // 2. NO_CHANGE 的判定口径是"实际返回的那一页"
    // ---------------------------------------------------------------------

    /**
     * 只发生在第 30-50 位的置换必须记 NO_CHANGE，不能记 APPLIED。
     *
     * <p>把窗口外的置换算成 APPLIED 会重演旧 {@code ai_applied} 那种"覆盖率虚高、
     * 实际没影响"的读数：报表上重排覆盖 100%，NDCG@10 却纹丝不动，两边谁都解释不了。
     * 同时断言重排**确实**发生了（第 30/50 位真的换了），证明 NO_CHANGE 说的是
     * "用户看不见"，不是"没跑"。
     */
    @Test
    void permutationOutsideTheReturnedPageIsReportedAsNoChange() {
        var wiring = rerankClient();
        var candidates = candidates(50);
        var ranked = new ArrayList<>(productIds(50));
        java.util.Collections.swap(ranked, 29, 49);   // 第 30 位与第 50 位对调
        expectRerank(wiring, rerankResponse("mock", List.copyOf(ranked)));

        var result = rerank(wiring, candidates, rerankOptions("smart tv", 0, 10, "relevance",
                true, 50));

        assertThat(result.status()).isEqualTo(AiRerankStatus.NO_CHANGE);
        assertThat(result.applied()).isFalse();
        assertThat(result.status().fallback()).isFalse();       // 不是降级，只是不可见
        assertThat(ids(result.products()).subList(0, 10)).isEqualTo(productIds(10));
        assertThat(ids(result.products()).get(29)).isEqualTo("P50");
        assertThat(ids(result.products()).get(49)).isEqualTo("P30");
    }

    /** 同样一次置换，只要落进本页就必须是 APPLIED。 */
    @Test
    void permutationInsideTheReturnedPageIsReportedAsApplied() {
        var wiring = rerankClient();
        var candidates = candidates(50);
        var ranked = new ArrayList<>(productIds(50));
        java.util.Collections.swap(ranked, 8, 9);     // 第 9 位与第 10 位对调，仍在 top-10 内
        expectRerank(wiring, rerankResponse("mock", List.copyOf(ranked)));

        var result = rerank(wiring, candidates, rerankOptions("smart tv", 0, 10, "relevance",
                true, 50));

        assertThat(result.status()).isEqualTo(AiRerankStatus.APPLIED);
        assertThat(result.applied()).isTrue();
    }

    // ---------------------------------------------------------------------
    // 3. 降级分支：各自可达、可区分、都保持 BM25 顺序
    // ---------------------------------------------------------------------

    /** 读超时。重排提示词含 N 条商品，延迟远高于改写，这一类必须能单独统计。 */
    @Test
    void readTimeoutDegradesToTimeout() {
        assertTransportFallback(new SocketTimeoutException("Read timed out"),
                AiRerankStatus.TIMEOUT);
    }

    /** 连接超时走的是另一条异常链，同样落到 TIMEOUT。 */
    @Test
    void connectTimeoutDegradesToTimeout() {
        assertTransportFallback(new HttpConnectTimeoutException("connect timed out"),
                AiRerankStatus.TIMEOUT);
    }

    /** 适配器没起来 / 端口不通：传输故障，不是超时。 */
    @Test
    void connectionRefusedDegradesToTransportError() {
        assertTransportFallback(new ConnectException("Connection refused"),
                AiRerankStatus.TRANSPORT_ERROR);
    }

    /** DNS 解析失败同样是传输故障。 */
    @Test
    void unknownHostDegradesToTransportError() {
        assertTransportFallback(new UnknownHostException("ai-adapter.test"),
                AiRerankStatus.TRANSPORT_ERROR);
    }

    /** 适配器活着但返回 5xx。 */
    @Test
    void serverErrorDegradesToTransportError() {
        var wiring = rerankClient();
        var candidates = candidates(5);
        wiring.rerankServer().expect(requestTo(RERANK_URL)).andRespond(withServerError());

        assertDegradedToBm25(rerank(wiring, candidates), candidates,
                AiRerankStatus.TRANSPORT_ERROR);
    }

    /**
     * 契约被违反导致的 422 也是传输层可见的状态码，不是 INVALID_RESPONSE。
     *
     * <p>两者的排查方向不同：4xx 说明"我们发出去的请求不合契约"（改 Java 侧的裁剪），
     * INVALID_RESPONSE 说明"适配器回给我们的东西不合契约"（改适配器或换模型）。
     */
    @Test
    void unprocessableEntityDegradesToTransportError() {
        var wiring = rerankClient();
        var candidates = candidates(5);
        wiring.rerankServer().expect(requestTo(RERANK_URL))
                .andRespond(withStatus(HttpStatus.UNPROCESSABLE_ENTITY));

        assertDegradedToBm25(rerank(wiring, candidates), candidates,
                AiRerankStatus.TRANSPORT_ERROR);
    }

    /**
     * 适配器可达但响应不可用：四种典型形态都必须落到 INVALID_RESPONSE，
     * 且候选集原样返回。
     *
     * <p>注意"模型返回空列表"在这里：空排名不是"没变化"，而是响应不可用——
     * 合法的重排至少要点名一个候选。把它记成 NO_CHANGE 会让"模型每次都空手而归"
     * 伪装成"模型认为 BM25 已经最优"。
     */
    @ParameterizedTest(name = "{0}")
    @MethodSource("unusableResponses")
    void unusableResponsesDegradeToInvalidResponse(String scenario, String body) {
        var wiring = rerankClient();
        var candidates = candidates(5);
        expectRerank(wiring, body);

        assertDegradedToBm25(rerank(wiring, candidates), candidates,
                AiRerankStatus.INVALID_RESPONSE);
    }

    static Stream<Arguments> unusableResponses() {
        return Stream.of(
                arguments("模型返回空列表", "{\"ranked_product_ids\":[],\"provider\":\"mock\"}"),
                arguments("缺少 ranked_product_ids", "{\"provider\":\"mock\",\"latency_ms\":42}"),
                arguments("ranked_product_ids 不是数组",
                        "{\"ranked_product_ids\":\"P01\",\"provider\":\"mock\"}"),
                arguments("ranked_product_ids 里没有字符串",
                        "{\"ranked_product_ids\":[1,2,3],\"provider\":\"mock\"}"),
                arguments("响应体不是合法 JSON", "this is not json at all"),
                arguments("响应体为空", ""));
    }

    /**
     * 并发预算耗尽：在发出 HTTP 之前就降级成 OVERLOADED。
     *
     * <p>用重入而不是多线程来制造预算耗尽：适配器的响应回调里再调一次 rerank，此时外层
     * 仍持有唯一的信号量许可。这样整条断言是确定性的，不依赖任何 sleep 或线程调度。
     *
     * <p>为什么这条闸门重要：重排同步阻塞在 Tomcat 工作线程上，没有它一波重排流量就能
     * 占满线程池，把不用 AI 的普通搜索一起拖死。OVERLOADED 与 TIMEOUT 必须分开——
     * 前者要加容量或调预算，后者要看模型延迟。
     */
    @Test
    void exhaustedConcurrencyBudgetDegradesBeforeAnyHttpCall() {
        var wiring = rerankClient(true, rerankProperties(true, 50, 1));
        var candidates = candidates(5);
        var plan = wiring.client().rerankPlan(rerankOptions(true));
        var nested = new AtomicReference<RerankResult>();

        wiring.rerankServer().expect(requestTo(RERANK_URL)).andRespond(request -> {
            // 外层此刻正持有唯一许可，这次重入必须在 tryAcquire 处被拦下，一个字节都不发。
            nested.set(wiring.client().rerank(plan, "smart tv", candidates, "nested-request"));
            return withSuccess(rerankResponse("mock", List.of("P03", "P01")),
                    MediaType.APPLICATION_JSON).createResponse(request);
        });

        var outer = wiring.client().rerank(plan, "smart tv", candidates, "outer-request");

        assertDegradedToBm25(nested.get(), candidates, AiRerankStatus.OVERLOADED);
        assertThat(nested.get().status().called())
                .describedAs("OVERLOADED 是发出请求之前就被拦下的").isFalse();
        assertThat(outer.status()).isEqualTo(AiRerankStatus.APPLIED);
        // 只登记了一条期望：重入那次若真的发了 HTTP，这里会因为"多余的请求"而失败。
        wiring.rerankServer().verify();
    }

    /**
     * 排列不变量被破坏时记 INTERNAL_ERROR，而不是 INVALID_RESPONSE。
     *
     * <p>这是两个状态必须分开的全部理由：INTERNAL_ERROR 说"这是我们自己的 bug，去看
     * RerankOrdering"，INVALID_RESPONSE 说"模型返回非法，去看适配器"。混淆会把排查
     * 引到完全错误的方向——而这条路径正常情况下不可达，一旦出现就是最需要指对方向的时候。
     *
     * <p>制造手法见 {@link RerankTestSupport.ShrinkingCandidateList}：适配器这次返回的是
     * 完全合法的响应（所以绝不该是 INVALID_RESPONSE），破坏来自候选集自身在遍历途中变形。
     */
    @Test
    void brokenPermutationInvariantIsReportedAsInternalErrorNotInvalidResponse() {
        var wiring = rerankClient();
        // shrinkOnRead=3：构造请求体一趟 + reorder 内部两趟，第三趟读完末位后收缩。
        var hostile = new RerankTestSupport.ShrinkingCandidateList(candidates(4), 3);
        expectRerank(wiring, rerankResponse("mock", List.of("P01")));

        var result = rerank(wiring, hostile);

        assertThat(hostile.shrunk())
                .describedAs("这个用例必须真的破坏不变量，否则它什么都没测到").isTrue();
        assertThat(result.status()).isEqualTo(AiRerankStatus.INTERNAL_ERROR)
                .isNotEqualTo(AiRerankStatus.INVALID_RESPONSE);
        assertThat(result.status().failure()).isTrue();
        assertThat(result.status().fallback()).isTrue();
        assertThat(result.provider()).isNull();
        assertThat(result.candidateCount()).isNull();
        // 结果被强制退回入参本身：宁可拿到没优化过的顺序，也不能拿到一个丢了文档的顺序。
        assertThat(result.products()).isSameAs(hostile);
        wiring.rerankServer().verify();
    }

    /**
     * 候选集里出现 null 元素时记 INTERNAL_ERROR，而不是伪装成适配器的错。
     *
     * <p>候选集由本服务从 Elasticsearch 结果装配，出现 null 只可能是上游装配缺陷。
     * 它会在 {@code Product::productId} 处抛 NPE；若落进通用的 RuntimeException 分支，
     * 就会被 failureKind 归类成 INVALID_RESPONSE 或 TRANSPORT_ERROR——把一次本地缺陷
     * 记成"模型返回非法"，正是 INTERNAL_ERROR 这个状态存在的意义所要避免的。
     *
     * <p>本用例里适配器返回的是完全合法的响应，所以任何"怪适配器"的归类都是错的。
     */
    @Test
    void nullCandidateIsOurDefectNotTheAdaptersFault() {
        var wiring = rerankClient();
        var hostile = new java.util.ArrayList<>(candidates(3));
        hostile.add(null);
        expectRerank(wiring, rerankResponse("mock", List.of("P01")));

        var result = rerank(wiring, hostile);

        assertThat(result.status()).isEqualTo(AiRerankStatus.INTERNAL_ERROR)
                .isNotEqualTo(AiRerankStatus.INVALID_RESPONSE)
                .isNotEqualTo(AiRerankStatus.TRANSPORT_ERROR);
        assertThat(result.status().fallback()).isTrue();
        assertThat(result.products()).isSameAs(hostile);
    }

    /** 五种降级原因两两互不相同——塌缩成同一个值就等于没有可观测性。 */
    @Test
    void everyDegradationReasonIsDistinguishable() {
        assertThat(List.of(AiRerankStatus.OVERLOADED, AiRerankStatus.TIMEOUT,
                        AiRerankStatus.TRANSPORT_ERROR, AiRerankStatus.INVALID_RESPONSE,
                        AiRerankStatus.INTERNAL_ERROR))
                .doesNotHaveDuplicates()
                .allMatch(AiRerankStatus::fallback);
        // OVERLOADED 不是"故障"（是主动限流），其余四个是。
        assertThat(AiRerankStatus.OVERLOADED.failure()).isFalse();
        assertThat(AiRerankStatus.TIMEOUT.failure()).isTrue();
        assertThat(AiRerankStatus.TRANSPORT_ERROR.failure()).isTrue();
        assertThat(AiRerankStatus.INVALID_RESPONSE.failure()).isTrue();
        assertThat(AiRerankStatus.INTERNAL_ERROR.failure()).isTrue();
    }

    // ---------------------------------------------------------------------
    // 4. 计划阶段：哪些请求根本不调用重排
    // ---------------------------------------------------------------------

    /**
     * 五种"不调用"的原因各自可达且互不相同，且一次 HTTP 都不发。
     *
     * <p>两台 MockRestServiceServer 都没登记任何期望，真的发出请求就会抛 AssertionError
     * （Error 不会被 rerank() 里的 catch RuntimeException 吞掉），测试立刻变红。
     */
    @ParameterizedTest(name = "{0}")
    @MethodSource("plansThatSkipTheCall")
    void requestsThatShouldNotBeRerankedNeverCallTheAdapter(String scenario, boolean aiEnabled,
            RerankProperties properties, SearchOptions options, AiRerankStatus expected) {
        var wiring = rerankClient(aiEnabled, properties);
        var candidates = candidates(5);

        var plan = wiring.client().rerankPlan(options);
        var result = wiring.client().rerank(plan, "smart tv", candidates, "request-under-test");

        assertThat(plan.active()).isFalse();
        assertThat(plan.depth()).isEqualTo(options.size());   // 不调用 ⇒ 检索深度保持原样
        assertThat(plan.skipStatus()).isEqualTo(expected);
        assertDegradedToBm25(result, candidates, expected);
        assertThat(expected.failure()).isFalse();             // "没打算调用"不是故障
        wiring.rerankServer().verify();
        wiring.rewriteServer().verify();
    }

    static Stream<Arguments> plansThatSkipTheCall() {
        var enabled = RerankProperties.defaults();
        return Stream.of(
                arguments("全局 AI 开关关着", false, enabled, rerankOptions(true),
                        AiRerankStatus.AI_DISABLED),
                arguments("重排 kill switch 关着", true, rerankProperties(false, 50, 8),
                        rerankOptions(true), AiRerankStatus.DISABLED),
                arguments("请求没有要求重排", true, enabled, rerankOptions(false),
                        AiRerankStatus.NOT_REQUESTED),
                arguments("第二页起不重排", true, enabled,
                        rerankOptions("smart tv", 1, 10, "relevance", true, null),
                        AiRerankStatus.NOT_APPLICABLE),
                arguments("用户显式按价格排序", true, enabled,
                        rerankOptions("smart tv", 0, 10, "price_asc", true, null),
                        AiRerankStatus.NOT_APPLICABLE));
    }

    /** 全局开关压过重排开关：两个都关时报 AI_DISABLED，运维一眼看出该开哪个。 */
    @Test
    void theGlobalKillSwitchWinsOverTheRerankKillSwitch() {
        var wiring = rerankClient(false, rerankProperties(false, 50, 8));

        assertThat(wiring.client().rerankPlan(rerankOptions(true)).skipStatus())
                .isEqualTo(AiRerankStatus.AI_DISABLED)
                .isNotEqualTo(AiRerankStatus.DISABLED);
    }

    /** 空查询、空候选集：不满足适配器契约，如实记成"不适用"而不是伪造一个传输故障。 */
    @Test
    void blankQueryOrEmptyCandidateSetIsNotApplicableRatherThanAFailure() {
        var wiring = rerankClient();
        var plan = wiring.client().rerankPlan(rerankOptions(true));

        var blankQuery = wiring.client().rerank(plan, "   ", candidates(5), "request-under-test");
        var noCandidates = wiring.client().rerank(plan, "smart tv", List.of(), "request-under-test");
        var nullCandidates = wiring.client().rerank(plan, "smart tv", null, "request-under-test");

        assertThat(blankQuery.status()).isEqualTo(AiRerankStatus.NOT_APPLICABLE);
        assertThat(noCandidates.status()).isEqualTo(AiRerankStatus.NOT_APPLICABLE);
        assertThat(nullCandidates.status()).isEqualTo(AiRerankStatus.NOT_APPLICABLE);
        assertThat(blankQuery.status().failure()).isFalse();
        assertThat(nullCandidates.products()).isEmpty();
        wiring.rerankServer().verify();
    }

    /** 候选集全是没有可用 id 的文档时同样不调用，但文档一条不少地原样返回。 */
    @Test
    void candidateSetWithoutAnyUsableIdIsNotApplicableAndLosesNothing() {
        var wiring = rerankClient();
        var candidates = List.of(product("", "blank"), product(null, "null id"));
        var plan = wiring.client().rerankPlan(rerankOptions(true));

        var result = wiring.client().rerank(plan, "smart tv", candidates, "request-under-test");

        assertDegradedToBm25(result, candidates, AiRerankStatus.NOT_APPLICABLE);
        wiring.rerankServer().verify();
    }

    /** null plan 走最保守的分支：当成"没请求过重排"，绝不调用。 */
    @Test
    void nullPlanIsTreatedAsNotRequested() {
        var wiring = rerankClient();
        var candidates = candidates(3);

        assertDegradedToBm25(wiring.client().rerank(null, "smart tv", candidates, "r"),
                candidates, AiRerankStatus.NOT_REQUESTED);
        wiring.rerankServer().verify();
    }

    /** 候选深度：请求覆盖生效、缺省用服务端默认、永不低于本页条数、永不超过契约上限。 */
    @Test
    void candidateDepthIsPlannedTogetherWithTheDecisionToCall() {
        var wiring = rerankClient();

        assertThat(wiring.client().rerankPlan(rerankOptions(true)).depth())
                .isEqualTo(RerankProperties.DEFAULT_DEPTH);
        assertThat(wiring.client()
                .rerankPlan(rerankOptions("smart tv", 0, 10, "relevance", true, 20)).depth())
                .isEqualTo(20);
        assertThat(wiring.client()
                .rerankPlan(rerankOptions("smart tv", 0, 30, "relevance", true, 5)).depth())
                .isEqualTo(30);   // 候选比本页还浅就不叫重排了
        assertThat(wiring.client()
                .rerankPlan(rerankOptions("smart tv", 0, 10, "relevance", true, 500)).depth())
                .isEqualTo(RerankProperties.MAX_DEPTH);
        assertThat(wiring.client().rerankPlan(rerankOptions(true)).active()).isTrue();
        assertThat(wiring.client().rerankPlan(rerankOptions(true)).skipStatus()).isNull();
    }

    // ---------------------------------------------------------------------
    // 5. 出站请求形状与 provider 回报
    // ---------------------------------------------------------------------

    /** 出站请求的形状即 ai-adapter 的 RerankRequest 契约；改错了整条链路会稳定 422。 */
    @Test
    void sendsTheContractedRerankRequest() {
        var wiring = rerankClient();
        wiring.rerankServer().expect(requestTo(RERANK_URL))
                .andExpect(method(HttpMethod.POST))
                .andExpect(header("X-Request-ID", "request-under-test"))
                .andExpect(content().contentTypeCompatibleWith(MediaType.APPLICATION_JSON))
                .andExpect(jsonPath("$.query").value("smart tv"))
                .andExpect(jsonPath("$.request_id").value("request-under-test"))
                .andExpect(jsonPath("$.candidates.length()").value(3))
                .andExpect(jsonPath("$.candidates[0].product_id").value("P01"))
                .andExpect(jsonPath("$.candidates[0].bm25_score").value(1.5))
                .andRespond(withSuccess(rerankResponse("mock", List.of("P02")),
                        MediaType.APPLICATION_JSON));

        rerank(wiring, candidates(3));

        wiring.rerankServer().verify();
    }

    /** provider 是契约必填项但不影响排序结果；缺失时只降级成占位名，不能拖垮整次重排。 */
    @Test
    void missingProviderNameStillYieldsAUsableRerank() {
        var wiring = rerankClient();
        expectRerank(wiring, rerankResponse(null, List.of("P03", "P01")));

        var result = rerank(wiring, candidates(3));

        assertThat(result.provider()).isEqualTo("unknown");
        assertThat(result.status()).isEqualTo(AiRerankStatus.APPLIED);
    }

    /** 换任何真实 provider 都不需要改 Java：provider 名只做记录，不作为准入条件。 */
    @ParameterizedTest
    @ValueSource(strings = {"mock", "langchain", "openai", "bedrock-claude", "some-future-provider"})
    void acceptsReranksFromAnyProviderName(String provider) {
        var wiring = rerankClient();
        expectRerank(wiring, rerankResponse(provider, List.of("P03", "P01", "P02")));

        var result = rerank(wiring, candidates(3));

        assertThat(result.provider()).isEqualTo(provider);
        assertThat(ids(result.products())).containsExactly("P03", "P01", "P02");
    }

    /** 枚举名会直接出现在 API 响应里（Jackson 按 name() 序列化），改名即破坏前端契约。 */
    @Test
    void statusWireNamesAreStable() {
        assertThat(AiRerankStatus.values()).extracting(Enum::name)
                .containsExactly("APPLIED", "NO_CHANGE", "NOT_REQUESTED", "DISABLED",
                        "AI_DISABLED", "NOT_APPLICABLE", "OVERLOADED", "TIMEOUT",
                        "TRANSPORT_ERROR", "INVALID_RESPONSE", "INTERNAL_ERROR");
    }

    /**
     * fallback() / failure() / called() 的真值表。
     *
     * <p>告警挂在 failure() 上：把 NOT_APPLICABLE 或 OVERLOADED 算成故障会造成长期误报，
     * 把 TIMEOUT 算成正常则会把真故障藏起来。called() 用来算真实调用量，
     * OVERLOADED 必须不计入——它是在发出请求之前被拦下的。
     */
    @Test
    void statusHelpersSeparateFallbackFromFailureFromCalled() {
        assertThat(Arrays.stream(AiRerankStatus.values()).filter(status -> !status.fallback())
                .toList()).containsExactly(AiRerankStatus.APPLIED, AiRerankStatus.NO_CHANGE);
        assertThat(Arrays.stream(AiRerankStatus.values()).filter(AiRerankStatus::failure).toList())
                .containsExactly(AiRerankStatus.TIMEOUT, AiRerankStatus.TRANSPORT_ERROR,
                        AiRerankStatus.INVALID_RESPONSE, AiRerankStatus.INTERNAL_ERROR);
        assertThat(Arrays.stream(AiRerankStatus.values()).filter(AiRerankStatus::called).toList())
                .containsExactly(AiRerankStatus.APPLIED, AiRerankStatus.NO_CHANGE,
                        AiRerankStatus.TIMEOUT, AiRerankStatus.TRANSPORT_ERROR,
                        AiRerankStatus.INVALID_RESPONSE, AiRerankStatus.INTERNAL_ERROR);
    }

    // ---------------------------------------------------------------------
    // helpers
    // ---------------------------------------------------------------------

    private RerankResult rerank(RerankTestSupport.Wiring wiring, List<Product> candidates) {
        return rerank(wiring, candidates, rerankOptions(true));
    }

    private RerankResult rerank(RerankTestSupport.Wiring wiring, List<Product> candidates,
            SearchOptions options) {
        var plan = wiring.client().rerankPlan(options);
        return wiring.client().rerank(plan, "smart tv", candidates, "request-under-test");
    }

    private void expectRerank(RerankTestSupport.Wiring wiring, String body) {
        wiring.rerankServer().expect(requestTo(RERANK_URL))
                .andRespond(withSuccess(body, MediaType.APPLICATION_JSON));
    }

    private void assertTransportFallback(IOException failure, AiRerankStatus expected) {
        var wiring = rerankClient();
        var candidates = candidates(5);
        // RestClient 把底层 IOException 包成 ResourceAccessException，真正的类型藏在
        // cause 链里；failureKind() 必须逐层查看才认得出超时。
        wiring.rerankServer().expect(requestTo(RERANK_URL)).andRespond(withException(failure));

        assertDegradedToBm25(rerank(wiring, candidates), candidates, expected);
    }

    /** 降级的完整契约：状态说得出原因，顺序退回 BM25，文档一条不少，provider 与候选数不虚报。 */
    private void assertDegradedToBm25(RerankResult result, List<Product> candidates,
            AiRerankStatus expected) {
        assertThat(result.status()).isEqualTo(expected);
        assertThat(result.applied()).isFalse();
        assertThat(result.status().fallback()).isTrue();
        assertThat(result.provider()).isNull();
        assertThat(result.candidateCount()).isNull();
        assertThat(result.products()).containsExactlyElementsOf(candidates);
    }
}
