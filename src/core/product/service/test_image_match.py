"""Image fingerprint and customer-photo intent tests. No database."""

from __future__ import annotations

import unittest

from PIL import Image

from core.nlu.service.customer_shop import CatalogItem
from core.product.service.image_fingerprint import (
    EXACT_HAMMING,
    RELATED_COSINE,
    cosine,
    dhash_hex,
    find_match_code,
    hamming,
    jpeg_bytes,
    open_image,
    stamp_match_code,
    visual_vector,
)
from core.product.service.image_match import (
    MatchDecision,
    MatchHit,
    customer_image_intent,
    photo_should_identify_product,
)


def _solid(color, size=(240, 240)) -> Image.Image:
    return Image.new("RGB", size, color)


def _red_with_white_box(offset: int) -> Image.Image:
    image = Image.new("RGB", (240, 240), (180, 20, 20))
    for y in range(40, 120):
        for x in range(offset, offset + 70):
            image.putpixel((x, y), (245, 245, 245))
    return image


class FingerprintTests(unittest.TestCase):
    def test_recompressed_catalogue_photo_stays_exact(self):
        original = _red_with_white_box(30)
        recompressed = open_image(jpeg_bytes(original, quality=30))
        self.assertIsNotNone(recompressed)
        distance = hamming(dhash_hex(original), dhash_hex(recompressed))
        self.assertIsNotNone(distance)
        self.assertLessEqual(distance, EXACT_HAMMING)

    def test_stamped_post_matches_its_published_hash(self):
        original = _red_with_white_box(30)
        stamped = stamp_match_code(original, "AB-7K2M")
        published = open_image(jpeg_bytes(stamped, quality=45))
        self.assertIsNotNone(published)
        distance = hamming(dhash_hex(stamped), dhash_hex(published))
        self.assertLessEqual(distance, EXACT_HAMMING)
        self.assertNotEqual(dhash_hex(original), dhash_hex(stamped))

    def test_different_photo_is_not_an_exact_hash(self):
        distance = hamming(dhash_hex(_solid((200, 10, 10))), dhash_hex(_solid((10, 10, 200))))
        self.assertTrue(distance is None or distance > EXACT_HAMMING)

    def test_similar_layout_is_related_and_opposite_color_is_not(self):
        left = visual_vector(_red_with_white_box(20))
        right = visual_vector(_red_with_white_box(90))
        blue = visual_vector(_solid((10, 20, 200)))
        self.assertGreaterEqual(cosine(left, right), RELATED_COSINE)
        self.assertLess(cosine(left, blue), RELATED_COSINE)

    def test_code_is_read_from_a_caption(self):
        self.assertEqual(find_match_code("the tag says ab-7k2m on the sleeve"), "AB-7K2M")
        self.assertIsNone(find_match_code("no code here"))

    def test_stamp_round_trip_opens(self):
        stamped = stamp_match_code(_solid((20, 140, 60), size=(640, 640)), "AB-HQ4P")
        opened = open_image(jpeg_bytes(stamped))
        self.assertEqual(opened.size, (640, 640))


class CustomerImageIntentTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [
            CatalogItem(product_id="p1", name="Mango Juice", price="12.00", stock=4)
        ]
        self.exact = MatchDecision(
            kind="exact",
            hits=[MatchHit("p1", "Mango Juice", "12.00", "https://cdn.example/a.jpg", 1.0)],
        )
        self.related = MatchDecision(
            kind="related",
            hits=[
                MatchHit("p1", "Mango Juice", "12.00", "https://cdn.example/a.jpg", 0.94),
                MatchHit("p2", "Orange Juice", "10.00", "https://cdn.example/b.jpg", 0.91),
            ],
        )

    def test_catalogue_forward_opens_that_product(self):
        intent, slots, _missing = customer_image_intent(
            "I am providing you with an image.",
            self.catalog,
            self.exact,
        )
        self.assertEqual(intent, "view_product")
        self.assertEqual(slots["product_id"], "p1")

    def test_exact_photo_with_quantity_starts_an_order(self):
        intent, slots, _missing = customer_image_intent(
            "I want 2",
            self.catalog,
            self.exact,
        )
        self.assertEqual(intent, "create_order")
        self.assertEqual(slots["item_name"], "Mango Juice")
        self.assertEqual(slots["quantity"], "2")
        self.assertEqual(slots["product_id"], "p1")

    def test_foreign_photo_returns_related_products(self):
        intent, slots, _missing = customer_image_intent(
            "do you have this",
            self.catalog,
            self.related,
        )
        self.assertEqual(intent, "view_products")
        self.assertEqual(slots["related_product_ids"], "p1,p2")
        self.assertIn("closest", slots["image_match_heading"].lower())

    def test_availability_caption_uses_the_photo(self):
        intent, slots, _missing = customer_image_intent(
            "is this available?",
            self.catalog,
            self.exact,
        )
        self.assertEqual(intent, "view_product")
        self.assertEqual(slots["product_id"], "p1")
        self.assertEqual(slots["product_name"], "Mango Juice")

    def test_vision_refusal_is_not_a_product_description(self):
        from core.product.service.image_match import is_vision_refusal

        self.assertTrue(is_vision_refusal("I can't read images"))
        self.assertTrue(is_vision_refusal("Sorry, I cannot view the image you sent."))
        self.assertFalse(is_vision_refusal("red leather handbag, gold hardware"))

    def test_named_product_and_faq_are_left_to_the_text(self):
        self.assertIsNone(
            customer_image_intent("I want mango juice", self.catalog, self.exact)
        )
        self.assertFalse(
            photo_should_identify_product("where is your location", self.catalog)
        )
        self.assertIsNone(
            customer_image_intent("thanks", self.catalog, self.exact)
        )


if __name__ == "__main__":
    unittest.main()
