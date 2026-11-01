"""Run history and network cost, written to the library database (full detail) and the machine-wide app database (rollup).

A RunRecorder brackets one run of a tool (for the downloader: one find_and_download). It writes a 'running' row when the
run starts, so a run that is killed still leaves evidence (the next run marks it 'interrupted'), flushes the numbers
net_stats has collected every so often, and closes the row with the final status and counts at the end.

* library database (downloader_run_stats / _host_stats / _link_stats): everything, per run, per host and per link;
  it travels with the library.
* app database (run_rollup / host_rollup): the same runs and hosts in compact form with the library's id and path, so one
  query can compare libraries, hosts and days on this machine.

Every write is best-effort: statistics must never stop a download.
"""
import hashlib
import json
import sys
import time
import uuid

import library_db
import net_stats
import report_store

_APP_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS run_rollup (
        run_id TEXT PRIMARY KEY, library_id TEXT, library_path TEXT, script TEXT, started_at TEXT, ended_at TEXT,
        status TEXT, duration_s REAL, tasks INTEGER, links_seen INTEGER, downloaded INTEGER, failed INTEGER,
        bytes_saved INTEGER, requests INTEGER, bytes_transferred INTEGER)""",
    """CREATE TABLE IF NOT EXISTS host_rollup (
        run_id TEXT NOT NULL, host TEXT NOT NULL, requests INTEGER, bytes INTEGER, seconds REAL, ok INTEGER,
        client_errors INTEGER, forbidden INTEGER, rate_limited INTEGER, server_errors INTEGER, errors INTEGER,
        PRIMARY KEY (run_id, host))""",
)
_HOST_COLS = ('requests', 'bytes', 'seconds', 'ok', 'redirects', 'client_errors', 'forbidden', 'rate_limited',
              'server_errors', 'errors', 'slowest_seconds')
_COUNT_KEYS = ('tasks', 'links_seen', 'downloaded', 'duplicates', 'failed', 'deferred', 'skipped', 'bytes_saved')
FLUSH_EVERY_LINKS = 25


def code_version(*paths: str) -> str:
    """A short fingerprint of the code that ran (the live copy is not a git repo), plus the Python version."""
    h = hashlib.sha1()
    for p in paths:
        try:
            with open(p, 'rb') as f:
                h.update(f.read())
        except OSError:
            pass
    return f'{h.hexdigest()[:10]} py{sys.version_info.major}.{sys.version_info.minor}'


def _app_connect(path: str | None = None):
    import app_db
    con = app_db.connect(path)
    for stmt in _APP_SCHEMA:
        con.execute(stmt)
    return con


class RunRecorder:
    def __init__(self, base: str, script: str, settings: dict | None = None, version: str = '',
                 collector: net_stats.Collector | None = None, app_db_path: str | None = None):
        self.base, self.script = base, script
        self.settings, self.version = settings or {}, version
        self.collector = collector or net_stats.collector
        self.app_db_path = app_db_path
        self.run_id = ''
        self.started = 0.0
        self._root: str | None = None
        self._links_since_flush = 0
        self._link_rows_written = 0

    # -- lifecycle --------------------------------------------------------------------------------------------
    def start(self) -> str:
        self.started = time.time()
        self.run_id = time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
        self.collector.reset()
        try:
            self._root = report_store.root_for(self.base)
            if self._root is None:
                return self.run_id
            with library_db.db(self._root, write=True) as con:
                con.execute("UPDATE downloader_run_stats SET status='interrupted' WHERE status='running'")
                con.execute('INSERT INTO downloader_run_stats(run_id, script, started_at, status, code_version, settings_json) '
                            "VALUES(?,?,?,?,?,?)",
                            (self.run_id, self.script, time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(self.started)),
                             'running', self.version, json.dumps(self.settings, ensure_ascii=False, sort_keys=True)))
            self._app_write(status='running', counts={})
        except Exception as e:
            print(f'  [run-stats] could not start the run record: {e}')
            self._root = None
        return self.run_id

    def link_finished(self) -> None:
        """Call as links finish; flushes to the databases every FLUSH_EVERY_LINKS of them."""
        self._links_since_flush += 1
        if self._links_since_flush >= FLUSH_EVERY_LINKS:
            self.flush()

    def flush(self, counts: dict | None = None, status: str = 'running', ended: bool = False) -> None:
        self._links_since_flush = 0
        if self._root is None:
            return
        try:
            hosts = self.collector.snapshot_hosts()
            links = self.collector.take_finished_links()
            total = self.collector.totals()
            counts = counts or {}
            with library_db.db(self._root, write=True) as con:
                for host, stat in hosts.items():
                    con.execute(
                        f'INSERT OR REPLACE INTO downloader_host_stats(run_id, host, {", ".join(_HOST_COLS)}) '
                        f'VALUES(?,?,{",".join("?" * len(_HOST_COLS))})',
                        (self.run_id, host, *(getattr(stat, c) for c in _HOST_COLS)))
                for link in links:
                    if not (link.requests or link.outcome not in ('previously_done', 'skipped_known_failure', '')):
                        continue                      # a link skipped without touching the network is not worth a row
                    con.execute(
                        'INSERT OR REPLACE INTO downloader_link_stats(run_id, folder, url, host, started_at, requests, bytes, '
                        'seconds, precheck_seconds, download_seconds, precheck_result, outcome) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        (self.run_id, library_db.rel_folder(self._root, link.folder), link.url, link.host, link.started_at,
                         link.requests, link.bytes, link.seconds, link.precheck_seconds, link.download_seconds,
                         link.precheck_result, link.outcome))
                    self._link_rows_written += 1
                fields = {'requests': total.requests, 'bytes_transferred': total.bytes, 'request_seconds': total.seconds,
                          'duration_s': time.time() - self.started, 'status': status}
                if ended:
                    fields['ended_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
                for key in _COUNT_KEYS:
                    if key in counts:
                        fields[key] = counts[key]
                con.execute(f'UPDATE downloader_run_stats SET {", ".join(f"{k}=?" for k in fields)} WHERE run_id=?',
                            (*fields.values(), self.run_id))
            self._app_write(status=status, counts=counts, hosts=hosts, total=total, ended=ended)
        except Exception as e:
            print(f'  [run-stats] could not save run statistics: {e}')

    def finish(self, status: str, counts: dict | None = None, outcome_of=None) -> None:
        """Close the run. *outcome_of(folder, url)* gives the outcome of the link still being worked on."""
        self.collector.end_link(outcome_of)
        self.flush(counts, status=status, ended=True)

    # -- app database -----------------------------------------------------------------------------------------
    def _app_write(self, status: str, counts: dict, hosts: dict | None = None, total=None, ended: bool = False) -> None:
        try:
            con = _app_connect(self.app_db_path)
            try:
                with con:
                    lib_id = library_db.library_id(self._root) if self._root else ''
                    total = total or self.collector.totals()
                    con.execute(
                        'INSERT OR REPLACE INTO run_rollup(run_id, library_id, library_path, script, started_at, ended_at, '
                        'status, duration_s, tasks, links_seen, downloaded, failed, bytes_saved, requests, bytes_transferred) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (self.run_id, lib_id, self._root, self.script,
                         time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(self.started)),
                         time.strftime('%Y-%m-%dT%H:%M:%S') if ended else None, status, time.time() - self.started,
                         counts.get('tasks'), counts.get('links_seen'), counts.get('downloaded'), counts.get('failed'),
                         counts.get('bytes_saved'), total.requests, total.bytes))
                    for host, s in (hosts or {}).items():
                        con.execute(
                            'INSERT OR REPLACE INTO host_rollup(run_id, host, requests, bytes, seconds, ok, client_errors, '
                            'forbidden, rate_limited, server_errors, errors) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (self.run_id, host, s.requests, s.bytes, s.seconds, s.ok, s.client_errors, s.forbidden,
                             s.rate_limited, s.server_errors, s.errors))
            finally:
                con.close()
        except Exception:
            pass


# -- reading ---------------------------------------------------------------------------------------------------
def read_runs(base: str, limit: int = 20) -> list[dict]:
    root = report_store.root_for(base)
    if root is None:
        return []
    with library_db.db(root) as con:
        return [dict(r) for r in con.execute('SELECT * FROM downloader_run_stats ORDER BY started_at DESC LIMIT ?', (limit,))]


def read_hosts(base: str, run_id: str | None = None) -> list[dict]:
    """Per-host numbers for one run, or added up over every recorded run when run_id is None."""
    root = report_store.root_for(base)
    if root is None:
        return []
    sums = ', '.join(f'SUM({c}) AS {c}' for c in _HOST_COLS if c != 'slowest_seconds')
    with library_db.db(root) as con:
        if run_id:
            rows = con.execute('SELECT host, ' + ', '.join(_HOST_COLS) + ' FROM downloader_host_stats WHERE run_id=? '
                               'ORDER BY requests DESC', (run_id,))
        else:
            rows = con.execute(f'SELECT host, {sums}, MAX(slowest_seconds) AS slowest_seconds FROM downloader_host_stats '
                               'GROUP BY host ORDER BY requests DESC')
        return [dict(r) for r in rows]


def read_links(base: str, run_id: str, limit: int = 200, order: str = 'seconds') -> list[dict]:
    root = report_store.root_for(base)
    if root is None:
        return []
    if order not in ('seconds', 'bytes', 'requests'):
        order = 'seconds'
    with library_db.db(root) as con:
        return [dict(r) for r in con.execute(f'SELECT * FROM downloader_link_stats WHERE run_id=? ORDER BY {order} DESC LIMIT ?',
                                            (run_id, limit))]
