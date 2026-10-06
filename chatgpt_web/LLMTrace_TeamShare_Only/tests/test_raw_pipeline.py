import json
import os
import tempfile
import unittest

from llmtrace.core.mapping import apply_mappings
from llmtrace.core.raw_io import write_raw_records
from llmtrace.core.schema import Ids, RawRecord
from llmtrace.core.profile import load_profile
from llmtrace.core.mapping import process_surface


class RawStageTest(unittest.TestCase):
    def test_raw_output_does_not_inject_metadata(self):
        source = {
            'id': 45,
            'url': 'https://chatgpt.com/c/abc',
            'title': 'ChatGPT',
            'visit_count': 3,
            'typed_count': 1,
            'last_visit_time': 13433509595528772,
            'hidden': 0,
        }
        row = RawRecord(
            artifact='history',
            dataset='urls',
            record=dict(source),
            source_path=r'C\Users\u\...\History',
            source_locator='urls rowid=45',
            parser='chromium_history',
        )
        with tempfile.TemporaryDirectory() as td:
            manifest = write_raw_records(td, [row])
            path = os.path.join(td, 'history', 'urls.jsonl')
            with open(path, encoding='utf-8') as f:
                saved = json.loads(f.readline())
            self.assertEqual(saved, source)
            self.assertNotIn('artifact', saved)
            self.assertNotIn('fact_type', saved)
            self.assertNotIn('evidence_kind', saved)
            self.assertEqual(manifest['datasets'][0]['record_count'], 1)

    def test_surface_mapping_is_second_stage(self):
        raw = RawRecord(
            artifact='history', dataset='urls',
            record={'id': 45, 'url': 'https://chatgpt.com/c/abc', 'title': 'ChatGPT'},
            source_path='History', source_locator='urls rowid=45', parser='chromium_history'
        )
        mappings = [{
            'artifact': 'history',
            'dataset': 'urls',
            'when': {'url': {'contains': 'chatgpt.com/c/'}},
            'fact_type': 'conversation_url',
            'evidence_kind': 'event',
            'fact': {'url': 'url', 'title': 'title'},
        }]
        obs = apply_mappings([raw], mappings, Ids())
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].fact_type, 'conversation_url')
        self.assertEqual(obs[0].fact['url'], raw.record['url'])
        self.assertNotIn('fact_type', raw.record)

    def test_chatgpt_history_join_preserves_parsed_record(self):
        rows = [
            RawRecord('history', 'urls', {
                'id': 7, 'url': 'https://chatgpt.com/c/conv-1?x=1',
                'title': 'Conversation', 'last_visit_time': 13400000000000000,
            }, source_path='History', source_locator='urls:1', parser='chromium_history'),
            RawRecord('history', 'visits', {
                'id': 8, 'url': 7, 'visit_time': 13400000000000000,
                'from_visit': 0, 'transition': 1,
            }, source_path='History', source_locator='visits:1', parser='chromium_history'),
        ]
        observations = process_surface(rows, load_profile('chatgpt_web'), Ids())
        visit = next(o for o in observations if o.fact_type == 'conversation_visit')
        self.assertEqual(visit.fact['conversation_id'], 'conv-1')
        self.assertEqual(visit.fact['hostname'], 'chatgpt.com')
        self.assertEqual(visit.fact['query']['x'], '1')
        self.assertEqual(visit.parsed_record, rows[1].record)

    def test_history_ids_are_joined_within_one_browser_profile(self):
        chrome = r'C:\Users\u\AppData\Local\Google\Chrome\User Data\Default\History'
        edge = r'C:\Users\u\AppData\Local\Microsoft\Edge\User Data\Default\History'
        rows = [
            RawRecord('history', 'urls', {'id': 1, 'url': 'https://chatgpt.com/'}, chrome, 'u1', 'chromium_history'),
            RawRecord('history', 'visits', {'id': 10, 'url': 1, 'visit_time': 13400000000000000}, chrome, 'v1', 'chromium_history'),
            RawRecord('history', 'urls', {'id': 1, 'url': 'https://example.com/'}, edge, 'u1', 'chromium_history'),
            RawRecord('history', 'visits', {'id': 10, 'url': 1, 'visit_time': 13400000000000000}, edge, 'v1', 'chromium_history'),
        ]
        observations = process_surface(rows, load_profile('chatgpt_web'), Ids())
        visits = [o for o in observations if o.fact_type == 'url_visit']
        self.assertEqual({o.fact['hostname'] for o in visits}, {'chatgpt.com', 'example.com'})
        self.assertEqual(len({o.fact['_source_scope'] for o in visits}), 2)


if __name__ == '__main__':
    unittest.main()
