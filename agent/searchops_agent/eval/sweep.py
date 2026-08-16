"""BM25 字段权重扫描：量出"纯调参"在这个数据集上的天花板。

存在理由
--------
后面要上 AI（查询改写/语义召回）。如果不先把 BM25 自身的调参空间榨干，AI 带来的
任何提升都无法归因——本该属于"把 bullet_point 调高一点"的收益会被算到模型头上。
这个模块只做一件事：在 **train** 上系统地搜字段权重，找到 BM25 基线能达到的最好水平，
然后在 **holdout** 上验一次，回答"这个上限是真的还是过拟合出来的"。


为什么参数化成"相对 title 的比值"，而不是扫绝对值
------------------------------------------------
查询是 `multi_match` / `type: best_fields`（见 SearchQueryCompiler.organicQuery），
且没有设 `tie_breaker`——Elasticsearch 的 best_fields 就是 dis_max，tie_breaker 缺省为 0，
所以一篇文档的得分是

    score(d) = max_f ( w_f · bm25_f(d) )

排序键是 (_score desc, product_id asc)，`min_score` 为 0。于是把整个权重向量乘以任意
正数 λ，只是把所有文档的分数同乘 λ，**逐位不改变排序**，指标必然一字不差。

这不是推理出来的，是实测的（本模块开发时跑的第一件事，train 前 200 条）：

    field_weights = v7 原值                  → NDCG@10 0.4757913464  Recall@10 0.5636904762
    field_weights = v7 原值 × 2.5（title=10） → NDCG@10 0.4757913464  Recall@10 0.5636904762

十位小数完全一致。这也解释了任务背景里那条更早的观察——title 4.0→8.0 是空操作：
在 max 语义下，title 本来就是绝大多数文档的 argmax，等比放大它不会把 argmax 让给别人，
也不改变文档之间的相对序；只有把 title 压到 0.1 这种**改变了 argmax 归属**的程度才会动。

结论：权重向量只在"正比例缩放"这个等价类的意义下影响结果。因此本模块把 title 钉死在
4.0（v7 原值，纯粹是为了让候选权重落在 StrategyConfig 的 @Min(0)/@Max(20) 校验区间里，
并且读起来能跟 v7 直接对照），只搜另外四个字段相对 title 的比值

    r_f = w_f / w_title ,  f ∈ {brand, bullet_point, description, category}

v7 的比值是 brand 0.625 / bullet_point 0.375 / description 0.25 / category 0.30。
这样做的收益是搜索空间从 5 维降到 4 维，而且**每一个点都对应一个不同的排序**——
不会像扫绝对值那样，把大量互为缩放的等价配置反复评测一遍，还误以为它们"稳定复现"。

r_f 的上界取 5.0（4.0 × 5.0 = 20.0，正好顶到 @Max(20)）。r_f = 0 是一个**语义不同**
的点，不是"很小的权重"：SearchQueryCompiler 里有 `.filter(entry -> entry.getValue() > 0)`，
权重为 0 的字段会被整个从 `fields` 列表里删掉，于是"只在该字段命中"的文档不再被召回。
换句话说，0 改变的是**召回集合**，正数只改变**排序**。两者都在搜索空间里，但要分开解读。


搜索策略与理由
--------------
预算约束是实打实的：候选评测实测 ~14 ms/查询，train 1400 条一轮约 20 s。4 维、每维 16 档
的全网格是 65536 组 ≈ 15 天，不可能。所以分四段，每一段都有各自要回答的问题：

A. **单轴响应曲线**（各字段独立扫全部档位，其余保持 v7）。
   先回答"每个字段到底有没有头部空间、峰在哪"。这段的产物本身就是结论——即使后面
   什么都没搜到，响应曲线也说明了这个数据集上权重的敏感度量级。
B. **坐标下降**（从 A 的最优点出发，按敏感度排序逐轴重扫，多轮直到不再改善）。
   便宜、可解释，且在 best_fields 这种"max 聚合"下轴间耦合不算强，坐标下降通常够用。
C. **准随机局部搜索**（定种子，在当轮最优点周围的对数箱内采样）。
   专门补坐标下降的短板：它只沿轴走，抓不到需要两个字段同时动才更优的交互。
D. **细化**（在最终点周围做小步长乘性微调）。确认落在一个局部极值上，而不是网格的粗粒度上。

选择只用 train。holdout 由**另一个子命令**处理，`sweep` 这条路径根本不构造 holdout 的
查询体——纪律写进代码结构里，而不是写进注释里靠人自觉。

必须说清楚的一点：在 train 上从 N 个候选里取 argmax，胜者的 train 指标是**向上有偏**的
（winner's curse），train 上那个 p 值也因此不可信——检验用的数据正是挑选用的数据。
唯一有效的证据是 holdout 上那一次配对检验。报告里两者都给，但结论只认后者。


并发
----
EvaluationService.run 整个方法是 `@Transactional`，一轮评测会占住一条 Hikari 连接
约 20 s，而池子只有 8 条。所以并发固定在 2：实测 1.64× 加速（2×200 条从 5.62 s 降到
3.42 s，两种并发下 NDCG 逐位相同），同时给前台/后台/商城留下 6 条连接。再往上就是拿
整个环境的可用性换扫描速度，不值。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np

from .splits import PUBLISHED_V7_FIELD_WEIGHTS, Split, load_split, query_ids_hash, to_eval_queries

# --- 参数化 -------------------------------------------------------------------

ANCHOR_FIELD = "title"
ANCHOR_WEIGHT = 4.0
"""title 钉死在 v7 的 4.0。这个数值本身不影响任何指标（全局缩放是空操作，见模块 docstring），
它的作用只是把 r_f ∈ [0, 5] 映射到 StrategyConfig 允许的 [0, 20] 里。"""

RATIO_FIELDS: tuple[str, ...] = ("brand", "bullet_point", "description", "category")
MAX_RATIO = 5.0  # ANCHOR_WEIGHT * MAX_RATIO == 20.0 == StrategyConfig 的 @Max(20)

BASELINE_RATIOS: dict[str, float] = {
    f: PUBLISHED_V7_FIELD_WEIGHTS[f] / PUBLISHED_V7_FIELD_WEIGHTS[ANCHOR_FIELD] for f in RATIO_FIELDS
}
"""v7 在比值坐标下的位置：brand 0.625 / bullet_point 0.375 / description 0.25 / category 0.30。"""

LEVELS: tuple[float, ...] = (
    0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.375, 0.5, 0.625, 0.8, 1.0, 1.25, 1.6, 2.0, 2.5, 3.2, 5.0
)
"""单轴档位。0 之后大致按 √2 递进的对数格：等比步长才能在"比值"这个乘性坐标下均匀采样。
四个 v7 比值（0.625 / 0.375 / 0.25 / 0.30）都在表里，因此响应曲线必过基线点。"""

RATIO_DECIMALS = 6
PRIMARY_METRIC = "ndcg10"


def ratios_to_weights(ratios: Mapping[str, float]) -> dict[str, float]:
    """比值坐标 → StrategyConfig.field_weights。权重为 0 的字段仍然写出去，
    因为服务端要靠 `>0` 的过滤把它从 multi_match 的 fields 里删掉——这是刻意的语义。"""
    weights = {ANCHOR_FIELD: ANCHOR_WEIGHT}
    for f in RATIO_FIELDS:
        weights[f] = round(ANCHOR_WEIGHT * float(ratios[f]), 6)
    return weights


def weights_to_ratios(weights: Mapping[str, float]) -> dict[str, float]:
    anchor = float(weights[ANCHOR_FIELD])
    return {f: float(weights[f]) / anchor for f in RATIO_FIELDS}


def canonical(ratios: Mapping[str, float]) -> tuple[float, ...]:
    """缓存键。四舍五入到 6 位小数，避免浮点尾差把同一个点算成两个。"""
    return tuple(round(float(ratios[f]), RATIO_DECIMALS) for f in RATIO_FIELDS)


def clamp(r: float) -> float:
    return float(min(MAX_RATIO, max(0.0, r)))


def label(ratios: Mapping[str, float]) -> str:
    return " ".join(f"{f}={ratios[f]:.4g}" for f in RATIO_FIELDS)


# --- 单条评测记录 -------------------------------------------------------------


@dataclass
class Trial:
    ratios: dict[str, float]
    weights: dict[str, float]
    stage: str
    ndcg10: float
    recall10: float
    precision10: float
    mrr10: float
    zero_result_rate: float
    query_count: int
    seconds: float
    index: int
    queries: list[dict[str, Any]] | None = None
    """逐查询指标只在需要写产物时保留；扫描过程中一律丢弃（200 组 × 1400 条会白占内存）。"""

    def record(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "stage": self.stage,
            "ratios": {f: round(self.ratios[f], RATIO_DECIMALS) for f in RATIO_FIELDS},
            "field_weights": self.weights,
            "ndcg10": self.ndcg10,
            "recall10": self.recall10,
            "precision10": self.precision10,
            "mrr10": self.mrr10,
            "zero_result_rate": self.zero_result_rate,
            "query_count": self.query_count,
            "seconds": round(self.seconds, 3),
        }


# --- 评测器（缓存 + 并发 + 落盘日志）-------------------------------------------


class Evaluator:
    """把 (比值向量 → train 指标) 这个映射做成带缓存、带日志、可并发的函数。

    每评完一组就往 JSONL 追加一行并 flush：扫描要跑几十分钟，中途挂掉不能把前面
    的真实测量结果一起丢掉。
    """

    def __init__(
        self,
        client_factory: Callable[[], Any],
        queries: list[dict[str, Any]],
        log_path: Path,
        *,
        workers: int = 2,
        keep_queries_for: Callable[[float], bool] | None = None,
    ) -> None:
        from ..models import StrategyConfig  # 延迟导入：参数化逻辑不该依赖网络层

        self._config_cls = StrategyConfig
        self._client_factory = client_factory
        self._queries = queries
        self._workers = workers
        self._log = open(log_path, "a", encoding="utf-8")
        self.cache: dict[tuple[float, ...], Trial] = {}
        self.order: list[Trial] = []
        self.failures: list[dict[str, Any]] = []
        self._n = 0
        self._clients: dict[int, Any] = {}
        self._keep = keep_queries_for

    # -- 每个工作线程一个 httpx.Client，避免共享连接池带来的不确定性 --
    def _client(self) -> Any:
        import threading

        tid = threading.get_ident()
        if tid not in self._clients:
            self._clients[tid] = self._client_factory()
        return self._clients[tid]

    def _evaluate_one(self, ratios: dict[str, float], stage: str) -> Trial | None:
        weights = ratios_to_weights(ratios)
        cfg = self._config_cls(field_weights=weights)
        t0 = time.time()
        try:
            result = self._client().evaluate_candidate(self._queries, cfg, k=10)
        except Exception as exc:  # 单组失败不该终止整轮扫描
            self.failures.append({"ratios": dict(ratios), "stage": stage, "error": repr(exc)})
            return None
        dt = time.time() - t0
        if result.strategy_version != -1:
            raise RuntimeError(
                f"候选评测返回 strategy_version={result.strategy_version}，不是哨兵 -1；"
                "说明这轮没走候选路径，拒绝把它当成干跑结果"
            )
        return Trial(
            ratios=dict(ratios), weights=weights, stage=stage,
            ndcg10=result.ndcg10, recall10=result.recall10, precision10=result.precision10,
            mrr10=result.mrr10, zero_result_rate=result.zero_result_rate,
            query_count=result.query_count, seconds=dt, index=-1,
            queries=[q.model_dump() for q in result.queries],
        )

    def evaluate(self, batch: Sequence[Mapping[str, float]], stage: str) -> list[Trial]:
        """评测一批比值向量（缓存命中的直接返回）。批内并发，批间串行。"""
        pending: list[dict[str, float]] = []
        seen: set[tuple[float, ...]] = set()
        for ratios in batch:
            r = {f: clamp(float(ratios[f])) for f in RATIO_FIELDS}
            key = canonical(r)
            if key in self.cache or key in seen:
                continue
            seen.add(key)
            pending.append(r)

        if pending:
            with ThreadPoolExecutor(self._workers) as ex:
                fresh = list(ex.map(lambda r: self._evaluate_one(r, stage), pending))
            for trial in fresh:
                if trial is None:
                    continue
                self._n += 1
                trial.index = self._n
                key = canonical(trial.ratios)
                rec = trial.record()
                if self._keep is not None and not self._keep(trial.ndcg10):
                    trial.queries = None
                self.cache[key] = trial
                self.order.append(trial)
                self._log.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._log.flush()

        out: list[Trial] = []
        for ratios in batch:
            key = canonical({f: clamp(float(ratios[f])) for f in RATIO_FIELDS})
            if key in self.cache:
                out.append(self.cache[key])
        return out

    @property
    def evaluated(self) -> int:
        return self._n

    def best(self, metric: str = PRIMARY_METRIC) -> Trial:
        return max(self.order, key=lambda t: getattr(t, metric))

    def close(self) -> None:
        self._log.close()
        for c in self._clients.values():
            c.close()


# --- 各阶段 -------------------------------------------------------------------


def axis_scan(
    ev: Evaluator, center: Mapping[str, float], *, levels: Sequence[float] = LEVELS, stage: str = "A-axis"
) -> dict[str, list[dict[str, float]]]:
    """A 段：每个字段独立扫全部档位，其余固定在 center。产物是四条响应曲线。"""
    curves: dict[str, list[dict[str, float]]] = {}
    for f in RATIO_FIELDS:
        batch = []
        for lv in levels:
            r = dict(center)
            r[f] = clamp(lv)
            batch.append(r)
        trials = ev.evaluate(batch, f"{stage}:{f}")
        curves[f] = [
            {"ratio": t.ratios[f], "ndcg10": t.ndcg10, "recall10": t.recall10,
             "mrr10": t.mrr10, "zero_result_rate": t.zero_result_rate}
            for t in sorted(trials, key=lambda t: t.ratios[f])
        ]
    return curves


def coordinate_descent(
    ev: Evaluator,
    start: Mapping[str, float],
    *,
    levels: Sequence[float] = LEVELS,
    order: Sequence[str] | None = None,
    max_passes: int = 3,
    metric: str = PRIMARY_METRIC,
    stage: str = "B-coord",
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """B 段：逐轴重扫、取该轴最优、带着改进进入下一轴。缓存让重复点不再花钱。"""
    current = dict(start)
    trace: list[dict[str, Any]] = []
    fields = list(order or RATIO_FIELDS)
    best_score = getattr(ev.evaluate([current], stage)[0], metric)
    for p in range(1, max_passes + 1):
        improved = False
        for f in fields:
            batch = []
            for lv in levels:
                r = dict(current)
                r[f] = clamp(lv)
                batch.append(r)
            trials = ev.evaluate(batch, f"{stage}p{p}:{f}")
            top = max(trials, key=lambda t: getattr(t, metric))
            if getattr(top, metric) > best_score + 1e-12:
                trace.append({
                    "pass": p, "field": f,
                    "from": round(current[f], RATIO_DECIMALS), "to": round(top.ratios[f], RATIO_DECIMALS),
                    "score_from": best_score, "score_to": getattr(top, metric),
                })
                current = dict(top.ratios)
                best_score = getattr(top, metric)
                improved = True
        if not improved:
            trace.append({"pass": p, "field": None, "note": "本轮无改善，坐标下降收敛"})
            break
    return current, trace


def quasi_random_search(
    ev: Evaluator,
    center: Mapping[str, float],
    *,
    n: int = 48,
    seed: int = 20260816,
    spread: float = 3.0,
    zero_prob: float = 0.10,
    floor: float = 0.02,
    stage: str = "C-random",
) -> list[dict[str, float]]:
    """C 段：在 center 周围的**对数箱**里采样（每轴 ÷spread .. ×spread），
    补坐标下降抓不到的轴间交互。另以 zero_prob 的概率把某轴打到 0（召回集合的结构性改动）。
    种子固定 → 同一次扫描可原样复现。"""
    rng = np.random.default_rng(seed)
    batch: list[dict[str, float]] = []
    for _ in range(n):
        r: dict[str, float] = {}
        for f in RATIO_FIELDS:
            if rng.random() < zero_prob:
                r[f] = 0.0
                continue
            c = max(float(center[f]), floor)
            lo, hi = np.log(max(c / spread, floor)), np.log(min(c * spread, MAX_RATIO))
            r[f] = clamp(float(np.exp(rng.uniform(lo, hi))))
        batch.append(r)
    ev.evaluate(batch, stage)
    return batch


def local_refine(
    ev: Evaluator,
    center: Mapping[str, float],
    *,
    factors: Sequence[float] = (0.7, 0.85, 1.18, 1.4),
    stage: str = "D-refine",
) -> None:
    """D 段：在最终点上做小步长乘性微调，确认它是局部极值而不是网格粗粒度的假象。"""
    batch: list[dict[str, float]] = []
    for f in RATIO_FIELDS:
        if center[f] <= 0:
            continue  # 0 是结构性的"删字段"，乘性微调对它没有意义
        for k in factors:
            r = dict(center)
            r[f] = clamp(center[f] * k)
            batch.append(r)
    ev.evaluate(batch, stage)


# --- 扫描编排 -----------------------------------------------------------------


@dataclass
class SweepOutcome:
    best: Trial
    baseline_trial: Trial
    curves: dict[str, list[dict[str, float]]]
    coord_trace: list[dict[str, Any]]
    stage_bests: list[dict[str, Any]]
    evaluated: int
    seconds: float
    failures: list[dict[str, Any]] = field(default_factory=list)


def run_sweep(
    ev: Evaluator,
    *,
    random_n: int = 48,
    random_seed: int = 20260816,
    metric: str = PRIMARY_METRIC,
) -> SweepOutcome:
    """在 **train** 上跑完 A→D 四段。这个函数拿不到 holdout，也不该拿到。"""
    t0 = time.time()
    stage_bests: list[dict[str, Any]] = []

    def snapshot(name: str) -> None:
        b = ev.best(metric)
        stage_bests.append({
            "stage": name, "evaluated_so_far": ev.evaluated,
            "best_" + metric: getattr(b, metric),
            "best_ratios": {f: round(b.ratios[f], RATIO_DECIMALS) for f in RATIO_FIELDS},
        })

    baseline_trial = ev.evaluate([BASELINE_RATIOS], "baseline")[0]

    curves = axis_scan(ev, BASELINE_RATIOS)
    snapshot("A-axis")

    # 轴的处理顺序按 A 段实测的敏感度（该轴上 NDCG 的极差）从大到小——
    # 先动影响大的轴，坐标下降更快收敛，也更不容易被小轴上的噪声带偏。
    sensitivity = {
        f: max(p["ndcg10"] for p in curves[f]) - min(p["ndcg10"] for p in curves[f])
        for f in RATIO_FIELDS
    }
    order = sorted(RATIO_FIELDS, key=lambda f: -sensitivity[f])

    start = dict(ev.best(metric).ratios)
    current, coord_trace = coordinate_descent(ev, start, order=order, metric=metric)
    snapshot("B-coord")

    quasi_random_search(ev, ev.best(metric).ratios, n=random_n, seed=random_seed)
    snapshot("C-random")

    # 随机段若找到更好的点，再沿轴收敛一次，然后细化
    current, coord2 = coordinate_descent(ev, ev.best(metric).ratios, order=order,
                                         metric=metric, max_passes=2, stage="B2-coord")
    coord_trace += coord2
    snapshot("B2-coord")

    local_refine(ev, ev.best(metric).ratios)
    snapshot("D-refine")

    return SweepOutcome(
        best=ev.best(metric), baseline_trial=baseline_trial, curves=curves,
        coord_trace=coord_trace, stage_bests=stage_bests,
        evaluated=ev.evaluated, seconds=time.time() - t0, failures=list(ev.failures),
    )


# --- 产物 ---------------------------------------------------------------------


def trial_to_run(
    trial: Trial, *, side: str, split: Split, note: str
) -> dict[str, Any]:
    """把一次评测转成与 evaluation-latest.json 同构的 run（可直接喂 loader/stats/gate）。"""
    if trial.queries is None:
        raise ValueError("这条 Trial 没有保留逐查询指标，无法写出 run 产物")
    ids = split.train_ids if side == "train" else split.holdout_ids
    return {
        "run_id": None,
        "strategy_version": -1,
        "query_count": trial.query_count,
        "precision10": trial.precision10,
        "recall10": trial.recall10,
        "mrr10": trial.mrr10,
        "ndcg10": trial.ndcg10,
        "zero_result_rate": trial.zero_result_rate,
        "queries": trial.queries,
        "generated_at": None,
        "strategy_source": "candidate",
        "field_weights": trial.weights,
        "ratios_vs_title": {f: round(trial.ratios[f], RATIO_DECIMALS) for f in RATIO_FIELDS},
        "split": side,
        "split_seed": split.seed,
        "split_total": split.total,
        "query_ids_sha256": query_ids_hash(ids),
        "note": note,
    }


def _write(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# --- CLI ----------------------------------------------------------------------


def _sweep_cmd(args: Any) -> int:
    from ..client import SearchOpsClient

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    split = load_split(args.manifest, args.queries)

    # 纪律：这条路径只构造 train 的查询体。holdout 走 `verify` 子命令。
    train_queries = to_eval_queries(split.train)

    log_path = out / "sweep-train-log.jsonl"
    if log_path.exists():
        log_path.unlink()  # 每次扫描是一份完整记录，不跟上一次的混在一起

    best_seen = [0.0]

    def keep(score: float) -> bool:
        """只为"当前最好"保留逐查询数组，其余丢掉——否则几百组 × 1400 条纯属白占内存。"""
        if score >= best_seen[0]:
            best_seen[0] = score
            return True
        return False

    ev = Evaluator(
        lambda: SearchOpsClient(base_url=args.base_url, timeout=900.0),
        train_queries, log_path, workers=args.workers, keep_queries_for=keep,
    )
    try:
        outcome = run_sweep(ev, random_n=args.random_n, random_seed=args.random_seed)
        best = outcome.best
        # 冠军重跑一次拿完整逐查询数组（评测是确定性的，聚合值必须逐位对上）
        rerun = ev._evaluate_one(best.ratios, "final-rerun")
        if rerun is None:
            raise RuntimeError("冠军配置重跑失败")
        if abs(rerun.ndcg10 - best.ndcg10) > 1e-12:
            raise RuntimeError(f"冠军重跑不一致：{rerun.ndcg10} != {best.ndcg10}")
        best.queries = rerun.queries
    finally:
        ev.close()

    top = sorted(ev.order, key=lambda t: -t.ndcg10)[:20]
    baseline = outcome.baseline_trial
    report = {
        "role": "train-only sweep（选择只在 train 上做；holdout 未被本命令读取）",
        "parameterisation": {
            "scheme": "title 钉死在 4.0，只搜其余四个字段相对 title 的比值 r_f = w_f / w_title",
            "why": (
                "multi_match/best_fields 无 tie_breaker → score(d)=max_f(w_f·bm25_f(d))，"
                "整体乘正数只缩放分数不改排序；实测 v7 权重 ×2.5 在 train 前 200 条上 "
                "NDCG@10/Recall@10 十位小数完全一致（0.4757913464 / 0.5636904762）。"
                "因此绝对值不可辨识，只有比值可辨识。"
            ),
            "anchor": {"field": ANCHOR_FIELD, "weight": ANCHOR_WEIGHT,
                       "note": "锚点数值不影响任何指标，只为让 w_f 落在 StrategyConfig 的 [0,20] 里"},
            "ratio_fields": list(RATIO_FIELDS),
            "baseline_ratios": {f: round(BASELINE_RATIOS[f], RATIO_DECIMALS) for f in RATIO_FIELDS},
            "levels": list(LEVELS),
            "max_ratio": MAX_RATIO,
            "zero_semantics": (
                "r_f=0 会让服务端把该字段从 multi_match.fields 里删掉"
                "（SearchQueryCompiler 的 value>0 过滤）——改的是召回集合，不只是排序"
            ),
        },
        "search_plan": {
            "A-axis": "四条单轴响应曲线（其余固定在 v7）",
            "B-coord": "坐标下降，轴序按 A 段实测敏感度降序",
            "C-random": f"对数箱内准随机采样 {args.random_n} 点（seed={args.random_seed}）",
            "B2-coord": "随机段之后再收敛一次",
            "D-refine": "最终点乘性微调，确认是局部极值",
            "why": (
                "4 维 × 18 档全网格 = 104976 组 × 20 s ≈ 24 天，不可行；"
                "分段设计让每一段各自回答一个问题（头部空间 / 收敛 / 交互 / 粒度）"
            ),
        },
        "budget": {
            "distinct_configs_evaluated": ev.evaluated,
            "wall_seconds": round(outcome.seconds, 1),
            "wall_minutes": round(outcome.seconds / 60, 2),
            "workers": args.workers,
            "train_n": len(split.train),
            "failures": outcome.failures,
        },
        "train_baseline_v7": baseline.record(),
        "axis_curves": outcome.curves,
        "axis_sensitivity_ndcg_range": {
            f: round(max(p["ndcg10"] for p in outcome.curves[f]) - min(p["ndcg10"] for p in outcome.curves[f]), 6)
            for f in RATIO_FIELDS
        },
        "coordinate_trace": outcome.coord_trace,
        "stage_bests": outcome.stage_bests,
        "best": {
            **best.record(),
            "delta_vs_baseline": {
                m: round(getattr(best, m) - getattr(baseline, m), 6)
                for m in ("ndcg10", "recall10", "precision10", "mrr10")
            },
        },
        "top20_by_ndcg10": [t.record() for t in top],
        "caveat_winners_curse": (
            f"best 是在 {ev.evaluated} 组候选里按 train NDCG@10 取的 argmax，"
            "该数值向上有偏；train 上的显著性检验也无效（选择与检验用了同一批数据）。"
            "唯一有效证据是 holdout 上的那一次配对检验。"
        ),
    }

    _write(out / "sweep-train-report.json", report)
    _write(out / "best-config.json", {
        "field_weights": best.weights,
        "ratios_vs_title": {f: round(best.ratios[f], RATIO_DECIMALS) for f in RATIO_FIELDS},
        "selected_on": "train",
        "selection_metric": PRIMARY_METRIC,
        "train_ndcg10": best.ndcg10,
        "train_baseline_ndcg10": baseline.ndcg10,
        "split_manifest": str(args.manifest),
        "distinct_configs_evaluated": ev.evaluated,
    })
    _write(out / "sweep-best-train.json",
           trial_to_run(best, side="train", split=split,
                        note="train 上扫描出的最优字段权重（选择集，非验收集）"))

    print(f"评估 {ev.evaluated} 组 / {outcome.seconds/60:.1f} 分钟")
    print(f"train v7 基线 NDCG@10 {baseline.ndcg10:.6f}")
    print(f"train 最优     NDCG@10 {best.ndcg10:.6f}  ({label(best.ratios)})")
    print(f"train Δ = {best.ndcg10 - baseline.ndcg10:+.6f}")
    return 0


def _verify_cmd(args: Any) -> int:
    """在 holdout 上跑一次选定的最优配置。**只在 train 选完之后调用一次**。"""
    from ..client import SearchOpsClient

    out = Path(args.out)
    split = load_split(args.manifest, args.queries)
    cfg = json.loads(Path(args.best).read_text(encoding="utf-8"))
    weights = {k: float(v) for k, v in cfg["field_weights"].items()}

    ev = Evaluator(
        lambda: SearchOpsClient(base_url=args.base_url, timeout=900.0),
        to_eval_queries(split.holdout), out / "sweep-holdout-log.jsonl", workers=1,
        keep_queries_for=lambda _score: True,  # 只跑一组，逐查询数组必须留下
    )
    try:
        # 走 evaluate() 而不是 _evaluate_one()：这条路径会把这次评测写进 JSONL 日志。
        # holdout 只允许看一次，那一次必须留下痕迹——否则"看过几次"这件事无从审计。
        trials = ev.evaluate([weights_to_ratios(weights)], "holdout-verify")
        if not trials:
            raise RuntimeError(f"holdout 评测失败：{ev.failures}")
        trial = trials[0]
    finally:
        ev.close()

    path = _write(out / "sweep-best-holdout.json",
                  trial_to_run(trial, side="holdout", split=split,
                               note="train 选出的最优配置在 holdout 上的唯一一次评测"))
    print(f"holdout n={trial.query_count} NDCG@10 {trial.ndcg10:.6f} Recall@10 {trial.recall10:.6f} "
          f"P@10 {trial.precision10:.6f} MRR@10 {trial.mrr10:.6f} zero {trial.zero_result_rate:.4f}")
    print(f"→ {path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="BM25 字段权重扫描（train 选择 / holdout 验收分离）")
    p.add_argument("--base-url", default="http://127.0.0.1:8080")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sweep", help="只在 train 上扫描")
    s.add_argument("--manifest", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--queries", default=None)
    s.add_argument("--workers", type=int, default=2,
                   help="并发候选评测数。EvaluationService.run 是 @Transactional，"
                        "一轮占一条 Hikari 连接（池 8），默认 2")
    s.add_argument("--random-n", type=int, default=48)
    s.add_argument("--random-seed", type=int, default=20260816)
    s.set_defaults(func=_sweep_cmd)

    v = sub.add_parser("verify", help="在 holdout 上跑一次已选定的配置")
    v.add_argument("--manifest", required=True)
    v.add_argument("--best", required=True, help="best-config.json 路径")
    v.add_argument("--out", required=True)
    v.add_argument("--queries", default=None)
    v.set_defaults(func=_verify_cmd)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ANCHOR_FIELD", "ANCHOR_WEIGHT", "RATIO_FIELDS", "BASELINE_RATIOS", "LEVELS", "MAX_RATIO",
    "ratios_to_weights", "weights_to_ratios", "canonical", "Trial", "Evaluator",
    "axis_scan", "coordinate_descent", "quasi_random_search", "local_refine",
    "run_sweep", "SweepOutcome", "trial_to_run", "main",
]
