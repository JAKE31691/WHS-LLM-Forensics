import tempfile
import unittest
from pathlib import Path

from llmtrace.core.profile import load_profile
from llmtrace.core.schema import Inference, Observation
from llmtrace.core.team_share import export_team_share
from llmtrace.core.team_share_only import _publish_team_share


class TeamShareOnlyTest(unittest.TestCase):
    def test_publish_leaves_only_csv_and_self_contained_html(self):
        profile = load_profile('chatgpt_web')
        observation = Observation(
            id='obs:0001',
            source_artifact='history',
            source_path=r'C:\Case\Default\History',
            source_locator='table=visits;row=1',
            fact_type='chatgpt_route',
            fact={'url': 'https://chatgpt.com/'},
            parsed_record={
                'url': 'https://chatgpt.com/',
                'html_text': '</script><p>evidence</p>',
            },
        )
        inference = Inference(
            id='inf:0001',
            behavior_id='B01',
            behavior_name='ChatGPT Web 접근',
            rule_id='R-ACCESS-MIN-01',
            when_from='2026-10-06T01:05:00Z',
            supported_by=['obs:0001'],
            detail={
                'base_rule_id': 'R-ACCESS-01',
                'matched_rule_level': 'minimum',
                'refs': {
                    'access_history': {'observation_id': 'obs:0001'},
                },
            },
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staged = export_team_share(
                profile,
                str(root / 'staged'),
                observations=[observation],
                inferences=[inference],
                environment={'source_type': 'E01', 'source': r'C:\Evidence\case.E01'},
            )
            output = root / 'final'
            result = _publish_team_share(staged, output, None)

            self.assertEqual(
                sorted(path.name for path in output.iterdir()),
                [
                    'LLMTrace_팀공유_행위별_아티팩트.html',
                    'LLMTrace_행위별_아티팩트_매트릭스.csv',
                ],
            )
            html_text = Path(result['html']).read_text(encoding='utf-8')
            self.assertIn('llmtrace-embedded-raw-data', html_text)
            self.assertIn('window.__llmtraceRawIndex=', html_text)
            self.assertIn('window.__llmtraceRawShards[0]=', html_text)
            self.assertIn('<\\/script><p>evidence</p>', html_text)
            self.assertNotIn('raw_data', result)
            self.assertTrue(result['self_contained_html'])


if __name__ == '__main__':
    unittest.main()
