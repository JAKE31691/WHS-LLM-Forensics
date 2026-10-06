import tempfile
import unittest
from pathlib import Path

from llmtrace.core.forensic_summary import build_forensic_summary, render_forensic_html
from llmtrace.core.schema import Observation


class ForensicSummaryTest(unittest.TestCase):
    def test_deduplicates_accounts_conversations_messages_and_visits(self):
        observations = [
            Observation(
                'account', 'cache', 'Cache', 'me', 'account_observation',
                {'account_id': 'user-1', 'email': 'analyst@example.com', 'name': '분석 계정'},
                timestamps={'ts_record': '2026-10-01T00:00:00Z'},
            ),
            Observation(
                'history-state', 'local_storage', 'Local Storage', 'record=1',
                'conversation_history_state',
                {
                    'conversation_id': 'conv-1', 'title': '테스트 대화',
                    'membership': True, 'state_role': 'current',
                    'create_time': '2026-10-01T01:00:00Z',
                    'update_time': '2026-10-01T01:05:00Z',
                },
                evidence_kind='state', seq=10,
            ),
            Observation(
                'snapshot-1', 'cache', 'Cache', 'conversation', 'conversation_snapshot',
                {
                    'conversation_id': 'conv-1', 'title': '테스트 대화',
                    'message_sequence': [
                        {
                            'message_id': 'u1', 'role': 'user', 'text': '질문입니다.',
                            'content_sha256': 'u-hash',
                            'create_time': '2026-10-01T01:01:00Z',
                        },
                        {
                            'message_id': 'a1', 'role': 'assistant', 'text': '응답입니다.',
                            'content_sha256': 'a-hash',
                            'create_time': '2026-10-01T01:02:00Z',
                        },
                    ],
                },
                timestamps={'ts_record': '2026-10-01T01:03:00Z'},
            ),
            Observation(
                'snapshot-2', 'cache', 'Cache', 'conversation-again',
                'conversation_snapshot',
                {
                    'conversation_id': 'conv-1', 'title': '테스트 대화',
                    'message_sequence': [
                        {
                            'message_id': 'u1', 'role': 'user', 'text': '질문입니다.',
                            'content_sha256': 'u-hash',
                            'create_time': '2026-10-01T01:01:00Z',
                        },
                        {
                            'message_id': 'a1', 'role': 'assistant', 'text': '응답입니다.',
                            'content_sha256': 'a-hash',
                            'create_time': '2026-10-01T01:02:00Z',
                        },
                    ],
                },
                timestamps={'ts_record': '2026-10-01T01:04:00Z'},
            ),
            Observation(
                'visit-1', 'history', 'History', 'visits:1', 'conversation_visit',
                {'conversation_id': 'conv-1', 'title': '테스트 대화', 'visit_id': 1},
                timestamps={'ts_visit': '2026-10-01T01:00:30Z'},
            ),
        ]

        summary = build_forensic_summary(observations)
        self.assertEqual(len(summary['accounts']), 1)
        self.assertEqual(len(summary['conversations']), 1)
        self.assertEqual(len(summary['qa']), 2)
        self.assertEqual(len(summary['prompts']), 1)
        self.assertEqual(summary['total_user_messages'], 1)
        self.assertEqual(summary['visited_conversations'][0]['visit_count'], 1)

        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / 'forensic_info.html'
            render_forensic_html(str(output), {'service': 'ChatGPT'}, summary)
            text = output.read_text(encoding='utf-8')
        self.assertIn('analyst@example.com', text)
        self.assertIn('질문입니다.', text)
        self.assertIn('응답입니다.', text)
        self.assertNotIn('source_path', text)
        self.assertNotIn('parsed_record', text)


if __name__ == '__main__':
    unittest.main()
