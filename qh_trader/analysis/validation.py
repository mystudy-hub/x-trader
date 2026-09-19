"""[Analysis 层] 样本划分与敏感性验证 (S3-07, FR-VAL-01, FR-VAL-02, A17).

- 样本划分只按时间先后单向切分；样本外不参与调参；
- walk-forward 支持 purge（训练与测试之间留空 gap）避免相邻 Bar 泄漏；
- 敏感性矩阵把手续费、滑点、参与率、触价规则、涨跌停情景逐维扰动，
  保存每次试验的结果或失败原因 (硬约束：保存试验数量与失败结果)。
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class SampleSplit(Generic[T]):
    """样本划分块 (样本内 / 样本外)."""

    in_sample: tuple[T, ...]
    out_of_sample: tuple[T, ...]

    def __len__(self) -> int:
        return len(self.in_sample) + len(self.out_of_sample)


def train_test_split(items: Sequence[T], *, split_ratio: float = 0.7) -> tuple[tuple[T, ...], tuple[T, ...]]:
    """按时间先后顺序进行严格单向切分 (无未来信息渗透)."""
    if not (0.0 < split_ratio < 1.0):
        raise ValueError("split_ratio must be between 0 and 1")
    n = len(items)
    if n == 0:
        return (), ()
    split_point = int(n * split_ratio)
    return tuple(items[:split_point]), tuple(items[split_point:])


def walk_forward_slices(
    items: Sequence[T],
    *,
    train_size: int,
    test_size: int,
    gap: int = 0,
) -> list[tuple[tuple[T, ...], tuple[T, ...]]]:
    """生成滑动窗口样本切片；gap 为训练末尾与测试开头之间留空的样本数 (purge)."""
    if train_size <= 0 or test_size <= 0:
        raise ValueError("train_size and test_size must be positive integers")
    if gap < 0:
        raise ValueError("gap cannot be negative")

    slices = []
    start = 0
    while start + train_size + gap + test_size <= len(items):
        train = tuple(items[start : start + train_size])
        test_start = start + train_size + gap
        test = tuple(items[test_start : test_start + test_size])
        slices.append((train, test))
        start += test_size
    return slices


# ---------------------------------------------------------------------- 敏感性分析


@dataclass(frozen=True, slots=True)
class SensitivityTrial:
    """一次敏感性试验：参数扰动、结果摘要或失败原因."""

    trial_id: int
    parameters: Mapping[str, str]
    succeeded: bool
    summary: Mapping[str, str] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SensitivityReport:
    baseline: Mapping[str, str]
    dimensions: Mapping[str, tuple[str, ...]]
    trials: tuple[SensitivityTrial, ...]

    @property
    def trial_count(self) -> int:
        return len(self.trials)

    @property
    def failure_count(self) -> int:
        return sum(1 for t in self.trials if not t.succeeded)

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline": dict(self.baseline),
            "dimensions": {k: list(v) for k, v in self.dimensions.items()},
            "trial_count": self.trial_count,
            "failure_count": self.failure_count,
            "trials": [
                {
                    "trial_id": t.trial_id,
                    "parameters": dict(t.parameters),
                    "succeeded": t.succeeded,
                    "summary": dict(t.summary),
                    "error": t.error,
                }
                for t in self.trials
            ],
        }


def run_sensitivity(
    baseline: Mapping[str, Any],
    dimensions: Mapping[str, Sequence[Any]],
    runner: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    *,
    mode: str = "one_at_a_time",
) -> SensitivityReport:
    """逐维 (one_at_a_time) 或全组合 (grid) 扰动基线参数并运行 runner；失败被记录而不中止."""
    if mode not in {"one_at_a_time", "grid"}:
        raise ValueError("mode must be 'one_at_a_time' or 'grid'")
    trials: list[SensitivityTrial] = []
    combos: list[dict[str, Any]] = []
    if mode == "one_at_a_time":
        for name, values in dimensions.items():
            for value in values:
                params = dict(baseline)
                params[name] = value
                combos.append(params)
    else:
        names = list(dimensions)
        for values in itertools.product(*(dimensions[n] for n in names)):
            params = dict(baseline)
            params.update(dict(zip(names, values, strict=True)))
            combos.append(params)

    for index, params in enumerate(combos, start=1):
        shown = {k: _text(v) for k, v in params.items()}
        try:
            summary = runner(params)
            trials.append(SensitivityTrial(index, shown, True, {k: _text(v) for k, v in summary.items()}))
        except Exception as exc:  # noqa: BLE001 - 失败结果必须被保存而不是中止整个矩阵
            trials.append(SensitivityTrial(index, shown, False, {}, f"{type(exc).__name__}: {exc}"))

    return SensitivityReport(
        baseline={k: _text(v) for k, v in baseline.items()},
        dimensions={k: tuple(_text(v) for v in vs) for k, vs in dimensions.items()},
        trials=tuple(trials),
    )


def _text(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)
