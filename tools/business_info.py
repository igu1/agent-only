# business info tools - agent uses these to look up stuff on the fly
from agno.tools import tool
from tools.getAi import get_active_locations, get_active_faqs, get_active_products, get_product_variants


@tool
def get_business_locations() -> list[dict]:
    """Get all active business locations with address, phone, email details."""
    return get_active_locations()


@tool
def get_business_location_by_name(location_name: str) -> dict | None:
    """Get a specific business location by name."""
    for loc in get_active_locations():
        if loc.get('name', '').lower() == location_name.lower():
            return loc
    return None


@tool
def get_primary_business_location() -> dict | None:
    """Get the primary business location."""
    locations = get_active_locations()
    for loc in locations:
        if loc.get('is_primary', False):
            return loc
    return locations[0] if locations else None


@tool
def get_faqs() -> list[dict]:
    """Get frequently asked questions with answers for the business."""
    return get_active_faqs()


@tool
def get_products() -> list[dict]:
    """Get all active products with name, description, price, and discount info."""
    return get_active_products()


@tool
def get_product_details(product_id: int) -> dict:
    """Get product variants/options for a specific product by its ID."""
    variants = get_product_variants(product_id)
    return {"product_id": product_id, "variants": variants}
