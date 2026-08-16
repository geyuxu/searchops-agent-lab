package lab.searchops.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 候选集重排的配置，独立于 {@link SearchProperties} 里的改写配置。
 *
 * <p>为什么单独一个 record 而不是往 SearchProperties 上加字段：
 * <ul>
 *   <li>两条 AI 链路可以独立开关（"只想开改写"或"只想开重排"是常态），配置也应当独立；</li>
 *   <li>SearchProperties 是构造器绑定的 record，加分量会改变它的规范构造器签名，
 *       牵连所有既有构造点；新开一个前缀 {@code lab.search.rerank.*} 的 record
 *       只有一个构造器，绑定行为没有任何歧义。</li>
 * </ul>
 *
 * @param enabled        重排 kill switch。关掉时状态是 AiRerankStatus.DISABLED，
 *                       与全局 AI 开关关掉时的 AI_DISABLED 可区分。
 * @param depth          BM25 取回的候选深度 N。0/负数回落到默认 50；上限 200 是适配器契约
 *                       RerankRequest.candidates 的 max_length，超出会被适配器 422 掉。
 * @param timeoutMs      重排专用读超时。改写默认 5000ms 对重排远远不够（提示词含 N 条商品）。
 * @param maxConcurrency 同时在途的重排调用上限，超出即立刻降级（AiRerankStatus.OVERLOADED）。
 *                       前台搜索是同步阻塞在请求线程上的，没有这道闸门时慢重排会占满线程池。
 */
@ConfigurationProperties(prefix = "lab.search.rerank")
public record RerankProperties(
        boolean enabled,
        int depth,
        int timeoutMs,
        int maxConcurrency) {

    /** 适配器契约 RerankRequest.candidates 的 max_length；候选深度的硬上限。 */
    public static final int MAX_DEPTH = 200;

    /** 默认候选深度。诊断显示 57.6% 的彻底失败中，相关商品落在 11-200 位，50 覆盖其中大半。 */
    public static final int DEFAULT_DEPTH = 50;

    /** 默认读超时。见 HttpConfig.aiRerankRestClient 对这个数字的取舍说明。 */
    public static final int DEFAULT_TIMEOUT_MS = 15000;

    /** 默认并发预算。Tomcat 默认 200 个工作线程，8 条慢调用不足以拖垮普通搜索。 */
    public static final int DEFAULT_MAX_CONCURRENCY = 8;

    public RerankProperties {
        // 属性缺省时 int 是 0，一律回落到默认值；同时把深度钳在适配器契约的上限内，
        // 免得一个 RERANK_DEPTH=500 让每次重排都稳定 422。
        depth = depth > 0 ? Math.min(depth, MAX_DEPTH) : DEFAULT_DEPTH;
        timeoutMs = timeoutMs > 0 ? timeoutMs : DEFAULT_TIMEOUT_MS;
        maxConcurrency = maxConcurrency > 0 ? maxConcurrency : DEFAULT_MAX_CONCURRENCY;
    }

    /** 未接入 Spring 配置时的默认值（重排开着，但仍需请求显式要求才会调用）。 */
    public static RerankProperties defaults() {
        return new RerankProperties(true, DEFAULT_DEPTH, DEFAULT_TIMEOUT_MS, DEFAULT_MAX_CONCURRENCY);
    }

    /**
     * 本次请求实际要取回的候选深度。
     *
     * <p>永远不小于本页要展示的条数——候选比展示还浅就不叫重排了；也永远不超过契约上限。
     *
     * @param requested 请求参数 rerank_depth，null 表示用服务端默认
     * @param pageSize  本页要返回的条数
     */
    public int effectiveDepth(Integer requested, int pageSize) {
        var wanted = requested != null && requested > 0 ? requested : depth;
        return Math.min(MAX_DEPTH, Math.max(wanted, Math.max(pageSize, 1)));
    }
}
