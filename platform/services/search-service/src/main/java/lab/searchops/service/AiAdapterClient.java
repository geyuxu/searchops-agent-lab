package lab.searchops.service;

import tools.jackson.databind.JsonNode;
import java.util.LinkedHashMap;
import java.util.Map;
import lab.searchops.config.SearchProperties;
import lab.searchops.domain.ApiModels.SearchOptions;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

@Component
public class AiAdapterClient {
    private static final Logger log = LoggerFactory.getLogger(AiAdapterClient.class);
    private final RestClient client;
    private final SearchProperties properties;

    public AiAdapterClient(@Qualifier("aiRestClient") RestClient client, SearchProperties properties) {
        this.client = client;
        this.properties = properties;
    }

    public RewriteResult rewrite(SearchOptions options) {
        if (!properties.aiEnabled() || !options.useAi()) {
            return new RewriteResult(options.query(), false, "disabled");
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
            if (response == null || !"mock".equals(response.path("provider").asText())) {
                throw new IllegalStateException("AI adapter returned an invalid provider response");
            }
            return new RewriteResult(response.path("rewritten_query").asText(options.query()),
                    true, response.path("provider").asText());
        } catch (RuntimeException exception) {
            log.warn("AI rewrite unavailable; using BM25 fallback: {}", exception.getMessage());
            return new RewriteResult(options.query(), false, "fallback");
        }
    }

    public record RewriteResult(String query, boolean applied, String provider) {}
}
