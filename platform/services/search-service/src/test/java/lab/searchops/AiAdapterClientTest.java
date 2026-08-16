package lab.searchops;

import static lab.searchops.AiTestSupport.REWRITE_URL;
import static lab.searchops.AiTestSupport.aiClient;
import static lab.searchops.AiTestSupport.options;
import static lab.searchops.AiTestSupport.rewriteResponse;
import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.content;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.header;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.method;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.requestTo;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withException;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withServerError;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withSuccess;

import java.io.IOException;
import java.net.ConnectException;
import java.net.SocketTimeoutException;
import java.net.http.HttpConnectTimeoutException;
import lab.searchops.domain.AiRewriteStatus;
import lab.searchops.service.AiAdapterClient;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.test.web.client.MockRestServiceServer;

/**
 * AiAdapterClient 的降级契约测试。
 *
 * <p>这个类保护的核心承诺是 docs/ai-handoff.md 里那句"替换成真实 AI 服务时不需要改
 * 商城、搜索后台和前端代码"，以及"AI 出问题时静默降级要能被区分出来"。
 */
class AiAdapterClientTest {
    /**
     * 最重要的一条回归。
     *
     * <p>修复前 AiAdapterClient 里写着 {@code "mock".equals(provider)}，任何真实 provider
     * 的响应都会抛 IllegalStateException，再被同一个方法里的 catch 静默降级成纯 BM25：
     * 请求返回 200、ai_applied=false、日志之外没有任何痕迹，看起来就像"AI 没效果"。
     * 只要有人再把 provider 名当准入条件，这条参数化测试就会红。
     */
    @ParameterizedTest
    @ValueSource(strings = {"mock", "langchain", "openai", "bedrock-claude", "some-future-provider"})
    void acceptsRewritesFromAnyProviderName(String provider) {
        var wiring = aiClient(true);
        expectRewrite(wiring.server(), rewriteResponse("4k television", provider));

        var result = wiring.client().rewrite(options("smart tv", true));

        assertThat(result.status()).isEqualTo(AiRewriteStatus.APPLIED);
        assertThat(result.applied()).isTrue();
        assertThat(result.provider()).isEqualTo(provider);
        assertThat(result.query()).isEqualTo("4k television");
        wiring.server().verify();
    }

    /** 出站请求的形状即 ai-adapter 契约；改错了会让上面那条"provider 可插拔"变成空头支票。 */
    @Test
    void sendsTheContractedRewriteRequest() {
        var wiring = aiClient(true);
        wiring.server().expect(requestTo(REWRITE_URL))
                .andExpect(method(HttpMethod.POST))
                .andExpect(header("X-Request-ID", "request-under-test"))
                .andExpect(content().contentTypeCompatibleWith(MediaType.APPLICATION_JSON))
                .andRespond(withSuccess(rewriteResponse("4k television", "langchain"),
                        MediaType.APPLICATION_JSON));

        wiring.client().rewrite(options("smart tv", true));

        wiring.server().verify();
    }

    /**
     * ai_applied 的语义回归。
     *
     * <p>修复前只要调用没抛异常就是 true，mock provider 没命中任何规则、原样返回查询时
     * 也报 true——这个字段因此无法回答"AI 到底有没有影响这次检索"。现在必须是
     * false + NO_CHANGE，并且仍然保留 provider（调用是成功的，不是降级）。
     */
    @Test
    void successfulCallThatDidNotChangeTheQueryIsNotApplied() {
        var wiring = aiClient(true);
        expectRewrite(wiring.server(), rewriteResponse("wireless mouse", "mock"));

        var result = wiring.client().rewrite(options("wireless mouse", true));

        assertThat(result.status()).isEqualTo(AiRewriteStatus.NO_CHANGE);
        assertThat(result.applied()).isFalse();
        assertThat(result.status().fallback()).isFalse();
        assertThat(result.provider()).isEqualTo("mock");
    }

    /**
     * 只有大小写/空白差异的"改写"在 SearchQueryCompiler.applyRewrite 归一化之后完全等价，
     * 对外声称改写过属于噪音，会污染 AI 收益的统计口径。
     */
    @Test
    void whitespaceAndCaseOnlyDifferencesAreNotCountedAsRewrites() {
        var wiring = aiClient(true);
        expectRewrite(wiring.server(), rewriteResponse("wireless mouse", "langchain"));

        var result = wiring.client().rewrite(options("  Wireless   MOUSE ", true));

        assertThat(result.status()).isEqualTo(AiRewriteStatus.NO_CHANGE);
        assertThat(result.applied()).isFalse();
    }

    /** provider 是契约必填项，但它不影响改写结果；缺失时只降级为占位名，不能拖垮整次调用。 */
    @Test
    void missingProviderNameStillYieldsAnAppliedRewrite() {
        var wiring = aiClient(true);
        expectRewrite(wiring.server(), rewriteResponse("4k television", null));

        var result = wiring.client().rewrite(options("smart tv", true));

        assertThat(result.status()).isEqualTo(AiRewriteStatus.APPLIED);
        assertThat(result.provider()).isEqualTo("unknown");
    }

    /**
     * kill switch 关着时必须一次网络调用都不发。
     *
     * <p>MockRestServiceServer 没有登记任何期望，一旦真的发出请求就会抛 AssertionError
     * （Error 不会被 rewrite() 里的 catch RuntimeException 吞掉），测试立刻变红。
     */
    @Test
    void killSwitchSkipsTheCallAndReportsDisabled() {
        var wiring = aiClient(false);

        var result = wiring.client().rewrite(options("smart tv", true));

        assertThat(result.status()).isEqualTo(AiRewriteStatus.DISABLED);
        assertThat(result.applied()).isFalse();
        assertThat(result.provider()).isNull();
        assertThat(result.query()).isEqualTo("smart tv");
        wiring.server().verify();
    }

    /**
     * "没打算调用"必须和"调用挂了"分开。
     *
     * <p>修复前 {@code !aiEnabled() || !useAi()} 是一条短路或，DISABLED 与 NOT_REQUESTED
     * 塌缩成同一个 false，运维分不清是 kill switch 关着还是调用方压根没请求 AI。
     */
    @Test
    void requestThatDidNotAskForAiIsDistinguishableFromTheKillSwitch() {
        var wiring = aiClient(true);

        var result = wiring.client().rewrite(options("smart tv", false));

        assertThat(result.status()).isEqualTo(AiRewriteStatus.NOT_REQUESTED)
                .isNotEqualTo(AiRewriteStatus.DISABLED);
        assertThat(result.status().failure()).isFalse();
        wiring.server().verify();
    }

    /** 真实 LLM 最常见的失败就是读超时；它必须能单独统计，而不是混进笼统的"调用失败"。 */
    @Test
    void readTimeoutIsClassifiedAsTimeout() {
        assertFallback(new SocketTimeoutException("Read timed out"), AiRewriteStatus.TIMEOUT);
    }

    /** 连接超时走的是另一条异常链（HttpTimeoutException），同样要落到 TIMEOUT。 */
    @Test
    void connectTimeoutIsClassifiedAsTimeout() {
        assertFallback(new HttpConnectTimeoutException("connect timed out"), AiRewriteStatus.TIMEOUT);
    }

    /** 适配器没起来 / 端口不通：是传输故障，不是超时，混为一谈会误导排障方向。 */
    @Test
    void connectionRefusedIsClassifiedAsTransportError() {
        assertFallback(new ConnectException("Connection refused"), AiRewriteStatus.TRANSPORT_ERROR);
    }

    /** 适配器活着但返回 5xx：同样是传输层故障。 */
    @Test
    void serverErrorIsClassifiedAsTransportError() {
        var wiring = aiClient(true);
        wiring.server().expect(requestTo(REWRITE_URL)).andRespond(withServerError());

        var result = wiring.client().rewrite(options("smart tv", true));

        assertThat(result.status()).isEqualTo(AiRewriteStatus.TRANSPORT_ERROR);
        assertThat(result.applied()).isFalse();
        assertThat(result.provider()).isNull();
        assertThat(result.query()).isEqualTo("smart tv");
    }

    /**
     * 适配器可达但不守契约：网络没问题，改代码的方向完全不同，所以必须单独成一类。
     * 三种典型形态各测一次。
     */
    @Test
    void responseMissingRewrittenQueryIsInvalid() {
        assertInvalid("{\"provider\":\"langchain\",\"extracted_filters\":{}}");
    }

    @Test
    void blankRewrittenQueryIsInvalid() {
        assertInvalid("{\"rewritten_query\":\"   \",\"provider\":\"langchain\"}");
    }

    @Test
    void malformedJsonBodyIsInvalid() {
        assertInvalid("this is not json at all");
    }

    /**
     * 降级时交给查询编译器的文本必须回退成原始查询。
     *
     * <p>这是"降级不影响排序"的前提：一旦这里漏回退（比如返回 null 或半截改写），
     * 用户会在 AI 故障时静默拿到一份和 use_ai=false 不同的结果，且无从察觉。
     */
    @Test
    void everyFallbackReturnsTheOriginalQueryUntouched() {
        var wiring = aiClient(true);
        wiring.server().expect(requestTo(REWRITE_URL)).andRespond(withServerError());

        var result = wiring.client().rewrite(options("smart tv", true));

        assertThat(result.query()).isEqualTo("smart tv");
        assertThat(result.status().fallback()).isTrue();
    }

    /**
     * fallback()/failure() 的真值表。
     *
     * <p>告警会挂在 failure() 上：把 DISABLED 或 NOT_REQUESTED 算成故障会造成长期误报，
     * 把 TIMEOUT 算成正常则会把真故障藏起来——两边都不能错。
     */
    @Test
    void statusHelpersSeparateFallbackFromFailure() {
        assertThat(AiRewriteStatus.APPLIED.fallback()).isFalse();
        assertThat(AiRewriteStatus.NO_CHANGE.fallback()).isFalse();
        assertThat(AiRewriteStatus.NOT_REQUESTED.fallback()).isTrue();
        assertThat(AiRewriteStatus.DISABLED.fallback()).isTrue();
        assertThat(AiRewriteStatus.TIMEOUT.fallback()).isTrue();
        assertThat(AiRewriteStatus.TRANSPORT_ERROR.fallback()).isTrue();
        assertThat(AiRewriteStatus.INVALID_RESPONSE.fallback()).isTrue();

        assertThat(AiRewriteStatus.TIMEOUT.failure()).isTrue();
        assertThat(AiRewriteStatus.TRANSPORT_ERROR.failure()).isTrue();
        assertThat(AiRewriteStatus.INVALID_RESPONSE.failure()).isTrue();
        assertThat(AiRewriteStatus.APPLIED.failure()).isFalse();
        assertThat(AiRewriteStatus.NO_CHANGE.failure()).isFalse();
        assertThat(AiRewriteStatus.NOT_REQUESTED.failure()).isFalse();
        assertThat(AiRewriteStatus.DISABLED.failure()).isFalse();
    }

    /** 枚举名会直接出现在 API 响应里（Jackson 按 name() 序列化），改名即破坏前端契约。 */
    @Test
    void statusWireNamesAreStable() {
        assertThat(AiRewriteStatus.values()).extracting(Enum::name)
                .containsExactly("APPLIED", "NO_CHANGE", "NOT_REQUESTED", "DISABLED",
                        "TIMEOUT", "TRANSPORT_ERROR", "INVALID_RESPONSE");
    }

    private void assertInvalid(String body) {
        var wiring = aiClient(true);
        expectRewrite(wiring.server(), body);

        var result = wiring.client().rewrite(options("smart tv", true));

        assertThat(result.status()).isEqualTo(AiRewriteStatus.INVALID_RESPONSE);
        assertThat(result.applied()).isFalse();
        assertThat(result.provider()).isNull();
        assertThat(result.query()).isEqualTo("smart tv");
    }

    private void assertFallback(IOException transportFailure, AiRewriteStatus expected) {
        var wiring = aiClient(true);
        // RestClient 会把底层 IOException 包成 ResourceAccessException，真正的类型藏在
        // cause 链里；classify() 必须逐层查看才认得出超时。
        wiring.server().expect(requestTo(REWRITE_URL)).andRespond(withException(transportFailure));

        AiAdapterClient.RewriteResult result = wiring.client().rewrite(options("smart tv", true));

        assertThat(result.status()).isEqualTo(expected);
        assertThat(result.applied()).isFalse();
        assertThat(result.provider()).isNull();
        assertThat(result.query()).isEqualTo("smart tv");
    }

    private void expectRewrite(MockRestServiceServer server, String body) {
        server.expect(requestTo(REWRITE_URL))
                .andRespond(withSuccess(body, MediaType.APPLICATION_JSON));
    }
}
