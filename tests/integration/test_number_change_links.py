"""A parent who changes SIM must reach their child's inquiry, not a second one.

The duplicate guard matched on institution + year + student name + PHONE TAIL.
A parent who changes their WhatsApp number arrives as a brand new conversation
whose tail matches nothing, so the guard missed and a SECOND inquiry was created
for the same child. Staff then saw two rows, rang twice, and could not tell
which was current.

Email survives a SIM change. So a SECOND key is tried - only after the phone
tail misses, so the phone stays primary and there is never a question of which
match wins:

    1.  institution + year + student + phone tail
    2.  institution + year + student + email          (fallback)

Student name stays mandatory in BOTH. It is the only thing separating siblings,
who routinely share one family email - dropping it would fold a second child
into the first one's inquiry. Half these tests exist to prove it still does.

Linking is only half the job. The inquiry still holds the OLD number, and a
submitted inquiry is read-only to the chat, so nothing may correct it here.
What it can do is tell the people who are allowed to - otherwise staff open one
tidy inquiry and ring a number the parent no longer has.

    python -m unittest tests.integration.test_number_change_links -v
"""
import os
import random
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from db_conn import connect                               # noqa: E402
from test_confirmation_gate import (                      # noqa: E402
    GateCase, CLASS_OTP_OFF, CLASS_OTP_OFF_ALT, OTP_OFF, _row,
)


def _inquiry(registration_id):
    with connect() as cn:
        c = cn.cursor()
        c.execute("""SELECT StudentName, MobileNo, Email
                       FROM CmpRegistration WHERE RegistrationID = ?""", registration_id)
        r = c.fetchone()
        return dict(zip([d[0] for d in c.description], r)) if r else None


def _open_notes(registration_id):
    with connect() as cn:
        c = cn.cursor()
        c.execute("""SELECT ISNULL(ClientFeedBack,'') + ' || ' + ISNULL(Description,'')
                       FROM CmpInquiryFollowup
                      WHERE RegistrationID = ? AND ISNULL(IsFollowed,'N') = 'N'""",
                  registration_id)
        return '\n'.join(r[0] for r in c.fetchall())


def _inquiry_count(institution_id, student_name):
    with connect() as cn:
        c = cn.cursor()
        c.execute("""SELECT COUNT(*) FROM CmpRegistration
                      WHERE InstitutionID = ? AND RStatus = 'V'
                        AND UPPER(LTRIM(RTRIM(StudentName))) = ?""",
                  institution_id, student_name.strip().upper())
        return c.fetchone()[0]


class NumberChangeCase(GateCase):
    """OTP off so a whole submission happens without sending any mail."""

    institution = OTP_OFF
    klass = CLASS_OTP_OFF

    def setUp(self):
        super().setUp()
        self.email = 'itest.%s@example.com' % self.tag
        self.new_phone = '+9197%08d' % random.randrange(100000000)

    def submit_first(self):
        """The March inquiry: the parent's original number."""
        lead = self.open_conversation('itest-old-' + self.tag)
        self.collect(lead, email=self.email)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        reg = _row(lead)['RegistrationID']
        self.assertIsNotNone(reg, 'the first inquiry was never created')
        return lead, reg

    def second_chat(self, **overrides):
        """The June chat: new number, same child, same email."""
        lead = self.open_conversation('itest-new-' + self.tag)
        fields = {'name': 'itest parent %s' % self.tag,
                  'student_name': self.student,
                  'phone_number': self.new_phone,
                  'email': self.email,
                  'institution_id': self.institution}
        fields.update(overrides)
        self.update(lead, **fields)
        return lead, self.update(lead, class_id=self.klass)


# ── the gap being closed ──────────────────────────────────────────────────

class TestANewNumberReachesTheSameInquiry(NumberChangeCase):

    def test_the_second_chat_links_instead_of_duplicating(self):
        _, reg = self.submit_first()
        lead2, res = self.second_chat()
        self.assertTrue(res.get('already_registered'),
                        'the new number was not recognised as the same parent: %s' % res)
        self.assertEqual(_row(lead2)['RegistrationID'], reg,
                         'the second chat did not reach the existing inquiry')

    def test_no_second_inquiry_is_created(self):
        self.submit_first()
        lead2, _ = self.second_chat()
        self.update(lead2, details_confirmed=True)
        self.assertEqual(_inquiry_count(self.institution, self.student), 1,
                         'a duplicate inquiry was created for the same child')

    def test_the_phone_key_still_works_on_its_own(self):
        """The fallback must not have displaced the primary key."""
        _, reg = self.submit_first()
        lead2, res = self.second_chat(phone_number=self.phone)     # SAME number
        self.assertTrue(res.get('already_registered'))
        self.assertEqual(_row(lead2)['RegistrationID'], reg)

    def test_the_inquiry_keeps_its_original_number(self):
        """Linking must not rewrite a submitted inquiry."""
        _, reg = self.submit_first()
        before = _inquiry(reg)['MobileNo']
        self.second_chat()
        self.assertEqual(_inquiry(reg)['MobileNo'], before,
                         'the chat edited a submitted inquiry')


# ── the other half: staff are told ────────────────────────────────────────

class TestStaffAreToldTheNewNumber(NumberChangeCase):

    def test_a_note_reaches_the_staff_worklist(self):
        _, reg = self.submit_first()
        self.second_chat()
        notes = _open_notes(reg)
        self.assertIn(self.new_phone.lstrip('+')[-9:], notes.replace('+', ''),
                      'staff were never told the new number: %r' % notes)

    def test_the_note_names_the_number_on_the_inquiry_too(self):
        """Staff need both to know which to keep."""
        _, reg = self.submit_first()
        self.second_chat()
        notes = _open_notes(reg).replace('+', '')
        self.assertIn(self.phone.lstrip('+')[-9:], notes,
                      'the note did not say what the inquiry currently holds')

    def test_the_note_says_a_person_must_make_the_change(self):
        _, reg = self.submit_first()
        self.second_chat()
        self.assertIn('update the contact number', _open_notes(reg))

    def test_no_note_when_the_phone_matched(self):
        """Nothing changed, so there is nothing to tell anyone."""
        _, reg = self.submit_first()
        self.second_chat(phone_number=self.phone)
        notes = _open_notes(reg)
        self.assertNotIn('now messaging from', notes,
                         'a number-change note was filed for an unchanged number')

    def test_it_does_not_escalate(self):
        """A SIM change is not a request for a person."""
        _, reg = self.submit_first()
        lead2, _ = self.second_chat()
        with connect() as cn:
            c = cn.cursor()
            c.execute('SELECT Escalated FROM CmpAgentConversation WHERE ConversationID = ?',
                      lead2)
            self.assertFalse(bool(c.fetchone()[0]),
                             'a number change silenced the assistant')


# ── siblings must NOT be folded together ──────────────────────────────────

class TestSiblingsStaySeparate(NumberChangeCase):
    """The whole risk of adding email as a key. One family, one email, two
    children - the student name is all that keeps them apart."""

    def test_a_sibling_on_the_same_email_gets_their_own_inquiry(self):
        _, first = self.submit_first()

        # the sister: same parent, same email, even the same new number
        sister = self.student + ' sister'
        lead2 = self.open_conversation('itest-sis-' + self.tag)
        self.update(lead2, name='itest parent %s' % self.tag,
                    student_name=sister, phone_number=self.new_phone,
                    email=self.email, institution_id=self.institution)
        res = self.update(lead2, class_id=CLASS_OTP_OFF_ALT)
        self.assertFalse(res.get('already_registered'),
                         'the sister was matched to her brother by email')
        self.assertNotEqual(_row(lead2)['RegistrationID'], first)

        self.update(lead2, details_confirmed=True)
        second = _row(lead2)['RegistrationID']
        self.assertIsNotNone(second, 'the sibling was never registered')
        self.assertNotEqual(second, first,
                            'the sibling was folded into the first inquiry')

    def test_the_sibling_flow_in_one_chat_is_unaffected(self):
        lead, first = self.submit_first()
        self.update(lead, student_name=self.student + ' junior')
        self.update(lead, class_id=CLASS_OTP_OFF_ALT)
        self.update(lead, details_confirmed=True)
        second = _row(lead)['RegistrationID']
        self.assertIsNotNone(second)
        self.assertNotEqual(second, first,
                            'the second child reused the first inquiry')


# ── the parent name is what keeps two families apart ──────────────────────

class TestTwoFamiliesAreNotMerged(NumberChangeCase):
    """The whole risk of using email as a key.

    An address can be shared - a family address, a placeholder like
    abc@gmail.com, one typed by whoever filled the form - and a common child
    name does the rest. The parent's name is what tells two families apart, and
    it is exactly what does NOT change when a number does.

    Getting this wrong is not symmetric: binding two families into one inquiry
    means the second is never called, while refusing to bind costs a duplicate
    row and a note. These pin the safe direction.
    """

    def other_family(self, **overrides):
        """Same child name, same email, different parent and number."""
        lead = self.open_conversation('itest-other-' + self.tag)
        fields = {'name': 'itest SOMEONE ELSE %s' % self.tag,
                  'student_name': self.student,
                  'phone_number': self.new_phone,
                  'email': self.email,
                  'institution_id': self.institution}
        fields.update(overrides)
        self.update(lead, **fields)
        return lead, self.update(lead, class_id=self.klass)

    def test_a_different_parent_name_does_not_link(self):
        self.submit_first()
        lead2, res = self.other_family()
        self.assertFalse(res.get('already_registered'),
                         'two different families were bound to one inquiry')
        self.assertIsNone(_row(lead2)['RegistrationID'])

    def test_the_other_family_still_gets_their_own_inquiry(self):
        """The point of creating first and flagging second: nobody is left
        without an inquiry while a person decides."""
        _, first = self.submit_first()
        lead2, _ = self.other_family()
        self.update(lead2, details_confirmed=True)
        second = _row(lead2)['RegistrationID']
        self.assertIsNotNone(second, 'the second family was left with nothing')
        self.assertNotEqual(second, first)

    def test_staff_are_told_it_may_be_a_duplicate(self):
        _, first = self.submit_first()
        lead2, _ = self.other_family()
        self.update(lead2, details_confirmed=True)
        second = _row(lead2)['RegistrationID']
        notes = _open_notes(second)
        self.assertIn('duplicate', notes.lower(),
                      'the doubt was never written down: %r' % notes)
        self.assertIn(str(first), notes,
                      'the note does not say which inquiry to compare against')

    def test_the_note_names_the_other_parent(self):
        """Staff need enough to compare the two rows without hunting."""
        self.submit_first()
        lead2, _ = self.other_family()
        self.update(lead2, details_confirmed=True)
        # the CRM title-cases the stored parent name ("Itest Parent ..."), which
        # is exactly why SameParent compares case-insensitively
        notes = _open_notes(_row(lead2)['RegistrationID']).lower()
        self.assertIn(('itest parent %s' % self.tag).lower(), notes)

    def test_the_same_parent_still_links(self):
        """The narrowing must not have cost the case the key exists for."""
        _, reg = self.submit_first()
        lead2, res = self.second_chat()
        self.assertTrue(res.get('already_registered'),
                        'a genuine SIM change stopped being recognised')
        self.assertEqual(_row(lead2)['RegistrationID'], reg)

    def test_the_parent_name_match_ignores_case_and_spacing(self):
        _, reg = self.submit_first()
        lead2, res = self.second_chat(
            name='  ' + ('itest parent %s' % self.tag).upper() + ' ')
        self.assertTrue(res.get('already_registered'),
                        'the same parent in different case failed to match')
        self.assertEqual(_row(lead2)['RegistrationID'], reg)

    def test_a_stray_double_space_still_links(self):
        """The CRM collapses runs of spaces as it title-cases, so a parent who
        mistyped one would otherwise look like a different family."""
        _, reg = self.submit_first()
        lead2, res = self.second_chat(
            name='itest  parent  %s' % self.tag)          # doubled spaces
        self.assertTrue(res.get('already_registered'),
                        'a mistyped space made the same parent look like another')
        self.assertEqual(_row(lead2)['RegistrationID'], reg)

    def test_spacing_and_case_together_still_link(self):
        _, reg = self.submit_first()
        lead2, res = self.second_chat(
            name='  ITEST   PARENT   %s  ' % self.tag.upper())
        self.assertTrue(res.get('already_registered'))
        self.assertEqual(_row(lead2)['RegistrationID'], reg)

    def test_a_genuinely_different_name_is_still_rejected(self):
        """Normalising whitespace must not have loosened the check itself."""
        self.submit_first()
        lead2, res = self.second_chat(name='itest  someone  else  %s' % self.tag)
        self.assertFalse(res.get('already_registered'),
                         'whitespace normalisation started matching strangers')

    def test_a_blank_parent_name_never_links(self):
        """Belt and braces: a chat with no parent name never reaches the key at
        all, because `namesDistinct` gates both call sites first. Pinned here so
        that guard cannot quietly go away and leave '' matching ''."""
        self.submit_first()
        lead2 = self.open_conversation('itest-blank-' + self.tag)
        self.update(lead2, student_name=self.student, phone_number=self.new_phone,
                    email=self.email, institution_id=self.institution)
        res = self.update(lead2, class_id=self.klass)
        self.assertFalse(res.get('already_registered'),
                         'a conversation with no parent name was linked')


# ── the email key must not match on nothing ───────────────────────────────

class TestTheEmailKeyIsNarrow(NumberChangeCase):

    def test_a_different_email_does_not_match(self):
        self.submit_first()
        lead2, res = self.second_chat(email='itest.other.%s@example.com' % self.tag)
        self.assertFalse(res.get('already_registered'),
                         'a different email matched')
        self.assertIsNone(_row(lead2)['RegistrationID'])

    def test_the_match_is_case_and_space_insensitive(self):
        """Parents type their address differently every time."""
        _, reg = self.submit_first()
        lead2, res = self.second_chat(email='  ' + self.email.upper() + ' ')
        self.assertTrue(res.get('already_registered'),
                        'the same address in different case failed to match')
        self.assertEqual(_row(lead2)['RegistrationID'], reg)

    def test_a_different_student_does_not_match(self):
        self.submit_first()
        lead2, res = self.second_chat(student_name='itest someone %s' % self.tag)
        self.assertFalse(res.get('already_registered'))
        self.assertIsNone(_row(lead2)['RegistrationID'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
