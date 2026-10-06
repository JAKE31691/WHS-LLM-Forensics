import unittest

from llmtrace.core.engine import run_all
from llmtrace.core.profile import load_profile
from llmtrace.core.schema import Ids, Observation

class RuleTest(unittest.TestCase):
    def test_ruleset_defines_all_evidence_levels(self):
        profile = load_profile('chatgpt_web')
        profile.rules.validate_ruleset_shape()

    def test_a02_login_uses_nested_url_query_fields(self):
        scope = 'chrome-default'
        observations = [
            Observation(
                'login-start', 'history', 'History', 'start', 'url_visit',
                {
                    'hostname': 'chatgpt.com', 'path': '/auth/login_with',
                    'query': {
                        'auth_session_logging_id': 'session-2',
                        'connection': 'google-oauth2',
                    },
                    '_source_scope': scope,
                },
                timestamps={'ts_visit': '2026-09-15T01:00:00Z'},
            ),
            Observation(
                'authorize', 'history', 'History', 'authorize', 'url_visit',
                {
                    'hostname': 'auth.openai.com',
                    'path': '/api/accounts/authorize',
                    'query': {
                        'auth_session_logging_id': 'session-2',
                        'state': 'oauth-state-2',
                    },
                    '_source_scope': scope,
                },
                timestamps={'ts_visit': '2026-09-15T01:00:02Z'},
            ),
            Observation(
                'callback', 'history', 'History', 'callback', 'url_visit',
                {
                    'hostname': 'chatgpt.com',
                    'path': '/api/auth/callback/openai',
                    'query': {'code': 'code-2', 'state': 'oauth-state-2'},
                    '_source_scope': scope,
                },
                timestamps={'ts_visit': '2026-09-15T01:00:05Z'},
            ),
            Observation(
                'account', 'cache', 'Cache', 'me', 'account_observation',
                {
                    'hostname': 'chatgpt.com', 'path': '/backend-api/me',
                    'account_id': 'account-2', '_source_scope': scope,
                },
                timestamps={'ts_record': '2026-09-15T01:00:06Z'},
            ),
        ]
        profile = load_profile('chatgpt_web')
        inferences, _ = run_all(observations, profile.rules, Ids())
        login = next(
            item for item in inferences
            if item.detail.get('base_rule_id') == 'R-LOGIN-01'
        )

        self.assertEqual(login.rule_id, 'R-LOGIN-STD-01')
        self.assertEqual(login.confidence, 'confirmed')

    def test_a02_login_rejects_mismatched_nested_oauth_state(self):
        observations = [
            Observation(
                'login-start', 'history', 'History', 'start', 'url_visit',
                {
                    'hostname': 'chatgpt.com', 'path': '/auth/login_with',
                    'query': {'auth_session_logging_id': 'session-2'},
                },
                timestamps={'ts_visit': '2026-09-15T01:00:00Z'},
            ),
            Observation(
                'authorize', 'history', 'History', 'authorize', 'url_visit',
                {
                    'hostname': 'auth.openai.com',
                    'path': '/api/accounts/authorize',
                    'query': {
                        'auth_session_logging_id': 'session-2',
                        'state': 'oauth-state-a',
                    },
                },
                timestamps={'ts_visit': '2026-09-15T01:00:02Z'},
            ),
            Observation(
                'callback', 'history', 'History', 'callback', 'url_visit',
                {
                    'hostname': 'chatgpt.com',
                    'path': '/api/auth/callback/openai',
                    'query': {'code': 'code-2', 'state': 'oauth-state-b'},
                },
                timestamps={'ts_visit': '2026-09-15T01:00:05Z'},
            ),
        ]
        profile = load_profile('chatgpt_web')
        inferences, _ = run_all(observations, profile.rules, Ids())

        self.assertFalse(any(
            item.detail.get('base_rule_id') == 'R-LOGIN-01'
            for item in inferences
        ))

    def test_tempchat_use_suppresses_open(self):
        obs = [
            Observation('o1','history','History','v1','url_visit',
                        {'hostname':'chatgpt.com','path':'/','query':{'temporary-chat':'true'}},
                        timestamps={'ts_visit':'2026-09-15T01:00:00.000Z'}),
            Observation('o2','cookies','Cookies','row1','cookie_record',
                        {'name':'history_off_conv-123'}),
            Observation('o3','cache','Cache','entry1','conversation_stream_status',
                        {'hostname':'chatgpt.com','path':'/backend-api/conversation/conv-123/stream_status',
                         'json':{'status':'IS_STREAMING'}},
                        timestamps={'ts_record':'2026-09-15T01:00:02.000Z'}),
        ]
        profile = load_profile('chatgpt_web')
        infs, _ = run_all(obs, profile.rules, Ids())
        rule_ids = [x.rule_id for x in infs]
        self.assertIn('R-TEMPCHAT-USE-MIN-01', rule_ids)
        self.assertNotIn('R-TEMPCHAT-OPEN-MIN-01', rule_ids)

    def test_a12_tempchat_standard_binds_same_conversation_and_stream_end(self):
        conversation_id = 'temp-conversation-12'
        scope = 'chrome-default'
        observations = [
            Observation(
                'route-history', 'history', 'History', 'visit', 'url_visit',
                {
                    'hostname': 'chatgpt.com', 'path': '/ko-KR/',
                    'query': {'temporary-chat': 'true'},
                    '_source_scope': scope,
                },
                timestamps={'ts_visit': '2026-09-20T05:39:18Z'},
            ),
            Observation(
                'route-data', 'cache', 'Cache', 'route-data', 'http_cache_entry',
                {
                    'hostname': 'chatgpt.com', 'path': '/ko-KR.data',
                    'query': {'temporary-chat': 'true'},
                    '_source_scope': scope,
                },
                timestamps={'ts_record': '2026-09-20T05:39:18.100Z'},
            ),
            Observation(
                'history-off', 'cookies', 'Cookies', 'history-off', 'cookie_record',
                {
                    'name': f'history_off_{conversation_id}',
                    'conversation_id': conversation_id,
                    '_source_scope': scope,
                },
                timestamps={'ts_created': '2026-09-20T05:39:48.327004Z'},
            ),
            Observation(
                'conv-key', 'cookies', 'Cookies', 'conv-key', 'cookie_record',
                {
                    'name': f'conv_key_{conversation_id}',
                    'conversation_id': conversation_id,
                    '_source_scope': scope,
                },
                timestamps={'ts_created': '2026-09-20T05:39:48.327124Z'},
            ),
            Observation(
                'wrong-stream', 'cache', 'Cache', 'wrong-stream',
                'conversation_stream_status',
                {
                    'hostname': 'chatgpt.com',
                    'path': '/backend-api/conversation/other/stream_status',
                    'conversation_id': 'other',
                    'json': {'status': 'IS_STREAMING'},
                    '_source_scope': scope,
                },
                timestamps={'ts_record': '2026-09-20T05:39:48.900Z'},
            ),
            Observation(
                'matching-stream', 'cache', 'Cache', 'matching-stream',
                'conversation_stream_status',
                {
                    'hostname': 'chatgpt.com',
                    'path': f'/backend-api/conversation/{conversation_id}/stream_status',
                    'conversation_id': conversation_id,
                    'json': {'status': 'IS_STREAMING'},
                    '_source_scope': scope,
                },
                timestamps={'ts_record': '2026-09-20T05:39:49.237474Z'},
            ),
        ]
        profile = load_profile('chatgpt_web')
        inferences, _ = run_all(observations, profile.rules, Ids())
        use = next(
            item for item in inferences
            if item.detail.get('base_rule_id') == 'R-TEMPCHAT-USE-01'
        )

        self.assertEqual(use.rule_id, 'R-TEMPCHAT-USE-STD-01', use.detail)
        self.assertEqual(use.confidence, 'confirmed')
        self.assertEqual(use.when_from, '2026-09-20T05:39:18Z')
        self.assertEqual(use.when_to, '2026-09-20T05:39:49.237474Z')
        self.assertEqual(
            use.detail['refs']['tempchat_stream']['observation_id'],
            'matching-stream',
        )
        self.assertFalse(any(
            item.detail.get('base_rule_id') == 'R-TEMPCHAT-OPEN-01'
            for item in inferences
        ))

    def test_highest_rule_level_and_history_window(self):
        obs = [
            Observation('h1', 'history', 'History', 'v1', 'url_visit',
                        {'hostname': 'chatgpt.com', 'path': '/'},
                        timestamps={'ts_visit': '2026-09-15T01:00:00+00:00'}),
            Observation('c1', 'cache', 'Cache', 'e1', 'http_cache_entry',
                        {'hostname': 'chatgpt.com', 'path': '/unauth-mweb/a'},
                        timestamps={'ts_record': '2026-09-15T01:00:05+00:00'}),
            Observation('h2', 'history', 'History', 'v2', 'url_visit',
                        {'hostname': 'chatgpt.com', 'path': '/'},
                        timestamps={'ts_visit': '2026-09-15T01:00:10+00:00'}),
        ]
        profile = load_profile('chatgpt_web')
        infs, _ = run_all(obs, profile.rules, Ids())
        access = [item for item in infs if item.behavior_id == 'B01']
        self.assertEqual(len(access), 2)
        self.assertEqual(access[0].rule_id, 'R-ACCESS-STD-01')
        self.assertEqual(access[0].span_sec, 10.0)
        self.assertEqual(access[1].rule_id, 'R-ACCESS-MIN-01')
        self.assertEqual(access[1].when_to, '')
        self.assertIsNone(access[1].span_sec)

    def test_rule_evidence_does_not_cross_browser_profiles(self):
        chrome = 'chrome-default'
        edge = 'edge-default'
        obs = [
            Observation('h1', 'history', 'History', 'v1', 'url_visit',
                        {'hostname': 'chatgpt.com', 'path': '/', '_source_scope': chrome},
                        timestamps={'ts_visit': '2026-09-15T01:00:00+00:00'}),
            Observation('c1', 'cache', 'Cache', 'e1', 'http_cache_entry',
                        {'hostname': 'chatgpt.com', 'path': '/unauth-mweb/a', '_source_scope': edge},
                        timestamps={'ts_record': '2026-09-15T01:00:05+00:00'}),
        ]
        profile = load_profile('chatgpt_web')
        infs, _ = run_all(obs, profile.rules, Ids())
        access = [item for item in infs if item.behavior_id == 'B01']
        self.assertEqual(len(access), 1)
        self.assertEqual(access[0].rule_id, 'R-ACCESS-MIN-01')

    def test_a07_prompt_edit_requires_user_branch_replacement(self):
        scope = 'chrome-default'
        before_sequence = [
            {'message_id': 'common', 'role': 'assistant', 'content_sha256': 'same'},
            {'message_id': 'old-user', 'role': 'user', 'content_sha256': 'old'},
            {'message_id': 'old-answer', 'role': 'assistant', 'content_sha256': 'old-answer'},
        ]
        after_sequence = [
            {'message_id': 'common', 'role': 'assistant', 'content_sha256': 'same'},
            {'message_id': 'new-user', 'role': 'user', 'content_sha256': 'new'},
            {'message_id': 'new-answer', 'role': 'assistant', 'content_sha256': 'new-answer'},
        ]
        obs = [
            Observation(
                'before', 'cache', 'Cache', 'old', 'conversation_snapshot',
                {'hostname': 'chatgpt.com', 'conversation_id': 'conv-1',
                 'current_node': 'old-answer', 'message_sequence': before_sequence,
                 '_source_scope': scope},
                timestamps={'ts_record': '2026-09-11T07:22:35+00:00'},
            ),
            Observation(
                'stream', 'cache', 'Cache', 'stream', 'conversation_stream_status',
                {'hostname': 'chatgpt.com', 'conversation_id': 'conv-1',
                 'json': {'status': 'IS_STREAMING'}, '_source_scope': scope},
                timestamps={'ts_record': '2026-09-11T08:52:44+00:00'},
            ),
            Observation(
                'after', 'cache', 'Cache', 'new', 'conversation_snapshot',
                {'hostname': 'chatgpt.com', 'conversation_id': 'conv-1',
                 'current_node': 'new-answer', 'message_sequence': after_sequence,
                 '_source_scope': scope},
                timestamps={'ts_record': '2026-09-11T08:52:45+00:00'},
            ),
        ]
        profile = load_profile('chatgpt_web')
        infs, _ = run_all(obs, profile.rules, Ids())
        edits = [item for item in infs if item.behavior_id == 'B07']
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].rule_id, 'R-PROMPT-EDIT-STD-01')
        self.assertEqual(edits[0].confidence, 'confirmed')

    def test_a07_does_not_match_single_snapshot_or_append_only_turn(self):
        scope = 'chrome-default'
        shared = [
            {'message_id': 'u1', 'role': 'user', 'content_sha256': 'p1'},
            {'message_id': 'a1', 'role': 'assistant', 'content_sha256': 'a1'},
        ]
        obs = [
            Observation(
                'before', 'cache', 'Cache', 'old', 'conversation_snapshot',
                {'conversation_id': 'conv-1', 'current_node': 'a1',
                 'message_sequence': shared, '_source_scope': scope},
                timestamps={'ts_record': '2026-09-11T07:22:35+00:00'},
            ),
            Observation(
                'after', 'cache', 'Cache', 'new', 'conversation_snapshot',
                {'conversation_id': 'conv-1', 'current_node': 'a2',
                 'message_sequence': shared + [
                     {'message_id': 'u2', 'role': 'user', 'content_sha256': 'p2'},
                     {'message_id': 'a2', 'role': 'assistant', 'content_sha256': 'a2'},
                 ], '_source_scope': scope},
                timestamps={'ts_record': '2026-09-11T08:52:45+00:00'},
            ),
        ]
        profile = load_profile('chatgpt_web')
        infs, _ = run_all(obs, profile.rules, Ids())
        self.assertFalse([item for item in infs if item.behavior_id == 'B07'])

    def test_a08_memory_checksum_transition_confirms_state_change_not_method(self):
        scope = 'chrome-default'
        obs = [
            Observation(
                'memory-before', 'cache', 'Cache', 'before', 'memory_summary_checksum',
                {'source_checksum': 'source-old', 'cached_source_checksum': 'cached-old',
                 'cached_generated_at': '2026-09-18T22:02:06+00:00',
                 'is_stale': True, '_source_scope': scope},
                timestamps={'ts_record': '2026-09-19T01:19:00+00:00'},
            ),
            Observation(
                'memory-after', 'cache', 'Cache', 'after', 'memory_summary_checksum',
                {'source_checksum': 'source-new', 'cached_source_checksum': 'source-new',
                 'cached_generated_at': '2026-09-19T01:27:23+00:00',
                 'is_stale': False, '_source_scope': scope},
                timestamps={'ts_record': '2026-09-19T12:09:40+00:00'},
            ),
        ]
        profile = load_profile('chatgpt_web')
        infs, _ = run_all(obs, profile.rules, Ids())
        changes = [
            item for item in infs
            if item.rule_id == 'R-MEMORY-STATE-CHANGE-STD-01'
        ]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].behavior_id, 'B08')
        self.assertEqual(changes[0].confidence, 'confirmed')
        self.assertIn('프롬프트를 통한 메모리 요청', changes[0].alternatives)

    def test_a08_unchanged_checksum_does_not_create_state_change(self):
        scope = 'chrome-default'
        obs = [
            Observation(
                'a', 'cache', 'Cache', 'a', 'memory_summary_checksum',
                {'source_checksum': 'same', '_source_scope': scope},
                timestamps={'ts_record': '2026-09-19T01:00:00+00:00'},
            ),
            Observation(
                'b', 'cache', 'Cache', 'b', 'memory_summary_checksum',
                {'source_checksum': 'same', '_source_scope': scope},
                timestamps={'ts_record': '2026-09-19T02:00:00+00:00'},
            ),
        ]
        profile = load_profile('chatgpt_web')
        infs, _ = run_all(obs, profile.rules, Ids())
        self.assertFalse([
            item for item in infs
            if item.detail.get('base_rule_id') == 'R-MEMORY-STATE-CHANGE-01'
        ])

    def test_a09_file_object_without_upload_time_is_only_presumed(self):
        obs = [Observation(
            'file-object', 'cache', 'Cache', 'simple', 'file_metadata',
            {
                'file_id': 'file-9', 'filename': 'evidence.png',
                'mime_type': 'image/png', '_source_scope': 'chrome-default',
            },
            timestamps={'ts_record': '2026-09-19T16:02:49Z'},
        )]
        profile = load_profile('chatgpt_web')
        infs, _ = run_all(obs, profile.rules, Ids())
        matches = [item for item in infs if item.behavior_id == 'B09']
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].rule_id, 'R-FILE-UPLOAD-MIN-01')
        self.assertEqual(matches[0].confidence, 'presumed')

    def test_a09_service_upload_time_and_message_attachment_reach_standard(self):
        scope = 'chrome-default'
        obs = [
            Observation(
                'library-file', 'cache', 'Cache', 'library', 'library_file',
                {
                    'file_id': 'file-9', 'filename': 'evidence.png',
                    'mime_type': 'image/png', 'size': 952194,
                    'file_upload_time': '2026-09-19T16:02:48Z',
                    '_source_scope': scope,
                },
                timestamps={'ts_event': '2026-09-19T16:02:48Z'},
            ),
            Observation(
                'attachment', 'cache', 'Cache', 'conversation',
                'conversation_attachment',
                {
                    'conversation_id': 'conversation-9', 'message_id': 'message-9',
                    'message_role': 'user', 'message_create_time': '2026-09-19T16:03:00Z',
                    'file_id': 'file-9', 'filename': 'evidence.png',
                    'mime_type': 'image/png', 'source': 'local',
                    '_source_scope': scope,
                },
                timestamps={'ts_event': '2026-09-19T16:03:00Z'},
            ),
        ]
        profile = load_profile('chatgpt_web')
        infs, _ = run_all(obs, profile.rules, Ids())
        upload = next(item for item in infs if item.detail.get('base_rule_id') == 'R-FILE-UPLOAD-01')
        attachment = next(item for item in infs if item.detail.get('base_rule_id') == 'R-FILE-ATTACHMENT-01')
        self.assertEqual(upload.rule_id, 'R-FILE-UPLOAD-STD-01')
        self.assertEqual(upload.confidence, 'confirmed')
        self.assertEqual(attachment.rule_id, 'R-FILE-ATTACHMENT-STD-01')
        self.assertEqual(attachment.confidence, 'confirmed')

    def test_a10_completed_download_reaches_extended_and_binds_matching_chain(self):
        scope = 'chrome-default'
        conversation_id = 'conversation-10'
        file_id = 'file-10'
        content_url = (
            'https://chatgpt.com/backend-api/estuary/content'
            f'?id={file_id}&fn=evidence.py&cd=attachment'
        )
        observations = [
            Observation(
                'intent', 'cache', 'Cache', 'intent', 'download_request',
                {
                    'hostname': 'chatgpt.com',
                    'path': f'/backend-api/conversation/{conversation_id}/interpreter/download',
                    'query': {'download_intent': 'true'},
                    'conversation_id': conversation_id,
                    'message_id': 'message-10',
                    '_source_scope': scope,
                },
                timestamps={'ts_record': '2026-09-20T01:04:54Z'},
            ),
            Observation(
                'estuary', 'cache', 'Cache', 'content', 'file_content_reference',
                {
                    'hostname': 'chatgpt.com',
                    'path': '/backend-api/estuary/content',
                    'url': content_url,
                    'query': {'id': file_id, 'cd': 'attachment'},
                    'file_id': file_id, 'filename': 'evidence.py',
                    '_source_scope': scope,
                },
                timestamps={'ts_record': '2026-09-20T01:04:55Z'},
            ),
            Observation(
                'browser', 'history', 'History', 'download', 'browser_download',
                {
                    'download_id': 6,
                    'conversation_id': conversation_id,
                    'tab_url': f'https://chatgpt.com/c/{conversation_id}',
                    'target_path': r'C:\Users\vboxuser\Downloads\evidence.py',
                    'received_bytes': 26346, 'total_bytes': 26346,
                    'state': 1, 'interrupt_reason': 0,
                    'download_complete': True,
                    '_source_scope': scope,
                },
                timestamps={'ts_event': '2026-09-20T01:04:55.100Z'},
                seq=10,
            ),
            Observation(
                'wrong-chain', 'history', 'History', 'wrong', 'download_url_chain',
                {
                    'download_id': 5, 'file_id': 'wrong-file',
                    'url': 'https://chatgpt.com/backend-api/estuary/content?id=wrong-file',
                    '_source_scope': scope,
                },
                seq=11,
            ),
            Observation(
                'chain', 'history', 'History', 'chain', 'download_url_chain',
                {
                    'download_id': 6, 'file_id': file_id, 'url': content_url,
                    '_source_scope': scope,
                },
                seq=12,
            ),
        ]
        profile = load_profile('chatgpt_web')
        inferences, _ = run_all(observations, profile.rules, Ids())
        download = next(
            item for item in inferences
            if item.detail.get('base_rule_id') == 'R-DOWNLOAD-01'
        )
        self.assertEqual(download.rule_id, 'R-DOWNLOAD-EXT-01')
        self.assertEqual(download.confidence, 'confirmed')
        self.assertEqual(
            download.detail['refs']['download_url_chain']['observation_id'],
            'chain',
        )

    def test_a10_intent_without_completed_history_is_only_minimum(self):
        observations = [Observation(
            'intent', 'cache', 'Cache', 'intent', 'download_request',
            {
                'hostname': 'chatgpt.com',
                'path': '/backend-api/conversation/conversation-10/interpreter/download',
                'query': {'download_intent': 'true'},
                'conversation_id': 'conversation-10',
                '_source_scope': 'chrome-default',
            },
            timestamps={'ts_record': '2026-09-20T01:04:54Z'},
        )]
        profile = load_profile('chatgpt_web')
        inferences, _ = run_all(observations, profile.rules, Ids())
        download = next(
            item for item in inferences
            if item.detail.get('base_rule_id') == 'R-DOWNLOAD-01'
        )
        self.assertEqual(download.rule_id, 'R-DOWNLOAD-MIN-01')
        self.assertEqual(download.confidence, 'suspected')

    def test_a11_active_list_removal_is_confirmed_but_deletion_is_not_claimed(self):
        conversation_id = 'conversation-removed'
        common = {
            'conversation_id': conversation_id,
            'title': '인사 대화',
            'user_id': 'user-11',
            'workspace_account_id': 'workspace-11',
            '_source_scope': 'chrome-default',
        }
        observations = [
            Observation(
                'previous', 'local_storage', 'Local Storage', 'previous',
                'conversation_history_state',
                {
                    **common,
                    'membership': True,
                    'transition_candidate': True,
                    'state_role': 'residual',
                    'source_class': 'local_storage_residual',
                },
                timestamps={'ts_record': '2026-09-20T03:36:56.732Z'},
                evidence_kind='residual', seq=835,
            ),
            Observation(
                'current-absence', 'local_storage', 'Local Storage', 'current',
                'conversation_history_state',
                {
                    **common,
                    'membership': False,
                    'previous_membership': True,
                    'transition_type': 'removed_from_active_list',
                    'state_role': 'current',
                    'source_class': 'local_storage_current',
                    'previous_leveldb_seq_number': 835,
                    'current_leveldb_seq_number': 861,
                },
                timestamps={'ts_record': '2026-09-20T05:36:34.123Z'},
                evidence_kind='state', seq=861,
            ),
        ]
        profile = load_profile('chatgpt_web')
        inferences, contexts = run_all(observations, profile.rules, Ids())
        removal = next(
            item for item in inferences
            if item.detail.get('base_rule_id') == 'R-CONVERSATION-ACTIVE-REMOVAL-01'
        )

        self.assertEqual(removal.rule_id, 'R-CONVERSATION-ACTIVE-REMOVAL-STD-01')
        self.assertEqual(removal.behavior_id, 'B11')
        self.assertEqual(removal.confidence, 'confirmed')
        self.assertEqual(removal.when_from, '2026-09-20T03:36:56.732Z')
        self.assertEqual(removal.when_to, '2026-09-20T05:36:34.123Z')
        self.assertIn('대화 보관(archive) 또는 기타 활성 목록 제외 상태', removal.alternatives)
        self.assertTrue(any(
            item.rule_id == 'R-CONVERSATION-REMOVED-01' for item in contexts
        ))
        self.assertFalse(any(
            item.detail.get('base_rule_id') == 'R-ACCOUNT-DELETE-FLOW-01'
            for item in inferences
        ))

    def test_a11_ordinary_logout_marker_does_not_become_account_delete(self):
        observations = [
            Observation(
                'history-logout', 'history', 'History', 'visit', 'url_visit',
                {
                    'hostname': 'chatgpt.com', 'path': '/auth/logout',
                    '_source_scope': 'chrome-default',
                },
                timestamps={'ts_visit': '2026-09-20T02:45:00Z'},
            ),
            Observation(
                'cache-logout', 'cache', 'Cache', 'entry', 'http_cache_entry',
                {
                    'hostname': 'chatgpt.com', 'path': '/auth/logout',
                    'logout_reason': 'logout_confirm_modal',
                    'logout_source': 'modal.logout-confirm-modal',
                    'logout_kind': 'ordinary_logout_confirmation',
                    '_source_scope': 'chrome-default',
                },
                timestamps={'ts_record': '2026-09-20T02:45:01Z'},
            ),
        ]
        profile = load_profile('chatgpt_web')
        inferences, _ = run_all(observations, profile.rules, Ids())

        self.assertTrue(any(
            item.detail.get('base_rule_id') == 'R-LOGOUT-01'
            for item in inferences
        ))
        self.assertFalse(any(
            item.detail.get('base_rule_id') == 'R-ACCOUNT-DELETE-FLOW-01'
            for item in inferences
        ))

    def test_a13_google_connector_link_uses_query_state_and_reaches_standard(self):
        scope = 'chrome-default'
        state = 'oauth_s_a13'
        observations = [
            Observation(
                'oauth-start', 'history', 'History', 'oauth-start', 'url_visit',
                {
                    'hostname': 'accounts.google.com',
                    'path': '/o/oauth2/v2/auth',
                    'query': {
                        'redirect_uri': 'https://chatgpt.com/connector_platform_oauth_redirect',
                        'state': state,
                        'scope': 'openid https://www.googleapis.com/auth/drive',
                    },
                    '_source_scope': scope,
                },
                timestamps={'ts_visit': '2026-09-20T08:07:38Z'},
            ),
            Observation(
                'connector-backend', 'cache', 'Cache', 'connector',
                'http_cache_entry',
                {
                    'hostname': 'chatgpt.com',
                    'path': '/backend-api/aip/connectors/connector-13',
                    '_source_scope': scope,
                },
                timestamps={'ts_record': '2026-09-20T08:08:38Z'},
            ),
            Observation(
                'oauth-callback', 'history', 'History', 'oauth-callback',
                'url_visit',
                {
                    'hostname': 'chatgpt.com',
                    'path': '/connector_platform_oauth_redirect',
                    'query': {'code': 'code-13', 'state': state},
                    '_source_scope': scope,
                },
                timestamps={'ts_visit': '2026-09-20T08:08:32Z'},
            ),
        ]
        profile = load_profile('chatgpt_web')
        inferences, _ = run_all(observations, profile.rules, Ids())
        linked = next(
            item for item in inferences
            if item.detail.get('base_rule_id') == 'R-GOOGLE-CONNECTOR-LINK-01'
        )

        self.assertEqual(linked.rule_id, 'R-GOOGLE-CONNECTOR-LINK-STD-01')
        self.assertEqual(linked.confidence, 'confirmed')
        self.assertEqual(linked.when_from, '2026-09-20T08:07:38Z')
        self.assertEqual(linked.when_to, '2026-09-20T08:08:32Z')

    def test_a13_google_connector_link_rejects_different_oauth_state(self):
        observations = [
            Observation(
                'oauth-start', 'history', 'History', 'oauth-start', 'url_visit',
                {
                    'hostname': 'accounts.google.com',
                    'path': '/o/oauth2/v2/auth',
                    'query': {
                        'redirect_uri': 'https://chatgpt.com/connector_platform_oauth_redirect',
                        'state': 'state-a',
                    },
                },
                timestamps={'ts_visit': '2026-09-20T08:07:38Z'},
            ),
            Observation(
                'oauth-callback', 'history', 'History', 'oauth-callback',
                'url_visit',
                {
                    'hostname': 'chatgpt.com',
                    'path': '/connector_platform_oauth_redirect',
                    'query': {'code': 'code-13', 'state': 'state-b'},
                },
                timestamps={'ts_visit': '2026-09-20T08:08:32Z'},
            ),
        ]
        profile = load_profile('chatgpt_web')
        inferences, _ = run_all(observations, profile.rules, Ids())

        self.assertFalse(any(
            item.detail.get('base_rule_id') == 'R-GOOGLE-CONNECTOR-LINK-01'
            for item in inferences
        ))

    def test_a13_drive_prompt_stream_and_result_link_reach_standard(self):
        scope = 'chrome-default'
        conversation_id = 'conversation-13'
        observations = [
            Observation(
                'draft', 'local_storage', 'Local Storage', 'draft',
                'conversation_draft',
                {
                    'draft_text': (
                        'Google Drive에서 실험 문서를 찾아 확인한 뒤 '
                        '피드백 파일을 업로드해줘.'
                    ),
                    'user_id': 'user-13', '_source_scope': scope,
                },
                timestamps={'ts_modified': '2026-09-21T09:45:08Z'},
            ),
            Observation(
                'visit', 'history', 'History', 'visit', 'conversation_visit',
                {
                    'hostname': 'chatgpt.com',
                    'conversation_id': conversation_id,
                    'title': '드라이브 문서 피드백 작성',
                    '_source_scope': scope,
                },
                timestamps={'ts_visit': '2026-09-21T09:45:28Z'},
            ),
            Observation(
                'stream', 'cache', 'Cache', 'stream',
                'conversation_stream_status',
                {
                    'conversation_id': conversation_id,
                    'json': {'status': 'IS_STREAMING'},
                    '_source_scope': scope,
                },
                timestamps={'ts_record': '2026-09-21T09:45:29Z'},
            ),
            Observation(
                'result-link', 'cache', 'Cache', 'result-link',
                'url_safety_check',
                {
                    'conversation_id': conversation_id,
                    'target_url': 'https://drive.google.com/file/d/file-13/view',
                    'target_hostname': 'drive.google.com',
                    'external_file_id': 'file-13',
                    '_source_scope': scope,
                },
                timestamps={'ts_record': '2026-09-21T09:46:03Z'},
            ),
        ]
        profile = load_profile('chatgpt_web')
        inferences, _ = run_all(observations, profile.rules, Ids())
        result = next(
            item for item in inferences
            if item.detail.get('base_rule_id') == 'R-GOOGLE-DRIVE-CONNECTOR-RESULT-01'
        )

        self.assertEqual(
            result.rule_id, 'R-GOOGLE-DRIVE-CONNECTOR-RESULT-STD-01'
        )
        self.assertEqual(result.confidence, 'confirmed')
        self.assertEqual(result.when_from, '2026-09-21T09:45:08Z')
        self.assertEqual(result.when_to, '2026-09-21T09:46:03Z')

    def test_a13_drive_keywords_in_draft_alone_do_not_prove_execution(self):
        observations = [
            Observation(
                'draft', 'local_storage', 'Local Storage', 'draft',
                'conversation_draft',
                {
                    'draft_text': 'Google Drive에서 문서를 찾아 업로드해줘.',
                },
                timestamps={'ts_modified': '2026-09-21T09:45:08Z'},
            ),
        ]
        profile = load_profile('chatgpt_web')
        inferences, _ = run_all(observations, profile.rules, Ids())

        self.assertFalse(any(
            item.detail.get('base_rule_id')
            == 'R-GOOGLE-DRIVE-CONNECTOR-RESULT-01'
            for item in inferences
        ))

if __name__ == '__main__':
    unittest.main()
