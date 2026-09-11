"""Every remaining inquiry field a parent could still move after submission.

The read-only guard covered student name, phone, email and class. Everything
else the agent can send went through silently - including the PARENT's own
name, which reaches CmpApplicantParentDetails at insert - while the assistant
told the parent the change had been made. Same shape as the phone and email
bugs, on the fields nobody had listed.

Three classes, deliberately treated differently:

  IDENTITY (parent name, location, relation, institution)
      value -> DIFFERENT value is refused; blank -> value is still collection,
      exactly as it is for phone, email and class. The agent re-sends the whole
      lead object every turn and the processor re-sends the routed institution
      on every web-chat turn, so a presence check would refuse every message.

  FORWARD COMMITMENTS (visit date/time, call-back date)
      presence is refused, because nothing syncs them anywhere: a visit booked
      in chat after submission sits in a write-only column no screen reads,
      while the parent has been told it is arranged.

  BOOKKEEPING (inquiry type, source, status)
      dropped SILENTLY - no notice, no follow-up row. The parent never sees
      these and never asks about them.

AI-derived metadata is deliberately NOT frozen: interest score/level/reason are
re-rated every turn by design and are what staff triage by.

    python -m unittest tests.integration.test_submitted_is_read_only -v
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


def _wide(conversation_id):
    """The columns the narrow helper does not read."""
    with connect() as cn:
        c = cn.cursor()
        c.execute("""SELECT ParentName, Location, RelationID, InstitutionID,
                            InquiryTypeID, SourceID, StatusID, TourDate, TourTime,
                            FollowupDate, InterestScore, InterestLevel, InterestReason
                       FROM dbo.CmpAgentConversation WHERE ConversationID = ?""",
                  conversation_id)
        r = c.fetchone()
        return dict(zip([d[0] for d in c.description], r))


def _followup_notes(registration_id):
    with connect() as cn:
        c = cn.cursor()
        c.execute("""SELECT ISNULL(ClientFeedBack,'') + ' || ' + ISNULL(Description,'')
                       FROM CmpInquiryFollowup
                      WHERE RegistrationID = ? AND ISNULL(IsFollowed,'N') = 'N'
                      ORDER BY InquiryFollowupID DESC""", registration_id)
        rows = c.fetchall()
        return '\n'.join(r[0] for r in rows)


class ReadOnlyCase(GateCase):
    """OTP off so a full submission happens without sending any mail."""

    institution = OTP_OFF
    klass = CLASS_OTP_OFF

    def submitted(self, **extra):
        """A chat with a created inquiry, optionally pre-loaded with fields."""
        lead = self.open_conversation()
        self.collect(lead, **extra)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        reg = _row(lead)['RegistrationID']
        self.assertIsNotNone(reg, 'the inquiry was never created')
        return lead, reg


# ── identity: a DIFFERENT value is refused ────────────────────────────────

class TestIdentityFieldsAreFrozen(ReadOnlyCase):

    def test_the_parent_name_cannot_be_changed(self):
        """Never guarded before, and it reaches the inquiry at insert."""
        lead, _ = self.submitted()
        before = _wide(lead)['ParentName']
        res = self.update(lead, name='itest SOMEONE ELSE %s' % self.tag)
        self.assertTrue(res.get('edit_refused'), 'the parent name changed silently')
        self.assertEqual(_wide(lead)['ParentName'], before)

    def test_the_location_cannot_be_changed(self):
        lead, _ = self.submitted(location='Kozhikode')
        res = self.update(lead, location='Feroke')
        self.assertTrue(res.get('edit_refused'))
        self.assertEqual(_wide(lead)['Location'], 'Kozhikode')

    def test_the_relation_cannot_be_changed(self):
        lead, _ = self.submitted(relation_id=1)
        res = self.update(lead, relation_id=2)
        self.assertTrue(res.get('edit_refused'))
        self.assertEqual(_wide(lead)['RelationID'], 1)

    def test_the_institution_cannot_be_changed(self):
        """Defence in depth - the processor overrides this before it arrives,
        but a different school here re-aims the duplicate guard."""
        lead, _ = self.submitted()
        res = self.update(lead, institution_id=1)
        self.assertTrue(res.get('edit_refused'))
        self.assertEqual(_wide(lead)['InstitutionID'], OTP_OFF)

    def test_every_refusal_reaches_the_staff_worklist(self):
        lead, reg = self.submitted(location='Kozhikode')
        self.update(lead, name='itest OTHER %s' % self.tag, location='Feroke')
        notes = _followup_notes(reg)
        self.assertIn('parent name', notes)
        self.assertIn('location', notes)


# ── identity: an unchanged resend must stay silent ────────────────────────

class TestAnIdenticalResendIsNotAChange(ReadOnlyCase):
    """The agent sends the whole lead object every turn. If that read as a
    change, the parent would be refused on every message they sent."""

    def test_resending_the_same_details_refuses_nothing(self):
        lead, _ = self.submitted(location='Kozhikode', relation_id=1)
        res = self.collect(lead, location='Kozhikode', relation_id=1)
        self.assertFalse(res.get('edit_refused'),
                         'an identical resend was treated as an edit')

    def test_the_routed_institution_is_resent_every_turn_without_refusal(self):
        lead, _ = self.submitted()
        for _ in range(3):
            res = self.update(lead, institution_id=OTP_OFF)
            self.assertFalse(res.get('edit_refused'),
                             'the routed institution read as a change')

    def test_a_blank_field_can_still_be_filled(self):
        """Blank-to-value is collection, not a change - the same rule the
        phone, email and class guards already follow."""
        lead, _ = self.submitted()                 # no location collected
        self.assertIsNone(_wide(lead)['Location'])
        res = self.update(lead, location='Feroke')
        self.assertFalse(res.get('edit_refused'), 'filling a blank was refused')
        self.assertEqual(_wide(lead)['Location'], 'Feroke')


# ── forward commitments: presence is refused ──────────────────────────────

class TestForwardCommitmentsAreRefused(ReadOnlyCase):

    def test_a_visit_date_is_not_accepted(self):
        lead, _ = self.submitted()
        res = self.update(lead, tour_date='2026-10-01', tour_time='10:00')
        self.assertTrue(res.get('edit_refused'), 'a visit was booked in chat')
        row = _wide(lead)
        self.assertIsNone(row['TourDate'])
        self.assertIsNone(row['TourTime'])

    def test_a_call_back_date_is_not_accepted(self):
        lead, _ = self.submitted()
        res = self.update(lead, followup_date='2026-10-05')
        self.assertTrue(res.get('edit_refused'))
        self.assertIsNone(_wide(lead)['FollowupDate'])

    def test_the_visit_request_reaches_staff(self):
        """Refusing is only half of it - staff must be able to schedule it."""
        lead, reg = self.submitted()
        self.update(lead, tour_date='2026-10-01')
        self.assertIn('visit date', _followup_notes(reg))

    def test_before_submission_a_visit_is_accepted_normally(self):
        """The freeze is post-submission only."""
        lead = self.open_conversation()
        self.collect(lead)
        res = self.update(lead, tour_date='2026-10-01', tour_time='10:00')
        self.assertFalse(res.get('edit_refused'))
        self.assertIsNotNone(_wide(lead)['TourDate'])


# ── bookkeeping: dropped, but silently ────────────────────────────────────

class TestBookkeepingIsDroppedQuietly(ReadOnlyCase):

    def test_source_status_and_type_do_not_change(self):
        lead, _ = self.submitted()
        before = _wide(lead)
        self.update(lead, source=9, status=9, inquiry_type_id=9)
        after = _wide(lead)
        for col in ('SourceID', 'StatusID', 'InquiryTypeID'):
            with self.subTest(col=col):
                self.assertEqual(after[col], before[col])

    def test_they_do_not_refuse_and_do_not_reach_staff(self):
        """A parent never asks to change the lead source. Telling them we
        refused, or filing it for staff, would both be nonsense."""
        lead, reg = self.submitted()
        res = self.update(lead, source=9, status=9, inquiry_type_id=9)
        self.assertFalse(res.get('edit_refused'),
                         'the parent was told we refused something they never asked')
        notes = _followup_notes(reg)
        for word in ('source', 'status', 'inquiry type'):
            self.assertNotIn(word, notes.lower(),
                             'bookkeeping noise reached the staff worklist')


# ── AI-derived metadata must stay live ────────────────────────────────────

class TestInterestStaysLive(ReadOnlyCase):
    """Deliberately NOT frozen: the prompt re-rates these every turn and they
    are what staff triage by. Freezing them would pin the score to whatever it
    was on the submission turn."""

    def test_the_score_still_moves_after_submission(self):
        lead, _ = self.submitted()
        self.update(lead, interest_score=90, interest_level='hot',
                    interest_reason='asked about documents after submitting')
        row = _wide(lead)
        self.assertEqual(row['InterestScore'], 90)
        self.assertEqual((row['InterestLevel'] or '').lower(), 'hot')
        self.assertIn('documents', row['InterestReason'] or '')

    def test_re_rating_does_not_count_as_an_edit(self):
        lead, _ = self.submitted()
        res = self.update(lead, interest_score=90, interest_level='hot',
                          interest_reason='still engaged')
        self.assertFalse(res.get('edit_refused'),
                         'a re-rating was refused as a field change')


# ── the flows this must not have broken ───────────────────────────────────

class TestTheExistingFlowsSurvive(ReadOnlyCase):

    def test_collection_before_submission_is_untouched(self):
        lead = self.open_conversation()
        res = self.collect(lead, location='Kozhikode', relation_id=1)
        self.assertFalse(res.get('edit_refused'))
        row = _wide(lead)
        self.assertEqual(row['Location'], 'Kozhikode')
        self.assertEqual(row['RelationID'], 1)

    def test_a_correction_before_submission_is_still_allowed(self):
        lead = self.open_conversation()
        self.collect(lead, location='Kozhikode')
        res = self.update(lead, location='Feroke')
        self.assertFalse(res.get('edit_refused'),
                         'a pre-submission change was refused')
        self.assertEqual(_wide(lead)['Location'], 'Feroke')

    def test_a_second_child_can_still_give_their_own_details(self):
        """A stale link is pre-submission for THAT child, so the guard must
        not fire - the sibling's relation and class are collected fresh."""
        lead, first = self.submitted(relation_id=1)
        self.update(lead, student_name=self.student + ' junior', relation_id=2)
        res = self.update(lead, class_id=CLASS_OTP_OFF_ALT, details_confirmed=True)
        self.assertFalse(res.get('edit_refused'),
                         'the sibling flow was blocked by the read-only guard')
        second = _row(lead)['RegistrationID']
        self.assertIsNotNone(second, 'the sibling was never registered')
        self.assertNotEqual(second, first)
        self.assertEqual(_wide(lead)['RelationID'], 2,
                         'the sibling could not give their own relation')

    def test_the_original_four_are_still_guarded(self):
        lead, _ = self.submitted()
        res = self.update(lead, email='itest.changed.%s@example.com' % self.tag)
        self.assertTrue(res.get('edit_refused'))
        self.assertNotIn('changed', _row(lead)['Email'] or '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
