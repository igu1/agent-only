import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime
from tools.datetime_tools import get_current_day
from core.manager import create_agent

class TestDatetimeTools(unittest.TestCase):
    def test_get_current_day(self):
        # The tool returns an object, we need to call its entrypoint
        result = get_current_day.entrypoint()
        expected = datetime.now().strftime("%A")
        self.assertEqual(result, expected)
        self.assertIn(result, ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])

    @patch('core.manager.get_profile')
    @patch('core.manager.get_flows')
    @patch('core.manager.get_model')
    @patch('core.manager.Agent')
    def test_create_agent_with_tool(self, mock_agent_class, mock_get_model, mock_get_flows, mock_get_profile):
        # Setup mocks
        mock_get_profile.return_value = {
            'business_name': 'Test Business',
            'business_description': 'Test Description'
        }
        mock_get_flows.return_value = {}
        mock_get_model.return_value = MagicMock()

        # Create agent
        create_agent(agent_id=1)

        # Check if Agent was initialized with correct tools
        args, kwargs = mock_agent_class.call_args
        tools = kwargs.get('tools', [])

        # get_current_day should be in the tools list
        self.assertIn(get_current_day, tools)

if __name__ == '__main__':
    unittest.main()
