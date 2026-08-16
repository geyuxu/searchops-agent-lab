package lab.searchops.service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import lab.searchops.domain.ApiModels.SearchOptions;
import lab.searchops.domain.StrategyConfig;
import org.springframework.stereotype.Component;

@Component
public class SearchQueryCompiler {
    private static final Map<String, String> ALLOWED_FIELDS = Map.of(
            "title", "title",
            "brand", "brand",
            "bullet_point", "bullet_point",
            "description", "description",
            "category", "category");

    public Compiled compile(SearchOptions options, StrategyConfig strategy, String rewrittenByAi) {
        var effective = applyRewrite(rewrittenByAi == null ? options.query() : rewrittenByAi, strategy);
        var organic = organicQuery(effective, options, strategy);
        Map<String, Object> query = organic;

        if (!strategy.pinnedProductIds().isEmpty() && isRelevanceSort(options.sort())) {
            query = Map.of("pinned", Map.of(
                    "ids", strategy.pinnedProductIds(),
                    "organic", organic));
        }
        if (!strategy.brandBoosts().isEmpty()) {
            var functions = strategy.brandBoosts().entrySet().stream()
                    .map(entry -> Map.<String, Object>of(
                            "filter", Map.of("term", Map.of("brand.keyword", entry.getKey())),
                            "weight", entry.getValue()))
                    .toList();
            query = Map.of("function_score", Map.of(
                    "query", query,
                    "functions", functions,
                    "score_mode", "multiply",
                    "boost_mode", "multiply"));
        }

        var body = new LinkedHashMap<String, Object>();
        body.put("from", options.page() * options.size());
        body.put("size", options.size());
        body.put("track_total_hits", true);
        body.put("query", query);
        body.put("sort", sort(options.sort()));
        if (strategy.minimumScore() > 0 && isRelevanceSort(options.sort())) {
            body.put("min_score", strategy.minimumScore());
        }
        body.put("aggs", Map.of(
                "brands", Map.of("terms", Map.of("field", "brand.keyword", "size", 30)),
                "categories", Map.of("terms", Map.of("field", "category.keyword", "size", 30))));
        return new Compiled(effective, query, body);
    }

    private Map<String, Object> organicQuery(String effective, SearchOptions options,
            StrategyConfig strategy) {
        var filters = new ArrayList<Map<String, Object>>();
        if (options.brand() != null && !options.brand().isBlank()) {
            filters.add(Map.of("term", Map.of("brand.keyword", options.brand())));
        }
        if (options.category() != null && !options.category().isBlank()) {
            filters.add(Map.of("term", Map.of("category.keyword", options.category())));
        }
        if (Boolean.TRUE.equals(options.inStock())) {
            filters.add(Map.of("range", Map.of("inventory", Map.of("gt", 0))));
        }
        if (options.priceMin() != null || options.priceMax() != null) {
            var range = new LinkedHashMap<String, Object>();
            if (options.priceMin() != null) range.put("gte", options.priceMin());
            if (options.priceMax() != null) range.put("lte", options.priceMax());
            filters.add(Map.of("range", Map.of("price_cents", range)));
        }

        Map<String, Object> must;
        if (effective == null || effective.isBlank() || "*".equals(effective)) {
            must = Map.of("match_all", Map.of());
        } else {
            var expanded = expandSynonyms(effective, strategy.synonyms());
            var fields = strategy.fieldWeights().entrySet().stream()
                    .filter(entry -> ALLOWED_FIELDS.containsKey(entry.getKey()))
                    .filter(entry -> entry.getValue() > 0)
                    .map(entry -> ALLOWED_FIELDS.get(entry.getKey()) + "^" + entry.getValue())
                    .toList();
            must = Map.of("multi_match", Map.of(
                    "query", expanded,
                    "fields", fields.isEmpty() ? List.of("title^4", "brand^2", "description") : fields,
                    "type", "best_fields",
                    "operator", "or",
                    "fuzziness", "AUTO"));
        }

        var bool = new LinkedHashMap<String, Object>();
        bool.put("must", List.of(must));
        if (!filters.isEmpty()) bool.put("filter", filters);
        if (!strategy.blockedProductIds().isEmpty()) {
            bool.put("must_not", List.of(Map.of("ids", Map.of("values", strategy.blockedProductIds()))));
        }
        return Map.of("bool", bool);
    }

    private String applyRewrite(String query, StrategyConfig strategy) {
        var normalized = query == null ? "" : String.join(" ", query.trim().split("\\s+")).toLowerCase();
        for (var rule : strategy.rewriteRules()) {
            if (normalized.equals(rule.match().trim().toLowerCase())) return rule.rewrite();
        }
        return normalized;
    }

    private String expandSynonyms(String query, Map<String, List<String>> synonyms) {
        var expanded = new ArrayList<String>();
        expanded.add(query);
        var lower = query.toLowerCase();
        synonyms.forEach((term, values) -> {
            if (lower.contains(term.toLowerCase())) expanded.addAll(values);
        });
        return String.join(" ", expanded);
    }

    /**
     * 是否按相关性排序。
     *
     * <p>从私有实例方法改成 public static：重排也必须回答同一个问题（用户显式按价格排序时
     * 不能用相关性重排），两处各写一份判定迟早会分叉。这里是唯一定义。
     */
    public static boolean isRelevanceSort(String sort) {
        return sort == null || sort.isBlank() || "relevance".equals(sort);
    }

    private List<Map<String, Object>> sort(String sort) {
        return switch (sort == null ? "relevance" : sort) {
            case "price_asc" -> List.of(Map.of("price_cents", "asc"), Map.of("product_id", "asc"));
            case "price_desc" -> List.of(Map.of("price_cents", "desc"), Map.of("product_id", "asc"));
            case "title_asc" -> List.of(Map.of("title.keyword", "asc"), Map.of("product_id", "asc"));
            case "popularity" -> List.of(Map.of("popularity", "desc"), Map.of("product_id", "asc"));
            case "relevance" -> List.of(Map.of("_score", "desc"), Map.of("product_id", "asc"));
            default -> throw new IllegalArgumentException("Unknown sort: " + sort);
        };
    }

    public record Compiled(String effectiveQuery, Map<String, Object> query, Map<String, Object> body) {}
}
