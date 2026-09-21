"""Per-merchant zero-dual-authority migration from legacy REAL to exact minor strings."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from shopping_cli.core.errors import ConflictError, NotFoundError, ValidationError
from shopping_cli.db.session import now_iso

CURRENCY_TABLE_VERSION = "kiwi-workbench-currency-v1-2026-09-21"
SUPPORTED_EXPONENTS: dict[str, int] = {"CNY": 2}
MAX_KNP_MINOR = 9_007_199_254_740_991
MODES = {"LEGACY_REAL", "FROZEN", "EXACT_MINOR"}


@dataclass(frozen=True)
class ExactProductMoney:
    currency: str
    price_minor: str
    floor_price_minor: str
    authority_version: int


def money_mode(conn: sqlite3.Connection, merchant_id: str) -> tuple[str, int]:
    row = conn.execute(
        "select mode, authority_version from merchant_money_authority where merchant_id = ?",
        (merchant_id,),
    ).fetchone()
    if row is None:
        return ("LEGACY_REAL", 0)
    mode = str(row["mode"])
    if mode not in MODES:
        raise RuntimeError(f"unknown money authority mode: {mode}")
    return (mode, int(row["authority_version"]))


def assert_legacy_money_write_allowed(conn: sqlite3.Connection, merchant_id: str) -> None:
    mode, _ = money_mode(conn, merchant_id)
    if mode == "FROZEN":
        raise ConflictError("money migration is frozen; legacy price writes are disabled")
    if mode == "EXACT_MINOR":
        raise ConflictError("exact minor money is authoritative; legacy float writes are disabled")


def assert_legacy_delivery_projection_unchanged(
    conn: sqlite3.Connection,
    merchant_id: str,
    currency: str,
    fee: float,
) -> None:
    """Allow non-money delivery edits only when the legacy fee projection is unchanged."""
    mode, _ = money_mode(conn, merchant_id)
    if mode == "LEGACY_REAL":
        return
    row = conn.execute(
        """
        select currency, fee, fee_minor_text, money_currency_table_version
        from delivery_rules where merchant_id=?
        """,
        (merchant_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown delivery rule for merchant: {merchant_id}")
    if str(row["currency"]) != currency:
        raise ConflictError("delivery currency is frozen by the money authority")
    if mode == "FROZEN":
        if Decimal(str(row["fee"])) != Decimal(str(fee)):
            raise ConflictError("money migration is frozen; legacy delivery fee writes are disabled")
        return
    if row["money_currency_table_version"] != CURRENCY_TABLE_VERSION:
        raise ConflictError("exact delivery money contract version is not current")
    exact = _minor_text(row["fee_minor_text"], "fee_minor")
    scale = Decimal(10) ** SUPPORTED_EXPONENTS[_supported_currency(currency)]
    if Decimal(str(fee)) != Decimal(exact) / scale:
        raise ConflictError("exact minor delivery fee is authoritative; legacy float write differs")


def begin_money_migration(conn: sqlite3.Connection, merchant_id: str) -> int:
    _require_merchant(conn, merchant_id)
    mode, version = money_mode(conn, merchant_id)
    if mode != "LEGACY_REAL":
        raise ConflictError(f"money migration cannot start from {mode}")
    stamp = now_iso()
    conn.execute(
        """
        insert into merchant_money_authority(
            merchant_id, mode, authority_version, started_at, activated_at, updated_at
        ) values (?, 'FROZEN', ?, ?, null, ?)
        on conflict(merchant_id) do update set
            mode='FROZEN', authority_version=excluded.authority_version,
            started_at=excluded.started_at, activated_at=null, updated_at=excluded.updated_at
        """,
        (merchant_id, version + 1, stamp, stamp),
    )
    # 旧 staging 不得跨迁移代次复用。
    conn.execute(
        """
        update products set price_minor_text=null, floor_price_minor_text=null,
            money_currency_table_version=''
        where merchant_id = ?
        """,
        (merchant_id,),
    )
    conn.execute(
        """
        update delivery_rules set fee_minor_text=null, money_currency_table_version=''
        where merchant_id = ?
        """,
        (merchant_id,),
    )
    return version + 1


def stage_product_money(
    conn: sqlite3.Connection,
    *,
    merchant_id: str,
    sku: str,
    currency: str,
    price_minor: str,
    floor_price_minor: str,
) -> ExactProductMoney:
    mode, version = money_mode(conn, merchant_id)
    if mode != "FROZEN":
        raise ConflictError("exact money can only be staged while the merchant is FROZEN")
    row = conn.execute(
        "select merchant_id, currency from products where sku = ?",
        (sku,),
    ).fetchone()
    if row is None or str(row["merchant_id"]) != merchant_id:
        raise NotFoundError(f"Unknown product SKU for merchant: {sku}")
    normalized_currency = _supported_currency(currency)
    if str(row["currency"]) != normalized_currency:
        raise ConflictError("staged currency differs from the current authoritative product currency")
    price = _minor_text(price_minor, "price_minor")
    floor = _minor_text(floor_price_minor, "floor_price_minor")
    if int(floor) > int(price):
        raise ValidationError("floor_price_minor must not exceed price_minor")
    conn.execute(
        """
        update products set price_minor_text=?, floor_price_minor_text=?,
            money_currency_table_version=? where sku=? and merchant_id=?
        """,
        (price, floor, CURRENCY_TABLE_VERSION, sku, merchant_id),
    )
    return ExactProductMoney(normalized_currency, price, floor, version)


def money_migration_report(conn: sqlite3.Connection, merchant_id: str) -> dict[str, Any]:
    mode, version = money_mode(conn, merchant_id)
    rows = conn.execute(
        """
        select sku, currency, price_minor_text, floor_price_minor_text,
               money_currency_table_version from products where merchant_id=? order by sku
        """,
        (merchant_id,),
    ).fetchall()
    incomplete = [
        str(row["sku"])
        for row in rows
        if row["price_minor_text"] is None
        or row["floor_price_minor_text"] is None
        or row["money_currency_table_version"] != CURRENCY_TABLE_VERSION
    ]
    delivery = conn.execute(
        """
        select fee_minor_text, money_currency_table_version
        from delivery_rules where merchant_id=?
        """,
        (merchant_id,),
    ).fetchone()
    delivery_staged = (
        delivery is not None
        and delivery["fee_minor_text"] is not None
        and delivery["money_currency_table_version"] == CURRENCY_TABLE_VERSION
    )
    return {
        "merchant_id": merchant_id,
        "mode": mode,
        "authority_version": version,
        "product_count": len(rows),
        "staged_count": len(rows) - len(incomplete),
        "incomplete_skus": incomplete,
        "delivery_fee_staged": delivery_staged,
    }


def activate_exact_money(conn: sqlite3.Connection, merchant_id: str) -> int:
    mode, version = money_mode(conn, merchant_id)
    if mode != "FROZEN":
        raise ConflictError("exact money activation requires FROZEN mode")
    report = money_migration_report(conn, merchant_id)
    if report["product_count"] == 0:
        raise ConflictError("cannot activate exact money without products")
    if report["incomplete_skus"]:
        raise ConflictError(
            "exact money staging is incomplete: " + ", ".join(report["incomplete_skus"])
        )
    if not report["delivery_fee_staged"]:
        raise ConflictError("delivery fee exact money staging is incomplete")
    stamp = now_iso()
    changed = conn.execute(
        """
        update merchant_money_authority set mode='EXACT_MINOR', activated_at=?, updated_at=?
        where merchant_id=? and mode='FROZEN' and authority_version=?
        """,
        (stamp, stamp, merchant_id, version),
    )
    if changed.rowcount != 1:
        raise ConflictError("money authority changed while activating")
    return version


def abort_money_migration(conn: sqlite3.Connection, merchant_id: str) -> None:
    mode, version = money_mode(conn, merchant_id)
    if mode != "FROZEN":
        raise ConflictError("only a FROZEN migration can be aborted")
    stamp = now_iso()
    conn.execute(
        """
        update merchant_money_authority set mode='LEGACY_REAL', activated_at=null, updated_at=?
        where merchant_id=? and mode='FROZEN' and authority_version=?
        """,
        (stamp, merchant_id, version),
    )
    conn.execute(
        """
        update products set price_minor_text=null, floor_price_minor_text=null,
            money_currency_table_version='' where merchant_id=?
        """,
        (merchant_id,),
    )
    conn.execute(
        """
        update delivery_rules set fee_minor_text=null, money_currency_table_version=''
        where merchant_id=?
        """,
        (merchant_id,),
    )


def stage_delivery_fee(
    conn: sqlite3.Connection,
    *,
    merchant_id: str,
    currency: str,
    fee_minor: str,
) -> str:
    mode, _ = money_mode(conn, merchant_id)
    if mode != "FROZEN":
        raise ConflictError("delivery fee can only be staged while the merchant is FROZEN")
    row = conn.execute(
        "select currency from delivery_rules where merchant_id=?",
        (merchant_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown delivery rule for merchant: {merchant_id}")
    normalized = _supported_currency(currency)
    if str(row["currency"]) != normalized:
        raise ConflictError("staged delivery currency differs from the current delivery rule")
    fee = _minor_text(fee_minor, "fee_minor")
    conn.execute(
        """
        update delivery_rules set fee_minor_text=?, money_currency_table_version=?
        where merchant_id=?
        """,
        (fee, CURRENCY_TABLE_VERSION, merchant_id),
    )
    return fee


def exact_delivery_fee(conn: sqlite3.Connection, merchant_id: str) -> str:
    mode, _ = money_mode(conn, merchant_id)
    if mode != "EXACT_MINOR":
        raise ConflictError("exact money is not authoritative for this merchant")
    row = conn.execute(
        """
        select currency, fee_minor_text, money_currency_table_version
        from delivery_rules where merchant_id=?
        """,
        (merchant_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown delivery rule for merchant: {merchant_id}")
    _supported_currency(str(row["currency"]))
    if row["money_currency_table_version"] != CURRENCY_TABLE_VERSION:
        raise ConflictError("delivery money contract version is not current")
    return _minor_text(row["fee_minor_text"], "fee_minor")


def update_delivery_fee_exact(
    conn: sqlite3.Connection,
    *,
    merchant_id: str,
    expected_authority_version: int,
    fee_minor: str,
) -> str:
    mode, version = money_mode(conn, merchant_id)
    if mode != "EXACT_MINOR" or version != expected_authority_version:
        raise ConflictError("exact money authority version changed")
    current = conn.execute(
        "select currency from delivery_rules where merchant_id=?",
        (merchant_id,),
    ).fetchone()
    if current is None:
        raise NotFoundError(f"Unknown delivery rule for merchant: {merchant_id}")
    currency = _supported_currency(str(current["currency"]))
    fee = _minor_text(fee_minor, "fee_minor")
    legacy_fee = float(Decimal(fee) / (Decimal(10) ** SUPPORTED_EXPONENTS[currency]))
    conn.execute(
        """
        update delivery_rules set fee_minor_text=?, money_currency_table_version=?,
            fee=?, updated_at=? where merchant_id=?
        """,
        (fee, CURRENCY_TABLE_VERSION, legacy_fee, now_iso(), merchant_id),
    )
    return fee


def exact_product_money(conn: sqlite3.Connection, merchant_id: str, sku: str) -> ExactProductMoney:
    mode, version = money_mode(conn, merchant_id)
    if mode != "EXACT_MINOR":
        raise ConflictError("exact money is not authoritative for this merchant")
    row = conn.execute(
        """
        select merchant_id, currency, price_minor_text, floor_price_minor_text,
               money_currency_table_version from products where sku=?
        """,
        (sku,),
    ).fetchone()
    if row is None or str(row["merchant_id"]) != merchant_id:
        raise NotFoundError(f"Unknown product SKU for merchant: {sku}")
    if row["money_currency_table_version"] != CURRENCY_TABLE_VERSION:
        raise ConflictError("product money contract version is not current")
    return ExactProductMoney(
        _supported_currency(str(row["currency"])),
        _minor_text(row["price_minor_text"], "price_minor"),
        _minor_text(row["floor_price_minor_text"], "floor_price_minor"),
        version,
    )


def update_product_money_exact(
    conn: sqlite3.Connection,
    *,
    merchant_id: str,
    sku: str,
    expected_authority_version: int,
    price_minor: str,
    floor_price_minor: str,
) -> ExactProductMoney:
    mode, version = money_mode(conn, merchant_id)
    if mode != "EXACT_MINOR" or version != expected_authority_version:
        raise ConflictError("exact money authority version changed")
    current = exact_product_money(conn, merchant_id, sku)
    price = _minor_text(price_minor, "price_minor")
    floor = _minor_text(floor_price_minor, "floor_price_minor")
    if int(floor) > int(price):
        raise ValidationError("floor_price_minor must not exceed price_minor")
    exponent = SUPPORTED_EXPONENTS[current.currency]
    scale = Decimal(10) ** exponent
    # REAL columns remain compatibility projections derived from the exact authority.
    legacy_price = float(Decimal(price) / scale)
    legacy_floor = float(Decimal(floor) / scale)
    conn.execute(
        """
        update products set price_minor_text=?, floor_price_minor_text=?,
            money_currency_table_version=?, price=?, floor_price=?, updated_at=?
        where sku=? and merchant_id=?
        """,
        (
            price,
            floor,
            CURRENCY_TABLE_VERSION,
            legacy_price,
            legacy_floor,
            now_iso(),
            sku,
            merchant_id,
        ),
    )
    return ExactProductMoney(current.currency, price, floor, version)


def decimal_token_to_minor(currency: str, token: str) -> str:
    normalized = _supported_currency(currency)
    exponent = SUPPORTED_EXPONENTS[normalized]
    try:
        amount = Decimal(token)
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("amount must be an authoritative plain decimal string") from exc
    if amount < 0 or "e" in token.lower():
        raise ValidationError("amount must be a non-negative plain decimal string")
    scaled = amount * (Decimal(10) ** exponent)
    if scaled != scaled.to_integral_value():
        raise ValidationError(f"{currency} amount exceeds exponent {exponent}")
    return _minor_text(str(int(scaled)), "amount_minor")


def validate_minor_text(value: Any, field: str = "amount_minor") -> str:
    return _minor_text(value, field)


def minor_to_legacy_major(currency: str, minor: str) -> float:
    normalized = _supported_currency(currency)
    exact = _minor_text(minor, "amount_minor")
    return float(Decimal(exact) / (Decimal(10) ** SUPPORTED_EXPONENTS[normalized]))


def _minor_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or not value.isascii() or not value.isdigit():
        raise ValidationError(f"{field} must be a non-negative decimal integer string")
    if len(value) > 1 and value.startswith("0"):
        raise ValidationError(f"{field} must not contain leading zeros")
    number = int(value)
    if number > MAX_KNP_MINOR:
        raise ValidationError(f"{field} exceeds the locked KNP safe integer boundary")
    return str(number)


def _supported_currency(value: str) -> str:
    currency = str(value or "")
    if currency not in SUPPORTED_EXPONENTS:
        raise ValidationError(f"currency {currency!r} is not enabled for exact money")
    return currency


def _require_merchant(conn: sqlite3.Connection, merchant_id: str) -> None:
    if conn.execute("select 1 from merchants where id=?", (merchant_id,)).fetchone() is None:
        raise NotFoundError(f"Unknown merchant: {merchant_id}")
