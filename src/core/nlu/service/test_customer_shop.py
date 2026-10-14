import unittest

from core.nlu.service.customer_shop import (
    CatalogItem,
    classify_customer_shop_intent,
    extract_product_query_name,
    extract_quantity,
    is_clear_product_subject,
    is_questionish_product_name,
    looks_like_existing_order_question,
    mentioned_size,
    resolve_catalog_query,
)


def _item(name: str, product_id: str = "p1", price: str = "10") -> CatalogItem:
    return CatalogItem(product_id=product_id, name=name, price=price, stock=12)


class CustomerShopOrderParsingTests(unittest.TestCase):
    def test_extracts_mango_juice_and_quantity_from_instagram_phrasing(self):
        text = "I want mango juice 20pcs"
        self.assertEqual(extract_product_query_name(text), "mango juice")
        self.assertEqual(extract_quantity(text), 20)

    def test_matches_listed_mango_juice_and_places_order_slots(self):
        catalog = [_item("Mango Juice", "mj")]
        intent, slots, _ = classify_customer_shop_intent(
            "I want mango juice 20pcs", catalog=catalog
        )
        self.assertEqual(intent, "create_order")
        self.assertEqual(slots.get("item_name"), "Mango Juice")
        self.assertEqual(slots.get("quantity"), "20")
        self.assertEqual(slots.get("product_id"), "mj")
        self.assertNotIn("I want", slots.get("item_name", ""))

    def test_matches_product_with_extra_catalog_words(self):
        catalog = [_item("Fresh Mango Juice 500ml", "fmj")]
        intent, slots, _ = classify_customer_shop_intent(
            "I want mango juice 20pcs", catalog=catalog
        )
        self.assertEqual(intent, "create_order")
        self.assertEqual(slots.get("item_name"), "Fresh Mango Juice 500ml")
        self.assertEqual(slots.get("quantity"), "20")

    def test_token_match_when_catalog_words_are_not_contiguous(self):
        catalog = [_item("Mango Fruit Juice", "mfj"), _item("Apple Juice", "aj")]
        matches = resolve_catalog_query("mango juice", catalog)
        self.assertEqual([item.name for item in matches], ["Mango Fruit Juice"])

    def test_does_not_quote_full_sentence_when_product_is_unknown(self):
        intent, slots, _ = classify_customer_shop_intent(
            "I want mango juice 20pcs", catalog=[]
        )
        self.assertEqual(intent, "create_order")
        self.assertEqual(slots.get("item_name"), "mango juice")
        self.assertEqual(slots.get("quantity"), "20")

    def test_existing_i_want_to_order_phrasing_still_works(self):
        catalog = [_item("Mango Juice", "mj")]
        intent, slots, _ = classify_customer_shop_intent(
            "I want to order mango juice", catalog=catalog
        )
        self.assertEqual(intent, "create_order")
        self.assertEqual(slots.get("item_name"), "Mango Juice")

    def test_catalog_browse_is_unchanged(self):
        intent, slots, _ = classify_customer_shop_intent(
            "what products do you have", catalog=[_item("Mango Juice")]
        )
        self.assertEqual(intent, "view_products")
        self.assertEqual(slots, {})

    def test_product_query_still_views_named_item(self):
        catalog = [_item("Mango Juice", "mj")]
        intent, slots, _ = classify_customer_shop_intent(
            "do you have mango juice", catalog=catalog
        )
        self.assertEqual(intent, "view_product")
        self.assertEqual(slots.get("product_name"), "Mango Juice")


class ExistingOrderQuestionTests(unittest.TestCase):
    def test_delivery_and_past_order_are_not_new_purchases(self):
        self.assertTrue(looks_like_existing_order_question("When do I receive my order"))
        self.assertTrue(looks_like_existing_order_question("Did you create my order from yesterday?"))
        self.assertTrue(looks_like_existing_order_question("I placed an order for 2 shoes yesterday"))
        self.assertTrue(looks_like_existing_order_question("I asked for 3"))

    def test_a_new_purchase_is_not_an_existing_order_question(self):
        self.assertFalse(looks_like_existing_order_question("I want mango juice 20pcs"))
        self.assertFalse(looks_like_existing_order_question("What products are available"))

    def test_size_question_is_not_a_product_name(self):
        self.assertTrue(is_questionish_product_name("Pls is this available in size 39?"))
        self.assertEqual(mentioned_size("Pls is this available in size 39?"), "39")
        self.assertFalse(is_questionish_product_name("Naa"))

    def test_catalog_copy_invites_a_purchase(self):
        from core.nlu.service.customer_shop import format_customer_catalog

        text = format_customer_catalog([_item("Naa")])
        self.assertIn("Here's what we've got", text)
        self.assertIn("how many", text.lower())


class LocationAndDeliveryAreNotProductsTests(unittest.TestCase):
    def test_whereabouts_is_not_a_product_subject(self):
        self.assertFalse(is_clear_product_subject("Im at adenta"))
        self.assertTrue(is_clear_product_subject("mango juice"))
        self.assertFalse(is_clear_product_subject("Can you deliver to my location"))

    def test_delivery_question_is_not_a_product_search(self):
        catalog = [_item("Mango Juice", "mj"), _item("Adenta Bread", "ab")]
        intent, slots, _ = classify_customer_shop_intent(
            "Can you deliver to my location",
            catalog=catalog,
            current_intent="create_order",
        )
        self.assertEqual(intent, "business_conversation")
        self.assertNotIn("item_name", slots)
        self.assertNotIn("product_name", slots)

    def test_location_follow_up_does_not_become_the_item(self):
        catalog = [_item("Mango Juice", "mj"), _item("Adenta Bread", "ab")]
        intent, slots, _ = classify_customer_shop_intent(
            "Im at adenta",
            catalog=catalog,
            current_intent="create_order",
        )
        self.assertEqual(intent, "create_order")
        self.assertEqual(slots.get("customer_location"), "adenta")
        self.assertNotIn("item_name", slots)
        self.assertNotIn("product_name", slots)

    def test_location_outside_an_order_is_not_a_catalog_lookup(self):
        catalog = [_item("Adenta Bread", "ab")]
        intent, slots, _ = classify_customer_shop_intent(
            "Im at adenta",
            catalog=catalog,
        )
        self.assertEqual(intent, "business_conversation")
        self.assertEqual(slots, {})

    def test_unclear_sentence_is_not_stored_as_the_product(self):
        from core.nlu.service.customer_shop import scrub_non_product_slots

        slots = scrub_non_product_slots(
            {"item_name": "Im at adenta", "quantity": "1"},
            "Im at adenta",
        )
        self.assertNotIn("item_name", slots)
        self.assertEqual(slots.get("customer_location"), "adenta")


if __name__ == "__main__":
    unittest.main()
