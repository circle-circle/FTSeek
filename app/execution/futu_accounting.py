"""Futu account currency normalization shared by execution and dashboard paths."""
from __future__ import annotations

import math
from typing import Any, Mapping


def _safe_float(value: Any, default: float | None = 0.0) -> float | None:
    if value is None:
        return default
    if isinstance(value, str):
        value = value.strip()
        if value in ("N/A", "--", "-", "") or value.lower() in ("nan", "none"):
            return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _rounded(value: float) -> float:
    return round(value, 2)


def _risk_scalar(value: Any) -> Any:
    """Preserve broker risk fields without pretending N/A is numeric zero."""
    if value is None:
        return "N/A"
    if type(value).__name__ in ("bool", "bool_"):
        return bool(value)
    if isinstance(value, str):
        text = value.strip()
        return text or "N/A"
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return str(value)
    if not math.isfinite(number):
        return "N/A"
    return int(number) if number.is_integer() else number


def _risk_fields(raw: Mapping[str, Any]) -> dict:
    """Broker-native account risk fields.

    ``exposure_limit/used_limit/remaining_limit`` and
    ``beginning_dtbp/remaining_dtbp/dt_call_amount`` are broker amount fields;
    they are intentionally **not** FX-converted here because Futu already returns
    them in the account's risk currency and the REAL account may report ``N/A``.
    ``pdt_seq`` is a count; the remaining fields are broker enums/booleans.
    The Dashboard labels them as broker-native to avoid a false USD claim.
    """
    fields = (
        "risk_status", "risk_level", "exposure_level", "exposure_limit",
        "used_limit", "remaining_limit", "is_pdt", "pdt_seq", "beginning_dtbp",
        "dt_status", "remaining_dtbp", "dt_call_amount",
    )
    return {field: _risk_scalar(raw.get(field, "N/A")) for field in fields}


def _direct_account(raw: Mapping[str, Any], currency: str) -> dict:
    account = {
        "total_assets": _safe_float(raw.get("total_assets")),
        "cash": _safe_float(raw.get("cash")),
        "market_val": _safe_float(raw.get("market_val")),
        "long_mv": _safe_float(raw.get("long_mv")),
        "short_mv": abs(_safe_float(raw.get("short_mv"))),
        "avl_withdrawal_cash": _safe_float(raw.get("avl_withdrawal_cash")),
        "power": _safe_float(raw.get("power")),
        "buying_power": _safe_float(raw.get("power")),
        "frozen_cash": _safe_float(raw.get("frozen_cash")),
        "debt": _safe_float(raw.get("debt")),
        "interest_debt": _safe_float(raw.get("interest_debt")),
        "initial_margin": _safe_float(raw.get("initial_margin")),
        "maintain_margin": _safe_float(raw.get("maintain_margin")),
        "maintenance_margin": _safe_float(raw.get("maintenance_margin")),
        "short_sell_power": _safe_float(raw.get("short_sell_power")),
        "net_assets_power": _safe_float(raw.get("net_assets_power")),
        "max_power_short": _safe_float(raw.get("max_power_short")),
        "margin_call_margin": _safe_float(raw.get("margin_call_margin")),
        "interest_charged_amount": _safe_float(raw.get("interest_charged_amount")),
        "currency": "USD",
        "account_scope": "global",
        "raw_currency": currency,
        "fx": 1.0,
    }
    account.update(_risk_fields(raw))
    return account


def normalize_futu_account(
    raw: Mapping[str, Any], fallback_rate: float = 0.0
) -> dict:
    """Normalize a Futu accinfo row to one USD-denominated canonical dictionary.

    The account's ``total_assets`` is denominated in its native currency.  Futu's
    ``usd_assets`` is the gross USD sub-account side, so it is not net equity.
    The native-currency sub-account leg can be negative and must be removed before
    deriving the native-to-USD rate.
    """
    if raw is None:
        return {}

    currency = str(raw.get("currency", "USD") or "USD").upper()
    if currency in ("", "USD", "N/A"):
        return _direct_account(raw, currency or "USD")

    total_assets = _safe_float(raw.get("total_assets"))
    same_currency_field = f"{currency.lower()}_assets"
    same_currency_assets = raw.get(same_currency_field)
    same_currency_value = _safe_float(same_currency_assets, None)
    usd_assets = _safe_float(raw.get("usd_assets"))

    fx_rate = 0.0
    fx_source = "fallback"
    if same_currency_assets is not None and same_currency_value is not None:
        gross_native_assets = total_assets - same_currency_value
        if usd_assets > 0 and gross_native_assets > 0:
            fx_rate = usd_assets / gross_native_assets
            fx_source = "derived"

    if fx_rate <= 0:
        fx_rate = fallback_rate
    if fx_rate <= 0 and currency == "HKD":
        fx_rate = 1.0 / 7.8
    if fx_rate <= 0:
        raise ValueError(f"cannot derive FX for Futu account currency {currency}")

    cash_native = _safe_float(raw.get("cash"))
    cash_usd = cash_native * fx_rate
    usd_cash = _safe_float(raw.get("us_cash"))
    if usd_cash > 0 and abs(usd_cash - cash_usd) <= max(1.0, abs(cash_usd) * 0.01):
        cash = usd_cash
        cash_source = "us_cash(与折算值一致)"
    else:
        cash = cash_usd
        cash_source = f"cash×{fx_rate:.4f}(us_cash 不一致, 弃用)"

    usd_available = _safe_float(raw.get("us_avl_withdrawal_cash"))
    avl_withdrawal_cash = (
        usd_available if usd_available > 0
        else _safe_float(raw.get("avl_withdrawal_cash")) * fx_rate
    )
    usd_power = _safe_float(raw.get("usd_net_cash_power"))
    power_native = _safe_float(raw.get("power"))
    buying_power = usd_power if usd_power > 0 else power_native * fx_rate

    maintain_margin_native = _safe_float(
        raw.get("maintain_margin", raw.get("maintenance_margin"))
    )
    long_mv = _safe_float(raw.get("long_mv")) * fx_rate
    short_mv = abs(_safe_float(raw.get("short_mv"))) * fx_rate

    account = {
        "total_assets": _rounded(total_assets * fx_rate),
        "cash": _rounded(cash),
        "market_val": _rounded(_safe_float(raw.get("market_val")) * fx_rate),
        "long_mv": _rounded(long_mv),
        "short_mv": _rounded(short_mv),
        "avl_withdrawal_cash": _rounded(avl_withdrawal_cash),
        "power": _rounded(buying_power),
        "buying_power": _rounded(buying_power),
        "frozen_cash": _rounded(_safe_float(raw.get("frozen_cash")) * fx_rate),
        "debt": _rounded(_safe_float(raw.get("debt")) * fx_rate),
        "interest_debt": _rounded(_safe_float(raw.get("interest_debt")) * fx_rate),
        "initial_margin": _rounded(_safe_float(raw.get("initial_margin")) * fx_rate),
        "maintain_margin": _rounded(maintain_margin_native * fx_rate),
        "maintenance_margin": _rounded(maintain_margin_native * fx_rate),
        "short_sell_power": _rounded(
            _safe_float(raw.get("short_sell_power")) * fx_rate
        ),
        "net_assets_power": _rounded(
            _safe_float(raw.get("net_assets_power")) * fx_rate
        ),
        "max_power_short": _rounded(
            _safe_float(raw.get("max_power_short")) * fx_rate
        ),
        "margin_call_margin": _rounded(
            _safe_float(raw.get("margin_call_margin")) * fx_rate
        ),
        "interest_charged_amount": _rounded(
            _safe_float(raw.get("interest_charged_amount")) * fx_rate
        ),
        "currency": "USD",
        "account_scope": "global",
        "raw_currency": currency,
        "fx": fx_rate,
        "_fx_rate": fx_rate,
        "_original_currency": currency,
        "_fx_source": fx_source,
        "_usd_side_assets": usd_assets,
        "_usd_cash_field": usd_cash,
        "_cash_source": cash_source,
        "_buying_power_source": (
            "usd_net_cash_power" if usd_power > 0 else f"power×{fx_rate:.4f}"
        ),
    }
    account.update(_risk_fields(raw))
    return account
