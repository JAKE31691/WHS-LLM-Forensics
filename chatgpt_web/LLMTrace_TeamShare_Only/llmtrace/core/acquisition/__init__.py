"""Common disk-image acquisition backend shared by all LLM surfaces.

The EWF/RAW/VMDK, partition, NTFS, WOF and MFT implementation was migrated
from the earlier Claude prototype.  Only the artifact-selection policy moved
out: each surface now supplies that through ``profiles/<surface>/collection.py``.
"""

from .formats import open_image, ImageError, describe
from .partition import read_partitions, Partition
from .ntfs import NtfsVolume, NtfsError
from .extract import extract_case, ExtractResult
from .manager import (
    SUPPORTED_IMAGE_EXTS,
    collection_definition,
    discover_collection_files,
    extract_image,
    image_collection_spec,
    is_disk_image,
    normalize_rel,
    prepare_input,
)

__all__ = [
    'open_image', 'ImageError', 'describe', 'read_partitions', 'Partition',
    'NtfsVolume', 'NtfsError', 'extract_case', 'ExtractResult',
    'SUPPORTED_IMAGE_EXTS', 'collection_definition', 'discover_collection_files',
    'extract_image', 'image_collection_spec', 'is_disk_image', 'normalize_rel',
    'prepare_input',
]
