package lab.searchops.service;

import tools.jackson.databind.JsonNode;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import lab.searchops.domain.ApiModels.FacetBucket;
import lab.searchops.domain.ApiModels.Product;
import lab.searchops.domain.ApiModels.SearchOptions;
import lab.searchops.domain.ApiModels.SearchResponse;
import lab.searchops.domain.StrategyConfig;
import org.springframework.stereotype.Service;

@Service
public class ProductSearchService {
    public static final String DATA_NOTICE = "Product text and relevance labels: public Amazon ESCI data. "
            + "Prices, inventory, traffic, users and orders: deterministic simulated data.";

    private final ElasticsearchGateway elasticsearch;
    private final SearchQueryCompiler compiler;
    private final StrategyService strategies;
    private final SearchAnalyticsRepository analytics;
    private final AiAdapterClient ai;

    public ProductSearchService(ElasticsearchGateway elasticsearch, SearchQueryCompiler compiler,
            StrategyService strategies, SearchAnalyticsRepository analytics, AiAdapterClient ai) {
        this.elasticsearch = elasticsearch;
        this.compiler = compiler;
        this.strategies = strategies;
        this.analytics = analytics;
        this.ai = ai;
    }

    public SearchResponse search(SearchOptions options) {
        return search(options, null);
    }

    public SearchResponse search(SearchOptions options, StrategyConfig override) {
        var started = System.nanoTime();
        var active = strategies.current();
        var strategy = override == null ? active.config() : override;
        var rewrite = ai.rewrite(options);
        var compiled = compiler.compile(options, strategy, rewrite.query());
        var raw = elasticsearch.search(compiled.body());
        var products = products(raw.path("hits").path("hits"));
        var total = raw.path("hits").path("total").path("value").asLong();
        var latency = Math.max(0, (System.nanoTime() - started) / 1_000_000);
        var version = override == null ? active.version() : -1;
        // rewrite.applied() 现在只在"改写结果 != 原始查询"时为 true；
        // AI 调用成功但没动查询会是 false + status=NO_CHANGE，调用失败则是 false + 具体降级原因。
        var response = new SearchResponse(options.requestId(), options.query(), compiled.effectiveQuery(),
                total, options.page(), options.size(), latency, version, products, facets(raw),
                rewrite.applied(), rewrite.status(), rewrite.provider(), DATA_NOTICE);
        if (options.logRequest() && override == null) {
            analytics.record(options, compiled.effectiveQuery(), total, latency,
                    products.stream().map(Product::productId).limit(10).toList(), version);
        }
        return response;
    }

    public Optional<Product> product(String id) {
        var raw = elasticsearch.getProduct(id);
        if (raw == null || !raw.path("found").asBoolean()) return Optional.empty();
        return Optional.of(product(raw.path("_source"), null));
    }

    public Map<String, Object> explain(String query, String productId, String requestId) {
        var options = new SearchOptions(query, "en-US", 0, 10, null, null, null, null,
                null, "relevance", false, requestId, false);
        var strategy = strategies.current();
        var compiled = compiler.compile(options, strategy.config(), query);
        var explanation = elasticsearch.explain(productId, compiled.query());
        return Map.of(
                "query", query,
                "effective_query", compiled.effectiveQuery(),
                "product_id", productId,
                "strategy_version", strategy.version(),
                "matched", explanation.path("matched").asBoolean(),
                "explanation", explanation.path("explanation"),
                "compiled_query", compiled.query());
    }

    public Map<String, Object> rebuild(String path) {
        return elasticsearch.rebuild(path);
    }

    private List<Product> products(JsonNode hits) {
        var products = new ArrayList<Product>();
        hits.forEach(hit -> products.add(product(hit.path("_source"), hit.path("_score").isNumber()
                ? hit.path("_score").asDouble() : null)));
        return products;
    }

    private Product product(JsonNode source, Double score) {
        return new Product(
                source.path("product_id").asText(),
                source.path("title").asText(),
                source.path("brand").asText(),
                source.path("description").asText(),
                source.path("bullet_point").asText(),
                source.path("color").asText(),
                source.path("locale").asText("us"),
                source.path("category").asText("Other"),
                source.path("price_cents").asInt(),
                source.path("currency").asText("USD"),
                source.path("inventory").asInt(),
                source.path("placeholder_hue").asInt(),
                source.path("provenance").asText("Amazon ESCI public dataset"),
                score);
    }

    private Map<String, List<FacetBucket>> facets(JsonNode response) {
        var result = new LinkedHashMap<String, List<FacetBucket>>();
        result.put("brands", buckets(response.path("aggregations").path("brands").path("buckets")));
        result.put("categories", buckets(response.path("aggregations").path("categories").path("buckets")));
        return result;
    }

    private List<FacetBucket> buckets(JsonNode nodes) {
        var values = new ArrayList<FacetBucket>();
        nodes.forEach(node -> values.add(new FacetBucket(node.path("key").asText(),
                node.path("doc_count").asLong())));
        return values;
    }
}
