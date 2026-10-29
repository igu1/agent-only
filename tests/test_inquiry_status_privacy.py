"""Inquiry status may only reach the family it belongs to.

Status rows carry a child's name, class, school and admission stage. A mobile
number is not a secret, so on the website chat anyone could type a neighbour's
number and read their child's details. The CRM now decides (WhatsApp = the
sender's own number; elsewhere mobile AND email must sit on one inquiry); these
tests pin the agent's half: what it sends, and that a guessing chat is stopped.

    python -m unittest discover -s tests -p "test_inquiry_status_privacy.py"
"""
import asyncio
import unittest
from unittest.mock import patch

import anyio

import infra.tenants as T
import tools.business_info as B
from tools.turn_context import current_turn, set_turn


class StatusCase(unittest.TestCase):

    def setUp(self):
        self.addCleanup(T._current_org.reset, T._current_org.set('acme'))
        B._status_failures.clear()
        self.addCleanup(B._status_failures.clear)
        self.calls = []

    def lookup(self, phone, email=None, *, rows=(), channel='webchat', lead=7):
        set_turn(lead_id=lead, channel_type=channel)

        def fake(phone_number, *, email=None, lead_id=None):
            self.calls.append({'phone': phone_number, 'email': email, 'lead_id': lead_id})
            return list(rows)
        with patch.object(B, 'get_inquiry_status_rows', side_effect=fake):
            return B._inquiry_status_lookup(phone, email)


class TestWebChatNeedsBoth(StatusCase):

    def test_a_number_alone_reveals_nothing_and_asks_for_the_email(self):
        r = self.lookup('97455551234')
        self.assertEqual(self.calls, [], 'the CRM was asked without an email')
        self.assertTrue(r['verification_needed'])
        self.assertEqual(r['rows'], [])

    def test_both_go_to_the_crm_with_the_conversation(self):
        self.lookup('97455551234', 'parent@x.com', rows=[{'student_name': 'A'}])
        self.assertEqual(self.calls, [{'phone': '97455551234', 'email': 'parent@x.com',
                                       'lead_id': 7}])

    def test_a_match_returns_the_rows(self):
        r = self.lookup('97455551234', 'parent@x.com', rows=[{'student_name': 'A'}])
        self.assertEqual(r, [{'student_name': 'A'}])


class TestEveryMissLooksTheSame(StatusCase):

    def test_the_reply_never_says_which_detail_was_wrong(self):
        r = self.lookup('97455551234', 'wrong@x.com')
        text = r['tell_parent'].lower()
        self.assertIn('without saying which detail', text)
        self.assertNotIn('verification_needed', r)

    def test_three_misses_lock_the_chat(self):
        for _ in range(B.STATUS_LOOKUP_MAX_FAILURES):
            self.lookup('97455551234', 'guess@x.com')
        self.calls.clear()
        r = self.lookup('97455551234', 'real@x.com', rows=[{'student_name': 'A'}])
        self.assertTrue(r['locked'])
        self.assertEqual(self.calls, [], 'a locked chat still reached the CRM')

    def test_the_lock_is_per_conversation(self):
        for _ in range(B.STATUS_LOOKUP_MAX_FAILURES):
            self.lookup('97455551234', 'guess@x.com', lead=7)
        r = self.lookup('97455551234', 'real@x.com', rows=[{'student_name': 'A'}], lead=8)
        self.assertEqual(r, [{'student_name': 'A'}])

    def test_a_success_clears_earlier_misses(self):
        self.lookup('97455551234', 'typo@x.com')
        self.lookup('97455551234', 'real@x.com', rows=[{'student_name': 'A'}])
        self.assertNotIn(('acme', 7), B._status_failures)


class TestWhatsApp(StatusCase):

    def test_no_email_is_needed_and_the_conversation_is_sent(self):
        r = self.lookup('97455551234', channel='whatsapp', rows=[{'student_name': 'A'}])
        self.assertEqual(r, [{'student_name': 'A'}])
        self.assertEqual(self.calls[0]['lead_id'], 7)

    def test_a_miss_is_not_counted_as_guessing(self):
        # the CRM uses the sender's own number there - nothing to guess
        for _ in range(B.STATUS_LOOKUP_MAX_FAILURES + 1):
            r = self.lookup('97455551234', channel='whatsapp')
        self.assertNotIn('locked', r)


class TestTheRealRequest(StatusCase):
    """The cases above replace get_inquiry_status_rows, so they could not see
    it rejecting the email/lead_id it is now handed - which it once did, and
    every web chat status question would have died with a TypeError. This one
    stops only at the HTTP call."""

    def test_email_and_conversation_reach_the_crm_url(self):
        import tools.getAi as G
        set_turn(lead_id=7, channel_type='webchat')
        with patch.object(G, 'get', return_value={'ok': True, 'data': []}) as http:
            B._inquiry_status_lookup('97455551234', 'parent@x.com')
        url = http.call_args.args[0]
        self.assertIn('phone=97455551234', url)
        self.assertIn('email=parent%40x.com', url)
        self.assertIn('lead_id=7', url)


class TestTheConversationReachesTheTool(unittest.TestCase):
    """The agent runs in a worker thread (anyio.to_thread). If the turn's
    conversation did not travel with it, the tool would send no lead_id and the
    CRM would apply the strict rule to WhatsApp too."""

    def test_it_survives_the_thread_hop(self):
        async def turn():
            set_turn(lead_id=42, channel_type='WhatsApp')
            return await anyio.to_thread.run_sync(current_turn)
        seen = asyncio.run(turn())
        self.assertEqual((seen.lead_id, seen.channel_type), (42, 'whatsapp'))


if __name__ == '__main__':
    unittest.main()
