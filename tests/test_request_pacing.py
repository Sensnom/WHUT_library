"""Avoid duplicate initialization queries without slowing unrelated requests."""

import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

from tests.test_whut_login import seat


class Clock:
    def __init__(self):
        self.now = 10.0
        self.waits = []

    def time(self):
        return self.now

    def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay


class RequestPacingTests(unittest.TestCase):
    def test_same_endpoint_waits_only_for_the_remaining_interval(self):
        self.assertTrue(hasattr(seat, 'RequestPacer'), 'The API needs shared per-endpoint pacing')
        clock = Clock()
        pacer = seat.RequestPacer(clock=clock.time, sleep=clock.sleep)
        pacer.wait('https://zw.whut.edu.cn/jsq/static/rooms')
        clock.now += 1
        pacer.wait('https://zw.whut.edu.cn/jsq/static/rooms')
        self.assertEqual(clock.waits, [2.5])
        self.assertEqual(clock.now, 13.5)

    def test_unrelated_endpoints_and_first_requests_are_not_delayed(self):
        self.assertTrue(hasattr(seat, 'RequestPacer'), 'The API needs shared per-endpoint pacing')
        clock = Clock()
        pacer = seat.RequestPacer(clock=clock.time, sleep=clock.sleep)
        pacer.wait('https://zw.whut.edu.cn/jsq/static/rooms')
        pacer.wait('https://zw.whut.edu.cn/jsq/static/timeline/first')
        pacer.wait('https://zw.whut.edu.cn/jsq/static/timeline/second')
        self.assertEqual(clock.waits, [])
        clock.now += 4
        pacer.wait('https://zw.whut.edu.cn/jsq/static/rooms')
        self.assertEqual(clock.waits, [])

    def test_changing_verification_token_does_not_bypass_endpoint_spacing(self):
        self.assertTrue(hasattr(seat, 'RequestPacer'), 'The API needs shared per-endpoint pacing')
        clock = Clock()
        pacer = seat.RequestPacer(clock=clock.time, sleep=clock.sleep)
        pacer.wait('https://zw.whut.edu.cn/jsq/static/book?capToken=first')
        pacer.wait('https://zw.whut.edu.cn/jsq/static/book?capToken=second')
        self.assertEqual(clock.waits, [3.5])

    def test_api_applies_pacing_to_actual_posts(self):
        self.assertTrue(hasattr(seat, 'RequestPacer'), 'The API needs shared per-endpoint pacing')
        clock = Clock()
        pacer = seat.RequestPacer(clock=clock.time, sleep=clock.sleep)
        sent = []

        def respond(*args, **kwargs):
            sent.append(clock.now)
            return Mock(status_code=200, json=Mock(return_value={'status': True}))

        with patch('requests.Session') as session:
            session.return_value.post.side_effect = respond
            with seat.SeatAPI(Mock(), 'token', pacer=pacer) as api:
                api.post('rooms')
                api.post('timeline')
                api.post('rooms')
        self.assertEqual(sent, [10, 10, 13.5])


class BrowserInitializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver = sync_playwright().start()
        cls.addClassCleanup(driver.stop)
        cls.browser = driver.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def test_reservation_browser_suppresses_only_automatic_room_lookup(self):
        self.assertTrue(hasattr(seat, 'prepare_reservation_page'), 'Suppress the redundant browser query')
        page = self.browser.new_page()
        self.addCleanup(page.close)
        reached = []
        page.route(seat.APP_URL, lambda route: route.fulfill(content_type='text/html', body='''
            <script>
            const base = '/jsq/static/';
            Promise.all([
                fetch(base+'frontApi/res/findRoomDuration/building/day', {method:'POST'})
                    .then(() => 'sent').catch(() => 'blocked'),
                fetch(base+'frontApi/user/getUserInfo', {method:'POST'}).then(r => r.json()),
                fetch(base+'cap/cg/checkHigh', {method:'POST'}).then(r => r.json()),
            ]).then(results => {document.body.textContent=results[0];});
            </script>
        '''))

        def response(route):
            reached.append(route.request.url)
            route.fulfill(json={'status': True})

        page.route(seat.API_BASE + '/**', response)
        seat.prepare_reservation_page(page)
        page.goto(seat.APP_URL)
        page.wait_for_function("() => document.body.textContent === 'blocked'")
        self.assertEqual(reached, [seat.API_BASE + '/frontApi/user/getUserInfo', seat.API_BASE + '/cap/cg/checkHigh'])

    def test_renewal_installs_the_same_filter_before_authentication(self):
        self.assertIn('on_page', __import__('inspect').signature(seat.SeatLogin.renew_page).parameters)
        old_page = self.browser.new_page()
        self.addCleanup(old_page.close)
        login = object.__new__(seat.SeatLogin)
        login.username, login.password = 'fixture-user', 'fixture-password'
        hook = Mock()

        def authenticate(page, *args):
            hook.assert_called_once_with(page)
            return 'new-token'

        with patch.object(seat, 'authenticate_page', side_effect=authenticate), \
                patch.object(seat, 'save_auth_state'):
            login.cache_path = None
            page, token = login.renew_page(old_page, 'old-token', on_page=hook)
        self.addCleanup(page.context.close)
        self.assertEqual(token, 'new-token')


if __name__ == '__main__':
    unittest.main()
