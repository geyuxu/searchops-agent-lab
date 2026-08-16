package lab.searchops.api;

import java.util.Map;
import lab.searchops.service.ProductSearchService;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/index")
public class IndexController {
    private final ProductSearchService search;

    public IndexController(ProductSearchService search) {
        this.search = search;
    }

    @PostMapping("/rebuild")
    public Map<String, Object> rebuild(@RequestBody(required = false) Map<String, String> request) {
        return search.rebuild(request == null ? null : request.get("path"));
    }
}

