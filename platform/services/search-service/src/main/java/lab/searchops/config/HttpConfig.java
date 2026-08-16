package lab.searchops.config;

import java.time.Duration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.web.client.RestClient;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

@Configuration
public class HttpConfig {
    @Bean("elasticsearchRestClient")
    RestClient elasticsearchRestClient(SearchProperties properties) {
        var factory = new JdkClientHttpRequestFactory();
        factory.setReadTimeout(Duration.ofSeconds(30));
        return RestClient.builder().baseUrl(properties.elasticsearchUrl())
                .requestFactory(factory).build();
    }

    @Bean("aiRestClient")
    RestClient aiRestClient(SearchProperties properties) {
        var factory = new JdkClientHttpRequestFactory();
        var timeout = Duration.ofMillis(Math.max(50, properties.aiTimeoutMs()));
        factory.setReadTimeout(timeout);
        return RestClient.builder().baseUrl(properties.aiAdapterUrl())
                .requestFactory(factory).build();
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
