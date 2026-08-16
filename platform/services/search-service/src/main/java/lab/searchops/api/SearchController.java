package lab.searchops.api;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import java.util.Map;
import lab.searchops.domain.ApiModels.SearchOptions;
import lab.searchops.domain.ApiModels.SearchResponse;
import lab.searchops.service.ProductSearchService;
import org.slf4j.MDC;
import org.springframework.http.ResponseEntity;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1")
@Validated
public class SearchController {
    private final ProductSearchService search;

    public SearchController(ProductSearchService search) {
        this.search = search;
    }

    @GetMapping("/search")
    public SearchResponse search(
            @RequestParam(name = "q", defaultValue = "") @Size(max = 500) String query,
            @RequestParam(defaultValue = "en-US") @Size(max = 20) String locale,
            @RequestParam(defaultValue = "0") @Min(0) int page,
            @RequestParam(defaultValue = "24") @Min(1) @Max(100) int size,
            @RequestParam(required = false) @Size(max = 300) String brand,
            @RequestParam(required = false) @Size(max = 300) String category,
            @RequestParam(name = "price_min", required = false) @Min(0) Integer priceMin,
            @RequestParam(name = "price_max", required = false) @Min(0) Integer priceMax,
            @RequestParam(name = "in_stock", required = false) Boolean inStock,
            @RequestParam(defaultValue = "relevance") String sort,
            @RequestParam(name = "use_ai", defaultValue = "false") boolean useAi,
            // 重排开关，与 use_ai 完全独立：可以只开其中一个。默认 false，
            // 因此不带这个参数的既有调用方（前台、后台、评测脚本）行为一个字节都没变。
            @RequestParam(name = "rerank", defaultValue = "false") boolean rerank,
            // 候选深度 N 的请求级覆盖，缺省用 lab.search.rerank.depth（默认 50）。
            // 上限 200 是 AI 适配器 RerankRequest.candidates 的契约上限，超了会被 422。
            // 注意它与 size 是两回事：size 仍是对外返回的条数（上限 100），
            // 重排先取 N 条候选、重排、再截断到 size，分页语义不受影响。
            @RequestParam(name = "rerank_depth", required = false) @Min(1) @Max(200)
                    Integer rerankDepth) {
        return search.search(new SearchOptions(query, locale, page, size, brand, category,
                priceMin, priceMax, inStock, sort, useAi, requestId(), true, rerank, rerankDepth));
    }

    @GetMapping("/products/{productId}")
    public ResponseEntity<?> product(@PathVariable @NotBlank String productId) {
        return search.product(productId).<ResponseEntity<?>>map(ResponseEntity::ok)
                .orElseGet(() -> ResponseEntity.notFound().build());
    }

    @GetMapping("/search/explain")
    public Map<String, Object> explain(
            @RequestParam(name = "q") @NotBlank @Size(max = 500) String query,
            @RequestParam(name = "product_id") @NotBlank String productId) {
        return search.explain(query, productId, requestId());
    }

    private String requestId() {
        var value = MDC.get("requestId");
        return value == null ? "unknown" : value;
    }
}

