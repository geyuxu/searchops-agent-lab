package lab.searchops.domain;

/**
 * AI 查询改写这一次调用的确定性结果。
 *
 * <p>为什么需要它：在此之前 {@code SearchResponse.ai_applied} 是唯一的可观测信号，
 * 而 "AI 被关闭"、"请求没要求用 AI"、"调用超时"、"适配器返回了看不懂的内容" 四种情况
 * 全部塌缩成同一个 {@code false}。运维只能看到"AI 没效果"，无法判断是没跑还是跑挂了，
 * 这正是把静默 BM25 降级误读成"AI 无收益"的根因。
 *
 * <p>取值分两类：
 * <ul>
 *   <li>{@link #APPLIED}、{@link #NO_CHANGE}：AI 适配器被调用且返回了合法响应，没有发生降级。</li>
 *   <li>其余取值：本次检索走的是纯 BM25 路径（降级），取值即降级原因。</li>
 * </ul>
 */
public enum AiRewriteStatus {
    /** 调用成功，且改写后的查询与原始查询确实不同。 */
    APPLIED,

    /** 调用成功，但适配器把查询原样返回（例如 mock provider 没有命中任何规则）。 */
    NO_CHANGE,

    /** 请求本身没有要求 AI（use_ai=false）。不是故障。 */
    NOT_REQUESTED,

    /** 全局开关 lab.search.ai-enabled=false（AI_ENABLED 的 kill switch）。不是故障。 */
    DISABLED,

    /** 连接或读取超时。真实 LLM 场景下最需要单独识别的一类降级。 */
    TIMEOUT,

    /** 其它网络故障，或适配器返回了 4xx/5xx 状态码。 */
    TRANSPORT_ERROR,

    /** 响应体缺失、不是合法 JSON，或缺少可用的 rewritten_query。 */
    INVALID_RESPONSE;

    /** 本次检索是否因为这个状态而退回到纯 BM25。 */
    public boolean fallback() {
        return this != APPLIED && this != NO_CHANGE;
    }

    /** 是否属于故障降级（区别于 DISABLED/NOT_REQUESTED 这类正常的"没打算调用"）。 */
    public boolean failure() {
        return this == TIMEOUT || this == TRANSPORT_ERROR || this == INVALID_RESPONSE;
    }
}
