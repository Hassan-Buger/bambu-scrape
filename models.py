"""Core data models and domain schemas for catalog synchronization and APIs."""

import re
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Canonical machine-readable error codes referenced by API surface
ERROR_INVALID_CATEGORY = "invalid_category"
ERROR_UNAUTHORIZED = "unauthorized"
ERROR_RUN_CONFLICT = "run_conflict"
ERROR_RUN_NOT_FOUND = "run_not_found"
ERROR_INTERNAL = "internal_error"

_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


class ChangeFlag(str, Enum):
    """Event flags emitted by the catalog diff engine."""

    IS_NEW_PRODUCT = "IS_NEW_PRODUCT"
    IS_NEW_VARIANT = "IS_NEW_VARIANT"
    IS_NEW_CATEGORY = "IS_NEW_CATEGORY"
    PRICE_CHANGED = "PRICE_CHANGED"
    AVAILABILITY_CHANGED = "AVAILABILITY_CHANGED"


class Money(BaseModel):
    """Monetary amount represented in integer minor units with a 3-letter currency code."""

    model_config = ConfigDict(extra="ignore")

    amount_minor: int
    currency: str

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        """Validate currency is exactly three uppercase ASCII letters."""
        if not _CURRENCY_PATTERN.match(value):
            raise ValueError(f"Currency must be exactly three uppercase letters, got {value!r}")
        return value

    @classmethod
    def from_decimal_str(cls, price_str: str, currency: str) -> "Money":
        """Parse decimal string price into integer minor units without float precision loss."""
        d = Decimal(price_str)
        minor = int((d * 100).quantize(Decimal("1")))
        return cls(amount_minor=minor, currency=currency)


class Product(BaseModel):
    """Product stub containing identity fields."""

    model_config = ConfigDict(extra="ignore")

    product_id: int


class Variant(BaseModel):
    """Variant stub containing identity fields."""

    model_config = ConfigDict(extra="ignore")

    variant_id: int


# =====================================================================
# STANDARDIZED EU CATALOG API SCHEMAS (V1)
# =====================================================================


class VariantSchema(BaseModel):
    """Normalized product variant entity."""

    model_config = ConfigDict(extra="ignore")

    variant_id: str
    sku: str
    title: str
    price: float
    compare_at_price: float | None = None
    in_stock: bool = True
    attributes: dict[str, str] = Field(default_factory=dict)


class ProductSchema(BaseModel):
    """Normalized full product detail entity."""

    model_config = ConfigDict(extra="ignore")

    product_id: str
    title: str
    slug: str
    category: str
    category_path: list[str] = Field(default_factory=list)
    store_region: str = "EU"
    currency: str = "EUR"
    url: str
    description: str | None = None
    specifications: dict[str, str] = Field(default_factory=dict)
    variants: list[VariantSchema] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    updated_at: str


class PaginationMeta(BaseModel):
    """Pagination metadata for collection listings."""

    model_config = ConfigDict(extra="ignore")

    page: int
    limit: int
    total: int
    pages: int


class ProductListResponse(BaseModel):
    """Standardized response envelope for product lists."""

    model_config = ConfigDict(extra="ignore")

    success: bool = True
    data: list[ProductSchema]
    pagination: PaginationMeta


class ProductDetailResponse(BaseModel):
    """Standardized response envelope for single product details."""

    model_config = ConfigDict(extra="ignore")

    success: bool = True
    data: ProductSchema | None = None
    error: str | None = None


class CategoryItem(BaseModel):
    """Catalog category overview item."""

    model_config = ConfigDict(extra="ignore")

    category_key: str
    title: str
    handle: str
    product_count: int


class CategoryListResponse(BaseModel):
    """Standardized response envelope for categories list."""

    model_config = ConfigDict(extra="ignore")

    success: bool = True
    data: list[CategoryItem]
