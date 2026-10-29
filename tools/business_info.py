# business info tools - agent uses these to look up stuff on the fly
import threading

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
    def get_fee_details(class_id: int) -> dict:
        """Get the official fee chart for a class by its class ID: fee receipt
        groups, installments, fee items, and net amounts. Use for any question
        about fees, tuition, or costs for a specific class.

        Returns {"rows": [...]} when a chart exists. When the school has not
        configured one, returns {"rows": [], "no_fee_chart": true, "tell_parent":
        ...} - say that, and quote NO figure.
        """
        rows = get_fee_chart(class_id)
        if rows:
            return {"rows": rows}
        # An empty list is silence, and the model filled it: asked for Grade 3
        # fees at a school with NO fee rows at all, it answered "the first
        # installment of 3,500 SAR is due at admission". Nobody set that number.
        # A parent can act on a figure like that, so the tool says in words what
        # an empty list left open.
        return {
            "rows": [],
            "no_fee_chart": True,
            "tell_parent": (
                "This school has not published a fee chart for this class, so "
                "you do NOT have its fees. Say plainly that you do not have the "
                "figures for that class and that the admissions team will "
                "confirm them. Do NOT state, estimate or illustrate ANY amount, "
                "instalment count or fee structure - there is nothing to base "
                "one on. Then carry on with the inquiry."
            ),
        }

    @tool
    def get_inquiry_status(phone_number: str, email: str | None = None) -> list[dict] | dict:
        """Look up the LIVE admission inquiry status, for the school's CURRENT
        ADMISSION YEAR only. Returns one row per student: student_name,
        class_name, institution_name, status_name, inquiry_date,
        next_followup_date. Use whenever the parent or student asks about the
        status or progress of their inquiry, application, or admission.
        Pass the mobile number AND the email address the inquiry was made with
        (on WhatsApp the email is not needed). A row may also carry next_step
        (and next_step_ar / status_name_ar): the school's own next-step line for
        that status - give it to the parent as written, after the status."""
        return _inquiry_status_lookup(phone_number, email)

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


# ── inquiry status: who may see it ───────────────────────────────────────
# The CRM makes the decision (WhatsApp = the sender's own number; elsewhere the
# mobile AND email must sit on the same inquiry). This side only asks for what
# the CRM will need and stops a chat that keeps guessing.

STATUS_LOOKUP_MAX_FAILURES = 3

# (org, lead_id) -> failed lookups in that conversation. In-process on purpose:
# it only has to outlast one chat's guessing, and a restart costing a guesser
# three more tries is not a weakness worth a database column.
_status_failures: dict[tuple[str, int], int] = {}
_status_failures_lock = threading.Lock()


def _failure_key() -> tuple[str, int] | None:
    from infra.tenants import get_org
    from tools.turn_context import current_turn
    lead_id = current_turn().lead_id
    return (get_org(), int(lead_id)) if lead_id else None


def _inquiry_status_lookup(phone_number: str, email: str | None) -> list[dict] | dict:
    from tools.turn_context import current_turn
    turn = current_turn()
    on_whatsapp = turn.channel_type == 'whatsapp'
    email = (email or '').strip() or None

    if not on_whatsapp and not email:
        # nothing to check yet - no CRM call, and not counted as a failure
        return {
            "rows": [],
            "verification_needed": True,
            "tell_parent": (
                "To protect student information, ask for the mobile number AND the "
                "email address used on the inquiry, then call this again with both. "
                "Do not say whether any inquiry exists."
            ),
        }

    key = _failure_key()
    with _status_failures_lock:
        failures = _status_failures.get(key, 0) if key else 0
    if failures >= STATUS_LOOKUP_MAX_FAILURES:
        return {
            "rows": [],
            "locked": True,
            "tell_parent": (
                "Status cannot be checked in this chat any more. Ask the parent to "
                "contact the admissions office to check their inquiry status. Do not "
                "try another lookup."
            ),
        }

    rows = get_inquiry_status_rows(phone_number, email=email, lead_id=turn.lead_id)
    if rows:
        if key:
            with _status_failures_lock:
                _status_failures.pop(key, None)
        return rows

    if key and not on_whatsapp:
        with _status_failures_lock:
            _status_failures[key] = _status_failures.get(key, 0) + 1
    # ONE neutral answer for every miss - wrong number, wrong email, no email on
    # file, or no current-year inquiry - so the lookup cannot be used to test
    # whether a number or an address is registered.
    return {
        "rows": [],
        "tell_parent": (
            "No inquiry was found for these details in the CURRENT admission year. "
            "Say exactly that, without saying which detail did not match and "
            "without saying the family is unknown to the school. Suggest checking "
            "the mobile number and email used on the inquiry, and offer to start a "
            "new inquiry or to have the admissions office help."
            if not on_whatsapp else
            "No inquiry was found for this WhatsApp number in the CURRENT admission "
            "year. Say exactly that - do not say the family is unknown to the "
            "school. Offer to start a new inquiry, or to have the admissions office "
            "help with an earlier admission."
        ),
    }
