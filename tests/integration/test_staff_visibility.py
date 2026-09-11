"""What staff can actually see, tested against the real queries.

Two screens were hiding facts they already had:

  Conversations tab - a form abandoned at the first question, a parent who
  failed the code three times, and a parent still typing all rendered the same
  blank cell. Every input was on the row; none of it was selected.

  Follow-up dialog - a refused correction IS written to the open follow-up, and
  then the dialog resets blank and prefills from the conversation, so the only
  way to find that text was the History tab with a date range.

Both endpoints sit behind [Authorize] and want a CRM user's JWT, which this
harness does not have - it authenticates as the agent. So these tests take the
SQL **out of the C# source** and run it against the same database the API uses.
That is narrower than an HTTP test (it does not prove the wiring) but it cannot
drift from the implementation, and the part most likely to be wrong - the
derivation and the scoping - is exactly what it exercises.

Conversation states: the ones the flow reaches easily are built through the real
API; the OTP ones are set by writing the columns directly, on a conversation
this test created. The OTP ladder has its own tests - what is under test here is
the CASE expression that reads those columns.

    python -m unittest tests.integration.test_staff_visibility -v
"""
import io
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from db_conn import connect                               # noqa: E402
from test_confirmation_gate import (                      # noqa: E402
    GateCase, CLASS_OTP_OFF, CLASS_OTP_OFF_ALT, OTP_OFF, _row,
)

SERVER = os.getenv(
    'ITEST_SERVER_SRC',
    r'D:\Campus_Angular_Project\OnlineCampusAngular\server\src')

CONV_READ_CS = os.path.join(
    SERVER, 'OnlineCampus.Infrastructure', 'Crm', 'AgentKnowledge',
    'AgentKnowledgeRepository.cs')
AGENT_CTX_CS = os.path.join(
    SERVER, 'OnlineCampus.Infrastructure', 'Registration',
    'StudentInquiryFollowup', 'StudentInquiryFollowupRepository.cs')


def verbatim_sql(path, marker):
    """The @"..." literal that follows `marker` in a C# file.

    Reading the query from the source is the whole point: a copy pasted into the
    test would keep passing after someone edited the real one.
    """
    if not os.path.exists(path):
        raise unittest.SkipTest('%s not reachable from here' % path)
    src = io.open(path, encoding='utf-8').read()
    if marker not in src:
        raise AssertionError('marker %r not found in %s' % (marker, path))
    i = src.index('@"', src.index(marker)) + 2
    out = []
    while True:
        ch = src[i]
        if ch == '"':
            if src[i + 1] == '"':          # "" is an escaped quote inside @"..."
                out.append('"')
                i += 2
                continue
            break
        out.append(ch)
        i += 1
    return ''.join(out)


def run_conversation_read(institution_id, needs_attention_only):
    sql = verbatim_sql(
        CONV_READ_CS,
        'public async Task<List<AgentConversationListRow>> ConversationReadAsync')
    batch = ('DECLARE @InstitutionID INT = %d;\n'
             'DECLARE @NeedsAttentionOnly INT = %d;\n%s'
             % (institution_id, 1 if needs_attention_only else 0, sql))
    with connect() as cn:
        c = cn.cursor()
        c.execute(batch)
        cols = [d[0] for d in c.description]
        return {r[cols.index('ConversationID')]: dict(zip(cols, r))
                for r in c.fetchall()}


def run_agent_context(registration_id):
    sql = verbatim_sql(
        AGENT_CTX_CS,
        'public async Task<FollowupAgentContextDto?> GetAgentContextAsync')
    batch = ('DECLARE @RegistrationID INT = %d;\n%s' % (registration_id, sql))
    with connect() as cn:
        c = cn.cursor()
        c.execute(batch)
        cols = [d[0] for d in c.description]
        row = c.fetchone()
        return dict(zip(cols, row)) if row else None


def set_columns(conversation_id, **cols):
    """Put a conversation THIS TEST created into a given OTP state."""
    sets = ', '.join('%s = ?' % k for k in cols)
    with connect() as cn:
        c = cn.cursor()
        c.execute('UPDATE dbo.CmpAgentConversation SET %s WHERE ConversationID = ?'
                  % sets, *list(cols.values()), conversation_id)
        cn.commit()


class VisibilityCase(GateCase):
    institution = OTP_OFF
    klass = CLASS_OTP_OFF


# ── the query itself must be valid and derive the states ──────────────────

class TestTheStuckStateIsDerived(VisibilityCase):

    def stuck(self, lead, needs_attention_only=False):
        rows = run_conversation_read(self.institution, needs_attention_only)
        return rows.get(lead)

    def test_a_brand_new_chat_is_form_incomplete_at_the_parent_name(self):
        lead = self.open_conversation()
        # route it and nothing else - the institution filter (pre-existing) drops
        # conversations that have not been routed to a school yet
        self.update(lead, institution_id=self.institution)
        row = self.stuck(lead)
        self.assertIsNotNone(row, 'the conversation was not in the list at all')
        self.assertEqual(row['StuckCode'], 'formIncomplete')
        self.assertEqual(row['MissingField'], 'parent')

    def test_the_missing_field_walks_forward_as_details_arrive(self):
        lead = self.open_conversation()
        self.update(lead, institution_id=self.institution,
                    name='itest parent %s' % self.tag)
        self.assertEqual(self.stuck(lead)['MissingField'], 'student')
        self.update(lead, student_name=self.student)
        self.assertEqual(self.stuck(lead)['MissingField'], 'phone')
        self.update(lead, phone_number=self.phone)
        self.assertEqual(self.stuck(lead)['MissingField'], 'email')
        self.update(lead, email='itest.%s@example.com' % self.tag)
        self.assertEqual(self.stuck(lead)['MissingField'], 'class')

    def test_a_complete_unconfirmed_form_is_awaiting_confirmation(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        row = self.stuck(lead)
        self.assertEqual(row['StuckCode'], 'awaitingConfirmation')
        self.assertIsNone(row['MissingField'])

    def test_a_submitted_inquiry_says_submitted(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        self.assertIsNotNone(_row(lead)['RegistrationID'])
        row = self.stuck(lead)
        self.assertEqual(row['StuckCode'], 'submitted')
        self.assertIsNone(row['MissingField'],
                          'a submitted inquiry reported a missing field')

    def test_an_outstanding_code_says_waiting_for_code(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        set_columns(lead, OtpCode='123456', OtpVerified=0)
        self.assertEqual(self.stuck(lead)['StuckCode'], 'otpPending')

    def test_three_wrong_attempts_say_verification_failed(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        set_columns(lead, OtpAttempts=3, OtpVerified=0)
        self.assertEqual(self.stuck(lead)['StuckCode'], 'otpFailed')

    def test_a_used_up_resend_allowance_also_says_verification_failed(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        set_columns(lead, OtpResendCount=3, OtpVerified=0)
        self.assertEqual(self.stuck(lead)['StuckCode'], 'otpFailed')

    def test_a_failed_code_beats_an_outstanding_one(self):
        """Both are true at once after the third wrong guess; the one a person
        has to act on must win."""
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        set_columns(lead, OtpCode='123456', OtpAttempts=3, OtpVerified=0)
        self.assertEqual(self.stuck(lead)['StuckCode'], 'otpFailed')

    def test_submitted_beats_every_otp_state(self):
        """StuckCode answers only how far the FORM got - a verified inquiry is
        finished no matter what the code columns still hold."""
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        set_columns(lead, OtpAttempts=3, OtpVerified=0)
        self.assertEqual(self.stuck(lead)['StuckCode'], 'submitted')


# ── the student applying for themselves ──────────────────────────────────

class TestASelfInquiryReadsAsIncomplete(VisibilityCase):
    """The student is chatting, so their own name lands in BOTH slots until the
    guardian's name arrives.

    `namesDistinct` blocks promotion for exactly that reason - the parent-name
    question is still pending - so this column has to say the same thing. It was
    reading 'inProgress', which is the one value that means "nothing to see",
    and the chat fell out of the needs-attention list while genuinely stuck.
    """

    def self_inquiry(self, confirmed):
        lead = self.open_conversation()
        same = 'itest %s' % self.tag            # the SAME string in both slots
        fields = dict(name=same, student_name=same, phone_number=self.phone,
                      email='itest.%s@example.com' % self.tag,
                      institution_id=self.institution, class_id=self.klass)
        if confirmed:
            fields['details_confirmed'] = True
        self.update(lead, **fields)
        self.assertIsNone(_row(lead)['RegistrationID'],
                          'an inquiry was created with one name in both slots')
        return lead

    def row(self, lead):
        return run_conversation_read(self.institution, False)[lead]

    def test_it_reads_as_a_missing_parent_name(self):
        row = self.row(self.self_inquiry(confirmed=False))
        self.assertEqual(row['StuckCode'], 'formIncomplete')
        self.assertEqual(row['MissingField'], 'parent')

    def test_it_still_reads_that_way_once_the_student_has_confirmed(self):
        """The case that was hidden: everything looks present and confirmed, so
        the column said 'inProgress' while the server refused to promote."""
        row = self.row(self.self_inquiry(confirmed=True))
        self.assertEqual(row['StuckCode'], 'formIncomplete')
        self.assertEqual(row['MissingField'], 'parent')

    def test_it_is_not_mislabelled_as_awaiting_confirmation(self):
        """Before the fix this branch matched first and named the wrong step."""
        self.assertNotEqual(
            self.row(self.self_inquiry(confirmed=False))['StuckCode'],
            'awaitingConfirmation')

    def test_the_case_differs_but_the_name_is_the_same(self):
        """The CRM title-cases names, so the comparison cannot be case-sensitive."""
        lead = self.open_conversation()
        self.update(lead, name='itest %s' % self.tag,
                    student_name=('itest %s' % self.tag).upper(),
                    phone_number=self.phone,
                    email='itest.%s@example.com' % self.tag,
                    institution_id=self.institution, class_id=self.klass)
        row = self.row(lead)
        self.assertEqual(row['StuckCode'], 'formIncomplete')
        self.assertEqual(row['MissingField'], 'parent')

    def test_staff_see_it_once_the_student_has_confirmed(self):
        """Said yes, still did not go through - that is stuck, and the student
        has already done everything they believe was asked of them."""
        lead = self.self_inquiry(confirmed=True)
        self.assertIn(lead, run_conversation_read(self.institution, True),
                      'a stuck self-inquiry is still invisible to staff')

    def test_an_unconfirmed_one_is_left_to_the_assistant(self):
        """Mid-collection is not stuck - the SELF-INQUIRY rule has the model
        ask for the guardian's name next, and staff do not need to see that."""
        lead = self.self_inquiry(confirmed=False)
        self.assertIn(lead, run_conversation_read(self.institution, False))
        self.assertNotIn(lead, run_conversation_read(self.institution, True),
                         'every half-typed chat now lands on the staff list')

    def test_the_guardian_name_releases_it(self):
        """The whole point of flagging it - and the normal path must still work."""
        lead = self.self_inquiry(confirmed=True)
        self.update(lead, name='itest guardian %s' % self.tag)
        row = self.row(lead)
        self.assertEqual(row['StuckCode'], 'awaitingConfirmation',
                         'the guardian name did not move it on')
        self.assertIsNone(row['MissingField'])

        self.update(lead, details_confirmed=True)
        self.assertIsNotNone(_row(lead)['RegistrationID'],
                             'the self-inquiry never completed')
        self.assertEqual(self.row(lead)['StuckCode'], 'submitted')

    def test_two_genuinely_different_names_are_untouched(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        row = self.row(lead)
        self.assertEqual(row['StuckCode'], 'awaitingConfirmation')
        self.assertIsNone(row['MissingField'])


# ── the filter must agree with the column ─────────────────────────────────

class TestNeedsAttentionFilter(VisibilityCase):

    def test_a_failed_code_is_included(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        set_columns(lead, OtpAttempts=3, OtpVerified=0)
        self.assertIn(lead, run_conversation_read(self.institution, True))

    def test_awaiting_confirmation_is_included(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        self.assertIn(lead, run_conversation_read(self.institution, True))

    def test_an_escalated_chat_is_still_included(self):
        """The filter this replaced showed exactly these - it must not have
        stopped showing them."""
        lead = self.open_conversation()
        self.update(lead, institution_id=self.institution)
        set_columns(lead, Escalated=1)
        self.assertIn(lead, run_conversation_read(self.institution, True))

    def test_an_escalated_submitted_chat_is_still_included(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        set_columns(lead, Escalated=1)
        rows = run_conversation_read(self.institution, True)
        self.assertIn(lead, rows)
        self.assertEqual(rows[lead]['StuckCode'], 'submitted',
                         'the escalation overwrote how far the form got')

    def test_a_half_typed_form_is_not_included(self):
        """Someone mid-sentence is not waiting on staff."""
        lead = self.open_conversation()
        self.update(lead, institution_id=self.institution,
                    name='itest parent %s' % self.tag)
        # routed, so it IS in the unfiltered list - which is what makes its
        # absence from the filtered one mean something
        self.assertIn(lead, run_conversation_read(self.institution, False))
        self.assertNotIn(lead, run_conversation_read(self.institution, True))

    def test_an_outstanding_code_is_not_included(self):
        """A code sent two minutes ago needs no one."""
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        set_columns(lead, OtpCode='123456', OtpVerified=0)
        self.assertNotIn(lead, run_conversation_read(self.institution, True))

    def test_a_plain_submitted_inquiry_is_not_included(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        self.assertNotIn(lead, run_conversation_read(self.institution, True))

    def test_unfiltered_still_returns_everything(self):
        lead = self.open_conversation()
        self.update(lead, institution_id=self.institution,
                    name='itest parent %s' % self.tag)
        self.assertIn(lead, run_conversation_read(self.institution, False))


# ── the transcript header fields ──────────────────────────────────────────

class TestTheAiReadIsReturned(VisibilityCase):
    """InterestReason and AISummary were written every turn and selected never."""

    def test_the_interest_reason_comes_back(self):
        lead = self.open_conversation()
        self.update(lead, institution_id=self.institution,
                    interest_score=55, interest_level='warm',
                    interest_reason='asked fees for Grade 4, code failed')
        row = run_conversation_read(self.institution, False)[lead]
        self.assertIn('code failed', row['InterestReason'] or '')
        self.assertEqual(row['InterestScore'], 55)

    def test_the_summary_column_is_present_and_bounded(self):
        """NVARCHAR(MAX) x 200 rows is why it is truncated."""
        lead = self.open_conversation()
        self.update(lead, institution_id=self.institution)
        row = run_conversation_read(self.institution, False)[lead]
        self.assertIn('AISummary', row)
        self.assertLessEqual(len(row['AISummary'] or ''), 600)


# ── the follow-up dialog: pending requests, correctly scoped ──────────────

class TestPendingRequestsReachTheDialog(VisibilityCase):

    def submitted_with_a_refused_correction(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        reg = _row(lead)['RegistrationID']
        self.assertIsNotNone(reg)
        res = self.update(lead, email='itest.changed.%s@example.com' % self.tag)
        self.assertTrue(res.get('edit_refused'), 'nothing was refused to record')
        return lead, reg

    def test_the_refused_correction_is_returned(self):
        _, reg = self.submitted_with_a_refused_correction()
        ctx = run_agent_context(reg)
        self.assertIsNotNone(ctx, 'no agent context for an agent inquiry')
        self.assertIn('email', (ctx['PendingRequests'] or '').lower(),
                      'the request is written but still not returned: %r'
                      % (ctx['PendingRequests'],))

    def test_nothing_pending_returns_null(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        reg = _row(lead)['RegistrationID']
        ctx = run_agent_context(reg)
        self.assertIsNotNone(ctx)
        self.assertIsNone(ctx['PendingRequests'],
                          'an inquiry with no open request showed one')

    def test_the_existing_prefill_still_comes_back(self):
        lead, reg = self.submitted_with_a_refused_correction()
        self.update(lead, interest_score=80, interest_level='hot',
                    interest_reason='wants to finish this week')
        ctx = run_agent_context(reg)
        self.assertEqual(ctx['InterestScore'], 80)
        self.assertIn('finish', ctx['InterestReason'] or '')

    # THE SCOPING TRAP
    def test_a_siblings_request_does_not_appear_on_this_inquiry(self):
        """The conversation lookup in this query deliberately falls back to the
        parent's PHONE, so siblings share the score and the summary. Pending
        requests must NOT follow that fallback, or one child's refused
        correction shows on the other child's dialog.
        """
        lead, first = self.submitted_with_a_refused_correction()

        # the sibling, same parent and phone, their own inquiry
        self.update(lead, student_name=self.student + ' junior')
        self.update(lead, class_id=CLASS_OTP_OFF_ALT)
        self.update(lead, details_confirmed=True)
        second = _row(lead)['RegistrationID']
        self.assertIsNotNone(second, 'the sibling was never registered')
        self.assertNotEqual(second, first)

        ctx = run_agent_context(second)
        self.assertIsNotNone(ctx, 'the sibling got no context at all')
        self.assertIsNone(ctx['PendingRequests'],
                          'the sibling inherited the first child\'s request')


if __name__ == '__main__':
    unittest.main(verbosity=2)
