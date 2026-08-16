package lab.searchops.service;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.function.Function;

/**
 * 把模型返回的 id 顺序合并回候选集，产出候选集的一个**排列**。
 *
 * <h2>为什么"集合不变"这条不可妥协</h2>
 * 重排是排序阶段的优化，它对召回不负任何责任。候选集是 BM25 已经证明"至少有资格被展示"
 * 的那批文档；如果重排能让文档消失，那么模型的一次幻觉（少返一个 id、返回一个不存在的
 * product_id、把 id 拼错一个字符）就会变成**召回下降**——用户搜不到本来搜得到的商品，
 * 而在评测里它表现为 Recall@10 下跌。到那时故障的形状和"BM25 没召回"一模一样，
 * 排查会被引向索引和查询编译，谁也不会怀疑到重排头上。反过来，只要集合恒等，
 * 重排最坏的情况也只是"顺序没变好"，永远退化不到比 BM25 更差的召回。
 * 这也是本次改动能被单独归因的前提：候选集不变 ⇒ 指标差异只可能来自顺序。
 *
 * <p>因此这里按**下标**而不是按 id 做合并。用 id 做集合运算在候选集出现重复 id 时会
 * 悄悄丢文档（去重 = 丢文档），用下标则天然保证结果是输入的排列：每个下标最多被取走一次
 * （从 {@code slots} 队列里弹出后不再放回），未被点名的下标在第二趟按原序全部补齐，
 * 于是"结果长度 == 输入长度"就足以证明每个下标恰好出现一次。末尾的显式检查守的就是这一点。
 */
public final class RerankOrdering {
    private RerankOrdering() {}

    /**
     * @param candidates BM25 顺序的候选集，不为 null；元素顺序即降级时要保持的顺序
     * @param rankedIds  模型返回的 id 顺序，可能少返、多返、含集外 id、含重复 id、为 null
     * @param idOf       从候选元素取 id 的函数
     * @return candidates 的一个排列：先是被模型点名且确实在候选集里的元素（按模型顺序），
     *         其余元素按原 BM25 顺序追加在后面
     * @throws IllegalStateException 排列不变量被破坏——这是本类自身的缺陷，绝不该发生
     */
    public static <T> List<T> reorder(List<T> candidates, List<String> rankedIds,
            Function<? super T, String> idOf) {
        if (candidates == null || candidates.isEmpty()) return List.of();

        // id -> 该 id 在候选集中的所有下标（按原序）。用队列而不是单个下标：
        // 候选集理论上可能出现重复 id，此时模型第 n 次点名该 id 就取第 n 个下标，
        // 不会因为"这个 id 已经用过了"而把第二份文档丢掉。
        var slots = new HashMap<String, ArrayDeque<Integer>>();
        for (var index = 0; index < candidates.size(); index++) {
            var id = idOf.apply(candidates.get(index));
            // id 为空的候选不可能被模型点名，但它依然要出现在结果里——由第二趟补齐。
            if (id == null || id.isBlank()) continue;
            slots.computeIfAbsent(id, key -> new ArrayDeque<>()).addLast(index);
        }

        var taken = new boolean[candidates.size()];
        var ordered = new ArrayList<T>(candidates.size());

        if (rankedIds != null) {
            for (var id : rankedIds) {
                if (id == null) continue;
                var positions = slots.get(id);
                // 集外 id（模型幻觉出来的、或多返的）与已经取完的重复点名：直接忽略。
                // 忽略是安全的——它不会让任何候选元素消失，只是这一条排名建议作废。
                if (positions == null || positions.isEmpty()) continue;
                var index = positions.pollFirst();
                taken[index] = true;
                ordered.add(candidates.get(index));
            }
        }

        // 模型没提到的候选（少返、或被上面忽略掉的那些位置）按原 BM25 顺序追加到末尾。
        for (var index = 0; index < candidates.size(); index++) {
            if (!taken[index]) ordered.add(candidates.get(index));
        }

        // 防御性检查：结果必须是输入的排列。上面的构造已经保证"每个下标最多一次"，
        // 因此长度相等即等价于"每个下标恰好一次"，即集合（含重复度）与输入完全相同。
        // 这条断言是给未来的修改者的护栏——任何让文档凭空消失或凭空出现的改动都会当场炸掉，
        // 而不是变成一次悄无声息的召回下降（理由见类注释）。
        if (ordered.size() != candidates.size()) {
            throw new IllegalStateException(
                    "Rerank must be a permutation of the candidate set: expected "
                            + candidates.size() + " documents but produced " + ordered.size());
        }
        return Collections.unmodifiableList(ordered);
    }
}
