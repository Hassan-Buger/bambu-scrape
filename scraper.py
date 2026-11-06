"""Bambu Lab EU Storefront Catalog Scraper.

Production-grade extraction pipeline targeting the EU regional storefront
(https://eu.store.bambulab.com) exclusively across all major catalog categories.

Extracts collection metadata and deep single-page product details (PDP), including:
- Structured specifications and compatibility tables
- Complete multi-variant matrices with float pricing and live stock flags
- High-resolution media galleries from the EU CDN
- Strict EU regional constraints (preventing US/USD fallback)
"""

import argparse
import json
import logging
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, cast

from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from database import init_db, sync_category_counts, upsert_product

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger("bambu_scraper_eu")

# =====================================================================
# STRICT EU REGIONAL CONFIGURATION
# =====================================================================

TARGET_EU_DOMAIN = "eu.store.bambulab.com"
BASE_URL = f"https://{TARGET_EU_DOMAIN}"

# Strict negative constraints: any redirect or fallback to these domains is blocked
BANNED_DOMAINS = [
    "us.store.bambulab.com",
    "store.bambulab.com",
    "ca.store.bambulab.com",
    "uk.store.bambulab.com",
    "au.store.bambulab.com",
    "jp.store.bambulab.com",
]

# Major Catalog Categories Configuration
CATEGORY_CONFIGS: dict[str, dict[str, Any]] = {
    "printers": {
        "title": "3D Printers",
        "handle": "3d-printer",
        "path": ["3D Printers"],
    },
    "filaments": {
        "title": "Filaments",
        "handle": "bambu-lab-3d-printer-filament",
        "path": ["Filaments"],
    },
    "ams": {
        "title": "AMS",
        "handle": "all-ams",
        "path": ["AMS"],
    },
    "accessories": {
        "title": "Accessories",
        "handle": "accessories",
        "path": ["Accessories"],
    },
    "laser_crafting": {
        "title": "Laser & Crafting",
        "handle": "laser-crafting",
        "path": ["Laser & Crafting"],
    },
}

EU_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9,de;q=0.8,fr;q=0.7",
    "Cache-Control": "no-cache",
    "Cookie": "currency=EUR; country=DE; locale=en;",
}


# =====================================================================
# SAFE REDIRECT & REQUEST HANDLER
# =====================================================================


class EURedirectHandler(urllib.request.HTTPRedirectHandler):
    """Enforce strict EU domain constraint on all HTTP redirects."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Inspect redirect destination and strictly require the EU domain."""
        parsed = urllib.parse.urlparse(newurl)
        netloc = parsed.netloc.lower()

        # Strict positive constraint: netloc MUST be eu.store.bambulab.com
        if netloc and netloc != TARGET_EU_DOMAIN:
            error_msg = (
                f"Strict EU domain violation: blocked non-EU redirect to '{newurl}' "
                f"(domain: '{netloc}')."
            )
            logger.error(error_msg)
            raise PermissionError(error_msg)

        logger.info("Following safe EU redirect (Code %d) to: %s", code, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(EURedirectHandler())


def fetch_url(url: str, delay_s: float = 1.0, max_retries: int = 3) -> str:
    """Fetch EU HTML content with rate limiting, GeoIP bypass, and retries."""
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc and parsed.netloc.lower() != TARGET_EU_DOMAIN:
        raise ValueError(
            f"Strict EU domain violation: Attempted request to non-EU domain '{parsed.netloc}'."
        )

    # Ensure Skip Redirect (skr=yes) parameter is attached to bypass non-EU IP redirects
    if "skr=yes" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}skr=yes"

    time.sleep(delay_s)
    logger.info("Fetching [EU]: %s", url)

    req = urllib.request.Request(url, headers=EU_HEADERS)
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            with _opener.open(req, timeout=20.0) as resp:
                content: str = resp.read().decode("utf-8", errors="ignore")
                return content
        except PermissionError:
            raise
        except Exception as e:
            last_error = e
            logger.warning("Attempt %d failed for %s: %s", attempt, url, e)
            if attempt < max_retries:
                time.sleep(2.0 * attempt)

    raise RuntimeError(f"Failed to fetch {url} after {max_retries} attempts: {last_error}")


# =====================================================================
# FILTERING & PARSING HELPERS
# =====================================================================


def is_refurbished(title: str, handle: str) -> bool:
    """Filter out refurbished, renewed, or recertified items."""
    keywords = ["refurbished", "renewed", "recertified", "b-stock", "open-box"]
    text = f"{title} {handle}".lower()
    return any(kw in text for kw in keywords)


def extract_collection_products(html: str) -> list[dict[str, Any]]:
    """Extract product summary entries from a category collection page."""
    idx = html.find('productList":')
    if idx == -1:
        idx = html.find('productList\\":')
    if idx == -1:
        return []

    start = html.find("[", idx)
    if start == -1:
        return []

    count = 0
    in_str = False
    escape = False
    end = -1

    for i in range(start, len(html)):
        ch = html[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if not in_str:
            if ch == "[":
                count += 1
            elif ch == "]":
                count -= 1
                if count == 0:
                    end = i + 1
                    break

    if end == -1:
        return []

    raw_arr = html[start:end]
    try:
        clean_arr = raw_arr.encode("utf-8").decode("unicode_escape")
        return cast(list[dict[str, Any]], json.loads(clean_arr))
    except Exception:
        clean_arr = raw_arr.replace('\\"', '"').replace("\\\\", "\\")
        return cast(list[dict[str, Any]], json.loads(clean_arr))


def extract_schema_ld(soup: BeautifulSoup) -> dict[str, Any] | None:
    """Extract Schema.org ProductGroup JSON-LD structured data."""
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
            if data.get("@type") == "ProductGroup":
                return cast(dict[str, Any], data)
            if isinstance(data, list):
                for item in data:
                    if item.get("@type") == "ProductGroup":
                        return cast(dict[str, Any], item)
        except Exception:
            continue
    return None


def extract_spec_table_dict(soup: BeautifulSoup) -> dict[str, str]:
    """Extract technical parameters and hardware comparison from the HTML table."""
    specs: dict[str, str] = {}
    table = soup.find("table")
    if not table:
        return specs

    for row in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
        if len(cells) >= 2:
            key = cells[0]
            val = cells[1]
            if len(cells) > 2 and cells[1] and cells[2]:
                val = f"{cells[1]} ({cells[2]})"
            clean_key = re.sub(r"[^\w\s-]", "", key).strip().lower().replace(" ", "_")
            if clean_key and val:
                specs[clean_key] = val
    return specs


def extract_media_gallery(html: str) -> list[str]:
    """Extract high-resolution media gallery URLs from Next.js RSC components."""
    media_list: list[str] = []
    seen_urls: set[str] = set()

    matches = re.findall(
        r'https://store\.bblcdn\.eu/s[0-9]+/default/[a-zA-Z0-9]+/[^"\'\s]+\.(?:jpg|jpeg|png|webp)',
        html,
        re.I,
    )
    for url in matches:
        clean_url = url.split("__op__")[0].split("?")[0]
        if clean_url not in seen_urls and not clean_url.endswith("logo.png"):
            seen_urls.add(clean_url)
            media_list.append(clean_url)
    return media_list


def parse_variant_attributes(title: str) -> dict[str, str]:
    """Extract attributes (color, material, spool_type) from variant title."""
    attrs: dict[str, str] = {}
    lower = title.lower()

    if "with spool" in lower or "filament with spool" in lower:
        attrs["spool_type"] = "Spool"
    elif "refill" in lower:
        attrs["spool_type"] = "Refill"

    for mat in ["pla", "petg", "abs", "asa", "tpu", "pc", "pa", "carbon fiber"]:
        if mat in lower:
            attrs["material"] = mat.upper()
            break

    color_match = re.search(r"-\s*([A-Za-z\s]+?)(?:\s*\(|\s*/|$)", title)
    if color_match:
        attrs["color"] = color_match.group(1).strip()

    return attrs


# =====================================================================
# MAIN SCRAPING & DEEP PDP EXTRACTION
# =====================================================================


def scrape_product_detail(
    handle: str,
    category_key: str,
    summary_item: dict[str, Any] | None = None,
    delay_s: float = 1.0,
) -> dict[str, Any] | None:
    """Scrape full single-page depth for a product URL."""
    url = f"{BASE_URL}/en/products/{handle}?skr=yes"
    html = fetch_url(url, delay_s=delay_s)
    soup = BeautifulSoup(html, "lxml")

    ld_data = extract_schema_ld(soup)

    if ld_data and ld_data.get("name"):
        title = str(ld_data["name"])
    elif summary_item and summary_item.get("name"):
        title = str(summary_item["name"])
    else:
        title = handle

    if is_refurbished(title, handle):
        logger.info("Skipping refurbished product: %s (%s)", title, handle)
        return None

    specs_dict = extract_spec_table_dict(soup)
    media_gallery = extract_media_gallery(html)

    cat_config = CATEGORY_CONFIGS.get(category_key, {})
    category_name = cat_config.get("title", category_key.title())
    category_path = cat_config.get("path", [category_name])

    variants: list[dict[str, Any]] = []
    if ld_data and "hasVariant" in ld_data:
        for v in ld_data.get("hasVariant", []):
            sku = str(v.get("sku", ""))
            v_title = str(v.get("name", title))
            offers = v.get("offers", {})

            price_raw = offers.get("price", 0)
            price = round(float(price_raw), 2) if price_raw else 0.0

            compare_at_price = None
            price_spec = offers.get("priceSpecification")
            if isinstance(price_spec, dict) and price_spec.get("price"):
                compare_at_price = round(float(price_spec["price"]), 2)

            in_stock = "InStock" in str(offers.get("availability", ""))
            v_attrs = parse_variant_attributes(v_title)

            variants.append(
                {
                    "variant_id": sku or f"{handle}-variant-{len(variants)+1}",
                    "sku": sku,
                    "title": v_title,
                    "price": price,
                    "compare_at_price": compare_at_price,
                    "in_stock": in_stock,
                    "attributes": v_attrs,
                }
            )

    if not variants and summary_item:
        price_val = summary_item.get("lowerPrice") or summary_item.get("price")
        price = round(float(price_val), 2) if price_val else 0.0
        v_id = str(summary_item.get("id", ""))
        variants.append(
            {
                "variant_id": v_id,
                "sku": str(summary_item.get("lowerPriceSkuId") or v_id),
                "title": title,
                "price": price,
                "compare_at_price": None,
                "in_stock": True,
                "attributes": parse_variant_attributes(title),
            }
        )

    product_id_raw = None
    if ld_data and ld_data.get("productGroupID"):
        product_id_raw = ld_data["productGroupID"]
    elif summary_item and summary_item.get("id"):
        product_id_raw = summary_item["id"]

    product_id = str(product_id_raw or handle)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    return {
        "product_id": product_id,
        "title": title,
        "slug": handle,
        "category": category_name,
        "category_path": category_path,
        "category_key": category_key,
        "store_region": "EU",
        "currency": "EUR",
        "url": f"{BASE_URL}/en/products/{handle}",
        "description": ld_data.get("description") if ld_data else None,
        "specifications": specs_dict,
        "variants": variants,
        "images": media_gallery,
        "updated_at": now_iso,
    }


def scrape_category(
    category_key: str,
    max_pages: int = 5,
    limit_per_category: int | None = None,
    delay_s: float = 1.0,
    persist_db: bool = True,
) -> list[dict[str, Any]]:
    """Scrape all products belonging to a category with multi-page collection pagination."""
    config = CATEGORY_CONFIGS.get(category_key)
    if not config:
        raise ValueError(
            f"Unknown category '{category_key}'. Available: {list(CATEGORY_CONFIGS.keys())}"
        )

    handle = config["handle"]
    logger.info("=== Scraping Category '%s' (handle: %s) ===", category_key, handle)

    all_summary_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for page in range(1, max_pages + 1):
        page_param = f"page={page}&" if page > 1 else ""
        collection_url = f"{BASE_URL}/en/collections/{handle}?{page_param}skr=yes"

        try:
            html = fetch_url(collection_url, delay_s=delay_s)
        except Exception as e:
            logger.warning("Could not fetch page %d for %s: %s", page, handle, e)
            break

        items = extract_collection_products(html)
        if not items:
            logger.info("No more products found on page %d for '%s'", page, handle)
            break

        new_items_count = 0
        for item in items:
            item_id = str(item.get("id", ""))
            if item_id and item_id not in seen_ids:
                seen_ids.add(item_id)
                all_summary_items.append(item)
                new_items_count += 1

        logger.info(
            "Page %d for '%s': added %d new items (total: %d)",
            page,
            handle,
            new_items_count,
            len(all_summary_items),
        )
        if new_items_count == 0:
            break

        if limit_per_category and len(all_summary_items) >= limit_per_category:
            all_summary_items = all_summary_items[:limit_per_category]
            break

    logger.info(
        "Collection '%s' complete: %d products discovered",
        category_key,
        len(all_summary_items),
    )

    detailed_products: list[dict[str, Any]] = []
    for idx, item in enumerate(all_summary_items, start=1):
        seo_code = item.get("seoCode")
        if not seo_code:
            continue

        logger.info(
            "[%d/%d] Scraping details for '%s' (%s)...",
            idx,
            len(all_summary_items),
            seo_code,
            category_key,
        )
        try:
            prod = scrape_product_detail(
                handle=seo_code,
                category_key=category_key,
                summary_item=item,
                delay_s=delay_s,
            )
            if prod:
                detailed_products.append(prod)
                if persist_db:
                    upsert_product(prod)
        except Exception as e:
            logger.error("Error scraping product '%s': %s", seo_code, e)

    if persist_db:
        sync_category_counts()

    return detailed_products


def scrape_all(
    max_pages: int = 5,
    limit_per_category: int | None = None,
    delay_s: float = 1.0,
    persist_db: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Scrape the full catalog across all major categories."""
    init_db()
    results: dict[str, list[dict[str, Any]]] = {}
    for cat in CATEGORY_CONFIGS:
        results[cat] = scrape_category(
            category_key=cat,
            max_pages=max_pages,
            limit_per_category=limit_per_category,
            delay_s=delay_s,
            persist_db=persist_db,
        )
    return results


# =====================================================================
# CLI RUNNER & DATA EXPORT
# =====================================================================


def main() -> None:
    """CLI entry point for running the EU catalog scraper."""
    parser = argparse.ArgumentParser(
        description="Bambu Lab EU Catalog Scraper - All Major Categories & Deep PDP."
    )
    parser.add_argument(
        "--category",
        choices=list(CATEGORY_CONFIGS.keys()) + ["all"],
        default="printers",
        help="Category to scrape (default: printers)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of products to scrape per category",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=5,
        help="Max collection pages to paginate (default: 5)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay in seconds between requests (default: 1.0)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="Output JSON file path (prints to stdout if omitted)",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Do not persist to SQLite database",
    )

    args = parser.parse_args()
    persist = not args.no_db

    data: dict[str, Any] | list[dict[str, Any]]
    if args.category == "all":
        data = scrape_all(
            max_pages=args.pages,
            limit_per_category=args.limit,
            delay_s=args.delay,
            persist_db=persist,
        )
    else:
        init_db()
        data = scrape_category(
            category_key=args.category,
            max_pages=args.pages,
            limit_per_category=args.limit,
            delay_s=args.delay,
            persist_db=persist,
        )

    json_str = json.dumps(data, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(json_str)
        logger.info("Successfully exported data to '%s'", args.output)
    else:
        print(json_str)


if __name__ == "__main__":
    main()
