package lab.searchops.api;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.Size;
import java.util.Map;
import lab.searchops.service.EvaluationService;
import lab.searchops.service.SearchAnalyticsRepository;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/ops")
@Validated
public class OperationsController {
    private final SearchAnalyticsRepository analytics;
    private final EvaluationService evaluations;

    public OperationsController(SearchAnalyticsRepository analytics, EvaluationService evaluations) {
        this.analytics = analytics;
        this.evaluations = evaluations;
    }

    @GetMapping("/metrics")
    public Map<String, Object> summary() { return analytics.summary(); }

    @GetMapping("/zero-results")
    public Object zeroResults(@RequestParam(defaultValue = "50") @Min(1) @Max(500) int limit) {
        return analytics.zeroResults(limit);
    }

    @GetMapping("/popular-queries")
    public Object popular(@RequestParam(defaultValue = "50") @Min(1) @Max(500) int limit) {
        return analytics.popular(limit);
    }

    @GetMapping("/low-quality")
    public Object lowQuality(
            @RequestParam(defaultValue = "50") @Min(1) @Max(500) int limit,
            @RequestParam(defaultValue = "0.5") @Min(0) double threshold) {
        return analytics.lowQuality(limit, threshold);
    }

    @GetMapping("/query")
    public Object query(@RequestParam @Size(min = 1, max = 500) String query) {
        return analytics.queryDetail(query);
    }

    @GetMapping("/evaluation-runs")
    public Object evaluationRuns(@RequestParam(defaultValue = "20") @Min(1) @Max(100) int limit) {
        return evaluations.latestRuns(limit);
    }
}

