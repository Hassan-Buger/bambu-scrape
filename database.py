"""SQLite database persistence and query interface for catalog data."""

import json
import os
import sqlite3
import time
from typing import Any

from config import settings

_DEFAULT_DB_FILE = "catalog.sqlite3"


def get_db_path() -> str:
    """Return the absolute path to the SQLite catalog database."""
    os.makedirs(settings.DATA_DIR, exist_ok=True)
    return os.path.join(settings.DATA_DIR, _DEFAULT_DB_FILE)


def init_db(db_path: str | None = None) -> None:
    """Initialize SQLite database tables and indexes."""
    target_path = db_path or get_db_path()
    with sqlite3.connect(target_path) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS products (
                product_id TEXT PRIMARY KEY,
                slug TEXT UNIQUE,
                title TEXT NOT NULL,
                category_key TEXT NOT NULL,
                category_title TEXT NOT NULL,
                in_stock INTEGER NOT NULL DEFAULT 1,
                min_price REAL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_products_category
            ON products(category_key);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_products_slug
            ON products(slug);
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                category_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                handle TEXT NOT NULL,
                product_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
        """)
        conn.commit()


def upsert_product(product: dict[str, Any], db_path: str | None = None) -> None:
    """Insert or update a product record in the database."""
    target_path = db_path or get_db_path()
    product_id = str(product.get("product_id", ""))
    slug = str(product.get("slug") or product.get("handle") or product_id)
    title = str(product.get("title", ""))
    category_key = str(product.get("category_key") or product.get("category", "")).lower()
    category_title = str(product.get("category") or product.get("category_title", category_key))

    variants = product.get("variants", [])
    in_stock = 1 if any(v.get("in_stock", v.get("available", False)) for v in variants) else 0

    prices = [v.get("price") for v in variants if v.get("price") is not None]
    min_price = min(prices) if prices else None

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if "updated_at" not in product or not product["updated_at"]:
        product["updated_at"] = now_iso

    payload_json = json.dumps(product, ensure_ascii=False)

    with sqlite3.connect(target_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO products (
                product_id, slug, title, category_key, category_title,
                in_stock, min_price, payload_json, updated_at, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(product_id) DO UPDATE SET
                slug = excluded.slug,
                title = excluded.title,
                category_key = excluded.category_key,
                category_title = excluded.category_title,
                in_stock = excluded.in_stock,
                min_price = excluded.min_price,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at,
                is_active = 1;
        """,
            (
                product_id,
                slug,
                title,
                category_key,
                category_title,
                in_stock,
                min_price,
                payload_json,
                now_iso,
            ),
        )
        conn.commit()


def get_products(
    category_key: str | None = None,
    in_stock: bool | None = None,
    page: int = 1,
    limit: int = 50,
    db_path: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Query products with filtering and pagination."""
    target_path = db_path or get_db_path()
    offset = max(0, (page - 1) * limit)

    where_clauses = ["is_active = 1"]
    params: list[Any] = []

    if category_key:
        where_clauses.append("category_key = ?")
        params.append(category_key.lower())

    if in_stock is not None:
        where_clauses.append("in_stock = ?")
        params.append(1 if in_stock else 0)

    where_sql = " AND ".join(where_clauses)

    with sqlite3.connect(target_path) as conn:
        cursor = conn.cursor()

        # Get total count
        cursor.execute(f"SELECT COUNT(*) FROM products WHERE {where_sql};", params)  # noqa: S608
        total = cursor.fetchone()[0]

        # Get page records
        query_sql = f"""
            SELECT payload_json FROM products
            WHERE {where_sql}
            ORDER BY product_id ASC
            LIMIT ? OFFSET ?;
        """  # noqa: S608
        cursor.execute(query_sql, params + [limit, offset])
        rows = cursor.fetchall()

        products = [json.loads(r[0]) for r in rows]
        return products, total


def get_product(id_or_slug: str, db_path: str | None = None) -> dict[str, Any] | None:
    """Retrieve a single product by numeric product_id or slug handle."""
    target_path = db_path or get_db_path()
    with sqlite3.connect(target_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT payload_json FROM products
            WHERE is_active = 1 AND (product_id = ? OR slug = ?)
            LIMIT 1;
        """,
            (id_or_slug, id_or_slug),
        )
        row = cursor.fetchone()
        if row:
            return json.loads(row[0])  # type: ignore[no-any-return]
    return None


def sync_category_counts(db_path: str | None = None) -> None:
    """Synchronize total item counts per category in the categories table."""
    target_path = db_path or get_db_path()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with sqlite3.connect(target_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT category_key, category_title, COUNT(*)
            FROM products
            WHERE is_active = 1
            GROUP BY category_key, category_title;
        """)
        rows = cursor.fetchall()
        for cat_key, cat_title, count in rows:
            cursor.execute(
                """
                INSERT INTO categories (category_key, title, handle, product_count, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(category_key) DO UPDATE SET
                    title = excluded.title,
                    product_count = excluded.product_count,
                    updated_at = excluded.updated_at;
            """,
                (cat_key, cat_title, cat_key, count, now_iso),
            )
        conn.commit()


def get_categories(db_path: str | None = None) -> list[dict[str, Any]]:
    """Retrieve all categories with their product counts."""
    target_path = db_path or get_db_path()
    with sqlite3.connect(target_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT category_key, title, handle, product_count, updated_at
            FROM categories
            ORDER BY title ASC;
        """)
        rows = cursor.fetchall()
        return [
            {
                "category_key": r[0],
                "title": r[1],
                "handle": r[2],
                "product_count": r[3],
                "updated_at": r[4],
            }
            for r in rows
        ]


def seed_initial_fixtures(db_path: str | None = None) -> int:
    """Seed verified offline test fixtures into the database if available."""
    target_path = db_path or get_db_path()
    base_dir = os.path.dirname(os.path.abspath(__file__))
    fixtures = [
        os.path.join(base_dir, "tests", "fixtures", "printers", "product_printer_detail.json"),
        os.path.join(base_dir, "tests", "fixtures", "filaments", "product_filament_detail.json"),
    ]

    seeded_count = 0
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for fixture_file in fixtures:
        if not os.path.exists(fixture_file):
            continue
        try:
            with open(fixture_file, encoding="utf-8") as f:
                raw = json.load(f)

            # Map raw fixture to normalized ProductSchema format
            p_id = str(raw.get("product_id", ""))
            slug = str(raw.get("handle", p_id))
            cat_key = str(raw.get("category_key", "printers"))
            cat_title = str(raw.get("category", cat_key.title()))

            # Parse spec table to dict
            spec_dict: dict[str, str] = {}
            raw_specs = raw.get("spec_table", [])
            if isinstance(raw_specs, list):
                for row in raw_specs:
                    if isinstance(row, list) and len(row) >= 2:
                        k = str(row[0]).strip().lower().replace(" ", "_")
                        v_str = str(row[1]).strip()
                        if len(row) > 2 and row[2]:
                            v_str = f"{v_str} ({row[2]})"
                        if k:
                            spec_dict[k] = v_str

            # Map variants
            variants_list: list[dict[str, Any]] = []
            raw_variants = raw.get("variants", [])
            if isinstance(raw_variants, list):
                for v in raw_variants:
                    if not isinstance(v, dict):
                        continue
                    v_id = str(v.get("variant_id", ""))
                    price_val = v.get("price_minor", 0)
                    price_float = round(float(price_val) / 100.0, 2)
                    compare_minor = v.get("compare_at_minor")
                    compare_float = (
                        round(float(compare_minor) / 100.0, 2) if compare_minor else None
                    )
                    variants_list.append(
                        {
                            "variant_id": v_id,
                            "sku": str(v.get("sku", v_id)),
                            "title": str(v.get("title", "")),
                            "price": price_float,
                            "compare_at_price": compare_float,
                            "in_stock": bool(v.get("available", True)),
                            "attributes": {},
                        }
                    )

            images_list: list[str] = []
            raw_media = raw.get("media", [])
            if isinstance(raw_media, list):
                for m in raw_media:
                    if isinstance(m, dict) and m.get("url"):
                        images_list.append(str(m["url"]))

            normalized_product = {
                "product_id": p_id,
                "title": str(raw.get("title", "")),
                "slug": slug,
                "category": cat_title,
                "category_path": [cat_title],
                "category_key": cat_key,
                "store_region": "EU",
                "currency": "EUR",
                "url": str(raw.get("url", f"https://eu.store.bambulab.com/en/products/{slug}")),
                "description": raw.get("description"),
                "specifications": spec_dict,
                "variants": variants_list,
                "images": images_list,
                "updated_at": now_iso,
            }

            upsert_product(normalized_product, db_path=target_path)
            seeded_count += 1
        except Exception:
            continue

    if seeded_count > 0:
        sync_category_counts(db_path=target_path)

    return seeded_count
