# agent factory - builds the agno agent with tools
import os
from pathlib import Path
import threading
from decouple import config
from agno.agent import Agent
from agno.models.groq import Groq
from agno.models.google import Gemini
from agno.models.openrouter import OpenRouter
from agno.db.sqlite import SqliteDb

MEM_DIR = Path(__file__).resolve().parent.parent / "mem"
MEM_DIR.mkdir(exist_ok=True)

agent_db = SqliteDb(db_file=str(MEM_DIR / "db.sqlite3"))

_db_init_lock = threading.Lock()


def _init_agent_db() -> None:
    with _db_init_lock:
        agent_db._get_table(table_type="versions", create_table_if_not_found=True)
        agent_db._get_table(table_type="sessions", create_table_if_not_found=True)
        agent_db._get_table(table_type="memories", create_table_if_not_found=True)


_init_agent_db()
from infra.profile import (
    get_profile,
    get_flows,
    build_identity_instructions,
    build_base_guard_instructions,
    build_responsibilities_instructions,
    build_tone_instructions,
    build_flow_instructions,
    build_format_guidelines,
    build_escalation_guidelines,
    build_privacy_instructions,
    build_restrictions_instructions,
    build_introduction,
    build_expected_output,
)
from tools.business_info import (
    get_business_locations,
    get_business_location_by_name,
    get_primary_business_location,
    get_faqs,
    get_products,
    get_product_details,
)
from tools.datetime_tools import get_current_day
from tools.getSource import get_sources


def build_agent_prompt(*, profile: dict, agent_id: int | None = None) -> dict:
    flow = get_flows(agent_id=agent_id)
    instructions: list[str] = []
    instructions.extend(build_base_guard_instructions())
    instructions.append("")
    instructions.extend(build_identity_instructions(profile))
    instructions.append("")
    resp = build_responsibilities_instructions(profile)
    if resp:
        instructions.extend(resp)
        instructions.append("")
    tone = build_tone_instructions(flow)
    if tone:
        instructions.extend(tone)
    instructions.extend(build_flow_instructions(agent_id=agent_id, flow=flow))
    instructions.append("")
    fmt = build_format_guidelines(flow)
    if fmt:
        instructions.extend(fmt)
    esc = build_escalation_guidelines(flow)
    if esc:
        instructions.extend(esc)
    instructions.append("## Available Tools")
    instructions.append("Use these tools to look up business data when the user asks:")
    instructions.append("- get_business_locations / get_primary_business_location - for location, address, working hours, and holidays. operating_hours: {0=Mon..6=Sun, each: open/close HH:MM, is_closed bool}. special_holidays: multiline 'DD-MM-YYYY - Name'. For hours/availability: call get_current_day() for today, map day to int, check is_closed first, then compare time in 24hr (8pm=20:00). Always answer definitively with actual times.")
    instructions.append("- get_faqs - for frequently asked questions")
    instructions.append("- get_products / get_product_details - for product catalog and pricing")
    instructions.append("- get_current_day - for getting the current day of the week")
    instructions.append("- get_sources - for getting available lead source IDs and names")
    instructions.append("")
    privacy = build_privacy_instructions(flow)
    if privacy:
        instructions.extend(privacy)
    restrictions = build_restrictions_instructions(flow)
    if restrictions:
        instructions.extend(restrictions)

    expected_output = build_expected_output(flow, agent_id=agent_id)
    return {
        'name': profile.get('business_name', 'Cronomind AI'),
        'instructions': instructions,
        'description': profile.get('business_description', ''),
        'introduction': build_introduction(agent_id=agent_id),
        'expected_output': expected_output,
    }


_model_provider: str = ''

def get_model_provider() -> str:
    """Return the active model provider name (e.g. 'google', 'openrouter', 'groq')."""
    return _model_provider


def supports_audio() -> bool:
    """Return True if the active model supports native audio input."""
    return _model_provider == 'google'


def get_model(model_name: str = None):
    global _model_provider
    model_name = model_name or config('AI_MODEL', default='openrouter')
    _model_provider = model_name
    
    if model_name == 'groq':
        return Groq(id=config('GROQ_MODEL_ID', default="meta-llama/llama-3.1-8b-instant"))
    elif model_name == 'google':
        return Gemini(id=config('GOOGLE_MODEL_ID', default="google/gemini-3-flash-preview"))
    elif model_name == 'openrouter':
        return OpenRouter(id=config('OPENROUTER_MODEL_ID', default="google/gemini-3.1-flash-lite-preview"))
    else:
        raise ValueError(f"Unknown model provider: {model_name}")


def create_agent(*, agent_id: int | None = None) -> Agent:
    profile = get_profile(agent_id=agent_id)
    if not profile:
        raise ValueError("No active AI profile found.")

    agent_config = build_agent_prompt(profile=profile, agent_id=agent_id)

    agent = Agent(
        name=agent_config['name'],
        model=get_model(),
        id=agent_config.get("id"),
        tools=[
            get_business_locations,
            get_business_location_by_name,
            get_primary_business_location,
            get_faqs,
            get_products,
            get_product_details,
            get_current_day,
            get_sources,
        ],
        db=agent_db,
        use_json_mode=False,
        enable_user_memories=True,
        enable_agentic_memory=True,
        add_memories_to_context=True,
        add_history_to_context=True,
        num_history_runs=15,
        max_tool_calls_from_history=5,
        cache_session=True,
        description=agent_config['description'],
        instructions=agent_config['instructions'],
        introduction=agent_config.get('introduction'),
        expected_output=agent_config.get('expected_output'),
        add_name_to_context=True,
        add_datetime_to_context=False,
        add_location_to_context=False,
        timezone_identifier="Asia/Kolkata",
        user_message_role="user",
        retries=1,
        delay_between_retries=1,
        debug_mode=False,
        debug_level=1,
        telemetry=False,
    )
    return agent