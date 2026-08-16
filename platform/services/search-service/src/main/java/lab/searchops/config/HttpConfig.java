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
