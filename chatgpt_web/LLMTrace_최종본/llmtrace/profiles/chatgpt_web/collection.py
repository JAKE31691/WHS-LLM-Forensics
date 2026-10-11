"""ChatGPT Web collection definition.

All paths are Windows NTFS volume-root-relative.  The common acquisition backend
uses these definitions for E01/RAW/VMDK extraction; the same profile can also
use the parser plan against an already-collected folder.
"""

_BROWSER_ROOTS = [
    r'Users\*\AppData\Local\Google\Chrome\User Data\*',
]

# ChatGPT Web is intentionally Chrome-only.  The acquisition exclusion keeps
# Edge files out of new image extractions, while SOURCE_PATH_EXCLUDES also
# protects runs against already-collected folders that happen to contain Edge.
SOURCE_PATH_EXCLUDES = [r'Microsoft\Edge\User Data']

_include = []
for root in _BROWSER_ROOTS:
    _include += [
        root + r'\History',
        root + r'\Favicons',
        root + r'\DIPS',
        root + r'\Preferences',
        root + r'\Network\Cookies',
        root + r'\Cookies',              # older Chromium layouts
        root + r'\Sessions\**',
        root + r'\Session Storage\**',
        root + r'\Local Storage\**',
        root + r'\IndexedDB\**',
        root + r'\Cache\Cache_Data\**',
    ]

COLLECTION = {
    'image': {
        'syntax': 'glob',
        'include': _include,
        'exclude': [r'Users\*\AppData\Local\Microsoft\Edge\User Data\**'],
        'noise': [
            r'**\Code Cache\**',
            r'**\GPUCache\**',
            r'**\DawnCache\**',
            r'**\Crashpad\**',
        ],
        'max_file_bytes': 512 * 1024 * 1024,
    },
}

# Common parser composition. Implement each parser once under common/parsers.
PARSER_PLAN = [
    {'parser': 'chromium_history', 'artifact': 'history'},
    {'parser': 'chromium_cache', 'artifact': 'cache'},
    {'parser': 'chromium_local_storage', 'artifact': 'local_storage'},
    {'parser': 'chromium_session_storage', 'artifact': 'session_storage'},
    {'parser': 'chromium_sessions', 'artifact': 'sessions'},
    {'parser': 'chromium_cookies', 'artifact': 'cookies'},
    {'parser': 'chromium_preferences', 'artifact': 'preferences'},
    {'parser': 'chromium_dips', 'artifact': 'dips'},
    {'parser': 'chromium_favicons', 'artifact': 'favicons'},
    {'parser': 'chromium_indexeddb', 'artifact': 'indexeddb'},
]
