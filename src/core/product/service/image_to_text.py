"""Turn a customer photo into text a text-only model can read.

gpt-oss-120b does not accept images. Pillow reads colors, optional Tesseract
reads printed words, and the catalog match (already computed) is written out
as a sentence. No vision model is called.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from PIL import Image

from core.product.service.image_fingerprint import open_image
from core.product.service.image_match import MatchDecision


def image_to_text(image_bytes: bytes, decision: Optional[MatchDecision] = None) -> str:
    """Plain-text description of a customer photo."""
    lines = ["Photo notes:"]
    image = open_image(image_bytes or b"")
    if image is None:
        lines.append("- The file could not be opened as an image.")
    else:
        colors = main_color_names(image)
        if colors:
            lines.append("- Main colors: " + ", ".join(colors))
        printed = printed_text(image)
        if printed:
            lines.append("- Text in the photo: " + printed)
    lines.append("- " + catalog_match_line(decision))
    return "\n".join(lines)


def main_color_names(image: Image.Image, limit: int = 2) -> List[str]:
    """Up to `limit` plain color names, most common first."""
    small = image.convert("RGB").resize((16, 16), Image.Resampling.BOX)
    reader = getattr(small, "get_flattened_data", None)
    pixels = reader() if reader is not None else small.getdata()
    counts = {}
    for pixel in pixels:
        name = color_name(pixel)
        counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [name for name, _count in ranked[:limit]]


def color_name(pixel: Sequence[int]) -> str:
    red, green, blue = int(pixel[0]), int(pixel[1]), int(pixel[2])
    if max(red, green, blue) < 45:
        return "black"
    if min(red, green, blue) > 210:
        return "white"
    if max(red, green, blue) - min(red, green, blue) < 25:
        return "gray"
    channels: List[Tuple[str, int]] = [("red", red), ("green", green), ("blue", blue)]
    channels.sort(key=lambda item: item[1], reverse=True)
    first, second = channels[0], channels[1]
    if first[1] > 140 and second[1] > 100 and first[0] == "red" and second[0] == "green":
        return "yellow" if red >= green else "green"
    if first[0] == "red" and green > 80 and blue < 80:
        return "orange" if green > 100 else "brown"
    if first[0] == "red" and blue > 80 and green < 100:
        return "pink"
    return first[0]


def printed_text(image: Image.Image) -> str:
    """Words printed on the photo, when Tesseract is installed. Empty otherwise."""
    try:
        import pytesseract
    except ImportError:
        return ""
    try:
        raw = pytesseract.image_to_string(image) or ""
    except Exception:
        return ""
    words = " ".join(raw.split())
    return words[:240]


def catalog_match_line(decision: Optional[MatchDecision]) -> str:
    if decision is None or not decision.found or not decision.hits:
        return "Catalog match: none. Do not invent a product name, price, or stock."
    if decision.kind == "exact":
        hit = decision.hits[0]
        price = f", price {hit.price}" if hit.price else ""
        return f"Catalog match: exact, {hit.name}{price}."
    names = ", ".join(hit.name for hit in decision.hits if hit.name)
    return f"Catalog match: closest are {names}. Ask which one they mean."
