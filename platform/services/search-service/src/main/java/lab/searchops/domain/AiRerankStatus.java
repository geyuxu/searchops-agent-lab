package lab.searchops.domain;

/**
 * AI 候选集重排这一次调用的确定性结果。
 *
 * <p>为什么新建一个枚举而不是复用 {@link AiRewriteStatus}：两条链路的取值域并不相同
 * （重排有 {@link #NOT_APPLICABLE}、{@link #OVERLOADED}、{@link #INTERNAL_ERROR} 这些
 * 改写根本不存在的状态），而且两条链路可以独立开关、独立降级——同一次请求完全可能是
 * "改写成功 + 重排超时"。若把它们塞进同一个 {@code ai_status} 字段，运维看到一个
 * {@code TIMEOUT} 将无法判断是哪一段超时了，正是这类塌缩当初逼出了 AiRewriteStatus。
 * 因此重排在 SearchResponse 里占用一组独立字段：{@code rerank_status} /
 * {@code rerank_applied} / {@code rerank_provider} / {@code rerank_candidates}。
 *
 * <p>取值分三类：
 * <ul>
 *   <li>{@link #APPLIED}、{@link #NO_CHANGE}：适配器被调用且返回了合法响应。</li>
 *   <li>{@link #NOT_REQUESTED}、{@link #DISABLED}、{@link #AI_DISABLED}、
 *       {@link #NOT_APPLICABLE}：根本没有发起调用，且都不是故障。</li>
 *   <li>其余取值：发起了调用（或本该发起）但失败，本次结果是纯 BM25 顺序，取值即降级原因。</li>
 * </ul>
 *
 * <p>无论取哪个值，返回给调用方的文档集合都与 BM25 检索结果完全相同——重排只可能改变顺序，
 * 降级只可能让顺序退回 BM25。这条不变量由 {@link lab.searchops.service.RerankOrdering} 保证。
 */
public enum AiRerankStatus {
    /** 调用成功，且实际返回给调用方的那一页顺序确实被改变了。 */
    APPLIED,

    /**
     * 调用成功，但重排后本页顺序与 BM25 完全一致。
     *
     * <p>注意判定口径是"本页"（截断后的 size 条）而不是整个候选集：只发生在第 30-50 位之间的
     * 置换对用户和 NDCG@10 都不可见，把它记成 APPLIED 会重演旧 {@code ai_applied} 那种
     * "覆盖率虚高、实际没影响"的读数。
     */
    NO_CHANGE,

    /** 请求本身没有要求重排（rerank=false / use_rerank=false）。不是故障。 */
    NOT_REQUESTED,

    /** 重排开关 lab.search.rerank.enabled=false（RERANK_ENABLED 的 kill switch）。不是故障。 */
    DISABLED,

    /**
     * 全局 AI 开关 lab.search.ai-enabled=false，重排随之被一起关闭。不是故障。
     *
     * <p>与 {@link #DISABLED} 分开的理由和 AiRewriteStatus 里 DISABLED/NOT_REQUESTED 分开
     * 一样：两个开关关的是不同的东西，塌缩成一个值就没法回答"该关哪个/开哪个"。
     */
    AI_DISABLED,

    /**
     * 请求要求了重排，但这条链路对本次请求不适用，因此没有调用。不是故障。
     *
     * <p>覆盖：page&gt;0（分页语义见 ProductSearchService 的注释）、非 relevance 排序
     * （用户显式要求按价格/标题排，用相关性重排等于推翻用户的选择）、空查询浏览、
     * 候选集为空。
     */
    NOT_APPLICABLE,

    /**
     * 并发预算耗尽，本次直接降级，没有发起调用。
     *
     * <p>重排是同步阻塞在请求线程上的秒级调用，没有这道闸门时一波重排流量就能占满
     * Tomcat 线程池，把不用 AI 的普通搜索一起拖死。宁可这一条请求退回 BM25。
     */
    OVERLOADED,

    /** 连接或读取超时。重排的提示词包含 N 条商品，延迟远高于改写，这一类必须能单独统计。 */
    TIMEOUT,

    /** 其它网络故障，或适配器返回了 4xx/5xx 状态码。 */
    TRANSPORT_ERROR,

    /** 响应体缺失、不是合法 JSON，或 ranked_product_ids 缺失/为空。 */
    INVALID_RESPONSE,

    /**
     * 合并重排结果时自身的不变量被破坏，结果已强制退回 BM25 顺序。
     *
     * <p>这个值永远不该出现：出现即 {@link lab.searchops.service.RerankOrdering} 有缺陷。
     * 之所以给它一个专用取值而不是混进 {@link #INVALID_RESPONSE}，是因为把自己的 bug
     * 记成"模型返回非法"会把排查引到完全错误的方向。
     */
    INTERNAL_ERROR;

    /** 本次返回的顺序是否就是纯 BM25 顺序（NO_CHANGE 也是，但它不是降级）。 */
    public boolean fallback() {
        return this != APPLIED && this != NO_CHANGE;
    }

    /** 是否属于故障降级（区别于 DISABLED/NOT_REQUESTED/NOT_APPLICABLE 这类正常的"没打算调用"）。 */
    public boolean failure() {
        return this == TIMEOUT || this == TRANSPORT_ERROR || this == INVALID_RESPONSE
                || this == INTERNAL_ERROR;
    }

    /** 是否真的向适配器发出了 HTTP 请求（OVERLOADED 是发出前就被拦下的）。 */
    public boolean called() {
        return this == APPLIED || this == NO_CHANGE || this == TIMEOUT
                || this == TRANSPORT_ERROR || this == INVALID_RESPONSE || this == INTERNAL_ERROR;
    }
}
