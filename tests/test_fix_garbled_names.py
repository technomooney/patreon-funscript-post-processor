import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import fix_garbled_names as f


def _garble(raw: bytes) -> str:
    out = []
    for b in raw:
        try:
            out.append(bytes([b]).decode('cp1252'))
        except UnicodeDecodeError:
            out.append(chr(b))
    return ''.join(out)


class TruncatedNameFolderCompletion(unittest.TestCase):
    TITLE = '指名W要求寝取体验的客人（泡泡屋W第二集）明日方舟，W，维什戴尔'
    FOLDER = '[126989065] 2025-05-08 ' + TITLE

    def _truncated(self, tag=''):
        return _garble(self.TITLE.encode('utf-8')[:70]) + tag

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

    def test_completes_from_clean_sibling_with_mid_tag(self):
        full = '53.【定制ntr】ラプラス・ダークネス ライブで中出しされた'
        name = '53 (SMOOTH).' + _garble(full[3:].encode('utf-8')[:-2]) + '.funscript'
        new, _ = f._resolve_new_name(name, '[9] 2025-01-01 other', [full + '.funscript', full + '.mp4'])
        self.assertEqual(new, '53 (SMOOTH).' + full[3:] + '.funscript')


if __name__ == '__main__':
    unittest.main()
