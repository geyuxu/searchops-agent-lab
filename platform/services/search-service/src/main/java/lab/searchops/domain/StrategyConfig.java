package lab.searchops.domain;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public record StrategyConfig(
        @Size(max = 100) Map<@NotBlank String, @Size(max = 20) List<@NotBlank String>> synonyms,
        @JsonProperty("rewrite_rules") @Size(max = 100) List<@Valid RewriteRule> rewriteRules,
        @JsonProperty("pinned_product_ids") @Size(max = 100) List<@NotBlank String> pinnedProductIds,
        @JsonProperty("blocked_product_ids") @Size(max = 1000) List<@NotBlank String> blockedProductIds,
        @JsonProperty("brand_boosts") @Size(max = 100) Map<@NotBlank String, @Min(0) @Max(100) Double> brandBoosts,
        @JsonProperty("field_weights") @Size(max = 10) Map<@NotBlank String, @Min(0) @Max(20) Double> fieldWeights,
        @JsonProperty("minimum_score") @Min(0) @Max(1000) Double minimumScore) {

    public record RewriteRule(@NotBlank @Size(max = 500) String match,
                              @NotBlank @Size(max = 500) String rewrite) {}

    public StrategyConfig {
        synonyms = synonyms == null ? Map.of() : Map.copyOf(synonyms);
        rewriteRules = rewriteRules == null ? List.of() : List.copyOf(rewriteRules);
        pinnedProductIds = pinnedProductIds == null ? List.of() : List.copyOf(pinnedProductIds);
        blockedProductIds = blockedProductIds == null ? List.of() : List.copyOf(blockedProductIds);
        brandBoosts = brandBoosts == null ? Map.of() : Map.copyOf(brandBoosts);
        fieldWeights = fieldWeights == null || fieldWeights.isEmpty()
                ? defaultFieldWeights() : Map.copyOf(fieldWeights);
        minimumScore = minimumScore == null ? 0.0 : minimumScore;
    }

    public static StrategyConfig baseline() {
        return new StrategyConfig(Map.of(), List.of(), List.of(), List.of(), Map.of(),
                defaultFieldWeights(), 0.0);
    }

    private static Map<String, Double> defaultFieldWeights() {
        var weights = new LinkedHashMap<String, Double>();
        weights.put("title", 4.0);
        weights.put("brand", 2.5);
        weights.put("bullet_point", 1.5);
        weights.put("description", 1.0);
        weights.put("category", 1.2);
        return weights;
    }
}

