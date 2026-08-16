package lab.searchops.service;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import lab.searchops.config.RerankProperties;
import lab.searchops.domain.ApiModels.EvaluationQuery;
import lab.searchops.domain.ApiModels.EvaluationRequest;
import lab.searchops.domain.ApiModels.EvaluationResult;
import lab.searchops.domain.ApiModels.QueryMetric;
import lab.searchops.domain.ApiModels.SearchOptions;
import org.springframework.beans.factory.annotation.Autowired;
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

    /** 评测固定 k=10，因此本页窗口恒为 10；候选深度的下限由它决定。 */
    private static final int EVALUATION_PAGE_SIZE = 10;

    private final ProductSearchService search;
    private final StrategyService strategies;
    private final JdbcTemplate jdbc;
    private final RerankProperties rerankProperties;

    @Autowired
    public EvaluationService(ProductSearchService search, StrategyService strategies,
            JdbcTemplate jdbc, RerankProperties rerankProperties) {
        this.search = search;
        this.strategies = strategies;
        this.jdbc = jdbc;
        this.rerankProperties = rerankProperties;
    }

    /**
     * 兼容构造器：重排配置取默认值。
     *
     * <p>只影响结果里回显的 rerank_depth（请求没显式指定深度时回显服务端默认值），
     * 不影响任何指标计算——真正决定检索深度的是 ProductSearchService 那条链路上的配置。
     */
    public EvaluationService(ProductSearchService search, StrategyService strategies, JdbcTemplate jdbc) {
        this(search, strategies, jdbc, RerankProperties.defaults());
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
        // 本轮真正跑过的模型标识，逐条查询从 SearchResponse 上观测。
        //
        // 为什么用 LinkedHashSet 而不是"取第一个非空值"：正常情况下整轮只会有一个模型，
        // 但适配器是一个可以被重启/改配置的独立进程，一轮 600 条查询要跑几分钟。中途换了模型
        // 而产物只记第一个，正是这次要修的缺陷（产物声称的模型不是跑出数字的模型）的另一种形态。
        // 出现多个就把它们全都记上，让产物自己说"这轮不干净"，而不是替它挑一个。
        // 保持插入顺序是为了让同一轮跑出来的产物可逐字节比对，不受 HashSet 迭代顺序影响。
        var rewriteModels = new LinkedHashSet<String>();
        var rerankModels = new LinkedHashSet<String>();
        for (var query : request.queries()) {
            // 第 11 个实参是 SearchOptions.useAi。原来这里写死 false，配合 AiAdapterClient
            // 里的 `!aiEnabled() || !useAi()` 短路，导致 AI_ENABLED=true 也测不到 AI。
            // 现在跟随请求；请求缺省仍是 false，无 AI 基线因此保持可复现。
            // 第 11 个实参是 useAi，第 14/15 个是重排开关与候选深度。
            // 评测走的是 page=0 + size=10 + relevance 排序，正好落在重排生效的条件里
            // （见 AiAdapterClient.rerankPlan）：取回 N 条候选、重排、截回 10 条，
            // 于是 NDCG@10 / Recall@10 量到的就是重排对 top-10 的真实影响。
            var options = new SearchOptions(query.query(), "en-US", 0, 10, null, null,
                    null, null, null, "relevance", request.useAi(),
                    "evaluation-" + query.queryId(), false,
                    request.useRerank(), request.rerankDepth());
            // 候选配置走 ProductSearchService 的 override 重载——也就是 /strategies/preview
            // 干跑用的那条编译路径（SearchQueryCompiler.compile(options, config, rewritten)），
            // 不另起一套编译逻辑，preview 与离线评测因此不可能对同一份配置得出不同的查询。
            // 已发布策略仍走单参重载，调用形状与改动前逐字节一致。
            var response = candidate == null ? search.search(options) : search.search(options, candidate);
            collect(rewriteModels, response.aiModel());
            collect(rerankModels, response.rerankModel());
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
                metrics, request.useAi(),
                // 重排关闭时这两个键在响应里整体消失（non_null inclusion），
                // 既有产物的 JSON 形状因此逐字节不变；开启时它们让产物自证条件。
                request.useRerank() ? Boolean.TRUE : null,
                // 回显**实际生效**的深度而不是请求里那个可能为 null 的值：产物要能自证条件，
                // "没写深度"和"深度 50"必须在归档里看得出区别。
                request.useRerank()
                        ? rerankProperties.effectiveDepth(request.rerankDepth(), EVALUATION_PAGE_SIZE)
                        : null,
                // 观测到的模型标识。一次都没观测到就是 null（键整体消失）——无 AI 的基线跑
                // 因此与新增这两个字段之前逐字节一致，baselines/ 与 experiments/ 下的既有归档仍可直接 diff。
                observed(rewriteModels), observed(rerankModels),
                OffsetDateTime.now());
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
        return runOne(query, useAi, false);
    }

    /**
     * 单条评测，可分别开关改写与重排。
     *
     * <p>两个开关刻意分开：只有能跑出"只开重排"这一组，重排的收益才是可归因的。
     */
    public EvaluationResult runOne(EvaluationQuery query, boolean useAi, boolean useRerank) {
        return run(new EvaluationRequest(List.of(query), 10, false, useAi, null, useRerank, null));
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

    /** 把一条查询观测到的模型标识收进集合；没有标识（未调用/降级/适配器没声明）就什么都不记。 */
    private void collect(Set<String> models, String model) {
        if (model != null && !model.isBlank()) models.add(model);
    }

    /**
     * 本轮观测到的模型标识，写进 EvaluationResult。
     *
     * <p>一个都没有 → null，键在响应里整体消失。**刻意不填 "unknown" 之类的占位值**：
     * 这个字段是给"这组数字是谁跑出来的"当证据用的，一个编造的值在归档里与真实模型名无法区分。
     *
     * <p>多于一个 → 逗号连接全部列出。这只在一轮评测中途适配器换了模型时才可能发生，
     * 是产物不干净的信号，必须原样暴露而不是替读者挑一个。
     */
    private String observed(Set<String> models) {
        return models.isEmpty() ? null : String.join(",", models);
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

