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
    /**
     * 候选配置评测在响应里回填的 strategy_version 哨兵值。
     *
     * <p>候选配置根本没有版本号——它还没走过 create/submit/approve/publish，
     * 拿任何真实版本号回填都是在说谎。-1 与 {@code ProductSearchService.search(options, override)}
     * 给候选检索用的 SearchResponse.strategy_version 是同一个哨兵，全仓库只有这一种含义。
     */
    public static final int CANDIDATE_STRATEGY_VERSION = -1;

    /** EvaluationResult.strategy_source 在候选配置评测时的取值；已发布策略评测时该字段为 null。 */
    public static final String CANDIDATE_STRATEGY_SOURCE = "candidate";

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
        // candidate == null 是既有路径：评测当前**已发布**策略，下面每一处分支都退回原样。
        var candidate = request.strategyConfig();
        // 候选配置评测不读取任何 strategy：连当前版本号都不查，更不会创建/发布/回滚任何版本。
        var version = candidate == null ? strategies.current().version() : CANDIDATE_STRATEGY_VERSION;

        // 候选配置评测强制不落库（persist 一律视为 false），与请求里传的 persist 无关。
        //
        // 理由：quality_metrics 的唯一键是 (query_id, strategy_version)，而候选配置没有版本号。
        // 若允许落库，它只能借用某个版本号（或哨兵）写进去，于是这一轮候选跑的数字会通过
        // ON CONFLICT ... DO UPDATE 覆盖掉同一 (query_id, strategy_version) 下已发布策略的
        // 真实基线数据，且覆盖不可逆——一次参数扫描就能把整张质量基线表毁掉。
        // 同理，evaluation_runs 里也不该出现一条 strategy_version 指向"不存在的版本"的记录。
        var persist = request.persist() && candidate == null;

        var metrics = new ArrayList<QueryMetric>(request.queries().size());
        for (var query : request.queries()) {
            // 第 11 个实参是 SearchOptions.useAi。原来这里写死 false，配合 AiAdapterClient
            // 里的 `!aiEnabled() || !useAi()` 短路，导致 AI_ENABLED=true 也测不到 AI。
            // 现在跟随请求；请求缺省仍是 false，无 AI 基线因此保持可复现。
            var options = new SearchOptions(query.query(), "en-US", 0, 10, null, null,
                    null, null, null, "relevance", request.useAi(),
                    "evaluation-" + query.queryId(), false);
            // 候选配置走 ProductSearchService 的 override 重载——也就是 /strategies/preview
            // 干跑用的那条编译路径（SearchQueryCompiler.compile(options, config, rewritten)），
            // 不另起一套编译逻辑，preview 与离线评测因此不可能对同一份配置得出不同的查询。
            // 已发布策略仍走单参重载，调用形状与改动前逐字节一致。
            var response = candidate == null ? search.search(options) : search.search(options, candidate);
            var ranked = response.products().stream().map(product -> product.productId()).toList();
            var metric = metric(query, ranked);
            metrics.add(metric);
            if (persist) persist(metric, version);
        }
        var count = metrics.size();
        var run = new EvaluationResult(
                UUID.randomUUID(), version,
                candidate == null ? null : CANDIDATE_STRATEGY_SOURCE,
                // 只有"要求落库却被强制忽略"时才回填 true，其余情况为 null（响应里整键省略）。
                request.persist() && !persist ? Boolean.TRUE : null,
                count,
                average(metrics, QueryMetric::precision10),
                average(metrics, QueryMetric::recall10),
                average(metrics, QueryMetric::mrr10),
                average(metrics, QueryMetric::ndcg10),
                average(metrics, metric -> metric.zeroResult() ? 1.0 : 0.0),
                metrics, request.useAi(), OffsetDateTime.now());
        if (persist) {
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

    /**
     * 候选配置评测入口：对一个**尚未发布**的 strategy_config 跑整轮离线评测。
     *
     * <p>与 {@link #run(EvaluationRequest)} 共用同一条实现路径，只多一条前置校验：
     * strategy_config 必须存在。这条校验是给调用方（尤其是 Agent 工具网关）的护栏——
     * 少传一个字段就会静默变成"评测当前已发布策略"，Agent 会把别人的成绩当成自己提案的成绩。
     *
     * <p>安全等级 DRY_RUN：只读计算，不读取、不修改、不发布任何 strategy，也不写任何表
     * （落库由 {@link #run(EvaluationRequest)} 强制关闭，理由见那里的注释）。
     */
    public EvaluationResult runCandidate(EvaluationRequest request) {
        if (request.strategyConfig() == null) {
            throw new IllegalArgumentException(
                    "strategy_config is required for candidate evaluation; "
                            + "omit this endpoint to evaluate the published strategy");
        }
        return run(request);
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

