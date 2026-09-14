"""Immutable dataset manifests captured by readers; full experiment manifests remain an S3 task."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from qh_trader.core.objects import freeze_payload


@dataclass(frozen=True, slots=True)
class DataSnapshot:
    snapshot_id: str | None
    datasets: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "datasets", freeze_payload(self.datasets))

    def as_dict(self) -> dict[str, Any]:
        return json.loads(
            json.dumps({"snapshot_id": self.snapshot_id, "datasets": self.datasets}, default=dict, allow_nan=False)
        )
