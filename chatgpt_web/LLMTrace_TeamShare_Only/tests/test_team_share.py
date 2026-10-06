import csv
import tempfile
import unittest
from pathlib import Path

from llmtrace.core.profile import load_profile
from llmtrace.core.schema import Inference, Observation
from llmtrace.core.team_share import TEAM_SHARE_COLUMNS, export_team_share


class TeamShareExportTest(unittest.TestCase):
    def test_chatgpt_team_share_uses_case_matches_and_two_grades(self):
        profile = load_profile('chatgpt_web')
        observations = [
            Observation(
                id='obs:0001', source_artifact='history',
                source_path=r'C:\Case\Default\History',
                source_locator='table=visits;row=1',
                fact_type='chatgpt_route',
                fact={'url': 'https://chatgpt.com/', 'fact_only_marker': 'removed'},
                parsed_record={'url': 'https://chatgpt.com/', 'visit_count': 1},
            ),
            Observation(
                id='obs:0002', source_artifact='cache',
                source_path=r'C:\Case\Default\Cache\Cache_Data',
                source_locator='key=https://chatgpt.com/unauth-mweb/test',
                fact_type='cache_entry',
                fact={'key': 'https://chatgpt.com/unauth-mweb/test'},
                parsed_record={'request_key': 'https://chatgpt.com/unauth-mweb/test'},
            ),
        ]
        inferences = [
            Inference(
                id='inf:0001', behavior_id='B01',
                behavior_name='ChatGPT Web 접근', rule_id='R-ACCESS-STD-01',
                when_from='2026-10-06T01:10:00.400000Z',
                when_to='2026-10-06T01:20:00.600000Z',
                supported_by=['obs:0001', 'obs:0002'],
                detail={
                    'base_rule_id': 'R-ACCESS-01',
                    'matched_rule_level': 'standard',
                    'refs': {
                        'access_history': {'observation_id': 'obs:0001'},
                        'unauth_cache': {'observation_id': 'obs:0002'},
                    },
                },
            ),
            Inference(
                id='inf:0002', behavior_id='B01',
                behavior_name='ChatGPT Web 접근', rule_id='R-ACCESS-MIN-01',
                when_from='2026-10-06T01:05:00.987654Z',
                supported_by=['obs:0001'],
                detail={
                    'base_rule_id': 'R-ACCESS-01',
                    'matched_rule_level': 'minimum',
                    'refs': {
                        'access_history': {'observation_id': 'obs:0001'},
                    },
                },
            ),
        ]
        environment = {
            'source_type': 'E01', 'source': r'C:\Evidence\case01.E01',
            'machine': 'M1307', 'os': 'Windows 11',
        }

        with tempfile.TemporaryDirectory() as td:
            result = export_team_share(
                profile, td, observations, inferences, environment,
            )
            csv_path = Path(result['csv'])
            html_path = Path(result['html'])

            with csv_path.open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(list(rows[0]), TEAM_SHARE_COLUMNS)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['행위 분류'], 'A01 · ChatGPT Web 접속')
            self.assertEqual(rows[1]['행위 분류'], 'A01 · ChatGPT Web 접속')
            self.assertEqual(rows[0]['아티팩트'], 'Chrome History')
            self.assertEqual(rows[0]['경로'], r'C:\Case\Default\History')
            self.assertEqual(rows[0]['아티팩트 등급'], '직접')
            self.assertEqual(rows[1]['아티팩트'], 'Chrome History | Chromium Cache')
            self.assertEqual(
                rows[1]['경로'],
                'C:\\Case\\Default\\History | C:\\Case\\Default\\Cache\\Cache_Data',
            )
            self.assertEqual(rows[1]['아티팩트 등급'], '직접 | 귀속')
            self.assertEqual(
                len(csv_path.read_text(encoding='utf-8-sig').splitlines()),
                3,
            )
            self.assertEqual(result['mode'], 'case_match_export')
            self.assertEqual(result['actions'], 1)
            self.assertEqual(result['csv_rows'], 2)
            self.assertEqual(result['artifact_rows'], 2)
            self.assertEqual(result['raw_records'], 2)

            html_text = html_path.read_text(encoding='utf-8')
            self.assertEqual(html_text.count('<details class="action"'), 1)
            self.assertEqual(html_text.count('<details class="match"'), 2)
            self.assertNotIn('<details class="action" open', html_text)
            self.assertIn('분석 개요', html_text)
            self.assertNotIn('<span>아티팩트 행</span>', html_text)
            self.assertIn('행위별 매칭 결과', html_text)
            self.assertIn('종합분석', html_text)
            self.assertIn('원본 파싱 모음', html_text)
            self.assertIn('매칭 1', html_text)
            self.assertIn('매칭 2', html_text)
            self.assertIn('<span class="match-date">2026-10-06</span>', html_text)
            self.assertIn('<span class="match-clock">01:05:00</span>', html_text)
            self.assertIn('<span class="match-clock">01:15:00</span>', html_text)
            self.assertIn('표시 시간대: UTC', html_text)
            self.assertNotIn(
                '<div class="legend"><span class="grade direct">직접</span>',
                html_text,
            )
            self.assertLess(
                html_text.index('<span class="match-clock">01:05:00</span>'),
                html_text.index('<span class="match-clock">01:15:00</span>'),
            )
            self.assertIn('아티팩트</th><th>경로</th><th>아티팩트 등급', html_text)
            self.assertIn('C:\\Evidence\\case01.E01', html_text)
            self.assertIn('직접', html_text)
            self.assertIn('귀속', html_text)
            self.assertIn('Chrome History', html_text)
            self.assertNotIn('Chrome/Edge History', html_text)
            self.assertNotIn('R-ACCESS-01', html_text)

            raw_dir = html_path.parent / 'raw_parsing_data'
            raw_index = (raw_dir / 'index.js').read_text(encoding='utf-8')
            raw_shard = (raw_dir / 'shard_0000.js').read_text(encoding='utf-8')
            self.assertIn('obs:0001', raw_index)
            self.assertNotIn('chatgpt_route', raw_index)
            self.assertIn('parsed_record', html_text)
            self.assertNotIn('2차 Observation fact', html_text)
            self.assertIn('visit_count', raw_shard)
            self.assertIn('https://chatgpt.com/', raw_shard)
            self.assertNotIn('fact_only_marker', raw_shard)

    def test_profile_without_team_share_labels_exports_empty_match_result(self):
        profile = load_profile('claude_app')
        with tempfile.TemporaryDirectory() as td:
            result = export_team_share(profile, td)
            self.assertEqual(result['actions'], 0)
            self.assertEqual(result['artifact_rows'], 0)
            self.assertTrue(Path(result['csv']).exists())
            self.assertTrue(Path(result['html']).exists())
            self.assertTrue(Path(result['raw_data']['index']).exists())
            self.assertIn(
                '규칙에 매칭된 사용자 행위가 없습니다.',
                Path(result['html']).read_text(encoding='utf-8'),
            )


if __name__ == '__main__':
    unittest.main()
