"""读取 platform 产出的评测结果。

`make evaluate` 会写 `platform/data/processed/evaluation-latest.json`，其中 `queries`
是逐查询指标数组——这正是配对检验所需的粒度，无需额外埋点。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_LATEST = Path(__file__).resolve().parents[3] / "platform/data/processed/evaluation-latest.json"


def load_run(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def by_query_id(run: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """把 queries 数组转成 query_id → 指标 的映射，供配对对齐使用。"""
    return {int(q["query_id"]): q for q in run.get("queries", [])}


def headline(run: dict[str, Any]) -> str:
    return (
        f"strategy v{run.get('strategy_version')} · {run.get('query_count')} 条查询 · "
        f"P@10 {run.get('precision10'):.4f} · R@10 {run.get('recall10'):.4f} · "
        f"MRR@10 {run.get('mrr10'):.4f} · NDCG@10 {run.get('ndcg10'):.4f} · "
        f"zero-result {run.get('zero_result_rate'):.4f}"
    )


def load_latest() -> dict[str, Any]:
    return load_run(DEFAULT_LATEST)
