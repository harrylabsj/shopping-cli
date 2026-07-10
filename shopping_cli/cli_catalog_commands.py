"""Catalog, product, search, delivery, and policy CLI commands."""

from __future__ import annotations

import argparse
from typing import Any

from shopping_cli.cli_common import db_path_from_args, emit
from shopping_cli.core.catalog import (
    create_merchant,
    create_product,
    list_merchants,
    search_merchants,
    search_products,
    set_stock,
    update_merchant,
    update_product,
    upsert_delivery_rule,
)
from shopping_cli.core.policies import create_policy, list_policies, policy_summary, search_policies
from shopping_cli.db.session import db_session


def cmd_merchant_create(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        merchant = create_merchant(
            conn,
            merchant_id=args.id,
            name=args.name,
            city=args.city or "",
            service_area=args.service_area or "",
            contact=args.contact or "",
            hours=args.hours or "",
            automation_boundaries=args.automation_boundaries or "",
            tags=args.tags or "",
            delivery_fee=args.delivery_fee,
            delivery_eta_minutes=args.delivery_eta_minutes,
            delivery_radius_km=args.delivery_radius_km,
        )
    emit({"ok": True, "merchant": merchant, "message": f"Merchant created: {args.id}"}, args.format)


def cmd_merchant_list(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        merchants = list_merchants(conn, limit=args.limit, offset=args.offset)
    if args.format == "text":
        if not merchants:
            print("No merchants found.")
            return
        print(f"{'MERCHANT_ID':<14} {'NAME':<24} {'CITY':<14} SERVICE_AREA")
        for merchant in merchants:
            print(
                f"{merchant['id']:<14} "
                f"{merchant['name']:<24} "
                f"{merchant['city'] or '-':<14} "
                f"{merchant['service_area'] or '-'}"
            )
        return
    emit({"ok": True, "results": merchants}, args.format)


def cmd_merchant_update(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        merchant = update_merchant(
            conn,
            merchant_id=args.id,
            name=args.name,
            city=args.city,
            service_area=args.service_area,
            contact=args.contact,
            hours=args.hours,
            automation_boundaries=args.automation_boundaries,
            tags=args.tags,
            delivery_fee=args.delivery_fee,
            delivery_eta_minutes=args.delivery_eta_minutes,
            delivery_radius_km=args.delivery_radius_km,
        )
    emit({"ok": True, "merchant": merchant, "message": f"Merchant updated: {args.id}"}, args.format)


def cmd_delivery_set(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        delivery = upsert_delivery_rule(
            conn,
            args.merchant,
            service_area=args.service_area or "",
            fee=args.fee,
            eta_minutes=args.eta_minutes,
            radius_km=args.radius_km,
            notes=args.notes or "",
        )
    if args.format == "text":
        emit_delivery_rule_text(args.merchant, delivery)
        return
    emit({"ok": True, "merchant_id": args.merchant, "delivery": delivery}, args.format)


def emit_delivery_rule_text(merchant_id: str, delivery: dict[str, Any]) -> None:
    print(f"Delivery rule updated: {merchant_id}")
    print(f"Service area: {delivery.get('service_area') or '-'}")
    print(f"Fee: {delivery.get('currency') or 'CNY'} {float(delivery.get('fee') or 0):g}")
    print(f"ETA: {int(delivery.get('eta_minutes') or 0)} minutes")
    print(f"Radius: {float(delivery.get('radius_km') or 0):g} km")
    print(f"Notes: {delivery.get('notes') or '-'}")


def cmd_product_add(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        product = create_product(
            conn,
            merchant_id=args.merchant,
            sku=args.sku,
            title=args.title,
            price=args.price,
            stock=args.stock,
            currency=args.currency,
            category=args.category or "",
            tags=args.tags or "",
            description=args.description or "",
            delivery_attributes=args.delivery_attributes or "",
        )
    emit({"ok": True, "product": product, "message": f"Product added: {args.sku}"}, args.format)


def cmd_product_stock(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        product = set_stock(conn, args.sku, args.stock, args.merchant or "")
    emit({"ok": True, "product": product, "message": f"Stock set: {args.sku} -> {args.stock}"}, args.format)


def cmd_product_update(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        product = update_product(
            conn,
            sku=args.sku,
            merchant_id=args.merchant or "",
            title=args.title,
            price=args.price,
            stock=args.stock,
            currency=args.currency,
            category=args.category,
            tags=args.tags,
            description=args.description,
            delivery_attributes=args.delivery_attributes,
        )
    emit({"ok": True, "product": product, "message": f"Product updated: {args.sku}"}, args.format)


def cmd_search_products(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        results = search_products(
            conn,
            query=args.query or "",
            city=args.city or "",
            area=args.area or "",
            max_price=args.max_price,
            include_out_of_stock=args.include_out_of_stock,
            limit=args.limit,
            offset=args.offset,
        )
    if args.format == "text":
        if not results:
            query = args.query or "all products"
            print(f"No products found for {query}.")
            return
        print(f"{'SKU':<14} {'STOCK':<6} {'PRICE':<10} {'MERCHANT':<20} TITLE")
        for product in results:
            price = f"{product['currency']} {product['price']:g}"
            print(
                f"{product['sku']:<14} "
                f"{product['stock']:<6} "
                f"{price:<10} "
                f"{product['merchant']['name']:<20} "
                f"{product['title']}"
            )
        return
    emit({"ok": True, "query": args.query or "", "results": results}, args.format)


def cmd_search_merchants(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        results = search_merchants(
            conn,
            query=args.query or "",
            city=args.city or "",
            limit=args.limit,
            offset=args.offset,
        )
    if args.format == "text":
        if not results:
            query = args.query or "all merchants"
            print(f"No merchants found for {query}.")
            return
        print(f"{'MERCHANT_ID':<16} {'CITY':<14} {'PRODUCTS':<8} {'SERVICE_AREA':<22} NAME")
        for merchant in results:
            print(
                f"{merchant['id']:<16} "
                f"{merchant['city'] or '-':<14} "
                f"{merchant['product_count']:<8} "
                f"{merchant['service_area'] or '-':<22} "
                f"{merchant['name']}"
            )
        return
    emit({"ok": True, "query": args.query or "", "results": results}, args.format)


def emit_policy_table(policies: list[dict[str, Any]], empty_message: str) -> None:
    if not policies:
        print(empty_message)
        return
    print(f"{'CODE':<14} {'RISK':<5} {'CATEGORY':<20} TITLE")
    for policy in policies:
        risk = "HIGH" if policy.get("high_risk") else "-"
        print(
            f"{policy['code']:<14} "
            f"{risk:<5} "
            f"{(policy['category'] or '-'):<20} "
            f"{policy['title'] or ''}"
        )


def cmd_policy_add(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        policy = create_policy(
            conn,
            merchant_id=args.merchant,
            code=args.code,
            body=args.body,
            category=args.category or "",
            title=args.title or "",
            tags=args.tags or "",
            high_risk=args.high_risk,
        )
    emit({"ok": True, "policy": policy, "message": f"Policy added: {args.merchant}/{args.code}"}, args.format)


def cmd_policy_list(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        policies = list_policies(
            conn,
            merchant_id=args.merchant or "",
            category=args.category or "",
            limit=args.limit,
            offset=args.offset,
        )
    if args.format == "text":
        emit_policy_table(policies, "No policies found.")
        return
    emit({"ok": True, "results": policies}, args.format)


def cmd_policy_show(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        policy = policy_summary(conn, args.merchant, args.code)
    emit({"ok": True, "policy": policy}, args.format)


def cmd_search_policies(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        results = search_policies(
            conn,
            query=args.query or "",
            merchant_id=args.merchant or "",
            category=args.category or "",
            limit=args.limit,
            offset=args.offset,
        )
    if args.format == "text":
        emit_policy_table(results, f"No policies found for {args.query or 'all policies'}.")
        return
    emit({"ok": True, "query": args.query or "", "results": results}, args.format)
