package lab.searchops.api;

import jakarta.validation.Valid;
import lab.searchops.domain.ApiModels.EvaluationQuery;
import lab.searchops.domain.ApiModels.EvaluationRequest;
import lab.searchops.domain.ApiModels.EvaluationResult;
import lab.searchops.service.EvaluationService;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
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

    /**
     * use_ai / use_rerank 走查询参数而不是请求体：EvaluationQuery 的请求体形状已经冻结在
     * searchops-tools.openapi.json 里，加可选查询参数不动契约。两者默认 false 且互相独立，
     * 与 /run 以及 SearchController 的 use_ai / rerank 参数保持同一套默认语义。
     */
    @PostMapping("/query")
    public EvaluationResult runOne(@Valid @RequestBody EvaluationQuery query,
            @RequestParam(name = "use_ai", defaultValue = "false") boolean useAi,
            @RequestParam(name = "use_rerank", defaultValue = "false") boolean useRerank) {
        return evaluations.runOne(query, useAi, useRerank);
    }
}

