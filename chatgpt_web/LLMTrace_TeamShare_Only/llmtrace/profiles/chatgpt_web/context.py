CONTEXT_SECTIONS = [
    {
        'id': 'account',
        'title': 'Account / Authentication Context',
        'fact_types': ['account_observation', 'account_session_observation', 'cookie_record'],
        'description': 'Observed account/session state. A state value is not automatically a user action.',
    },
    {
        'id': 'memory',
        'title': 'Memory / Personalization Context',
        'fact_types': [
            'memory_list_snapshot', 'memory_summary_checksum',
            'memory_correction_capability',
        ],
        'description': 'Memory state and capability observations. These do not alone identify the exact edit method.',
    },
    {
        'id': 'conversation',
        'title': 'Conversation / Message Context',
        'fact_types': [
            'conversation_visit', 'conversation_load', 'conversation_snapshot',
            'conversation_draft', 'conversation_draft_cleared',
            'conversation_stream_status', 'conversation_attachment',
            'conversation_history_state', 'active_conversation_list',
        ],
    },
    {
        'id': 'files',
        'title': 'File Context',
        'fact_types': [
            'file_metadata', 'file_content_reference', 'file_content',
            'browser_download', 'download_url_chain', 'library_file',
        ],
    },
    {
        'id': 'connectors',
        'title': 'Connector / External Service Context',
        'fact_types': ['url_safety_check'],
        'description': (
            'External-service result links and connector-related evidence. '
            'A link does not by itself prove a specific provider sub-action.'
        ),
    },
    {
        'id': 'state',
        'title': 'Current / Residual State',
        'evidence_kinds': ['state', 'residual'],
        'description': 'Update-type and residual values are shown as trackable state, not automatically as timeline events.',
    },
    {
        'id': 'events',
        'title': 'Event Evidence',
        'evidence_kinds': ['event'],
    },
]
