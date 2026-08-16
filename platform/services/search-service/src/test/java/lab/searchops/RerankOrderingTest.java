package lab.searchops;

import static lab.searchops.RerankTestSupport.candidates;
import static lab.searchops.RerankTestSupport.ids;
import static lab.searchops.RerankTestSupport.product;
import static lab.searchops.RerankTestSupport.productIds;
import static lab.searchops.RerankTestSupport.reversed;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.catchThrowable;
import static org.junit.jupiter.params.provider.Arguments.arguments;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Random;
import java.util.stream.Stream;
import lab.searchops.domain.ApiModels.Product;
import lab.searchops.service.RerankOrdering;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;

/**
 * 重排合并逻辑的不变量测试：<b>输出永远是输入的一个排列</b>。
 *
 * <h2>为什么这是本轮最重要的一组测试</h2>
 * 重排只对"顺序"负责，对"召回"不负任何责任。一旦 {@link RerankOrdering} 能让文档消失，
 * 模型的一次幻觉（少返一个 id、返回一个不存在的 product_id、把 id 拼错一个字符）就会变成
 * 一次**静默的召回下降**：用户搜不到本来搜得到的商品，评测里表现为 Recall@10 下跌，
 * 而故障的形状与"BM25 根本没召回"一模一样——排查会被引向索引和查询编译，
 * 没有人会怀疑到重排头上。这条不变量此前在 Java 侧零测试覆盖。
 *
 * <p>集合恒等还是本次改动能被单独归因的前提：候选集不变 ⇒ 指标差异只可能来自顺序。
 * 留出集上 n=600 的 ΔNDCG@10（N=20 时 +0.0968）之所以能被解读成"重排的收益"，
 * 靠的就是这条不变量成立。
 *
 * <p>下面按两条路线覆盖：先用参数化列举模型可能返回的各种恶意与畸形输入，
 * 再用一个定种子的属性式测试做上万次随机组合的遍历。
 */
class RerankOrderingTest {

    /**
     * 不变量本体：无论模型返回什么，输出集合（含重复度）必须与输入完全相同。
     *
     * <p>覆盖任务点名的全部形态：空列表、部分 id、集外 id、重复 id、完全颠倒、超量返回，
     * 外加几种真实世界里同样会出现的畸形（null 列表、列表里混 null、空串 id、
     * 被改坏大小写与空白的 id）。
     */
    @ParameterizedTest(name = "{0}")
    @MethodSource("hostileRankings")
    void resultIsAlwaysAPermutationOfTheCandidateSet(String scenario, List<String> rankedIds) {
        var candidates = candidates(8);

        var reordered = RerankOrdering.reorder(candidates, rankedIds, Product::productId);

        assertIsPermutationOf(candidates, reordered);
    }

    static Stream<Arguments> hostileRankings() {
        var all = productIds(8);
        var oversized = new ArrayList<String>();
        oversized.addAll(all);                       // 全量
        oversized.addAll(all);                       // 再来一遍（重复点名）
        oversized.addAll(List.of("ghost-1", "ghost-2", "ghost-3"));  // 再掺集外 id
        return Stream.of(
                arguments("模型返回空列表", List.<String>of()),
                arguments("模型返回 null（字段缺失）", null),
                arguments("只返回部分 id", List.of("P05", "P01", "P03")),
                arguments("只返回一个 id", List.of("P08")),
                arguments("返回候选集外的 id", List.of("ghost-1", "P04", "P99")),
                arguments("返回的 id 全部在候选集外", List.of("x", "y", "z")),
                arguments("重复返回同一个 id", List.of("P02", "P02", "P02", "P02")),
                arguments("顺序完全颠倒", reversed(all)),
                arguments("返回数量超过候选数", List.copyOf(oversized)),
                arguments("列表里混入 null 元素", Arrays.asList("P02", null, "P01", null)),
                arguments("空串与空白 id", Arrays.asList("", "   ", "P06")),
                arguments("id 被改坏了大小写与空白", List.of(" P01", "p02", "P03 ")));
    }

    /**
     * 属性式遍历：随机候选集 × 随机畸形排名，几千次组合下不变量都不许破。
     *
     * <p>上面的参数化列举的是"我们想得到的"恶意形态；这一条覆盖"想不到的"组合——
     * 候选集里混入重复 id、空 id、空白 id，排名里混入截断 id、集外 id、重复、null。
     * 种子固定，失败可原样复现，且断言描述里带着种子与轮次。
     */
    @Test
    void randomisedRankingsNeverChangeTheDocumentSet() {
        var seed = 20260802L;   // 与留出集评测同日；固定即可复现
        var random = new Random(seed);

        for (var round = 0; round < 3000; round++) {
            var candidates = randomCandidates(random);
            var rankedIds = randomRanking(random, candidates);

            var reordered = RerankOrdering.reorder(candidates, rankedIds, Product::productId);

            assertThat(reordered)
                    .describedAs("seed=%d round=%d candidates=%s ranked=%s",
                            seed, round, ids(candidates), rankedIds)
                    .hasSameSizeAs(candidates)
                    .containsExactlyInAnyOrderElementsOf(candidates);
        }
    }

    /**
     * 未被点名的候选按原 BM25 顺序补齐在后面。
     *
     * <p>模型只点名前 3 条是最常见的真实形态（LLM 经常只吐它有把握的那几个）。
     * 剩下的必须原封不动按 BM25 顺序接上——否则"模型少说了几句"就等于把尾部打乱，
     * 而尾部恰恰是 BM25 唯一还能保证的东西。
     */
    @Test
    void candidatesTheModelDidNotMentionKeepTheirBm25Order() {
        var candidates = candidates(8);

        var reordered = RerankOrdering.reorder(candidates, List.of("P05", "P08", "P02"),
                Product::productId);

        assertThat(ids(reordered))
                .containsExactly("P05", "P08", "P02", "P01", "P03", "P04", "P06", "P07");
        assertIsPermutationOf(candidates, reordered);
    }

    /** 集外 id 与重复点名被忽略之后，被"跳过"的位置同样按原序补齐，不许留空。 */
    @Test
    void ignoredRankingEntriesStillLeaveEveryCandidateInPlace() {
        var candidates = candidates(5);

        var reordered = RerankOrdering.reorder(candidates,
                Arrays.asList("ghost", "P04", "P04", null, "P01", ""), Product::productId);

        assertThat(ids(reordered)).containsExactly("P04", "P01", "P02", "P03", "P05");
        assertIsPermutationOf(candidates, reordered);
    }

    /** 完全颠倒是合法排名，必须被原样执行——不变量守的是集合，不是顺序。 */
    @Test
    void fullyReversedRankingIsAppliedVerbatim() {
        var candidates = candidates(6);

        var reordered = RerankOrdering.reorder(candidates, reversed(productIds(6)),
                Product::productId);

        assertThat(ids(reordered)).containsExactly("P06", "P05", "P04", "P03", "P02", "P01");
        assertIsPermutationOf(candidates, reordered);
    }

    /**
     * 候选集里出现重复 id 时，两份文档都必须活下来。
     *
     * <p>这正是 RerankOrdering 按**下标**而不是按 id 合并的原因：用 id 做集合运算时
     * "去重"就等于"丢文档"，而且丢得毫无痕迹。模型点名一次 D，只能取走第一份。
     */
    @Test
    void duplicateIdsInTheCandidateSetAreNeverDeduplicated() {
        var first = product("D", "first copy");
        var second = product("D", "second copy");
        var other = product("P01");
        var candidates = List.of(first, second, other);

        var reordered = RerankOrdering.reorder(candidates, List.of("D"), Product::productId);

        assertThat(reordered).containsExactly(first, second, other);
        assertIsPermutationOf(candidates, reordered);
    }

    /** 模型点名两次 D 时，两个下标依次被取走，仍然是排列。 */
    @Test
    void repeatedMentionsOfADuplicatedIdConsumeEachSlotOnce() {
        var first = product("D", "first copy");
        var second = product("D", "second copy");
        var other = product("P01");
        var candidates = List.of(other, first, second);

        var reordered = RerankOrdering.reorder(candidates, List.of("D", "D", "D"),
                Product::productId);

        assertThat(reordered).containsExactly(first, second, other);
        assertIsPermutationOf(candidates, reordered);
    }

    /**
     * id 为 null / 空白的候选不可能被模型点名，但它依然是一篇文档，必须出现在结果里。
     *
     * <p>AiAdapterClient 会把这类候选从请求体里剔掉（契约要求 product_id 非空），
     * 若合并时也顺手把它丢了，就会出现"某个商品因为缺 id 而在开重排后消失"。
     */
    @Test
    void candidatesWithoutAUsableIdSurviveInOriginalOrder() {
        var candidates = List.of(product("P01"), product(null, "null id"),
                product("   ", "blank id"), product("P02"));

        var reordered = RerankOrdering.reorder(candidates, List.of("P02"), Product::productId);

        assertThat(ids(reordered)).containsExactly("P02", "P01", null, "   ");
        assertIsPermutationOf(candidates, reordered);
    }

    /** 边界：空候选集与 null 候选集都返回空列表，不抛异常。 */
    @Test
    void emptyOrNullCandidateSetYieldsAnEmptyResult() {
        assertThat(RerankOrdering.reorder(List.<Product>of(), List.of("P01"), Product::productId))
                .isEmpty();
        assertThat(RerankOrdering.reorder(null, List.of("P01"), Product::productId)).isEmpty();
    }

    /** 单条候选：无论模型说什么，结果都只能是这一条。 */
    @Test
    void singleCandidateIsAlwaysReturnedRegardlessOfTheRanking() {
        var candidates = candidates(1);

        assertThat(RerankOrdering.reorder(candidates, List.of("nope"), Product::productId))
                .isEqualTo(candidates);
        assertThat(RerankOrdering.reorder(candidates, List.of("P01", "P01"), Product::productId))
                .isEqualTo(candidates);
    }

    /** 返回不可变列表：调用方（含未来的缓存层）不该能就地改动重排结果。 */
    @Test
    void resultIsUnmodifiable() {
        var reordered = RerankOrdering.reorder(candidates(3), List.of("P02"), Product::productId);

        assertThat(catchThrowable(() -> reordered.add(product("P99"))))
                .isInstanceOf(UnsupportedOperationException.class);
    }

    /**
     * 护栏本身也要有测试：不变量一旦破了，必须**当场抛异常**，而不是返回一个少了文档的列表。
     *
     * <p>正常输入下这条路径不可达（那正是它的目的），所以这里用一个会在遍历途中自己少掉
     * 一条的候选列表从外部制造破坏。抛出的必须是 IllegalStateException——
     * AiAdapterClient 正是靠这个类型把"自身 bug"（INTERNAL_ERROR）与"模型返回非法"
     * （INVALID_RESPONSE）分开的，混淆会把排查引到错误方向。
     */
    @Test
    void aBrokenPermutationFailsLoudlyInsteadOfSilentlyLosingDocuments() {
        // shrinkOnRead=2：reorder 内部恰好两趟遍历，第二趟读到末位元素之后收缩，
        // 于是最后那次长度校验会看到"少了一条"。
        var hostile = new RerankTestSupport.ShrinkingCandidateList(candidates(4), 2);

        var thrown = catchThrowable(
                () -> RerankOrdering.reorder(hostile, List.of("P01"), Product::productId));

        assertThat(hostile.shrunk())
                .describedAs("这个用例必须真的破坏不变量，否则它什么都没测到").isTrue();
        assertThat(thrown).isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("permutation");
    }

    /** 输出集合（含重复度）与输入完全相同——元素相同、数量相同、没有凭空多出来的重复。 */
    private static void assertIsPermutationOf(List<Product> input, List<Product> output) {
        assertThat(output).hasSameSizeAs(input).containsExactlyInAnyOrderElementsOf(input);
        assertThat(ids(output)).containsExactlyInAnyOrderElementsOf(ids(input));
        // 候选 id 互不相同时，输出里也不许出现重复 id（"重复"是"丢文档"的另一副面孔：
        // 一条被复制两次，必然意味着另一条不见了）。
        if (ids(input).stream().distinct().count() == input.size()) {
            assertThat(ids(output)).doesNotHaveDuplicates();
        }
    }

    /** 随机候选集：长度 1..40，其中掺入重复 id、null id、空白 id。 */
    private static List<Product> randomCandidates(Random random) {
        var size = 1 + random.nextInt(40);
        var products = new ArrayList<Product>(size);
        for (var index = 0; index < size; index++) {
            var id = switch (random.nextInt(10)) {
                case 0 -> null;
                case 1 -> "   ";
                // 与前面某一条撞 id，制造候选集内重复
                case 2 -> "P%02d".formatted(1 + random.nextInt(Math.max(1, index)));
                default -> "P%02d".formatted(index + 1);
            };
            products.add(product(id, "#" + index));
        }
        return List.copyOf(products);
    }

    /** 随机排名：从候选 id 里抽样、打乱，再掺入集外 id、截断 id、重复与 null。 */
    private static List<String> randomRanking(Random random, List<Product> candidates) {
        if (random.nextInt(20) == 0) return null;   // 字段整体缺失
        var pool = new ArrayList<String>();
        for (var product : candidates) {
            if (random.nextBoolean()) pool.add(product.productId());
        }
        var extras = random.nextInt(6);
        for (var index = 0; index < extras; index++) {
            pool.add(switch (random.nextInt(4)) {
                case 0 -> "ghost-" + random.nextInt(1000);
                case 1 -> candidates.get(random.nextInt(candidates.size())).productId();  // 重复
                case 2 -> null;
                default -> "P";   // 截断成前缀，与任何候选都不等值
            });
        }
        java.util.Collections.shuffle(pool, random);
        return pool;
    }
}
