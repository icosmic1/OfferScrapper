#!/usr/bin/env python3
import argparse
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Callable, Iterable
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MAX_BRAND_TOKENS = 4
UNKNOWN_BRAND = "Unknown"
CURRENCY_PRICE_PATTERN = r"[$€£₹]\s*([\d,]+(?:\.\d{1,2})?)"
IGNORED_BRAND_TOKENS = {"new", "men", "women", "for", "with", "and", "the", "unisex", "official"}
INDIA_PINCODE_PATTERN = r"^\d{6}$"
AVAILABLE_AVAILABILITY_TOKENS = {"instock", "preorder", "limitedavailability"}
UNAVAILABLE_AVAILABILITY_TOKENS = {"outofstock", "soldout", "discontinued"}
DEFAULT_DELIVERY_AVAILABILITY = "unknown"


@dataclass
class ScrapeInput:
    product_keyword: str
    country: str
    pincode: str
    max_pages: int = 3


@dataclass
class Offer:
    brand_name: str
    product_title: str
    best_available_price: float
    original_price: float | None
    source_website: str
    source_url: str
    delivery_availability: str


class RateLimitedSession:
    def __init__(self, requests_per_second: float = 1.0):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.min_interval = 1.0 / max(requests_per_second, 0.1)
        self._last_request_at = 0.0

    def get(self, url: str, *, timeout: float = 20.0) -> requests.Response:
        wait_time = self.min_interval - (time.time() - self._last_request_at)
        if wait_time > 0:
            time.sleep(wait_time)

        retries = 3
        backoff = 1.5
        for attempt in range(retries):
            response = self.session.get(url, timeout=timeout)
            self._last_request_at = time.time()
            if response.status_code not in (429, 500, 502, 503, 504):
                response.raise_for_status()
                return response
            if attempt < retries - 1:
                retry_after = response.headers.get("Retry-After")
                pause = backoff * (2 ** attempt)
                if retry_after:
                    try:
                        pause = float(retry_after)
                    except ValueError:
                        parsed = parsedate_to_datetime(retry_after)
                        pause = max((parsed.timestamp() - time.time()), 0.0)
                time.sleep(pause)
        response.raise_for_status()
        return response


def validate_input(data: ScrapeInput) -> None:
    if not data.product_keyword.strip():
        raise ValueError("product_keyword is required")

    country_key = data.country.strip().lower()
    if country_key != "india":
        raise ValueError("Only India is supported")

    if not re.match(INDIA_PINCODE_PATTERN, str(data.pincode).strip()):
        raise ValueError("Invalid pincode format for India")


class BaseProvider:
    name = "base"

    def search(self, data: ScrapeInput, session: RateLimitedSession) -> list[Offer]:
        raise NotImplementedError


class DummyJsonProvider(BaseProvider):
    name = "dummyjson.com"

    def search(self, data: ScrapeInput, session: RateLimitedSession) -> list[Offer]:
        offers: list[Offer] = []
        limit = 30
        for page in range(data.max_pages):
            skip = page * limit
            url = (
                "https://dummyjson.com/products/search"
                f"?q={quote_plus(data.product_keyword)}&limit={limit}&skip={skip}"
            )
            payload = session.get(url).json()
            products = payload.get("products", [])
            if not products:
                break

            for item in products:
                brand = item.get("brand") or infer_brand(item.get("title", ""))
                price = Decimal(str(item.get("price", 0)))
                discount = Decimal(str(item.get("discountPercentage", 0)))
                original_price = None
                if Decimal("0") < discount < Decimal("100"):
                    discount_multiplier = Decimal("1") - (discount / Decimal("100"))
                    original_price = float(round(price / discount_multiplier, 2))
                offers.append(
                    Offer(
                        brand_name=brand,
                        product_title=item.get("title", "").strip(),
                        best_available_price=float(price),
                        original_price=original_price,
                        source_website=self.name,
                        source_url=f"https://dummyjson.com/products/{item.get('id')}",
                        delivery_availability="available",
                    )
                )
        return offers


class EbayProvider(BaseProvider):
    name = "ebay.in"

    def search(self, data: ScrapeInput, session: RateLimitedSession) -> list[Offer]:
        offers: list[Offer] = []
        for page in range(1, data.max_pages + 1):
            url = (
                "https://www.ebay.in/sch/i.html"
                f"?_nkw={quote_plus(data.product_keyword)}"
                f"&_pgn={page}&_stpos={quote_plus(data.pincode)}"
            )
            html = session.get(url).text
            page_offers = parse_ebay_page(html)
            if not page_offers:
                break
            offers.extend(page_offers)
        return offers


class JsonLdSearchProvider(BaseProvider):
    search_url_template = ""
    page_size = 24
    brand_override: str | None = None

    def build_search_url(self, data: ScrapeInput, page: int) -> str:
        return self.search_url_template.format(
            query=quote_plus(data.product_keyword),
            page=page,
            offset=(page - 1) * self.page_size,
        )

    def search(self, data: ScrapeInput, session: RateLimitedSession) -> list[Offer]:
        offers: list[Offer] = []
        for page in range(1, data.max_pages + 1):
            url = self.build_search_url(data, page)
            html = session.get(url).text
            page_offers = parse_json_ld_offers(
                html,
                source_website=self.name,
                source_url=url,
                brand_override=self.brand_override,
            )
            if not page_offers:
                break
            offers.extend(page_offers)
        return offers


class NykaaProvider(JsonLdSearchProvider):
    name = "nykaa.com"
    search_url_template = "https://www.nykaa.com/search/result/?q={query}&page_no={page}"


class NykaaManProvider(JsonLdSearchProvider):
    name = "nykaaman.com"
    search_url_template = "https://www.nykaaman.com/search/result/?q={query}&page_no={page}"


class AdidasIndiaProvider(JsonLdSearchProvider):
    name = "adidas.co.in"
    search_url_template = "https://www.adidas.co.in/search?q={query}&start={offset}"
    brand_override = "adidas"


class PumaIndiaProvider(JsonLdSearchProvider):
    name = "in.puma.com"
    search_url_template = "https://in.puma.com/in/en/search?q={query}&start={offset}"
    brand_override = "puma"


class ReebokIndiaProvider(JsonLdSearchProvider):
    name = "reebok.in"
    search_url_template = "https://www.reebok.in/search?q={query}&start={offset}"
    brand_override = "reebok"


def parse_ebay_page(html: str) -> list[Offer]:
    soup = BeautifulSoup(html, "html.parser")
    offers: list[Offer] = []

    for card in soup.select("li.s-item"):
        title_el = card.select_one(".s-item__title")
        price_el = card.select_one(".s-item__price")
        link_el = card.select_one(".s-item__link")
        shipping_el = card.select_one(".s-item__shipping, .s-item__logisticsCost")

        if not title_el or not price_el or not link_el:
            continue

        title = title_el.get_text(" ", strip=True)
        if not title or title.lower().startswith("shop on ebay"):
            continue

        price = parse_first_price(price_el.get_text(" ", strip=True))
        if price is None:
            continue

        shipping_text = shipping_el.get_text(" ", strip=True).lower() if shipping_el else ""
        unavailable = "does not ship" in shipping_text or "not available" in shipping_text

        offers.append(
            Offer(
                brand_name=infer_brand(title),
                product_title=title,
                best_available_price=price,
                original_price=None,
                source_website="ebay.in",
                source_url=link_el.get("href", "").strip(),
                delivery_availability="unavailable" if unavailable else "available",
            )
        )
    return offers


def parse_json_ld_offers(
    html: str,
    *,
    source_website: str,
    source_url: str,
    brand_override: str | None = None,
) -> list[Offer]:
    soup = BeautifulSoup(html, "html.parser")
    offers: list[Offer] = []
    seen: set[tuple[str, str]] = set()

    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for product in iter_json_ld_products(payload):
            title = str(product.get("name", "")).strip()
            if not title:
                continue

            current_url = resolve_product_url(product.get("url"), source_url)
            offer_blob = product.get("offers")
            price = extract_price(offer_blob)
            if price is None:
                continue

            original_price = extract_original_price(offer_blob, current_price=price)
            delivery_availability = extract_delivery_availability(offer_blob)
            brand_name = brand_override or extract_brand_name(product) or infer_brand(title)
            key = (title.lower(), current_url.lower())
            if key in seen:
                continue
            seen.add(key)
            offers.append(
                Offer(
                    brand_name=brand_name,
                    product_title=title,
                    best_available_price=price,
                    original_price=original_price,
                    source_website=source_website,
                    source_url=current_url,
                    delivery_availability=delivery_availability,
                )
            )
    return offers


def iter_json_ld_products(payload: object) -> Iterable[dict]:
    if isinstance(payload, list):
        for item in payload:
            yield from iter_json_ld_products(item)
        return

    if not isinstance(payload, dict):
        return

    item_type = str(payload.get("@type", "")).lower()
    if item_type == "product":
        yield payload

    if item_type == "itemlist":
        for entry in payload.get("itemListElement", []):
            if isinstance(entry, dict):
                candidate = entry.get("item", entry)
                yield from iter_json_ld_products(candidate)

    for value in payload.values():
        if isinstance(value, (dict, list)):
            yield from iter_json_ld_products(value)


def extract_brand_name(product: dict) -> str | None:
    brand = product.get("brand")
    if isinstance(brand, dict):
        value = brand.get("name")
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(brand, str) and brand.strip():
        return brand.strip()
    return None


def extract_price(offer_blob: object) -> float | None:
    for candidate in normalize_offer_candidates(offer_blob):
        for key in ("price", "lowPrice", "highPrice"):
            value = candidate.get(key)
            if value is None:
                continue
            parsed = parse_first_price(str(value))
            if parsed is not None:
                return parsed
    return None


def extract_original_price(offer_blob: object, *, current_price: float) -> float | None:
    for candidate in normalize_offer_candidates(offer_blob):
        for key in ("highPrice", "listPrice", "priceBeforeDiscount"):
            value = candidate.get(key)
            if value is None:
                continue
            parsed = parse_first_price(str(value))
            if parsed is not None and parsed > current_price:
                return parsed
    return None


def normalize_offer_candidates(offer_blob: object) -> list[dict]:
    if isinstance(offer_blob, dict):
        return [offer_blob]
    if isinstance(offer_blob, list):
        return [item for item in offer_blob if isinstance(item, dict)]
    return []


def resolve_product_url(url: object, base_url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        return base_url
    return urljoin(base_url, url.strip())


def extract_delivery_availability(offer_blob: object) -> str:
    for candidate in normalize_offer_candidates(offer_blob):
        value = str(candidate.get("availability", "")).strip().lower()
        if not value:
            continue
        parts = [part for part in re.split(r"[#/]", value) if part]
        canonical = parts[-1] if parts else value
        normalized = re.sub(r"[^a-z0-9]+", "", canonical)
        if normalized in AVAILABLE_AVAILABILITY_TOKENS:
            return "available"
        if normalized in UNAVAILABLE_AVAILABILITY_TOKENS:
            return "unavailable"
    return DEFAULT_DELIVERY_AVAILABILITY


def parse_first_price(text: str) -> float | None:
    match = re.search(CURRENCY_PRICE_PATTERN, text)
    if not match:
        match = re.search(r"([\d,]+(?:\.\d{1,2})?)", text)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def infer_brand(title: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9&'-]+", title)
    for token in tokens[:MAX_BRAND_TOKENS]:
        cleaned = re.sub(r"['’]s$", "", token).strip("&'’")
        if len(cleaned) > 1 and cleaned.lower() not in IGNORED_BRAND_TOKENS:
            return cleaned
    return UNKNOWN_BRAND


def best_price_per_brand(offers: Iterable[Offer]) -> list[Offer]:
    by_brand: dict[str, Offer] = {}
    for offer in offers:
        if offer.delivery_availability == "unavailable":
            continue

        key = (offer.brand_name or UNKNOWN_BRAND).strip().lower() or UNKNOWN_BRAND.lower()
        existing = by_brand.get(key)
        if existing is None or offer.best_available_price < existing.best_available_price:
            by_brand[key] = offer

    return sorted(by_brand.values(), key=lambda o: (o.brand_name.lower(), o.best_available_price))


def run(data: ScrapeInput, providers: list[BaseProvider]) -> list[Offer]:
    validate_input(data)
    session = RateLimitedSession(requests_per_second=1.0)

    collected: list[Offer] = []
    for provider in providers:
        try:
            collected.extend(provider.search(data, session))
        except requests.RequestException as exc:
            logging.warning("Provider %s failed: %s", provider.name, exc)
            continue

    return best_price_per_brand(collected)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Find best priced branded product offers in India")
    parser.add_argument("product_keyword", type=str)
    parser.add_argument("country", type=str, help="Country (only India is supported)")
    parser.add_argument("pincode", type=str)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--pretty", action="store_true", help="Pretty print JSON output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    data = ScrapeInput(
        product_keyword=args.product_keyword,
        country=args.country,
        pincode=args.pincode,
        max_pages=max(1, args.max_pages),
    )
    results = run(
        data,
        providers=[
            NykaaProvider(),
            NykaaManProvider(),
            AdidasIndiaProvider(),
            PumaIndiaProvider(),
            ReebokIndiaProvider(),
            EbayProvider(),
            DummyJsonProvider(),
        ],
    )
    payload = [asdict(o) for o in results]
    print(json.dumps(payload, indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()
