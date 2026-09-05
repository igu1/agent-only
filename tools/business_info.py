# business info tools - agent uses these to look up stuff on the fly
from agno.tools import tool
from tools.getAi import (
    get_active_locations,
    get_active_faqs,
    get_active_products,
    get_product_variants,
    get_fee_chart,
    get_inquiry_status_rows,
)
from tools.getSource import get_sources as _get_sources


def build_business_tools(*, agent_id: int | None = None) -> list:
    """The lookup tools, BOUND to the agent that will use them.

    Every school has its OWN agent carrying its own FAQs, campuses, products
    and lead sources; agent 0 is the company agent, whose rows are the pooled
    union across the group. These were module-level functions that passed no
    agent at all, and the backend answers an agent-less request company-wide -
    so a chat about one school quoted the whole group's FAQ list, and campus
    questions returned NOTHING, because only the per-school agents carry
    locations.

    Binding the id when the agent is constructed keeps every lookup inside the
    school the conversation is about, with no per-call context to propagate:
    agents are already cached per (org, agent_id), so each one owns tools that
    can only see its own rows.

    Docstrings are the descriptions the model reads - keep them intact.
    """

    @tool
    def get_business_locations() -> list[dict]:
        """Get all active business locations with address, phone, email details."""
        return get_active_locations(agent_id=agent_id)

    @tool
    def get_business_location_by_name(location_name: str) -> dict | None:
        """Get a specific business location by name."""
        for loc in get_active_locations(agent_id=agent_id):
            if loc.get('name', '').lower() == location_name.lower():
                return loc
        return None

    @tool
    def get_primary_business_location() -> dict | None:
        """Get the primary business location."""
        locations = get_active_locations(agent_id=agent_id)
        for loc in locations:
            if loc.get('is_primary', False):
                return loc
        return locations[0] if locations else None

    @tool
    def get_faqs() -> list[dict]:
        """Get frequently asked questions with answers for the business."""
        return get_active_faqs(agent_id=agent_id)

    @tool
    def get_products() -> list[dict]:
        """Get all active products with name, description, price, and discount info."""
        return get_active_products(agent_id=agent_id)

    @tool
    def get_product_details(product_id: int) -> dict:
        """Get product variants/options for a specific product by its ID."""
        variants = get_product_variants(product_id)
        return {"product_id": product_id, "variants": variants}

    @tool
    def get_sources() -> list[dict]:
        """Get the available lead source IDs and names."""
        return _get_sources(agent_id=agent_id)

    # keyed by class / phone rather than by agent - nothing to bind
    @tool
    def get_fee_details(class_id: int) -> list[dict]:
        """Get the official fee chart for a class by its class ID: fee receipt
        groups, installments, fee items, and net amounts. Use for any question
        about fees, tuition, or costs for a specific class."""
        return get_fee_chart(class_id)

    @tool
    def get_inquiry_status(phone_number: str) -> list[dict]:
        """Look up the LIVE admission inquiry status registered against a mobile
        number. Returns one row per inquiry: student_name, class_name,
        institution_name, status_name, inquiry_date, next_followup_date. Use
        whenever the parent or student asks about the status or progress of their
        inquiry, application, or admission. Pass the mobile number the inquiry was
        made with (any format - matching is on the trailing digits)."""
        return get_inquiry_status_rows(phone_number)

    return [
        get_business_locations,
        get_business_location_by_name,
        get_primary_business_location,
        get_faqs,
        get_products,
        get_product_details,
        get_fee_details,
        get_inquiry_status,
        get_sources,
    ]
