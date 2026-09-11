"""An empty fee chart must say so, not hand the model a silence to fill.

get_fee_details returned a bare list. For a class whose school has configured
NO fee rows at all that was `[]` - and asked for Grade 3 fees at exactly such a
school, the assistant answered:

    "our annual tuition fee is divided into three installments: the first
     installment of 3,500 SAR is due at admission"

Nobody set that number. There is no chart. A parent can act on a figure like
that, so this is the most expensive kind of invention in the product.

The prompt already said "Do not invent pricing" - it was not enough, because an
empty list is ambiguous and the rule was competing with the parent's question.
The tool now answers in words, at the moment the model asks.

    python -m unittest discover -s tests -p "test_fee_lookup.py"
"""
import unittest
from unittest.mock import patch

from tools.business_info import build_business_tools


def fee_tool(rows):
    """The bound get_fee_details, with the backend's answer canned."""
    with patch('tools.business_info.get_fee_chart', return_value=rows):
        tools = build_business_tools(agent_id=2)
        fn = next(t for t in tools if getattr(t, 'name', '') == 'get_fee_details')
        entrypoint = getattr(fn, 'entrypoint', fn)
        return entrypoint(class_id=47)


REAL_ROWS = [
    {'fee_receipt_name': 'General - First Child',
     'installment_name': 'Admission Installment',
     'fee_item_name': 'Registration Fee', 'net_amount': 2000.0},
]


class TestAChartThatExistsIsUnchanged(unittest.TestCase):

    def test_the_rows_come_through(self):
        result = fee_tool(REAL_ROWS)
        self.assertEqual(result['rows'], REAL_ROWS)

    def test_it_is_not_flagged_as_missing(self):
        self.assertNotIn('no_fee_chart', fee_tool(REAL_ROWS))

    def test_the_amounts_survive_intact(self):
        """The model quotes these - they must not be reshaped on the way."""
        self.assertEqual(fee_tool(REAL_ROWS)['rows'][0]['net_amount'], 2000.0)


class TestAnEmptyChartSpeaks(unittest.TestCase):

    def test_it_is_flagged(self):
        self.assertTrue(fee_tool([])['no_fee_chart'])

    def test_the_rows_are_still_an_empty_list(self):
        """Shape stays predictable - a caller iterating rows must not break."""
        self.assertEqual(fee_tool([])['rows'], [])

    def test_it_tells_the_model_what_to_say(self):
        told = fee_tool([])['tell_parent']
        self.assertIn('do NOT have', told.replace("do not have", "do NOT have"))
        self.assertIn('admissions team', told)

    def test_it_forbids_inventing_a_figure(self):
        """The exact failure: a specific SAR amount at a school with no chart."""
        told = fee_tool([])['tell_parent'].lower()
        self.assertIn('do not state', told)
        for word in ('amount', 'instalment', 'structure'):
            with self.subTest(word=word):
                self.assertIn(word, told)

    def test_it_still_moves_the_inquiry_on(self):
        """Honesty must not end the conversation - the form continues."""
        self.assertIn('carry on with the inquiry', fee_tool([])['tell_parent'])

    def test_none_is_treated_like_empty(self):
        """A failed backend call returns nothing, not []."""
        self.assertTrue(fee_tool(None)['no_fee_chart'])


class TestTheRuleBacksItUp(unittest.TestCase):

    def test_an_empty_lookup_is_an_answer(self):
        from infra.profile import build_restrictions_base_instructions
        blob = '\n'.join(build_restrictions_base_instructions())
        self.assertIn('no_fee_chart', blob)
        self.assertIn('Never fill the gap', blob)

    def test_the_older_rule_is_still_there(self):
        from infra.profile import build_restrictions_base_instructions
        blob = '\n'.join(build_restrictions_base_instructions())
        self.assertIn('Do not invent pricing', blob)


if __name__ == '__main__':
    unittest.main(verbosity=2)
