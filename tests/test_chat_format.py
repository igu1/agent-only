import unittest

from infra.chat_format import to_whatsapp


class ToWhatsAppTests(unittest.TestCase):
    def test_plain_text_is_unchanged(self):
        self.assertEqual(to_whatsapp('Hello! How can I help?'), 'Hello! How can I help?')

    def test_empty_and_none(self):
        self.assertEqual(to_whatsapp(''), '')
        self.assertEqual(to_whatsapp(None), '')

    def test_bold(self):
        self.assertEqual(to_whatsapp('Fee is **QAR 12,000** per year'), 'Fee is *QAR 12,000* per year')

    def test_bullets_become_dots_and_are_not_read_as_bold(self):
        src = 'Documents:\n- Passport copy\n* Birth certificate\n  - Photo'
        self.assertEqual(to_whatsapp(src), 'Documents:\n• Passport copy\n• Birth certificate\n  • Photo')

    def test_numbered_lines_pass_through(self):
        self.assertEqual(to_whatsapp('1. Fill the form\n2. Verify'), '1. Fill the form\n2. Verify')

    def test_link_becomes_label_and_url(self):
        self.assertEqual(to_whatsapp('See [the prospectus](https://x.org/p.pdf).'),
                         'See the prospectus: https://x.org/p.pdf.')

    def test_heading_becomes_bold_line(self):
        self.assertEqual(to_whatsapp('## Fees\nTerm 1'), '*Fees*\nTerm 1')

    def test_bold_inside_bullet(self):
        self.assertEqual(to_whatsapp('- **Grade 1**: QAR 10,000'), '• *Grade 1*: QAR 10,000')


if __name__ == '__main__':
    unittest.main()
