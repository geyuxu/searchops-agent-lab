package lab.searchops.api;

import jakarta.validation.Valid;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import lab.searchops.domain.ApiModels.CreateStrategyRequest;
import lab.searchops.domain.ApiModels.PreviewRequest;
import lab.searchops.domain.ApiModels.PreviewResponse;
import lab.searchops.domain.ApiModels.PublishRequest;
import lab.searchops.domain.ApiModels.RollbackRequest;
import lab.searchops.domain.ApiModels.SearchOptions;
import lab.searchops.domain.ApiModels.TransitionRequest;
import lab.searchops.service.ProductSearchService;
import lab.searchops.service.StrategyRepository;
import lab.searchops.service.StrategyService;
import org.slf4j.MDC;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1")
public class StrategyController {
    private final StrategyService strategies;
    private final StrategyRepository repository;
    private final ProductSearchService search;

    public StrategyController(StrategyService strategies, StrategyRepository repository,
            ProductSearchService search) {
        this.strategies = strategies;
        this.repository = repository;
        this.search = search;
    }

    @GetMapping("/strategies/current")
    public Object current() { return strategies.current(); }

    @GetMapping("/strategies")
    public Object history() { return strategies.history(); }

    @GetMapping("/strategies/{id}")
    public Object byId(@PathVariable UUID id) { return strategies.byId(id); }

    @PostMapping("/strategies")
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

    @PostMapping("/strategies/preview")
    public PreviewResponse preview(@Valid @RequestBody PreviewRequest request) {
        var locale = request.locale() == null ? "en-US" : request.locale();
        var options = new SearchOptions(request.query(), locale, 0, request.size(), null, null,
                null, null, null, "relevance", false, requestId(), false);
        var current = search.search(options);
        var proposed = search.search(options, request.config());
        var currentPositions = positions(current.products().stream().map(p -> p.productId()).toList());
        var proposedPositions = positions(proposed.products().stream().map(p -> p.productId()).toList());
        var changed = new ArrayList<Map<String, Object>>();
        var ids = new java.util.LinkedHashSet<String>();
        ids.addAll(currentPositions.keySet());
        ids.addAll(proposedPositions.keySet());
        for (var id : ids) {
            if (!java.util.Objects.equals(currentPositions.get(id), proposedPositions.get(id))) {
                var change = new LinkedHashMap<String, Object>();
                change.put("product_id", id);
                change.put("current_position", currentPositions.get(id));
                change.put("proposed_position", proposedPositions.get(id));
                changed.add(change);
            }
        }
        return new PreviewResponse(current, proposed, changed, true);
    }

    @GetMapping("/audit")
    public Object audit(@RequestParam(defaultValue = "100") int limit) {
        return repository.auditLog(Math.min(Math.max(limit, 1), 500));
    }

    private Map<String, Integer> positions(java.util.List<String> ids) {
        var result = new LinkedHashMap<String, Integer>();
        for (var index = 0; index < ids.size(); index++) result.put(ids.get(index), index + 1);
        return result;
    }

    private String requestId() {
        var value = MDC.get("requestId");
        return value == null ? "preview" : value;
    }
}

