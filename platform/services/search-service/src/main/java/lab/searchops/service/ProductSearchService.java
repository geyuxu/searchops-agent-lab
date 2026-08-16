package lab.searchops.service;

import tools.jackson.databind.JsonNode;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import lab.searchops.domain.ApiModels.FacetBucket;
import lab.searchops.domain.ApiModels.Product;
import lab.searchops.domain.ApiModels.SearchOptions;
import lab.searchops.domain.ApiModels.SearchResponse;
import lab.searchops.domain.StrategyConfig;
import org.springframework.stereotype.Service;

@Service
public class ProductSearchService {
    public static final String DATA_NOTICE = "Product text and relevance labels: public Amazon ESCI data. "
            + "Prices, inventory, traffic, users and orders: deterministic simulated data.";

    private final ElasticsearchGateway elasticsearch;
    private final SearchQueryCompiler compiler;
    private final StrategyService strategies;
    private final SearchAnalyticsRepository analytics;
    private final AiAdapterClient ai;

    public ProductSearchService(ElasticsearchGateway elasticsearch, SearchQueryCompiler compiler,
            StrategyService strategies, SearchAnalyticsRepository analytics, AiAdapterClient ai) {
        this.elasticsearch = elasticsearch;
        this.compiler = compiler;
        this.strategies = strategies;
        this.analytics = analytics;
        this.ai = ai;
    }

    public SearchResponse search(SearchOptions options) {
        return search(options, null);
    }

    public SearchResponse search(SearchOptions options, StrategyConfig override) {
        var started = System.nanoTime();
        var active = strategies.current();
        var strategy = override == null ? active.config() : override;
        var rewrite = ai.rewrite(options);

        // 检索后重排：先按候选深度 N 取回 BM25 结果，重排这 N 条，再截断到 size。
        //
        // 分页语义：plan 只在 page==0 时 active（判定见 AiAdapterClient.rerankPlan），
        // 因此 page>0 时 retrieval 与 options 是同一个对象，编译出的查询体与改动前逐字节相同，
        // 第 2 页起完全是原来的 BM25 分页。已知代价：第 1 页被重排后，它与第 2 页之间可能
        // 出现重复或遗漏（某条被从第 30 位提到第 3 位，它仍会按 BM25 顺序出现在第 2 页）。
        // 接受这个代价的理由：(a) 要根治必须缓存"本次候选集的重排结果"并让后续页从缓存切片，
        // 而 LLM 重排不保证确定性，没有缓存层时逐页重排只会让分页更乱；(b) 重排的价值窗口
        // 就是 top-10（NDCG@10 只看第一页，用户绝大多数也只看第一页）；(c) 只动第一页
        // 意味着对外分页契约、size 上限 100 的校验、from/size 的计算全部原封不动。
        var plan = ai.rerankPlan(options);
        var retrieval = plan.active() ? options.withSize(plan.depth()) : options;
        var compiled = compiler.compile(retrieval, strategy, rewrite.query());
        var raw = elasticsearch.search(compiled.body());
        var candidates = products(raw.path("hits").path("hits"));

        // 送去重排的是 effective query——也就是真正把这批候选检索出来的那段文本
        // （经过策略改写规则与 AI 改写）。用别的文本给候选排序，等于让重排在评价
        // 一个它没见过的检索意图。
        var rerank = ai.rerank(plan, compiled.effectiveQuery(), candidates, options.requestId());
        var products = page(rerank.products(), options.size());

        var total = raw.path("hits").path("total").path("value").asLong();
        var latency = Math.max(0, (System.nanoTime() - started) / 1_000_000);
        var version = override == null ? active.version() : -1;
        // rewrite.applied() 现在只在"改写结果 != 原始查询"时为 true；
        // AI 调用成功但没动查询会是 false + status=NO_CHANGE，调用失败则是 false + 具体降级原因。
        // 两个 *_model 与各自的 *_provider 并排透传：provider 说得出"哪个 provider 类跑的"，
        // model 才说得出"哪个模型跑的"。适配器没声明时它们是 null，键在响应里整体消失，
        // 因此不带 AI 的响应形状与新增这两个字段之前逐字节一致。
        var response = new SearchResponse(options.requestId(), options.query(), compiled.effectiveQuery(),
                total, options.page(), options.size(), latency, version, products, facets(raw),
                rewrite.applied(), rewrite.status(), rewrite.provider(), rewrite.model(),
                rerank.applied(), rerank.status(), rerank.provider(), rerank.model(),
                rerank.candidateCount(), DATA_NOTICE);
        if (options.logRequest() && override == null) {
            analytics.record(options, compiled.effectiveQuery(), total, latency,
                    products.stream().map(Product::productId).limit(10).toList(), version);
        }
        return response;
    }

    public Optional<Product> product(String id) {
        var raw = elasticsearch.getProduct(id);
        if (raw == null || !raw.path("found").asBoolean()) return Optional.empty();
        return Optional.of(product(raw.path("_source"), null));
    }

    public Map<String, Object> explain(String query, String productId, String requestId) {
        var options = new SearchOptions(query, "en-US", 0, 10, null, null, null, null,
                null, "relevance", false, requestId, false);
        var strategy = strategies.current();
        var compiled = compiler.compile(options, strategy.config(), query);
        var explanation = elasticsearch.explain(productId, compiled.query());
        return Map.of(
                "query", query,
                "effective_query", compiled.effectiveQuery(),
                "product_id", productId,
                "strategy_version", strategy.version(),
                "matched", explanation.path("matched").asBoolean(),
                "explanation", explanation.path("explanation"),
                "compiled_query", compiled.query());
    }

    public Map<String, Object> rebuild(String path) {
        return elasticsearch.rebuild(path);
    }

    /**
     * 截断到本页要返回的条数。
     *
     * <p>不重排时 ES 取回的就是 size 条，这里是恒等操作——所以不重排的响应形状与内容
     * 与改动前完全一致。重排时才真正把 N 条候选切回 size 条。
     */
    private List<Product> page(List<Product> products, int size) {
        var limit = Math.max(0, size);
        return products.size() <= limit ? products : List.copyOf(products.subList(0, limit));
    }

    private List<Product> products(JsonNode hits) {
        var products = new ArrayList<Product>();
        hits.forEach(hit -> products.add(product(hit.path("_source"), hit.path("_score").isNumber()
                ? hit.path("_score").asDouble() : null)));
        return products;
    }

    private Product product(JsonNode source, Double score) {
        return new Product(
                source.path("product_id").asText(),
                source.path("title").asText(),
                source.path("brand").asText(),
                source.path("description").asText(),
                source.path("bullet_point").asText(),
                source.path("color").asText(),
                source.path("locale").asText("us"),
                source.path("category").asText("Other"),
                source.path("price_cents").asInt(),
                source.path("currency").asText("USD"),
                source.path("inventory").asInt(),
                source.path("placeholder_hue").asInt(),
                source.path("provenance").asText("Amazon ESCI public dataset"),
                score);
    }

    private Map<String, List<FacetBucket>> facets(JsonNode response) {
        var result = new LinkedHashMap<String, List<FacetBucket>>();
        result.put("brands", buckets(response.path("aggregations").path("brands").path("buckets")));
        result.put("categories", buckets(response.path("aggregations").path("categories").path("buckets")));
        return result;
    }

    private List<FacetBucket> buckets(JsonNode nodes) {
        var values = new ArrayList<FacetBucket>();
        nodes.forEach(node -> values.add(new FacetBucket(node.path("key").asText(),
                node.path("doc_count").asLong())));
        return values;
    }
}
