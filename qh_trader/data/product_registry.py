"""[Data 层] S4 测试品种组合登记 (S4-05, FR-CAL-08, FR-CON-01/02, A25 新增范围).

只登记"已确定接入"的品种属性与来源状态，不把未核验值当成实盘默认值：

- 合约乘数与最小变动价位：用于研究模式的保证金/费用估算，标注来源；
- 夜盘收盘分档：决定 Sessions 模板 (参考 `docs/references/中国期货交易时间段与集合竞价机制详解.md`)；
- 日盘竞价风格：区分上期所/大商所"夜盘品种日盘再竞价"、郑商所"只撤不报窗口"与无夜盘品种"标准日盘竞价"；
- 手续费与保证金率：研究假设，须在 S0/规则登记中核验后替换 (FR-RULE-05)。

`verification_status` 为 `pending_verification` 的条目不得作为实盘或精确核算默认值；
研究模式使用它们时必须在运行清单与报告中显式标注为假设。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from enum import StrEnum

from qh_trader.core.constants import Exchange, MissingRuleError
from qh_trader.core.objects import ProductId, require_text


class DayAuctionStyle(StrEnum):
    """日盘开盘前的竞价/撤单窗风格."""

    RE_AUCTION = "RE_AUCTION"          # 上期所 / 大商所：有夜盘品种日盘 08:55-09:00 再竞价
    CANCEL_ONLY = "CANCEL_ONLY"        # 郑商所：有夜盘品种日盘 08:55-08:59 只撤不报，08:59-09:00 静默
    STANDARD = "STANDARD"              # 无夜盘品种：日盘 08:55-09:00 标准竞价


@dataclass(frozen=True, slots=True)
class ProductSpec:
    """单个品种的研究属性；所有字段都写入运行清单."""

    product: str
    name: str
    exchange: Exchange
    multiplier: Decimal
    price_tick: Decimal
    margin_ratio: Decimal
    commission_per_lot: Decimal
    night_session: bool
    night_close: time | None
    day_auction_style: DayAuctionStyle
    main_months: tuple[int, ...]
    reason: str
    source: str
    verification_status: str

    def __post_init__(self) -> None:
        require_text(self.product, "product")
        require_text(self.name, "name")
        if not isinstance(self.exchange, Exchange):
            raise TypeError("product spec requires an Exchange")
        for field in ("multiplier", "price_tick"):
            value = getattr(self, field)
            if not isinstance(value, Decimal) or value <= 0:
                raise ValueError(f"{field} must be a positive Decimal")
        for field in ("margin_ratio", "commission_per_lot"):
            value = getattr(self, field)
            if not isinstance(value, Decimal) or value < 0:
                raise ValueError(f"{field} must be a non-negative Decimal")
        if self.night_session and self.night_close is None:
            raise ValueError("a night-session product must declare its night close time")
        if not self.night_session and self.night_close is not None:
            raise ValueError("a product without night session cannot declare a night close time")
        if not isinstance(self.day_auction_style, DayAuctionStyle):
            raise TypeError("day_auction_style must be a DayAuctionStyle")
        if not self.main_months or any(not 1 <= month <= 12 for month in self.main_months):
            raise ValueError("main_months must be a non-empty tuple of 1..12")
        require_text(self.source, "source")
        require_text(self.verification_status, "verification_status")

    @property
    def product_id(self) -> ProductId:
        return ProductId(self.exchange, self.product)


# 研究假设：手续费为每手固定值，保证金率为交易所基准的粗估。所有条目在规则登记核验前
# 一律标记 pending_verification，研究模式必须在报告中标注。
_RESEARCH_SOURCE = "research_assumption_pending_rule_verification"

PRODUCT_REGISTRY: dict[str, ProductSpec] = {
    "rb": ProductSpec(
        product="rb",
        name="螺纹钢",
        exchange=Exchange.SHFE,
        multiplier=Decimal("10"),
        price_tick=Decimal("1"),
        margin_ratio=Decimal("0.10"),
        commission_per_lot=Decimal("1.5"),
        night_session=True,
        night_close=time(23, 0),
        day_auction_style=DayAuctionStyle.RE_AUCTION,
        main_months=(1, 5, 10),
        reason="有夜盘、今昨仓区分、首个工程样本",
        source=_RESEARCH_SOURCE,
        verification_status="pending_verification",
    ),
    "fg": ProductSpec(
        product="fg",
        name="玻璃",
        exchange=Exchange.CZCE,
        multiplier=Decimal("20"),
        price_tick=Decimal("1"),
        margin_ratio=Decimal("0.12"),
        commission_per_lot=Decimal("6"),
        night_session=True,
        night_close=time(23, 0),
        day_auction_style=DayAuctionStyle.CANCEL_ONLY,
        main_months=(1, 5, 9),
        reason="郑商所只撤不报窗口",
        source=_RESEARCH_SOURCE,
        verification_status="pending_verification",
    ),
    "MA": ProductSpec(
        product="MA",
        name="甲醇",
        exchange=Exchange.CZCE,
        multiplier=Decimal("10"),
        price_tick=Decimal("1"),
        margin_ratio=Decimal("0.08"),
        commission_per_lot=Decimal("2"),
        night_session=True,
        night_close=time(23, 0),
        day_auction_style=DayAuctionStyle.CANCEL_ONLY,
        main_months=(1, 5, 9),
        reason="跨交易所、短年份代码、只撤不报窗口",
        source=_RESEARCH_SOURCE,
        verification_status="pending_verification",
    ),
    "c": ProductSpec(
        product="c",
        name="玉米",
        exchange=Exchange.DCE,
        multiplier=Decimal("10"),
        price_tick=Decimal("1"),
        margin_ratio=Decimal("0.08"),
        commission_per_lot=Decimal("1.2"),
        night_session=True,
        night_close=time(23, 0),
        day_auction_style=DayAuctionStyle.RE_AUCTION,
        main_months=(1, 5, 9),
        reason="农产品夜盘",
        source=_RESEARCH_SOURCE,
        verification_status="pending_verification",
    ),
    "m": ProductSpec(
        product="m",
        name="豆粕",
        exchange=Exchange.DCE,
        multiplier=Decimal("10"),
        price_tick=Decimal("1"),
        margin_ratio=Decimal("0.08"),
        commission_per_lot=Decimal("1.5"),
        night_session=True,
        night_close=time(23, 0),
        day_auction_style=DayAuctionStyle.RE_AUCTION,
        main_months=(1, 5, 9),
        reason="市价单与跨期指令",
        source=_RESEARCH_SOURCE,
        verification_status="pending_verification",
    ),
    "i": ProductSpec(
        product="i",
        name="铁矿石",
        exchange=Exchange.DCE,
        multiplier=Decimal("100"),
        price_tick=Decimal("0.5"),
        margin_ratio=Decimal("0.10"),
        commission_per_lot=Decimal("6"),
        night_session=True,
        night_close=time(23, 0),
        day_auction_style=DayAuctionStyle.RE_AUCTION,
        main_months=(1, 5, 9),
        reason="市价单与跨期指令、非整数价位跳",
        source=_RESEARCH_SOURCE,
        verification_status="pending_verification",
    ),
    "AP": ProductSpec(
        product="AP",
        name="苹果",
        exchange=Exchange.CZCE,
        multiplier=Decimal("10"),
        price_tick=Decimal("1"),
        margin_ratio=Decimal("0.10"),
        commission_per_lot=Decimal("5"),
        night_session=False,
        night_close=None,
        day_auction_style=DayAuctionStyle.STANDARD,
        main_months=(1, 5, 10),
        reason="无夜盘",
        source=_RESEARCH_SOURCE,
        verification_status="pending_verification",
    ),
    "jd": ProductSpec(
        product="jd",
        name="鸡蛋",
        exchange=Exchange.DCE,
        multiplier=Decimal("10"),
        price_tick=Decimal("1"),
        margin_ratio=Decimal("0.09"),
        commission_per_lot=Decimal("3"),
        night_session=False,
        night_close=None,
        day_auction_style=DayAuctionStyle.STANDARD,
        main_months=(1, 5, 9),
        reason="无夜盘",
        source=_RESEARCH_SOURCE,
        verification_status="pending_verification",
    ),
    "au": ProductSpec(
        product="au",
        name="黄金",
        exchange=Exchange.SHFE,
        multiplier=Decimal("1000"),
        price_tick=Decimal("0.02"),
        margin_ratio=Decimal("0.09"),
        commission_per_lot=Decimal("10"),
        night_session=True,
        night_close=time(2, 30),
        day_auction_style=DayAuctionStyle.RE_AUCTION,
        main_months=(6, 12),
        reason="凌晨 02:30 收盘",
        source=_RESEARCH_SOURCE,
        verification_status="pending_verification",
    ),
    "cu": ProductSpec(
        product="cu",
        name="铜",
        exchange=Exchange.SHFE,
        multiplier=Decimal("5"),
        price_tick=Decimal("10"),
        margin_ratio=Decimal("0.10"),
        commission_per_lot=Decimal("15"),
        night_session=True,
        night_close=time(1, 0),
        day_auction_style=DayAuctionStyle.RE_AUCTION,
        main_months=tuple(range(1, 13)),
        reason="次日 01:00 收盘、大价位跳",
        source=_RESEARCH_SOURCE,
        verification_status="pending_verification",
    ),
}

# 第二章第 4 项的测试品种组合：螺纹钢 / 甲醇 / 铁矿或豆粕 / 苹果或鸡蛋 / 黄金或铜。
S4_TEST_PRODUCT_COMBOS: tuple[tuple[str, ...], ...] = (
    ("rb",),
    ("MA",),
    ("i", "m"),
    ("AP", "jd"),
    ("au", "cu"),
)


def normalize_product(product: str) -> str:
    """把用户输入/合约代码映射到登记键 (郑商所大小写不敏感)."""
    for key in PRODUCT_REGISTRY:
        if key.casefold() == str(product).casefold():
            return key
    raise MissingRuleError(f"product is not registered for S4 test scope: {product}")


def get_product_spec(product: str) -> ProductSpec:
    return PRODUCT_REGISTRY[normalize_product(product)]


def registered_products() -> tuple[ProductSpec, ...]:
    return tuple(PRODUCT_REGISTRY[key] for key in sorted(PRODUCT_REGISTRY))
