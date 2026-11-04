import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import downloadContent as dc


def _record(*pairs):
    return [{'script': 'downloadContent', 'links': [{'url': u, 'status': s} for u, s in pairs]}]


class PreviouslySeenLinks(unittest.TestCase):
    def _seen(self, records):
        with mock.patch.object(dc.folder_log, 'read', return_value=records):
            return dc._previously_seen_links('x')

    def test_settled_links_are_seen_failed_ones_are_not(self):
        seen = self._seen(_record(('a', 'downloaded'), ('b', 'failed'), ('c', 'skipped_known_failure'),
                                  ('d', 'deferred_mega')))
        self.assertEqual(seen - {''}, {'a', 'c'})

    def test_all_failed_is_still_a_prior_run(self):
        self.assertTrue(self._seen(_record(('a', 'failed'))))

    def test_no_record_or_no_links_is_empty(self):
        self.assertEqual(self._seen([]), set())
        self.assertEqual(self._seen(_record()), set())


if __name__ == '__main__':
    unittest.main()
