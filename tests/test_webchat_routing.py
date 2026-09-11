"""A web chat is routed to its campus BEFORE it exists, never during.

Two holes, one shape - a null institution meaning two different things.

1. A company with ONE school returned institution_id: null. There was nothing
   to choose, so `institution_required` was false and the widget opened its
   composer - and then sent every message unrouted. Same null a visitor who has
   not picked yet gets, so nothing downstream could tell them apart. The
   conversation row was written with InstitutionID NULL and fell out of the
   staff list's institution filter.

2. Both clients already refuse to chat before a campus is known - the Angular
   panel renders only once one is chosen, the widget keeps its composer behind
   the picker - but the endpoint itself accepted anything. A caller posting
   straight to it got a company-level conversation answered out of the POOLED
   data of every school in the group.

The pooling is the part that matters: that is what commit 81e4a25 was written
to stop.

    python -m unittest discover -s tests -p "test_webchat_routing.py"
"""
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import api.routes.msgchannels.webchat as WC


ONE = [{'id': 7, 'name': 'Only School'}]
THREE = [{'id': 1, 'name': 'Sanabl'}, {'id': 2, 'name': 'Model Girls'},
         {'id': 3, 'name': 'ABC'}]


class SessionCase(unittest.IsolatedAsyncioTestCase):

    async def session(self, choices, institution_id=None, agent_id=0):
        with patch.object(WC, 'build_institution_choices', return_value=choices), \
             patch.object(WC, '_channel_agent_id', return_value=agent_id), \
             patch.object(WC, 'build_introduction', return_value='welcome'), \
             patch.object(WC, 'build_institution_prompt', return_value='which campus?'):
            return await WC.webchat_session('1', institution_id=institution_id)


class TestOneSchoolIsNotAChoice(SessionCase):

    async def test_the_single_school_is_resolved(self):
        res = await self.session(ONE)
        self.assertEqual(res['institution_id'], 7,
                         'a single-school company still starts unrouted')

    async def test_no_picker_is_asked_for(self):
        self.assertFalse((await self.session(ONE))['institution_required'])

    async def test_the_greeting_names_that_school(self):
        """Not the group prompt - there is no group to choose from."""
        with patch.object(WC, 'build_institution_choices', return_value=ONE), \
             patch.object(WC, '_channel_agent_id', return_value=0), \
             patch.object(WC, 'build_introduction', return_value='welcome') as intro, \
             patch.object(WC, 'build_institution_prompt', return_value='which campus?'):
            res = await WC.webchat_session('1')
        self.assertEqual(res['introduction'], 'welcome')
        self.assertEqual(intro.call_args.kwargs['institution_id'], 7)


class TestSeveralSchoolsStillAsk(SessionCase):

    async def test_a_picker_is_required(self):
        res = await self.session(THREE)
        self.assertTrue(res['institution_required'])
        self.assertIsNone(res['institution_id'])

    async def test_the_choices_are_offered(self):
        self.assertEqual(len((await self.session(THREE))['institutions']), 3)

    async def test_an_explicit_choice_wins(self):
        res = await self.session(THREE, institution_id=2)
        self.assertEqual(res['institution_id'], 2)
        self.assertFalse(res['institution_required'])

    async def test_a_channel_bound_to_one_school_is_untouched(self):
        """Its institution comes from the CHANNEL, so it offers no choices and
        needs no resolving here."""
        res = await self.session([], agent_id=1)
        self.assertEqual(res['institutions'], [])
        self.assertFalse(res['institution_required'])


class MessageCase(unittest.IsolatedAsyncioTestCase):

    class Req:
        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

    async def send(self, body, choices, agent_id=0):
        self.forwarded = []
        with patch.object(WC, 'build_institution_choices', return_value=choices), \
             patch.object(WC, '_channel_agent_id', return_value=agent_id), \
             patch.object(WC, 'handle_incoming_webhook',
                          side_effect=lambda **kw: self.forwarded.append(kw) or {'ok': True}):
            return await WC.webchat_message('1', self.Req(body))


class TestAnUnroutedMessageIsRefused(MessageCase):

    async def test_it_is_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            await self.send({'session_id': 's', 'text': 'hi'}, THREE)
        self.assertEqual(caught.exception.status_code, 400)

    async def test_it_says_what_is_missing(self):
        with self.assertRaises(HTTPException) as caught:
            await self.send({'session_id': 's', 'text': 'hi'}, THREE)
        self.assertIn('campus', caught.exception.detail)

    async def test_nothing_reaches_the_agent(self):
        """The point: no conversation row, no company agent, no pooled data."""
        with self.assertRaises(HTTPException):
            await self.send({'session_id': 's', 'text': 'hi'}, THREE)
        self.assertEqual(self.forwarded, [])

    async def test_a_blank_institution_is_still_unrouted(self):
        for value in (None, '', 0, 'abc'):
            with self.subTest(value=value):
                with self.assertRaises(HTTPException):
                    await self.send(
                        {'session_id': 's', 'text': 'hi', 'institution_id': value}, THREE)


class TestARoutedMessageGoesThrough(MessageCase):

    async def test_it_is_forwarded(self):
        res = await self.send(
            {'session_id': 's', 'text': 'hi', 'institution_id': 2}, THREE)
        self.assertTrue(res['ok'])
        self.assertEqual(len(self.forwarded), 1)
        self.assertEqual(self.forwarded[0]['channel_type'], 'webchat')

    async def test_a_channel_bound_to_one_school_needs_no_institution(self):
        """WhatsApp-style binding: nothing to choose, so nothing to demand."""
        res = await self.send({'session_id': 's', 'text': 'hi'}, [], agent_id=1)
        self.assertTrue(res['ok'])
        self.assertEqual(len(self.forwarded), 1)

    async def test_a_single_school_company_is_not_blocked(self):
        """It was resolved at session time, so the client sends it - but even if
        an old client does not, one school cannot be the wrong school."""
        res = await self.send({'session_id': 's', 'text': 'hi'}, ONE)
        self.assertTrue(res.get('ok') or self.forwarded,
                        'a one-school company was locked out of its own chat')


if __name__ == '__main__':
    unittest.main(verbosity=2)
