"""Optional SURFACE-SPECIFIC FIRST-STAGE parsers.

Only use this package when the native artifact format itself is surface-specific.
Return ``RawRecord`` objects and keep ``RawRecord.record`` limited to fields and
values actually recovered from that artifact record.

Example signature:

    def parse_raw(input_root, profile):
        yield RawRecord(...)

Do NOT create Observations here. Surface meaning belongs in mappings.py.
"""
