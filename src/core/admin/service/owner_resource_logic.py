"""Pure helpers for the owner resources feed. No database access."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, quote, urlparse
from xml.etree import ElementTree

_YT_ID = re.compile(r"^[\w-]{11}$")

INDUSTRY_NEWS_QUERIES = {
    "Agriculture & Farming": "farming agribusiness small farm",
    "Automotive": "auto repair car dealership small business",
    "Beauty & Personal Care": "beauty salon spa small business",
    "Construction & Trades": "construction contractor small business",
    "Education & Training": "education tutoring training business",
    "Energy & Utilities": "solar energy small business",
    "Fashion & Apparel": "fashion apparel retail business",
    "Finance & Insurance": "small business finance insurance",
    "Food & Beverage": "restaurant bakery food business",
    "Government & Public Sector": "public sector procurement small business",
    "Healthcare & Wellness": "clinic wellness small business",
    "Hospitality & Tourism": "hotel restaurant tourism business",
    "Logistics & Transportation": "logistics delivery small business",
    "Manufacturing": "small manufacturing business",
    "Media & Entertainment": "media creator small business",
    "Nonprofit & Community": "nonprofit fundraising",
    "Professional Services": "professional services small business",
    "Real Estate & Property": "real estate property business",
    "Retail & E-commerce": "ecommerce retail small business",
    "Technology & Software": "software startup small business",
    "Other": "small business",
}


def youtube_id_from(raw: str) -> Optional[str]:
    value = (raw or "").strip()
    if not value:
        return None
    if _YT_ID.match(value):
        return value
    parsed = urlparse(value)
    host = (parsed.netloc or "").lower()
    if "youtu.be" in host:
        segment = parsed.path.strip("/").split("/")[0] if parsed.path else ""
        return segment if _YT_ID.match(segment) else None
    query_id = (parse_qs(parsed.query).get("v") or [None])[0]
    if query_id and _YT_ID.match(query_id):
        return query_id
    parts = [part for part in parsed.path.split("/") if part]
    for marker in ("embed", "shorts", "live"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts) and _YT_ID.match(parts[index + 1]):
                return parts[index + 1]
    return None


def thumbnail_for(url: Optional[str], explicit: Optional[str] = None) -> Optional[str]:
    given = (explicit or "").strip()
    if given:
        return given
    video_id = youtube_id_from(url or "")
    if not video_id:
        return None
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def news_search_query(industry: Optional[str]) -> str:
    label = (industry or "").strip()
    if label in INDUSTRY_NEWS_QUERIES:
        return INDUSTRY_NEWS_QUERIES[label]
    if label:
        return f"{label} small business"
    return "small business"


def news_rss_url(industry: Optional[str]) -> str:
    query = quote(news_search_query(industry))
    return (
        "https://news.google.com/rss/search?"
        f"q={query}&hl=en-GH&gl=GH&ceid=GH:en"
    )


def rank_for_industry(rows: Sequence[Any], industry: Optional[str]) -> List[Any]:
    """Niche matches first, then items tagged for every industry. Other niches drop out."""
    wanted = (industry or "").strip().casefold()

    def bucket(row: Any) -> int:
        tag = (getattr(row, "industry", None) or "").strip()
        if wanted and tag.casefold() == wanted:
            return 0
        if not tag:
            return 1
        return 2

    chosen = [row for row in rows if bucket(row) < 2]

    def sort_key(row: Any):
        published = getattr(row, "published_at", None)
        stamp = published.timestamp() if isinstance(published, datetime) else 0
        return (bucket(row), int(getattr(row, "sort_order", 0) or 0), -stamp)

    chosen.sort(key=sort_key)
    return chosen


def parse_news_rss(xml_text: str, *, industry: Optional[str] = None, limit: int = 6) -> List[dict]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    items: List[dict] = []
    for node in root.findall("./channel/item"):
        title = _text(node.find("title"))
        link = _text(node.find("link"))
        if not title or not link:
            continue
        source_node = node.find("source")
        source = _text(source_node) or _source_from_title(title)
        headline = _headline(title, source)
        published = _parse_date(_text(node.find("pubDate")))
        digest = hashlib.sha1(link.encode("utf-8")).hexdigest()[:12]
        items.append(
            {
                "id": f"live_{digest}",
                "kind": "news",
                "title": headline[:200],
                "summary": None,
                "url": link,
                "youtube_id": None,
                "thumbnail_url": None,
                "industry": industry,
                "source_name": source or "News",
                "body": None,
                "active": True,
                "sort_order": 0,
                "published_at": published,
            }
        )
        if len(items) >= limit:
            break
    return items


def _text(node) -> str:
    if node is None or node.text is None:
        return ""
    return re.sub(r"\s+", " ", node.text).strip()


def _source_from_title(title: str) -> str:
    if " - " not in title:
        return ""
    return title.rsplit(" - ", 1)[-1].strip()


def _headline(title: str, source: str) -> str:
    if source and title.endswith(f" - {source}"):
        return title[: -(len(source) + 3)].strip() or title
    return title


def _parse_date(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _video(
    seed_id: str,
    title: str,
    video_id: str,
    *,
    industry: Optional[str],
    source: str,
    summary: str,
    sort_order: int = 0,
) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    return {
        "id": seed_id,
        "kind": "video",
        "title": title,
        "summary": summary,
        "url": url,
        "thumbnail_url": thumbnail_for(url),
        "industry": industry,
        "source_name": source,
        "body": None,
        "sort_order": sort_order,
    }


def _item(**kwargs) -> dict:
    kwargs.setdefault("thumbnail_url", None)
    kwargs.setdefault("body", None)
    kwargs.setdefault("summary", None)
    kwargs.setdefault("industry", None)
    kwargs.setdefault("sort_order", 0)
    return kwargs


# Catalogue of public videos and articles. Not inserted on startup, so an admin
# delete stays deleted. seed_owner_resources() can still apply this list by hand.
SEED_RESOURCES: Iterable[dict] = [
    _video(
        "res_seed_v_sinek",
        "Start with why",
        "u4ZoJKF_VuA",
        industry=None,
        source="TEDx Talks",
        summary="Simon Sinek on why customers follow a business that leads with purpose, not a feature list.",
        sort_order=20,
    ),
    _video(
        "res_seed_v_gross",
        "The single biggest reason start-ups succeed",
        "bNpx7gpSqbY",
        industry=None,
        source="TED",
        summary="Bill Gross on timing, team, and the idea — a useful check before you spend on the next move.",
        sort_order=21,
    ),
    _video(
        "res_seed_v_shopify",
        "The official Shopify tutorial",
        "ferhOYx1NMo",
        industry="Retail & E-commerce",
        source="Learn With Shopify",
        summary="Set up an online store: theme, products, domain, and the first pages customers will see.",
    ),
    _video(
        "res_seed_v_shopify2",
        "Set up your store the right way",
        "VVyBLLu8fXs",
        industry="Retail & E-commerce",
        source="Learn With Shopify",
        summary="Collections, menus, legal pages, payments, and checkout for a shop that is ready to take orders.",
        sort_order=1,
    ),
    _video(
        "res_seed_v_fashion",
        "Build the online shop for your label",
        "ferhOYx1NMo",
        industry="Fashion & Apparel",
        source="Learn With Shopify",
        summary="The same store setup walkthrough, useful when you sell clothing or accessories online.",
    ),
    _video(
        "res_seed_v_bakery",
        "How to start a bakery business",
        "jkTpzMJxUFA",
        industry="Food & Beverage",
        source="BNR Universe",
        summary="Bakery models, startup costs, location, pricing, and how to get the first customers in the door.",
    ),
    _video(
        "res_seed_v_salon",
        "How to open a salon",
        "FRuUJtF43j0",
        industry="Beauty & Personal Care",
        source="Square",
        summary="Concept, pricing, location, bookings, and the systems that keep a salon from becoming chaotic.",
    ),
    _video(
        "res_seed_v_milady",
        "Owning a salon, spa, or barbershop",
        "api6J8ch6EU",
        industry="Beauty & Personal Care",
        source="Learning Nails & Esthetics",
        summary="What changes when you stop only doing the service and start running the beauty business.",
        sort_order=1,
    ),
    _video(
        "res_seed_v_farm",
        "How to start a small farm",
        "heTxEsrPVdQ",
        industry="Agriculture & Farming",
        source="No-Till Growers",
        summary="A practical walkthrough of starting a farm business, including cost and planning.",
    ),
    _video(
        "res_seed_v_property",
        "Start a buy-to-let property business",
        "LpKmu2276Xg",
        industry="Real Estate & Property",
        source="Your First Four Houses",
        summary="How an investment property portfolio is put together, and the checks before you buy.",
    ),
    _video(
        "res_seed_v_quickbooks",
        "What is a sole proprietorship?",
        "aKkcggE7SRw",
        industry="Finance & Insurance",
        source="Intuit QuickBooks",
        summary="A short explanation of the simplest business structure, and what it means for tax and liability.",
    ),
    _video(
        "res_seed_v_incorporate",
        "Incorporated and unincorporated businesses",
        "ODmzwj-tBQQ",
        industry="Professional Services",
        source="A Series",
        summary="The difference between trading in your own name and running through a company.",
    ),
    _video(
        "res_seed_v_yc",
        "How to start a startup",
        "CBYhVcO4WgI",
        industry="Technology & Software",
        source="Y Combinator",
        summary="Sam Altman and Dustin Moskovitz on the first decisions when you start a technology company.",
    ),
    _video(
        "res_seed_v_media",
        "How great leaders inspire action",
        "qp0HIF3SfI4",
        industry="Media & Entertainment",
        source="TED",
        summary="The talk media and brand businesses use when they need a message people actually repeat.",
    ),
    _item(
        id="res_seed_n_sba",
        kind="news",
        title="10 steps to start your business",
        summary="Planning, funding, registration, and the licenses most new businesses have to sort out.",
        url="https://www.sba.gov/business-guide/10-steps-start-your-business",
        source_name="U.S. Small Business Administration",
        sort_order=30,
    ),
    _item(
        id="res_seed_n_plan",
        kind="news",
        title="Write your business plan",
        summary="What to put in a plan you can actually use: customers, offer, costs, and how you will get paid.",
        url="https://www.sba.gov/business-guide/plan-your-business/write-your-business-plan",
        source_name="U.S. Small Business Administration",
        sort_order=31,
    ),
    _item(
        id="res_seed_n_bakery",
        kind="news",
        title="How to sell baked goods online",
        summary="Store setup, licensing, delivery, and in-person sales for a bakery that also takes orders online.",
        url="https://www.shopify.com/sell/baked-goods",
        industry="Food & Beverage",
        source_name="Shopify",
    ),
    _item(
        id="res_seed_n_property",
        kind="news",
        title="Breaking into commercial property",
        summary="How early investors look at tenants, leases, and cash flow instead of treating a building like a home.",
        url="https://www.jll.com/en-us/guides/the-early-investors-guide-to-breaking-into-commercial-real-estate",
        industry="Real Estate & Property",
        source_name="JLL",
    ),
    _item(
        id="res_seed_g_outlets",
        kind="guide",
        title="Connect the places customers already message you",
        summary="Link WhatsApp, Instagram, Facebook, TikTok, or YouTube so Autobus can publish and reply there.",
        source_name="Autobus",
        body=(
            "Open Marketing, then Manage outlets.\n\n"
            "Link the channel your customers already use. WhatsApp and Instagram are the usual first two. "
            "Facebook, TikTok, and YouTube can follow once the first one is receiving messages.\n\n"
            "After a channel is connected, new chats show up in Inbox, and Marketing can publish to that outlet. "
            "If a connection asks you to approve access, finish that prompt in the same sitting — a half-connected outlet will not receive replies."
        ),
    ),
    _item(
        id="res_seed_g_products",
        kind="guide",
        title="Add what you actually sell",
        summary="Products with a price and a photo give your inbox and your AI something real to talk about.",
        source_name="Autobus",
        sort_order=1,
        body=(
            "Open Products and add the things people ask for most. Start with three, not the whole catalogue.\n\n"
            "Each item needs a name, a price in your currency, and at least one clear photo. "
            "A short description helps when a customer asks what is included.\n\n"
            "Once those products exist, orders and the chatbot can refer to them by name instead of inventing an answer."
        ),
    ),
    _item(
        id="res_seed_g_inbox",
        kind="guide",
        title="Reply from one inbox",
        summary="Chats from the channels you connected land in Inbox. The customer still sees the reply on their app.",
        source_name="Autobus",
        sort_order=2,
        body=(
            "Open Inbox. Each conversation is the thread from one customer on one channel.\n\n"
            "Reply in Autobus and the message goes back on WhatsApp, Instagram, or whichever outlet it came from. "
            "You do not need to switch apps to answer.\n\n"
            "Unanswered threads are the ones that cost you the sale. Clear those before you spend time on a new post."
        ),
    ),
    _item(
        id="res_seed_g_marketing",
        kind="guide",
        title="Publish a post without leaving Autobus",
        summary="Write the caption, pick the outlets, and check Recent posts to see what went out.",
        source_name="Autobus",
        sort_order=3,
        body=(
            "Open Marketing and start a post. Write the caption the way you would say it to a customer standing in front of you.\n\n"
            "Choose the outlets you already connected. A photo or short video performs better than text alone on Instagram and TikTok.\n\n"
            "After you publish, open Recent posts to confirm it left Autobus. If an outlet failed, the status on that post tells you which one."
        ),
    ),
    _item(
        id="res_seed_g_ai",
        kind="guide",
        title="Teach My AI what your business does",
        summary="Your business profile is what the chatbot uses when someone asks who you are and what you sell.",
        source_name="Autobus",
        sort_order=4,
        body=(
            "Open Intelligence and check the business name, what you sell, who you serve, and where you operate.\n\n"
            "If you have a price list, menu, or brochure, upload it there. The chatbot answers from those files plus the profile you wrote.\n\n"
            "A vague profile produces vague replies. One clear sentence about what you sell is more useful than a long slogan."
        ),
    ),
    _item(
        id="res_seed_g_reports",
        kind="guide",
        title="Read the week in Reports",
        summary="Orders, revenue, and activity in one place so you can see what actually sold.",
        source_name="Autobus",
        sort_order=5,
        body=(
            "Open Reports once a week. Look at orders and revenue before you look at anything else.\n\n"
            "If a channel is quiet, that is a cue to post there or to stop spending time on an outlet nobody uses.\n\n"
            "Reports describe what already happened. The inbox is where the next order still is."
        ),
    ),
    _item(
        id="res_seed_o_week",
        kind="other",
        title="Weekly owner checklist",
        summary="Four things that keep the business moving without a long planning session.",
        source_name="Autobus",
        body=(
            "1. Reply to every unanswered inbox thread.\n"
            "2. Confirm new orders and mark the ones you have fulfilled.\n"
            "3. Publish one post on the channel your customers actually use.\n"
            "4. Open Reports and note what sold."
        ),
    ),
    _item(
        id="res_seed_o_first",
        kind="other",
        title="First-week setup",
        summary="The shortest path from a new account to a business customers can reach.",
        source_name="Autobus",
        sort_order=1,
        body=(
            "1. Finish your business profile, including the industry. That industry is what picks the videos on your home screen.\n"
            "2. Connect at least one outlet.\n"
            "3. Add three products or services with prices.\n"
            "4. Send one post.\n"
            "5. Turn on notifications so a new message is not waiting until tomorrow."
        ),
    ),
]
