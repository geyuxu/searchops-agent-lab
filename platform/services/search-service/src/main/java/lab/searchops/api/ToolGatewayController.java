package lab.searchops.api;

import jakarta.validation.Valid;
import java.util.UUID;
import lab.searchops.domain.ApiModels.CreateStrategyRequest;
import lab.searchops.domain.ApiModels.EvaluationQuery;
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

    @PostMapping("/evaluations/query")
    public Object evaluate(@Valid @RequestBody EvaluationQuery query) {
        return evaluations.runOne(query);
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

