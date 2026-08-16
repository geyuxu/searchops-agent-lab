package lab.searchops.service;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import lab.searchops.domain.ApiModels.EvaluationQuery;
import lab.searchops.domain.ApiModels.EvaluationRequest;
import lab.searchops.domain.ApiModels.EvaluationResult;
import lab.searchops.domain.ApiModels.QueryMetric;
import lab.searchops.domain.ApiModels.SearchOptions;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class EvaluationService {
    private final ProductSearchService search;
    private final StrategyService strategies;
    private final JdbcTemplate jdbc;

    public EvaluationService(ProductSearchService search, StrategyService strategies, JdbcTemplate jdbc) {
        this.search = search;
        this.strategies = strategies;
        this.jdbc = jdbc;
    }

    @Transactional
    public EvaluationResult run(EvaluationRequest request) {
        if (request.k() != 10) {
            throw new IllegalArgumentException("This baseline endpoint evaluates exactly k=10");
        }
        var version = strategies.current().version();
        var metrics = new ArrayList<QueryMetric>(request.queries().size());
        for (var query : request.queries()) {
            // 第 11 个实参是 SearchOptions.useAi。原来这里写死 false，配合 AiAdapterClient
            // 里的 `!aiEnabled() || !useAi()` 短路，导致 AI_ENABLED=true 也测不到 AI。
            // 现在跟随请求；请求缺省仍是 false，无 AI 基线因此保持可复现。
            var options = new SearchOptions(query.query(), "en-US", 0, 10, null, null,
                    null, null, null, "relevance", request.useAi(),
                    "evaluation-" + query.queryId(), false);
            var response = search.search(options);
            var ranked = response.products().stream().map(product -> product.productId()).toList();
            var metric = metric(query, ranked);
            metrics.add(metric);
            if (request.persist()) persist(metric, version);
        }
        var count = metrics.size();
        var run = new EvaluationResult(
                UUID.randomUUID(), version, count,
                average(metrics, QueryMetric::precision10),
                average(metrics, QueryMetric::recall10),
                average(metrics, QueryMetric::mrr10),
                average(metrics, QueryMetric::ndcg10),
                average(metrics, metric -> metric.zeroResult() ? 1.0 : 0.0),
                metrics, request.useAi(), OffsetDateTime.now());
        if (request.persist()) {
            jdbc.update("""
                    INSERT INTO evaluation_runs
                      (id, strategy_version, query_count, precision10, recall10, mrr10, ndcg10, zero_result_rate)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, run.runId(), run.strategyVersion(), run.queryCount(), run.precision10(),
                    run.recall10(), run.mrr10(), run.ndcg10(), run.zeroResultRate());
        }
        return run;
    }

    public List<Map<String, Object>> latestRuns(int limit) {
        return jdbc.queryForList("""
                SELECT id AS run_id, strategy_version, query_count, precision10, recall10,
                       mrr10, ndcg10, zero_result_rate, created_at
                FROM evaluation_runs ORDER BY created_at DESC LIMIT ?
                """, limit);
    }

    /** 单条评测，不走 AI。保留这个重载是为了让既有调用方（工具网关契约）行为完全不变。 */
    public EvaluationResult runOne(EvaluationQuery query) {
        return runOne(query, false);
    }

    /**
     * 单条评测。与 {@link #run(EvaluationRequest)} 共用同一条实现路径，
     * 保证 /evaluations/run 与 /evaluations/query 两条入口对 useAi 的处理完全一致。
     */
    public EvaluationResult runOne(EvaluationQuery query, boolean useAi) {
        return run(new EvaluationRequest(List.of(query), 10, false, useAi));
    }

    private QueryMetric metric(EvaluationQuery query, List<String> ranked) {
        var relevantTotal = query.judgments().values().stream().filter(this::relevant).count();
        var relevantRetrieved = 0;
        var reciprocalRank = 0.0;
        var dcg = 0.0;
        for (var index = 0; index < Math.min(10, ranked.size()); index++) {
            var label = query.judgments().getOrDefault(ranked.get(index), "I");
            if (relevant(label)) {
                relevantRetrieved++;
                if (reciprocalRank == 0) reciprocalRank = 1.0 / (index + 1);
            }
            dcg += gain(label) / log2(index + 2);
        }
        var ideal = query.judgments().values().stream()
                .sorted(Comparator.comparingInt(this::gain).reversed())
                .limit(10).toList();
        var idcg = 0.0;
        for (var index = 0; index < ideal.size(); index++) {
            idcg += gain(ideal.get(index)) / log2(index + 2);
        }
        return new QueryMetric(
                query.queryId(), query.query(),
                relevantRetrieved / 10.0,
                relevantTotal == 0 ? 0 : relevantRetrieved / (double) relevantTotal,
                reciprocalRank,
                idcg == 0 ? 0 : dcg / idcg,
                ranked.isEmpty());
    }

    private void persist(QueryMetric metric, int strategyVersion) {
        jdbc.update("""
                INSERT INTO quality_metrics
                  (query_id, query_text, strategy_version, precision10, recall10, mrr10, ndcg10, zero_result)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (query_id, strategy_version) DO UPDATE SET
                  query_text = EXCLUDED.query_text,
                  precision10 = EXCLUDED.precision10,
                  recall10 = EXCLUDED.recall10,
                  mrr10 = EXCLUDED.mrr10,
                  ndcg10 = EXCLUDED.ndcg10,
                  zero_result = EXCLUDED.zero_result,
                  evaluated_at = now()
                """, metric.queryId(), metric.query(), strategyVersion, metric.precision10(),
                metric.recall10(), metric.mrr10(), metric.ndcg10(), metric.zeroResult());
    }

    private boolean relevant(String label) {
        return "E".equals(label) || "S".equals(label);
    }

    private int gain(String label) {
        return switch (label) {
            case "E" -> 3;
            case "S" -> 2;
            case "C" -> 1;
            default -> 0;
        };
    }

    private double log2(double value) {
        return Math.log(value) / Math.log(2);
    }

    private double average(List<QueryMetric> metrics,
            java.util.function.ToDoubleFunction<QueryMetric> extractor) {
        return metrics.stream().mapToDouble(extractor).average().orElse(0);
    }
}

