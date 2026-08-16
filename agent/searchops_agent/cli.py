"""命令行入口。

    python -m searchops_agent.cli tools              查看提案者可见的工具与安全等级
    python -m searchops_agent.cli show   <run.json>  打印一次评测总览
    python -m searchops_agent.cli selfcheck [run]    零假设自检：同一份数据应判不显著
    python -m searchops_agent.cli compare <a> <b>    配对比较两次评测
    python -m searchops_agent.cli gate    <a> <b>    对候选执行晋级门禁
    python -m searchops_agent.cli propose --proposer rule|llm
"""

from __future__ import annotations

import argparse
import sys

from .client import SearchOpsClient
from .eval.gate import GatePolicy, evaluate_gate
from .eval.loader import DEFAULT_LATEST, by_query_id, headline, load_run
from .eval.stats import align_report, compare
from .loop import ProposalLoop
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

    p = sub.add_parser("propose", help="跑一轮诊断与提案")
    p.add_argument("--proposer", choices=("rule", "llm"), default="rule")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--apply", action="store_true", help="通过门禁后真正建草稿并提交待审")

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

    proposer = RuleProposer() if args.proposer == "rule" else LLMProposer()
    with SearchOpsClient(base_url=args.base_url) as c:
        report = ProposalLoop(c, proposer, dry_run=not args.apply).run()
        print(report.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
