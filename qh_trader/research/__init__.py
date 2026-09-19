"""策略研究与快速回测通道."""

from .cost_assumptions import ResearchCostModel
from .vector_backtest import ScanResult, scan_dma_parameters

__all__ = [
    "ResearchCostModel",
    "ScanResult",
    "scan_dma_parameters",
]
