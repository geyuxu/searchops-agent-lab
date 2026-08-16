"""命令行入口。

    python -m searchops_agent.cli tools              查看提案者可见的工具与安全等级
    python -m searchops_agent.cli show   <run.json>  打印一次评测总览
    python -m searchops_agent.cli selfcheck [run]    零假设自检：同一份数据应判不显著
    python -m searchops_agent.cli compare <a> <b>    配对比较两次评测
    python -m searchops_agent.cli gate    <a> <b>    对候选执行晋级门禁
    python -m searchops_agent.cli propose --proposer rule|llm

`propose` 是完整闭环：诊断 → 提案 → 干跑预览 → **自证评测**（train 子集，DRY_RUN）→
门禁 → （--apply 时）建草稿并提交待审。基线读 experiments/baseline-v7-train.json，不重跑；
holdout 不参与其中任何一步。每次运行写一份 experiments/propose-{提案器}-{run_tag}.json。
"""

from __future__ import annotations

import argparse
import sys

from .client import SearchOpsClient
from .eval.gate import GatePolicy, evaluate_gate
from .eval.loader import DEFAULT_LATEST, by_query_id, headline, load_run
from .eval.stats import align_report, compare
from .loop import (
    DEFAULT_ARTIFACTS_DIR,
    DEFAULT_EVAL_SAMPLE,
    DEFAULT_SPLIT_MANIFEST,
    DEFAULT_TRAIN_BASELINE,
    ProposalLoop,
)
from .tools import build_registry, describe


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="searchops-agent")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tools", help="打印工具注册表与安全等级")

    p = sub.add_parser("show"); p.add_argument("run")
    p = sub.add_parser("selfcheck"); p.add_argument("run", nargs="?", default=str(DEFAULT_LATEST))

    for name in ("compare", "gate"):
        p = sub.add_parser(name)
        p.add_argument("baseline")
        p.add_argument("candidate")
        p.add_argument("--metric", default="ndcg10")
        p.add_argument("--iterations", type=int, default=10_000)

    p = sub.add_parser("propose", help="跑一轮闭环：诊断 → 提案 → 干跑 → 自证评测 → 门禁 → 提交待审")
    p.add_argument("--proposer", choices=("rule", "llm"), default="rule")
    p.add_argument("--model", default=None, help="LLM 提案器的模型名（--proposer llm 时生效）")
    p.add_argument("--limit", type=int, default=50, help="诊断时每类问题查询取多少条")
    p.add_argument("--apply", action="store_true", help="通过门禁后真正建草稿并提交待审")
    p.add_argument(
        "--eval-sample", type=int, default=DEFAULT_EVAL_SAMPLE,
        help=f"自证评测用的 train 子集条数（置换序前 N 条，确定性）；0 表示整个 train。默认 {DEFAULT_EVAL_SAMPLE}",
    )
    p.add_argument("--split-manifest", default=str(DEFAULT_SPLIT_MANIFEST))
    p.add_argument("--baseline", default=str(DEFAULT_TRAIN_BASELINE), help="train 侧基线（不重跑）")
    p.add_argument("--artifacts-dir", default=str(DEFAULT_ARTIFACTS_DIR))
    p.add_argument(
        "--run-tag", default=None,
        help="产物文件名里的运行标识（时间戳由调用方给）；不传则按目录里已有的同提案器产物序号递增",
    )
    p.add_argument("--metric", dest="primary_metric", default="ndcg10", help="门禁主指标")
    p.add_argument("--iterations", dest="stat_iterations", type=int, default=10_000)
    p.add_argument("--timeout", type=float, default=900.0, help="HTTP 超时（整轮评测可能几十秒）")
    p.add_argument("--no-artifact", action="store_true", help="不写产物 JSON")
    p.add_argument("--slim", action="store_true", help="产物里不写逐查询指标")
    p.add_argument(
        "--allow-baseline-drift", action="store_true",
        help="跳过『基线权重必须等于线上 current strategy』的检查（默认失败关闭）",
    )

    args = ap.parse_args(argv)

    if args.cmd == "tools":
        with SearchOpsClient(base_url=args.base_url) as c:
            print(describe(build_registry(c)))
        return 0

    if args.cmd == "show":
        print(headline(load_run(args.run)))
        return 0

    if args.cmd == "selfcheck":
        run = load_run(args.run)
        m = by_query_id(run)
        print(f"零假设自检 · {headline(run)}")
        print(f"  {align_report(m, m)}")
        for r in compare(m, m, iterations=2000):
            assert not r.significant, f"自检失败：同一份数据被判显著（{r.metric}）"
            print(f"  {r.summary()}")
        print("  ✓ 同一份数据与自身比较全部不显著——检验无系统性偏向")
        return 0

    if args.cmd in ("compare", "gate"):
        base, cand = by_query_id(load_run(args.baseline)), by_query_id(load_run(args.candidate))
        if args.cmd == "compare":
            print(align_report(base, cand))
            for r in compare(base, cand, iterations=args.iterations):
                print(f"  {r.summary()}")
            return 0
        decision = evaluate_gate(base, cand, GatePolicy(primary_metric=args.metric), iterations=args.iterations)
        print(decision.render())
        return 0 if decision.promote else 1

    from .proposers import LLMProposer, RuleProposer

    if args.proposer == "rule":
        proposer = RuleProposer()
    else:
        proposer = LLMProposer(model=args.model) if args.model else LLMProposer()

    with SearchOpsClient(base_url=args.base_url, timeout=args.timeout) as c:
        loop = ProposalLoop(
            c,
            proposer,
            policy=GatePolicy(primary_metric=args.primary_metric),
            dry_run=not args.apply,
            split_manifest=args.split_manifest,
            baseline_run=args.baseline,
            eval_sample=args.eval_sample,
            artifacts_dir=args.artifacts_dir,
            diagnose_limit=args.limit,
            iterations=args.stat_iterations,
            verify_baseline_against_service=not args.allow_baseline_drift,
            include_query_metrics=not args.slim,
        )
        report = loop.run(run_tag=args.run_tag, write_artifact=not args.no_artifact)
        print(report.render())
    # 门禁 BLOCK 是完全可接受的结果，不算失败；只有真出错才返回非零。
    return 1 if report.errored else 0


if __name__ == "__main__":
    sys.exit(main())
