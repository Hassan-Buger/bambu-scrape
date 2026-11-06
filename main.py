"""FastAPI application entry point and HTTP service for EU catalog data."""

import asyncio
import logging
import math
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, status

import database
from config import settings
from models import (
    ERROR_UNAUTHORIZED,
    CategoryItem,
    CategoryListResponse,
    PaginationMeta,
    ProductDetailResponse,
    ProductListResponse,
    ProductSchema,
)

logger = logging.getLogger(__name__)


def _background_scrape_worker(category: str) -> None:
    """Execute catalog scrape in a background worker thread."""
    from scraper import CATEGORY_CONFIGS, scrape_all, scrape_category

    logger.info("Starting background scrape worker for category '%s'", category)
    try:
        if category == "all":
            scrape_all(max_pages=5, delay_s=1.0, persist_db=True)
        elif category in CATEGORY_CONFIGS:
            scrape_category(category_key=category, max_pages=5, delay_s=1.0, persist_db=True)
        else:
            logger.error("Unknown category for background scrape: %s", category)
    except Exception as e:
        logger.error("Background scrape error: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Validate server prerequisites on startup and initialize database."""
    if not settings.SCRAPER_API_KEY:
        error_msg = "SCRAPER_API_KEY environment variable must be set to start server."
        logger.error(error_msg)
        raise RuntimeError(error_msg)

    # Initialize SQLite schema
    database.init_db()

    # Seed initial verified fixtures if database has no records
    _, total = database.get_products(limit=1)
    if total == 0:
        seeded = database.seed_initial_fixtures()
        logger.info("Database initialized and seeded with %d verified EU fixtures.", seeded)
    else:
        logger.info("Database initialized with %d existing products.", total)

    yield
    logger.info("Service shutting down.")


app = FastAPI(
    title="Bambu Lab EU Catalog Service",
    description="Standardized REST API for Bambu Lab EU Storefront Catalog & Specifications",
    version="1.0.0",
    lifespan=lifespan,
)


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> str:
    """Validate X-API-Key header against configured SCRAPER_API_KEY."""
    if not settings.SCRAPER_API_KEY or not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "data": None,
                "meta": {},
                "error": {
                    "code": ERROR_UNAUTHORIZED,
                    "message": "Missing or invalid X-API-Key header",
                },
            },
        )
    if not secrets.compare_digest(x_api_key, settings.SCRAPER_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "data": None,
                "meta": {},
                "error": {
                    "code": ERROR_UNAUTHORIZED,
                    "message": "Missing or invalid X-API-Key header",
                },
            },
        )
    return x_api_key


# =====================================================================
# PUBLIC & STANDARDIZED API V1 ENDPOINTS
# =====================================================================


@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check() -> dict[str, str]:
    """Health check endpoint open to unauthenticated callers."""
    return {"status": "ok"}


@app.get(
    "/api/v1/products",
    response_model=ProductListResponse,
    status_code=status.HTTP_200_OK,
)
def list_products(
    category: str | None = Query(
        default=None,
        description="Filter by category slug (e.g. 'printers', 'filaments', 'ams', 'accessories')",
    ),
    in_stock: bool | None = Query(
        default=None,
        description="Filter by stock availability (true for in stock only)",
    ),
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),
    limit: int = Query(default=50, ge=1, le=100, description="Items per page"),
) -> ProductListResponse:
    """Retrieve paginated list of catalog products with optional category and stock filtering."""
    products_raw, total = database.get_products(
        category_key=category,
        in_stock=in_stock,
        page=page,
        limit=limit,
    )
    pages = math.ceil(total / limit) if limit > 0 and total > 0 else 1

    return ProductListResponse(
        success=True,
        data=[ProductSchema.model_validate(p) for p in products_raw],
        pagination=PaginationMeta(
            page=page,
            limit=limit,
            total=total,
            pages=pages,
        ),
    )


@app.get(
    "/api/v1/products/{id_or_slug}",
    response_model=ProductDetailResponse,
    status_code=status.HTTP_200_OK,
)
def get_product_detail(id_or_slug: str) -> ProductDetailResponse:
    """Retrieve full granular details for a single product by numeric product_id or slug handle."""
    product_raw = database.get_product(id_or_slug)
    if not product_raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "success": False,
                "data": None,
                "error": f"Product with id or slug '{id_or_slug}' not found in EU catalog.",
            },
        )
    return ProductDetailResponse(
        success=True,
        data=ProductSchema.model_validate(product_raw),
        error=None,
    )


@app.get(
    "/api/v1/categories",
    response_model=CategoryListResponse,
    status_code=status.HTTP_200_OK,
)
def list_categories() -> CategoryListResponse:
    """Retrieve all available scraped categories with item counts."""
    categories_raw = database.get_categories()
    return CategoryListResponse(
        success=True,
        data=[CategoryItem.model_validate(c) for c in categories_raw],
    )


@app.post(
    "/api/v1/scrape",
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_scrape(
    background_tasks: BackgroundTasks,
    category: str = Query(
        default="all",
        description="Category to scrape ('printers', 'filaments', 'ams', 'accessories', 'all')",
    ),
    api_key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Trigger an asynchronous background catalog scrape (protected by X-API-Key)."""
    background_tasks.add_task(asyncio.to_thread, _background_scrape_worker, category)
    return {
        "success": True,
        "message": f"Scrape task for category '{category}' accepted and running in background.",
    }
