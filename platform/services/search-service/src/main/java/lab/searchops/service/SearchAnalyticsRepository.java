package lab.searchops.service;

import tools.jackson.core.JacksonException;
import tools.jackson.databind.ObjectMapper;
import java.util.List;
import java.util.Map;
import lab.searchops.domain.ApiModels.SearchOptions;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

@Repository
public class SearchAnalyticsRepository {
    private final JdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public SearchAnalyticsRepository(JdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    public void record(SearchOptions options, String effectiveQuery, long resultCount, long latencyMs,
            List<String> productIds, int strategyVersion) {
        var filters = new java.util.LinkedHashMap<String, Object>();
        if (options.brand() != null) filters.put("brand", options.brand());
        if (options.category() != null) filters.put("category", options.category());
        if (options.priceMin() != null) filters.put("price_min", options.priceMin());
        if (options.priceMax() != null) filters.put("price_max", options.priceMax());
        if (options.inStock() != null) filters.put("in_stock", options.inStock());
        jdbc.update("""
                INSERT INTO search_requests
                  (request_id, query, effective_query, locale, filters, result_count,
                   latency_ms, top_product_ids, strategy_version)
                VALUES (?, ?, ?, ?, ?::jsonb, ?, ?, ?::jsonb, ?)
                ON CONFLICT (request_id) DO NOTHING
                """, options.requestId(), options.query(), effectiveQuery, options.locale(),
                json(filters), resultCount, latencyMs, json(productIds), strategyVersion);
    }

    public Map<String, Object> summary() {
        return jdbc.queryForMap("""
                SELECT COUNT(*)::bigint AS search_requests,
                       COALESCE(percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms), 0)::double precision AS p50_latency_ms,
                       COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms), 0)::double precision AS p95_latency_ms,
                       COUNT(*) FILTER (WHERE result_count = 0)::bigint AS zero_result_requests,
                       CASE WHEN COUNT(*) = 0 THEN 0 ELSE
                         COUNT(*) FILTER (WHERE result_count = 0)::double precision / COUNT(*)
                       END AS zero_result_rate
                FROM search_requests WHERE searched_at >= now() - interval '24 hours'
                """);
    }

    public List<Map<String, Object>> zeroResults(int limit) {
        return jdbc.queryForList("""
                SELECT query, COUNT(*)::bigint AS requests, MAX(searched_at) AS last_seen
                FROM search_requests WHERE result_count = 0
                GROUP BY query ORDER BY requests DESC, query LIMIT ?
                """, limit);
    }

    public List<Map<String, Object>> popular(int limit) {
        return jdbc.queryForList("""
                SELECT query, COUNT(*)::bigint AS requests,
                       AVG(latency_ms)::double precision AS avg_latency_ms,
                       COUNT(*) FILTER (WHERE result_count = 0)::bigint AS zero_results
                FROM search_requests GROUP BY query ORDER BY requests DESC, query LIMIT ?
                """, limit);
    }

    public List<Map<String, Object>> lowQuality(int limit, double threshold) {
        return jdbc.queryForList("""
                SELECT DISTINCT ON (query_id) query_id, query_text AS query, strategy_version,
                       precision10, recall10, mrr10, ndcg10, zero_result, evaluated_at
                FROM quality_metrics WHERE ndcg10 < ?
                ORDER BY query_id, evaluated_at DESC LIMIT ?
                """, threshold, limit);
    }

    public Map<String, Object> queryDetail(String query) {
        var traffic = jdbc.queryForList("""
                SELECT request_id, result_count, latency_ms, strategy_version, searched_at,
                       top_product_ids
                FROM search_requests WHERE lower(query) = lower(?)
                ORDER BY searched_at DESC LIMIT 50
                """, query);
        var quality = jdbc.queryForList("""
                SELECT query_id, strategy_version, precision10, recall10, mrr10, ndcg10,
                       zero_result, evaluated_at
                FROM quality_metrics WHERE lower(query_text) = lower(?)
                ORDER BY evaluated_at DESC LIMIT 20
                """, query);
        return Map.of("query", query, "recent_requests", traffic, "quality_history", quality);
    }

    private String json(Object value) {
        try {
            return mapper.writeValueAsString(value);
        } catch (JacksonException exception) {
            throw new IllegalArgumentException("Unable to serialize analytics JSON", exception);
        }
    }
}
