"""Claude App image collection policy migrated from the original prototype.

This reproduces the old default ``CLAUDE_PATTERNS`` behavior while leaving all
EWF/NTFS extraction mechanics in the shared core acquisition backend.
"""

COLLECTION = {
    'image': {
        'syntax': 'glob',
        'include': [
            r'Users\*\AppData\Local\Packages\Claude_*\**',
            r'Users\*\AppData\Roaming\Claude\**',
            r'Users\*\AppData\Local\Claude\**',
            r'Users\*\AppData\Local\Anthropic*\**',
            r'Users\*\AppData\Roaming\Anthropic*\**',
            r'ProgramData\Packages\Claude_*\**',
        ],
        'exclude': [],
        'noise': [
            r'**\Cache\Cache_Data\**',
            r'**\Code Cache\**',
            r'**\GPUCache\**',
            r'**\DawnCache\**',
            r'**\component_crx_cache\**',
            r'**\Crashpad\**',
        ],
        'max_file_bytes': 512 * 1024 * 1024,
    },
}

# Claude Desktop uses Chromium/Electron stores.  These common parsers recover
# native records; Claude meaning, when validated, belongs in mappings.py.
PARSER_PLAN = [
    {'parser': 'chromium_cache', 'artifact': 'cache'},
    {'parser': 'chromium_local_storage', 'artifact': 'local_storage'},
    {'parser': 'chromium_session_storage', 'artifact': 'session_storage'},
    {'parser': 'chromium_sessions', 'artifact': 'sessions'},
    {'parser': 'chromium_preferences', 'artifact': 'preferences'},
    {'parser': 'chromium_cookies', 'artifact': 'cookies'},
    {'parser': 'chromium_indexeddb', 'artifact': 'indexeddb'},
]
