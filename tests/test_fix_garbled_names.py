import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import fix_garbled_names as f


def _moji(s: str) -> str:
    return s.encode('utf-8').decode('cp1252', errors='replace')


class TruncatedNameFolderCompletion(unittest.TestCase):
    TITLE = '指名W要求寝取体验的客人（泡泡屋W第二集）明日方舟，W，维什戴尔'
    FOLDER = '[126989065] 2025-05-08 ' + TITLE

    def _truncated(self, tag=''):
        raw = self.TITLE.encode('utf-8')[:70]
        return ''.join(chr(b) for b in raw) + tag

    def test_completes_from_folder(self):
        name = self._truncated() + '.funscript'
        new, _ = f._resolve_new_name(name, self.FOLDER)
        self.assertEqual(new, self.TITLE + '.funscript')

    def test_keeps_trailing_tag(self):
        name = self._truncated(' (SMOOTH)') + '.funscript'
        new, _ = f._resolve_new_name(name, self.FOLDER)
        self.assertEqual(new, self.TITLE + ' (SMOOTH).funscript')

    def test_unrelated_folder_not_used(self):
        name = self._truncated() + '.funscript'
        new, _ = f._resolve_new_name(name, '[1] 2025-01-01 something else')
        self.assertNotIn('something else', new)


if __name__ == '__main__':
    unittest.main()
