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
            @RequestParam(name = "use_ai", defaultValue = "false") boolean useAi) {
        return search.search(new SearchOptions(query, locale, page, size, brand, category,
                priceMin, priceMax, inStock, sort, useAi, requestId(), true));
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

