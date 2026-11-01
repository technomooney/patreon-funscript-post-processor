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
import threading
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
HEARTBEAT_SECONDS = 30      # how often a live run says so; a 'running' row silent for 4 beats belongs to a dead run
STALE_AFTER_SECONDS = 4 * HEARTBEAT_SECONDS


def _stamp(ts: float | None = None) -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(ts if ts is not None else time.time()))


def _is_stale(updated_epoch: float | None, now: float | None = None) -> bool:
    """True when a running run has not said so for STALE_AFTER_SECONDS (or never did: a row from before heartbeats).
    Judged on seconds since 1970, which a daylight-saving change cannot make ambiguous."""
    if not updated_epoch:
        return True
    return (now if now is not None else time.time()) - updated_epoch > STALE_AFTER_SECONDS


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
                 collector: net_stats.Collector | None = None, app_db_path: str | None = None,
                 counts_fn=None, heartbeat_seconds: float = HEARTBEAT_SECONDS):
        self.base, self.script = base, script
        self.settings, self.version = settings or {}, version
        self.collector = collector or net_stats.collector
        self.app_db_path = app_db_path
        self.run_id = ''
        self.started = 0.0
        self._root: str | None = None
        self._links_since_flush = 0
        self._link_rows_written = 0
        self.counts_fn = counts_fn                     # () -> counts so far, so a run that dies keeps its counts
        self._heartbeat_seconds = heartbeat_seconds
        self._stop = threading.Event()
        self._beat: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------------------------------------------
    def start(self) -> str:
        self.started = time.time()
        self.run_id = time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
        self.collector.reset()
        try:
            self._root = report_store.root_for(self.base)
            if self._root is None:
                return self.run_id
            dead: list[str] = []
            with library_db.db(self._root, write=True) as con:
                # Runs still marked 'running' that stopped saying so are dead (killed, power loss...). A live run in
                # another terminal keeps its heartbeat fresh and is left alone.
                for row in con.execute("SELECT run_id, started_at, duration_s, updated_at, updated_epoch "
                                       "FROM downloader_run_stats WHERE status='running'").fetchall():
                    if _is_stale(row['updated_epoch']):
                        dead.append(row['run_id'])
                        last = row['updated_at'] or row['started_at']
                        con.execute("UPDATE downloader_run_stats SET status='interrupted', ended_at=COALESCE(ended_at, ?), "
                                    "note=COALESCE(note, 'no sign of life since the last save: killed, crashed hard or power loss') "
                                    "WHERE run_id=?", (last, row['run_id']))
                con.execute('INSERT INTO downloader_run_stats(run_id, script, started_at, started_epoch, status, code_version, '
                            'settings_json, updated_at, updated_epoch) VALUES(?,?,?,?,?,?,?,?,?)',
                            (self.run_id, self.script, _stamp(self.started), self.started, 'running', self.version,
                             json.dumps(self.settings, ensure_ascii=False, sort_keys=True), _stamp(), time.time()))
            self._app_write(status='running', counts={})
            self._app_mark_interrupted(dead)
            self._beat = threading.Thread(target=self._heartbeat, daemon=True, name='run-stats-heartbeat')
            self._beat.start()
        except Exception as e:
            print(f'  [run-stats] could not start the run record: {e}')
            self._root = None
        return self.run_id

    def _heartbeat(self) -> None:
        while not self._stop.wait(self._heartbeat_seconds):
            self._touch()

    def _touch(self) -> None:
        if self._root is None:
            return
        try:
            with library_db.db(self._root, write=True) as con:
                con.execute('UPDATE downloader_run_stats SET updated_at=?, updated_epoch=?, duration_s=? WHERE run_id=? AND status=?',
                            (_stamp(), time.time(), time.time() - self.started, self.run_id, 'running'))
        except Exception:
            pass

    def _app_mark_interrupted(self, run_ids: list[str]) -> None:
        if not run_ids:
            return
        try:
            con = _app_connect(self.app_db_path)
            try:
                with con:
                    con.executemany("UPDATE run_rollup SET status='interrupted' WHERE run_id=? AND status='running'",
                                    [(r,) for r in run_ids])
            finally:
                con.close()
        except Exception:
            pass

    def link_finished(self) -> None:
        """Call as links finish; flushes to the databases every FLUSH_EVERY_LINKS of them."""
        self._links_since_flush += 1
        if self._links_since_flush >= FLUSH_EVERY_LINKS:
            self.flush()

    def flush(self, counts: dict | None = None, status: str = 'running', ended: bool = False, note: str | None = None) -> None:
        self._links_since_flush = 0
        if self._root is None:
            return
        try:
            if counts is None and self.counts_fn is not None:
                counts = self.counts_fn()
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
                          'duration_s': time.time() - self.started, 'status': status, 'updated_at': _stamp(),
                          'updated_epoch': time.time()}
                if ended:
                    fields['ended_at'] = _stamp()
                if note:
                    fields['note'] = note[:500]
                for key in _COUNT_KEYS:
                    if key in counts:
                        fields[key] = counts[key]
                con.execute(f'UPDATE downloader_run_stats SET {", ".join(f"{k}=?" for k in fields)} WHERE run_id=?',
                            (*fields.values(), self.run_id))
            self._app_write(status=status, counts=counts, hosts=hosts, total=total, ended=ended)
        except Exception as e:
            print(f'  [run-stats] could not save run statistics: {e}')

    def finish(self, status: str, counts: dict | None = None, outcome_of=None, note: str | None = None) -> None:
        """Close the run. *outcome_of(folder, url)* gives the outcome of the link still being worked on; *note* says why a
        run that did not complete ended (the error of a crash)."""
        self._stop.set()
        if self._beat is not None:
            self._beat.join(timeout=2)
        self.collector.end_link(outcome_of)
        self.flush(counts, status=status, ended=True, note=note)

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
        rows = [dict(r) for r in con.execute('SELECT * FROM downloader_run_stats '
                                             'ORDER BY COALESCE(started_epoch, 0) DESC, started_at DESC LIMIT ?', (limit,))]
    for r in rows:
        if r['status'] == 'running' and _is_stale(r.get('updated_epoch')):
            r['status'] = 'interrupted'               # nothing has said otherwise since; the next run will record it
            r['note'] = r.get('note') or 'no sign of life since the last save: killed, crashed hard or power loss'
            r['ended_at'] = r['ended_at'] or r.get('updated_at') or r['started_at']
    return rows


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
