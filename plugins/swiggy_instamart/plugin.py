"""Conversation-independent voice mode for building an Instamart order."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.integrations.swiggy import (
    FileTokenStore,
    Server,
    SwiggyAuthError,
    SwiggyClient,
    SwiggyError,
    search_products,
)
from backend.integrations.swiggy.payment_job import enqueue_instamart_payment_monitor
from backend.llm_client import async_chat_with_tools
from backend.plugins import (
    BasePlugin,
    InteractionContext,
    InteractionModeDefinition,
    InteractionResult,
)
from backend.services.interaction_modes.registry import normalize_interaction_text

from .cart import (
    cart_fingerprint,
    cart_items,
    cart_total,
    cart_update_payload,
    compact_addresses,
    item_name,
    qr_available,
    summarize_cart,
)

logger = logging.getLogger(__name__)

_EXIT_PHRASES = {
    "cancel order",
    "cancel order mode",
    "exit order",
    "exit order mode",
    "stop ordering",
}
_KEEP_PHRASES = {"keep", "keep it", "keep cart", "keep the cart", "use it"}
_CLEAR_PHRASES = {"clear", "clear it", "clear cart", "clear the cart", "start over"}
_SHOW_CART_PHRASES = {"cart", "show cart", "what is in my cart", "review cart"}
_CONFIRM_ADDRESS_PHRASES = {
    "हाँ",
    "हां",
    "हाँ यही",
    "haan",
    "han",
    "haan yahi",
    "yes",
    "yes please",
    "yeah",
    "yep",
    "correct",
    "that is right",
    "use it",
    "use that",
    "use this address",
    "confirm address",
}
_CHANGE_ADDRESS_PHRASES = {
    "नहीं",
    "नही",
    "nahi",
    "nahin",
    "mat karo",
    "no",
    "nope",
    "change address",
    "another address",
    "different address",
}

_ORDINALS = {
    "one": 1,
    "first": 1,
    "two": 2,
    "second": 2,
    "three": 3,
    "third": 3,
}

_SHOPPING_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "collect_items",
            "description": (
                "Collect every grocery requested in this turn. The application "
                "searches them and asks the user to choose exact variants."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "quantity": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 20,
                                },
                                "notes": {"type": "string"},
                            },
                            "required": ["query", "quantity", "notes"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["items"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "choose_candidate",
            "description": "Choose one numbered product variant from the last spoken search results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer", "minimum": 1},
                    "quantity": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["number", "quantity"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_cart_item_quantity",
            "description": "Set a numbered current cart item's quantity. Use zero to remove it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer", "minimum": 1},
                    "quantity": {"type": "integer", "minimum": 0, "maximum": 20},
                },
                "required": ["number", "quantity"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_cart",
            "description": "Read the current cart back to the user.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "review_order",
            "description": (
                "Read a fresh final cart review when the user indicates they are "
                "done shopping. This never checks out or places the order."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
]


class SwiggyInstamartPlugin(BasePlugin):
    """Own the exclusive ``swiggy_order`` interaction state machine."""

    SUPPORTED_ACCESS_LEVELS: List[str] = ["interaction"]
    INTERACTION_MODES = (
        InteractionModeDefinition(
            mode_id="swiggy_order",
            # Pulse Hindi preserves English code-switching inconsistently: the
            # same spoken command has arrived through streaming as
            # "order स्विंगी" and through wake capture as "ऑर्डर्स वेगी".
            activation_phrases=("order swiggy", "order स्विंगी", "ऑर्डर्स वेगी"),
            idle_timeout_seconds=10 * 60,
            max_duration_seconds=30 * 60,
        ),
    )

    name = "Swiggy Instamart Order"
    description = "Build and explicitly confirm an Instamart order by voice"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.linked_user_id = str(config.get("linked_user_id") or "").strip()
        self.token_directory = str(config.get("token_directory") or "").strip()
        self.llm_operation = str(config.get("llm_operation") or "plugin_assistant")
        self.llm_timeout_seconds = float(config.get("llm_timeout_seconds", 3.0))
        if self.llm_timeout_seconds <= 0:
            raise ValueError("llm_timeout_seconds must be positive")
        self.mcp_request_timeout_seconds = float(
            config.get("mcp_request_timeout_seconds", 15.0)
        )
        self.mcp_max_attempts = int(config.get("mcp_max_attempts", 2))
        if self.mcp_request_timeout_seconds <= 0:
            raise ValueError("mcp_request_timeout_seconds must be positive")
        if self.mcp_max_attempts <= 0:
            raise ValueError("mcp_max_attempts must be positive")
        self.search_samples = int(config.get("search_samples", 3))
        self.review_valid_seconds = int(config.get("review_valid_seconds", 120))
        self.checkout_limit_rupees = float(config.get("checkout_limit_rupees", 1000))
        self.hermes_plugin_id = str(config.get("hermes_plugin_id") or "hermes")
        self.preferred_address_label = str(
            config.get("preferred_address_label") or ""
        ).strip()
        self._token_store: Optional[FileTokenStore] = None
        self.client: Optional[SwiggyClient] = None
        self._mutation_lock = asyncio.Lock()

    async def initialize(self):
        if not self.enabled:
            return
        if not self.linked_user_id or "${" in self.linked_user_id:
            raise ValueError(
                "SWIGGY_LINKED_USER_ID must name the linked Chronicle user"
            )
        if not self.token_directory or "${" in self.token_directory:
            raise ValueError("SWIGGY_TOKEN_DIRECTORY must point at private OAuth files")
        self._token_store = FileTokenStore(Path(self.token_directory))
        if not self._token_store.configured:
            raise ValueError(
                "Swiggy token_directory must contain tokens.json and client.json"
            )
        self.client = SwiggyClient(
            self._token_store,
            max_attempts=self.mcp_max_attempts,
            request_timeout_seconds=self.mcp_request_timeout_seconds,
        )

    async def cleanup(self):
        self.client = None

    async def health_check(self) -> Dict[str, Any]:
        configured = bool(self._token_store and self._token_store.configured)
        return {
            "ok": configured,
            "message": (
                "Swiggy OAuth files configured" if configured else "Missing OAuth files"
            ),
        }

    async def on_interaction_start(
        self, context: InteractionContext
    ) -> Optional[InteractionResult]:
        if context.session.user_id != self.linked_user_id:
            return InteractionResult(
                reply="Swiggy order mode is not linked for this Chronicle user.",
                end=True,
                end_reason="unauthorized_user",
            )
        if self.client is None:
            return InteractionResult(
                reply="Swiggy order mode is not configured yet.",
                end=True,
                end_reason="not_configured",
            )

        try:
            result = await self.client.call(Server.INSTAMART, "get_addresses")
        except (SwiggyAuthError, SwiggyError) as exc:
            return self._swiggy_failure(exc, end=True)
        addresses = compact_addresses(result.data)
        if not addresses:
            return InteractionResult(
                reply="Your Swiggy account has no saved delivery address. Add one in Swiggy, then try again.",
                end=True,
                end_reason="no_saved_address",
            )

        state = {
            "addresses": addresses,
            "selected_address": None,
            "pending_request": (context.input.text if context.input else ""),
            "candidates": [],
        }
        suggested = addresses[0]
        if self.preferred_address_label:
            preferred = normalize_interaction_text(self.preferred_address_label)
            matches = [
                address
                for address in addresses
                if normalize_interaction_text(str(address.get("label") or ""))
                == preferred
            ]
            if len(matches) != 1:
                state["pending_address"] = None
                return InteractionResult(
                    reply=(
                        f"I couldn't find your preferred saved address "
                        f"{self.preferred_address_label}. Which saved address should I use? "
                        "Say its label."
                    ),
                    phase="select_address",
                    plugin_state=state,
                )
            suggested = matches[0]
        state["pending_address"] = suggested
        return InteractionResult(
            reply=f"Use {suggested['label']} for delivery?",
            phase="confirm_address",
            plugin_state=state,
        )

    async def on_interaction_turn(
        self, context: InteractionContext
    ) -> Optional[InteractionResult]:
        text = (context.input.text if context.input else "").strip()
        normalized = normalize_interaction_text(text)
        state = dict(context.session.plugin_state)
        phase = context.session.phase

        if phase == "checkout_in_progress":
            return InteractionResult(
                reply="The checkout worker stopped before recording Swiggy's result. I will not submit it again. Check the Swiggy app before retrying.",
                plugin_state=state,
                end=True,
                end_reason="checkout_outcome_unknown",
            )
        if phase != "awaiting_payment" and normalized in _EXIT_PHRASES:
            return InteractionResult(
                reply="Order mode closed. I left the current Instamart cart as it is.",
                plugin_state=state,
                end=True,
                end_reason="user_cancelled",
            )
        if phase == "confirm_address":
            return await self._confirm_address(context, state, normalized)
        if phase == "select_address":
            return await self._select_address(context, state, normalized)
        if phase == "existing_cart_decision":
            return await self._existing_cart_decision(context, state, normalized)
        if phase == "cart_update_in_progress":
            return await self._resume_cart_update(state)
        if phase == "shopping":
            if normalized == "complete order":
                return await self._review(state)
            if normalized == "confirm order":
                return InteractionResult(
                    reply="First say complete order so I can read back a fresh cart review.",
                    phase="shopping",
                    plugin_state=state,
                )
            return await self._shopping_turn(context, state, text)
        if phase == "awaiting_confirmation":
            if normalized == "complete order":
                return await self._review(state)
            if normalized != "confirm order":
                return InteractionResult(
                    reply="Say confirm order as a separate command to place this reviewed cart, or cancel order to leave it unchanged.",
                    phase=phase,
                    plugin_state=state,
                )
            return await self._checkout(context, state)
        if phase == "awaiting_payment":
            return await self._payment_turn(context, state, normalized)
        return InteractionResult(
            reply="I lost the order step, so I closed the mode without changing your cart.",
            plugin_state=state,
            end=True,
            end_reason="invalid_phase",
        )

    async def on_interaction_end(
        self, context: InteractionContext
    ) -> Optional[InteractionResult]:
        if context.end_reason in {"idle_timeout", "max_duration"}:
            return InteractionResult(
                reply="Swiggy order mode timed out. Your cart is still saved in Instamart."
            )
        return None

    async def _select_address(
        self, context: InteractionContext, state: dict, normalized: str
    ) -> InteractionResult:
        addresses = state.get("addresses") or []
        selected = self._resolve_number_or_label(normalized, addresses)
        if selected is None:
            return InteractionResult(
                reply="I didn't get that address. Say its number or label, such as one or home.",
                phase="select_address",
                plugin_state=state,
            )
        return self._propose_address(state, selected)

    async def _confirm_address(
        self, context: InteractionContext, state: dict, normalized: str
    ) -> InteractionResult:
        addresses = state.get("addresses") or []
        if not addresses:
            return InteractionResult(
                reply="I lost the suggested delivery address, so I closed order mode.",
                plugin_state=state,
                end=True,
                end_reason="invalid_address_state",
            )

        pending = state.get("pending_address")
        if not isinstance(pending, dict) or not pending.get("id"):
            return InteractionResult(
                reply="I lost the address awaiting confirmation, so I closed order mode.",
                plugin_state=state,
                end=True,
                end_reason="invalid_address_state",
            )
        if normalized in _CHANGE_ADDRESS_PHRASES:
            state["pending_address"] = None
            return InteractionResult(
                reply="Okay. Which saved address should I use? Say its label.",
                phase="select_address",
                plugin_state=state,
            )
        if normalized in _CONFIRM_ADDRESS_PHRASES:
            return await self._accept_address(context, state, pending)

        selected = self._resolve_number_or_label(normalized, addresses)
        if selected is not None:
            return self._propose_address(state, selected)
        return InteractionResult(
            reply=f"Say yes to use {pending['label']}, or say change address.",
            phase="confirm_address",
            plugin_state=state,
        )

    @staticmethod
    def _propose_address(state: dict, selected: dict) -> InteractionResult:
        state["pending_address"] = selected
        return InteractionResult(
            reply=f"Use {selected['label']} for delivery?",
            phase="confirm_address",
            plugin_state=state,
        )

    async def _accept_address(
        self, context: InteractionContext, state: dict, selected: dict
    ) -> InteractionResult:
        state["selected_address"] = selected
        state.pop("pending_address")
        try:
            cart = await self._get_cart()
        except (SwiggyAuthError, SwiggyError) as exc:
            return self._swiggy_failure(exc, state=state, phase=context.session.phase)
        if cart_items(cart):
            return InteractionResult(
                reply=f"Using {selected['label']}. {summarize_cart(cart)} Say keep cart or clear cart.",
                phase="existing_cart_decision",
                plugin_state=state,
            )
        return await self._enter_shopping(context, state)

    async def _existing_cart_decision(
        self, context: InteractionContext, state: dict, normalized: str
    ) -> InteractionResult:
        if normalized in _KEEP_PHRASES:
            return await self._enter_shopping(context, state)
        if normalized in _CLEAR_PHRASES:
            try:
                async with self._mutation_lock:
                    await self.client.call(Server.INSTAMART, "clear_cart")
            except (SwiggyAuthError, SwiggyError) as exc:
                return self._swiggy_failure(exc, state=state)
            return await self._enter_shopping(context, state, prefix="Cart cleared. ")
        return InteractionResult(
            reply="Your existing cart is still unchanged. Say keep cart or clear cart.",
            phase="existing_cart_decision",
            plugin_state=state,
        )

    async def _enter_shopping(
        self, context: InteractionContext, state: dict, *, prefix: str = ""
    ) -> InteractionResult:
        pending = str(state.get("pending_request") or "").strip()
        state["pending_request"] = ""
        if pending:
            if normalize_interaction_text(pending) == "complete order":
                result = await self._review(state)
                result.reply = f"{prefix}{result.reply or ''}".strip()
                return result
            result = await self._shopping_turn(context, state, pending)
            result.reply = f"{prefix}{result.reply or ''}".strip()
            return result
        return InteractionResult(
            reply=f"{prefix}What would you like from Instamart?".strip(),
            phase="shopping",
            plugin_state=state,
        )

    async def _shopping_turn(
        self, context: InteractionContext, state: dict, text: str
    ) -> InteractionResult:
        normalized = normalize_interaction_text(text)
        if normalized in _SHOW_CART_PHRASES:
            return await self._show_cart(state)

        choice = self._candidate_choice(normalized)
        if choice is not None and state.get("candidates"):
            number, quantity = choice
            return await self._choose_candidate(
                context,
                state,
                number,
                quantity or int(state.get("candidate_quantity") or 1),
            )

        add_match = re.match(
            r"^(?:add|find|search(?: for)?|look for)\s+(.+)$", normalized
        )
        if add_match:
            return await self._search(state, add_match.group(1))

        remove_match = re.match(r"^remove\s+(?:item\s+)?(\d+)$", normalized)
        if remove_match:
            return await self._set_cart_quantity(
                context, state, int(remove_match.group(1)), 0
            )

        return await self._llm_shopping_turn(context, state, text)

    async def _search(self, state: dict, query: str) -> InteractionResult:
        try:
            candidates = await self._find_candidates(state, query)
        except (SwiggyAuthError, SwiggyError) as exc:
            return self._swiggy_failure(exc, state=state)
        state["candidates"] = candidates
        state["candidate_query"] = query
        state["candidate_quantity"] = 1
        state["candidate_notes"] = ""
        state["pending_collections"] = []
        if not candidates:
            return InteractionResult(
                reply=f"I couldn't find an in-stock match for {query}.",
                phase="shopping",
                plugin_state=state,
            )
        options = "; ".join(
            f"{index}, {value['brand']} {value['name']}, {value['variant']}, {value['price']:g} rupees"
            for index, value in enumerate(candidates[:3], start=1)
        )
        return InteractionResult(
            reply=f"I found: {options}. Which number and how many?",
            phase="shopping",
            plugin_state=state,
        )

    async def _find_candidates(self, state: dict, query: str) -> list[dict]:
        address = state.get("selected_address") or {}
        result = await search_products(
            self.client,
            str(address.get("id") or ""),
            query,
            samples=self.search_samples,
        )
        candidates = []
        for product in result.organic or result.products:
            for variant in product.variants:
                if not variant.in_stock or not variant.spin_id:
                    continue
                candidates.append(
                    {
                        "name": product.name,
                        "brand": product.brand,
                        "spin_id": variant.spin_id,
                        "sku_id": variant.sku_id,
                        "variant": variant.quantity or variant.label,
                        "price": variant.price,
                    }
                )
                if len(candidates) >= 6:
                    break
            if len(candidates) >= 6:
                break
        return candidates

    async def _collect_items(self, state: dict, raw_items: Any) -> InteractionResult:
        if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 5:
            return InteractionResult(
                reply="Please request one to five grocery items at a time.",
                phase="shopping",
                plugin_state=state,
            )
        items = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                return InteractionResult(
                    reply="I couldn't safely understand that grocery list.",
                    phase="shopping",
                    plugin_state=state,
                )
            query = str(raw_item.get("query") or "").strip()
            notes = str(raw_item.get("notes") or "").strip()
            try:
                quantity = int(raw_item.get("quantity", 1))
            except (TypeError, ValueError):
                quantity = 0
            if not query or not 1 <= quantity <= 20:
                return InteractionResult(
                    reply="Each grocery needs a name and a quantity from one to twenty.",
                    phase="shopping",
                    plugin_state=state,
                )
            items.append({"query": query, "quantity": quantity, "notes": notes})

        semaphore = asyncio.Semaphore(3)

        async def search_item(item: dict) -> dict:
            async with semaphore:
                candidates = await self._find_candidates(state, item["query"])
            return {**item, "candidates": candidates}

        try:
            collections = await asyncio.gather(*(search_item(item) for item in items))
        except (SwiggyAuthError, SwiggyError) as exc:
            return self._swiggy_failure(exc, state=state)

        missing = [item["query"] for item in collections if not item["candidates"]]
        available = [item for item in collections if item["candidates"]]
        if not available:
            return InteractionResult(
                reply="I couldn't find an in-stock match for any of those items.",
                phase="shopping",
                plugin_state=state,
            )
        state["pending_collections"] = available
        prompt = self._advance_collection(state)
        if missing:
            prompt = f"I couldn't find {', '.join(missing)}. {prompt}"
        return InteractionResult(
            reply=prompt,
            phase="shopping",
            plugin_state=state,
        )

    @staticmethod
    def _advance_collection(state: dict) -> str:
        pending = list(state.get("pending_collections") or [])
        current = pending.pop(0)
        state["pending_collections"] = pending
        state["candidates"] = current["candidates"]
        state["candidate_query"] = current["query"]
        state["candidate_quantity"] = current["quantity"]
        state["candidate_notes"] = current["notes"]
        options = "; ".join(
            f"{index}, {value['brand']} {value['name']}, {value['variant']}, {value['price']:g} rupees"
            for index, value in enumerate(current["candidates"][:3], start=1)
        )
        note = f" ({current['notes']})" if current["notes"] else ""
        return (
            f"For {current['query']}{note}, I found: {options}. "
            f"Which option for quantity {current['quantity']}?"
        )

    async def _choose_candidate(
        self,
        context: InteractionContext,
        state: dict,
        number: int,
        quantity: int,
    ) -> InteractionResult:
        candidates = state.get("candidates") or []
        if number < 1 or number > min(3, len(candidates)):
            return InteractionResult(
                reply="Choose one of the first three product numbers I read out.",
                phase="shopping",
                plugin_state=state,
            )
        candidate = candidates[number - 1]
        try:
            cart = await self._get_cart()
            payload = cart_update_payload(cart)
            existing = next(
                (
                    value
                    for value in payload
                    if value.get("spinId") == candidate["spin_id"]
                ),
                None,
            )
            if existing:
                existing["quantity"] = int(existing.get("quantity", 0)) + quantity
            else:
                new_item = {"spinId": candidate["spin_id"], "quantity": quantity}
                if candidate.get("sku_id"):
                    new_item["skuId"] = candidate["sku_id"]
                payload.append(new_item)
        except (SwiggyAuthError, SwiggyError) as exc:
            return self._swiggy_failure(exc, state=state)
        state["candidates"] = []
        return await self._stage_cart_update(
            context,
            state,
            payload,
            reply_prefix=f"Added {quantity} {candidate['name']}. ",
        )

    async def _set_cart_quantity(
        self,
        context: InteractionContext,
        state: dict,
        number: int,
        quantity: int,
    ) -> InteractionResult:
        try:
            cart = await self._get_cart()
        except (SwiggyAuthError, SwiggyError) as exc:
            return self._swiggy_failure(exc, state=state)
        items = cart_items(cart)
        payload = cart_update_payload(cart)
        if number < 1 or number > len(payload) or number > len(items):
            return InteractionResult(
                reply="That cart item number doesn't exist. Say show cart to hear the current items.",
                phase="shopping",
                plugin_state=state,
            )
        changed_name = item_name(items[number - 1])
        if quantity == 0:
            payload.pop(number - 1)
        else:
            payload[number - 1]["quantity"] = quantity
        action = "Removed" if quantity == 0 else f"Set {quantity} of"
        return await self._stage_cart_update(
            context,
            state,
            payload,
            reply_prefix=f"{action} {changed_name}. ",
        )

    async def _stage_cart_update(
        self,
        context: InteractionContext,
        state: dict,
        payload: list[dict],
        *,
        reply_prefix: str,
    ) -> InteractionResult:
        """Checkpoint an exact full-cart payload before the external write."""
        if context.checkpoint is None:
            return InteractionResult(
                reply="I could not establish a durable cart checkpoint, so I did not change the cart.",
                plugin_state=state,
                end=True,
                end_reason="checkpoint_unavailable",
            )
        state = {
            **state,
            "pending_cart_update": {
                "selected_address_id": state["selected_address"]["id"],
                "items": payload,
                "reply_prefix": reply_prefix,
            },
        }
        context.session.phase = "cart_update_in_progress"
        context.session.plugin_state = state
        await context.checkpoint()
        return await self._resume_cart_update(state)

    async def _resume_cart_update(self, state: dict) -> InteractionResult:
        """Apply a checkpointed full-cart replacement; safe to repeat after a crash."""
        pending = state.get("pending_cart_update") or {}
        try:
            async with self._mutation_lock:
                await self.client.call(
                    Server.INSTAMART,
                    "update_cart",
                    selectedAddressId=pending["selected_address_id"],
                    items=pending["items"],
                )
            fresh = await self._get_cart()
        except (KeyError, SwiggyAuthError, SwiggyError) as exc:
            return self._swiggy_failure(
                exc, state=state, phase="cart_update_in_progress"
            )
        state = dict(state)
        state.pop("pending_cart_update", None)
        reply = f"{pending.get('reply_prefix', '')}{summarize_cart(fresh)}"
        if state.get("pending_collections"):
            reply = f"{reply} {self._advance_collection(state)}"
        return InteractionResult(
            reply=reply,
            phase="shopping",
            plugin_state=state,
        )

    async def _show_cart(self, state: dict) -> InteractionResult:
        try:
            cart = await self._get_cart()
        except (SwiggyAuthError, SwiggyError) as exc:
            return self._swiggy_failure(exc, state=state)
        return InteractionResult(
            reply=summarize_cart(cart), phase="shopping", plugin_state=state
        )

    async def _llm_shopping_turn(
        self, context: InteractionContext, state: dict, text: str
    ) -> InteractionResult:
        candidates = [
            f"{index}: {value['brand']} {value['name']} {value['variant']}"
            for index, value in enumerate((state.get("candidates") or [])[:3], start=1)
        ]
        system = (
            "You classify one committed spoken Instamart shopping turn into one safe "
            "structured intent. Put every requested grocery in collect_items, up to "
            "five items, preserving quantity and notes. "
            "Never invent IDs. Product IDs are resolved by the application. "
            "Checkout and order confirmation are unavailable. If the request is unclear, "
            "respond briefly without a tool. Current spoken candidates: "
            + ("; ".join(candidates) if candidates else "none")
        )
        try:
            response = await async_chat_with_tools(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
                tools=_SHOPPING_TOOLS,
                operation=self.llm_operation,
                timeout_seconds=self.llm_timeout_seconds,
            )
            message = response.choices[0].message
        except Exception as exc:  # noqa: BLE001 - shopping stays usable on LLM outage
            logger.warning("Instamart turn classification failed: %s", exc)
            return InteractionResult(
                reply="Try saying add followed by a product name, show cart, or complete order.",
                phase="shopping",
                plugin_state=state,
            )
        if not message.tool_calls:
            reply = (message.content or "").strip()
            return InteractionResult(
                reply=reply or "What product should I search for?",
                phase="shopping",
                plugin_state=state,
            )
        if len(message.tool_calls) != 1:
            return InteractionResult(
                reply="I couldn't safely understand that grocery list. Please try one to five items again.",
                phase="shopping",
                plugin_state=state,
            )
        call = message.tool_calls[0]
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except (TypeError, ValueError):
            arguments = {}
        name = call.function.name
        if name == "collect_items":
            return await self._collect_items(state, arguments.get("items"))
        if name == "choose_candidate":
            return await self._choose_candidate(
                context,
                state,
                int(arguments.get("number", 0)),
                int(arguments.get("quantity", 1)),
            )
        if name == "set_cart_item_quantity":
            return await self._set_cart_quantity(
                context,
                state,
                int(arguments.get("number", 0)),
                int(arguments.get("quantity", 0)),
            )
        if name == "show_cart":
            return await self._show_cart(state)
        if name == "review_order":
            return await self._review(state)
        return InteractionResult(
            reply="I couldn't safely map that request. Try saying add followed by a product name.",
            phase="shopping",
            plugin_state=state,
        )

    async def _review(self, state: dict) -> InteractionResult:
        try:
            cart = await self._get_cart()
            payment = await self.client.call(Server.INSTAMART, "get_payment_options")
        except (SwiggyAuthError, SwiggyError) as exc:
            return self._swiggy_failure(exc, state=state)
        if not cart_items(cart):
            return InteractionResult(
                reply="Your cart is empty. Add something before completing the order.",
                phase="shopping",
                plugin_state=state,
            )
        total = cart_total(cart)
        if total is not None and total >= self.checkout_limit_rupees:
            return InteractionResult(
                reply=f"The cart is {total:g} rupees, above the {self.checkout_limit_rupees:g} rupee MCP checkout limit. Finish it in the Swiggy app.",
                phase="shopping",
                plugin_state=state,
            )
        if not qr_available(payment.data):
            return InteractionResult(
                reply="UPI scan-and-pay is not available for this cart. I will not silently switch to Cash; use Swiggy or change the cart.",
                phase="shopping",
                plugin_state=state,
            )
        address = state.get("selected_address") or {}
        state = {
            **state,
            "review_fingerprint": cart_fingerprint(cart, str(address.get("id") or "")),
            "reviewed_at": time.time(),
            "payment_choice": {"paymentMethod": "UPI", "generateUPIQR": True},
            "candidates": [],
        }
        summary = summarize_cart(cart)
        return InteractionResult(
            reply=f"Final review. {summary} Delivery is to {address.get('label', 'your selected address')}. Payment is UPI scan or tap. Say confirm order as a separate command to place it.",
            phase="awaiting_confirmation",
            plugin_state=state,
            event_data={"cart_total": total, "address": address.get("label")},
        )

    async def _checkout(
        self, context: InteractionContext, state: dict
    ) -> InteractionResult:
        reviewed_at = float(state.get("reviewed_at") or 0)
        if time.time() - reviewed_at > self.review_valid_seconds:
            refreshed = await self._review(state)
            refreshed.reply = (
                f"That review expired, so I refreshed it. {refreshed.reply}"
            )
            return refreshed
        try:
            cart = await self._get_cart()
        except (SwiggyAuthError, SwiggyError) as exc:
            return self._swiggy_failure(exc, state=state)
        address = state.get("selected_address") or {}
        fingerprint = cart_fingerprint(cart, str(address.get("id") or ""))
        if fingerprint != state.get("review_fingerprint"):
            refreshed = await self._review(state)
            refreshed.reply = f"The cart changed after your review. {refreshed.reply}"
            return refreshed

        if context.checkpoint is None:
            return InteractionResult(
                reply="I could not establish a durable checkout checkpoint, so I did not place the order.",
                plugin_state=state,
                end=True,
                end_reason="checkpoint_unavailable",
            )
        state = {
            **state,
            "checkout_attempt": {
                "input_id": context.input.input_id if context.input else "system",
                "started_at": time.time(),
            },
        }
        context.session.phase = "checkout_in_progress"
        context.session.plugin_state = state
        await context.checkpoint()

        try:
            async with self._mutation_lock:
                checkout = await self.client.call(
                    Server.INSTAMART,
                    "checkout",
                    addressId=address["id"],
                    paymentMethod="UPI",
                    generateUPIQR=True,
                )
        except (SwiggyAuthError, SwiggyError) as exc:
            logger.error("Instamart checkout outcome is unknown: %s", exc)
            return InteractionResult(
                reply="Swiggy did not return a definite checkout result. I will not submit it again. Check the Swiggy app before retrying.",
                plugin_state=state,
                end=True,
                end_reason="checkout_outcome_unknown",
            )
        data = checkout.data if isinstance(checkout.data, dict) else {}
        status = str(data.get("status") or "").upper()
        if status != "PENDING_PAYMENT":
            if status not in {"SUCCESS", "CONFIRMED", "ORDER_PLACED", "PLACED"}:
                return InteractionResult(
                    reply="Swiggy returned an unfamiliar checkout result. I will not submit it again. Check the Swiggy app before retrying.",
                    phase="finished",
                    plugin_state={
                        **state,
                        "order_id": data.get("orderId"),
                        "checkout_status": status or "missing",
                    },
                    end=True,
                    end_reason="checkout_outcome_unknown",
                )
            return InteractionResult(
                reply="Instamart order placed successfully.",
                phase="finished",
                plugin_state={**state, "order_id": data.get("orderId")},
                end=True,
                end_reason="order_placed",
                event_data={"order_id": data.get("orderId")},
            )

        order_id = str(data.get("orderId") or "")
        paas_id = str(data.get("paasId") or "")
        bridge_url = str(data.get("bridgeUrl") or "")
        if not order_id or not paas_id:
            return InteractionResult(
                reply="Swiggy created a pending payment but omitted its tracking identifiers. Check the Swiggy app before trying again.",
                phase="finished",
                plugin_state={**state, "payment_status": "unknown"},
                end=True,
                end_reason="checkout_tracking_missing",
            )

        notification_sent = False
        if bridge_url and context.services is not None:
            result = await context.services.call_plugin(
                self.hermes_plugin_id,
                "notify",
                {
                    "text": "Your Swiggy Instamart UPI payment link:\n" + bridge_url,
                    "sensitive": True,
                },
                user_id=context.session.user_id,
            )
            notification_sent = bool(result and result.success)

        polling_interval = int(data.get("pollingIntervalInMs") or 5000)
        max_polling = int(data.get("maxTimeToPollForInMs") or 300000)
        state = {
            **state,
            "order_id": order_id,
            "paas_id": paas_id,
            "bridge_url": bridge_url,
            "payment_status": "pending",
        }
        context.session.phase = "awaiting_payment"
        context.session.plugin_state = state
        if context.dialogue_thread_id:
            await context.checkpoint()
        try:
            payment_job_id = enqueue_instamart_payment_monitor(
                interaction_id=context.session.interaction_id,
                user_id=context.session.user_id,
                client_id=context.session.client_id,
                audio_session_id=context.session.audio_session_id,
                token_directory=self.token_directory,
                order_id=order_id,
                paas_id=paas_id,
                polling_interval_ms=polling_interval,
                max_polling_ms=max_polling,
                dialogue_thread_id=context.dialogue_thread_id,
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 - order already exists; never replay checkout
            logger.error("Could not enqueue Instamart payment monitor: %s", exc)
            payment_job_id = ""

        state = {
            **state,
            "order_id": order_id,
            "paas_id": paas_id,
            "bridge_url": bridge_url,
            "payment_status": "pending",
            "payment_job_id": payment_job_id,
            "polling_interval_ms": polling_interval,
            "max_polling_ms": max_polling,
        }
        if not payment_job_id:
            reply = "The order is waiting for UPI payment, but I could not start automatic payment monitoring. Use the link in Chronicle or Swiggy, then check the Swiggy app before retrying."
        elif bridge_url and notification_sent:
            reply = "The order is waiting for UPI payment. I sent the secure scan-or-tap link to Discord and will monitor it."
        elif bridge_url:
            reply = "The order is waiting for UPI payment. The link is in Chronicle, but Discord delivery failed; say resend payment link after Hermes is available."
        else:
            reply = "The order is waiting for UPI payment, but Swiggy did not return a payment link. Check the Swiggy app; I will still monitor the attempt."
        return InteractionResult(
            reply=reply,
            phase="awaiting_payment",
            plugin_state=state,
            event_data={"payment_url": bridge_url, "order_id": order_id},
        )

    async def _payment_turn(
        self, context: InteractionContext, state: dict, normalized: str
    ) -> InteractionResult:
        if normalized in {"resend payment link", "send payment link", "resend link"}:
            bridge_url = str(state.get("bridge_url") or "")
            if not bridge_url:
                return InteractionResult(
                    reply="Swiggy did not provide a payment link for this attempt.",
                    phase="awaiting_payment",
                    plugin_state=state,
                )
            if context.services is None:
                return InteractionResult(
                    reply="The payment link is still available in Chronicle, but Hermes is unavailable.",
                    phase="awaiting_payment",
                    plugin_state=state,
                    event_data={"payment_url": bridge_url},
                )
            result = await context.services.call_plugin(
                self.hermes_plugin_id,
                "notify",
                {
                    "text": "Your Swiggy Instamart UPI payment link:\n" + bridge_url,
                    "sensitive": True,
                },
                user_id=context.session.user_id,
            )
            sent = bool(result and result.success)
            return InteractionResult(
                reply=(
                    "I resent the payment link to Discord."
                    if sent
                    else "Hermes could not deliver the payment link to Discord."
                ),
                phase="awaiting_payment",
                plugin_state=state,
                event_data={"payment_url": bridge_url},
            )
        if normalized in {"cancel order", "cancel the order"}:
            return InteractionResult(
                reply="I cannot cancel a created Instamart order here. Call Swiggy customer care at 080-67466729.",
                phase="awaiting_payment",
                plugin_state=state,
            )
        return InteractionResult(
            reply="The UPI payment is still being monitored. Say resend payment link if you need it again.",
            phase="awaiting_payment",
            plugin_state=state,
        )

    async def _get_cart(self) -> dict | list:
        result = await self.client.call(Server.INSTAMART, "get_cart")
        return result.data if isinstance(result.data, (dict, list)) else {}

    @staticmethod
    def _resolve_number_or_label(text: str, values: list[dict]) -> Optional[dict]:
        number = SwiggyInstamartPlugin._explicit_selection_number(text)
        if number is not None and 1 <= number <= len(values):
            return values[number - 1]
        exact_matches = [
            value
            for value in values
            if text == normalize_interaction_text(str(value.get("label") or ""))
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]
        if exact_matches:
            return None
        matched = [
            value
            for value in values
            if text
            and text in normalize_interaction_text(str(value.get("label") or ""))
        ]
        return matched[0] if len(matched) == 1 else None

    @staticmethod
    def _explicit_selection_number(text: str) -> Optional[int]:
        token = text.strip()
        if token.isdigit():
            return int(token)
        if token in _ORDINALS:
            return _ORDINALS[token]
        match = re.fullmatch(
            r"(?:option|number)\s+(one|first|two|second|three|third|[1-9])"
            r"(?:\s+(?:quantity|qty|times|of)\s+\d+)?",
            token,
        )
        if not match:
            return None
        selected = match.group(1)
        return int(selected) if selected.isdigit() else _ORDINALS[selected]

    @staticmethod
    def _candidate_choice(text: str) -> Optional[tuple[int, Optional[int]]]:
        number = SwiggyInstamartPlugin._explicit_selection_number(text)
        if number is None:
            return None
        quantity = None
        quantity_match = re.search(r"(?:quantity|qty|times|of)\s+(\d+)", text)
        if quantity_match:
            quantity = max(1, min(20, int(quantity_match.group(1))))
        return number, quantity

    @staticmethod
    def _swiggy_failure(
        exc: Exception,
        *,
        state: Optional[dict] = None,
        end: bool = False,
        phase: str = "shopping",
    ) -> InteractionResult:
        if isinstance(exc, SwiggyAuthError):
            reply = "Your Swiggy authorization expired. Refresh the linked token files, then try again."
            reason = "authorization_required"
        else:
            reply = f"Swiggy could not complete that step: {exc}"
            reason = "swiggy_error"
        return InteractionResult(
            reply=reply,
            phase=phase if not end else None,
            plugin_state=state,
            end=end,
            end_reason=reason if end else None,
        )
