"""train / holdout 划分：把"调参用的集合"和"验收用的集合"彻底分开。

为什么需要这个模块
------------------
仓库里既有的 200 条评测查询是 `platform/data/processed/queries.jsonl` 的**前 200 行**
（按文件顺序取头部，实测其中 ESCI 自带 split 标记 train 144 / test 56，两侧混在一起）。
它是一个**混合集**，不是留出集。在这 200 条上反复调权重、再报告同一批数字，报出来的
是"拟合这 200 条噪声"的能力，不是泛化能力——尤其在标注极稀疏（top-10 里约 12% 的位置
有人工标注）的条件下，随机波动很容易被当成改进。

本模块从全量 10000 条里抽一个**固定的** train / holdout：train 用来搜参数、可以反复看；
holdout 只在最后验收时看一次。两侧都用同一份 v7 基线做参照点。

三条设计选择（都写在这里，免得后面靠猜）
----------------------------------------

**(1) 只保留至少有一条 E/S 标注的查询。**
EvaluationService 里 `relevant()` 只认 E/S，`gain()` 只有 E=3 / S=2 / C=1、其余 0。
一条查询若标注里没有任何 E/S，则 relevantTotal=0 → recall10 恒为 0，IDCG=0 → ndcg10
恒为 0：无论排序怎么变这两个指标都不动。把这类查询放进评测集，等于往分母里灌常数——
既稀释真实效应量（均值被固定的 0 拉平），又在配对检验的差值向量里塞进一堆结构性的 0，
把 sd 压小、把置信区间做窄，得到虚假的"稳"。
代价要说清楚：排除它们之后，这个集合就不再覆盖"本来就该返回空/无相关品"的查询，
zero-result 类问题必须靠别的监控看，不能指望这个集合发现。

实测结果：本仓库的 queries.jsonl 全部 10000 条**都**满足该条件，因此实际排除 0 条
（上游 ESCI 抽样时已经预筛过；另外也核对过每条查询至少有一个 E/S 商品确实存在于
20000 条商品索引里，同样 0 条落空）。过滤仍然保留在代码里，因为它是评测集的语义前提，
不是一次性的数据清洗动作——换一批 queries.jsonl 时它必须继续生效。

**(2) 划分不用 `random`，用 query_id 的 SHA-256 排序。**
`random.Random(seed).shuffle()` 的复现性依赖 CPython 的 RNG 实现细节；而
`sha256(f"{seed}|{query_id}")` 只依赖种子和 id，跨解释器版本、跨机器、跨语言都一样。
划分方式是"按哈希值排序得到一个确定的置换 → 取前 total 条 → 前 train_n 条进 train，
其余进 holdout"。附带的好处：置换是稳定的，日后只增大 total 时前面的样本不重排。
注意反过来说也成立——改 `total` 或 `train_ratio` 会改变两侧的成员，因此这两个值也
一并记进 manifest，不能只记种子。

**(3) 规模 2000（train 1400 / holdout 600）。**
两端约束都实测过：
  - 成本：候选评测实测约 14.5 ms/查询（热态，400 条 × 2 次，见下方"实测依据"）。
    train 一轮 ≈ 20 s，holdout 一轮 ≈ 9 s。200 个候选的扫描在 train 上约 68 分钟，
    可接受。
  - 功效：用一个真实的结构性权重改动（bullet_point 1.5→3.0、category 1.2→0.5）在
    400 条上量到逐查询 NDCG@10 差值的 sd = 0.1374（Recall@10 差值 sd = 0.2177）。
    配对检验在 α=0.05、power=0.80 下的最小可检出均值差约 2.80·sd/√n：
        NDCG@10   n=1400 → 0.0103    n=600 → 0.0157
        Recall@10 n=1400 → 0.0163    n=600 → 0.0249
    即 train 能稳定分辨 ~1 个 NDCG 点的改动，holdout 能复核 ~1.6 个点的改动。
    再小的样本（例如沿用 200 条）对应的最小可检出差约 0.027 NDCG——比这里能观察到的
    典型效应还大，等于什么都测不出来。
    对比：如果只用 600/300，holdout 的最小可检出差会退到 0.0222，恰好压在典型效应
    量级上，验收会变成掷硬币；把 holdout 提到 600 是为了让"train 上看到的改进"在
    holdout 上有机会被确认或否掉，而不是双方都不显著、无从判断。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_QUERIES_PATH = _REPO_ROOT / "platform/data/processed/queries.jsonl"

SPLIT_SEED = 20260816
"""划分种子。只影响 query_id 的哈希置换，与 stats.py 里 bootstrap/置换检验的种子
互相独立（那边重采样的是已经定下来的查询集合）。改这个值 = 换一套完全不同的
train/holdout，此前所有基线作废。"""

DEFAULT_TOTAL = 2000
DEFAULT_TRAIN_RATIO = 0.7

RELEVANT_LABELS = frozenset({"E", "S"})
"""与 EvaluationService.relevant() 对齐：只有 E/S 计入 relevant。"""

PUBLISHED_V7_FIELD_WEIGHTS: dict[str, float] = {
    "title": 4.0,
    "brand": 2.5,
    "bullet_point": 1.5,
    "description": 1.0,
    "category": 1.2,
}
"""已发布策略 v7（"Rollback to v5"）的字段权重，抄自 GET /api/v1/tools/strategies/current。
写死在这里而不是运行时读服务，是为了让基线可以脱离服务当前状态复现；
`build_baselines()` 会在跑之前跟线上 current strategy 对一次，不一致就报错而不是静默继续。"""


# --- 池子与过滤 ---------------------------------------------------------------


def has_relevant_judgment(judgments: Mapping[str, str]) -> bool:
    """判断一条查询是否至少有一个 E/S 标注——见模块开头设计选择 (1)。"""
    return any(label in RELEVANT_LABELS for label in judgments.values())


def load_pool(path: str | Path | None = None) -> tuple[list[dict[str, Any]], int]:
    """读 queries.jsonl，返回 (可用池, 被排除条数)。

    池子按 query_id 升序返回，保证后续哈希置换的输入是确定的（不依赖文件行序）。
    """
    src = Path(path) if path is not None else DEFAULT_QUERIES_PATH
    pool: list[dict[str, Any]] = []
    excluded = 0
    with open(src, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if has_relevant_judgment(row.get("judgments") or {}):
                pool.append(row)
            else:
                excluded += 1
    pool.sort(key=lambda r: int(r["query_id"]))
    return pool, excluded


# --- 确定性置换 ---------------------------------------------------------------


def permutation_key(query_id: int, seed: int) -> str:
    """一条查询在置换里的排序键。纯函数，跨版本跨机器一致。"""
    return hashlib.sha256(f"{seed}|{query_id}".encode("utf-8")).hexdigest()


def deterministic_permutation(pool: Sequence[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    """把池子按 (哈希键, query_id) 排序——query_id 作为 tie-break，防止哈希碰撞引入歧义。"""
    return sorted(pool, key=lambda r: (permutation_key(int(r["query_id"]), seed), int(r["query_id"])))


def query_ids_hash(query_ids: Iterable[int]) -> str:
    """一侧集合的指纹：sha256(",".join(升序 query_id))。

    先排序再哈希，指纹只反映**集合成员**，不受内部顺序影响——这正是我们想固定的东西。
    """
    joined = ",".join(str(q) for q in sorted(int(q) for q in query_ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# --- 划分对象 -----------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    seed: int
    total: int
    train_ratio: float
    train: list[dict[str, Any]]
    holdout: list[dict[str, Any]]
    pool_size: int
    excluded_no_relevant: int
    source: str

    @property
    def train_ids(self) -> list[int]:
        return sorted(int(r["query_id"]) for r in self.train)

    @property
    def holdout_ids(self) -> list[int]:
        return sorted(int(r["query_id"]) for r in self.holdout)

    def manifest(self, generated_at: str | None = None) -> dict[str, Any]:
        """生成 split-manifest 的内容。

        `generated_at` 由调用方传入（或省略）：模块内部不取系统时间，否则同种子重跑
        会产出不同的文件，"可复现"就成了空话。
        """
        payload: dict[str, Any] = {
            "split_seed": self.seed,
            "total": self.total,
            "train_ratio": self.train_ratio,
            "train_count": len(self.train),
            "holdout_count": len(self.holdout),
            "train_query_ids_sha256": query_ids_hash(self.train_ids),
            "holdout_query_ids_sha256": query_ids_hash(self.holdout_ids),
            "pool_size": self.pool_size,
            "excluded_no_relevant_judgment": self.excluded_no_relevant,
            "source_queries": self.source,
            "filter": "至少一条 E/S 标注（与 EvaluationService.relevant() 同口径）",
            "assignment": (
                "按 sha256(f'{seed}|{query_id}') 升序置换（query_id 为 tie-break），"
                "取前 total 条；前 train_count 条为 train，其余为 holdout"
            ),
            "id_hash_spec": 'sha256(",".join(str(qid) for qid in sorted(ids)))',
            "train_query_ids": self.train_ids,
            "holdout_query_ids": self.holdout_ids,
        }
        if generated_at is not None:
            payload["generated_at"] = generated_at
        return payload


def build_split(
    path: str | Path | None = None,
    *,
    seed: int = SPLIT_SEED,
    total: int = DEFAULT_TOTAL,
    train_ratio: float = DEFAULT_TRAIN_RATIO,
) -> Split:
    """构造划分。同参数重跑必然得到逐条相同的两个集合。"""
    pool, excluded = load_pool(path)
    if total > len(pool):
        raise ValueError(f"total={total} 超过可用池 {len(pool)} 条")
    permuted = deterministic_permutation(pool, seed)[:total]
    train_n = int(round(total * train_ratio))
    src = Path(path) if path is not None else DEFAULT_QUERIES_PATH
    return Split(
        seed=seed,
        total=total,
        train_ratio=train_ratio,
        train=permuted[:train_n],
        holdout=permuted[train_n:],
        pool_size=len(pool),
        excluded_no_relevant=excluded,
        source=str(src.relative_to(_REPO_ROOT)) if src.is_relative_to(_REPO_ROOT) else str(src),
    )


def to_eval_queries(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """转成 evaluate_candidate 要的请求体形状（丢掉 split/provenance 等无关字段）。"""
    return [
        {"query_id": int(r["query_id"]), "query": r["query"], "judgments": dict(r["judgments"])}
        for r in rows
    ]


# --- 加载与校验 ---------------------------------------------------------------


def load_split(manifest_path: str | Path, queries_path: str | Path | None = None) -> Split:
    """从 manifest 重建划分，并**校验两侧指纹**。

    重建走的是同一条 build_split 路径（种子/total/ratio 从 manifest 读），因此这既是
    加载器也是复现性检查：哈希对不上就说明 queries.jsonl 变了或参数被改过，直接报错，
    绝不静默返回一个"看起来像"的集合。
    """
    with open(manifest_path, encoding="utf-8") as fh:
        m = json.load(fh)
    split = build_split(
        queries_path, seed=int(m["split_seed"]), total=int(m["total"]), train_ratio=float(m["train_ratio"])
    )
    for side, recorded in (
        ("train", m["train_query_ids_sha256"]),
        ("holdout", m["holdout_query_ids_sha256"]),
    ):
        actual = query_ids_hash(split.train_ids if side == "train" else split.holdout_ids)
        if actual != recorded:
            raise ValueError(f"{side} 指纹不匹配：manifest {recorded} != 重建 {actual}")
    return split


def verify_reproducible(path: str | Path | None = None, **kw: Any) -> bool:
    """同参数连跑两次，逐条比较两侧成员。返回 True 表示可复现。"""
    a = build_split(path, **kw)
    b = build_split(path, **kw)
    return a.train_ids == b.train_ids and a.holdout_ids == b.holdout_ids


# --- 基线 ---------------------------------------------------------------------


def _result_to_run(
    result: Any,
    *,
    side: str,
    split: Split,
    field_weights: Mapping[str, float],
    weights_from_version: int,
) -> dict[str, Any]:
    """把 EvaluationResult 转成与 evaluation-latest.json 同构的 dict。

    保留服务端返回的 strategy_version=-1（候选评测的诚实标记，不是 v7 的官方跑分），
    另外挂一组 `split_*` / `weights_*` 元数据说明这批数字的来历。多出来的键不影响
    loader.by_query_id() 与 stats.compare()——它们只读 queries 数组。
    """
    run: dict[str, Any] = {
        "run_id": result.run_id,
        "strategy_version": result.strategy_version,
        "query_count": result.query_count,
        "precision10": result.precision10,
        "recall10": result.recall10,
        "mrr10": result.mrr10,
        "ndcg10": result.ndcg10,
        "zero_result_rate": result.zero_result_rate,
        "queries": [q.model_dump() for q in result.queries],
        "generated_at": result.generated_at,
    }
    run["strategy_source"] = "candidate"
    run["weights_from_strategy_version"] = weights_from_version
    run["field_weights"] = dict(field_weights)
    run["split"] = side
    run["split_seed"] = split.seed
    run["split_total"] = split.total
    run["query_ids_sha256"] = query_ids_hash(
        split.train_ids if side == "train" else split.holdout_ids
    )
    return run


def build_baselines(
    split: Split,
    client: Any,
    *,
    field_weights: Mapping[str, float] | None = None,
    weights_from_version: int = 7,
    verify_against_service: bool = True,
) -> dict[str, dict[str, Any]]:
    """用给定权重在 train / holdout 上各跑一次候选评测。

    走 evaluate_candidate（DRY_RUN）而不是 /evaluations/run：不落库、不碰 strategy 版本，
    因此可以随时重跑而不污染策略历史。
    """
    from ..models import StrategyConfig  # 延迟导入：本模块的划分逻辑不该依赖网络层

    weights = dict(field_weights or PUBLISHED_V7_FIELD_WEIGHTS)
    if verify_against_service:
        current = client.current_strategy()
        if current.version != weights_from_version:
            raise RuntimeError(
                f"线上 current strategy 是 v{current.version}，不是 v{weights_from_version}；"
                "拒绝把它当成 v7 基线跑"
            )
        live = {k: float(v) for k, v in (current.config.field_weights or {}).items()}
        if live != {k: float(v) for k, v in weights.items()}:
            raise RuntimeError(f"权重与线上 v{current.version} 不一致：线上 {live} != 本地 {weights}")

    config = StrategyConfig(field_weights=weights)
    runs: dict[str, dict[str, Any]] = {}
    for side, rows in (("train", split.train), ("holdout", split.holdout)):
        result = client.evaluate_candidate(to_eval_queries(rows), config, k=10)
        runs[side] = _result_to_run(
            result, side=side, split=split, field_weights=weights, weights_from_version=weights_from_version
        )
    return runs


# --- CLI ----------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m searchops_agent.eval.splits --out experiments/ --baseline`

    把划分、manifest、两侧基线一次性生成到指定目录。产物路径由调用方给，模块本身
    不假设写到哪儿——baselines/ 下的历史基线是不可再生资产，绝不能被这条命令覆盖。
    """
    import argparse

    p = argparse.ArgumentParser(description="构造 train/holdout 划分并（可选）跑两侧基线")
    p.add_argument("--out", required=True, help="产物目录")
    p.add_argument("--queries", default=None, help="queries.jsonl 路径（默认仓库内的处理产物）")
    p.add_argument("--seed", type=int, default=SPLIT_SEED)
    p.add_argument("--total", type=int, default=DEFAULT_TOTAL)
    p.add_argument("--train-ratio", type=float, default=DEFAULT_TRAIN_RATIO)
    p.add_argument("--generated-at", default=None, help="写进 manifest 的时间戳；不传则不写这个字段")
    p.add_argument("--baseline", action="store_true", help="额外跑 v7 权重的两侧基线")
    p.add_argument("--base-url", default="http://127.0.0.1:8080")
    args = p.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    split = build_split(args.queries, seed=args.seed, total=args.total, train_ratio=args.train_ratio)
    reproducible = verify_reproducible(
        args.queries, seed=args.seed, total=args.total, train_ratio=args.train_ratio
    )
    if not reproducible:
        raise RuntimeError("同参数两次构造得到不同集合——划分逻辑不可复现，拒绝写出产物")

    manifest_path = out / "split-manifest.json"
    manifest_path.write_text(
        json.dumps(split.manifest(args.generated_at), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"pool {split.pool_size} 条（排除 {split.excluded_no_relevant} 条无 E/S 标注）")
    print(f"train {len(split.train)} / holdout {len(split.holdout)} → {manifest_path}")

    if args.baseline:
        from ..client import SearchOpsClient

        with SearchOpsClient(base_url=args.base_url, timeout=900.0) as client:
            runs = build_baselines(split, client)
        for side, run in runs.items():
            path = out / f"baseline-v7-{side}.json"
            path.write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(
                f"{side}: n={run['query_count']} NDCG@10 {run['ndcg10']:.4f} "
                f"Recall@10 {run['recall10']:.4f} P@10 {run['precision10']:.4f} "
                f"MRR@10 {run['mrr10']:.4f} → {path}"
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "SPLIT_SEED",
    "DEFAULT_TOTAL",
    "DEFAULT_TRAIN_RATIO",
    "DEFAULT_QUERIES_PATH",
    "PUBLISHED_V7_FIELD_WEIGHTS",
    "Split",
    "build_split",
    "load_split",
    "load_pool",
    "has_relevant_judgment",
    "deterministic_permutation",
    "permutation_key",
    "query_ids_hash",
    "to_eval_queries",
    "verify_reproducible",
    "build_baselines",
]
