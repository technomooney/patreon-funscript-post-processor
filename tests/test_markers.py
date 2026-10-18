import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import markers  # noqa: E402
import consolidate_packs  # noqa: E402


class MarkerTests(unittest.TestCase):
    def test_skips_for_either_marker(self):
        self.assertTrue(markers.skips({'.manual'}))
        self.assertTrue(markers.skips(['a', '.consolidated']))
        self.assertFalse(markers.skips(['a.mp4']))

    def test_consolidate_writes_only_consolidated(self):
        with tempfile.TemporaryDirectory() as t:
            folder = os.path.join(t, 'post')
            os.makedirs(folder)
            os.environ.setdefault('ACTION_LOG_POINTER_PATH', os.path.join(t, 'p.json'))
            consolidate_packs._mark_consolidated(t, folder, {'pack_folder': t, 'video': 'v', 'orig_folder': folder}, 'n')
            self.assertEqual(sorted(f for f in os.listdir(folder) if f.startswith('.')), ['.consolidated', '.folder_log.json'] if os.path.exists(os.path.join(folder, '.folder_log.json')) else ['.consolidated'])
            self.assertFalse(os.path.exists(os.path.join(folder, '.manual')))


if __name__ == '__main__':
    unittest.main()
