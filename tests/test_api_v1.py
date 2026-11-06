"""Unit and integration tests for API v1 endpoints and strict EU domain enforcement."""

import urllib.request

import pytest
from fastapi.testclient import TestClient

import database
from config import Settings
from main import app
from scraper import TARGET_EU_DOMAIN, EURedirectHandler


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path: pytest.TempPathFactory) -> None:
    """Initialize a clean temporary SQLite database with seeded fixtures for each test."""
    db_file = str(tmp_path / "test_catalog.sqlite3")  # type: ignore[operator]
    database.init_db(db_path=db_file)
    database.seed_initial_fixtures(db_path=db_file)


def test_list_products(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify GET /api/v1/products returns normalized EU products with pagination."""
    monkeypatch.setattr("main.settings", Settings(SCRAPER_API_KEY="test-key"))
    with TestClient(app) as client:
        response = client.get("/api/v1/products?limit=10")
        assert response.status_code == 200
        data = response.json()

        assert data["success"] is True
        assert isinstance(data["data"], list)
        assert len(data["data"]) >= 1

        first_prod = data["data"][0]
        assert "product_id" in first_prod
        assert "title" in first_prod
        assert "slug" in first_prod
        assert first_prod["store_region"] == "EU"
        assert first_prod["currency"] == "EUR"
        assert isinstance(first_prod["variants"], list)
        assert isinstance(first_prod["images"], list)

        pagination = data["pagination"]
        assert pagination["page"] == 1
        assert pagination["limit"] == 10
        assert pagination["total"] >= 1
        assert pagination["pages"] >= 1


def test_list_products_category_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify GET /api/v1/products filters by category."""
    monkeypatch.setattr("main.settings", Settings(SCRAPER_API_KEY="test-key"))
    with TestClient(app) as client:
        response = client.get("/api/v1/products?category=printers")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        for prod in data["data"]:
            assert "printer" in prod["category"].lower() or prod.get("category_key") == "printers"


def test_get_product_detail_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify GET /api/v1/products/:id_or_slug returns single product with full PDP depth."""
    monkeypatch.setattr("main.settings", Settings(SCRAPER_API_KEY="test-key"))
    with TestClient(app) as client:
        # Retrieve product list to get an existing slug
        list_resp = client.get("/api/v1/products")
        slug = list_resp.json()["data"][0]["slug"]

        response = client.get(f"/api/v1/products/{slug}")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

        prod = data["data"]
        assert prod["slug"] == slug
        assert prod["store_region"] == "EU"
        assert prod["currency"] == "EUR"
        assert isinstance(prod["specifications"], dict)
        assert isinstance(prod["variants"], list)
        assert len(prod["variants"]) >= 1

        variant = prod["variants"][0]
        assert isinstance(variant["price"], float)
        assert isinstance(variant["in_stock"], bool)


def test_get_product_detail_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify GET /api/v1/products/:id_or_slug returns 404 for unknown items."""
    monkeypatch.setattr("main.settings", Settings(SCRAPER_API_KEY="test-key"))
    with TestClient(app) as client:
        response = client.get("/api/v1/products/non-existent-product-handle-999")
        assert response.status_code == 404
        assert response.json()["detail"]["success"] is False


def test_list_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify GET /api/v1/categories returns category summaries with product counts."""
    monkeypatch.setattr("main.settings", Settings(SCRAPER_API_KEY="test-key"))
    with TestClient(app) as client:
        response = client.get("/api/v1/categories")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert isinstance(data["data"], list)
        assert len(data["data"]) >= 1
        cat = data["data"][0]
        assert "category_key" in cat
        assert "title" in cat
        assert "product_count" in cat
        assert cat["product_count"] >= 1


def test_strict_eu_redirect_handler_allowed() -> None:
    """Verify EURedirectHandler permits safe redirects on eu.store.bambulab.com."""
    handler = EURedirectHandler()
    dummy_req = urllib.request.Request("https://eu.store.bambulab.com/en/collections/3d-printer")
    result = handler.redirect_request(
        req=dummy_req,
        fp=None,
        code=302,
        msg="Found",
        headers={},
        newurl=f"https://{TARGET_EU_DOMAIN}/collections/3d-printer?skr=yes",
    )
    assert result is not None
    assert result.full_url == f"https://{TARGET_EU_DOMAIN}/collections/3d-printer?skr=yes"


def test_strict_eu_redirect_handler_blocked() -> None:
    """Verify EURedirectHandler strictly blocks redirects to US or non-EU domains."""
    handler = EURedirectHandler()
    dummy_req = urllib.request.Request("https://eu.store.bambulab.com/en/collections/3d-printer")

    # US domain hop attempt must raise PermissionError
    with pytest.raises(PermissionError, match="Strict EU domain violation"):
        handler.redirect_request(
            req=dummy_req,
            fp=None,
            code=302,
            msg="Found",
            headers={},
            newurl="https://us.store.bambulab.com/collections/3d-printer",
        )

    # Canadian / Global domain hop attempt must also be blocked
    with pytest.raises(PermissionError, match="Strict EU domain violation"):
        handler.redirect_request(
            req=dummy_req,
            fp=None,
            code=302,
            msg="Found",
            headers={},
            newurl="https://ca.store.bambulab.com/collections/3d-printer",
        )
