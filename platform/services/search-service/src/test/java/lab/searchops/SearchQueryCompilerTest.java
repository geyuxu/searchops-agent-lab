package lab.searchops;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;
import java.util.Map;
import lab.searchops.domain.ApiModels.SearchOptions;
import lab.searchops.domain.StrategyConfig;
import lab.searchops.service.SearchQueryCompiler;
import org.junit.jupiter.api.Test;

class SearchQueryCompilerTest {
    private final SearchQueryCompiler compiler = new SearchQueryCompiler();

    @Test
    void compilesGovernedPolicyIntoElasticsearchQuery() {
        var config = new StrategyConfig(
                Map.of("tv", List.of("television")),
                List.of(new StrategyConfig.RewriteRule("smart tv", "4k tv")),
                List.of("pinned-1"), List.of("blocked-1"), Map.of("Acme", 2.0),
                Map.of("title", 5.0, "brand", 2.0), 0.25);
        var options = new SearchOptions("smart tv", "en-US", 0, 10, "Acme", null,
                null, 50000, true, "relevance", false, "test", false);

        var compiled = compiler.compile(options, config, null);

        assertThat(compiled.effectiveQuery()).isEqualTo("4k tv");
        assertThat(compiled.body()).containsEntry("min_score", 0.25);
        assertThat(compiled.body().toString()).contains("pinned-1", "blocked-1", "brand.keyword",
                "price_cents", "inventory", "title^5.0");
    }

    @Test
    void blankQueryUsesMatchAllAndSupportsDeterministicSort() {
        var options = new SearchOptions("", "en-US", 0, 24, null, null,
                null, null, null, "price_asc", false, "test", false);
        var compiled = compiler.compile(options, StrategyConfig.baseline(), null);
        assertThat(compiled.body().toString())
                .contains("match_all", "price_cents=asc", "product_id=asc")
                .doesNotContain("{_id=asc}");
    }
}
