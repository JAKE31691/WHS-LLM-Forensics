"""SECOND-STAGE surface interpretation.

First-stage common parsers MUST NOT add surface meaning. They return RawRecord
objects whose ``record`` is the recovered native artifact record.

Define either:

    RAW_MAPPINGS = [...]

for simple declarative transformations, or:

    def process(records, ids, profile): ...

for complex surface-specific interpretation.
"""

RAW_MAPPINGS = []
