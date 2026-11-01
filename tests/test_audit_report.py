"""generate_audit_report shows the reports kept in the library DB, and never needs a loose CSV."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import generate_audit_report  # noqa: E402
import library_db  # noqa: E402
import report_store  # noqa: E402
import run_stats  # noqa: E402


class AuditReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        library_db._roots.clear()
        self.root = library_db.use_root(os.path.join(os.path.realpath(self._tmp.name), 'lib'))
        self.post = os.path.join(self.root, 'post')
        os.makedirs(self.post)

    def tearDown(self):
        self._tmp.cleanup()

    def test_db_reports_and_link_outcomes_are_rendered(self):
        report_store.record_outcome(self.root, 'http://x/1', self.post, domain='x', reason='mega_not_found',
                                    http_status=-9, permanent=True)
        report_store.replace_report(self.root, 'funscript_check',
                                    [{'folder': self.post, 'issue': 'video_without_funscript', 'file': 'a <b>.mp4'}])
        html = generate_audit_report.generate(self.root)
        self.assertIn('mega_not_found', html)
        self.assertIn('video_without_funscript', html)
        self.assertIn('a &lt;b&gt;.mp4', html)      # escaped
        self.assertNotIn('a <b>.mp4', html)

    def test_cells_carry_the_full_value_to_copy(self):
        report_store.record_outcome(self.root, 'https://mega.nz/file/AbCdEfGh#KEY', self.post, domain='mega.nz')
        html = generate_audit_report.generate(self.root)
        self.assertIn('data-copy="https://mega.nz/file/AbCdEfGh#KEY"', html)   # the whole link
        self.assertIn(f'data-copy="{self.post}"', html)                         # absolute, though shown relative
        self.assertIn('class="cp"', html)

    def test_loose_csvs_are_not_read_any_more(self):
        os.makedirs(os.path.join(self.root, '_reports'))
        with open(os.path.join(self.root, '_reports', 'funscript_check.csv'), 'w') as f:
            f.write('folder,issue\n/x,STALE_LOOSE_CSV_ROW\n')
        self.assertNotIn('STALE_LOOSE_CSV_ROW', generate_audit_report.generate(self.root))


    def test_run_history_and_network_sections(self):
        import net_stats
        col = net_stats.Collector()
        rec = run_stats.RunRecorder(self.root, 'downloadContent', collector=col,
                                    app_db_path=os.path.join(self._tmp.name, 'app.db'), version='abc py3.14')
        rec.start()
        col.begin_link(self.post, 'https://rule34video.com/video/9/x')
        col.record('rule34video.com', status=200, nbytes=3 * 1048576, seconds=20.0)
        col.record('rule34video.com', status=429, seconds=0.2)
        col.note(precheck_result='size shortcut', precheck_seconds=4.0, download_seconds=0.0)
        rec.finish('completed', {'tasks': 3, 'links_seen': 4, 'downloaded': 2, 'failed': 1, 'bytes_saved': 5 * 1048576},
                   outcome_of=lambda f, u: 'downloaded')
        html = generate_audit_report.generate(self.root)
        for needle in ('Run History', 'completed', 'abc py3.14', 'Network by Host', 'rule34video.com', '429/exit 6',
                       'Slowest Links', 'size shortcut', 'https://rule34video.com/video/9/x'):
            self.assertIn(needle, html, needle)
        self.assertIn('data-copy="https://rule34video.com/video/9/x"', html)       # copy buttons work here too
        self.assertIn('>5.0<', html)                                                  # saved MB
        self.assertIn('data-copy="152"', html)                                        # 3 MB over 20.2 s = 152 KB/s

    def test_a_crashed_run_shows_its_status_and_error(self):
        import net_stats
        rec = run_stats.RunRecorder(self.root, 'downloadContent', collector=net_stats.Collector(),
                                    app_db_path=os.path.join(self._tmp.name, 'app.db'))
        rec.start()
        rec.finish('crashed', {'tasks': 2}, note='RuntimeError: disk gone')
        html = generate_audit_report.generate(self.root)
        self.assertIn('crashed', html)
        self.assertIn('RuntimeError: disk gone', html)

    def test_no_run_sections_without_any_run(self):
        html = generate_audit_report.generate(self.root)
        self.assertNotIn('Run History', html)


if __name__ == '__main__':
    unittest.main()
