import os
import csv
import json
import tempfile
import unittest

from llmtrace.core.pipeline import run


class EngineTest(unittest.TestCase):
    def test_chatgpt_login_rule(self):
        root = os.path.dirname(os.path.dirname(__file__))
        sample = os.path.join(root, 'samples', 'chatgpt_web_observations.jsonl')
        with tempfile.TemporaryDirectory() as td:
            result = run('chatgpt_web', td, observations_path=sample)
            self.assertGreaterEqual(result['inferences'], 1)
            
            with open(os.path.join(td, 'behaviors.csv'), encoding='utf-8-sig') as f:
                text = f.read()
            self.assertIn('R-LOGIN-STD-01', text)
            self.assertTrue(os.path.exists(os.path.join(td, 'report.html')))
            self.assertTrue(os.path.exists(os.path.join(td, 'forensic_info.html')))
            self.assertTrue(os.path.exists(os.path.join(td, 'run.json')))
            self.assertTrue(os.path.exists(os.path.join(td, 'problems.csv')))
            self.assertTrue(os.path.exists(os.path.join(
                td, 'team_share', 'LLMTrace_행위별_아티팩트_매트릭스.csv'
            )))
            self.assertTrue(os.path.exists(os.path.join(
                td, 'team_share', 'LLMTrace_팀공유_행위별_아티팩트.html'
            )))
            self.assertIsNotNone(result['team_share'])
            self.assertEqual(result['team_share']['mode'], 'case_match_export')
            self.assertGreater(result['team_share']['artifact_rows'], 0)
            self.assertEqual(result['team_share']['csv_rows'], result['inferences'])

            with open(os.path.join(td, 'behaviors.csv'), encoding='utf-8-sig', newline='') as f:
                header = next(csv.reader(f))
            self.assertEqual(header, [
                'id', 'question', 'behavior_id', 'behavior_name', 'confidence',
                'when_from', 'when_to', 'span_sec', 'actor', 'rule_id',
                'when_basis', 'note', 'alternatives', 'supported_by',
            ])
            with open(os.path.join(td, 'run.json'), encoding='utf-8') as f:
                run_data = json.load(f)
            self.assertEqual(run_data['status'], 'completed')
            self.assertEqual(
                run_data['counts']['team_share_artifact_rows'],
                result['team_share']['artifact_rows'],
            )
            self.assertEqual(
                run_data['counts']['team_share_csv_rows'],
                result['team_share']['csv_rows'],
            )

            with open(os.path.join(td, 'report.html'), encoding='utf-8') as f:
                report = f.read()
            self.assertIn('1. 사용자 행위 분석 결과', report)
            self.assertIn('2. 현재 시스템의 유의미 정보', report)
            self.assertIn('3. Observation / 원본 파싱 결과', report)
            self.assertIn('1. 계정 관련 내용', report)
            self.assertIn('2. 대화목록', report)
            self.assertIn('3. 프롬프트 입력', report)
            self.assertIn('4. 호출된 대화방의 질문과 응답', report)
            self.assertIn('5. 방문했던 대화방', report)
            self.assertIn('6. 유저가 메시지를 보낸 횟수', report)

            with open(os.path.join(td, 'forensic_info.html'), encoding='utf-8') as f:
                forensic = f.read()
            self.assertIn('LLMTrace 포렌식 유의미 정보', forensic)
            self.assertNotIn('Observation / 원본 파싱 결과', forensic)
            self.assertEqual(
                result['forensic_info']['html'], os.path.join(td, 'forensic_info.html')
            )

if __name__ == '__main__':
    unittest.main()
