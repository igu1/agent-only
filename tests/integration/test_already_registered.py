"""A chat must not announce that it found its own inquiry.

Where the institution has OTP off, promotion never sets OtpVerified, so the
early-link branch - gated on `!OtpVerified` - keeps running after submission.
It shares its duplicate guard with promotion (institution + phone tail +
student name), so on the very next turn it matched the row the SAME chat had
just created, re-linked the conversation to it, and returned
already_registered=true. The parent, one turn after "your details have been
submitted", was told:

    "Good news - this student's inquiry is already registered with us, so no
     further verification is needed."

as though a second chat had beaten them to it.

The other half matters just as much: a DIFFERENT existing inquiry - the parent
really did come through another chat or the public form - must still link,
still skip the code, and still say so. These two cases are the whole fix.

    python -m unittest tests.integration.test_already_registered -v
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from test_confirmation_gate import (          # noqa: E402
    GateCase, CLASS_OTP_OFF, OTP_OFF, _row,
)


class AlreadyRegisteredCase(GateCase):
    """OTP off on purpose: that is the posture where OtpVerified stays false
    forever, which is what let the branch re-fire. No code is sent, so no mail
    is either."""

    institution = OTP_OFF
    klass = CLASS_OTP_OFF

    def submit(self, lead):
        """Take one chat all the way to a created inquiry."""
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        reg = _row(lead)['RegistrationID']
        self.assertIsNotNone(reg, 'the inquiry was never created')
        return reg

    def reach_the_link_check(self, lead):
        """Everything the early-link branch requires, and nothing more.

        The class is part of that set - the branch will not look at all until
        ClassID > 0 - but the confirmation is NOT: linking to an inquiry that
        already exists is not a promotion, so it must happen without one.
        """
        self.collect(lead)
        return self.update(lead, class_id=self.klass)


# ── 1. the same inquiry, the same chat ────────────────────────────────────

class TestAChatDoesNotFindItself(AlreadyRegisteredCase):

    def test_the_turn_after_submission_does_not_claim_already_registered(self):
        """The exact reproduction: submit, then say anything at all."""
        lead = self.open_conversation()
        reg = self.submit(lead)

        res = self.update(lead, interest_score=80, interest_level='hot',
                          interest_reason='asked about documents')
        self.assertFalse(res.get('already_registered'),
                         'the chat announced its OWN inquiry as a duplicate')
        self.assertEqual(_row(lead)['RegistrationID'], reg,
                         'the link changed on a turn that carried no details')

    def test_it_stays_quiet_for_several_turns(self):
        """The branch runs on EVERY later turn while OtpVerified is false, so
        one clean turn does not prove it is closed."""
        lead = self.open_conversation()
        reg = self.submit(lead)
        for turn in range(4):
            res = self.update(lead, interest_score=70 + turn, interest_level='hot',
                              interest_reason='turn %d' % turn)
            self.assertFalse(res.get('already_registered'),
                             'turn %d announced the chat\'s own inquiry' % turn)
        self.assertEqual(_row(lead)['RegistrationID'], reg)

    def test_it_stays_quiet_when_the_parent_asks_a_question(self):
        """A question turn still carries the re-rated interest fields, so it
        still reaches /leads/update/ and still runs the branch."""
        lead = self.open_conversation()
        self.submit(lead)
        res = self.update(lead, interest_score=85, interest_level='hot',
                          interest_reason='asked about bus routes')
        self.assertFalse(res.get('already_registered'))

    def test_no_duplicate_inquiry_is_created(self):
        lead = self.open_conversation()
        reg = self.submit(lead)
        for _ in range(3):
            self.update(lead, interest_score=75, interest_level='hot',
                        interest_reason='still talking')
        self.assertEqual(_row(lead)['RegistrationID'], reg,
                         'a second inquiry appeared for the same student')


# ── 2. a genuinely different inquiry ──────────────────────────────────────

class TestARealDuplicateStillLinks(AlreadyRegisteredCase):
    """The behaviour the guard must NOT have cost: the same parent and student
    arriving in a SECOND chat, whose own RegistrationID is still null."""

    def test_a_second_chat_links_to_the_first_chats_inquiry(self):
        first = self.open_conversation('itest-A-' + self.tag)
        reg = self.submit(first)

        # a second chat, same parent, same student, same number - a different
        # browser session, or the other channel
        second = self.open_conversation('itest-B-' + self.tag)
        self.assertIsNone(_row(second)['RegistrationID'],
                          'the new chat started already linked')
        res = self.reach_the_link_check(second)
        self.assertTrue(res.get('already_registered'),
                        'the real duplicate stopped being detected: %s' % res)
        self.assertEqual(_row(second)['RegistrationID'], reg,
                         'the second chat did not link to the existing inquiry')

    def test_the_second_chat_skips_the_code(self):
        """Linking is what makes verification unnecessary - that must survive."""
        first = self.open_conversation('itest-A-' + self.tag)
        self.submit(first)
        second = self.open_conversation('itest-B-' + self.tag)
        self.reach_the_link_check(second)
        row = _row(second)
        self.assertTrue(bool(row['OtpVerified']),
                        'the linked chat was left unverified')
        self.assertIsNone(row['OtpCode'], 'a code was issued to a linked chat')

    def test_it_is_announced_exactly_once(self):
        """already_registered drives a deterministic one-time notice; repeating
        it every turn would be the same bug wearing the other face."""
        first = self.open_conversation('itest-A-' + self.tag)
        self.submit(first)
        second = self.open_conversation('itest-B-' + self.tag)
        self.assertTrue(self.reach_the_link_check(second).get('already_registered'))
        res = self.update(second, interest_score=60, interest_level='warm',
                          interest_reason='linked, now chatting')
        self.assertFalse(res.get('already_registered'),
                         'the notice repeated on a later turn')

    def test_no_second_inquiry_is_created_for_the_linked_chat(self):
        first = self.open_conversation('itest-A-' + self.tag)
        reg = self.submit(first)
        second = self.open_conversation('itest-B-' + self.tag)
        self.reach_the_link_check(second)
        self.update(second, details_confirmed=True)
        self.assertEqual(_row(second)['RegistrationID'], reg,
                         'the linked chat created a duplicate inquiry')


# ── 3. the sibling flow must be untouched ─────────────────────────────────

class TestTheSiblingFlowIsUnaffected(AlreadyRegisteredCase):
    """The guard compares against the conversation's CURRENT RegistrationID,
    which during a second child still points at the first. A sibling search
    looks for a different student, so it can never match that row - but the
    flow is close enough to the change to be worth pinning."""

    def test_a_second_child_still_reaches_its_own_inquiry(self):
        lead = self.open_conversation()
        first = self.submit(lead)

        self.update(lead, student_name=self.student + ' junior')
        self.update(lead, class_id=CLASS_OTP_OFF)
        res = self.update(lead, details_confirmed=True)
        second = _row(lead)['RegistrationID']
        self.assertIsNotNone(second, 'the sibling was never registered: %s' % res)
        self.assertNotEqual(second, first,
                            'the sibling reused the first child\'s inquiry')


if __name__ == '__main__':
    unittest.main(verbosity=2)
