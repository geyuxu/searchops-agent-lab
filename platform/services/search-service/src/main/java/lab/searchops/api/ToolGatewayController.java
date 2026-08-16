package lab.searchops.api;

import jakarta.validation.Valid;
import java.util.UUID;
import lab.searchops.domain.ApiModels.CreateStrategyRequest;
import lab.searchops.domain.ApiModels.EvaluationQuery;
import lab.searchops.domain.ApiModels.EvaluationRequest;
import lab.searchops.domain.ApiModels.PreviewRequest;
import lab.searchops.domain.ApiModels.PublishRequest;
import lab.searchops.domain.ApiModels.RollbackRequest;
import lab.searchops.domain.ApiModels.TransitionRequest;
import lab.searchops.service.EvaluationService;
import lab.searchops.service.SearchAnalyticsRepository;
import lab.searchops.service.StrategyService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/tools")
public class ToolGatewayController {
    private final SearchAnalyticsRepository analytics;
    private final StrategyService strategies;
    private final StrategyController strategyController;
    private final EvaluationService evaluations;

    public ToolGatewayController(SearchAnalyticsRepository analytics, StrategyService strategies,
            StrategyController strategyController, EvaluationService evaluations) {
        this.analytics = analytics;
        this.strategies = strategies;
        this.strategyController = strategyController;
        this.evaluations = evaluations;
    }

    @GetMapping("/query-metrics")
    public Object queryMetrics(@RequestParam String query) { return analytics.queryDetail(query); }

    @GetMapping("/zero-result-queries")
    public Object zeroResults(@RequestParam(defaultValue = "50") int limit) {
        return analytics.zeroResults(Math.min(Math.max(limit, 1), 500));
    }

    @GetMapping("/low-quality-queries")
    public Object lowQuality(@RequestParam(defaultValue = "50") int limit) {
        return analytics.lowQuality(Math.min(Math.max(limit, 1), 500), 0.5);
    }

    @GetMapping("/strategies/current")
    public Object current() { return strategies.current(); }

    @GetMapping("/strategies/history")
    public Object history() { return strategies.history(); }

    @PostMapping("/strategies/preview")
    public Object preview(@Valid @RequestBody PreviewRequest request) {
        return strategyController.preview(request);
    }

    /**
     * 与 /api/v1/evaluations/query 行为对齐：同样接受可选的 use_ai 查询参数，默认 false。
     * 代理商不传参时，工具网关的行为与既有契约（searchops-tools.openapi.json）完全一致。
     */
    @PostMapping("/evaluations/query")
    public Object evaluate(@Valid @RequestBody EvaluationQuery query,
            @RequestParam(name = "use_ai", defaultValue = "false") boolean useAi) {
        return evaluations.runOne(query, useAi);
    }

    /**
     * 候选策略配置的整轮离线评测。安全等级 <b>DRY_RUN</b>，与上面的 /strategies/preview 同级：
     * 只做只读计算，不写任何状态——不建草稿、不发布、不回滚，也不写 quality_metrics /
     * evaluation_runs（落库由 EvaluationService 强制关闭，理由见那里的注释）。
     *
     * <p>补上的是 Agent 自证闭环最后一环：在这之前 Agent 只能 preview 单条查询，
     * 想拿整轮 NDCG@10 证明自己的提案，就必须先把提案发布出去——等于让未经审批的配置先上线。
     *
     * <p>请求体沿用 EvaluationRequest，但这里 strategy_config 是必填：少传会 400，
     * 而不是静默退化成"评测当前已发布策略"。响应用 strategy_version=-1 与
     * strategy_source="candidate" 标记这不是任何已发布版本的成绩。
     */
    @PostMapping("/evaluations/candidate")
    public Object evaluateCandidate(@Valid @RequestBody EvaluationRequest request) {
        return evaluations.runCandidate(request);
    }

    @PostMapping("/strategies/drafts")
    public Object create(@RequestHeader("Idempotency-Key") String key,
            @Valid @RequestBody CreateStrategyRequest request) {
        return strategies.create(key, request);
    }

    @PostMapping("/strategies/{id}/submit")
    public Object submit(@PathVariable UUID id, @RequestHeader("Idempotency-Key") String key,
            @Valid @RequestBody TransitionRequest request) {
        return strategies.submit(id, key, request);
    }

    @PostMapping("/strategies/{id}/approve")
    public Object approve(@PathVariable UUID id, @RequestHeader("Idempotency-Key") String key,
            @Valid @RequestBody TransitionRequest request) {
        return strategies.approve(id, key, request);
    }

    @PostMapping("/strategies/{id}/publish")
    public Object publish(@PathVariable UUID id, @RequestHeader("Idempotency-Key") String key,
            @Valid @RequestBody PublishRequest request) {
        return strategies.publish(id, key, request);
    }

    @PostMapping("/strategies/rollback")
    public Object rollback(@RequestHeader("Idempotency-Key") String key,
            @Valid @RequestBody RollbackRequest request) {
        return strategies.rollback(key, request);
    }
}

