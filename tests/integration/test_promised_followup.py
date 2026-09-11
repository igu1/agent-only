"""A promise the chat makes has to reach the people who can keep it.

When the assistant cannot answer something it says "our admissions team will
check and come back to you" - and that was the end of it. No follow-up row, no
escalation, nothing a person works from. Proven live during the audit:

    parent > Do you have a bus route to Feroke?
    agent  < We do offer school transportation. For specific routes like
             Feroke, our admissions team will be happy to verify...
    db     > followups = 0

The parent waits for a call nobody knows to make.

The assistant now reports the promise it just made, as TEXT - the server cannot
see what was asked, and "the assistant promised something, read the chat" is a
task rather than an answer.

    python -m unittest tests.integration.test_promised_followup -v
"""
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

BUS = 'bus route to Feroke'


def _open_notes(registration_id):
    with connect() as cn:
        c = cn.cursor()
        c.execute("""SELECT ISNULL(ClientFeedBack,'') + ' || ' + ISNULL(Description,'')
                       FROM CmpInquiryFollowup
                      WHERE RegistrationID = ? AND ISNULL(IsFollowed,'N') = 'N'""",
                  registration_id)
        return '\n'.join(r[0] for r in c.fetchall())


def _followup_count(registration_id):
    with connect() as cn:
        c = cn.cursor()
        c.execute('SELECT COUNT(*) FROM CmpInquiryFollowup WHERE RegistrationID = ?',
                  registration_id)
        return c.fetchone()[0]


class PromiseCase(GateCase):
    """OTP off so a whole submission happens without sending any mail."""

    institution = OTP_OFF
    klass = CLASS_OTP_OFF

    def submitted(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        reg = _row(lead)['RegistrationID']
        self.assertIsNotNone(reg, 'the inquiry was never created')
        return lead, reg


# ── the gap being closed ──────────────────────────────────────────────────

class TestThePromiseReachesStaff(PromiseCase):

    def test_an_unanswerable_question_lands_on_the_worklist(self):
        """The exact case from the audit."""
        lead, reg = self.submitted()
        self.update(lead, promised_staff_followup=BUS,
                    interest_score=85, interest_level='hot',
                    interest_reason='asked about transport after submitting')
        notes = _open_notes(reg)
        self.assertIn('Feroke', notes,
                      'the promise still goes nowhere: %r' % notes)

    def test_the_note_says_the_assistant_could_not_answer(self):
        lead, reg = self.submitted()
        self.update(lead, promised_staff_followup=BUS)
        notes = _open_notes(reg)
        self.assertIn('could not answer', notes)
        self.assertIn('please follow it up', notes)

    def test_two_promises_share_one_open_row(self):
        """Three questions are three lines, not three rows to work through."""
        lead, reg = self.submitted()
        self.update(lead, promised_staff_followup=BUS)
        self.update(lead, promised_staff_followup='science lab in Grade 6')
        self.assertEqual(_followup_count(reg), 1,
                         'each question opened its own follow-up')
        notes = _open_notes(reg)
        self.assertIn('Feroke', notes)
        self.assertIn('science lab', notes)

    def test_it_does_not_escalate(self):
        """One question needs a person; the whole conversation does not."""
        lead, _ = self.submitted()
        self.update(lead, promised_staff_followup=BUS)
        with connect() as cn:
            c = cn.cursor()
            c.execute('SELECT Escalated FROM CmpAgentConversation WHERE ConversationID = ?',
                      lead)
            self.assertFalse(bool(c.fetchone()[0]),
                             'an unanswered question silenced the assistant')

    def test_a_very_long_promise_is_bounded(self):
        lead, reg = self.submitted()
        self.update(lead, promised_staff_followup='x' * 900)
        self.assertIn('xxx', _open_notes(reg))     # written, not dropped

    def test_it_shares_the_row_with_a_refused_correction(self):
        """Both kinds of chat-side request belong on the same open row."""
        lead, reg = self.submitted()
        self.update(lead, email='itest.changed.%s@example.com' % self.tag)
        self.update(lead, promised_staff_followup=BUS)
        notes = _open_notes(reg)
        self.assertIn('email', notes.lower())
        self.assertIn('Feroke', notes)
        self.assertEqual(_followup_count(reg), 1)


# ── quiet turns must stay quiet ───────────────────────────────────────────

class TestNothingIsWrittenWithoutAPromise(PromiseCase):

    def test_an_ordinary_turn_writes_nothing(self):
        lead, reg = self.submitted()
        self.update(lead, interest_score=80, interest_level='hot',
                    interest_reason='still chatting')
        self.assertEqual(_followup_count(reg), 0)

    def test_an_empty_promise_writes_nothing(self):
        lead, reg = self.submitted()
        self.update(lead, promised_staff_followup='')
        self.assertEqual(_followup_count(reg), 0)

    def test_whitespace_only_writes_nothing(self):
        lead, reg = self.submitted()
        self.update(lead, promised_staff_followup='   ')
        self.assertEqual(_followup_count(reg), 0)


# ── before submission there is nowhere to write ───────────────────────────

class TestBeforeSubmission(PromiseCase):

    def test_a_promise_before_submission_creates_no_row(self):
        """No RegistrationID, so no worklist row can exist. The question is in
        the transcript and the summary; the Conversations screen is where a
        pre-submission chat is meant to be seen."""
        lead = self.open_conversation()
        self.collect(lead)
        res = self.update(lead, promised_staff_followup=BUS)
        self.assertTrue(res.get('ok'), 'the turn failed instead of ignoring it')
        self.assertIsNone(_row(lead)['RegistrationID'])

    def test_it_does_not_block_the_inquiry_being_created(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, promised_staff_followup=BUS,
                    details_confirmed=True)
        self.assertIsNotNone(_row(lead)['RegistrationID'],
                             'a promise stopped the inquiry being created')


# ── ⚠ the sibling trap ────────────────────────────────────────────────────

class TestASiblingsQuestionGoesToTheirOwnInquiry(PromiseCase):
    """RegistrationID lives on the CONVERSATION and is never cleared, so while
    a second child is being collected it still points at the first. Gating on
    it alone would file the sibling's question against their brother."""

    def test_a_question_during_the_siblings_collection_is_not_filed(self):
        lead, first = self.submitted()

        self.update(lead, student_name=self.student + ' junior')
        self.update(lead, promised_staff_followup='uniform size for the new child')

        notes = _open_notes(first)
        self.assertNotIn('uniform', notes,
                         "the sibling's question landed on the first child's inquiry")

    def test_once_the_sibling_has_their_own_inquiry_it_is_filed_there(self):
        lead, first = self.submitted()

        self.update(lead, student_name=self.student + ' junior')
        self.update(lead, class_id=CLASS_OTP_OFF_ALT)
        self.update(lead, details_confirmed=True)
        second = _row(lead)['RegistrationID']
        self.assertIsNotNone(second, 'the sibling was never registered')
        self.assertNotEqual(second, first)

        self.update(lead, promised_staff_followup='uniform size for the new child')
        self.assertIn('uniform', _open_notes(second))
        self.assertNotIn('uniform', _open_notes(first))


if __name__ == '__main__':
    unittest.main(verbosity=2)
