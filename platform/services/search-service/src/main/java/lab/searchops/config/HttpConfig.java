package lab.searchops.config;

import java.net.http.HttpClient;
import java.time.Duration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.web.client.RestClient;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

@Configuration
public class HttpConfig {
    /** AI 读超时的兜底值：配置缺失或非正数时使用，够真实 LLM 返回一次改写。 */
    private static final long DEFAULT_AI_READ_TIMEOUT_MS = 5000;

    /** AI 连接超时的兜底值：只是建连，适配器不可达时应当立刻降级而不是干等。 */
    private static final long DEFAULT_AI_CONNECT_TIMEOUT_MS = 1000;

    /** 重排读超时的兜底值，见 aiRerankRestClient 对这个数字的完整取舍说明。 */
    private static final long DEFAULT_RERANK_READ_TIMEOUT_MS = RerankProperties.DEFAULT_TIMEOUT_MS;

    /** 下限保护，避免把超时配成 0/1ms 让 AI 路径必然失败。 */
    private static final long MIN_TIMEOUT_MS = 50;

    @Bean("elasticsearchRestClient")
    RestClient elasticsearchRestClient(SearchProperties properties) {
        var factory = new JdkClientHttpRequestFactory();
        factory.setReadTimeout(Duration.ofSeconds(30));
        return RestClient.builder().baseUrl(properties.elasticsearchUrl())
                .requestFactory(factory).build();
    }

    @Bean("aiRestClient")
    RestClient aiRestClient(SearchProperties properties) {
        // 为什么改：读超时原本直接取 AI_TIMEOUT_MS（部署里是 400ms），任何真实 LLM 都必然
        // 超时并撞进 AiAdapterClient 的静默 BM25 降级，看起来像"AI 没效果"。默认抬到 5s，
        // 但仍然完全由 AI_TIMEOUT_MS 覆盖——超时值不写死。
        // 同时补上连接超时：JdkClientHttpRequestFactory 只能设读超时，连接超时必须建在
        // 底层 HttpClient 上，否则适配器不可达时的失败时机不可控。
        var readTimeout = timeout(properties.aiTimeoutMs(), DEFAULT_AI_READ_TIMEOUT_MS);
        var connectTimeout = timeout(properties.aiConnectTimeoutMs(), DEFAULT_AI_CONNECT_TIMEOUT_MS);
        var httpClient = HttpClient.newBuilder().connectTimeout(connectTimeout).build();
        var factory = new JdkClientHttpRequestFactory(httpClient);
        factory.setReadTimeout(readTimeout);
        return RestClient.builder().baseUrl(properties.aiAdapterUrl())
                .requestFactory(factory).build();
    }

    /**
     * 重排专用的 RestClient：与改写共用适配器地址，但读超时预算完全独立。
     *
     * <p>为什么必须分开：改写的提示词只有一条查询，5000ms 绰绰有余；重排的提示词要塞进
     * N 条（默认 50）商品标题，输出还要吐回 N 个 id，实测量级是秒。继续共用 5000ms 的话，
     * 重排会**稳定超时**——而超时的表现恰恰是"返回 200 + BM25 顺序"，于是整条链路看起来
     * 像"重排没有收益"，与它根本没跑起来无法区分。这正是改写路径当年 400ms 踩过的坑。
     *
     * <p>默认 15000ms 的风险，必须说清楚：前台搜索是同步阻塞在 Tomcat 工作线程上的，
     * 一次重排最坏会占住一个线程 15 秒。缓解措施有三层——
     * (1) 重排必须由请求显式要求（rerank=true），前台默认不发，普通流量完全不受影响；
     * (2) 连接超时仍沿用 aiConnectTimeoutMs（默认 1s），适配器不可达时 1 秒内就降级，
     *     15s 只花在"适配器活着但模型慢"这一种情况上；
     * (3) 并发预算 lab.search.rerank.max-concurrency（默认 8）会在预算耗尽时立即降级，
     *     所以最坏情况下被拖住的线程数有上界，不会吃光线程池。
     * 若要把重排接入真实前台流量，正确做法是先加异步/超时隔离，而不是把这个值调小——
     * 调小只会把"慢"变成"看起来没效果"。
     */
    @Bean("aiRerankRestClient")
    RestClient aiRerankRestClient(SearchProperties properties, RerankProperties rerank) {
        var readTimeout = timeout(rerank.timeoutMs(), DEFAULT_RERANK_READ_TIMEOUT_MS);
        var connectTimeout = timeout(properties.aiConnectTimeoutMs(), DEFAULT_AI_CONNECT_TIMEOUT_MS);
        var httpClient = HttpClient.newBuilder().connectTimeout(connectTimeout).build();
        var factory = new JdkClientHttpRequestFactory(httpClient);
        factory.setReadTimeout(readTimeout);
        return RestClient.builder().baseUrl(properties.aiAdapterUrl())
                .requestFactory(factory).build();
    }

    /** 配置值为 0（属性缺失时 int 的默认值）或负数时退回内置默认，其余尊重配置并加下限。 */
    private static Duration timeout(int configuredMs, long defaultMs) {
        return Duration.ofMillis(configuredMs > 0 ? Math.max(MIN_TIMEOUT_MS, configuredMs) : defaultMs);
    }

    @Bean
    WebMvcConfigurer corsConfigurer() {
        return new WebMvcConfigurer() {
            @Override
            public void addCorsMappings(CorsRegistry registry) {
                registry.addMapping("/api/**")
                        .allowedOrigins("http://localhost:3000", "http://localhost:3001")
                        .allowedMethods("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
                        .allowedHeaders("*")
                        .exposedHeaders("X-Request-ID");
            }
        };
    }
}
