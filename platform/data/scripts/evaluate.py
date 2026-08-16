#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

PROCESSED_DIR = Path(__file__).resolve().parents[1] / "processed"

# 两种跑法写两个文件。以前输出路径写死成 evaluation-latest.json，跑第二次直接把上一次
# 覆盖掉——正好是 A/B 对照最不能出的事：BM25 基线和 AI 候选互相踩，两份数据都作废。
# baselines/ 下的那份是只读归档，不是工作副本，不能拿它当"反正还有备份"的理由。
BASELINE_OUTPUT = PROCESSED_DIR / "evaluation-latest.json"
AI_OUTPUT = PROCESSED_DIR / "evaluation-ai-latest.json"

# 无 AI 时的客户端超时预算：每条查询 3 秒、下限 120 秒。取值与改动前完全一致，
# 保证 v7 基线的复现路径连超时行为都没变。
SECONDS_PER_QUERY = 3
MINIMUM_TIMEOUT_SECONDS = 120

# 与 .env 中同名变量对齐的兜底值。注意这两个变量的单位是**毫秒**。
DEFAULT_AI_TIMEOUT_MS = 5000
DEFAULT_AI_CONNECT_TIMEOUT_MS = 1000

SUMMARY_KEYS = ("run_id", "strategy_version", "query_count", "precision10", "recall10", "mrr10", "ndcg10", "zero_result_rate", "generated_at")

# 这两个状态说明 AI 适配器确实被调到了（NO_CHANGE 只是这条查询没命中改写规则）。
# 其余状态意味着服务端整轮都会静默退回 BM25，却照样返回 200——跑完只会得到一份
# 贴着 AI 标签的 BM25 结果，比没跑更糟。
AI_HEALTHY_STATUSES = ("APPLIED", "NO_CHANGE")


def env_int(name: str, default: int) -> int:
    # 环境变量由 evaluate.sh 从 .env 注入。写错了就退回默认值并出声，
    # 不值得让整轮评测崩在起跑线上。
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"warning: {name}={raw!r} is not an integer, falling back to {default}", file=sys.stderr)
        return default


def ai_budget_seconds() -> float:
    """单条查询最坏情况下花在 AI 上的秒数（连接超时耗尽后读超时也耗尽）。"""
    return (env_int("AI_CONNECT_TIMEOUT_MS", DEFAULT_AI_CONNECT_TIMEOUT_MS)
            + env_int("AI_TIMEOUT_MS", DEFAULT_AI_TIMEOUT_MS)) / 1000


def client_timeout(query_count: int, use_ai: bool) -> int:
    """客户端读超时预算，随"是否开 AI"与配置的 AI 超时一起伸缩。

    为什么不能继续写死 max(120, n*3)：EvaluationService 是**串行** for 循环，
    每条查询依次等 AI 返回。AI 读超时从 400ms 抬到 5000ms 之后，200 条查询的
    最坏服务端耗时是 200×(连接+读)=1000s，早已超过 600s 的客户端预算——
    一开 AI 客户端就先超时，报出来的却是与 AI 质量毫无关系的 socket timeout。
    """
    budget = max(MINIMUM_TIMEOUT_SECONDS, query_count * SECONDS_PER_QUERY)
    if not use_ai:
        return budget
    # 每条查询按"BM25 检索耗时 + AI 最坏耗时"计，再整体留一个下限量的余量，
    # 覆盖建连、序列化与落库这些不随查询条数线性增长的开销。
    return max(budget, int(query_count * (SECONDS_PER_QUERY + ai_budget_seconds())) + MINIMUM_TIMEOUT_SECONDS)


def probe_ai(search_url: str, query: str) -> dict[str, str | None]:
    """开跑前先用一条真实检索探一次 AI 链路，拿到实际生效的 provider 与状态。

    为什么必须探：EvaluationResult 只回显 use_ai，不带 provider；而 AI 一旦降级
    （kill switch 关着、适配器没起、超时），服务端静默退回 BM25 并照常返回 200。
    不探就只能等 200 条查询跑完二十分钟，再对着一份假的"AI 结果"猜哪里不对。
    """
    parameters = urllib.parse.urlencode({"q": query, "size": 1, "use_ai": "true"})
    request = urllib.request.Request(
        f"{search_url}/api/v1/search?{parameters}",
        headers={"X-Request-ID": "offline-evaluation-ai-probe"},
    )
    with urllib.request.urlopen(request, timeout=max(60, int(ai_budget_seconds()) + 30)) as response:
        probe = json.load(response)
    # ai_provider 受 spring.jackson.default-property-inclusion=non_null 影响，
    # 降级时整个字段会从响应里消失，所以一律用 get 而不是下标。
    return {
        "ai_status": probe.get("ai_status"),
        "ai_provider": probe.get("ai_provider"),
        "probe_query": probe.get("original_query", query),
        "probe_effective_query": probe.get("effective_query"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=Path, default=PROCESSED_DIR / "queries.jsonl")
    # 默认输出路径取决于 --use-ai，只能等解析完再决定，所以这里留 None。
    parser.add_argument("--output", type=Path, default=None,
                        help=f"结果文件；默认 {BASELINE_OUTPUT.name}，带 --use-ai 时默认 {AI_OUTPUT.name}")
    parser.add_argument("--search-url", default="http://localhost:8080")
    parser.add_argument("--limit", type=int, default=200)
    # 默认必须保持关闭：v7 的 200 条查询基线是在无 AI 条件下产生的，
    # 默认值一变，新旧结果就不可比。
    parser.add_argument("--use-ai", action="store_true", help="开启 AI 查询改写（默认关闭，保持 BM25 基线可比）")
    parser.add_argument("--timeout", type=float, default=None, help="客户端读超时秒数；默认按查询条数与 AI 超时预算推算")
    # persist 的默认值同样取决于 --use-ai，见下面 resolve 处的说明。
    parser.add_argument("--persist", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args()

    output = args.output if args.output is not None else (AI_OUTPUT if args.use_ai else BASELINE_OUTPUT)
    # 为什么 AI 跑默认不落库：quality_metrics 的唯一键是 (query_id, strategy_version)，
    # 服务端用的是 ON CONFLICT DO UPDATE。同一个 strategy_version 下跑一次 AI，
    # 就会把该版本的逐查询 BM25 指标原地覆盖掉，而 SearchAnalyticsRepository
    # 正是从这张表读"低 NDCG 查询"喂给运营后台。文件分开了、库里还混着一样没法对照。
    # 无 AI 路径的默认值保持 True，与改动前一致。
    persist = (not args.use_ai) if args.persist is None else args.persist

    queries = []
    with args.queries.open(encoding="utf-8") as source:
        for line in source:
            if len(queries) >= args.limit:
                break
            item = json.loads(line)
            queries.append({"query_id": item["query_id"], "query": item["query"], "judgments": item["judgments"]})

    body = {"queries": queries, "k": 10, "persist": persist}
    ai_context: dict[str, str | None] = {"ai_status": None, "ai_provider": None}
    if args.use_ai:
        # 只在开 AI 时才带 use_ai 字段：不带时请求体与改动前逐字节一致，
        # 连还没有 use_ai 字段的旧服务端构建也能照常复现基线。
        body["use_ai"] = True
        ai_context = probe_ai(args.search_url, queries[0]["query"])
        status = ai_context.get("ai_status")
        if status is not None and status not in AI_HEALTHY_STATUSES:
            raise SystemExit(f"AI probe returned ai_status={status}; refusing to write a BM25 fallback run to {output}")

    timeout = args.timeout if args.timeout is not None else client_timeout(len(queries), args.use_ai)
    payload = json.dumps(body).encode()
    request = urllib.request.Request(
        f"{args.search_url}/api/v1/evaluations/run",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "X-Request-ID": "offline-evaluation"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)

    # 落盘时补一段客户端上下文：服务端的 use_ai 只说明"请求要求了 AI"，
    # 说不出 provider 是谁、这一跑用了哪些参数。放在独立的 evaluation_client 下，
    # 不去伪造服务端契约里的字段。
    result["evaluation_client"] = {
        "use_ai": args.use_ai,
        "ai_provider": ai_context.get("ai_provider"),
        "ai_status": ai_context.get("ai_status"),
        "query_limit": args.limit,
        "persist": persist,
        "client_timeout_seconds": timeout,
        "search_url": args.search_url,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, delete=False) as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(output)

    # 摘要里必须带 AI 开关与 provider：两种跑法的指标块长得一模一样，
    # 终端上分不清哪一份是基线、哪一份是 AI，对照就只能靠记忆。
    summary = {key: result[key] for key in SUMMARY_KEYS if key in result}
    summary["use_ai"] = result.get("use_ai", args.use_ai)
    summary["ai_provider"] = ai_context.get("ai_provider")
    summary["ai_status"] = ai_context.get("ai_status")
    summary["output"] = str(output)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
