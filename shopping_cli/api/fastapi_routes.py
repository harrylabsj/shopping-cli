"""FastAPI route installation for the marketplace API dual stack.

Move-only extraction of the FastAPI branch that ``app.create_app`` used to
perform inline: the module-level availability guard (so the module imports
cleanly without fastapi and ``FastAPI`` is ``None``), the ``Authorization`` /
``Idempotency-Key`` header defaults, the request-body-limit middleware and
error-handler registration, and every ``@app.<method>`` route.  The fallback
stack is untouched; both stacks share the wrappers and route table in
``api.route_table``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shopping_cli.api import auth as api_auth
from shopping_cli.api.error_handlers import register_error_handlers
from shopping_cli.api.request_limits import RequestBodyLimitMiddleware as _RequestBodyLimitMiddleware
from shopping_cli.api.route_table import (
    _abandon_agent_message,
    _abandon_stale_agent_messages,
    _agent_heartbeat,
    _append_conversation_message,
    _audit_events,
    _buyer_ask,
    _capabilities,
    _claim_agent_message,
    _close_conversation,
    _complete_agent_message,
    _conversation_list,
    _create_agent_token,
    _create_conversation,
    _create_human_review,
    _create_merchant,
    _create_product,
    _fail_agent_message,
    _get_agent,
    _get_conversation,
    _get_human_review,
    _get_listing_projection,
    _get_merchant,
    _get_merchant_private_config,
    _get_product,
    _health,
    _human_review_queue,
    _ingest_channel_message,
    _list_agent_tokens,
    _list_agents,
    _list_listing_projections,
    _list_merchants,
    _merchant_conversations,
    _negotiation_abandon_claim,
    _negotiation_abandon_stale_claims,
    _negotiation_claim,
    _negotiation_complete_claim,
    _negotiation_fail_claim,
    _negotiation_heartbeat_claims,
    _negotiation_pending_messages,
    _negotiation_snapshot,
    _negotiation_submit_decision,
    _recover_merchant_token,
    _record_tool_call_audit,
    _resolve_human_review,
    _resolve_human_review_item,
    _revoke_agent_token,
    _revoke_merchant_tokens,
    _rotate_agent_token,
    _rotate_merchant_token,
    _search_merchants,
    _search_products,
    _sync_erp,
    _update_merchant,
    _update_product,
)

try:  # pragma: no cover - exercised when optional dependency is installed
    from fastapi import FastAPI, Header, Request, Response
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException
except ModuleNotFoundError:  # pragma: no cover - local CI currently has no fastapi
    FastAPI = None  # type: ignore[misc,assignment]
    Header = None  # type: ignore[assignment]
    Response = None  # type: ignore[misc,assignment]
    JSONResponse = None  # type: ignore[misc,assignment]
    RequestValidationError = None  # type: ignore[misc,assignment]
    Request = None  # type: ignore[misc,assignment]
    StarletteHTTPException = None  # type: ignore[misc,assignment]


def _auth_header_default() -> Any:
    if Header is None:
        return ""
    return Header(default="")


AUTHORIZATION_HEADER = _auth_header_default()


def _idempotency_key_header_default() -> Any:
    if Header is None:
        return ""
    return Header(default="", alias="Idempotency-Key")


IDEMPOTENCY_KEY_HEADER = _idempotency_key_header_default()


def register_fastapi_routes(app: Any, db_path: str | Path) -> None:
    """Register middleware, error handlers and every marketplace route on *app*.

    Exception mapping mirrors the fallback ``handle_request`` (403/404/409/413/
    429/405/400/500) so both stacks behave identically on the wire.  *app* must
    expose ``add_middleware`` (optional), ``exception_handler`` and the
    ``get``/``post``/``patch`` route decorators.
    """
    if hasattr(app, "add_middleware"):
        app.add_middleware(_RequestBodyLimitMiddleware)

    register_error_handlers(
        app,
        json_response=JSONResponse,
        request_validation_error=RequestValidationError,
        starlette_http_exception=StarletteHTTPException,
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return _health(db_path)

    @app.get("/merchants")
    def list_merchants(limit: str = "", offset: str = "") -> dict[str, Any]:
        return _list_merchants(db_path, {"limit": limit, "offset": offset})

    @app.post("/merchants")
    def create_merchant(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
        idempotency_key: str = IDEMPOTENCY_KEY_HEADER,
    ) -> dict[str, Any]:
        return _create_merchant(
            db_path,
            api_auth.payload_with_auth(payload, authorization, idempotency_key),
        )

    @app.get("/merchants/{merchant_id}")
    def get_merchant(merchant_id: str) -> dict[str, Any]:
        return _get_merchant(db_path, merchant_id)

    @app.get("/merchants/{merchant_id}/private-config")
    def get_merchant_private_config(
        merchant_id: str,
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _get_merchant_private_config(
            db_path,
            merchant_id,
            api_auth.payload_with_auth({}, authorization),
        )

    @app.post("/merchants/{merchant_id}/token/rotate")
    def rotate_merchant_token(
        merchant_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _rotate_merchant_token(
            db_path,
            merchant_id,
            api_auth.payload_with_auth(payload, authorization),
        )

    @app.post("/merchants/{merchant_id}/token/revoke")
    def revoke_merchant_tokens(
        merchant_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _revoke_merchant_tokens(
            db_path,
            merchant_id,
            api_auth.payload_with_auth(payload, authorization),
        )

    @app.post("/merchants/{merchant_id}/token/recover")
    def recover_merchant_token(
        merchant_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _recover_merchant_token(
            db_path,
            merchant_id,
            api_auth.payload_with_auth(payload, authorization),
        )

    @app.patch("/merchants/{merchant_id}")
    def update_merchant(
        merchant_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _update_merchant(db_path, merchant_id, api_auth.payload_with_auth(payload, authorization))

    @app.post("/products")
    def create_product(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _create_product(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.get("/products/{sku}")
    def get_product(
        sku: str,
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        # 审查 P2-1：精确库存仅向商品所属商户本人开放（owner 鉴权在 handler）
        return _get_product(
            db_path, sku, api_auth.payload_with_auth({}, authorization)
        )

    @app.patch("/products/{sku}")
    def update_product(
        sku: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _update_product(db_path, sku, api_auth.payload_with_auth(payload, authorization))

    @app.get("/v1/merchant/listings/projections")
    def merchant_listing_projections(
        merchant_id: str = "",
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        # 审查 P2-B：handoff_destination 仅向所属商户本人开放（owner 判定在 handler）
        return _list_listing_projections(
            db_path, {"merchant_id": merchant_id}, api_auth.payload_with_auth({}, authorization)
        )

    @app.get("/v1/merchant/listings/{sku}/projection")
    def merchant_listing_projection(
        sku: str,
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _get_listing_projection(db_path, sku, api_auth.payload_with_auth({}, authorization))

    @app.post("/v1/merchant/erp/sync")
    def merchant_erp_sync(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _sync_erp(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.get("/search/products")
    def search_products(
        query: str = "",
        city: str = "",
        area: str = "",
        max_price: str = "",
        include_out_of_stock: str = "",
        limit: str = "",
        offset: str = "",
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        # 审查 P2-1：精确库存仅向商品所属商户本人开放（owner 鉴权在 handler）
        return _search_products(
            db_path,
            {
                "query": query,
                "city": city,
                "area": area,
                "max_price": max_price,
                "include_out_of_stock": include_out_of_stock,
                "limit": limit,
                "offset": offset,
            },
            api_auth.payload_with_auth({}, authorization),
        )

    @app.get("/search/merchants")
    def search_merchants(query: str = "", city: str = "", limit: str = "", offset: str = "") -> dict[str, Any]:
        return _search_merchants(db_path, {"query": query, "city": city, "limit": limit, "offset": offset})

    @app.post("/channels/messages")
    def ingest_channel_message(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _ingest_channel_message(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/buyer/ask")
    def buyer_ask(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
        idempotency_key: str = IDEMPOTENCY_KEY_HEADER,
    ) -> dict[str, Any]:
        return _buyer_ask(db_path, api_auth.payload_with_auth(payload, authorization, idempotency_key))

    @app.post("/conversations")
    def create_conversation(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
        idempotency_key: str = IDEMPOTENCY_KEY_HEADER,
    ) -> dict[str, Any]:
        return _create_conversation(db_path, api_auth.payload_with_auth(payload, authorization, idempotency_key))

    @app.get("/buyers/{buyer_id}/conversations")
    def get_buyer_conversations(
        buyer_id: str,
        status: str = "",
        merchant_id: str = "",
        sku: str = "",
        updated_since: str = "",
        authorization: str = AUTHORIZATION_HEADER,
        limit: str = "",
        offset: str = "",
        include: str = "",
    ) -> dict[str, Any]:
        return _conversation_list(
            db_path,
            {
                "buyer_id": buyer_id,
                "status": status,
                "merchant_id": merchant_id,
                "sku": sku,
                "updated_since": updated_since,
                "limit": limit,
                "offset": offset,
                "include": include,
            },
            api_auth.payload_with_auth({}, authorization),
            owner_kind="buyer",
            owner_id=buyer_id,
        )

    @app.get("/conversations/{conversation_id}")
    def get_conversation(conversation_id: str, authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _get_conversation(db_path, conversation_id, api_auth.payload_with_auth({}, authorization))

    @app.post("/conversations/{conversation_id}/messages")
    def add_message(
        conversation_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _append_conversation_message(
            db_path, conversation_id, api_auth.payload_with_auth(payload, authorization)
        )

    @app.post("/conversations/{conversation_id}/close")
    def close_conversation(
        conversation_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _close_conversation(db_path, conversation_id, api_auth.payload_with_auth(payload, authorization))

    @app.post("/agents/heartbeat")
    def agent_heartbeat(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _agent_heartbeat(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/agents/tokens")
    def create_agent_token(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _create_agent_token(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.get("/agents/tokens")
    def list_agent_tokens(
        merchant_id: str = "",
        limit: str = "",
        offset: str = "",
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _list_agent_tokens(
            db_path,
            api_auth.payload_with_auth({}, authorization),
            merchant_id=merchant_id,
            limit=limit,
            offset=offset,
        )

    @app.post("/agents/tokens/revoke")
    def revoke_agent_token(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _revoke_agent_token(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/agents/tokens/rotate")
    def rotate_agent_token(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _rotate_agent_token(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/agents/messages/claim")
    def claim_agent_message_route(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _claim_agent_message(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/agents/messages/complete")
    def complete_agent_message_route(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _complete_agent_message(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/agents/messages/fail")
    def fail_agent_message_route(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _fail_agent_message(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/agents/messages/abandon")
    def abandon_agent_message_route(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _abandon_agent_message(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/agents/messages/abandon-stale")
    def abandon_stale_agent_messages_route(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _abandon_stale_agent_messages(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.get("/agents")
    def list_agents(
        authorization: str = AUTHORIZATION_HEADER,
        limit: str = "",
        offset: str = "",
    ) -> dict[str, Any]:
        return _list_agents(db_path, api_auth.payload_with_auth({}, authorization), limit=limit, offset=offset)

    @app.get("/agents/{agent_id}")
    def get_agent(agent_id: str, authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _get_agent(db_path, agent_id, api_auth.payload_with_auth({}, authorization))

    @app.get("/merchants/{merchant_id}/agents")
    def get_merchant_agents(
        merchant_id: str,
        authorization: str = AUTHORIZATION_HEADER,
        limit: str = "",
        offset: str = "",
    ) -> dict[str, Any]:
        return _list_agents(
            db_path,
            api_auth.payload_with_auth({}, authorization),
            owner_id=merchant_id,
            limit=limit,
            offset=offset,
        )

    @app.post("/audit/tool-calls")
    def record_tool_call_audit(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _record_tool_call_audit(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.get("/audit/events")
    def get_audit_events(
        merchant_id: str = "",
        event: str = "",
        limit: str = "",
        offset: str = "",
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _audit_events(
            db_path,
            api_auth.payload_with_auth({}, authorization),
            merchant_id=merchant_id,
            event=event,
            limit=limit,
            offset=offset,
        )

    @app.get("/human-review/queue")
    def human_review_queue(
        merchant_id: str = "",
        limit: str = "",
        offset: str = "",
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _human_review_queue(
            db_path,
            api_auth.payload_with_auth({}, authorization),
            merchant_id=merchant_id,
            limit=limit,
            offset=offset,
        )

    @app.get("/human-review/{review_id}")
    def get_human_review(review_id: str, authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _get_human_review(db_path, review_id, api_auth.payload_with_auth({}, authorization))

    @app.post("/human-review/{review_id}/resolve")
    def resolve_human_review_item(
        review_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _resolve_human_review_item(db_path, review_id, api_auth.payload_with_auth(payload, authorization))

    @app.get("/merchants/{merchant_id}/conversations")
    def get_merchant_conversations(
        merchant_id: str,
        status: str = "",
        buyer_id: str = "",
        sku: str = "",
        updated_since: str = "",
        authorization: str = AUTHORIZATION_HEADER,
        limit: str = "",
        offset: str = "",
        include: str = "",
    ) -> dict[str, Any]:
        return _conversation_list(
            db_path,
            {
                "merchant_id": merchant_id,
                "status": status,
                "buyer_id": buyer_id,
                "sku": sku,
                "updated_since": updated_since,
                "limit": limit,
                "offset": offset,
                "include": include,
            },
            api_auth.payload_with_auth({}, authorization),
            owner_kind="merchant",
            owner_id=merchant_id,
        )

    @app.get("/merchants/{merchant_id}/human-review")
    def human_review(
        merchant_id: str,
        limit: str = "",
        offset: str = "",
        authorization: str = AUTHORIZATION_HEADER,
        include: str = "",
    ) -> dict[str, Any]:
        return _merchant_conversations(
            db_path,
            merchant_id,
            api_auth.payload_with_auth({}, authorization),
            status="human_required",
            limit=limit,
            offset=offset,
            include=include,
        )

    @app.post("/conversations/{conversation_id}/human-review")
    def create_human_review(
        conversation_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _create_human_review(db_path, conversation_id, api_auth.payload_with_auth(payload, authorization))

    @app.post("/conversations/{conversation_id}/human-review/resolve")
    def resolve_human_review(
        conversation_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _resolve_human_review(db_path, conversation_id, api_auth.payload_with_auth(payload, authorization))

    @app.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        return _capabilities(db_path)

    @app.get("/negotiation/pending-messages")
    def negotiation_pending_messages(authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _negotiation_pending_messages(db_path, api_auth.payload_with_auth({}, authorization))

    @app.post("/negotiation/claims")
    def negotiation_claim(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _negotiation_claim(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/negotiation/claims/complete")
    def negotiation_complete_claim(
        payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER
    ) -> dict[str, Any]:
        return _negotiation_complete_claim(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/negotiation/claims/fail")
    def negotiation_fail_claim(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _negotiation_fail_claim(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/negotiation/claims/abandon")
    def negotiation_abandon_claim(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _negotiation_abandon_claim(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/negotiation/claims/heartbeat")
    def negotiation_heartbeat_claims(
        payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER
    ) -> dict[str, Any]:
        return _negotiation_heartbeat_claims(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/negotiation/claims/abandon-stale")
    def negotiation_abandon_stale_claims(
        payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER
    ) -> dict[str, Any]:
        return _negotiation_abandon_stale_claims(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.get("/negotiation/snapshot")
    def negotiation_snapshot(
        conversation_id: str = "",
        message_id: str = "",
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _negotiation_snapshot(
            db_path,
            api_auth.payload_with_auth({}, authorization),
            {"conversation_id": conversation_id, "message_id": message_id},
        )

    @app.post("/negotiation/decisions")
    def negotiation_submit_decision(
        payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER
    ) -> dict[str, Any]:
        return _negotiation_submit_decision(db_path, api_auth.payload_with_auth(payload, authorization))
