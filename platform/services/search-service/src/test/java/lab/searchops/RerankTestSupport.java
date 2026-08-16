package lab.searchops;

import java.util.AbstractList;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Collectors;
import java.util.stream.IntStream;
import lab.searchops.config.RerankProperties;
import lab.searchops.domain.ApiModels.Product;
import lab.searchops.domain.ApiModels.SearchOptions;
import lab.searchops.service.AiAdapterClient;
import org.springframework.test.web.client.MockRestServiceServer;
import org.springframework.web.client.RestClient;

/**
 * 重排相关单元测试的公共装配，与 {@link AiTestSupport} 同一套思路：
 * 不启动 Spring 上下文、不启动 Testcontainers，打桩只打到 HTTP 这一层。
 *
 * <p>为什么另起一个类而不是往 AiTestSupport 里加：重排走的是**第二个** RestClient
 * （{@code aiRerankRestClient}，读超时预算与改写完全独立，见 HttpConfig 的注释），
 * 因此装配要同时绑两台 MockRestServiceServer——改写那台必须能独立断言"一次都没被调用"。
 * 把这套装配塞进 AiTestSupport 会让既有改写测试的 Wiring 形状被迫改变。
 */
final class RerankTestSupport {
    /** 与 AiAdapterClient 里硬编码的路径一致；写错会让"没调用重排"伪装成通过。 */
    static final String RERANK_URL = AiTestSupport.AI_BASE_URL + "/ai/rerank";

    private RerankTestSupport() {}

    /** 全局 AI 与重排都开着的默认装配。 */
    static Wiring rerankClient() {
        return rerankClient(true, RerankProperties.defaults());
    }

    /**
     * 造一个真实的 AiAdapterClient，改写与重排各接一台 MockRestServiceServer。
     *
     * <p>不 mock AiAdapterClient 的理由同 AiTestSupport：本次要守的全部逻辑
     * （排列不变量、降级分类、NO_CHANGE 的窗口口径、并发预算）都在它内部。
     */
    static Wiring rerankClient(boolean aiEnabled, RerankProperties rerank) {
        var rewriteBuilder = RestClient.builder().baseUrl(AiTestSupport.AI_BASE_URL);
        var rewriteServer = MockRestServiceServer.bindTo(rewriteBuilder).build();
        var rerankBuilder = RestClient.builder().baseUrl(AiTestSupport.AI_BASE_URL);
        var rerankServer = MockRestServiceServer.bindTo(rerankBuilder).build();
        var client = new AiAdapterClient(rewriteBuilder.build(), rerankBuilder.build(),
                AiTestSupport.properties(aiEnabled), rerank);
        return new Wiring(client, rewriteServer, rerankServer);
    }

    /** 重排配置的便捷构造；四个分量的含义见 RerankProperties。 */
    static RerankProperties rerankProperties(boolean enabled, int depth, int maxConcurrency) {
        return new RerankProperties(enabled, depth, RerankProperties.DEFAULT_TIMEOUT_MS,
                maxConcurrency);
    }

    /** 一次搜索请求；useAi 固定为 false，这样改写链路不会发出任何 HTTP，噪音全部排除。 */
    static SearchOptions rerankOptions(String query, int page, int size, String sort,
            boolean useRerank, Integer rerankDepth) {
        return new SearchOptions(query, "en-US", page, size, null, null, null, null, null, sort,
                false, "request-under-test", false, useRerank, rerankDepth);
    }

    /** page=0 / size=10 / relevance 的常规重排请求。 */
    static SearchOptions rerankOptions(boolean useRerank) {
        return rerankOptions("smart tv", 0, 10, "relevance", useRerank, null);
    }

    /**
     * 改动前那条构造路径造出来的请求（13 参兼容构造器 == 没有重排这回事）。
     *
     * <p>"重排关闭时链路逐字节等价"这条断言拿它当参照物：它是既有调用点的原样写法。
     */
    static SearchOptions legacyOptions(String query, int page, int size) {
        return new SearchOptions(query, "en-US", page, size, null, null, null, null, null,
                "relevance", false, "request-under-test", false);
    }

    static Product product(String id) {
        return product(id, id + " title");
    }

    /** 同 id 不同 title：用来构造"候选集里出现重复 id"这种病态但可能的输入。 */
    static Product product(String id, String title) {
        return new Product(id, title, "Acme", "description of " + title, "bullet", "black", "us",
                "Electronics", 1999, "USD", 3, 120, "test", 1.5);
    }

    static List<Product> candidates(String... ids) {
        var products = new ArrayList<Product>(ids.length);
        for (var index = 0; index < ids.length; index++) {
            // title 带上序号，这样即使 id 重复，两条记录依然是不同的 Product 值，
            // "集合相同"的断言才真的在比文档而不是在比 id。
            products.add(product(ids[index], "#" + index + " " + ids[index]));
        }
        return List.copyOf(products);
    }

    /** P01..P{count} 的候选集，顺序即 BM25 顺序。 */
    static List<Product> candidates(int count) {
        return candidates(productIds(count).toArray(String[]::new));
    }

    static List<String> productIds(int count) {
        return IntStream.rangeClosed(1, count).mapToObj("P%02d"::formatted).toList();
    }

    static List<String> ids(List<Product> products) {
        return products.stream().map(Product::productId).toList();
    }

    static List<String> reversed(List<String> values) {
        var copy = new ArrayList<>(values);
        java.util.Collections.reverse(copy);
        return List.copyOf(copy);
    }

    /** 适配器契约里的成功响应；provider 为 null 时整个字段缺席。 */
    static String rerankResponse(String provider, List<String> rankedIds) {
        var ids = rankedIds.stream().map(id -> "\"%s\"".formatted(id))
                .collect(Collectors.joining(","));
        var providerField = provider == null ? "" : ",\"provider\":\"%s\"".formatted(provider);
        return "{\"ranked_product_ids\":[%s]%s,\"latency_ms\":42}".formatted(ids, providerField);
    }

    record Wiring(AiAdapterClient client, MockRestServiceServer rewriteServer,
            MockRestServiceServer rerankServer) {}

    /**
     * 一个会在被读到第 {@code shrinkOnRead} 次末位元素时**悄悄少掉一条**的候选列表。
     *
     * <p>存在的唯一理由：{@link lab.searchops.domain.AiRerankStatus#INTERNAL_ERROR} 这条分支
     * 只有在 {@link lab.searchops.service.RerankOrdering} 的排列不变量被破坏时才可达，而正常
     * 输入下它**永远不可达**（这正是那段代码的目的）。要证明"自身 bug"与"模型返回非法"
     * 被分成了两个状态，就必须从外部制造一次不变量破坏——测试不能改主源码，于是只能让
     * 候选集自己在遍历途中变形。
     *
     * <p>为什么是"读末位元素第 N 次"而不是"第 N 次 get"：前者是语义化的触发点——每一趟完整
     * 遍历恰好读末位一次，所以 N 就是"第几趟遍历结束时收缩"。reorder 内部是两趟
     * （建索引一趟、补齐未点名的一趟），AiAdapterClient.rerank 在 reorder 之前还会为
     * 构造请求体多遍历一趟。{@link #shrunk()} 让测试能断言"确实触发了"，万一将来遍历趟数
     * 变了，测试会以"没触发"这个明确的信号变红，而不是悄悄从别的路径蒙混过关。
     */
    static final class ShrinkingCandidateList extends AbstractList<Product> {
        private final List<Product> delegate;
        private final int lastIndex;
        private final int shrinkOnRead;
        private int lastIndexReads;
        private boolean shrunk;

        ShrinkingCandidateList(List<Product> products, int shrinkOnRead) {
            this.delegate = new ArrayList<>(products);
            this.lastIndex = products.size() - 1;
            this.shrinkOnRead = shrinkOnRead;
        }

        @Override
        public Product get(int index) {
            var value = delegate.get(index);
            if (index == lastIndex && ++lastIndexReads == shrinkOnRead) {
                delegate.remove(delegate.size() - 1);
                shrunk = true;
            }
            return value;
        }

        @Override
        public int size() {
            return delegate.size();
        }

        /** 是否真的收缩过；用来确认这次测试走的就是"不变量被破坏"那条路。 */
        boolean shrunk() {
            return shrunk;
        }
    }
}
