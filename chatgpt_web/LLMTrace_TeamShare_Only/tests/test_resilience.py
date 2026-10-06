import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from llmtrace.core.pipeline import run


class ResilienceTest(unittest.TestCase):
    def test_collected_history_reaches_behavior_engine(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as out_dir:
            root = Path(source_dir) / 'Default'
            root.mkdir()
            conn = sqlite3.connect(root / 'History')
            try:
                conn.execute('CREATE TABLE urls (id INTEGER, url TEXT, title TEXT, last_visit_time INTEGER)')
                conn.execute('CREATE TABLE visits (id INTEGER, url INTEGER, visit_time INTEGER, from_visit INTEGER, transition INTEGER)')
                conn.execute('INSERT INTO urls VALUES (1, ?, ?, ?)', ('https://chatgpt.com/', 'ChatGPT', 13400000000000000))
                conn.execute('INSERT INTO visits VALUES (10, 1, ?, 0, 1)', (13400000000000000,))
                conn.commit()
            finally:
                conn.close()

            events = []
            result = run(
                'chatgpt_web', out_dir, input_root=source_dir,
                progress=events.append,
            )
            self.assertEqual(result['problems'], 0)
            self.assertEqual(result['inferences'], 1)
            self.assertTrue(any(
                event.get('event') == 'artifact_processed'
                and event.get('artifact') == 'history'
                and event.get('source_count') == 1
                for event in events
            ))
            self.assertTrue(any(
                event.get('event') == 'action_analysis_completed'
                for event in events
            ))
            with open(Path(out_dir) / 'behaviors.csv', encoding='utf-8-sig') as f:
                self.assertIn('R-ACCESS-MIN-01', f.read())

    def test_parser_failure_is_recorded_and_other_parsers_continue(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as out_dir:
            root = Path(source_dir) / 'Default'
            root.mkdir()
            db = root / 'History'
            conn = sqlite3.connect(db)
            try:
                conn.execute('CREATE TABLE urls (id INTEGER, url TEXT, title TEXT)')
                conn.execute('INSERT INTO urls VALUES (1, ?, ?)', ('https://chatgpt.com/', 'ChatGPT'))
                conn.commit()
            finally:
                conn.close()
            (root / 'Preferences').write_text('{invalid json', encoding='utf-8')

            result = run('chatgpt_web', out_dir, input_root=source_dir, parse_only=True)
            self.assertGreaterEqual(result['raw_records'], 1)
            self.assertEqual(result['problems'], 1)

            with open(Path(out_dir) / 'problems.csv', encoding='utf-8-sig', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]['parser'], 'chromium_preferences')
            self.assertEqual(rows[0]['code'], 'parser_failed')

            with open(Path(out_dir) / 'run.json', encoding='utf-8') as f:
                run_data = json.load(f)
            self.assertEqual(run_data['status'], 'completed_with_problems')


if __name__ == '__main__':
    unittest.main()
