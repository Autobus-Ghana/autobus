"""Synced external catalogue: checkout link handoff instead of Autobus orders."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from core.nlu.service.intentprocessor import IntentProcessor


def test_checkout_url_with_qty_appends_and_replaces():
    assert (
        IntentProcessor._checkout_url_with_qty("https://shop.example/buy?product=1", 2)
        == "https://shop.example/buy?product=1&qty=2"
    )
    assert (
        IntentProcessor._checkout_url_with_qty("https://shop.example/buy?qty=9&product=1", 3)
        == "https://shop.example/buy?product=1&qty=3"
    )
    assert IntentProcessor._checkout_url_with_qty("", 2) == ""


def test_external_handoff_returns_link_without_creating_order():
    processor = IntentProcessor()
    product = SimpleNamespace(
        name="Rice Bag 5kg",
        number_in_stock=40,
        link="https://ama.usegreenmall.com/buy?v=abc",
        external_id="abc",
    )
    recorded = []

    with patch("core.embed.turn_context.record_embed_action", side_effect=lambda a: recorded.append(a)):
        reply = processor._external_catalog_checkout_handoff(
            matched_product=product,
            line_quantity=2,
            unit_price=Decimal("75.00"),
            currency="GHS",
            turn={"conversation_id": "t1"},
            metadata={"conversation_id": "t1", "external_customer_id": "c1", "external_id": "abc"},
            catalog=[],
        )

    assert "https://ama.usegreenmall.com/buy?v=abc&qty=2" in reply
    assert "finish checkout there" in reply.lower()
    assert "can't take payment" in reply.lower()
    assert recorded and recorded[0]["type"] == "checkout.handoff"
    assert recorded[0]["checkout"]["checkout_url"].endswith("qty=2")


def test_external_handoff_refuses_when_qty_exceeds_stock():
    processor = IntentProcessor()
    product = SimpleNamespace(
        name="Rice Bag 5kg",
        number_in_stock=1,
        link="https://ama.usegreenmall.com/buy?v=abc",
        external_id="abc",
    )
    reply = processor._external_catalog_checkout_handoff(
        matched_product=product,
        line_quantity=3,
        unit_price=Decimal("75.00"),
        currency="GHS",
        turn=None,
        metadata={},
        catalog=[],
    )
    assert "only have 1" in reply
    assert "buy?" not in reply
