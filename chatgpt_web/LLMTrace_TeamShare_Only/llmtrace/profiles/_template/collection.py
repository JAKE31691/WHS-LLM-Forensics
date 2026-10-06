"""Copy this file into a new surface profile and define only what that surface needs."""

COLLECTION = {
    'image': {
        # Volume-root-relative Windows paths. ``*`` = one component, ``**`` = recursive.
        'syntax': 'glob',
        'include': [
            # r'Users\*\AppData\...\ArtifactFile',
            # r'Users\*\AppData\...\ArtifactDirectory\**',
        ],
        'exclude': [],
        'noise': [],
        'max_file_bytes': 512 * 1024 * 1024,
    },
}

PARSER_PLAN = [
    # {'parser': 'chromium_history', 'artifact': 'history'},
]
