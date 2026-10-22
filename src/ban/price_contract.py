"""Bisq 2 monetary contract with exact decimal semantics and fail-closed validation."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

BTC_PRECISION = 8
FIAT_PRECISION = 4
MAX_LONG = 2**63 - 1


class ContractViolation(ValueError):
    pass


def positive_long(value: Any, name: str) -> int:
    if type(value) is not int or not 0 < value <= MAX_LONG:
        raise ContractViolation("invalid positive long: " + name)
    return value


def monetary_value(data: Any, code: str, kind: str, precision: int) -> int:
    if not isinstance(data, dict):
        raise ContractViolation("missing structured monetary side")
    if (data.get("type") != kind or data.get("code") != code
            or data.get("precision") != precision):
        raise ContractViolation("currency, precision or type mismatch")
    return positive_long(data.get("value"), "monetary.value")


def decode_price_quote(data: Any, currency: str) -> Decimal:
    """Fiat/BTC quote uses quote-side precision (4), BTC base-side precision (8)."""
    if not isinstance(data, dict):
        raise ContractViolation("missing PriceQuoteDto")
    market = data.get("market")
    if not isinstance(market, dict) or market.get("baseCurrencyCode") != "BTC" or market.get("quoteCurrencyCode") != currency:
        raise ContractViolation("price market mismatch")
    value = positive_long(data.get("value"), "priceQuote.value")
    if type(data.get("precision")) is not int or data["precision"] != FIAT_PRECISION:
        raise ContractViolation("fiat price precision must equal 4")
    base = monetary_value(data.get("baseSideMonetary"), "BTC", "Coin", BTC_PRECISION)
    quote = monetary_value(data.get("quoteSideMonetary"), currency, "Fiat", FIAT_PRECISION)
    expected = Decimal(quote) * Decimal(10**BTC_PRECISION) / Decimal(base)
    if abs(expected - Decimal(value)) > Decimal("0.5"):
        raise ContractViolation("price value disagrees with monetary side values")
    return Decimal(value).scaleb(-FIAT_PRECISION)


@dataclass(frozen=True)
class AmountBounds:
    denomination: str
    lower: Decimal
    upper: Decimal
    is_fixed: bool
    source_type: str

    def as_dict(self) -> dict:
        return {"denomination": self.denomination, "minimum": str(self.lower),
                "maximum": str(self.upper), "is_fixed": self.is_fixed,
                "source_type": self.source_type}

    def contains(self, fiat_amount: Decimal, fiat_per_btc: Decimal | None) -> str:
        if self.denomination == "BTC":
            if fiat_per_btc is None:
                return "unknown_dynamic_price"
            required = fiat_amount / fiat_per_btc
        else:
            required = fiat_amount
        if not (self.lower <= required <= self.upper):
            return "outside_advertised_bounds"
        return "fixed_amount_price_check_required" if self.is_fixed else "within_advertised_bounds"


def decode_amount_spec(data: Any, currency: str) -> AmountBounds:
    if not isinstance(data, dict):
        raise ContractViolation("missing amountSpec DTO")
    types = {
        "QuoteSideFixedAmountSpec": (currency, FIAT_PRECISION, True),
        "QuoteSideRangeAmountSpec": (currency, FIAT_PRECISION, False),
        "BaseSideFixedAmountSpec": ("BTC", BTC_PRECISION, True),
        "BaseSideRangeAmountSpec": ("BTC", BTC_PRECISION, False),
    }
    kind = data.get("type")
    if kind not in types:
        raise ContractViolation("unsupported amountSpec type")
    denomination, precision, is_fixed = types[kind]
    if is_fixed:
        low = high = positive_long(data.get("amount"), "amount")
    else:
        low = positive_long(data.get("minAmount"), "minAmount")
        high = positive_long(data.get("maxAmount"), "maxAmount")
    if low > high:
        raise ContractViolation("inverted amount range")
    return AmountBounds(denomination, Decimal(low).scaleb(-precision),
                        Decimal(high).scaleb(-precision), is_fixed, kind)


def float_markup(data: Any) -> Decimal | None:
    if data.get("type") != "FloatPriceSpec":
        return None
    number = data.get("percentage")
    if type(number) not in (float, int):
        raise ContractViolation("percentage must be numeric")
    result = Decimal(str(number))
    if not result.is_finite() or result <= -1 or result > 10:
        raise ContractViolation("invalid fractional percentage")
    # PriceUtil multiplies market price by (1 + percentage); 0.025 = 2.5%.
    return result
