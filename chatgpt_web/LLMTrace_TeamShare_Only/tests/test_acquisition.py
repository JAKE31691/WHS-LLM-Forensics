import re
import struct
import unittest

from llmtrace.core.acquisition.manager import _glob_to_regex, image_collection_spec
from llmtrace.core.profile import load_profile
from llmtrace.core.acquisition.partition import read_partitions


class AcquisitionTest(unittest.TestCase):
    def test_ntfs_logical_volume_is_not_misread_as_mbr(self):
        boot = bytearray(512)
        boot[0:3] = b'\xebR\x90'
        boot[3:11] = b'NTFS    '
        # Deliberately place MBR-looking garbage in the partition table area;
        # logical NTFS boot sectors use these bytes for bootstrap code.
        boot[446 + 4] = 0x72
        struct.pack_into('<II', boot, 446 + 8, 1920221984, 1816210284)
        boot[510:512] = b'\x55\xaa'

        class Reader:
            sector_size = 512
            size = 80 * 1024 * 1024

            def pread(self, offset, size):
                if offset == 0:
                    return bytes(boot[:size])
                return b'\x00' * size

        parts = read_partitions(Reader())
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].scheme, '없음(볼륨 이미지)')
        self.assertEqual(parts[0].start_lba, 0)
        self.assertEqual(parts[0].fs, 'NTFS')

    def test_volume_root_glob(self):
        rx = re.compile('^' + _glob_to_regex(
            r'Users\*\AppData\Local\Google\Chrome\User Data\*\History'
        ), re.I)
        self.assertTrue(rx.search(r'Users\alice\AppData\Local\Google\Chrome\User Data\Default\History'))
        self.assertTrue(rx.search(r'Users\bob\AppData\Local\Google\Chrome\User Data\Profile 1\History'))
        self.assertFalse(rx.search(r'Analysis\copy\Users\alice\AppData\Local\Google\Chrome\User Data\Default\History'))

    def test_recursive_glob(self):
        rx = re.compile('^' + _glob_to_regex(
            r'Users\*\AppData\Local\Google\Chrome\User Data\*\Local Storage\**'
        ), re.I)
        self.assertTrue(rx.search(
            r'Users\alice\AppData\Local\Google\Chrome\User Data\Default\Local Storage\leveldb\000003.log'
        ))

    def test_chatgpt_profile_supplies_image_paths(self):
        profile = load_profile('chatgpt_web')
        spec = image_collection_spec(profile)
        self.assertGreaterEqual(len(spec['include_patterns']), 10)
        self.assertEqual(spec['collection_meta']['profile'], 'chatgpt_web')


if __name__ == '__main__':
    unittest.main()

class ClaudeAcquisitionTest(unittest.TestCase):
    def test_original_claude_default_paths_migrated_to_profile(self):
        profile = load_profile('claude_app')
        spec = image_collection_spec(profile)
        compiled = [re.compile('^' + x, re.I) for x in spec['include_patterns']]
        samples = [
            r'Users\alice\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\IndexedDB\000003.log',
            r'Users\alice\AppData\Roaming\Claude\Local Storage\leveldb\000003.log',
            r'Users\alice\AppData\Local\Claude\logs\main.log',
            r'Users\alice\AppData\Local\AnthropicFoo\state.json',
            r'Users\alice\AppData\Roaming\AnthropicBar\state.json',
            r'ProgramData\Packages\Claude_test\state.dat',
        ]
        for sample in samples:
            self.assertTrue(any(rx.search(sample) for rx in compiled), sample)
        self.assertFalse(any(rx.search(r'Analysis\copy\Users\alice\AppData\Roaming\Claude\x') for rx in compiled))
