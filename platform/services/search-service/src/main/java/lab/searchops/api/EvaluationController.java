package lab.searchops.api;

import jakarta.validation.Valid;
import lab.searchops.domain.ApiModels.EvaluationQuery;
import lab.searchops.domain.ApiModels.EvaluationRequest;
import lab.searchops.domain.ApiModels.EvaluationResult;
import lab.searchops.service.EvaluationService;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/evaluations")
public class EvaluationController {
    private final EvaluationService evaluations;

    public EvaluationController(EvaluationService evaluations) {
        this.evaluations = evaluations;
    }

    @PostMapping("/run")
    public EvaluationResult run(@Valid @RequestBody EvaluationRequest request) {
        return evaluations.run(request);
    }

    @PostMapping("/query")
    public EvaluationResult runOne(@Valid @RequestBody EvaluationQuery query) {
        return evaluations.runOne(query);
    }
}

