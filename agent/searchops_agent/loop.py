"""提案闭环：诊断 → 提案 → 干跑预览 → **自证评测** → 门禁 → 提交待审。

与改动前的区别（也是本模块存在的理由）
--------------------------------------
旧的 `run(baseline, candidate_eval)` 要求调用方把两份评测结果喂进来，于是 Agent 并没有
"自证"能力——它只是在别人给的数字上过一遍门禁。谁给数字、数字怎么来的、跟提案里的那份
配置是不是同一份，全在闭环之外，无法审计。

现在候选那一侧由 Agent 自己跑：`evaluate_candidate`（DRY_RUN 级，服务端强制不落库、
不碰 strategy 版本、返回 strategy_version=-1）。提案里的 `StrategyConfig` 直接送进去评测，
因此"被门禁判定的配置"和"将要提交审批的配置"在代码上是同一个对象，不可能错位。

刻意不是一个自由的 ReAct 循环。阶段顺序固定，每个提案都必须走完 preview → 自证评测 →
门禁才可能进入 submit——模型能决定"提什么"，不能决定"跳过哪一步"。

循环终止于 SUBMITTED。approve / publish / rollback 不在本模块的能力范围内，
因为它们不在工具注册表里（见 tools.ALLOWED / tools.FORBIDDEN）。本文件里不出现这三个词
以外的任何调用——治理边界是代码事实，不是提示词约定。


三条必须写下来的方法学选择
--------------------------

**(1) 自证只在 train 上做，holdout 在本模块里根本没有取数路径。**
`load_split()` 返回的 Split 两侧都有，但本模块只经 `select_train_rows()` 取数，
而该函数会显式断言"选中的 query_id 与 holdout 无交集"。纪律写进代码结构，不靠自觉。
基线一侧还会核对文件里的 `split == "train"` 与 `query_ids_sha256`，
所以就算有人把 baseline-v7-holdout.json 传进来，也会当场报错而不是静默比出一个数字。

**(2) 基线直接读 experiments/baseline-v7-train.json，不重跑。**
省一轮 20 s 是次要的，主要是保证"基线"这个参照点在所有提案、所有提案器之间**逐位相同**。
每次重跑基线都会引入一份新的噪声，两个提案器就不再是在同一把尺子上比。
读进来之后要对三件事失败关闭：文件是 train 侧、查询集指纹与 manifest 一致、
基线权重与线上 current strategy 一致（否则"候选 vs 基线"的差里混进了未记录的策略漂移）。

**(3) 评测子集：train 的**前 N 条**（默认 N=700），N 可配，0 表示整个 train。**
`build_split()` 里 train 已经是"按 sha256(f'{seed}|{query_id}') 排序"的置换序，
所以"前 N 条"= 该置换的前缀 = 一个**确定性**且**无偏**的随机子集：
  - 确定性：同一份 manifest + 同一个 N，跨机器跨进程选出同一批 query_id；
  - 无偏：前缀是哈希序的前缀，与查询本身的任何属性无关；
  - 嵌套：N=700 的子集是 N=1400 的前缀，加大 N 不会换掉已有样本，历史产物仍可对照。
不用 `random.sample(seed=...)` 是同一个理由（见 splits.py 设计选择 (2)）：那依赖 CPython
RNG 的实现细节，而哈希序不依赖。
选 700（train 的一半）的依据是 splits.py 里实测的逐查询 NDCG@10 差值 sd=0.1374：
配对检验在 α=0.05 / power=0.80 下的最小可检出均值差 ≈ 2.80·sd/√n，
    n=700  → 0.0145   （单候选评测 ≈10 s）
    n=1400 → 0.0103   （单候选评测 ≈20 s）
两者都远大于"纯权重扫描"在 holdout 上量到的天花板 +0.0035，所以**只调权重的提案在哪个 N
下都会被门禁拦下**——子集不是这个结论的成因，只是把一次 rule 提案（2 条）的成本从 40 s
压到 20 s。真正需要 n=1400 的场合是符号空间（synonyms / rewrite_rules）里效应稀疏的候选，
那时用 `--eval-sample 0` 跑满，并在产物里如实记下 N。
子集的 N、成员指纹、选取方式一律写进产物：两个提案器的对照必须在同一批查询上才算数。

**(4) 每条提案同时报告两种分析，但只有一种能决定门禁。**
符号空间的提案效应是稀疏的——引擎侧 `SearchQueryCompiler.applyRewrite` 走整串相等
比较 + 整条替换，一条 rewrite_rule 最多影响一条查询；`expandSynonyms` 走子串包含触发，
虽能泛化但也误伤。于是"30 条规则动了 30 条查询"在 n=1400 的均值上被稀释到贴着最小可
检出差以下。只报整体检验，读到的永远是"什么都没发生"，而实际可能是"20 条被显著改善、
10 条被改坏"——这两种事实在均值上长得一模一样。

因此 `assess()` 在门禁之后额外算一份**受影响子集**（`stats.affected_analysis`）：
逐 query_id 容差比对候选与基线，取指标真的动了的那些查询，在该子集上跑同一套配对检验。
两套结论在产物里**并列**呈现（`proposals[].conclusions`），并且：

  * (a) 整体配对检验 —— 预注册、全评测子集、**门禁的唯一依据**；
  * (b) 受影响子集分析 —— **事后条件化**（成员资格由候选自己的结果决定），只作描述性
        证据，p 值不具备预注册检验的错误率保证，**不得**与 (a) 同等看待。

门禁判定不读 (b) 的任何字段。这不是"暂时没接上"，而是接上就等于挑一个对自己有利的切片：
子集的选中条件恰好是"这条查询动了"，零差值样本被系统性排除，差值天然远离零。
产物里的 `affected_subset.used_by_gate` 恒为 false，并原样带上 `stats.POST_HOC_CAVEAT` 全文。

失败隔离：(b) 的计算包在自己的 try 里，出错只写进 `affected_subset_error`，
不改变 (a) 的结论也不改变提案的 status。描述性分析永远不该有能力改变门禁结局。


产物
----
每次 run() 写一份 JSON 到 experiments/：`propose-{提案器}-{run_tag}.json`。
`run_tag` 由调用方传入；不传就按目录里已有的同提案器产物数递增（run001、run002……）。
**模块内不取系统时间**——文件名里塞一个 wall clock 会让"同参数重跑得到同一份产物"这句话
失效，也会让两次运行无法按内容比对。产物里唯一的时间量是 `eval_seconds`
（`time.perf_counter()` 的单调差值，是耗时不是时刻），它不参与命名，也不参与任何选择。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import SearchOpsClient, ToolGatewayError
from .eval.gate import GateDecision, GatePolicy, evaluate_gate
from .eval.loader import by_query_id, load_run
from .eval.splits import Split, load_split, query_ids_hash, to_eval_queries
from .eval.stats import (
    AFFECTED_DETECT_METRICS,
    AFFECTED_TOLERANCE,
    POST_HOC_CAVEAT,
    AffectedSubset,
    PairedResult,
    affected_analysis,
    benjamini_hochberg,
)
from .models import EvaluationResult, StrategyConfig
from .proposers import Proposal, Proposer
from .tools import Tool, build_registry

_REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_SPLIT_MANIFEST = _REPO_ROOT / "experiments/split-manifest.json"
DEFAULT_TRAIN_BASELINE = _REPO_ROOT / "experiments/baseline-v7-train.json"
DEFAULT_ARTIFACTS_DIR = _REPO_ROOT / "experiments"

DEFAULT_EVAL_SAMPLE = 700
"""默认评测子集大小：train 前 700 条。0 / None 表示整个 train。理由见模块开头 (3)。"""

EVAL_K = 10
"""服务端目前只接受 k=10。"""

ARTIFACT_SCHEMA = "searchops.propose-run/2"
"""产物 schema 版本。

/2 相对 /1 是**纯增量**：/1 的每个字段都还在原位、含义不变，只是每条提案多了
`affected_subset` / `affected_subset_error` / `conclusions` 三个键，顶层多了
`field_notes`。按 /1 写的读取方不改代码也能继续读 /2。
版本号仍然要涨，否则下游无法区分"这份产物没有子集分析"和"这份产物的候选没受影响"。
"""

ARTIFACT_SCHEMA_SUPERSEDES = ("searchops.propose-run/1",)
"""本 schema 向后兼容的旧版本，原样写进产物供下游判断。"""

# 状态词表。集中在这里，免得报告与产物各写各的。
STATUS_SUBMITTED = "submitted"
STATUS_BLOCKED = "blocked"
STATUS_DRY_RUN = "gate-passed（dry_run，未建草稿）"
STATUS_ERROR = "error"

#: 产物里新增结构的字段说明。写进产物本身而不是只写在这里——下游读的是 JSON，
#: 而"这份 p 值不能当预注册结论用"这种限定条件必须跟着数据走，不能靠读代码的人记得。
FIELD_NOTES: dict[str, str] = {
    "schema": (
        "searchops.propose-run/2。相对 /1 是纯增量：/1 的所有字段原位保留、含义不变，"
        "新增 proposals[].affected_subset、proposals[].affected_subset_error、"
        "proposals[].conclusions 与顶层 field_notes / schema_supersedes。"
    ),
    "proposals[].gate": (
        "(a) 整体配对检验。预注册口径：全评测子集（evaluation_setup.eval_query_ids 全体）、"
        "多指标 BH 校正。**门禁判定的唯一依据**，promote / status 只由它决定。"
    ),
    "proposals[].affected_subset": (
        "(b) 受影响子集分析。先逐 query_id 容差比对候选与基线，取检出口径内任一指标"
        "真的发生变化的查询，再在该子集上跑同一套配对检验（同一 bootstrap / 置换检验 /"
        "iterations）。**事后条件化（post-hoc conditioning）**：成员资格由候选自己的评测"
        "结果决定，零差值样本被系统性排除，子集内差值天然远离零，p 值不具备预注册检验的"
        "错误率保证。只作描述性证据，不得与 (a) 同等看待。used_by_gate 恒为 false。"
    ),
    "proposals[].affected_subset.query_ids": (
        "受影响查询的 query_id 列表（升序，各检出指标的并集）。按指标拆分见 changed_by_metric。"
    ),
    "proposals[].affected_subset.paired": (
        "子集上的配对检验结果，形状与 gate.paired 一致，另带 wins/losses/ties 与 post_hoc=true。"
        "bh_pass 恒为 null：BH 控制的是预注册检验族的 FDR，而该检验不在那个族里，"
        "套一个 BH 会让它看起来像门禁族的一员。"
    ),
    "proposals[].affected_subset_error": (
        "子集分析自身的异常（字符串，正常为 null）。它与门禁完全隔离：这里非 null 时"
        "gate 结论与 status 照常有效。"
    ),
    "proposals[].conclusions": (
        "(a) 与 (b) 两套结论按指标并列，便于直接读出'均值没动但 N 条查询被改了'这种情形。"
        "gate_decided_by 恒为 'overall_prereg'。"
    ),
}


class BaselineMismatch(RuntimeError):
    """基线与当前环境/划分对不上。失败关闭：宁可不跑，也不比出一个来历不明的差值。"""


# --- 评测台（自证的那一侧） ----------------------------------------------------


def select_train_rows(split: Split, sample: int | None) -> tuple[list[dict[str, Any]], list[int]]:
    """从 train 里取确定性子集：置换序的**前 N 条**。理由见模块开头 (3)。

    附带一条结构性断言：选中的 query_id 不得与 holdout 相交。holdout 只在最终验收时用，
    绝不参与任何选择——这条断言让"纪律"可以被测试，而不只是被声明。
    """
    rows = list(split.train)
    if sample:
        if sample < 0:
            raise ValueError(f"eval_sample 不能为负：{sample}")
        if sample > len(rows):
            raise ValueError(f"eval_sample={sample} 超过 train 的 {len(rows)} 条")
        rows = rows[:sample]
    ids = [int(r["query_id"]) for r in rows]
    leaked = sorted(set(ids) & set(split.holdout_ids))
    if leaked:
        raise BaselineMismatch(f"评测子集混入了 {len(leaked)} 条 holdout 查询：{leaked[:5]}…")
    return rows, ids


@dataclass(frozen=True)
class TrainBench:
    """自证用的评测台：train 子集 + 同源基线。**holdout 不在这里出现。**"""

    manifest_path: str
    baseline_path: str
    split_seed: int
    split_total: int
    train_size: int
    sample_size: int
    query_ids: list[int]
    rows: list[dict[str, Any]]
    baseline: dict[int, dict[str, Any]]
    baseline_headline: dict[str, Any]
    baseline_field_weights: dict[str, float]
    subset_ids_sha256: str
    train_ids_sha256: str
    #: 整个 train 侧的 query_id 与查询文本 —— 用于把诊断证据限定在 train 内。
    #: 注意范围是**整个 train**而不是评测子集：证据来自 train 但落在子集之外是合法的
    #: （只是这一轮量不到它的效果），而来自 holdout 则是泄漏。两件事必须分开。
    split_train_ids: list[int]
    train_query_texts: frozenset[str]

    SELECTION = "train 置换序（sha256(f'{split_seed}|{query_id}') 升序）的前 N 条"

    @classmethod
    def load(
        cls,
        *,
        manifest_path: str | Path = DEFAULT_SPLIT_MANIFEST,
        baseline_path: str | Path = DEFAULT_TRAIN_BASELINE,
        sample: int | None = DEFAULT_EVAL_SAMPLE,
        queries_path: str | Path | None = None,
    ) -> TrainBench:
        split = load_split(manifest_path, queries_path)  # 内部已校验两侧指纹
        rows, ids = select_train_rows(split, sample)

        run = load_run(baseline_path)
        side = run.get("split")
        if side != "train":
            raise BaselineMismatch(
                f"基线文件的 split 是 {side!r}，不是 'train'。"
                "自证只能在 train 上做，holdout 不得进入提案闭环。"
            )
        recorded = run.get("query_ids_sha256")
        actual = query_ids_hash(split.train_ids)
        if recorded != actual:
            raise BaselineMismatch(f"基线的 train 指纹与 manifest 不符：{recorded} != {actual}")

        baseline_all = by_query_id(run)
        missing = [q for q in ids if q not in baseline_all]
        if missing:
            raise BaselineMismatch(f"基线缺少 {len(missing)} 条子集查询（如 {missing[:5]}）")

        return cls(
            manifest_path=str(manifest_path),
            baseline_path=str(baseline_path),
            split_seed=split.seed,
            split_total=split.total,
            train_size=len(split.train),
            sample_size=len(ids),
            query_ids=ids,
            rows=to_eval_queries(rows),
            # 只保留子集，配对对齐才是干净的 n:n，align_report 里不会出现"仅 baseline N 条"
            baseline={q: baseline_all[q] for q in ids},
            baseline_headline={
                k: run.get(k)
                for k in ("run_id", "strategy_version", "weights_from_strategy_version",
                          "query_count", "precision10", "recall10", "mrr10", "ndcg10",
                          "zero_result_rate", "generated_at")
            },
            baseline_field_weights={k: float(v) for k, v in (run.get("field_weights") or {}).items()},
            subset_ids_sha256=query_ids_hash(ids),
            train_ids_sha256=actual,
            split_train_ids=list(split.train_ids),
            train_query_texts=frozenset(
                str(r.get("query") or "").strip() for r in split.train if r.get("query")
            ),
        )

    def assert_matches_live(self, current: Any) -> None:
        """基线权重必须与线上 current strategy 一致，否则"候选 − 基线"里混进了策略漂移。"""
        live = {k: float(v) for k, v in (current.config.field_weights or {}).items()}
        if live != self.baseline_field_weights:
            raise BaselineMismatch(
                f"线上 v{current.version} 的权重 {live} 与基线 {self.baseline_field_weights} 不一致；"
                "拒绝拿它当参照点（请重建基线或指定正确的基线文件）"
            )

    def describe(self) -> dict[str, Any]:
        return {
            "split_manifest": self.manifest_path,
            "split_seed": self.split_seed,
            "split_total": self.split_total,
            "side": "train",
            "train_size": self.train_size,
            "eval_sample_size": self.sample_size,
            "selection": self.SELECTION,
            "subset_query_ids_sha256": self.subset_ids_sha256,
            "train_query_ids_sha256": self.train_ids_sha256,
            "baseline_run": self.baseline_path,
            "baseline_headline": self.baseline_headline,
            "baseline_field_weights": self.baseline_field_weights,
            "holdout_used": False,
        }


# --- 单个提案的结局 ------------------------------------------------------------


@dataclass
class Outcome:
    proposal: Proposal
    preview_changed: int | None = None
    preview_query: str | None = None
    evaluation: EvaluationResult | None = None
    eval_seconds: float | None = None
    decision: GateDecision | None = None
    #: (b) 受影响子集分析。事后条件化，纯描述性——**不参与 status / promote 的任何判定**。
    affected: AffectedSubset | None = None
    #: 子集分析自己出的错。单独记录，好让它永远无法把一条本来有结论的提案变成 ERROR。
    affected_error: str | None = None
    strategy_id: str | None = None
    status: str = "pending"
    error: str | None = None

    def render(self) -> str:
        head = f"[{self.origin_tag}] {self.proposal.name} → {self.status}"
        lines = [head, f"    理由：{self.proposal.rationale}"]
        if self.preview_changed is not None:
            lines.append(f"    干跑：{self.preview_changed} 个位次变化（探针 {self.preview_query!r}）")
        if self.evaluation is not None:
            lines.append(
                f"    自证：n={self.evaluation.query_count} "
                f"NDCG@10 {self.evaluation.ndcg10:.4f} Recall@10 {self.evaluation.recall10:.4f} "
                f"MRR@10 {self.evaluation.mrr10:.4f} "
                f"zero-result {self.evaluation.zero_result_rate:.4f}"
                + (f"（{self.eval_seconds:.1f}s）" if self.eval_seconds is not None else "")
            )
        if self.decision:
            lines.append("    (a) 整体配对检验（预注册，全评测子集，门禁依据）：")
            lines += ["    " + ln for ln in self.decision.render().splitlines()]
        if self.affected is not None:
            lines.append("    (b) 受影响子集（事后条件化，仅描述性，门禁不采信）：")
            lines += ["    " + ln for ln in self.affected.summary().splitlines()]
        if self.affected_error:
            lines.append(f"    受影响子集分析失败（不影响门禁结论）：{self.affected_error}")
        if self.error:
            lines.append(f"    错误：{self.error}")
        return "\n".join(lines)

    @property
    def origin_tag(self) -> str:
        return self.proposal.origin


@dataclass
class LoopReport:
    proposer: str = ""
    model: str | None = None
    run_tag: str = ""
    bench: dict[str, Any] = field(default_factory=dict)
    diagnosis_size: dict[str, int] = field(default_factory=dict)
    outcomes: list[Outcome] = field(default_factory=list)
    artifact_path: str | None = None

    @property
    def submitted(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status == STATUS_SUBMITTED]

    @property
    def errored(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status == STATUS_ERROR]

    def render(self) -> str:
        lines = [
            f"提案器 {self.proposer}" + (f"（模型 {self.model}）" if self.model else "")
            + f" · 运行 {self.run_tag}",
            f"自证评测：train 子集 {self.bench.get('eval_sample_size')} / "
            f"{self.bench.get('train_size')} 条（{self.bench.get('selection')}）"
            f"，基线 {Path(self.bench.get('baseline_run', '')).name}，holdout 未参与",
            f"诊断：{self.diagnosis_size}",
            f"提案 {len(self.outcomes)} 条，通过门禁并提交 {len(self.submitted)} 条",
            "分析口径：(a) 整体配对检验 = 预注册、全评测子集、门禁的唯一依据；"
            "(b) 受影响子集 = 事后条件化，仅描述提案实际改了哪些查询，不参与判定",
        ]
        lines += [o.render() for o in self.outcomes]
        if self.artifact_path:
            lines.append(f"产物：{self.artifact_path}")
        return "\n".join(lines)


# --- 闭环 ----------------------------------------------------------------------


class ProposalLoop:
    """诊断 → 提案 → 干跑 → 自证 → 门禁 → 提交。

    RuleProposer 与 LLMProposer 走**完全相同**的这条路径：闭环只调用 `Proposer.propose()`，
    之后每一条 Proposal 都进同一个 `assess()`。对照实验成立的前提就是这一点，
    因此这里不为任何提案器留分支。
    """

    def __init__(
        self,
        client: SearchOpsClient,
        proposer: Proposer,
        *,
        policy: GatePolicy | None = None,
        preview_query: str | None = None,
        dry_run: bool = True,
        split_manifest: str | Path = DEFAULT_SPLIT_MANIFEST,
        baseline_run: str | Path = DEFAULT_TRAIN_BASELINE,
        eval_sample: int | None = DEFAULT_EVAL_SAMPLE,
        artifacts_dir: str | Path = DEFAULT_ARTIFACTS_DIR,
        diagnose_limit: int = 50,
        iterations: int = 10_000,
        use_ai: bool = False,
        verify_baseline_against_service: bool = True,
        include_query_metrics: bool = True,
        affected_tolerance: float = AFFECTED_TOLERANCE,
    ) -> None:
        self.client = client
        self.proposer = proposer
        self.policy = policy or GatePolicy()
        self.preview_query = preview_query
        self.dry_run = dry_run
        self.split_manifest = Path(split_manifest)
        self.baseline_run = Path(baseline_run)
        self.eval_sample = eval_sample
        self.artifacts_dir = Path(artifacts_dir)
        self.diagnose_limit = diagnose_limit
        #: 诊断阶段被滤掉的行数（非 train 或归属不明）。写进产物，让泄漏防线可审计。
        self.diagnosis_dropped = 0
        self.iterations = iterations
        self.use_ai = use_ai
        self.verify_baseline_against_service = verify_baseline_against_service
        self.include_query_metrics = include_query_metrics
        #: 判定"这条查询变了"的浮点容差。可配是为了让测试能钉住边界，不是为了调松。
        self.affected_tolerance = affected_tolerance
        #: 通过注册表访问客户端，越权在构造期就会抛 GovernanceViolation
        self.tools: dict[str, Tool] = build_registry(client)
        self._bench: TrainBench | None = None

    # -- 阶段 -------------------------------------------------------------

    @property
    def bench(self) -> TrainBench:
        """评测台惰性加载：构造 Loop 不该依赖磁盘上的划分与基线（便于离线构造与测试）。"""
        if self._bench is None:
            self._bench = TrainBench.load(
                manifest_path=self.split_manifest,
                baseline_path=self.baseline_run,
                sample=self.eval_sample,
            )
        return self._bench

    def diagnose(self, limit: int | None = None) -> dict[str, Any]:
        """拉取诊断证据，**并把 holdout 查询滤掉**。

        为什么必须滤：zero_result_queries / low_quality_queries 读的是线上分析表，
        那张表按流量记录，不认识 train/holdout 的划分。不滤的后果实测发生过——
        模型拿到 holdout 查询当证据，写出逐字钉死该查询的 rewrite_rule。
        本轮评测只在 train 上做，所以门禁判定没被污染；但这类提案一旦拿去 holdout 验收，
        就等于"用考题训练"，那个数字再也不能用了。

        自证在 train 上做（`select_train_rows` 已断言与 holdout 无交集），
        提案的**证据**也必须限定在 train 上——两处都堵住，泄漏才真的不可能发生。

        分析表里的行不一定带 query_id（有些只有 query 文本），所以按 id 与文本双路匹配；
        两者都对不上的行一律丢弃：宁可少给证据，也不给来历不明的证据。
        """
        n = self.diagnose_limit if limit is None else limit
        zero = self._train_only(self.tools["zero_result_queries"](limit=n))
        low = self._train_only(self.tools["low_quality_queries"](limit=n))
        return {
            "zero_result_queries": zero,
            "low_quality_queries": low,
            "current": self.tools["current_strategy"](),
        }

    def _train_only(self, rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """只保留可确认属于 train 的行。无法确认归属的行同样丢弃（失败关闭）。"""
        allowed_ids = set(self.bench.split_train_ids)
        allowed_text = self.bench.train_query_texts
        kept, dropped = [], 0
        for row in rows or []:
            qid = row.get("query_id")
            text = str(row.get("query") or "").strip()
            if (qid is not None and int(qid) in allowed_ids) or (text and text in allowed_text):
                kept.append(row)
            else:
                dropped += 1
        if dropped:
            self.diagnosis_dropped += dropped
        return kept

    def preview(self, config: StrategyConfig, query: str) -> int:
        resp = self.tools["preview"](query=query, config=config)
        return len(resp.changed_positions)

    def self_evaluate(self, config: StrategyConfig) -> EvaluationResult:
        """自证：对提案里的候选配置跑一轮 train 子集离线评测。

        走 DRY_RUN 级的 evaluate_candidate——服务端强制 persist=false、不读写 strategy 版本，
        因此反复评测不污染策略历史与审计流。返回的 strategy_version 恒为 -1，
        这是"这不是某个已发布版本的跑分"的诚实标记，原样带进产物。
        """
        return self.tools["evaluate_candidate"](
            self.bench.rows, config, k=EVAL_K, use_ai=self.use_ai
        )

    def _affected(self, candidate: dict[int, dict[str, Any]], decision: GateDecision) -> AffectedSubset:
        """(b) 受影响子集分析。**纯描述性**，调用方不得据此改变任何判定。

        两处口径刻意各自对齐一件事：

        * 报告指标取 `decision.results` 的**实际**指标序列，而不是重新从 policy 拼一遍。
          门禁比了什么，子集就比什么——"整体 vs 子集"必须逐指标对得上，中间不能有
          第二份推导出来的指标列表偷偷分叉。
        * 检出口径取 主指标 + `AFFECTED_DETECT_METRICS`（去重保序）。主指标必须在里面
          （它是门禁看的那个），recall10 补一个盲区（召回集合变了但位次没变）。

        重采样参数（iterations / 默认 seed）与门禁完全一致：两套结论之间唯一的差别
        必须是样本集合，不能是统计实现或随机流。
        """
        metrics = tuple(dict.fromkeys(r.metric for r in decision.results))
        detect = tuple(dict.fromkeys((self.policy.primary_metric, *AFFECTED_DETECT_METRICS)))
        return affected_analysis(
            self.bench.baseline,
            candidate,
            metrics=metrics,
            detect_metrics=detect,
            tolerance=self.affected_tolerance,
            iterations=self.iterations,
        )

    def assess(self, proposal: Proposal, diagnosis: dict[str, Any]) -> Outcome:
        """一条提案的完整评审：干跑 → 自证 → 门禁 →（非 dry_run 时）建草稿并提交。

        两个提案器共用这一个方法，不存在"规则走这条、模型走那条"的可能。
        """
        outcome = Outcome(proposal=proposal)
        try:
            probe = self.preview_query or _first_query(diagnosis)
            if probe:
                outcome.preview_query = probe
                outcome.preview_changed = self.preview(proposal.config, probe)

            started = time.perf_counter()
            outcome.evaluation = self.self_evaluate(proposal.config)
            outcome.eval_seconds = time.perf_counter() - started

            candidate = {int(q.query_id): q.model_dump() for q in outcome.evaluation.queries}
            outcome.decision = evaluate_gate(
                self.bench.baseline, candidate, self.policy, iterations=self.iterations
            )

            # (b) 受影响子集。**只是描述**：下面的 status 分支一个字段都不读它。
            # 包在自己的 try 里，是为了让这条性质不依赖"这段代码没 bug"——
            # 子集分析炸掉时门禁结论照常成立，提案不会因此变成 ERROR。
            try:
                outcome.affected = self._affected(candidate, outcome.decision)
            except Exception as exc:
                outcome.affected_error = f"{type(exc).__name__}: {exc}"

            if not outcome.decision.promote:
                outcome.status = STATUS_BLOCKED
            elif self.dry_run:
                outcome.status = STATUS_DRY_RUN
            else:
                draft = self.tools["create_draft"](name=proposal.name, config=proposal.config)
                self.tools["submit"](strategy_id=draft.id)
                outcome.strategy_id, outcome.status = draft.id, STATUS_SUBMITTED
                # 到此为止。审批与发布属于人类角色，对应方法不在 tools.ALLOWED 里。
        except ToolGatewayError as exc:
            outcome.status, outcome.error = STATUS_ERROR, str(exc)
        except Exception as exc:  # 一条提案炸掉不该拖垮整轮对照
            outcome.status, outcome.error = STATUS_ERROR, f"{type(exc).__name__}: {exc}"
        return outcome

    def run(self, *, run_tag: str | None = None, write_artifact: bool = True) -> LoopReport:
        bench = self.bench  # 先加载评测台：划分/基线有问题就当场失败，不要跑完才发现
        diagnosis = self.diagnose()
        current = diagnosis["current"]
        if self.verify_baseline_against_service:
            bench.assert_matches_live(current)

        proposer_name = getattr(self.proposer, "name", type(self.proposer).__name__)
        report = LoopReport(
            proposer=proposer_name,
            model=_proposer_model(self.proposer),
            run_tag=run_tag or self._next_run_tag(proposer_name),
            bench=bench.describe(),
            diagnosis_size={
                "zero_result": len(diagnosis["zero_result_queries"]),
                "low_quality": len(diagnosis["low_quality_queries"]),
            },
        )

        for proposal in self.proposer.propose(diagnosis, current.config):
            report.outcomes.append(self.assess(proposal, diagnosis))

        if write_artifact:
            report.artifact_path = str(self._write_artifact(report, current))
        return report

    # -- 产物 -------------------------------------------------------------

    def _next_run_tag(self, proposer_name: str) -> str:
        """按目录里已有的同提案器产物数递增。**不取系统时间**（见模块开头"产物"）。"""
        existing = list(self.artifacts_dir.glob(f"propose-{proposer_name}-run*.json"))
        return f"run{len(existing) + 1:03d}"

    def _write_artifact(self, report: LoopReport, current: Any) -> Path:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifacts_dir / f"propose-{report.proposer}-{report.run_tag}.json"
        path.write_text(
            json.dumps(self._artifact(report, current), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def _artifact(self, report: LoopReport, current: Any) -> dict[str, Any]:
        return {
            "schema": ARTIFACT_SCHEMA,
            "schema_supersedes": list(ARTIFACT_SCHEMA_SUPERSEDES),
            "field_notes": FIELD_NOTES,
            "run_tag": report.run_tag,
            "proposer": report.proposer,
            "proposer_class": type(self.proposer).__name__,
            "model": report.model,
            "dry_run": self.dry_run,
            "agent_terminates_at": "SUBMITTED（approve / publish / rollback 不在工具注册表内）",
            "current_strategy": {
                "id": current.id,
                "version": current.version,
                "name": current.name,
                "status": current.status,
                "config": current.config.model_dump(),
            },
            "evaluation_setup": {
                **report.bench,
                "k": EVAL_K,
                "use_ai": self.use_ai,
                "safety_class": "DRY_RUN（evaluate_candidate：不落库、不碰 strategy 版本）",
                "eval_query_ids": self.bench.query_ids,
            },
            "gate_policy": {
                "primary_metric": self.policy.primary_metric,
                "require_significant": self.policy.require_significant,
                "max_zero_result_increase": self.policy.max_zero_result_increase,
                "max_catastrophic_ratio": self.policy.max_catastrophic_ratio,
                "catastrophic_drop": self.policy.catastrophic_drop,
                "guard_metrics": list(self.policy.guard_metrics),
                "bootstrap_permutation_iterations": self.iterations,
            },
            "diagnosis_size": report.diagnosis_size,
            "proposals": [
                self._outcome_payload(o, current.config) for o in report.outcomes
            ],
            "summary": {
                "proposed": len(report.outcomes),
                "blocked": sum(1 for o in report.outcomes if o.status == STATUS_BLOCKED),
                "submitted": len(report.submitted),
                "errors": len(report.errored),
                "statuses": [o.status for o in report.outcomes],
                # 追加项：一眼看出"门禁全拦下"到底是"什么都没动"还是"动了但均值测不出"。
                "affected_subset_sizes": [
                    (o.affected.size if o.affected is not None else None) for o in report.outcomes
                ],
                "affected_subset_is_post_hoc": True,
            },
        }

    def _outcome_payload(self, o: Outcome, current: StrategyConfig) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": o.proposal.name,
            "origin": o.proposal.origin,
            "rationale": o.proposal.rationale,
            "evidence": list(o.proposal.evidence),
            "config": o.proposal.config.model_dump(),
            "config_diff_vs_current": _config_diff(current, o.proposal.config),
            "preview_query": o.preview_query,
            "preview_changed_positions": o.preview_changed,
            "status": o.status,
            "strategy_id": o.strategy_id,
            "error": o.error,
            "eval_seconds": round(o.eval_seconds, 3) if o.eval_seconds is not None else None,
        }
        if o.evaluation is not None:
            payload["evaluation"] = _evaluation_payload(
                o.evaluation, self.bench, include_queries=self.include_query_metrics
            )
        if o.decision is not None:
            payload["gate"] = _decision_payload(o.decision)
        # 新增字段一律**追加**，绝不改动上面任何一个既有键的含义（schema /2 是 /1 的超集）。
        payload["affected_subset"] = _affected_payload(o.affected)
        payload["affected_subset_error"] = o.affected_error
        payload["conclusions"] = _conclusions_payload(o.decision, o.affected)
        return payload


# --- 序列化助手 ----------------------------------------------------------------


def _evaluation_payload(
    result: EvaluationResult, bench: TrainBench, *, include_queries: bool
) -> dict[str, Any]:
    """候选评测的记录。形状与 baseline-v7-train.json 对齐，产物因此可以被单独复检。"""
    run: dict[str, Any] = {
        "run_id": result.run_id,
        "strategy_version": result.strategy_version,  # -1：候选评测的诚实标记
        "strategy_source": "candidate",
        "query_count": result.query_count,
        "precision10": result.precision10,
        "recall10": result.recall10,
        "mrr10": result.mrr10,
        "ndcg10": result.ndcg10,
        "zero_result_rate": result.zero_result_rate,
        "generated_at": result.generated_at,
        "split": "train",
        "split_seed": bench.split_seed,
        "split_total": bench.split_total,
        "query_ids_sha256": bench.subset_ids_sha256,
    }
    if include_queries:
        run["queries"] = [q.model_dump() for q in result.queries]
    return run


def _paired_payload(r: PairedResult, bh_pass: bool | None) -> dict[str, Any]:
    """一条配对检验结果的产物形状。

    新增的 `scope` / `post_hoc` 两个键默认按"整体、预注册"填；`_affected_payload`
    会把它们覆写成子集口径。每条 paired 记录都自带口径标记，是为了让任何一条被单独
    摘出来引用时都还带着它的适用范围——摘引用的人不一定回头看上下文。
    """
    return {
        "scope": "overall_prereg",
        "post_hoc": False,
        "metric": r.metric,
        "n": r.n,
        "baseline_mean": r.baseline_mean,
        "candidate_mean": r.candidate_mean,
        "delta": r.delta,
        "ci_low": r.ci_low,
        "ci_high": r.ci_high,
        "p_value": r.p_value,
        "cliffs_delta": r.cliffs_delta,
        "significant_by_p": r.significant,
        "ci_excludes_zero": r.ci_excludes_zero,
        "bh_pass": bh_pass,
        "effect": r.effect_label,
        "verdict": r.verdict_label,
        "summary": r.summary(),
    }


def _brief(r: PairedResult) -> dict[str, Any]:
    """并列表格里的精简版配对结论。字段是 _paired_payload 的真子集，含义逐字相同。"""
    return {
        "n": r.n,
        "delta": r.delta,
        "ci_low": r.ci_low,
        "ci_high": r.ci_high,
        "p_value": r.p_value,
        "cliffs_delta": r.cliffs_delta,
        "effect": r.effect_label,
        "significant_by_p": r.significant,
        "ci_excludes_zero": r.ci_excludes_zero,
        "verdict": r.verdict_label,
    }


def _affected_payload(a: AffectedSubset | None) -> dict[str, Any] | None:
    """(b) 的产物形状。

    `post_hoc` / `used_by_gate` / `caveat` 三个字段是**冗余的**，而且是故意冗余：
    下游可能只读其中任意一个，限定条件必须在每条读取路径上都撞得到，不能只写在 README。
    """
    if a is None:
        return None
    paired = []
    for r in a.results:
        wl = a.win_loss.get(r.metric)
        entry = _paired_payload(r, None)  # bh_pass=None：子集不属于预注册检验族，见下面的 note
        entry.update(
            scope="affected_post_hoc",
            post_hoc=True,
            bh_note="子集不做 BH 校正：BH 控制的是预注册检验族的 FDR，该检验不在那个族里",
            wins=wl.wins if wl is not None else None,
            losses=wl.losses if wl is not None else None,
            ties=wl.ties if wl is not None else None,
        )
        paired.append(entry)
    return {
        "post_hoc": True,
        "used_by_gate": False,
        "caveat": a.caveat,
        "definition": a.definition,
        "detect_metrics": list(a.detect_metrics),
        "tolerance": a.tolerance,
        "overall_n": a.overall_n,
        "affected_n": a.size,
        "affected_ratio": a.ratio,
        "is_empty": a.is_empty,
        "query_ids": list(a.query_ids),
        "changed_by_metric": {
            m: {"n": len(ids), "query_ids": list(ids)} for m, ids in a.changed_by_metric.items()
        },
        "zero_result_flips": {
            "n": len(a.zero_result_flip_ids),
            "query_ids": list(a.zero_result_flip_ids),
            "note": "单独报告，**不**并入子集定义（子集按指标值变化定义）；"
                    "两侧指标都为 0 时它是唯一能看见'结果换了一批'的信号",
        },
        "paired": paired,
        "summary": a.summary(),
    }


def _conclusions_payload(
    d: GateDecision | None, a: AffectedSubset | None
) -> dict[str, Any] | None:
    """(a) 与 (b) 逐指标并列。

    并列本身就是结论的一部分：稀疏效应下 (a) 常常是"Δ+0.0003，p=0.6，不显著"，
    而 (b) 可能是"n=23，Δ+0.06，win 15 / loss 8"。两行摆在一起，读者才看得出
    "什么都没发生"与"动了 23 条、有涨有跌"的区别；单看任何一行都会读错。
    """
    if d is None:
        return None
    rows = []
    for r in d.results:
        sub = a.result_for(r.metric) if a is not None else None
        wl = a.win_loss.get(r.metric) if a is not None else None
        row: dict[str, Any] = {"metric": r.metric, "overall_prereg": _brief(r)}
        if sub is None:
            row["affected_post_hoc"] = None
        else:
            row["affected_post_hoc"] = {
                **_brief(sub),
                "wins": wl.wins if wl is not None else None,
                "losses": wl.losses if wl is not None else None,
                "ties": wl.ties if wl is not None else None,
            }
        rows.append(row)
    return {
        "gate_decided_by": "overall_prereg",
        "overall_prereg_note": "预注册、全评测子集、BH 校正；promote 只由它决定",
        "affected_post_hoc_note": POST_HOC_CAVEAT,
        "overall_n": d.results[0].n if d.results else None,
        "affected_n": a.size if a is not None else None,
        "affected_subset_empty": a.is_empty if a is not None else None,
        "by_metric": rows,
    }


def _decision_payload(d: GateDecision) -> dict[str, Any]:
    # BH 标志用与 gate.evaluate_gate 相同的函数、相同的入参重算，纯函数，结果一致。
    flags = benjamini_hochberg([r.p_value for r in d.results])
    return {
        "promote": d.promote,
        "verdict": "PROMOTE" if d.promote else "BLOCK",
        "alignment": d.alignment,
        "reasons": list(d.reasons),
        "paired": [_paired_payload(r, f) for r, f in zip(d.results, flags)],
    }


def _config_diff(current: StrategyConfig, candidate: StrategyConfig) -> dict[str, Any]:
    cur, new = current.model_dump(), candidate.model_dump()
    return {k: {"from": cur.get(k), "to": v} for k, v in new.items() if cur.get(k) != v}


def _proposer_model(proposer: Any) -> str | None:
    """提案器用的模型名。规则提案器没有模型，返回 None——对照报告里这一栏必须能区分两者。"""
    for attr in ("model", "model_name", "llm_model"):
        value = getattr(proposer, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def _first_query(diagnosis: dict[str, Any]) -> str | None:
    for key in ("low_quality_queries", "zero_result_queries"):
        items = diagnosis.get(key) or []
        if items:
            return str(items[0].get("query") or "") or None
    return None


__all__ = [
    "ProposalLoop",
    "LoopReport",
    "Outcome",
    "TrainBench",
    "BaselineMismatch",
    "select_train_rows",
    "DEFAULT_EVAL_SAMPLE",
    "DEFAULT_SPLIT_MANIFEST",
    "DEFAULT_TRAIN_BASELINE",
    "DEFAULT_ARTIFACTS_DIR",
    "ARTIFACT_SCHEMA",
    "ARTIFACT_SCHEMA_SUPERSEDES",
    "FIELD_NOTES",
]
