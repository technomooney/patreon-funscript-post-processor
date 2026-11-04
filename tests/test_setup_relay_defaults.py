import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import setup_config as sc


class PrefetchDefaultTests(unittest.TestCase):
    def test_32_gb_or_more_suggests_15_gb(self):
        for ram in (32 * 1024, 32 * 1024 - 300, 64 * 1024, 128 * 1024):
            self.assertEqual(sc.default_prefetch_max_mb(ram), 15 * 1024, ram)

    def test_smaller_machines_get_a_quarter_of_their_ram_at_least_1_gb(self):
        self.assertEqual(sc.default_prefetch_max_mb(16 * 1024), 4 * 1024)
        self.assertEqual(sc.default_prefetch_max_mb(8 * 1024), 2 * 1024)
        self.assertEqual(sc.default_prefetch_max_mb(2 * 1024), 1024)

    def test_unreadable_ram_gets_the_conservative_default(self):
        self.assertEqual(sc.default_prefetch_max_mb(None), 2048)

    def test_ram_detection_reads_sysconf_and_survives_failure(self):
        with mock.patch.object(sc.os, 'name', 'posix'), \
                mock.patch.object(sc.os, 'sysconf', side_effect=lambda k: {'SC_PAGE_SIZE': 4096, 'SC_PHYS_PAGES': 8 * 1024 * 256}[k], create=True):
            self.assertEqual(sc._total_ram_mb(), 8 * 1024)
        with mock.patch.object(sc.os, 'name', 'posix'), mock.patch.object(sc.os, 'sysconf', side_effect=ValueError, create=True):
            self.assertIsNone(sc._total_ram_mb())


if __name__ == '__main__':
    unittest.main()
