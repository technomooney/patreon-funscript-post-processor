"""The machine-wide report reads the app database rollup across libraries."""
import contextlib
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))

import library_db  # noqa: E402
import net_stats  # noqa: E402
import run_stats  # noqa: E402
import run_stats_report as rep  # noqa: E402


class ReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = os.path.realpath(self._tmp.name)
        library_db._roots.clear()
        self.app = os.path.join(base, 'app.db')
        for name, host, nbytes, status in (('MDemaxis', 'rule34video.com', 2 * 1048576, 200), ('Pize', 'pixeldrain.com', 1048576, 429)):
            root = library_db.use_root(os.path.join(base, name))
            col = net_stats.Collector()
            rec = run_stats.RunRecorder(root, 'downloadContent', collector=col, app_db_path=self.app)
            rec.start()
            col.record(host, status=status, nbytes=nbytes, seconds=10.0)
            rec.finish('completed', {'tasks': 4, 'links_seen': 6, 'downloaded': 3, 'failed': 1})

    def tearDown(self):
        self._tmp.cleanup()

    def test_runs_and_hosts_across_libraries(self):
        runs, hosts = rep.load(path=self.app)
        self.assertEqual({os.path.basename(r['library_path']) for r in runs}, {'MDemaxis', 'Pize'})
        self.assertEqual({h['host'] for h in hosts}, {'rule34video.com', 'pixeldrain.com'})

    def test_filters(self):
        runs, hosts = rep.load(library='pize', path=self.app)
        self.assertEqual([os.path.basename(r['library_path']) for r in runs], ['Pize'])
        self.assertEqual([h['host'] for h in hosts], ['pixeldrain.com'])
        self.assertEqual(rep.load(days=1, path=self.app)[0].__len__(), 2)

    def test_rows_and_text(self):
        runs, hosts = rep.load(path=self.app)
        rows = rep.host_rows(hosts)
        by = {r['host']: r for r in rows}
        self.assertEqual((by['rule34video.com']['MB'], by['rule34video.com']['avg KB/s']), ('2.0', '205'))
        self.assertEqual(by['pixeldrain.com']['429/exit 6'], 1)
        text = rep.text_table(rep.run_rows(runs))
        self.assertIn('MDemaxis', text)
        self.assertIn('Pize', text)

    def test_nothing_recorded_is_reported_not_an_error(self):
        runs, hosts = rep.load(path=os.path.join(self._tmp.name, 'missing.db'))
        self.assertEqual((runs, hosts), ([], []))
        self.assertIn('nothing recorded', rep.text_table([]))

    def test_html_has_both_tables_and_copy_buttons(self):
        runs, hosts = rep.load(path=self.app)
        page = rep.html_page(runs, hosts)
        for needle in ('Run statistics', 'MDemaxis', 'rule34video.com', 'class="cp"', 'data-copy="pixeldrain.com"'):
            self.assertIn(needle, page)

    def test_command_line(self):
        out_file = os.path.join(self._tmp.name, 'r.html')
        old = os.environ.get('LIBRARY_TOOLS_HOME')
        os.environ['LIBRARY_TOOLS_HOME'] = os.path.dirname(self.app)
        try:
            os.replace(self.app, os.path.join(os.path.dirname(self.app), 'app.db'))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(rep.main(['--html', out_file]), 0)
        finally:
            if old is None:
                os.environ.pop('LIBRARY_TOOLS_HOME', None)
            else:
                os.environ['LIBRARY_TOOLS_HOME'] = old
        self.assertIn('2 run(s) recorded', buf.getvalue())
        self.assertTrue(os.path.isfile(out_file))


if __name__ == '__main__':
    unittest.main()
