"""Rule-based customer shop intents: catalog query and order placement.

Customer WhatsApp/Instagram/web sessions skip the large intent-classification LLM.
Product facts must come from the merchant Product table, never from intelligence RAG.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import re

# Keep in sync with core.nlu.service.slot_manager.is_placeholder_order_item_name
_PLACEHOLDER_ORDER_ITEM_NAMES = frozenset(
    {
        "order",
        "an order",
        "a order",
        "new order",
        "the order",
        "item",
        "items",
        "product",
        "products",
        "goods",
        "something",
        "purchase",
        "merchandise",
    }
)


def _is_placeholder_item_name(name: str) -> bool:
    n = (name or "").strip().lower()
    if len(n) < 2:
        return True
    return n in _PLACEHOLDER_ORDER_ITEM_NAMES

CUSTOMER_SHOP_INTENTS = frozenset(
    {"view_products", "view_product", "create_order", "check_order_status"}
)

_WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

_ORDER_PHRASES = (
    "place an order",
    "place order",
    "make an order",
    "create an order",
    "start an order",
    "open an order",
    "book an order",
    "i want to order",
    "i want an order",
    "i'd like to order",
    "id like to order",
    "i would like to order",
    "i want to buy",
    "i'd like to buy",
    "id like to buy",
    "i would like to buy",
    "i need to order",
    "i need to buy",
    "can i order",
    "can i buy",
    "i want to purchase",
    "please order",
    "order me",
    "get me",
    "i'll take",
    "ill take",
    "i will take",
    "add to order",
)

_ORDER_WORD_RE = re.compile(
    r"\b(?:order|orders|buy|buying|purchase|purchasing)\b",
    re.IGNORECASE,
)

_CATALOG_BROWSE_PHRASES = (
    "what products",
    "which products",
    "what do you sell",
    "what do you have",
    "what have you got",
    "what's available",
    "whats available",
    "what is available",
    "what's in stock",
    "whats in stock",
    "what is in stock",
    "show me your products",
    "show me the products",
    "show products",
    "list your products",
    "list of products",
    "your catalog",
    "your catalogue",
    "your menu",
    "your prices",
    "price list",
    "anything in stock",
    "available products",
    "products available",
    "what can i buy",
    "what can i order",
    "any product in stock",
    "any products in stock",
    "any item in stock",
    "any items in stock",
    "is there any product",
    "is there any products",
    "is there a product",
    "are there any products",
    "are there any product",
    "are there products",
    "do you have any products",
    "do you have products",
    "do you have any items",
    "do you have items",
    "have any products",
    "got any products",
    "products in stock",
    "items in stock",
    "anything available",
    "what products do you have",
    "what items do you have",
)

# Tokens that describe inventory in general, not a specific product name.
_GENERIC_QUERY_TOKENS = frozenset(
    {
        "is",
        "are",
        "there",
        "any",
        "some",
        "a",
        "an",
        "the",
        "product",
        "products",
        "item",
        "items",
        "goods",
        "stock",
        "in",
        "available",
        "listed",
        "you",
        "your",
        "have",
        "got",
        "do",
        "what",
        "which",
        "currently",
        "right",
        "now",
        "please",
    }
)

_PRODUCT_QUERY_PHRASES = (
    "do you have",
    "do you sell",
    "have you got",
    "how much is",
    "how much are",
    "how much for",
    "what's the price",
    "whats the price",
    "what is the price",
    "price of",
    "cost of",
    "is there",
    "are there",
    "still have",
    "in stock",
    "available",
)

_NAME_PREFIX_RE = re.compile(
    r"^(?:please\s+)*(?:do you (?:have|sell)|have you got|how much (?:is|are|for)|"
    r"what(?:'s|s| is) the price of|price of|cost of|"
    r"(?:is|are) there(?: any| some)?|"
    r"i (?:want|need|wanna|would like|'d like|d like)"
    r"(?: to (?:order|buy|purchase|get))?(?:\s+for)?|"
    r"can i (?:order|buy|get)|(?:order|buy|get) me|"
    r"(?:give|send) me|"
    r"i(?:'ll|ll| will) take)\s+",
    re.IGNORECASE,
)

_LEADING_PREP_RE = re.compile(r"^(?:for|of)\s+", re.IGNORECASE)

_QTY_UNITS = r"(?:pcs|pc|pieces|units?|bags?|bottles?|packs?|boxes?|cartons?)"
_WORD_NUM_ALT = "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True))
_QTY_CORE = rf"(?:(?:qty|quantity)\s+)?(?:\d+|{_WORD_NUM_ALT})\s*{_QTY_UNITS}?"

_LEADING_QTY_RE = re.compile(
    rf"^(?:x\s*)?{_QTY_CORE}(?:\s*x)?(?:\s+of)?\s+",
    re.IGNORECASE,
)
_TRAILING_QTY_RE = re.compile(
    rf"[\s,/]+(?:x\s*)?{_QTY_CORE}(?:\s*x)?\s*$",
    re.IGNORECASE,
)

_MATCH_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "and",
        "or",
        "for",
        "to",
        "in",
        "on",
        "with",
        "please",
        "pcs",
        "pc",
        "pieces",
        "units",
        "unit",
        "x",
        "i",
        "want",
        "need",
        "like",
        "would",
        "wanna",
        "buy",
        "order",
        "get",
        "me",
        "my",
        "some",
        "any",
        "give",
        "send",
        "take",
        "can",
    }
)

_THANKS_RE = re.compile(
    r"^(?:(?:ok|okay|alright|all right|it's ok(?:ay)?|its ok(?:ay)?|"
    r"it's fine|its fine)\s+)?"
    r"(?:thanks|thank you|thx|ty)"
    r"(?:\s+(?:ok|okay|so much|a lot|very much))?"
    r"[\s.!]*$",
    re.IGNORECASE,
)

_TRAILING_JUNK_RE = re.compile(
    r"[\s?!.]+$|^(?:the|a|an|some|any)\s+",
    re.IGNORECASE,
)

_QTY_RE = re.compile(
    r"(?:(?:qty|quantity|x)\s*)?(\d+)\s*(?:pcs|pieces|units|bags?|bottles?|packs?|boxes?|cartons?)?\b",
    re.IGNORECASE,
)

_FAQ_PHRASES = (
    "hours",
    "open",
    "close",
    "closing",
    "opening",
    "location",
    "address",
    "where are you",
    "directions",
    "refund",
    "policy",
    "policies",
    "delivery fee",
    "shipping fee",
)


@dataclass(frozen=True)
class CatalogItem:
    product_id: str
    name: str
    price: Optional[str]
    stock: Optional[int]
    category: str = ""
    description: str = ""

    @property
    def in_stock(self) -> bool:
        return self.stock is None or self.stock > 0


def catalog_items_from_products(products: Iterable[Any]) -> List[CatalogItem]:
    items: List[CatalogItem] = []
    for product in products or []:
        if getattr(product, "is_active", True) is False:
            continue
        name = (getattr(product, "name", None) or "").strip()
        if not name:
            continue
        price = getattr(product, "price", None)
        stock_tracked = getattr(product, "stock_tracked", True)
        stock = None if stock_tracked is False else getattr(product, "number_in_stock", None)
        try:
            stock_int = int(stock) if stock is not None else None
        except (TypeError, ValueError):
            stock_int = None
        items.append(
            CatalogItem(
                product_id=str(getattr(product, "product_id", "") or ""),
                name=name,
                price=None if price is None else str(price),
                stock=stock_int,
                category=(getattr(product, "category", None) or "").strip(),
                description=(getattr(product, "description", None) or "").strip(),
            )
        )
    return items


def format_customer_catalog(
    catalog: Sequence[CatalogItem],
    *,
    heading: Optional[str] = None,
    currency: Optional[str] = None,
) -> str:
    from core.user.currency import format_money

    if not catalog:
        return "We don't have anything listed just yet. Tell me what you're after and I'll check with the shop."
    ordered = sorted(catalog, key=lambda item: (not item.in_stock, item.name.lower()))
    lines = [heading or "Here's what we've got right now:"]
    for index, item in enumerate(ordered[:30], 1):
        bits = [f"{index}. {item.name}"]
        if item.price is not None:
            bits.append(format_money(item.price, currency))
        bits.append(_stock_phrase(item.stock))
        lines.append(" — ".join(bits))
    if len(ordered) > 30:
        lines.append(f"...and {len(ordered) - 30} more.")
    lines.append("Tell me which one you want and how many, and I'll set it aside.")
    return "\n".join(lines)


def format_customer_product(item: CatalogItem, *, currency: Optional[str] = None) -> str:
    from core.user.currency import format_money

    bits = [item.name]
    if item.price is not None:
        bits.append(f"price {format_money(item.price, currency)}")
    bits.append(_stock_phrase(item.stock))
    line = f"{bits[0]} is {', '.join(bits[1:])}."
    extra = []
    if item.category:
        extra.append(item.category)
    if item.description:
        extra.append(item.description[:160])
    if extra:
        line = f"{line} {' '.join(extra)}"
    if item.in_stock:
        line += " Want me to put some aside for you? Just tell me how many."
    else:
        line += " That one's sold out right now. I can show you what we still have."
    return line


def _stock_phrase(stock: Optional[int]) -> str:
    if stock is None:
        return "ready when you are"
    if stock <= 0:
        return "sold out"
    if stock == 1:
        return "1 left"
    return f"{stock} ready"


def normalize_shop_text(text: str) -> str:
    t = (text or "").lower().strip()
    t = t.replace("’", "'").replace("‘", "'")
    t = re.sub(r"[.!]+$", "", t).strip()
    t = re.sub(r"\s+", " ", t)
    return t


def is_shop_cancel(text: str) -> bool:
    t = normalize_shop_text(text)
    return t in {
        "cancel",
        "stop",
        "abort",
        "never mind",
        "nevermind",
        "forget it",
        "forget about it",
        "quit",
        "cancel order",
        "cancel the order",
    }


def is_shop_thanks(text: str) -> bool:
    """True when the customer is only thanking / wrapping up, not naming a product."""
    t = normalize_shop_text(text)
    if not t or looks_like_order_request(text):
        return False
    return bool(_THANKS_RE.match(t))


_EXISTING_ORDER_PHRASES = (
    "when do i receive",
    "when will i receive",
    "when will i get my",
    "when do i get my",
    "when will i get it",
    "when do i get it",
    "where is my order",
    "where's my order",
    "wheres my order",
    "order status",
    "status of my order",
    "track my order",
    "tracking my order",
    "did you create my order",
    "did you place my order",
    "did you make my order",
    "have you created my order",
    "i placed an order",
    "i made an order",
    "i already ordered",
    "i ordered yesterday",
    "my order from",
    "my order yesterday",
    "about my order",
    "has my order",
    "receive my order",
    "delivery of my order",
    "when is my order",
)

_SIZE_RE = re.compile(r"\b(?:size|sz)\s*#?\s*(\d{2}(?:\.\d)?)\b", re.IGNORECASE)


def looks_like_existing_order_question(text: str) -> bool:
    """True when the customer is asking about an order already placed, not buying now."""
    t = normalize_shop_text(text)
    if not t:
        return False
    if re.search(r"\bi asked for \d+\b", t):
        return True
    return any(phrase in t for phrase in _EXISTING_ORDER_PHRASES)


def is_questionish_product_name(name: str) -> bool:
    """True when a slot value is a question, not a catalog product name."""
    raw = name or ""
    n = normalize_shop_text(raw)
    if not n:
        return True
    if "?" in raw:
        return True
    starters = (
        "pls ",
        "plz ",
        "please ",
        "is ",
        "are ",
        "do ",
        "does ",
        "can ",
        "when ",
        "where ",
        "did ",
        "what ",
        "how ",
        "i placed",
        "i asked",
        "i want to know",
        "this available",
    )
    if any(n.startswith(starter) for starter in starters):
        return True
    return "available in size" in n or "in size" in n


def mentioned_size(text: str) -> str:
    match = _SIZE_RE.search(text or "")
    return match.group(1) if match else ""


_LOCATION_STATEMENT_RE = re.compile(
    r"^(?:i(?:'m| am|m)|we(?:'re| are))(?:\s+(?:currently|now|just|actually|still))?\s+(?:at|in|near|around)\b",
    re.IGNORECASE,
)


def looks_like_location_statement(text: str) -> bool:
    """True when the customer is saying where they are, not naming a product."""
    t = normalize_shop_text(text)
    if not t:
        return False
    return _LOCATION_STATEMENT_RE.search(t) is not None


def extract_stated_location(text: str) -> str:
    """Place named in 'I'm at Adenta' / 'Im at adenta'. Empty when it isn't a location."""
    match = re.match(
        r"^(?:i(?:'m| am|m)|we(?:'re| are))(?:\s+(?:currently|now|just|actually|still))?\s+(?:at|in|near|around)\s+(.+)$",
        (text or "").strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    place = match.group(1).strip(" ?!.,")
    return place if len(place) >= 2 else ""


def looks_like_order_request(text: str) -> bool:
    t = normalize_shop_text(text)
    if not t:
        return False
    if any(phrase in t for phrase in _ORDER_PHRASES):
        return True
    if _ORDER_WORD_RE.search(t) and not looks_like_faq(text):
        return True
    want_starters = (
        "i want",
        "i need",
        "i wanna",
        "i'd like",
        "id like",
        "i would like",
        "give me",
        "get me",
        "i'll take",
        "ill take",
        "i will take",
    )
    if any(t.startswith(s) for s in want_starters) and not looks_like_faq(text):
        return True
    return False


def looks_like_catalog_browse(text: str) -> bool:
    t = normalize_shop_text(text)
    if not t:
        return False
    return any(phrase in t for phrase in _CATALOG_BROWSE_PHRASES)


def leftover_is_generic_catalog_query(text: str) -> bool:
    """True when leftover text is only generic inventory words, not a product name."""
    tokens = [tok for tok in normalize_shop_text(text).split() if tok]
    if not tokens:
        return True
    return all(tok in _GENERIC_QUERY_TOKENS for tok in tokens)


def looks_like_generic_stock_inquiry(
    text: str, catalog: Sequence[CatalogItem] = ()
) -> bool:
    """True for 'is there any product in stock' style questions with no named product."""
    if match_catalog_in_text(text, catalog):
        return False
    if looks_like_catalog_browse(text):
        return True
    t = normalize_shop_text(text)
    if not t:
        return False
    patterns = (
        r"\b(?:is|are) there (?:any |some )?(?:product|products|item|items|goods)\b",
        r"\b(?:do you have|have you got) (?:any |some )?(?:product|products|item|items)\b",
        r"\bany (?:product|products|item|items).*(?:stock|available)\b",
        r"\b(?:product|products|items) (?:in stock|available)\b",
        r"\banything (?:in stock|available)\b",
    )
    return any(re.search(pattern, t) for pattern in patterns)


def looks_like_product_query(text: str) -> bool:
    t = normalize_shop_text(text)
    if not t:
        return False
    if looks_like_catalog_browse(text) or looks_like_order_request(text):
        return True
    return any(phrase in t for phrase in _PRODUCT_QUERY_PHRASES)


def looks_like_buy_named_item(text: str, catalog: Sequence[CatalogItem]) -> bool:
    """True when the user is asking to take a listed product, even without the word order."""
    if not match_catalog_in_text(text, catalog) and not extract_quantity(text, catalog):
        return False
    t = normalize_shop_text(text)
    starters = (
        "i want",
        "i need",
        "i wanna",
        "i'd like",
        "id like",
        "i would like",
        "give me",
        "get me",
        "i'll take",
        "ill take",
        "i will take",
        "please give",
        "please send",
    )
    if any(t.startswith(s) for s in starters):
        return True
    return bool(extract_quantity(text, catalog) and match_catalog_in_text(text, catalog))


def looks_like_delivery_question(text: str) -> bool:
    """True for 'can you deliver to my location' — not a product and not an existing order."""
    t = normalize_shop_text(text)
    if not t or looks_like_existing_order_question(text) or looks_like_order_request(text):
        return False
    phrases = (
        "can you deliver",
        "do you deliver",
        "will you deliver",
        "could you deliver",
        "would you deliver",
        "deliver to my",
        "deliver to me",
        "delivery to my",
        "delivery to me",
        "do you do delivery",
        "do you offer delivery",
        "can i get it delivered",
        "can you send it to my",
    )
    return any(phrase in t for phrase in phrases)


def looks_like_faq(text: str) -> bool:
    t = normalize_shop_text(text)
    if not t:
        return False
    return any(phrase in t for phrase in _FAQ_PHRASES)


def looks_like_product_or_order_utterance(text: str) -> bool:
    return looks_like_product_query(text) or looks_like_order_request(text)


def extract_quantity(text: str, catalog: Sequence[CatalogItem] = ()) -> Optional[int]:
    """Parse a purchase quantity, ignoring numbers that belong to product names."""
    stripped = text or ""
    for item in sorted(catalog, key=lambda i: len(i.name), reverse=True):
        if item.name and item.name.lower() in stripped.lower():
            stripped = re.sub(re.escape(item.name), " ", stripped, flags=re.IGNORECASE)
    t = normalize_shop_text(stripped)
    if not t:
        return None
    if t.isdigit():
        qty = int(t)
        return qty if qty > 0 else None
    for word, number in _WORD_NUMBERS.items():
        if re.search(rf"\b{word}\b", t):
            return number
    match = _QTY_RE.search(t)
    if not match:
        return None
    qty = int(match.group(1))
    return qty if qty > 0 else None


def _name_mentioned(text: str, name: str) -> bool:
    if not name or not text:
        return False
    pattern = r"(?<!\w)" + re.escape(name.lower()) + r"(?!\w)"
    return re.search(pattern, text.lower()) is not None


def match_catalog_in_text(
    text: str, catalog: Sequence[CatalogItem]
) -> List[CatalogItem]:
    """Return catalog items whose names appear in the user text (longest names first)."""
    t = normalize_shop_text(text)
    if not t or not catalog:
        return []
    ranked = sorted(catalog, key=lambda item: len(item.name), reverse=True)
    hits: List[CatalogItem] = []
    used_spans = []
    for item in ranked:
        name = item.name.strip()
        if len(name) < 2:
            continue
        if not _name_mentioned(t, name):
            continue
        start = t.find(name.lower())
        end = start + len(name.lower()) if start >= 0 else -1
        if start >= 0 and any(start < u_end and end > u_start for u_start, u_end in used_spans):
            # Shorter name fully inside an already-matched longer name.
            continue
        hits.append(item)
        if start >= 0:
            used_spans.append((start, end))
    return hits


def _stem_token(tok: str) -> str:
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
        return tok[:-1]
    return tok


def _significant_tokens(text: str) -> List[str]:
    t = re.sub(r"[^a-z0-9]+", " ", normalize_shop_text(text))
    tokens: List[str] = []
    seen = set()
    for raw in t.split():
        if raw.isdigit() or raw in _MATCH_STOPWORDS:
            continue
        stemmed = _stem_token(raw)
        if stemmed in _MATCH_STOPWORDS or len(stemmed) < 2:
            continue
        if stemmed not in seen:
            seen.add(stemmed)
            tokens.append(stemmed)
    return tokens


def _tokens_compatible(query_tok: str, product_tok: str) -> bool:
    if query_tok == product_tok:
        return True
    if len(query_tok) >= 4 and (query_tok in product_tok or product_tok in query_tok):
        return True
    return False


def match_catalog_by_tokens(
    query: str, catalog: Sequence[CatalogItem]
) -> List[CatalogItem]:
    """Match catalog items by significant-word overlap (order-independent)."""
    q_tokens = _significant_tokens(query)
    if not q_tokens or not catalog:
        return []
    scored: List[Tuple[int, CatalogItem]] = []
    for item in catalog:
        p_tokens = _significant_tokens(item.name)
        if not p_tokens:
            continue
        q_set, p_set = set(q_tokens), set(p_tokens)
        score = 0
        if q_set == p_set:
            score = 1000 + len(q_set)
        elif q_set.issubset(p_set):
            score = 500 + (len(q_set) * 10) - (len(p_set) - len(q_set))
        elif p_set.issubset(q_set):
            score = 300 + (len(p_set) * 10)
        elif all(any(_tokens_compatible(qt, pt) for pt in p_set) for qt in q_tokens):
            score = 200 + (len(q_tokens) * 10)
        if score:
            scored.append((score, item))
    scored.sort(key=lambda pair: (-pair[0], -len(pair[1].name)))
    if not scored:
        return []
    best = scored[0][0]
    # Keep near-best hits so close catalog variants can be disambiguated.
    return [item for score, item in scored if score >= best - 50]


def resolve_catalog_query(
    query: str, catalog: Sequence[CatalogItem]
) -> List[CatalogItem]:
    """Resolve a typed product name against the catalog (exact, then contains)."""
    q = (query or "").strip()
    if not q or _is_placeholder_item_name(q):
        return []
    q_norm = normalize_shop_text(q)
    exact = [item for item in catalog if normalize_shop_text(item.name) == q_norm]
    if exact:
        return exact
    mentioned = match_catalog_in_text(q, catalog)
    if mentioned:
        return mentioned
    contained = [
        item
        for item in catalog
        if q_norm and q_norm in normalize_shop_text(item.name)
    ]
    if contained:
        return contained
    inverted = [
        item
        for item in catalog
        if normalize_shop_text(item.name) and normalize_shop_text(item.name) in q_norm
    ]
    if inverted:
        return inverted
    return match_catalog_by_tokens(q, catalog)


def extract_product_query_name(text: str) -> str:
    """Best-effort leftover name after stripping order/query phrasing."""
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = _NAME_PREFIX_RE.sub("", cleaned).strip()
        cleaned = _LEADING_PREP_RE.sub("", cleaned).strip()
        cleaned = _LEADING_QTY_RE.sub("", cleaned).strip()
        cleaned = _TRAILING_QTY_RE.sub("", cleaned).strip()
        cleaned = _LEADING_PREP_RE.sub("", cleaned).strip()
        cleaned = re.sub(
            r"\b(?:please|thanks|thank you|now|today)\b",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?!.,")
        cleaned = _TRAILING_JUNK_RE.sub("", cleaned).strip()
    if _is_placeholder_item_name(cleaned):
        return ""
    if leftover_is_generic_catalog_query(cleaned):
        return ""
    if len(cleaned) < 2:
        return ""
    if extract_quantity(cleaned) and normalize_shop_text(cleaned).isdigit():
        return ""
    leftover_norm = normalize_shop_text(cleaned)
    if leftover_norm.startswith(
        (
            "i want",
            "i need",
            "i wanna",
            "i would like",
            "i'd like",
            "id like",
            "can i ",
            "do you ",
        )
    ):
        return ""
    return cleaned


def is_clear_product_subject(name: str) -> bool:
    """True only when a slot value is the product itself, not the customer's sentence.

    Location lines, delivery questions, and whole utterances are not products.
    Catalog search must use this subject and must not fall back to the raw message.
    """
    raw = (name or "").strip()
    normalized = normalize_shop_text(raw)
    if not normalized or _is_placeholder_item_name(normalized):
        return False
    if is_questionish_product_name(raw):
        return False
    if looks_like_location_statement(raw) or looks_like_delivery_question(raw):
        return False
    if any(
        marker in normalized
        for marker in ("im at ", "i'm at ", "i am at ", "my location", "deliver to")
    ):
        return False
    if looks_like_order_request(raw):
        subject = normalize_shop_text(extract_product_query_name(raw))
        if subject != normalized:
            return False
    if len(normalized.split()) > 8:
        return False
    return True


def scrub_non_product_slots(
    slots: Optional[Dict[str, Any]], user_message: str
) -> Dict[str, Any]:
    """Drop product slots the model copied from a sentence that does not name a product."""
    cleaned = dict(slots or {})
    location_only = looks_like_location_statement(user_message) and not looks_like_order_request(
        user_message
    )
    if location_only or looks_like_delivery_question(user_message):
        for key in ("item_name", "product_name", "product_id", "unit_price", "item_candidates"):
            cleaned.pop(key, None)
        if location_only:
            place = extract_stated_location(user_message)
            if place:
                cleaned["customer_location"] = place
        return cleaned
    for key in ("item_name", "product_name"):
        value = str(cleaned.get(key) or "").strip()
        if value and not is_clear_product_subject(value):
            cleaned.pop(key, None)
            if key == "item_name":
                cleaned.pop("product_id", None)
                cleaned.pop("unit_price", None)
                cleaned.pop("item_candidates", None)
    return cleaned


def classify_customer_shop_intent(
    user_message: str,
    *,
    current_intent: str = "",
    collected_slots: Optional[Dict[str, Any]] = None,
    catalog: Optional[Sequence[CatalogItem]] = None,
) -> Tuple[str, Dict[str, Any], List[str]]:
    """
    Cheap rule-based shop classifier for customer-channel sessions.

    Returns (intent, extracted_slots, unused_missing_hint). Missing slots are
    computed later by SlotManager so this can stay side-effect free.
    """
    catalog = list(catalog or [])
    collected = collected_slots or {}
    text = user_message or ""
    current = (current_intent or "").strip()

    if is_shop_thanks(text):
        return "goodbye", {}, []

    # Delivery and location are not products. Never scan the sentence for a catalog hit.
    if looks_like_delivery_question(text):
        return "business_conversation", {}, []

    if looks_like_location_statement(text) and not looks_like_order_request(text):
        if current == "create_order":
            place = extract_stated_location(text)
            return "create_order", ({"customer_location": place} if place else {}), []
        return "business_conversation", {}, []

    if (
        looks_like_faq(text)
        and not match_catalog_in_text(text, catalog)
        and not looks_like_order_request(text)
        and not looks_like_catalog_browse(text)
    ):
        return "business_conversation", {}, []

    if current == "create_order":
        return "create_order", _order_slots_from_message(text, catalog, collected), []

    if current == "view_product" and not looks_like_order_request(text):
        if not collected.get("product_name") and not collected.get("product_id"):
            view_slots = _view_slots_from_message(text, catalog)
            if view_slots:
                return "view_product", view_slots, []

    mentioned = match_catalog_in_text(text, catalog)
    if looks_like_generic_stock_inquiry(text, catalog) and not mentioned:
        return "view_products", {}, []
    if looks_like_catalog_browse(text) and not mentioned:
        return "view_products", {}, []

    if looks_like_order_request(text) or looks_like_buy_named_item(text, catalog):
        return "create_order", _order_slots_from_message(text, catalog, collected), []

    if mentioned or looks_like_product_query(text):
        view_slots = _view_slots_from_message(text, catalog)
        if not view_slots and (
            looks_like_catalog_browse(text) or looks_like_generic_stock_inquiry(text, catalog)
        ):
            return "view_products", {}, []
        if not view_slots and not mentioned:
            guessed = extract_product_query_name(text)
            if guessed and is_clear_product_subject(guessed):
                return "view_product", {"product_name": guessed}, []
            return "view_products", {}, []
        return "view_product", view_slots, []

    return "business_conversation", {}, []


def _order_slots_from_message(
    text: str,
    catalog: Sequence[CatalogItem],
    collected: Dict[str, Any],
) -> Dict[str, Any]:
    slots: Dict[str, Any] = {}
    qty = extract_quantity(text, catalog)
    if qty:
        slots["quantity"] = str(qty)

    leftover = extract_product_query_name(text)
    matches = match_catalog_in_text(text, catalog)
    if leftover:
        leftover_matches = resolve_catalog_query(leftover, catalog)
        if leftover_matches:
            matches = leftover_matches

    if len(matches) == 1:
        item = matches[0]
        slots["item_name"] = item.name
        if item.price is not None:
            slots["unit_price"] = str(item.price)
        slots["product_id"] = item.product_id
    elif len(matches) > 1:
        slots["item_candidates"] = ", ".join(item.name for item in matches)
    elif (
        leftover
        and is_clear_product_subject(leftover)
        and not leftover_is_generic_catalog_query(leftover)
        and not is_shop_thanks(text)
    ):
        slots["item_name"] = leftover
    return slots


def _view_slots_from_message(
    text: str, catalog: Sequence[CatalogItem]
) -> Dict[str, Any]:
    leftover = extract_product_query_name(text)
    matches = match_catalog_in_text(text, catalog)
    if leftover:
        leftover_matches = resolve_catalog_query(leftover, catalog)
        if leftover_matches:
            matches = leftover_matches
    if not matches:
        if leftover and is_clear_product_subject(leftover) and not leftover_is_generic_catalog_query(leftover):
            return {"product_name": leftover}
        return {}
    if len(matches) == 1:
        item = matches[0]
        return {
            "product_name": item.name,
            "product_id": item.product_id,
        }
    return {
        "product_name": leftover or matches[0].name,
        "item_candidates": ", ".join(item.name for item in matches),
    }
