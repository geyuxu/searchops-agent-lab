package lab.searchops.service;

import java.net.SocketTimeoutException;
import java.net.http.HttpTimeoutException;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.TimeoutException;
import lab.searchops.config.SearchProperties;
import lab.searchops.domain.AiRewriteStatus;
import lab.searchops.domain.ApiModels.SearchOptions;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.MediaType;
import org.springframework.http.converter.HttpMessageConversionException;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import tools.jackson.core.JacksonException;
import tools.jackson.databind.JsonNode;

@Component
public class AiAdapterClient {
    private static final Logger log = LoggerFactory.getLogger(AiAdapterClient.class);

    /** 适配器没有回报 provider 名时的占位值，仅用于观测，不参与任何判断。 */
    static final String UNKNOWN_PROVIDER = "unknown";

    private final RestClient client;
    private final SearchProperties properties;

    public AiAdapterClient(@Qualifier("aiRestClient") RestClient client, SearchProperties properties) {
        this.client = client;
        this.properties = properties;
    }

    public RewriteResult rewrite(SearchOptions options) {
        // 为什么拆成两个 if：原实现是 `!aiEnabled() || !useAi()` 的短路或，两种"没调用"
        // 在响应里完全无法区分，运维分不清是 kill switch 关着还是调用方没请求 AI。
        if (!properties.aiEnabled()) {
            return notCalled(options, AiRewriteStatus.DISABLED);
        }
        if (!options.useAi()) {
            return notCalled(options, AiRewriteStatus.NOT_REQUESTED);
        }

        var filters = new LinkedHashMap<String, Object>();
        if (options.brand() != null) filters.put("brand", options.brand());
        if (options.category() != null) filters.put("category", options.category());
        if (options.priceMin() != null) filters.put("price_min", options.priceMin());
        if (options.priceMax() != null) filters.put("price_max", options.priceMax());

        try {
            var response = client.post().uri("/ai/query-rewrite")
                    .contentType(MediaType.APPLICATION_JSON)
                    .header("X-Request-ID", options.requestId())
                    .body(Map.of(
                            "query", options.query(),
                            "locale", options.locale(),
                            "filters", filters,
                            "request_id", options.requestId()))
                    .retrieve().body(JsonNode.class);

            // 为什么只校验 rewritten_query：原实现要求 provider 必须等于 "mock"，
            // 任何真实 provider（langchain、openai…）都会在这里抛异常并被下面的 catch
            // 静默降级成 BM25——docs/ai-handoff.md 承诺的"换真实 provider 无需改 Java"因此不成立。
            // provider 名现在只做记录、向上传递，不再作为准入条件。
            var rewritten = response == null ? "" : response.path("rewritten_query").asText("");
            if (rewritten.isBlank()) {
                throw new InvalidAiResponseException(
                        "AI adapter response is missing a usable rewritten_query");
            }
            var provider = response.path("provider").asText("");
            if (provider.isBlank()) {
                // 契约里 provider 是必填项，但它不影响改写结果本身，因此只告警不降级。
                log.warn("AI adapter omitted provider name (request_id={})", options.requestId());
                provider = UNKNOWN_PROVIDER;
            }

            // 为什么比较归一化后的文本：SearchQueryCompiler 会把查询 trim + 折叠空白 + 转小写，
            // 只有大小写/空白差异的"改写"在编译后完全等价，对外声称改写过属于噪音。
            var changed = !normalize(rewritten).equals(normalize(options.query()));
            var status = changed ? AiRewriteStatus.APPLIED : AiRewriteStatus.NO_CHANGE;
            log.debug("AI rewrite {} by provider {} (request_id={})", status, provider,
                    options.requestId());
            return new RewriteResult(rewritten, changed, provider, status);
        } catch (RuntimeException exception) {
            // 原实现把所有 RuntimeException 折叠成一个 "fallback" 字符串，超时和协议错误
            // 无法分辨；真实 LLM 最常见的失败恰恰是超时，必须能单独统计。
            var status = classify(exception);
            log.warn("AI rewrite fell back to BM25 (status={}, request_id={}): {}",
                    status, options.requestId(), exception.toString());
            return new RewriteResult(options.query(), false, null, status);
        }
    }

    private RewriteResult notCalled(SearchOptions options, AiRewriteStatus status) {
        return new RewriteResult(options.query(), false, null, status);
    }

    private AiRewriteStatus classify(RuntimeException exception) {
        if (exception instanceof InvalidAiResponseException) {
            return AiRewriteStatus.INVALID_RESPONSE;
        }
        if (hasCause(exception, JacksonException.class)
                || hasCause(exception, HttpMessageConversionException.class)) {
            return AiRewriteStatus.INVALID_RESPONSE;
        }
        if (hasCause(exception, HttpTimeoutException.class)
                || hasCause(exception, SocketTimeoutException.class)
                || hasCause(exception, TimeoutException.class)) {
            // JdkClientHttpRequestFactory 的读超时会被 RestClient 包成
            // ResourceAccessException，真正的超时类型藏在 cause 链里，必须逐层查看。
            return AiRewriteStatus.TIMEOUT;
        }
        return AiRewriteStatus.TRANSPORT_ERROR;
    }

    private boolean hasCause(Throwable error, Class<? extends Throwable> type) {
        for (var cause = error; cause != null; cause = cause.getCause()) {
            if (type.isInstance(cause)) return true;
            if (cause.getCause() == cause) break; // 防御自引用 cause 造成的死循环
        }
        return false;
    }

    private String normalize(String value) {
        return value == null ? "" : String.join(" ", value.trim().split("\\s+")).toLowerCase();
    }

    /**
     * @param query    实际交给查询编译器的文本；降级时等于原始查询
     * @param applied  查询是否真的被 AI 改写（归一化后不等于原始查询）
     * @param provider 适配器回报的 provider 名；未真正调用或调用失败时为 null
     * @param status   本次调用的确定性结果，降级时即降级原因
     */
    public record RewriteResult(String query, boolean applied, String provider,
            AiRewriteStatus status) {}

    /** 适配器可达但响应不符合契约（响应体为空、缺少 rewritten_query）。 */
    static class InvalidAiResponseException extends RuntimeException {
        InvalidAiResponseException(String message) {
            super(message);
        }
    }
}
