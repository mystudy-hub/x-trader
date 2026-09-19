"""策略研究与快速回测通道."""

from .cost_assumptions import ResearchCostModel
from .vector_backtest import ScanResult, VectorFill, dma_signals, scan_dma_parameters, simulate_dma

__all__ = [
    "ResearchCostModel",
    "ScanResult",
    "VectorFill",
    "dma_signals",
    "scan_dma_parameters",
    "simulate_dma",
]
