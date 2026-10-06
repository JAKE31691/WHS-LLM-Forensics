import json
import tempfile
import unittest
from pathlib import Path

import brotli

from llmtrace.core.mapping import process_surface
from llmtrace.core.profile import load_profile
from llmtrace.core.report import render_html
from llmtrace.core.schema import Ids, RawRecord
from llmtrace.profiles.chatgpt_web.mappings import _source_scope


class CacheMappingTests(unittest.TestCase):
    def test_curated_case_artifacts_share_scope_without_crossing_cases(self):
        history = _source_scope(r'C:\Cases\08-1\History')
        cache = _source_scope(r'C:\Cases\08-1\Cache\Cache_Data')
        other = _source_scope(r'C:\Cases\08-2\Cache\Cache_Data')
        self.assertEqual(history, cache)
        self.assertNotEqual(history, other)

    def test_composite_cache_key_uses_final_request_url_and_omits_binary_body(self):
        body = b'not-json' * 10000
        raw = RawRecord(
            artifact='cache', dataset='entries', parser='chromium_cache', ordinal=1,
            source_path=r'C:\Users\a\AppData\Local\Google\Chrome\User Data\Default\Cache\Cache_Data',
            source_locator='key=x;version=0',
            record={
                'key': ('1/0/_dk_https://chatgpt.com https://chatgpt.com '
                        'https://chatgpt.com/backend-api/conversation/c1/stream_status'),
                'metadata': {},
                'body': body,
            },
        )
        obs = process_surface([raw], load_profile('chatgpt_web'), Ids())[0]
        self.assertEqual(obs.fact_type, 'conversation_stream_status')
        self.assertEqual(obs.fact['hostname'], 'chatgpt.com')
        self.assertEqual(obs.fact['conversation_id'], 'c1')
        self.assertNotIn('json', obs.fact)
        self.assertEqual(obs.parsed_record['body']['length'], len(body))

        with tempfile.TemporaryDirectory() as td:
            report = Path(td) / 'report.html'
            render_html(str(report), {'profile_id': 'x'}, [obs], [], [], [])
            self.assertLess(report.stat().st_size, 30_000)

    def test_drive_url_safety_check_preserves_conversation_and_external_file(self):
        conversation_id = 'conversation-13'
        external_file_id = 'drive-file-13'
        target = (
            'https%3A%2F%2Fdrive.google.com%2Ffile%2Fd%2F'
            f'{external_file_id}%2Fview%3Fusp%3Ddrivesdk'
        )
        raw = RawRecord(
            artifact='cache', dataset='entries', parser='chromium_cache', ordinal=1,
            source_path=r'C:\Cases\A13\Cache\Cache_Data',
            source_locator='url-safe',
            record={
                'key': (
                    'https://chatgpt.com/backend-api/conversation/'
                    f'{conversation_id}/url_safe?url={target}'
                ),
                'metadata': {'response_time': '2026-09-21T09:46:03Z'},
                'body': b'{"safe":false,"blocked":false}',
            },
        )

        observation = process_surface(
            [raw], load_profile('chatgpt_web'), Ids()
        )[0]

        self.assertEqual(observation.fact_type, 'url_safety_check')
        self.assertEqual(observation.fact['conversation_id'], conversation_id)
        self.assertEqual(observation.fact['target_hostname'], 'drive.google.com')
        self.assertEqual(observation.fact['external_file_id'], external_file_id)
        self.assertEqual(observation.fact['json'], {'safe': False, 'blocked': False})

    def test_brotli_conversation_body_becomes_active_branch_snapshot(self):
        body = {
            'id': 'conv-1',
            'title': 'A07 test',
            'current_node': 'a2',
            'messages': [
                {
                    'id': 'u1', 'author': {'role': 'user'},
                    'content': {'content_type': 'text', 'parts': ['old prompt']},
                    'create_time': 1789000000,
                },
                {
                    'id': 'a2', 'author': {'role': 'assistant'},
                    'content': {'content_type': 'text', 'parts': ['answer']},
                    'metadata': {'request_id': 'req-1', 'turn_id': 'turn-1'},
                },
            ],
            'mapping': {
                'root': {'id': 'root', 'parent': None, 'message': None},
                'u1': {
                    'parent': 'root',
                    'message': {
                        'id': 'u1', 'author': {'role': 'user'},
                        'content': {'content_type': 'text', 'parts': ['old prompt']},
                        'create_time': 1789000000,
                    },
                },
                'a2': {
                    'parent': 'u1',
                    'message': {
                        'id': 'a2', 'author': {'role': 'assistant'},
                        'content': {'content_type': 'text', 'parts': ['answer']},
                        'metadata': {'request_id': 'req-1', 'turn_id': 'turn-1'},
                    },
                },
                'unused': {
                    'parent': 'root',
                    'message': {
                        'id': 'unused', 'author': {'role': 'user'},
                        'content': {'content_type': 'text', 'parts': ['not active']},
                    },
                },
            },
        }
        raw = RawRecord(
            artifact='cache', dataset='entries', parser='chromium_cache', ordinal=1,
            source_path=r'C:\Chrome\User Data\Default\Cache\Cache_Data',
            source_locator='key=x;version=0',
            record={
                'key': 'https://chatgpt.com/backend-api/conversations/conv-1?include_has_versions=true',
                'metadata': {'response_time': '2026-09-11T08:52:44.852334'},
                'body': brotli.compress(json.dumps(body).encode('utf-8')),
            },
        )
        obs = process_surface([raw], load_profile('chatgpt_web'), Ids())[0]
        self.assertEqual(obs.fact_type, 'conversation_snapshot')
        self.assertEqual(obs.fact['conversation_id'], 'conv-1')
        self.assertEqual(obs.fact['message_ids'], ['u1', 'a2'])
        self.assertNotIn('unused', obs.fact['message_ids'])
        self.assertEqual(obs.fact['message_sequence'][0]['text'], 'old prompt')
        self.assertEqual(obs.fact['message_sequence'][1]['request_id'], 'req-1')

    def test_brotli_me_body_preserves_forensic_identity_fields(self):
        body = {
            'id': 'user-1', 'email': 'analyst@example.com', 'name': '홍길동',
            'phone_number': None, 'country': 'KR', 'region': 'Seoul',
            'client_id': 'client-1',
            'orgs': {'data': [{'id': 'org-1', 'name': 'Example Org'}]},
        }
        raw = RawRecord(
            artifact='cache', dataset='entries', parser='chromium_cache', ordinal=1,
            source_path=r'C:\Chrome\User Data\Default\Cache\Cache_Data',
            source_locator='key=me;version=0',
            record={
                'key': 'https://chatgpt.com/backend-api/me', 'metadata': {},
                'body': brotli.compress(json.dumps(body, ensure_ascii=False).encode('utf-8')),
            },
        )
        obs = process_surface([raw], load_profile('chatgpt_web'), Ids())[0]
        self.assertEqual(obs.fact_type, 'account_observation')
        self.assertEqual(obs.fact['account_id'], 'user-1')
        self.assertEqual(obs.fact['name'], '홍길동')
        self.assertEqual(obs.fact['organization_ids'], ['org-1'])
        self.assertNotIn('phone_number', obs.fact)

    def test_conversation_drafts_are_split_and_empty_state_is_not_a_draft(self):
        records = [
            RawRecord(
                artifact='local_storage', dataset='records', parser='leveldb', ordinal=1,
                source_path=r'C:\Chrome\User Data\Default\Local Storage\leveldb',
                source_locator='seq=10',
                record={
                    'storage_key': 'https://chatgpt.com',
                    'script_key': 'oai/apps/conversationDrafts',
                    'value': json.dumps({
                        'drafts': [{
                            'id': '6aa36059-92dc-83e8-9d73-517cbe4286cc',
                            'content': 'typed prompt', 'timestamp': 1789099957429,
                        }],
                        'userId': 'user-1',
                    }),
                    'leveldb_seq_number': 10, 'is_live': True,
                },
            ),
            RawRecord(
                artifact='local_storage', dataset='records', parser='leveldb', ordinal=2,
                source_path=r'C:\Chrome\User Data\Default\Local Storage\leveldb',
                source_locator='seq=11',
                record={
                    'storage_key': 'https://chatgpt.com',
                    'script_key': 'oai/apps/conversationDrafts',
                    'value': '{"drafts":[],"userId":null}',
                    'leveldb_seq_number': 11, 'is_live': True,
                },
            ),
        ]
        obs = process_surface(records, load_profile('chatgpt_web'), Ids())
        self.assertEqual([item.fact_type for item in obs], [
            'conversation_draft', 'conversation_draft_cleared',
        ])
        self.assertEqual(obs[0].fact['draft_text'], 'typed prompt')
        self.assertEqual(obs[0].fact['conversation_id'], '6aa36059-92dc-83e8-9d73-517cbe4286cc')
        self.assertEqual(obs[0].fact['user_id'], 'user-1')

    def test_memory_endpoints_are_mapped_to_distinct_semantic_facts(self):
        rows = [
            RawRecord(
                artifact='cache', dataset='entries', parser='chromium_cache', ordinal=1,
                source_path=r'C:\Chrome\User Data\Default\Cache\Cache_Data',
                source_locator='checksum',
                record={
                    'key': 'https://chatgpt.com/backend-api/memories/about_you/summary/checksum',
                    'metadata': {'response_time': '2026-09-19T12:09:40.840257'},
                    'body': brotli.compress(json.dumps({
                        'cachedGeneratedAtIso': '2026-09-19T01:27:23.834018+00:00',
                        'cachedSourceChecksum': 'new', 'sourceChecksum': 'new',
                        'isStale': False,
                    }).encode()),
                },
            ),
            RawRecord(
                artifact='cache', dataset='entries', parser='chromium_cache', ordinal=2,
                source_path=r'C:\Chrome\User Data\Default\Cache\Cache_Data',
                source_locator='actions',
                record={
                    'key': 'https://chatgpt.com/backend-api/memories/about_you/selection_correction_actions',
                    'metadata': {},
                    'body': json.dumps({'schemaVersion': 1, 'actions': [{
                        'id': 'make_correction', 'label': '수정하기',
                        'action': {'type': 'open_correction_composer', 'targetAction': 'correct'},
                    }]}, ensure_ascii=False).encode(),
                },
            ),
            RawRecord(
                artifact='cache', dataset='entries', parser='chromium_cache', ordinal=3,
                source_path=r'C:\Chrome\User Data\Default\Cache\Cache_Data',
                source_locator='list',
                record={
                    'key': 'https://chatgpt.com/backend-api/memories?include_memory_entries=false',
                    'metadata': {},
                    'body': b'{"memories":[],"memory_max_tokens":5000000,"memory_num_tokens":107}',
                },
            ),
        ]
        obs = process_surface(rows, load_profile('chatgpt_web'), Ids())
        self.assertEqual([item.fact_type for item in obs], [
            'memory_summary_checksum', 'memory_correction_capability',
            'memory_list_snapshot',
        ])
        self.assertFalse(obs[0].fact['is_stale'])
        self.assertEqual(obs[0].timestamps['ts_modified'], '2026-09-19T01:27:23.834018+00:00')
        self.assertEqual(obs[1].fact['correction_actions'][0]['type'], 'open_correction_composer')
        self.assertEqual(obs[2].fact['memory_num_tokens'], 107)

    def test_local_storage_identity_and_conversation_items_are_normalized(self):
        records = [
            RawRecord(
                artifact='local_storage', dataset='records', parser='leveldb', ordinal=1,
                source_path=r'C:\Chrome\User Data\Default\Local Storage\leveldb',
                source_locator='account',
                record={
                    'storage_key': 'https://chatgpt.com',
                    'script_key': 'oai/apps/accountSwitchSessions',
                    'value': json.dumps([{
                        'email': 'person@example.com', 'phoneNumber': '+821012345678',
                        'name': '수연', 'userId': 'user-1', 'sessionToken': 'secret',
                        'lastLoggedInAt': 1789766878000, 'authProvider': 'openai',
                        'workspaces': [{
                            'workspaceAccountId': 'workspace-1', 'workspaceName': '수연',
                        }],
                    }], ensure_ascii=False),
                    'leveldb_seq_number': 1, 'is_live': True,
                },
            ),
            RawRecord(
                artifact='local_storage', dataset='records', parser='leveldb', ordinal=2,
                source_path=r'C:\Chrome\User Data\Default\Local Storage\leveldb',
                source_locator='history',
                record={
                    'storage_key': 'https://chatgpt.com',
                    'script_key': 'cache/user-1/workspace-1/conversation-history',
                    'value': json.dumps({
                        'value': {'pages': [{'items': [{
                            'id': 'conv-1', 'title': 'Memory Recall',
                            'create_time': '2026-09-18T21:42:47Z',
                            'update_time': '2026-09-18T21:43:54Z',
                            'memory_scope': 'global_enabled', 'is_archived': False,
                        }], 'total': 1}]},
                        'timestamp': 1789781193707,
                    }),
                    'leveldb_seq_number': 2, 'is_live': True,
                },
            ),
        ]
        obs = process_surface(records, load_profile('chatgpt_web'), Ids())
        account = next(item for item in obs if item.fact_type == 'account_session_observation')
        history = next(item for item in obs if item.fact_type == 'conversation_history_state')
        self.assertEqual(account.fact['workspace_account_ids'], ['workspace-1'])
        self.assertTrue(account.fact['has_session_token'])
        self.assertNotIn('sessionToken', account.fact)
        self.assertNotIn('json', account.fact)
        self.assertEqual(history.fact['conversation_id'], 'conv-1')
        self.assertEqual(history.fact['memory_scope'], 'global_enabled')
        self.assertEqual(history.fact['user_id'], 'user-1')

    def test_logout_page_exposes_ordinary_logout_discriminator(self):
        body = brotli.compress(
            b'<a href="https://auth.openai.com/logout?logout_reason='
            b'logout_confirm_modal&logout_source=modal.logout-confirm-modal'
            b'&logout_correlation_id=correlation-11">continue</a>'
        )
        raw = RawRecord(
            artifact='cache', dataset='entries', parser='chromium_cache', ordinal=1,
            source_path=r'C:\Cases\A11\Cache\Cache_Data',
            source_locator='logout',
            record={
                'key': 'https://chatgpt.com/auth/logout',
                'metadata': {'response_time': '2026-09-20T02:45:00Z'},
                'body': body,
            },
        )
        observation = process_surface(
            [raw], load_profile('chatgpt_web'), Ids()
        )[0]

        self.assertEqual(observation.fact['logout_reason'], 'logout_confirm_modal')
        self.assertEqual(observation.fact['logout_source'], 'modal.logout-confirm-modal')
        self.assertEqual(observation.fact['logout_correlation_id'], 'correlation-11')
        self.assertEqual(observation.fact['logout_kind'], 'ordinary_logout_confirmation')

    def test_ordered_conversation_history_snapshots_derive_active_list_removal(self):
        key = 'cache/user-11/workspace-11/conversation-history'

        def history_record(ordinal, sequence, timestamp, items):
            return RawRecord(
                artifact='local_storage', dataset='records', parser='leveldb',
                ordinal=ordinal,
                source_path=r'C:\Cases\A11\Local Storage\leveldb',
                source_locator=f'history-{sequence}',
                record={
                    'storage_key': 'https://chatgpt.com',
                    'script_key': key,
                    'value': json.dumps({
                        'value': {'pages': [{'items': items, 'total': len(items)}]},
                        'timestamp': timestamp,
                    }, ensure_ascii=False),
                    'leveldb_seq_number': sequence,
                    'is_live': True,
                },
            )

        removed = {
            'id': 'conversation-removed', 'title': '인사 대화',
            'create_time': '2026-09-20T02:47:46Z',
            'update_time': '2026-09-20T02:50:15Z',
            'is_archived': False,
        }
        kept = {
            'id': 'conversation-kept', 'title': '남은 대화',
            'create_time': '2026-09-20T02:40:00Z',
            'update_time': '2026-09-20T02:41:00Z',
            'is_archived': False,
        }
        observations = process_surface([
            history_record(1, 10, 1789875416000, [removed, kept]),
            history_record(2, 20, 1789882594000, [kept]),
        ], load_profile('chatgpt_web'), Ids())

        previous = next(
            item for item in observations
            if item.fact.get('conversation_id') == 'conversation-removed'
            and item.fact.get('membership') is True
        )
        current_absence = next(
            item for item in observations
            if item.fact.get('conversation_id') == 'conversation-removed'
            and item.fact.get('membership') is False
        )
        current_kept = next(
            item for item in observations
            if item.fact.get('conversation_id') == 'conversation-kept'
            and item.seq == 20
        )

        self.assertEqual(previous.fact['state_role'], 'residual')
        self.assertTrue(previous.fact['transition_candidate'])
        self.assertEqual(previous.evidence_kind, 'residual')
        self.assertEqual(current_absence.fact['transition_type'], 'removed_from_active_list')
        self.assertEqual(current_absence.fact['state_role'], 'transition_current')
        self.assertEqual(current_absence.fact['previous_leveldb_seq_number'], 10)
        self.assertEqual(current_absence.fact['current_leveldb_seq_number'], 20)
        self.assertEqual(current_absence.fact['user_id'], 'user-11')
        self.assertEqual(current_absence.fact['workspace_account_id'], 'workspace-11')
        self.assertEqual(current_kept.fact['state_role'], 'current')

    def test_a09_file_objects_bodies_library_items_and_attachments_are_normalized(self):
        file_id = 'file_00000000000000000000000000000009'
        png = (
            b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0dIHDR'
            + (1448).to_bytes(4, 'big') + (1086).to_bytes(4, 'big')
            + b'forensic-test-payload'
        )
        source = r'C:\Chrome\User Data\Default\Cache\Cache_Data'
        rows = [
            RawRecord(
                artifact='cache', dataset='entries', parser='chromium_cache', ordinal=1,
                source_path=source, source_locator='simple',
                record={
                    'key': f'https://chatgpt.com/backend-api/files/{file_id}/simple',
                    'metadata': {'response_time': '2026-09-19T16:02:49Z'},
                    'body': json.dumps({
                        'id': file_id, 'file_name': '[GPT_WEB_A09-2].png',
                        'mime_type': 'image/png', 'is_library_file': True,
                        'library_file_id': 'libfile-9',
                    }).encode(),
                },
            ),
            RawRecord(
                artifact='cache', dataset='entries', parser='chromium_cache', ordinal=2,
                source_path=source, source_locator='content',
                record={
                    'key': f'https://chatgpt.com/backend-api/estuary/content?id={file_id}',
                    'metadata': {'response_time': '2026-09-19T16:02:50Z'},
                    'body': png,
                },
            ),
            RawRecord(
                artifact='cache', dataset='entries', parser='chromium_cache', ordinal=3,
                source_path=source, source_locator='library',
                record={
                    'key': 'https://chatgpt.com/backend-api/files/library/nodes',
                    'metadata': {'response_time': '2026-09-19T16:02:52Z'},
                    'body': json.dumps({'items': [{
                        'kind': 'file', 'file_id': file_id,
                        'id': 'libfile-9-list-item',
                        'name': '[GPT_WEB_A09-2].png', 'mime_type': 'image/png',
                        'file_size_bytes': len(png),
                        'file_upload_time': '2026-09-19T16:02:48Z',
                        'file_processed_time': '2026-09-19T16:02:51Z',
                        'origination_message_id': 'message-9',
                    }]}).encode(),
                },
            ),
            RawRecord(
                artifact='cache', dataset='entries', parser='chromium_cache', ordinal=4,
                source_path=source, source_locator='conversation',
                record={
                    'key': 'https://chatgpt.com/backend-api/conversations/conversation-9',
                    'metadata': {'response_time': '2026-09-19T16:03:32Z'},
                    'body': json.dumps({
                        'title': 'PNG 확인', 'current_node': 'assistant-9',
                        'messages': [{
                            'id': 'message-9', 'author': {'role': 'user'},
                            'create_time': 1789833770,
                            'content': {'content_type': 'text', 'parts': ['이 이미지를 봐줘']},
                            'metadata': {'attachments': [{
                                'id': file_id, 'name': '[GPT_WEB_A09-2].png',
                                'mime_type': 'image/png', 'size': len(png),
                                'width': 1448, 'height': 1086, 'source': 'local',
                                'library_file_id': 'libfile-9',
                            }]},
                        }],
                    }, ensure_ascii=False).encode(),
                },
            ),
        ]
        obs = process_surface(rows, load_profile('chatgpt_web'), Ids())
        metadata = next(item for item in obs if item.fact_type == 'file_metadata')
        content = next(item for item in obs if item.fact_type == 'file_content')
        library = next(item for item in obs if item.fact_type == 'library_file')
        attachment = next(item for item in obs if item.fact_type == 'conversation_attachment')
        snapshot = next(item for item in obs if item.fact_type == 'conversation_snapshot')

        self.assertEqual(metadata.fact['filename'], '[GPT_WEB_A09-2].png')
        self.assertEqual(content.fact['content_type_detected'], 'image/png')
        self.assertEqual(content.fact['size'], len(png))
        self.assertEqual(content.fact['width'], 1448)
        self.assertEqual(content.fact['height'], 1086)
        self.assertNotIn('body', content.fact)
        self.assertEqual(library.timestamps['ts_event'], '2026-09-19T16:02:48Z')
        self.assertTrue(library.fact['is_library_file'])
        self.assertEqual(attachment.fact['conversation_id'], 'conversation-9')
        self.assertEqual(attachment.fact['message_id'], 'message-9')
        self.assertEqual(attachment.fact['source'], 'local')
        self.assertEqual(snapshot.fact['attachment_file_ids'], [file_id])

    def test_a10_download_fields_and_python_body_are_normalized(self):
        conversation_id = 'conversation-10'
        message_id = 'message-10'
        file_id = 'file-10'
        filename = '[GPT_WEB_10_retry_filename]'
        payload = (
            b'#!/usr/bin/env python3\n'
            b'import json\n\n'
            b'def main():\n'
            b'    print(json.dumps({"ok": True}))\n'
        )
        cache_source = r'C:\Cases\A10\Cache\Cache_Data'
        history_source = r'C:\Cases\A10\History'
        content_url = (
            'https://chatgpt.com/backend-api/estuary/content'
            f'?id={file_id}&fn=%5BGPT_WEB_10_retry_filename%5D&cd=attachment'
        )
        rows = [
            RawRecord(
                artifact='cache', dataset='entries', parser='chromium_cache', ordinal=1,
                source_path=cache_source, source_locator='intent',
                record={
                    'key': (
                        'https://chatgpt.com/backend-api/conversation/'
                        f'{conversation_id}/interpreter/download?message_id={message_id}'
                        '&sandbox_path=%2Fmnt%2Fdata%2F%255BGPT_WEB_10_retry_filename%255D'
                        '&download_intent=true'
                    ),
                    'metadata': {'response_time': '2026-09-20T01:04:54Z'},
                    'body': b'{}',
                },
            ),
            RawRecord(
                artifact='cache', dataset='entries', parser='chromium_cache', ordinal=2,
                source_path=cache_source, source_locator='content',
                record={
                    'key': content_url,
                    'metadata': {'response_time': '2026-09-20T01:04:55Z'},
                    'body': payload,
                },
            ),
            RawRecord(
                artifact='history', dataset='downloads', parser='chromium_history', ordinal=3,
                source_path=history_source, source_locator='table=downloads;row=6',
                record={
                    'id': 6,
                    'tab_url': f'https://chatgpt.com/c/{conversation_id}',
                    'target_path': rf'C:\Users\vboxuser\Downloads\{filename}',
                    'current_path': rf'C:\Users\vboxuser\Downloads\{filename}',
                    'state': 1, 'interrupt_reason': 0,
                    'received_bytes': len(payload), 'total_bytes': len(payload),
                    'start_time': 13433504695129516,
                    'end_time': 13433504714463640,
                },
            ),
            RawRecord(
                artifact='history', dataset='downloads_url_chains',
                parser='chromium_history', ordinal=4,
                source_path=history_source,
                source_locator='table=downloads_url_chains;row=6',
                record={'id': 6, 'chain_index': 0, 'url': content_url},
            ),
        ]
        observations = process_surface(rows, load_profile('chatgpt_web'), Ids())
        intent = next(item for item in observations if item.fact_type == 'download_request')
        content = next(item for item in observations if item.fact_type == 'file_content')
        browser = next(item for item in observations if item.fact_type == 'browser_download')
        chain = next(item for item in observations if item.fact_type == 'download_url_chain')

        self.assertEqual(intent.fact['message_id'], message_id)
        self.assertEqual(intent.fact['sandbox_path'], f'/mnt/data/{filename}')
        self.assertEqual(intent.fact['download_intent'], 'true')
        self.assertEqual(content.fact['filename'], filename)
        self.assertEqual(content.fact['content_type_detected'], 'text/x-python')
        self.assertTrue(content.fact['python_source_detected'])
        self.assertEqual(content.fact['line_count'], 5)
        self.assertEqual(browser.fact['conversation_id'], conversation_id)
        self.assertEqual(browser.fact['target_filename'], filename)
        self.assertTrue(browser.fact['download_complete'])
        self.assertEqual(chain.fact['file_id'], file_id)
        self.assertEqual(chain.fact['filename'], filename)


if __name__ == '__main__':
    unittest.main()
