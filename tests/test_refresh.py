import json
import subprocess
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from threading import Thread
from unittest.mock import Mock, patch

from playwright.sync_api import expect, sync_playwright

from webui.__main__ import make_server
from webui.availability import replace_free_window
from webui.configuration import Configuration
from webui.jobs import JobRunner
from webui.official import OfficialAvailability


WORKER = """
import json, sys, time
def emit(kind, **data):
    print(json.dumps({'type':kind, **data}), flush=True)
emit('availability', date='2026-11-10', stage='sampling', time_step=30)
emit('room', room={'id':'r', 'name':'阅览室', 'floor':'六楼', 'total':2})
emit('seats', room_id='r', seats=[
    {'id':'a', 'label':'001', 'slots':[[540,660]], 'position':0},
    {'id':'b', 'label':'002', 'slots':[[720,780]], 'position':1}])
emit('complete')
for line in sys.stdin:
    time.sleep(.15)
    emit('seats', room_id='r', seats=[{'id':'a', 'label':'001', 'slots':[[600,660]], 'position':0}])
    emit('complete')
"""


class RefreshTests(unittest.TestCase):
    def wait_finished(self, runner):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if runner.snapshot()['status'] == 'finished':
                return
            time.sleep(.02)
        self.fail(str(runner.snapshot()))

    def command(self, **options):
        values = dict(date='today', floor=0, time_step=30, session='test', config_version=1)
        values.update(options)
        return [sys.executable, '-u', '-m', 'webui.worker', 'availability', json.dumps(values)]

    def test_refresh_reuses_worker_and_keeps_untouched_seats(self):
        runner = JobRunner()
        self.addCleanup(runner.close)
        popen = subprocess.Popen
        with patch('webui.jobs.subprocess.Popen', side_effect=lambda *a, **kw:
                   popen([sys.executable, '-u', '-c', WORKER], **kw)) as launches:
            runner.start(self.command(), 'availability')
            self.wait_finished(runner)
            first = runner.process
            runner.start(self.command(refresh=True), 'availability')
            self.assertEqual(runner.availability_snapshot()['rooms'][0]['seats'][1]['slots'], [[720, 780]])
            self.wait_finished(runner)
            self.assertIs(runner.process, first)
            self.assertEqual(launches.call_count, 1)
            rows = runner.availability_snapshot()['rooms'][0]['seats']
            self.assertEqual(rows[0]['slots'], [[600, 660]])
            self.assertEqual(rows[1]['slots'], [[720, 780]])
            runner.start(self.command(refresh=True, config_version=2), 'availability')
            self.wait_finished(runner)
            self.assertIsNot(runner.process, first)
            self.assertIsNotNone(first.poll())
            self.assertEqual(launches.call_count, 2)
            current = runner.process
            runner.close()
            self.assertIsNotNone(current.poll())

    def test_replacing_window_revokes_booked_time_and_preserves_other_windows(self):
        original = [(480, 720), (780, 840)]
        self.assertEqual(replace_free_window(original, 540, 570, False),
                         [(480, 540), (570, 720), (780, 840)])
        self.assertEqual(replace_free_window(original, 720, 780, True), [(480, 840)])
        self.assertEqual(original, [(480, 720), (780, 840)])

    def test_bulk_refresh_rotates_rooms_and_bypasses_cache(self):
        official = OfficialAvailability({}, '2026-11-10')
        official.opening_hours = Mock(return_value=(480, 600))
        official.current_cutoff = Mock(return_value=480)
        official.work = [({'id': room}, [{'id': room, 'label': room}]) for room in ('a', 'b')]
        for room in ('a', 'b'):
            official.publish_seats(room, [{'id': room, 'label': room, 'slots': [(480, 600)]}], Mock())
        official.query_chunk = Mock(side_effect=lambda room, *args, **kw:
                                    {room['id']: {'status': 'BOOKED'}})
        for _ in range(3):
            self.assertTrue(official.refresh(Namespace(time_step=30), Mock()))
        calls = official.query_chunk.call_args_list
        self.assertEqual([(call.args[0]['id'], *call.args[1:3]) for call in calls],
                         [('a', 480, 510), ('b', 480, 510), ('a', 510, 540)])
        self.assertTrue(all(call.kwargs == {'use_cache': False} for call in calls))
        self.assertEqual(official.rows['a']['a']['slots'], [(540, 600)])
        self.assertEqual(official.rows['b']['b']['slots'], [(510, 600)])

    def test_exact_refresh_reads_three_seats_and_keeps_other_rows(self):
        official = OfficialAvailability({}, '2026-11-10')
        official.opening_hours = Mock(return_value=(480, 600))
        official.current_cutoff = Mock(return_value=480)
        places = [{'id': str(i)} for i in range(5)]
        official.work = [({'id': 'r'}, places)]
        official.publish_seats('r', [{'id': p['id'], 'slots': [(480, 600)]} for p in places], Mock())
        official.read_timelines = Mock(return_value=iter([(0, {'id': '0', 'slots': []})]))
        official.refresh(Namespace(time_step=0), Mock())
        official.read_timelines.assert_called_once_with(list(enumerate(places[:3])), use_cache=False)
        self.assertEqual(official.rows['r']['4']['slots'], [(480, 600)])

    def test_closed_day_stops_refresh_without_querying(self):
        official = OfficialAvailability({}, '2026-11-10')
        official.opening_hours = Mock(return_value=(480, 600))
        official.current_cutoff = Mock(return_value=600)
        official.query_chunk = Mock()
        emit = Mock()
        self.assertFalse(official.refresh(Namespace(time_step=30), emit))
        self.assertTrue(emit.call_args.kwargs['closed'])
        official.query_chunk.assert_not_called()

    def test_bulk_query_uses_requested_room_without_navigating(self):
        origin = 'https://zw.whut.edu.cn'
        day = '2026-11-10'
        with sync_playwright() as driver, driver.chromium.launch() as browser:
            page = browser.new_page()
            page.route(origin + '/', lambda route: route.fulfill(
                content_type='text/html', body="""<canvas></canvas><script>
                    const view = {
                        seatPreview: {design: {}, reRender() {}},
                        $options: {methods: {freeSeatIdsDurationFun(range, preview) {
                            fetch('/jsq/static/frontApi/res/freeSeatIdsDuration/' + this.$route.query.id + '/' +
                                  this.$route.query.makeDate, {method:'POST', body:JSON.stringify({
                                  beginMinute:range.begin, endMinute:range.end, minMinute:0})})
                                .then(r => r.json()).then(r => preview.reRender(r.data));
                        }}},
                    };
                    Object.defineProperty(view, '$route', {get: () => ({query:{id:'current-room',makeDate:'other-day'}})});
                    document.querySelector('canvas').__vue__ = view;
                </script>"""))
            requests = []

            def respond(route):
                requests.append((route.request.url, route.request.post_data_json))
                route.fulfill(json={'status': True, 'data': {'a': {'status': 'FREE'}}})

            page.route(origin + '/jsq/static/frontApi/res/**', respond)
            page.goto(origin)
            official = OfficialAvailability({'APP_ORIGIN': origin}, day)
            official.attach(page)
            official.canvas = page.locator('canvas')
            official.check_limit = Mock()
            official.current_cutoff = Mock(return_value=0)
            for room in ('first-room', 'second-room'):
                official.next_bulk = 0
                self.assertEqual(official.query_chunk({'id': room}, 480, 510, 30),
                                 {'a': {'status': 'FREE'}})
            self.assertEqual(requests, [(f'{origin}/jsq/static/frontApi/res/freeSeatIdsDuration/{room}/{day}',
                                        {'beginMinute': 480, 'endMinute': 510, 'minMinute': 0})
                                       for room in ('first-room', 'second-room')])
            self.assertEqual(page.url, origin + '/')

    def test_browser_auto_refresh_preserves_filters_and_does_not_replace_unchanged_rows(self):
        runner = JobRunner()
        self.addCleanup(runner.close)
        popen = subprocess.Popen
        launches = []

        def launch(command, **kwargs):
            if list(command[1:5]) == ['-u', '-m', 'webui.worker', 'availability']:
                process = popen([sys.executable, '-u', '-c', WORKER], **kwargs)
                launches.append(process)
                return process
            return popen(command, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch(
            'webui.jobs.subprocess.Popen', side_effect=launch,
        ), make_server(0, runner, Configuration(Path(directory) / '.env')) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as driver, driver.chromium.launch() as browser:
                    page = browser.new_page()
                    errors = []
                    page.on('pageerror', lambda exc: errors.append(str(exc)))
                    page.goto(f'http://127.0.0.1:{server.server_port}/#availability')
                    expect(page.locator('#query')).to_be_enabled()
                    page.get_by_label('自动刷新').check()
                    expect(page.locator('.seat-row')).to_have_count(2)
                    page.locator('#seat-filter').fill('001')
                    expect(page.locator('.seat-row')).to_have_count(1)
                    expect(page.get_by_text('10:00–11:00', exact=True)).to_be_visible(timeout=6000)
                    expect(page.locator('#seat-filter')).to_have_value('001')
                    page.evaluate('window.savedRow = document.querySelector(".seat-row")')
                    version = runner.snapshot()['availability_version']
                    page.wait_for_function('version => fetch("/api/status").then(r => r.json()).then(s => s.availability_version > version + 1)',
                                           arg=version, timeout=6000)
                    page.wait_for_timeout(1000)
                    self.assertTrue(page.evaluate('window.savedRow === document.querySelector(".seat-row")'))
                    page.get_by_label('自动刷新').uncheck()
                    page.wait_for_timeout(1500)
                    job = runner.snapshot()['id']
                    page.wait_for_timeout(2000)
                    self.assertEqual(runner.snapshot()['id'], job)
                    self.assertEqual(len(launches), 1)
                    self.assertFalse(errors, errors)
            finally:
                server.shutdown()
                thread.join()
