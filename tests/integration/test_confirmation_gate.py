"""The DetailsConfirmed gate, against the REAL gateway and the REAL database.

These drive `/api/agent/v1/leads/update/` directly instead of chatting, because
the rule under test is a SERVER rule: an inquiry must not be created until the
parent has confirmed, NO MATTER WHAT THE MODEL SENDS. A test that went through
the model would be testing the prompt, which is exactly the thing the gate
exists to stop trusting.

They need infrastructure (a running API on 5200, TestAzhar reachable), so they
live outside the unit suite and are run by name:

    python -m unittest tests.integration.test_confirmation_gate -v

Isolation: every test opens its own conversation and invents its own student
name and phone, because the gateway's duplicate guard matches on
institution + phone tail + student name - shared details would make one test
link to another's inquiry instead of exercising its own path. Rows left behind
are identifiable: chat ids and student names start with 'itest'.
"""
import datetime
import io
import json
import os
import random
import sys
import unittest
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from db_conn import api_token, connect, database_name   # noqa: E402

API_BASE = os.getenv('AGENT_API_BASE', 'http://localhost:5200').rstrip('/')
APPSETTINGS_PATH = os.getenv(
    'ITEST_APPSETTINGS',
    r'D:\Campus_Angular_Project\OnlineCampusAngular\server\src\OnlineCampus.Api\appsettings.json')

# channel 1 is the company-level web chat (institution chosen in the chat);
# channel 2 is WhatsApp, bound to institution 1
WEBCHAT_CHANNEL = os.getenv('ITEST_WEBCHAT_CHANNEL', '1')
WHATSAPP_CHANNEL = os.getenv('ITEST_WHATSAPP_CHANNEL', '2')

# both OTP postures need covering: the flag is the LAST gate where OTP is off,
# and the gate BEFORE the code where it is on
OTP_ON = int(os.getenv('ITEST_INSTITUTION_OTP_ON', '1'))       # IsOTPForInquiry = Y
OTP_OFF = int(os.getenv('ITEST_INSTITUTION_OTP_OFF', '2'))     # = N
CLASS_OTP_ON = int(os.getenv('ITEST_CLASS_OTP_ON', '26'))      # institution 1, "7"
CLASS_OTP_ON_ALT = int(os.getenv('ITEST_CLASS_OTP_ON_ALT', '27'))     # "8"
CLASS_OTP_OFF = int(os.getenv('ITEST_CLASS_OTP_OFF', '44'))    # institution 2, "Grade 1"
CLASS_OTP_OFF_ALT = int(os.getenv('ITEST_CLASS_OTP_OFF_ALT', '45'))   # "Grade 2"


def _post(path, payload):
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        API_BASE + path, data=body, method='POST',
        headers={'Content-Type': 'application/json', 'X-Agent-Token': api_token()})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode('utf-8') or '{}')
    except urllib.error.URLError as e:
        reader = getattr(e, 'read', None)
        detail = ' - ' + reader().decode('utf-8', 'replace')[:300] if reader else ''
        raise AssertionError('POST %s failed: %s%s' % (path, e, detail))


def _row(conversation_id):
    with connect() as cn:
        cur = cn.cursor()
        cur.execute("""SELECT ClassID, RegistrationID, OtpCode, OtpVerified,
                              DetailsConfirmed, StudentName, Email, Phone
                         FROM dbo.CmpAgentConversation WHERE ConversationID = ?""",
                    conversation_id)
        r = cur.fetchone()
        if r is None:
            raise AssertionError('conversation %s not found' % conversation_id)
        return dict(zip([d[0] for d in cur.description], r))


class GateCase(unittest.TestCase):
    """One fresh conversation per test, with details unique to that test."""

    channel_type = 'webchat'
    channel_id = WEBCHAT_CHANNEL
    institution = OTP_ON
    klass = CLASS_OTP_ON

    @classmethod
    def setUpClass(cls):
        # fail loudly on a misconfigured run rather than reporting green
        try:
            _post('/api/agent/v1/inbound/', {'channel_type': 'webchat', 'channel_id': '0',
                                             'chat_id': '', 'text': ''})
        except AssertionError as e:
            raise unittest.SkipTest('gateway not reachable at %s (%s)' % (API_BASE, e))
        cls.database = database_name()

    def setUp(self):
        self.tag = '%s%05d' % (self.id().rsplit('.', 1)[-1][:20], random.randrange(100000))
        self.student = 'itest %s' % self.tag
        self.phone = '+9198%08d' % random.randrange(100000000)

    def open_conversation(self, chat_id=None):
        chat_id = chat_id or ('itest-' + self.tag)
        res = _post('/api/agent/v1/inbound/', {
            'channel_type': self.channel_type,
            'channel_id': self.channel_id,
            'chat_id': chat_id,
            'message_id': chat_id + '-m1',
            'text': 'hello',
            'has_media': False,
        })
        self.assertTrue(res.get('lead_id'), 'inbound returned no lead_id: %s' % res)
        return int(res['lead_id'])

    def update(self, lead, **fields):
        payload = {'lead_id': lead}
        payload.update(fields)
        return _post('/api/agent/v1/leads/update/', payload)

    def collect(self, lead, **overrides):
        """Everything the promotion gate requires EXCEPT the confirmation."""
        fields = {
            'name': 'itest parent %s' % self.tag,
            'student_name': self.student,
            'phone_number': self.phone,
            'email': 'itest.%s@example.com' % self.tag,
            'institution_id': self.institution,
        }
        fields.update(overrides)
        return self.update(lead, **fields)

    def assertNothingCreated(self, lead, why):
        row = _row(lead)
        self.assertIsNone(row['RegistrationID'], why)
        return row


# -- the core rule ----------------------------------------------------------

class TestCompleteDetailsAreNotAConfirmation(GateCase):
    """Before this gate, a full set of fields WAS the trigger: the inquiry was
    created the moment a class arrived, with nothing read back to anyone."""

    institution = OTP_OFF
    klass = CLASS_OTP_OFF

    def test_full_details_without_confirmation_create_nothing(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        row = self.assertNothingCreated(lead, 'inquiry created without a confirmation')
        self.assertFalse(bool(row['DetailsConfirmed']))
        self.assertEqual(row['ClassID'], self.klass,
                         'the class must still be recorded - a held inquiry is '
                         'still a lead staff can follow up')

    def test_an_absent_flag_fails_closed(self):
        """null must mean unconfirmed, never 'assume yes'."""
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)          # field simply not sent
        self.assertNothingCreated(lead, 'a missing flag was read as consent')

    def test_confirmation_creates_the_inquiry(self):
        """The other half: the gate must not block a genuine confirmation."""
        lead = self.open_conversation()
        self.collect(lead)
        res = self.update(lead, class_id=self.klass, details_confirmed=True)
        row = _row(lead)
        self.assertTrue(bool(row['DetailsConfirmed']))
        self.assertIsNotNone(row['RegistrationID'],
                             'a confirmed inquiry was not created: %s' % res)


class TestClassIdBeforeConfirmation(GateCase):
    """THE DANGEROUS CASE the gate exists for: the model sets the class - the
    field that used to trigger creation - before the parent has agreed."""

    institution = OTP_ON
    klass = CLASS_OTP_ON

    def test_early_class_with_an_explicit_false_sends_no_code_and_creates_nothing(self):
        lead = self.open_conversation()
        self.collect(lead)
        res = self.update(lead, class_id=self.klass, details_confirmed=False)
        row = _row(lead)
        self.assertIsNone(row['RegistrationID'], 'registration created before confirmation')
        self.assertIsNone(row['OtpCode'], 'a verification code was sent before confirmation')
        self.assertFalse(res.get('otp_required'),
                         'the parent was asked for a code they had not agreed to')

    def test_early_class_with_no_flag_at_all_is_equally_blocked(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        row = _row(lead)
        self.assertIsNone(row['RegistrationID'])
        self.assertIsNone(row['OtpCode'])


# -- ordering: confirm, THEN verify, THEN create ----------------------------

class TestConfirmationThenOtp(GateCase):
    """collect -> summary -> confirm -> code -> verify -> create."""

    institution = OTP_ON
    klass = CLASS_OTP_ON

    def test_confirmation_opens_the_otp_but_creates_nothing_yet(self):
        lead = self.open_conversation()
        self.collect(lead)
        res = self.update(lead, class_id=self.klass, details_confirmed=True)
        row = _row(lead)
        self.assertTrue(bool(row['DetailsConfirmed']))
        self.assertIsNotNone(row['OtpCode'], 'confirmation did not open verification')
        self.assertTrue(res.get('otp_required'))
        self.assertIsNone(row['RegistrationID'], 'created before the code was verified')

    def test_the_verified_code_creates_the_inquiry(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        code = _row(lead)['OtpCode']
        self.assertIsNotNone(code, 'no code to verify')
        res = self.update(lead, otp_code=code)
        row = _row(lead)
        self.assertTrue(bool(row['OtpVerified']))
        self.assertIsNotNone(row['RegistrationID'],
                             'a verified code did not create the inquiry: %s' % res)

    def test_a_silent_turn_does_not_revoke_the_confirmation(self):
        """null means 'the model said nothing', not 'the parent withdrew' -
        otherwise the OTP turn itself would undo the confirmation."""
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        self.update(lead, interest_score=80)            # carries no flag
        self.assertTrue(bool(_row(lead)['DetailsConfirmed']))

    def test_the_summary_is_not_read_back_twice(self):
        """The parent just confirmed those five fields in the agent's summary;
        the OTP notice repeating them is the duplicate the flow removes."""
        lead = self.open_conversation()
        self.collect(lead)
        res = self.update(lead, class_id=self.klass, details_confirmed=True)
        self.assertTrue(res.get('otp_required'))
        self.assertIsNone(res.get('otp_summary'),
                          'the OTP notice repeated the summary the parent just confirmed')


# -- a correction is not a confirmation -------------------------------------

class TestMaterialChangeResets(GateCase):
    """Reset must win over a confirmation claimed on the same turn: a parent
    cannot have agreed to a detail they were simultaneously correcting."""

    institution = OTP_ON
    klass = CLASS_OTP_ON

    def test_a_change_after_confirming_blocks_creation_until_confirmed_again(self):
        """The full journey the gate is for: confirm, change your mind, and the
        code you already hold must not be enough to register the new details."""
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        code = _row(lead)['OtpCode']
        self.assertIsNotNone(code)

        # the parent corrects the class on the next turn
        self.update(lead, class_id=CLASS_OTP_ON_ALT)
        row = _row(lead)
        self.assertFalse(bool(row['DetailsConfirmed']),
                         'a material change left the confirmation standing')

        # the outstanding code must not now register the CHANGED details
        self.update(lead, otp_code=code)
        self.assertNothingCreated(
            lead, 'changed details were registered without a new confirmation')

        # confirming again completes it
        self.update(lead, details_confirmed=True)
        code2 = _row(lead)['OtpCode']
        self.assertIsNotNone(code2, 'no code after re-confirmation')
        self.update(lead, otp_code=code2)
        row = _row(lead)
        self.assertIsNotNone(row['RegistrationID'], 're-confirmation never completed')
        self.assertEqual(row['ClassID'], CLASS_OTP_ON_ALT, 'the corrected class was lost')

    def test_a_same_turn_change_beats_a_same_turn_confirmation(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        self.update(lead, class_id=CLASS_OTP_ON_ALT, details_confirmed=True)
        row = _row(lead)
        self.assertFalse(bool(row['DetailsConfirmed']),
                         'the flag survived a change made on the same turn')
        self.assertIsNone(row['RegistrationID'])

    def test_a_new_student_name_resets_it(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        self.update(lead, student_name=self.student + ' two')
        self.assertFalse(bool(_row(lead)['DetailsConfirmed']))

    def test_a_new_parent_name_resets_it(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        self.update(lead, name='itest other parent %s' % self.tag)
        self.assertFalse(bool(_row(lead)['DetailsConfirmed']))

    def test_a_changed_email_resets_both_the_flag_and_the_verification(self):
        """Proof belongs to a contact: a new address is unproven, and the
        details it will receive are unconfirmed again."""
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        self.assertIsNotNone(_row(lead)['OtpCode'])
        self.update(lead, email='itest.%s.new@example.com' % self.tag, details_confirmed=True)
        row = _row(lead)
        self.assertFalse(bool(row['DetailsConfirmed']))
        self.assertFalse(bool(row['OtpVerified']))
        self.assertIsNone(row['RegistrationID'])

    def test_an_unchanged_resend_is_not_a_change(self):
        """The agent re-sends the whole lead object every turn - identical
        values must not be mistaken for corrections, or nothing would ever
        stay confirmed long enough to be created."""
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        self.collect(lead)                        # same values again, no flag
        self.assertTrue(bool(_row(lead)['DetailsConfirmed']),
                        'an unchanged resend was treated as a correction')


class TestOtpOffInstitution(GateCase):
    """Where no code is sent, the confirmation is the ONLY review there is."""

    institution = OTP_OFF
    klass = CLASS_OTP_OFF

    def test_a_change_on_the_confirming_turn_still_blocks_creation(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        self.update(lead, class_id=CLASS_OTP_OFF_ALT, details_confirmed=True)
        self.assertNothingCreated(lead, 'a corrected class registered itself immediately')

    def test_confirming_the_corrected_details_creates_it(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        self.update(lead, class_id=CLASS_OTP_OFF_ALT, details_confirmed=True)
        res = self.update(lead, details_confirmed=True)
        row = _row(lead)
        self.assertIsNotNone(row['RegistrationID'], 'never completed: %s' % res)
        self.assertEqual(row['ClassID'], CLASS_OTP_OFF_ALT)


class TestWhatsAppPhoneStaysChannelProven(GateCase):
    """The number the parent is messaging FROM needs no code - the gate must
    not have re-opened that question."""

    channel_type = 'whatsapp'
    channel_id = WHATSAPP_CHANNEL
    institution = OTP_ON
    klass = CLASS_OTP_ON

    def test_the_code_targets_the_email_only(self):
        chat = '9198%08d' % random.randrange(100000000)
        lead = self.open_conversation(chat_id=chat)
        seeded = _row(lead)['Phone']
        self.assertTrue(seeded, 'WhatsApp did not seed the sender number')
        self.collect(lead, phone_number=seeded)
        res = self.update(lead, class_id=self.klass, details_confirmed=True)
        self.assertTrue(res.get('otp_required'))
        self.assertEqual(res.get('otp_target'), 'email',
                         'WhatsApp asked to re-verify a channel-proven phone')


class TestSiblingStillCompletes(GateCase):
    """Re-promotion runs through the same gate, so the second child must still
    reach a record of its own - the flag must not have turned the sibling flow
    into a dead end."""

    institution = OTP_OFF
    klass = CLASS_OTP_OFF

    def test_a_second_child_gets_its_own_inquiry(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        first = _row(lead)['RegistrationID']
        self.assertIsNotNone(first, 'the first child was never registered')

        # a new child in the same chat: the parent, phone and email carry over
        self.update(lead, student_name=self.student + ' junior')
        row = _row(lead)
        self.assertFalse(bool(row['DetailsConfirmed']),
                         'the new child inherited the previous confirmation')
        self.assertIsNone(row['ClassID'], 'the class carried over to the new child')

        self.update(lead, class_id=CLASS_OTP_OFF_ALT)
        self.assertEqual(_row(lead)['RegistrationID'], first,
                         'the second child was registered before confirming')

        res = self.update(lead, details_confirmed=True)
        second = _row(lead)['RegistrationID']
        self.assertIsNotNone(second, 'the sibling never completed: %s' % res)
        self.assertNotEqual(second, first, 'the sibling reused the first inquiry')

    def test_a_second_child_at_an_otp_school_reuses_the_verification(self):
        """Verification belongs to the contacts, which have not changed - so
        the confirmation is the only gate the sibling has to pass."""
        lead = self.open_conversation()
        self.collect(lead, institution_id=OTP_ON)
        self.update(lead, class_id=CLASS_OTP_ON, details_confirmed=True)
        code = _row(lead)['OtpCode']
        self.assertIsNotNone(code)
        self.update(lead, otp_code=code)
        first = _row(lead)['RegistrationID']
        self.assertIsNotNone(first)

        self.update(lead, student_name=self.student + ' junior')
        self.update(lead, class_id=CLASS_OTP_ON_ALT)
        self.assertEqual(_row(lead)['RegistrationID'], first,
                         'the sibling registered before confirming')

        res = self.update(lead, details_confirmed=True)
        row = _row(lead)
        self.assertIsNone(res.get('otp_required'), 'the sibling was asked to verify again')
        self.assertNotEqual(row['RegistrationID'], first,
                            'the sibling never got its own inquiry: %s' % res)


class TestSiblingContactChange(GateCase):
    """A second child's contact is COLLECTION, not an edit to the first.

    The conversation still carries child #1's RegistrationID while the chat has
    moved to child #2, so the read-only policy used to fire on the sibling's
    own email and phone - refusing one loudly against the wrong child, and
    dropping the other in silence.
    """

    channel_type = 'whatsapp'
    channel_id = WHATSAPP_CHANNEL
    institution = OTP_ON
    klass = CLASS_OTP_ON

    def first_child(self):
        """A completed, verified inquiry - the state the bug needs."""
        chat = '9198%08d' % random.randrange(100000000)
        lead = self.open_conversation(chat_id=chat)
        proven = _row(lead)['Phone']
        self.collect(lead, phone_number=proven)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        code = _row(lead)['OtpCode']
        self.assertIsNotNone(code, 'no code for the first child')
        self.update(lead, otp_code=code)
        first = _row(lead)['RegistrationID']
        self.assertIsNotNone(first, 'the first child was never registered')
        return lead, first, proven

    # 1
    def test_a_siblings_new_email_is_stored(self):
        lead, first, _ = self.first_child()
        self.update(lead, student_name=self.student + ' two')
        new_email = 'itest.%s.sib@example.com' % self.tag
        self.update(lead, class_id=CLASS_OTP_ON_ALT, email=new_email)
        self.assertEqual((_row(lead)['Email'] or '').lower(), new_email,
                         'the sibling email was discarded as an edit to child #1')

    # 2
    def test_a_siblings_new_email_clears_verification_and_confirmation(self):
        """The contact changes BEFORE the sibling is registered - that is the
        live ordering. (Changing it AFTER is a real post-submission edit, and
        stays refused; test_a_genuine_post_submission_edit_is_still_refused
        covers that.)"""
        lead, first, _ = self.first_child()
        self.update(lead, student_name=self.student + ' two')
        self.update(lead, class_id=CLASS_OTP_ON_ALT)
        self.update(lead, email='itest.%s.sib@example.com' % self.tag,
                    details_confirmed=True)
        row = _row(lead)
        self.assertFalse(bool(row['OtpVerified']),
                         'an unproven address inherited the first child\'s proof')
        self.assertFalse(bool(row['DetailsConfirmed']))
        self.assertEqual(row['RegistrationID'], first, 'created before re-confirming')

    # 3
    def test_a_siblings_new_email_does_not_escalate(self):
        """Escalation silences the agent for the rest of the chat, so a wrong
        one strands the parent mid-flow."""
        lead, _, _ = self.first_child()
        self.update(lead, student_name=self.student + ' two')
        res = self.update(lead, class_id=CLASS_OTP_ON_ALT,
                          email='itest.%s.sib@example.com' % self.tag)
        self.assertFalse(res.get('edit_refused'),
                         'the sibling flow was refused as a post-submission edit')
        with connect() as cn:
            cur = cn.cursor()
            cur.execute('SELECT Escalated FROM dbo.CmpAgentConversation WHERE ConversationID = ?',
                        lead)
            self.assertFalse(bool(cur.fetchone()[0]), 'the conversation was escalated')

    # 4
    def test_the_new_address_gets_its_own_code_and_completes(self):
        lead, first, _ = self.first_child()
        self.update(lead, student_name=self.student + ' two')
        self.update(lead, class_id=CLASS_OTP_ON_ALT,
                    email='itest.%s.sib@example.com' % self.tag)
        res = self.update(lead, details_confirmed=True)
        code = _row(lead)['OtpCode']
        self.assertIsNotNone(code, 'no code for the new address: %s' % res)
        self.assertTrue(res.get('otp_required'))
        self.update(lead, otp_code=code)
        row = _row(lead)
        self.assertTrue(bool(row['OtpVerified']))
        self.assertNotEqual(row['RegistrationID'], first,
                            'the sibling never got its own inquiry')

    # 5
    def test_a_siblings_new_phone_is_stored(self):
        lead, first, proven = self.first_child()
        self.update(lead, student_name=self.student + ' two')
        other = '+919000%06d' % random.randrange(1000000)
        self.update(lead, class_id=CLASS_OTP_ON_ALT, phone_number=other)
        stored = _row(lead)['Phone']
        self.assertNotEqual(stored, proven,
                            'the typed number was silently discarded')
        self.assertEqual(''.join(c for c in (stored or '') if c.isdigit()),
                         ''.join(c for c in other if c.isdigit()))

    # 6
    def test_a_typed_whatsapp_number_is_not_channel_proven(self):
        """Proof belongs to the number they message FROM. A different number
        typed into the chat has none, even on WhatsApp."""
        lead, first, _ = self.first_child()
        self.update(lead, student_name=self.student + ' two')
        self.update(lead, class_id=CLASS_OTP_ON_ALT,
                    phone_number='+919000%06d' % random.randrange(1000000))
        row = _row(lead)
        self.assertFalse(bool(row['OtpVerified']),
                         'a typed number inherited the channel proof')
        self.assertEqual(row['RegistrationID'], first, 'created without re-verifying')

    # 7
    def test_the_same_number_in_another_shape_is_not_a_change(self):
        """The country code goes missing when the model re-extracts it. That is
        the same number, so nothing resets and nobody is told anything."""
        chat = '9198%08d' % random.randrange(100000000)
        lead = self.open_conversation(chat_id=chat)
        proven = _row(lead)['Phone']
        self.collect(lead, phone_number=proven)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        code = _row(lead)['OtpCode']
        self.update(lead, otp_code=code)
        self.assertTrue(bool(_row(lead)['OtpVerified']))

        bare = chat[2:]                      # same number, country code dropped
        res = self.update(lead, phone_number=bare)
        row = _row(lead)
        self.assertTrue(bool(row['OtpVerified']),
                        'a re-extraction of the proven number reset verification')
        self.assertFalse(res.get('edit_refused'),
                         'a re-extraction produced a refusal notice')
        self.assertEqual(row['Phone'], proven, 'the stored number was rewritten')

    # 8
    def test_a_genuine_post_submission_edit_is_still_refused(self):
        """The policy must not be weakened: SAME child, current year, after
        submission - that is a real edit and the chat may not apply it.

        It no longer escalates, though. Refusing is the policy working, not a
        reason to shut the assistant down for the rest of the conversation -
        see TestARefusedEditDoesNotEscalate.
        """
        lead, first, _ = self.first_child()
        before = _row(lead)['Email']
        res = self.update(lead, email='itest.%s.later@example.com' % self.tag)
        row = _row(lead)
        self.assertTrue(res.get('edit_refused'), 'a real post-submission edit slipped through')
        self.assertEqual(row['Email'], before, 'the refused email was written anyway')
        self.assertEqual(row['RegistrationID'], first)

    # 9
    def test_a_valid_link_still_blocks_a_class_edit_for_the_same_child(self):
        """The stale-link relaxation must not leak into the same-child case:
        with the link valid, a changed class is still an edit."""
        lead, first, _ = self.first_child()
        res = self.update(lead, class_id=CLASS_OTP_ON_ALT)
        row = _row(lead)
        self.assertTrue(res.get('edit_refused'))
        self.assertEqual(row['ClassID'], self.klass, 'the class was edited after submission')
        self.assertEqual(row['RegistrationID'], first, 'a duplicate inquiry was spawned')

    # 10
    def test_a_contact_is_never_accepted_silently_and_then_dropped(self):
        """THE INVARIANT behind both live failures. Whenever the turn was not
        refused, every contact it carried must be in the row afterwards - the
        agent is told nothing else, so anything dropped quietly becomes a
        promise to the parent that the record does not keep."""
        lead, _, proven = self.first_child()
        self.update(lead, student_name=self.student + ' two')
        email = 'itest.%s.inv@example.com' % self.tag
        phone = '+919000%06d' % random.randrange(1000000)
        res = self.update(lead, class_id=CLASS_OTP_ON_ALT, email=email, phone_number=phone)
        if not res.get('edit_refused'):
            row = _row(lead)
            self.assertEqual((row['Email'] or '').lower(), email,
                             'email accepted without a notice, then dropped')
            self.assertEqual(''.join(c for c in (row['Phone'] or '') if c.isdigit()),
                             ''.join(c for c in phone if c.isdigit()),
                             'phone accepted without a notice, then dropped')


class TestStaleYearStaysSafe(GateCase):
    """A returning parent whose link went stale with the SEASON, not the child:
    nothing may re-promote until they restate and confirm."""

    institution = OTP_OFF
    klass = CLASS_OTP_OFF

    def test_a_quiet_turn_on_a_promoted_chat_creates_nothing(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        first = _row(lead)['RegistrationID']
        self.assertIsNotNone(first)
        self.update(lead, interest_score=70)
        self.assertEqual(_row(lead)['RegistrationID'], first,
                         'an idle turn re-promoted the conversation')


def _resend_count(conversation_id):
    with connect() as cn:
        cur = cn.cursor()
        cur.execute('SELECT OtpResendCount FROM dbo.CmpAgentConversation WHERE ConversationID = ?',
                    conversation_id)
        return cur.fetchone()[0]


def _age_the_code(conversation_id, seconds):
    """Move the current code's issue time back, by pulling its expiry back.

    The cooldown is derived from expiry minus the validity window, so this is
    the honest way to simulate waiting without sleeping through the test.
    """
    with connect() as cn:
        cur = cn.cursor()
        cur.execute('''UPDATE dbo.CmpAgentConversation
                          SET OtpExpiresAt = DATEADD(second, -?, OtpExpiresAt)
                        WHERE ConversationID = ?''', seconds, conversation_id)
        cn.commit()


def _escalated(lead):
    with connect() as cn:
        cur = cn.cursor()
        cur.execute('SELECT Escalated FROM dbo.CmpAgentConversation WHERE ConversationID = ?',
                    lead)
        return bool(cur.fetchone()[0])


class TestARefusedEditDoesNotEscalate(GateCase):
    """Escalation stops the assistant for the rest of the conversation. That is
    right when a person has taken over or when the inquiry could not be
    created - not when the read-only policy simply did its job. A parent fixing
    a typo should not lose the ability to ask about fees or another child.
    """

    institution = OTP_OFF
    klass = CLASS_OTP_OFF

    def submitted(self):
        """A completed inquiry with a VALID link - edits here are real edits."""
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        reg = _row(lead)['RegistrationID']
        self.assertIsNotNone(reg, 'setup failed: nothing was registered')
        self.assertFalse(_escalated(lead), 'escalated before the edit was even tried')
        return lead, reg

    # 1
    def test_a_refused_email_edit_does_not_escalate(self):
        lead, reg = self.submitted()
        before = _row(lead)['Email']
        res = self.update(lead, email='itest.%s.changed@example.com' % self.tag)
        self.assertTrue(res.get('edit_refused'), 'the edit was not refused')      # 5
        self.assertEqual(_row(lead)['Email'], before, 'the refused email was written')  # 6
        self.assertFalse(_escalated(lead), 'a refused email edit escalated')

    # 2
    def test_a_refused_phone_edit_does_not_escalate(self):
        lead, reg = self.submitted()
        before = _row(lead)['Phone']
        res = self.update(lead, phone_number='+919000%06d' % random.randrange(1000000))
        self.assertTrue(res.get('edit_refused'))
        self.assertEqual(_row(lead)['Phone'], before, 'the refused phone was written')
        self.assertFalse(_escalated(lead), 'a refused phone edit escalated')

    # 3
    def test_a_refused_class_edit_does_not_escalate(self):
        lead, reg = self.submitted()
        res = self.update(lead, class_id=CLASS_OTP_OFF_ALT)
        self.assertTrue(res.get('edit_refused'))
        self.assertEqual(_row(lead)['ClassID'], self.klass, 'the refused class was written')
        self.assertEqual(_row(lead)['RegistrationID'], reg, 'a duplicate inquiry was spawned')
        self.assertFalse(_escalated(lead), 'a refused class edit escalated')

    # 4
    def test_a_refused_student_name_correction_does_not_escalate(self):
        lead, reg = self.submitted()
        before = _row(lead)['StudentName']
        res = self.update(lead, student_name=self.student + ' fixed',
                          student_name_correction=True)
        self.assertTrue(res.get('edit_refused'))
        self.assertEqual(_row(lead)['StudentName'], before,
                         'the refused name correction was written')
        self.assertFalse(_escalated(lead), 'a refused name correction escalated')

    def test_the_conversation_stays_open_to_the_ai(self):
        """The whole point: after a refusal the backend must still tell the
        agent this chat is live, or the parent is talking to nothing."""
        lead, reg = self.submitted()
        self.update(lead, email='itest.%s.changed@example.com' % self.tag)
        res = _post('/api/agent/v1/inbound/', {
            'channel_type': self.channel_type, 'channel_id': self.channel_id,
            'chat_id': 'itest-' + self.tag, 'message_id': 'itest-%s-m2' % self.tag,
            'text': 'what are the fees?', 'has_media': False})
        self.assertFalse(res.get('ignore'), 'the chat was ignored after a refusal')
        self.assertTrue(res.get('ai_enabled'), 'the AI was disabled by a refusal')
        self.assertFalse(res.get('escalated'))


class TestRealFailuresStillEscalate(GateCase):
    """The two cases where a person genuinely must take over stay untouched."""

    institution = OTP_OFF

    # 9
    def test_a_failed_promotion_still_escalates(self):
        """A class the school cannot register (here: one belonging to another
        institution) makes UspInsert_CmpInquiry throw. Only staff can finish
        that inquiry, so the assistant must stand down."""
        lead = self.open_conversation()
        self.collect(lead)
        res = self.update(lead, class_id=CLASS_OTP_ON, details_confirmed=True)
        self.assertTrue(res.get('promotion_failed'),
                        'the setup no longer reproduces a failed promotion')
        self.assertIsNone(_row(lead)['RegistrationID'])
        self.assertTrue(_escalated(lead), 'a failed promotion no longer escalates')

    # 10
    def test_the_escalation_endpoint_still_escalates(self):
        """The model-driven path: escalation:true reaches the gateway here."""
        lead = self.open_conversation()
        self.assertFalse(_escalated(lead))
        _post('/api/agent/v1/escalation/',
              {'lead_id': lead, 'message': 'connecting you with our team'})
        self.assertTrue(_escalated(lead), 'the escalation endpoint stopped escalating')


def _followups(registration_id):
    """Open and closed follow-ups on an inquiry, newest first."""
    with connect() as cn:
        cur = cn.cursor()
        cur.execute("""SELECT InquiryFollowupID, FollowupType, ContactPerson, ClientFeedBack,
                              Description, IsFollowed, NextFollowupDate, RStatus
                         FROM dbo.CmpInquiryFollowup
                        WHERE RegistrationID = ?
                        ORDER BY InquiryFollowupID DESC""", registration_id)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _open_followups(registration_id):
    return [f for f in _followups(registration_id)
            if (f['IsFollowed'] or 'N') == 'N' and (f['RStatus'] or 'V') == 'V']


def _registration(registration_id):
    with connect() as cn:
        cur = cn.cursor()
        cur.execute("""SELECT StudentName, MobileNo, Email, ClassID
                         FROM dbo.CmpRegistration WHERE RegistrationID = ?""", registration_id)
        r = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], r)) if r else {}


class TestCorrectionRequestReachesStaff(GateCase):
    """A submitted inquiry cannot be edited in chat, so the parent is told to
    raise it with admissions - which they can only do if somebody knows they
    asked. The request lands in the callback list staff already work from."""

    institution = OTP_OFF
    klass = CLASS_OTP_OFF

    def submitted(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        reg = _row(lead)['RegistrationID']
        self.assertIsNotNone(reg, 'setup failed: nothing was registered')
        # a fresh inquiry has no follow-ups yet
        self.assertEqual(_followups(reg), [], 'setup: inquiry already had follow-ups')
        return lead, reg

    def only_followup(self, reg, why):
        opens = _open_followups(reg)
        self.assertEqual(len(opens), 1, '%s (found %d open)' % (why, len(opens)))
        return opens[0]

    # 1
    def test_a_phone_correction_creates_a_follow_up(self):
        lead, reg = self.submitted()
        res = self.update(lead, phone_number='+919000%06d' % random.randrange(1000000))
        self.assertTrue(res.get('edit_refused'))
        f = self.only_followup(reg, 'a phone correction produced no single follow-up')
        self.assertIn('phone', (f['ClientFeedBack'] or '').lower())
        self.assertIn('Correction requested', f['ClientFeedBack'] or '')

    # 2
    def test_an_email_correction_creates_a_follow_up(self):
        lead, reg = self.submitted()
        res = self.update(lead, email='itest.%s.fix@example.com' % self.tag)
        self.assertTrue(res.get('edit_refused'))
        f = self.only_followup(reg, 'an email correction produced no single follow-up')
        self.assertIn('email', (f['ClientFeedBack'] or '').lower())
        self.assertIn('itest.%s.fix@example.com' % self.tag, f['Description'] or '',
                      'the requested value was not recorded for staff')

    # 3
    def test_a_student_name_correction_creates_a_follow_up(self):
        lead, reg = self.submitted()
        res = self.update(lead, student_name=self.student + ' fixed',
                          student_name_correction=True)
        self.assertTrue(res.get('edit_refused'))
        f = self.only_followup(reg, 'a name correction produced no single follow-up')
        self.assertIn('student name', (f['ClientFeedBack'] or '').lower())

    # 4
    def test_a_class_correction_creates_a_follow_up(self):
        lead, reg = self.submitted()
        res = self.update(lead, class_id=CLASS_OTP_OFF_ALT)
        self.assertTrue(res.get('edit_refused'))
        f = self.only_followup(reg, 'a class correction produced no single follow-up')
        self.assertIn('class', (f['ClientFeedBack'] or '').lower())

    # 5
    def test_the_submitted_inquiry_details_are_untouched(self):
        lead, reg = self.submitted()
        before = _registration(reg)
        self.update(lead, email='itest.%s.fix@example.com' % self.tag)
        self.update(lead, phone_number='+919000%06d' % random.randrange(1000000))
        after = _registration(reg)
        self.assertEqual(after.get('StudentName'), before.get('StudentName'))
        self.assertEqual(after.get('MobileNo'), before.get('MobileNo'))
        self.assertEqual(after.get('Email'), before.get('Email'))
        self.assertEqual(after.get('ClassID'), before.get('ClassID'))

    # 6
    def test_the_conversation_is_not_escalated(self):
        lead, reg = self.submitted()
        self.update(lead, email='itest.%s.fix@example.com' % self.tag)
        self.assertFalse(_escalated(lead),
                         'a correction request escalated and would stop the AI')

    # 7
    def test_the_chat_stays_open_to_the_ai(self):
        lead, reg = self.submitted()
        self.update(lead, email='itest.%s.fix@example.com' % self.tag)
        res = _post('/api/agent/v1/inbound/', {
            'channel_type': self.channel_type, 'channel_id': self.channel_id,
            'chat_id': 'itest-' + self.tag, 'message_id': 'itest-%s-m9' % self.tag,
            'text': 'what are the school timings?', 'has_media': False})
        self.assertFalse(res.get('ignore'))
        self.assertTrue(res.get('ai_enabled'))

    # 8
    def test_repeat_requests_do_not_pile_up_open_follow_ups(self):
        """One open follow-up per inquiry - and the later request is appended,
        so staff see the whole story on the row they are already holding."""
        lead, reg = self.submitted()
        self.update(lead, email='itest.%s.one@example.com' % self.tag)
        first = self.only_followup(reg, 'first request')
        self.update(lead, phone_number='+919000%06d' % random.randrange(1000000))
        second = self.only_followup(reg, 'a second request opened another follow-up')
        self.assertEqual(second['InquiryFollowupID'], first['InquiryFollowupID'],
                         'the second request replaced the follow-up instead of appending')
        self.assertIn('phone', (second['ClientFeedBack'] or '').lower())
        self.assertIn('itest.%s.one@example.com' % self.tag, second['Description'] or '',
                      'appending lost the earlier request')

    # 9
    def test_it_works_with_autofollowup_disabled(self):
        """AgentGateway:AutoFollowup is false in this environment. A correction
        is not a courtesy follow-up and must not depend on that switch."""
        with io.open(APPSETTINGS_PATH, encoding='utf-8-sig') as fh:
            cfg = json.load(fh)
        self.assertFalse((cfg.get('AgentGateway') or {}).get('AutoFollowup'),
                         'this test only proves something while AutoFollowup is off')
        lead, reg = self.submitted()
        self.update(lead, email='itest.%s.off@example.com' % self.tag)
        self.only_followup(reg, 'AutoFollowup=false suppressed the correction follow-up')

    # 10
    def test_staff_can_close_it_the_normal_way(self):
        """Completion is the existing mechanism: the next follow-up submitted on
        that inquiry marks earlier ones followed. After that a fresh correction
        opens a new one rather than reopening the closed row."""
        lead, reg = self.submitted()
        self.update(lead, email='itest.%s.close@example.com' % self.tag)
        original = self.only_followup(reg, 'first request')

        with connect() as cn:                      # staff complete it
            cur = cn.cursor()
            cur.execute("""UPDATE dbo.CmpInquiryFollowup SET IsFollowed = 'Y'
                            WHERE InquiryFollowupID = ?""", original['InquiryFollowupID'])
            cn.commit()
        self.assertEqual(_open_followups(reg), [], 'the follow-up did not close')

        self.update(lead, phone_number='+919000%06d' % random.randrange(1000000))
        reopened = self.only_followup(reg, 'a later request did not open a new follow-up')
        self.assertNotEqual(reopened['InquiryFollowupID'], original['InquiryFollowupID'],
                            'the closed follow-up was reused')

    def test_the_follow_up_is_dated_for_today(self):
        """The staff worklist filters on NextFollowupDate <= the date being
        viewed, so a future date would hide the request."""
        lead, reg = self.submitted()
        self.update(lead, email='itest.%s.date@example.com' % self.tag)
        f = self.only_followup(reg, 'no follow-up')
        self.assertIsNotNone(f['NextFollowupDate'])
        self.assertLessEqual(f['NextFollowupDate'].date(), datetime.date.today())

    def test_it_uses_an_existing_follow_up_type(self):
        """The type dropdown is a closed vocabulary and is required when staff
        open the row; an unknown code would leave it unselectable."""
        lead, reg = self.submitted()
        self.update(lead, email='itest.%s.type@example.com' % self.tag)
        f = self.only_followup(reg, 'no follow-up')
        self.assertIn(f['FollowupType'], ('M', 'E', 'P', 'D', 'O'))

    def test_no_follow_up_for_a_pre_submission_correction(self):
        """Before submission a correction is just collection - it is applied,
        not refused, so nothing should reach the callback list."""
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        self.update(lead, email='itest.%s.pre@example.com' % self.tag)
        self.update(lead, details_confirmed=True)
        reg = _row(lead)['RegistrationID']
        self.assertIsNotNone(reg)
        self.assertEqual(_followups(reg), [],
                         'a pre-submission correction created a staff callback')


class TestPhoneMustBeStructurallyValid(GateCase):
    """A typed phone is half the key that decides whether a conversation
    belongs to an existing inquiry, and both matching queries take the last
    Min(9, len) digits. Too few digits makes a weak key, and a weak key can
    bind a chat to somebody else's registration."""

    institution = OTP_OFF
    klass = CLASS_OTP_OFF

    def collected_with(self, phone):
        """A conversation carrying everything but a confirmation."""
        lead = self.open_conversation()
        self.collect(lead, phone_number=phone)
        return lead, self.update(lead, class_id=self.klass)

    # 1
    def test_seven_digits_is_accepted(self):
        lead, _ = self.collected_with('1234567')
        self.assertEqual(_row(lead)['Phone'], '1234567',
                         'the documented floor was rejected')

    # 2
    def test_fifteen_digits_is_accepted(self):
        lead, _ = self.collected_with('+' + '1' * 15)
        self.assertIsNotNone(_row(lead)['Phone'], 'the E.164 maximum was rejected')

    # 3
    def test_six_digits_is_rejected(self):
        lead = self.open_conversation()
        res = self.collect(lead, phone_number='123456')
        self.assertTrue(res.get('phone_invalid'), 'six digits was accepted')
        self.assertIsNone(_row(lead)['Phone'], 'a rejected number was stored')

    # 4
    def test_sixteen_digits_is_rejected(self):
        lead = self.open_conversation()
        res = self.collect(lead, phone_number='1' * 16)
        self.assertTrue(res.get('phone_invalid'), 'sixteen digits was accepted')
        self.assertIsNone(_row(lead)['Phone'])

    # 5
    def test_separators_and_spaces_are_stripped_before_counting(self):
        lead, _ = self.collected_with('+91 98765 43210')
        self.assertIsNotNone(_row(lead)['Phone'],
                             'a normally formatted number was rejected')

    def test_a_formatted_number_that_is_still_too_short_is_rejected(self):
        """Punctuation must not pad a number up to the floor."""
        lead = self.open_conversation()
        res = self.collect(lead, phone_number='+1 (23) 45-6')
        self.assertTrue(res.get('phone_invalid'))
        self.assertIsNone(_row(lead)['Phone'])

    # 6
    def test_a_rejected_number_never_reaches_the_conversation_row(self):
        lead = self.open_conversation()
        self.collect(lead, phone_number='12345')
        self.assertIsNone(_row(lead)['Phone'])

    def test_a_rejected_number_does_not_overwrite_a_good_one(self):
        lead, _ = self.collected_with('+919876543210')
        good = _row(lead)['Phone']
        self.update(lead, phone_number='12345')
        self.assertEqual(_row(lead)['Phone'], good,
                         'a bad number erased a number already on the record')

    # 7 + the promotion half of 8
    def test_a_rejected_number_cannot_create_an_inquiry(self):
        """No phone means the promotion gate never opens, so nothing can be
        written to CmpRegistration with an impossible number."""
        lead = self.open_conversation()
        self.collect(lead, phone_number='12345')
        self.update(lead, class_id=self.klass, details_confirmed=True)
        self.assertIsNone(_row(lead)['RegistrationID'],
                          'an inquiry was created with an unusable number')

    # 8 - THE ONE THIS RULE EXISTS FOR
    def test_a_short_phone_cannot_bind_this_chat_to_another_registration(self):
        """Build the collision deliberately: one family registers with a number
        ENDING in the digits a second family then mistypes, same institution,
        same year, same student name - every other part of the matching key.

        The early-link path would relink the second conversation to the first
        family's registration and force OtpVerified true. The floor is what
        stops the weak key existing at all.
        """
        shared_student = 'itest shared %s' % self.tag
        victim_phone = '+91987612345'          # ends in 12345

        victim = self.open_conversation()
        self.collect(victim, phone_number=victim_phone, student_name=shared_student)
        self.update(victim, class_id=self.klass, details_confirmed=True)
        victim_reg = _row(victim)['RegistrationID']
        self.assertIsNotNone(victim_reg, 'setup failed: no registration to collide with')

        # a different family, same student name, mistypes just the tail
        other = self.open_conversation(chat_id='itest-other-' + self.tag)
        res = self.collect(other, phone_number='12345', student_name=shared_student)
        self.update(other, class_id=self.klass, details_confirmed=True)
        row = _row(other)

        self.assertTrue(res.get('phone_invalid'))
        self.assertIsNone(row['Phone'], 'the short number was stored')
        self.assertIsNone(row['RegistrationID'],
                          'the chat was bound to another family\'s registration')
        self.assertNotEqual(row['RegistrationID'], victim_reg)
        self.assertFalse(bool(row['OtpVerified']),
                         'verification was forced on by an early link')

    # 9
    def test_the_flag_is_returned_to_the_agent(self):
        lead = self.open_conversation()
        res = self.collect(lead, phone_number='12345')
        self.assertIs(res.get('phone_invalid'), True)

    def test_a_valid_number_does_not_raise_the_flag(self):
        lead = self.open_conversation()
        res = self.collect(lead, phone_number='+919876543210')
        self.assertFalse(res.get('phone_invalid'))

    # 12
    def test_a_rejected_number_does_not_revoke_a_confirmation(self):
        """A typo is not a correction: it must not look like a material change."""
        lead, _ = self.collected_with('+919876543210')
        self.update(lead, details_confirmed=True)
        # already created at an OTP-off school, so use a fresh unconfirmed one
        lead2 = self.open_conversation(chat_id='itest-conf-' + self.tag)
        self.collect(lead2, phone_number='+919876543211',
                     student_name=self.student + ' two')
        self.update(lead2, class_id=self.klass)
        self.update(lead2, details_confirmed=True)
        before = _row(lead2)
        self.update(lead2, phone_number='12345')
        after = _row(lead2)
        self.assertEqual(bool(after['DetailsConfirmed']), bool(before['DetailsConfirmed']),
                         'a rejected number reset the confirmation')

    def test_a_rejected_number_does_not_clear_verification(self):
        lead = self.open_conversation(chat_id='itest-otp-' + self.tag)
        self.collect(lead, institution_id=OTP_ON)
        self.update(lead, class_id=CLASS_OTP_ON, details_confirmed=True)
        code = _row(lead)['OtpCode']
        self.assertIsNotNone(code)
        self.update(lead, otp_code=code)
        self.assertTrue(bool(_row(lead)['OtpVerified']))
        self.update(lead, phone_number='12345')
        self.assertTrue(bool(_row(lead)['OtpVerified']),
                        'a rejected number cleared a completed verification')

    def test_a_rejected_number_creates_no_correction_follow_up(self):
        """The parent did not state a usable change, so staff get no callback."""
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        reg = _row(lead)['RegistrationID']
        self.assertIsNotNone(reg)
        res = self.update(lead, phone_number='12345')
        self.assertTrue(res.get('phone_invalid'))
        self.assertFalse(res.get('edit_refused'),
                         'a typo was recorded as a correction request')
        self.assertEqual(_followups(reg), [])

    # 13
    def test_the_valid_phone_otp_flow_is_unchanged(self):
        lead = self.open_conversation()
        self.collect(lead, institution_id=OTP_ON, phone_number='+919876543210')
        self.update(lead, class_id=CLASS_OTP_ON, details_confirmed=True)
        code = _row(lead)['OtpCode']
        self.assertIsNotNone(code, 'a valid number no longer opens verification')
        self.update(lead, otp_code=code)
        row = _row(lead)
        self.assertTrue(bool(row['OtpVerified']))
        self.assertIsNotNone(row['RegistrationID'])


class TestWhatsAppSeedingIsUnaffected(GateCase):
    """The sender id comes from Meta with its own >= 8 floor and is not typed."""

    channel_type = 'whatsapp'
    channel_id = WHATSAPP_CHANNEL
    institution = OTP_ON
    klass = CLASS_OTP_ON

    def test_the_seeded_number_still_completes_an_inquiry(self):
        chat = '9198%08d' % random.randrange(100000000)
        lead = self.open_conversation(chat_id=chat)
        seeded = _row(lead)['Phone']
        self.assertTrue(seeded, 'WhatsApp seeding stopped working')
        self.collect(lead, phone_number=seeded)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        code = _row(lead)['OtpCode']
        self.assertIsNotNone(code)
        self.update(lead, otp_code=code)
        self.assertIsNotNone(_row(lead)['RegistrationID'])


class TestOtpResend(GateCase):
    """A parent whose code never arrived could not ask for another one: the
    turn carried no digits, so the server said nothing at all."""

    institution = OTP_ON
    klass = CLASS_OTP_ON

    def awaiting_code(self):
        """Confirmed details, a live code, nothing registered."""
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass, details_confirmed=True)
        row = _row(lead)
        self.assertIsNotNone(row['OtpCode'], 'setup failed: no code issued')
        self.assertEqual(_resend_count(lead), 0)
        return lead, row['OtpCode']

    def resend(self, lead):
        return self.update(lead, otp_resend_requested=True)

    # 1, 2
    def test_a_resend_issues_a_different_code(self):
        lead, first = self.awaiting_code()
        _age_the_code(lead, 61)
        res = self.resend(lead)
        self.assertEqual(res.get('otp_notice'), 'resent', res)
        second = _row(lead)['OtpCode']
        self.assertIsNotNone(second)
        self.assertNotEqual(second, first, 'the same code was re-sent')

    # 3, 20
    def test_the_old_code_stops_working(self):
        lead, first = self.awaiting_code()
        _age_the_code(lead, 61)
        self.resend(lead)
        res = self.update(lead, otp_code=first)
        row = _row(lead)
        self.assertFalse(bool(row['OtpVerified']), 'the superseded code still verified')
        self.assertIsNone(row['RegistrationID'], 'the old code created an inquiry')
        self.assertEqual(res.get('otp_notice'), 'retry')

    # 4, 5
    def test_the_new_code_verifies_and_only_then_creates_the_inquiry(self):
        lead, _ = self.awaiting_code()
        _age_the_code(lead, 61)
        self.resend(lead)
        second = _row(lead)['OtpCode']
        self.assertIsNone(_row(lead)['RegistrationID'], 'resend created an inquiry')
        self.update(lead, otp_code=second)
        row = _row(lead)
        self.assertTrue(bool(row['OtpVerified']))
        self.assertIsNotNone(row['RegistrationID'])

    # 6, 7, 8, 9, 10
    def test_a_resend_changes_nothing_else(self):
        lead, _ = self.awaiting_code()
        _age_the_code(lead, 61)
        before = _row(lead)
        self.resend(lead)
        after = _row(lead)
        self.assertEqual(after['StudentName'], before['StudentName'])
        self.assertEqual(after['Email'], before['Email'])
        self.assertEqual(after['Phone'], before['Phone'])
        self.assertEqual(after['ClassID'], before['ClassID'])
        self.assertTrue(bool(after['DetailsConfirmed']), 'confirmation was lost')
        self.assertFalse(bool(after['OtpVerified']))
        self.assertIsNone(after['RegistrationID'])
        self.assertFalse(_escalated(lead), 'a resend escalated')

    # 11
    def test_a_second_request_inside_the_cooldown_sends_nothing(self):
        lead, _ = self.awaiting_code()
        _age_the_code(lead, 61)
        self.resend(lead)
        live = _row(lead)['OtpCode']
        res = self.resend(lead)
        self.assertEqual(res.get('otp_notice'), 'resend_too_soon', res)
        self.assertEqual(_row(lead)['OtpCode'], live,
                         'a refused resend still replaced the code')
        self.assertEqual(_resend_count(lead), 1, 'a refused resend was counted')

    def test_asking_seconds_after_the_first_code_is_refused(self):
        """The cooldown runs from the last code ISSUE, not the last resend, so
        a parent who asks moments after the first code is told to wait rather
        than handed a second one. The mail is very likely still in flight."""
        lead, first = self.awaiting_code()
        res = self.resend(lead)
        self.assertEqual(res.get('otp_notice'), 'resend_too_soon', res)
        self.assertEqual(_row(lead)['OtpCode'], first, 'a code was issued anyway')
        self.assertEqual(_resend_count(lead), 0)

    def test_the_cooldown_expires(self):
        lead, _ = self.awaiting_code()
        _age_the_code(lead, 61)
        self.resend(lead)
        first = _row(lead)['OtpCode']
        _age_the_code(lead, 61)
        res = self.resend(lead)
        self.assertEqual(res.get('otp_notice'), 'resent', res)
        self.assertNotEqual(_row(lead)['OtpCode'], first)

    # 12, 13
    def test_the_allowance_runs_out_at_three(self):
        lead, _ = self.awaiting_code()
        for i in range(3):
            _age_the_code(lead, 61)
            self.assertEqual(self.resend(lead).get('otp_notice'), 'resent',
                             'resend %d was refused' % (i + 1))
        self.assertEqual(_resend_count(lead), 3)
        held = _row(lead)['OtpCode']

        _age_the_code(lead, 61)
        res = self.resend(lead)
        self.assertEqual(res.get('otp_notice'), 'resend_limit', res)
        self.assertEqual(_row(lead)['OtpCode'], held,
                         'a fourth resend issued a code anyway')
        self.assertEqual(_resend_count(lead), 3)

    def test_letting_the_code_expire_does_not_restore_the_allowance(self):
        """THE BYPASS: waiting five minutes reissues automatically. If that
        reset the counter, the limit would be worth nothing."""
        lead, _ = self.awaiting_code()
        for _ in range(3):
            _age_the_code(lead, 61)
            self.resend(lead)
        self.assertEqual(_resend_count(lead), 3)

        _age_the_code(lead, 6 * 60)                 # push it past expiry
        res = self.update(lead, otp_code='000000')  # wrong code on a dead one
        self.assertIn(res.get('otp_notice'), ('retry', 'new'))
        self.assertEqual(_resend_count(lead), 3,
                         'expiry reissue reset the resend allowance')
        _age_the_code(lead, 61)
        self.assertEqual(self.resend(lead).get('otp_notice'), 'resend_limit')

    def test_three_wrong_guesses_do_not_restore_the_allowance(self):
        """The other bypass: the automatic 'renewed' reissue."""
        lead, _ = self.awaiting_code()
        for _ in range(3):
            _age_the_code(lead, 61)
            self.resend(lead)
        self.assertEqual(_resend_count(lead), 3)

        for _ in range(3):
            self.update(lead, otp_code='000000')
        self.assertEqual(_resend_count(lead), 3,
                         'the wrong-attempt renewal reset the resend allowance')
        _age_the_code(lead, 61)
        self.assertEqual(self.resend(lead).get('otp_notice'), 'resend_limit')

    def test_a_contact_change_starts_a_fresh_allowance(self):
        """The one genuine new cycle: a changed email clears verification."""
        lead, _ = self.awaiting_code()
        for _ in range(3):
            _age_the_code(lead, 61)
            self.resend(lead)
        self.assertEqual(_resend_count(lead), 3)

        self.update(lead, email='itest.%s.new@example.com' % self.tag)
        self.assertEqual(_resend_count(lead), 0, 'a new cycle kept the old count')
        self.update(lead, details_confirmed=True)
        _age_the_code(lead, 61)
        self.assertEqual(self.resend(lead).get('otp_notice'), 'resent')

    # 15
    def test_expiry_reissue_is_unchanged(self):
        lead, first = self.awaiting_code()
        _age_the_code(lead, 6 * 60)
        res = self.update(lead, otp_code='000000')
        self.assertIn(res.get('otp_notice'), ('retry', 'new'))
        self.assertNotEqual(_row(lead)['OtpCode'], first, 'expiry stopped reissuing')

    # 16
    def test_wrong_attempt_renewal_is_unchanged(self):
        lead, first = self.awaiting_code()
        self.assertEqual(self.update(lead, otp_code='000000').get('otp_notice'), 'retry')
        self.assertEqual(self.update(lead, otp_code='000000').get('otp_notice'), 'retry')
        res = self.update(lead, otp_code='000000')
        self.assertEqual(res.get('otp_notice'), 'renewed')
        self.assertNotEqual(_row(lead)['OtpCode'], first)

    # 17
    def test_an_ordinary_turn_leaves_the_code_alone(self):
        """A question is not a resend request - nothing is issued, and the
        server stays silent so the model's answer is the only message."""
        lead, first = self.awaiting_code()
        res = self.update(lead, interest_score=70)
        self.assertIsNone(res.get('otp_notice'))
        self.assertEqual(_row(lead)['OtpCode'], first)
        self.assertEqual(_resend_count(lead), 0)

    # 18
    def test_a_changed_email_still_re_verifies(self):
        lead, first = self.awaiting_code()
        self.update(lead, email='itest.%s.chg@example.com' % self.tag)
        row = _row(lead)
        self.assertFalse(bool(row['OtpVerified']))
        self.assertFalse(bool(row['DetailsConfirmed']))

    # 19
    def test_the_resend_uses_the_same_target(self):
        lead, _ = self.awaiting_code()
        _age_the_code(lead, 61)
        res = self.resend(lead)
        self.assertEqual(res.get('otp_target'), 'both',
                         'web chat target changed')

    def test_a_typed_code_wins_over_a_resend_request_on_the_same_turn(self):
        lead, first = self.awaiting_code()
        res = self.update(lead, otp_code=first, otp_resend_requested=True)
        row = _row(lead)
        self.assertTrue(bool(row['OtpVerified']), 'the code was ignored: %s' % res)
        self.assertEqual(_resend_count(lead), 0, 'a verified turn also resent')

    def test_a_resend_before_confirmation_does_nothing(self):
        """The OTP block is gated on DetailsConfirmed, so there is no code to
        resend and nothing to report."""
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        res = self.resend(lead)
        self.assertIsNone(res.get('otp_notice'))
        self.assertIsNone(_row(lead)['OtpCode'])
        self.assertEqual(_resend_count(lead), 0)


class TestResendAtAnOtpOffSchool(GateCase):
    """No code is ever issued there, so a resend request is inert."""

    institution = OTP_OFF
    klass = CLASS_OTP_OFF

    def test_a_resend_request_creates_no_code(self):
        lead = self.open_conversation()
        self.collect(lead)
        self.update(lead, class_id=self.klass)
        res = self.update(lead, otp_resend_requested=True)
        self.assertIsNone(res.get('otp_notice'))
        self.assertIsNone(_row(lead)['OtpCode'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
