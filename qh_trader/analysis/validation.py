"""[Analysis 层] 样本划分与敏感性验证 (S3-07, FR-VAL-01, FR-VAL-02).

包含：
- 样本内 (In-Sample) / 样本外 (Out-of-Sample) 严格时间切分
- 滚动验证 (Walk-Forward Validation) 分块
- 确保样本外未参与调参，拟合数据不越过时间边界 (A17)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class SampleSplit(Sequence[T]):
    """样本划分块."""
    in_sample: tuple[T, ...]
    out_of_sample: tuple[T, ...]


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
) -> list[tuple[tuple[T, ...], tuple[T, ...]]]:
    """生成滑动窗口样本切片."""
    if train_size <= 0 or test_size <= 0:
        raise ValueError("train_size and test_size must be positive integers")

    slices = []
    start = 0
    while start + train_size + test_size <= len(items):
        train = tuple(items[start : start + train_size])
        test = tuple(items[start + train_size : start + train_size + test_size])
        slices.append((train, test))
        start += test_size

    return slices
