# msg processor - no more DB sessions, pure API + websocket
import anyio
from dataclasses import dataclass
import json
import re
from typing import Awaitable, Callable

from infra.django_api import post as django_post
from infra.ws_bridge import broadcast_to_conversation_async, broadcast_to_org_async
from api.routes.shared.agent_singleton import get_agent
from api.routes.shared.channel_handler import (
    save_incoming_message_and_charge,
    save_message,
    update_lead_ai_summary,
    update_lead_from_payload,
)


def _to_int(value):
    return int(value) if value is not None else None


@dataclass(frozen=True)
class LeadContext:
    lead_id: int
    org_id: int | None
    channel_agent_id: int


def _summary_to_text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    return getattr(value, 'summary', '') or ''


def _extract_last_json_object(text: str) -> dict | None:
    if not text:
        return None

    cleaned = text.strip()
    if cleaned.startswith('```'):
        cleaned = cleaned.strip('`')

    cleaned = cleaned.replace('```json', '').replace('```JSON', '').replace('```', '')

    decoder = json.JSONDecoder()
    best_obj: dict | None = None
    best_end = -1

    for i, ch in enumerate(cleaned):
        if ch != '{':
            continue
        try:
            obj, end = decoder.raw_decode(cleaned[i:])
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        absolute_end = i + int(end)
        if absolute_end >= best_end:
            best_end = absolute_end
            best_obj = obj

    if best_obj is not None:
        return best_obj

    blocks = re.findall(r'\{[\s\S]*\}', cleaned)
    for block in reversed(blocks):
        try:
            obj = json.loads(block)
        except Exception:
            continue
        if isinstance(obj, dict):
            return obj

    return None


async def _broadcast_escalation(*, conversation_id: int, escalation_message: str, org_id: int | None) -> None:
    await broadcast_to_conversation_async(
        conversation_id=conversation_id,
        payload={
            'type': 'conversation.updated',
            'conversation_id': conversation_id,
            'escalation_status': True,
            'indicator_type': 'escalation',
        },
    )

    if org_id is None:
        return

    await broadcast_to_org_async(
        organization_id=int(org_id),
        payload={
            'type': 'conversation.list_item.updated',
            'conversation_id': conversation_id,
            'lead_id': conversation_id,
            'last_message': escalation_message,
            'last_message_time': None,
            'indicator_type': 'escalation',
            'ai_enabled': True,
            'escalation_status': True,
        },
    )


# ── conversation STAGE ────────────────────────────────────────────────────
# The backend is the only thing that knows whether a code is outstanding or an
# inquiry is really submitted. It reports that at the END of a turn, so we
# compute the stage from its answer, store it on the session, and hand it to
# the model at the START of the next turn as a plain fact.
#
# The model is never asked to work the stage out from conversation history
# again: that inference is what produced "your details have already been
# submitted" while a verification code was still pending.

STAGE_COLLECTING = 'collecting'
STAGE_AWAITING_VERIFICATION = 'awaiting_verification'
STAGE_SUBMITTED = 'submitted'

_STAGE_KEY = 'admission_stage'

# Statuses meaning "the backend never took it", as opposed to "the backend
# considered it and said no". Mirrors the set in generic_webhook rather than
# importing it - that module imports this one, and a cycle is a worse price
# than four repeated numbers.
_TEMPORARY_BACKEND_FAILURES = frozenset({500, 502, 503, 504})

# One authoritative line per stage. This is what the model acts on - it beats
# any number of prose rules because it cannot drift from the backend.
STAGE_CONTEXT = {
    STAGE_COLLECTING: (
        "STAGE: collecting - no verification code is outstanding and nothing is "
        "submitted yet. Keep gathering the inquiry details, and answer any "
        "question the parent asks along the way. Never tell them their inquiry "
        "is 'registered' or 'submitted' - say their details are noted; the "
        "system confirms registration separately."
    ),
    STAGE_AWAITING_VERIFICATION: (
        "STAGE: awaiting_verification - a verification code has been sent and is "
        "still outstanding. The inquiry is NOT submitted. The parent may still "
        "correct ANY detail (name, student, class, mobile, email): make the "
        "correction and set the field - the system re-verifies and sends a fresh "
        "code by itself. NEVER tell them the inquiry is submitted, that details "
        "are locked, or that only staff can change them. If they type digits, "
        "that is the code: put it in otp_code. "
        "If they say the code never arrived or ask for another one, set "
        "otp_resend_requested: true and say nothing about having sent it - the "
        "system issues the code and reports back itself. Do NOT set it for an "
        "ordinary question: answer that normally, the code stays valid while "
        "you do."
    ),
    STAGE_SUBMITTED: (
        "STAGE: submitted - verification is complete and the inquiry is recorded. "
        "Details can no longer be changed in chat, in any field or any channel: "
        "if they ask for a change, do NOT set any lead field. Tell them plainly "
        "that the inquiry is already submitted so its details cannot be changed "
        "here, and that our admissions team handles corrections once an inquiry "
        "is in - then offer to help with anything else. "
        "Do NOT set escalation for this: a parent correcting a detail has not "
        "asked for a person, and escalating ends the conversation for them. "
        "Set escalation=true only when they ASK to speak to someone, or when you "
        "genuinely cannot help with what they need. "
        "Registering ANOTHER child or a new season inquiry is still allowed - "
        "that is a new inquiry, not a change."
    ),
}


# Whether THIS school verifies with a code at all (IsOTPForInquiry), which is
# not the same question as "is a code outstanding right now". The model writes
# its reply BEFORE the backend call, so on the turn the parent says "yes,
# register it" it has to already know - otherwise it fills the gap itself, and
# it filled it with a code that a no-OTP school never sends.
_VERIFIES_KEY = 'institution_verifies'

VERIFY_CONTEXT = {
    True: (
        " This school DOES verify: after they confirm, the system sends a code "
        "and says so itself - do not announce it before it has."
    ),
    False: (
        " This school does NOT verify with a code. Never mention a code, an OTP "
        "or verification, and never ask them to enter one - there is none. Once "
        "they confirm, the system registers the inquiry and says so itself."
    ),
}


def load_verifies(agent, session_id: str):
    """Whether this school verifies, as last reported by the backend."""
    try:
        state = agent.get_session_state(session_id=session_id) or {}
        value = state.get(_VERIFIES_KEY)
        return bool(value) if value is not None else None
    except Exception as e:
        _ = e
        return None


def save_verifies(agent, session_id: str, verifies: bool) -> None:
    try:
        agent.update_session_state({_VERIFIES_KEY: bool(verifies)}, session_id=session_id)
    except Exception as e:
        _ = e


def stage_context(stage: str, verifies=None) -> str:
    """The stage line, plus what this school does about verification.

    Only `collecting` carries it: by awaiting_verification a code demonstrably
    exists, and by submitted it no longer matters.
    """
    line = STAGE_CONTEXT.get(stage, STAGE_CONTEXT[STAGE_COLLECTING])
    if stage == STAGE_COLLECTING and verifies is not None:
        line += VERIFY_CONTEXT[bool(verifies)]
    return line


def compute_stage(update_resp: dict, *, previous: str | None = None) -> str:
    """Turn the backend's own flags into the conversation stage.

    Only the backend's word counts. An empty response means the turn carried no
    lead update, so the stage is unchanged. `submitted` is terminal - a later
    silent turn can never walk it back.
    """
    resp = update_resp or {}
    if (
        resp.get('otp_verified_now')
        or resp.get('registration_id')
        or resp.get('already_registered')
    ):
        return STAGE_SUBMITTED
    if previous == STAGE_SUBMITTED:
        return STAGE_SUBMITTED
    if resp.get('otp_required'):
        return STAGE_AWAITING_VERIFICATION
    return previous or STAGE_COLLECTING


def load_stage(agent, session_id: str) -> str:
    """The stage this conversation was left in, or `collecting` for a new one."""
    try:
        state = agent.get_session_state(session_id=session_id) or {}
        stage = state.get(_STAGE_KEY)
        return stage if stage in STAGE_CONTEXT else STAGE_COLLECTING
    except Exception as e:
        _ = e
        return STAGE_COLLECTING


# ── conversation INSTITUTION ─────────────────────────────────────────────
# Which college this chat is about is settled BEFORE the conversation - the
# inquiry page's dropdown - and stored on the session, so every later turn is
# TOLD it rather than re-reading it out of history. Same reasoning as STAGE:
# an inference the model has to redo each turn is an inference that eventually
# drifts, and here drifting means quoting another campus's fees.

_INSTITUTION_KEY = 'institution_id'


def load_institution(agent, session_id: str) -> int | None:
    """The institution this conversation was routed to, if any."""
    try:
        state = agent.get_session_state(session_id=session_id) or {}
        return _to_int(state.get(_INSTITUTION_KEY))
    except Exception as e:
        _ = e
        return None


def save_institution(agent, session_id: str, institution_id: int) -> None:
    try:
        agent.update_session_state({_INSTITUTION_KEY: int(institution_id)}, session_id=session_id)
    except Exception as e:
        _ = e


def save_stage(agent, session_id: str, stage: str) -> None:
    """Persist the stage on the session so the next turn - in any worker
    process - starts from the backend's truth rather than a guess."""
    if stage not in STAGE_CONTEXT:
        return
    try:
        agent.update_session_state({_STAGE_KEY: stage}, session_id=session_id)
    except Exception as e:
        _ = e


def _remember_server_message(agent, session_id: str, text: str) -> None:
    """Record a message the SERVER wrote (verification notice, did-you-mean
    email, success/refusal) in the agent's own history.

    Those turns deliberately SUPPRESS the model's reply and send the server's
    verdict instead - but the model never saw that verdict, so on the next
    turn it is answering a question it does not know was asked. That is how a
    parent's "yes" to "did you mean ...@gmail.com?" got read as confirming the
    misspelled address (asking again, forever), and how a still-pending
    verification code got mistaken for a completed submission. Written the
    same way agno seeds `introduction`: an assistant message on its own run.
    """
    if not text:
        return
    try:
        from uuid import uuid4
        from agno.models.message import Message
        from agno.run.agent import RunOutput

        session = agent.get_session(session_id=session_id)
        if session is None:
            return
        role = getattr(agent.model, 'assistant_message_role', None) or 'assistant'
        session.upsert_run(RunOutput(
            run_id=str(uuid4()),
            session_id=session_id,
            agent_id=getattr(agent, 'id', None),
            agent_name=getattr(agent, 'name', None),
            content=text,
            messages=[Message(role=role, content=text)],
        ))
        agent.save_session(session)
    except Exception as e:
        _ = e


async def _send_escalation(
    *,
    lead_id: int,
    escalation_message: str,
    send_message: Callable[[str], Awaitable[bool]],
) -> tuple[int | None, int | None]:
    org_id = None
    try:
        response = django_post('/api/agent/v1/escalation/', {
            'lead_id': int(lead_id),
            'message': escalation_message,
        })
        if response.get('ok'):
            org_id = _to_int(response.get('organization_id'))
    except Exception as e:
        _ = e

    if await send_message(escalation_message):
        save_message(lead_id=int(lead_id), content=escalation_message, sender_type='ai')
    return lead_id, org_id


def prepare_lead_context(
    *,
    channel_id: str,
    chat_id: str,
    channel_type: str,
    content: str,
    message_id: str | None,
    organization_id: int | None,
    branch_id: int | None,
    channel_agent_id: int | None,
) -> tuple[LeadContext | None, bool]:
    batch_key = f"{channel_type}:{channel_id}:{chat_id}"
    charge_key = f"ai_msg:{batch_key}:{message_id}"

    lead_id, ai_enabled, charged, escalated = save_incoming_message_and_charge(
        channel_id=str(channel_id),
        chat_id=str(chat_id),
        channel_type=channel_type,
        content=content,
        organization_id=organization_id,
        branch_id=branch_id,
        charge_key=charge_key,
    )

    if lead_id is None:
        return None, True

    if escalated or (not ai_enabled) or (not charged):
        return None, True

    if channel_agent_id is None:
        return None, True

    lead_id_int = _to_int(lead_id)
    if lead_id_int is None:
        return None, True

    return LeadContext(lead_id=lead_id_int, org_id=_to_int(organization_id), channel_agent_id=int(channel_agent_id)), False


async def handle_agent_batch(
    *,
    batched_text: str,
    user_id: str,
    session_id: str,
    lead_id: int,
    channel_agent_id: int,
    send_message: Callable[[str], Awaitable[bool]],
    escalation_message_getter: Callable[[], str],
    voice_audio_bytes: bytes | None = None,
    known_phone: str | None = None,
    channel_type: str | None = None,
    institution_id: int | None = None,
) -> None:
    from core.manager import supports_audio

    is_voice = voice_audio_bytes is not None and len(voice_audio_bytes) > 0

    # Voice message handling
    if is_voice and not supports_audio():
        unsupported_msg = "Voice messages are not supported yet."
        if await send_message(unsupported_msg):
            save_message(lead_id=lead_id, content=unsupported_msg, sender_type='ai')
        return

    try:
        agent = get_agent(channel_agent_id)
    except ValueError:
        return

    # The page's choice wins for this turn; a chat that already carries one
    # keeps it, so a widget that forgets to resend the id cannot un-route a
    # conversation halfway through.
    stored_institution = load_institution(agent, session_id)
    candidate = _to_int(institution_id)
    # Web chat is the one channel where this number came from a BROWSER: its
    # adapter reads institution_id out of the POST body, while every other
    # channel derives it from the channel binding, which the server owns. A
    # public widget endpoint means anyone can post any integer, so the client's
    # answer gets exactly the check the MODEL's answer already gets below - and
    # the channel's own answer, being authoritative, is left alone. That
    # distinction matters: an institution-scoped agent has an EMPTY choice
    # list, so validating the channel's value would reject it and send the
    # conversation back to the pooled company agent.
    if candidate is not None and channel_type == 'webchat':
        try:
            from infra.profile import get_institution_name
            if not get_institution_name(candidate, agent_id=channel_agent_id):
                candidate = None
        except Exception as e:
            # unverifiable is not the same as valid - drop it and keep whatever
            # this conversation already had
            _ = e
            candidate = None
    institution = candidate or stored_institution
    if institution is not None and institution != stored_institution:
        save_institution(agent, session_id, institution)

    # WHICH AGENT SERVES THIS TURN. Each school has its own agent, holding the
    # documents, FAQs, campuses, classes and flow its staff maintain; the
    # channel's agent may be the COMPANY agent, whose data is the pooled union
    # across the group. Serving a routed conversation from the pool means one
    # school's chat quoting a sister campus's FAQs and offering its classes.
    #
    # Switching the agent settles it structurally rather than by instruction:
    # a school's agent has no institution list at all, so the "which
    # institution?" question disappears from the prompt instead of being
    # suppressed by it, its tools can only reach its own rows, and its
    # knowledge base is its own Qdrant collection. Unresolvable institutions
    # stay on the channel's agent, which is the behaviour that existed before.
    serving_agent_id = channel_agent_id
    if institution is not None:
        try:
            from infra.profile import resolve_institution_agent_id
            resolved = resolve_institution_agent_id(institution, agent_id=channel_agent_id)
            if resolved is not None and resolved != channel_agent_id:
                agent = get_agent(resolved)
                serving_agent_id = resolved
        except Exception as e:
            _ = e          # stay on the channel's agent

    # NEW CHAT greeting, sent ONCE per conversation - an unseen session id
    # means this is the first message. On WhatsApp/Telegram/Instagram there is
    # no "opened the chat" event, so the first message is where the greeting
    # goes. Webchat does NOT get it sent here: its widget already showed the
    # very same server-built text when the panel opened (handed over by the
    # /session endpoint). Either way the parent HAS just been greeted, which
    # the model has to be told - see the GREETING line below.
    is_new_chat = False
    try:
        is_new_chat = agent.get_session(session_id=session_id) is None
    except Exception as e:
        _ = e

    if is_new_chat and channel_type != 'webchat':
        from infra.profile import build_introduction
        intro = build_introduction(agent_id=serving_agent_id, institution_id=institution)
        if intro:
            if await send_message(intro):
                save_message(lead_id=lead_id, content=intro, sender_type='ai')
            # agno seeds `introduction` into a brand-new session by itself, so
            # this one is already in history - nothing to remember here.

    async def send_server_message(text: str) -> bool:
        """Deliver a message the SERVER authored: to the parent, to the CRM
        transcript, and into the agent's history so the next turn knows what
        the parent is replying to.

        All three or none. Recording a message the channel refused would tell
        staff the parent had been answered, and would tell the NEXT turn the
        parent already knows something they were never told.
        """
        if not await send_message(text):
            return False
        save_message(lead_id=lead_id, content=text, sender_type='ai')
        _remember_server_message(agent, session_id, text)
        return True

    run_kwargs: dict = dict(user_id=user_id, session_id=session_id, debug_mode=False)

    from datetime import datetime
    from zoneinfo import ZoneInfo
    from infra.config import get_timezone
    from tools.getAi import get_active_profile
    tz_name = get_timezone(get_active_profile(agent_id=serving_agent_id))
    now = datetime.now(ZoneInfo(tz_name))
    current_date = now.strftime('%Y-%m-%d')
    current_time = now.strftime('%H:%M')
    current_datetime = now.isoformat()
    date_context = f"Context: Today is {current_date} and current time is {current_time} ({tz_name}, {current_datetime})."

    # what the BACKEND says this conversation is - not what the model infers
    # ...and what it says this SCHOOL does about verification, for the same
    # reason: an inference the model redoes each turn eventually drifts
    stage = load_stage(agent, session_id)
    date_context += "\n" + stage_context(stage, load_verifies(agent, session_id))

    # which college was chosen on the page - so the assistant never asks
    if institution is not None:
        from infra.profile import build_routing_context
        date_context += "\n" + build_routing_context(institution, agent_id=channel_agent_id)

    # The parent is reading the welcome RIGHT NOW - the server sent it moments
    # ago on WhatsApp/Telegram, or the widget showed it when the panel opened.
    # The model cannot see that it has just been delivered, so a first message
    # of "Hi" reads to it as an ungreeted opening and it welcomes them all over
    # again: two near-identical welcomes, back to back. Telling it the greeting
    # is already on screen is the same trick as STAGE - a fact it cannot argue
    # with, rather than a rule it might follow.
    if is_new_chat:
        date_context += (
            "\n"
            "GREETING: the welcome message has ALREADY been sent to this parent "
            "and they are reading it now - it named the school and listed what you "
            "can help with. Do NOT greet, welcome, or introduce yourself again, and "
            "do not repeat that list. Reply only with what moves things forward: "
            "answer what they asked, or - if they merely said hello - ask the first "
            "question of the flow. Never open with 'Welcome' or 'Hello'."
        )

    # channel-proven phone (WhatsApp sender) - the AI must not ask for it
    if known_phone:
        date_context += (
            f"\nContext: The parent's mobile number is already known from this channel: {known_phone}. "
            f"Do NOT ask for their mobile number; set lead.phone_number to exactly this value. "
            f"This conversation is already on WhatsApp, so do NOT ask whether the number is on WhatsApp; set whatsapp_opt_in true."
        )

    batched_text = f"{date_context}\n\n{batched_text}"
    
    if is_voice:
        from agno.media import Audio
        run_kwargs['audio'] = [Audio(content=voice_audio_bytes, format='ogg')]

    run_output = await anyio.to_thread.run_sync(
        lambda: agent.run(batched_text, **run_kwargs)
    )

    # Get the agent's session summary (same instance that's handling the conversation)
    session_summary = None
    try:
        summary_obj = agent.get_session_summary(session_id=session_id)
        if summary_obj:
            session_summary = getattr(summary_obj, 'summary', None)
    except Exception as e:
        _ = e
    
    # Fall back to run_output.summary if session summary is not available
    summary_to_save = session_summary or getattr(run_output, 'summary', None)
    update_lead_ai_summary(lead_id=lead_id, ai_summary=_summary_to_text(summary_to_save))
    data = _extract_last_json_object(run_output.content)
    if not isinstance(data, dict):
        return
    lead_payload = data.get('lead') or {}
    if data.get('status') is not None:
        lead_payload['status'] = data['status']
    if data.get('source') is not None:
        lead_payload['source'] = data['source']
    if data.get('is_chat_completed') is not None:
        lead_payload['is_chat_completed'] = bool(data['is_chat_completed'])
    # school CRM fields + AI interest rating - pass through to backend as-is
    for field in (
        'class_id', 'inquiry_type_id', 'institution_id',
        'tour_date', 'tour_time',
        'interest_score', 'interest_level', 'interest_reason',
        'followup_date', 'otp_code', 'whatsapp_opt_in',
        'student_name_correction', 'suggested_reply',
        # the assistant's own report of a promise it just made - the only thing
        # that puts an unanswerable question in front of staff
        'promised_staff_followup',
    ):
        if data.get(field) is not None:
            lead_payload[field] = data[field]
    # details_confirmed is forwarded even when False: null means "the model
    # said nothing this turn" and leaves the stored flag alone, while false is
    # the parent withdrawing it. Folding the two together would let a
    # correction keep a stale confirmation alive.
    if data.get('details_confirmed') is not None:
        lead_payload['details_confirmed'] = bool(data['details_confirmed'])
    # only true is meaningful: not asking for a code is not the same as asking
    # us not to send one, so false is simply left out
    if data.get('otp_resend_requested'):
        lead_payload['otp_resend_requested'] = True
    # the routed institution is the PAGE's answer, not the model's guess: it
    # overrides whatever the model put in institution_id, so an inquiry can
    # never be filed against the wrong college
    if institution is not None:
        lead_payload['institution_id'] = institution
    else:
        # NOTHING routed this chat - a company-level WhatsApp or Telegram
        # number, where there is no page to choose on and the assistant had to
        # ask. The parent has now answered, so ADOPT that answer: stored on the
        # session, it routes every later turn to the school's own agent, which
        # is what stops the group's pooled FAQs and class list being used for
        # the rest of the conversation. Without this the id reached the CRM but
        # the chat kept re-deriving the school from history - the same drift
        # the STAGE fact exists to prevent.
        # Validated against the real list first: a model-invented id must never
        # route a conversation.
        candidate = _to_int(data.get('institution_id'))
        if candidate is not None:
            try:
                from infra.profile import get_institution_name
                if get_institution_name(candidate, agent_id=channel_agent_id):
                    save_institution(agent, session_id, candidate)
            except Exception as e:
                _ = e
    # opt-out is one-way from the AI: forward only true so a routine
    # false can never silently clear a parent's stored "stop messaging me"
    if bool(data.get('whatsapp_opt_out')):
        lead_payload['whatsapp_opt_out'] = True
    update_resp: dict = {}
    if lead_payload:
        # PROVENANCE - decided here, never by the model. The website form
        # (SubmitInquiry) and this agent write the SAME inquiry table with the
        # same shape, so without this nothing tells them apart. The form sends
        # inquiry_source='online_form'; every agent-captured inquiry says
        # 'ai_agent' plus the channel it came in on. `source` cannot do this
        # job: the model picks it fresh every turn, so two identical inquiries
        # can carry different values.
        # Set only alongside real extracted data, so an empty turn still makes
        # no backend call.
        lead_payload.update({
            'inquiry_source': 'ai_agent',        # vs 'online_form' from the website form
            'inquiry_channel': channel_type or 'unknown',
            'inquiry_agent_id': channel_agent_id,
        })
        update_resp = update_lead_from_payload(lead_id=lead_id, payload=lead_payload)

    # the backend has just spoken - store what it said for the next turn
    next_stage = compute_stage(update_resp, previous=stage)
    if next_stage != stage:
        save_stage(agent, session_id, next_stage)
    # the school's posture travels on the same response; remembered so the NEXT
    # turn's prompt already carries it - by the confirming turn the backend has
    # reported it many times over
    if update_resp.get('institution_verifies') is not None:
        save_verifies(agent, session_id, bool(update_resp['institution_verifies']))

    # CLOSING ON NOTHING. A chat that produced an inquiry has moved to
    # awaiting_verification or submitted; still collecting means the backend
    # created nothing - typically because a required field never made it,
    # like a class the model accepted but could not map to a real one. The
    # model cannot see that, so it signs off ("your details have been noted,
    # our team will be in touch") and the parent waits for a call that will
    # never come. The stage is the only thing that knows, so it decides.
    premature_close = bool(data.get('is_chat_completed')) and next_stage == STAGE_COLLECTING
    if premature_close:
        # the CRM was just told this conversation is finished - take it back,
        # or staff see a closed chat with no inquiry behind it
        try:
            update_lead_from_payload(lead_id=lead_id, payload={'is_chat_completed': False})
        except Exception as e:
            _ = e
        logger_msg = (
            "Conversation %s: the AI closed a chat with no inquiry created "
            "(stage still collecting) - closing message suppressed."
        )
        try:
            import logging
            logging.getLogger(__name__).warning(logger_msg, session_id)
        except Exception as e:
            _ = e

    if bool(data.get('escalation')):
        escalation_message = data.get('message', '') or escalation_message_getter()
        _, org_id = await _send_escalation(
            lead_id=lead_id,
            escalation_message=escalation_message,
            send_message=send_message,
        )
        await _broadcast_escalation(conversation_id=lead_id, escalation_message=escalation_message, org_id=org_id)
        return

    # On a code-entry turn the MODEL cannot know whether the code is right -
    # its guess-reply ("thank you, I've updated your record") is misleading
    # when the code was wrong. Suppress it and let the server's deterministic
    # verdict (didn't-match / renewed / success below) be the only message.
    # 'new' is suppressed too: on the turn the code is first issued the AI's
    # guess-reply ("our team will review and get back to you") reads as if the
    # inquiry were finished, directly contradicting the review-and-verify
    # message that follows it - the server's message is the only one sent
    otp_failed_turn = bool(update_resp.get('otp_required')) and \
        update_resp.get('otp_notice') in (
            'new', 'retry', 'renewed',
            # a code was generated but the sender could not deliver it - the
            # model would happily say it was sent
            'send_failed',
            # the resend verdicts: the model asked for a code but does not know
            # whether it was issued, refused, or failed to send - only the
            # server does, so only the server tells the parent
            'resent', 'resend_too_soon', 'resend_limit', 'resend_failed')
    otp_success_turn = bool(update_resp.get('otp_verified_now'))
    # post-submission edit attempt: the server refused the change (submitted
    # inquiries are read-only in chat) - the AI's reply may falsely claim the
    # change was made, so it is suppressed like a wrong-code guess-reply
    edit_refused_turn = bool(update_resp.get('edit_refused'))
    # the inquiry insert failed server-side (configuration gap): the AI may
    # have claimed success - suppress and let the honest notice below speak
    promotion_failed_turn = bool(update_resp.get('promotion_failed'))
    # the email sat on a known-typo domain (gmoil.com...): it was NOT stored,
    # and the AI's "thanks, noted your email" would be false - suppress it
    # and ask the did-you-mean question instead
    email_typo = update_resp.get('email_typo_suggestion')
    # the number could not be a phone number and was NOT stored - the AI's
    # "thanks, I've noted your mobile" would be false, so it is suppressed and
    # the number is asked for again
    phone_invalid = bool(update_resp.get('phone_invalid'))
    # The save never reached the backend. The model wrote its reply before this
    # call and cannot know - so it must not be the one to speak. A BUSINESS
    # refusal is ok:true with its own flag and is deliberately excluded here;
    # an empty dict means no call was made and is also excluded.
    save_failed = (update_resp.get('ok') is False
                   and update_resp.get('status') in _TEMPORARY_BACKEND_FAILURES)
    if 'message' in data and not (
            otp_failed_turn or otp_success_turn or edit_refused_turn
            or promotion_failed_turn or email_typo or phone_invalid
            or save_failed or premature_close):
        message_text = data['message']
        if await send_message(message_text):
            save_message(lead_id=lead_id, content=message_text, sender_type='ai')

    # ... and say something true instead. Deliberately does NOT guess which
    # field is missing: it reopens the conversation so the next turn asks
    # properly, rather than inventing a reason.
    # Says what is true and what is not: nothing was stored, but nothing was
    # thrown away either - the parent's message is already on the record and
    # the details are still in the conversation, so trying again costs them
    # nothing. No escalation: a backend blip is not a reason to hand the chat
    # to a person. No retry: the server may have finished work this side never
    # saw, which is exactly why it must not be repeated automatically.
    if save_failed:
        await send_server_message(
            "I'm having trouble saving that just now - nothing has been lost, "
            "please give me a moment and try again."
        )

    if premature_close:
        await send_server_message(
            "Sorry - I can't pass this to our admissions team just yet, as I am "
            "still missing something. Let me check the details with you once more."
        )

    # deterministic SUCCESS confirmation - the one place the parent is told
    # their inquiry is actually registered. The AI's reply for the turn is
    # sent AFTER it: the prompt shapes it as a brief knowledge-base-grounded
    # follow-up (documents, fee offer, next steps) with no verdict claims, so
    # each school's own content does the informing - nothing hardcoded here.
    if otp_success_turn:
        ok_msg = (
            "Verification successful - your inquiry has been registered. "
            "Our admissions team will contact you soon."
            if update_resp.get('registration_id')
            else "Verification successful."
        )
        await send_server_message(ok_msg)
        if update_resp.get('registration_id') and data.get('message'):
            info_msg = data['message']
            if await send_message(info_msg):
                save_message(lead_id=lead_id, content=info_msg, sender_type='ai')

    # the registration could not be created (server-side configuration gap) -
    # the conversation is already escalated; tell the parent honestly instead
    # of leaving silence or a false success claim
    if promotion_failed_turn:
        fail_msg = (
            "Your details are verified and safely recorded, but our system "
            "could not complete the registration automatically. Our admissions "
            "team has been notified and will complete it for you - no action "
            "is needed from your side."
        )
        await send_server_message(fail_msg)

    # OTP gate: the backend held the inquiry until the parent verifies their
    # contact. otp_notice is sent at most ONCE per code event ('new'/'retry');
    # turns where a code is merely outstanding stay silent. otp_target names
    # WHICH contact the code went to - when the parent CHANGES their phone or
    # email, only the changed one is re-verified and the message says so.
    otp_notice = update_resp.get('otp_notice')
    if update_resp.get('otp_required') and otp_notice:
        dest = {
            'phone': 'your mobile number',
            'email': 'your email',
        }.get(update_resp.get('otp_target'), 'your mobile number / email')
        if otp_notice == 'retry':
            left = update_resp.get('otp_attempts_left') or 0
            tries = (
                f" You have {left} attempt{'s' if left != 1 else ''} left before a new code is sent."
                if left > 0 else ""
            )
            otp_msg = (
                f"That code didn't match. Please check the latest verification code "
                f"sent to {dest} and type it here.{tries}"
            )
        elif otp_notice == 'renewed':
            otp_msg = (
                f"Too many incorrect attempts, so we've sent a FRESH verification code "
                f"to {dest}. Please use the newest code - it is valid for 5 minutes."
            )
        elif otp_notice == 'send_failed':
            # says only what the sender reported. It could not hand the message
            # over; that is not the same as knowing it never arrived, so the
            # wording claims neither. The code IS live, and the resend path is
            # the way back - which is why this points at it.
            otp_msg = (
                "I wasn't able to send the verification code just now - that's "
                "on our side, not yours. Give it a moment and ask me to resend it."
            )
        elif otp_notice == 'resent':
            otp_msg = (
                f"Sure - I've sent a new verification code to {dest}. "
                f"It is valid for 5 minutes, and any earlier code no longer works."
            )
        elif otp_notice == 'resend_too_soon':
            # deliberately does NOT say a code was sent: none was made
            otp_msg = (
                f"The code was only just sent to {dest} - please give it a moment "
                f"to arrive, and check your spam folder. Ask me again in a minute "
                f"if it still hasn't come through."
            )
        elif otp_notice == 'resend_limit':
            otp_msg = (
                f"I've already sent several codes to {dest}, so I can't send another "
                f"right now. Please check your spam folder - if nothing has arrived, "
                f"our admissions team can complete this with you directly."
            )
        elif otp_notice == 'resend_failed':
            # honest: the code exists, the message did not go
            otp_msg = (
                "I wasn't able to send the code just now - that's on our side, "
                "not yours. Please try again in a moment."
            )
        else:
            # No details listed here any more. The code is only issued
            # after the parent has confirmed them in the chat, so this
            # notice repeating the same five fields asked them to review
            # what they had just reviewed. Corrections are still open -
            # saying so is enough.
            otp_msg = (
                f"We've sent a verification code to {dest} - "
                f"please type the code here to complete your inquiry. "
                f"The code is valid for 5 minutes. "
                f"If any of the details you just confirmed are wrong, "
                f"tell me instead and I'll correct them."
            )
        await send_server_message(otp_msg)

    # a number that cannot be a phone number was dropped rather than stored -
    # say so plainly and name the likely fix, because the commonest cause is a
    # number sent without its country code
    if phone_invalid:
        await send_server_message(
            "That doesn't look like a complete phone number. "
            "Could you send it with the country code?"
        )

    # misspelled email domain: ask for confirmation instead of silently
    # storing a dead mailbox (the OTP mail would never arrive) - the address
    # is re-collected, never auto-corrected on the parent's behalf
    if email_typo:
        typo_msg = (
            f"That email address looks misspelled - did you mean {email_typo}? "
            "Please type your email address again to confirm."
        )
        await send_server_message(typo_msg)

    # deterministic read-only notice: the backend refused a post-submission
    # edit and escalated the conversation so staff see the request - this
    # server verdict is the only message the parent gets on such a turn
    if edit_refused_turn:
        # Says only what is true. The old wording promised the request had been
        # passed to admissions and would be reviewed - nothing did that, and
        # nothing could: staff have no way to reply into this conversation.
        # It also invites the parent to carry on, because the assistant is
        # still here now that a refusal no longer escalates.
        lock_msg = (
            "This inquiry has already been submitted, so its details can't be "
            "changed here in the chat - our admissions team handles corrections "
            "once an inquiry is in. Please raise it with them directly and they "
            "will sort it out. Is there anything else I can help you with in "
            "the meantime?"
        )
        await send_server_message(lock_msg)

    # second chat, same inquiry: this conversation just linked to an inquiry
    # that already exists (registered via another chat or the public form) -
    # tell the parent clearly, once, that no re-verification is needed
    if update_resp.get('already_registered'):
        dup_msg = (
            "Good news - this student's inquiry is already registered with us, "
            "so no further verification is needed. Our admissions team will be "
            "in touch. Is there anything else I can help you with?"
        )
        await send_server_message(dup_msg)

