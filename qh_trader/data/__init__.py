"""第 3 层适配器：数据契约、日历与持久化数据源."""

from .downloader import FuturesDataDownloader, parse_instrument
from .replay import HistoricalMarketDataAdapter
from .schemas import (
    DataQualityReport,
    QualityIssue,
    arrow_table_to_bars,
    bars_to_arrow_table,
    convert_daily_records_to_bars,
    convert_daily_records_to_settlements,
    convert_minute_records_to_bars,
    validate_ohlc_records,
)
from .sources import (
    AkShareDataSource,
    BaseDataSource,
    SinaFuturesDataSource,
    TushareFuturesDataSource,
    create_data_source,
    normalize_instrument_to_symbol,
)
from .storage import ParquetDataStorage

__all__ = [
    "AkShareDataSource",
    "BaseDataSource",
    "DataQualityReport",
    "FuturesDataDownloader",
    "HistoricalMarketDataAdapter",
    "ParquetDataStorage",
    "QualityIssue",
    "SinaFuturesDataSource",
    "TushareFuturesDataSource",
    "arrow_table_to_bars",
    "bars_to_arrow_table",
    "convert_daily_records_to_bars",
    "convert_daily_records_to_settlements",
    "convert_minute_records_to_bars",
    "create_data_source",
    "normalize_instrument_to_symbol",
    "parse_instrument",
    "validate_ohlc_records",
]
