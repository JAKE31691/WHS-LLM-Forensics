import json
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

from llmtrace.common.parsers.base import available, get
from llmtrace.core.schema import Ids


class CommonParserTest(unittest.TestCase):
    def test_chatgpt_parser_plan_is_registered(self):
        required = {
            'chromium_history', 'chromium_cache', 'chromium_local_storage',
            'chromium_session_storage', 'chromium_sessions', 'chromium_cookies',
            'chromium_preferences', 'chromium_dips', 'chromium_favicons',
        }
        self.assertTrue(required.issubset(set(available())))

    def test_sqlite_parser_preserves_row_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = root / 'Users' / 'u' / 'AppData' / 'Local' / 'Google' / 'Chrome' / 'User Data' / 'Default'
            p.mkdir(parents=True)
            db = p / 'History'
            conn = sqlite3.connect(db)
            try:
                conn.execute('CREATE TABLE urls (id INTEGER, url TEXT, title TEXT, visit_count INTEGER)')
                conn.execute('INSERT INTO urls VALUES (?,?,?,?)', (1, 'https://chatgpt.com/c/a', 'A', 2))
                conn.commit()
            finally:
                conn.close()
            rows = list(get('chromium_history')(str(root), {'artifact': 'history'}, None))
            url_rows = [r for r in rows if r.dataset == 'urls']
            self.assertEqual(len(url_rows), 1)
            self.assertEqual(url_rows[0].record, {
                'id': 1, 'url': 'https://chatgpt.com/c/a', 'title': 'A', 'visit_count': 2
            })
            self.assertNotIn('fact_type', url_rows[0].record)

    def test_preferences_keeps_original_json_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = root / 'Default'
            p.mkdir()
            source = {'profile': {'content_settings': {'x': 1}}, 'flag': True}
            (p / 'Preferences').write_text(json.dumps(source), encoding='utf-8')
            rows = list(get('chromium_preferences')(str(root), {'artifact': 'preferences'}, None))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].record, source)
            self.assertEqual(rows[0].output_format, 'json')

    def test_sessions_outputs_native_command_payload(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = root / 'Default' / 'Sessions'
            p.mkdir(parents=True)
            payload = b'abc\x00\xff'
            command = bytes([6]) + payload
            data = b'SNSS' + struct.pack('<I', 3) + struct.pack('<H', len(command)) + command + struct.pack('<H', 0)
            (p / 'Session_123').write_bytes(data)
            rows = list(get('chromium_sessions')(str(root), {'artifact': 'sessions'}, None))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].record['version'], 3)
            self.assertEqual(rows[0].record['command_id'], 6)
            self.assertEqual(rows[0].record['payload'], payload)

    def test_bad_session_file_does_not_abort_other_session_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = root / 'Default' / 'Sessions'
            folder.mkdir(parents=True)
            (folder / 'Session_001').write_bytes(b'not-snss')
            command = bytes([6]) + b'ok'
            good = b'SNSS' + struct.pack('<I', 3) + struct.pack('<H', len(command)) + command + struct.pack('<H', 0)
            (folder / 'Session_002').write_bytes(good)
            problems = []
            rows = list(get('chromium_sessions')(
                str(root), {
                    'artifact': 'sessions', '_problem_sink': problems,
                    '_ids': Ids(), '_profile_name': 'test',
                }, None,
            ))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].record['payload'], b'ok')
            self.assertEqual(len(problems), 1)
            self.assertEqual(problems[0].code, 'record_parse_failed')

    def test_missing_ccl_artifacts_do_not_require_dependency(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(list(get('chromium_local_storage')(str(root), {'artifact': 'local_storage'}, None)), [])
            self.assertEqual(list(get('chromium_session_storage')(str(root), {'artifact': 'session_storage'}, None)), [])
            self.assertEqual(list(get('chromium_cache')(str(root), {'artifact': 'cache'}, None)), [])


if __name__ == '__main__':
    unittest.main()
