import importlib.util
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

SPEC = importlib.util.spec_from_file_location("whut_seat", Path(__file__).resolve().parents[1] / "whut-seat.py")
seat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seat)

FORM = """
<input id="un"><input id="pd" type="password"><button id="index_login_btn">登录</button>
<script>
  document.querySelector('button').onclick = () => {
    const user = document.querySelector('#un').value;
    const password = document.querySelector('#pd').value;
    if (user === 'fixture-user' && password === 'fixture-password') {
      %s
    }
  };
</script>
"""


class HeadlessLoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver = sync_playwright().start()
        cls.addClassCleanup(driver.stop)
        cls.browser = driver.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def page_with_form(self, on_submit):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.set_default_timeout(5000)
        page.route("https://one.whut.edu.cn/**", lambda route: route.fulfill(
            content_type="text/html; charset=utf-8", body=FORM % on_submit,
        ))
        page.route("https://zw.whut.edu.cn/**", lambda route: route.fulfill(
            content_type="text/html; charset=utf-8", body="""
              <script>
                if (location.search === '?done') {
                  sessionStorage.setItem('jsq_p-token', 'fixture-token');
                } else {
                  location.href = 'https://one.whut.edu.cn/tpass/login';
                }
              </script>
            """,
        ))
        return page

    def test_submits_school_form_and_waits_for_library_token(self):
        page = self.page_with_form("location.href = 'https://zw.whut.edu.cn/jsq-v/?done';")
        self.assertEqual(
            seat.authenticate_page(page, "fixture-user", "fixture-password"),
            "fixture-token",
        )

    def test_stops_when_school_requires_verification(self):
        page = self.page_with_form("document.body.append('请完成滑块验证');")
        with self.assertRaisesRegex(seat.LoginError, "验证"):
            seat.authenticate_page(page, "fixture-user", "fixture-password")

    def test_reuses_cached_cookie_without_opening_school_form(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / ".whut-auth" / "session.json"
            context = self.browser.new_context()
            self.addCleanup(context.close)
            context.add_cookies([{"name": "session", "value": "fixture", "url": seat.APP_ORIGIN}])
            seat.save_auth_state(state_path, context.storage_state())

            restored = self.browser.new_context(storage_state=json.loads(state_path.read_text()))
            self.addCleanup(restored.close)
            page = restored.new_page()
            page.set_default_timeout(5000)
            page.route(seat.APP_URL, lambda route: route.fulfill(
                content_type="text/html; charset=utf-8",
                body="""<script>
                    if (document.cookie.includes('session=fixture')) {
                        sessionStorage.setItem('jsq_p-token', 'cached-token');
                    }
                </script>""",
            ))
            self.assertEqual(seat.authenticate_page(page, "", ""), "cached-token")


class SeatQueryTests(unittest.TestCase):
    def test_full_window_candidate_skips_remaining_timeline_requests(self):
        api = Mock()
        api.post.side_effect = [
            {"status": True, "data": {
                "a": {"id": "a", "label": "010", "status": "FREE"},
                "b": {"id": "b", "label": "011", "status": "FREE"},
            }},
            {"status": True, "data": {"freeList": [{"label": "09:00-12:00"}]}},
            AssertionError("an exact match needs no further timeline requests"),
        ]
        found, result = seat.find_candidate(api, {"id": "1", "name": "room"}, "2026-11-10", 540, 720)
        self.assertTrue(found)
        self.assertEqual(result["seat_id"], "a")
        self.assertEqual(api.post.call_count, 2)


class TokenRefreshTests(unittest.TestCase):
    def test_rate_limit_identifies_endpoint_without_exposing_query_tokens(self):
        response = Mock(status_code=429)
        with patch('requests.Session') as session, seat.SeatAPI(Mock(), 'token') as api:
            session.return_value.post.return_value = response
            with self.assertRaises(seat.SeatRateLimitError) as raised:
                api.post('frontApi/make/freeBook/seat/day/540/600?capToken=private-token')
        message = str(raised.exception)
        self.assertIn('HTTP 429', message)
        self.assertIn('frontApi/make/freeBook/seat/day/540/600', message)
        self.assertNotIn('private-token', message)

    def test_parallel_requests_share_one_token_refresh(self):
        expired = Barrier(2, timeout=3)

        def post(*args, headers, **kwargs):
            if headers["token"] == "old-token":
                expired.wait()
                return Mock(json=Mock(return_value={"status": False, "code": 20003}))
            return Mock(json=Mock(return_value={"status": True}))

        first = Mock(post=post)
        second = Mock(post=post)
        login = Mock(return_value="new-token")
        with patch("requests.Session", side_effect=[first, second]), seat.SeatAPI(login, "old-token") as api:
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(api.post, ["first", "second"])), [{"status": True}] * 2)
        login.assert_called_once_with("old-token")
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()

    def test_rate_limit_stops_without_retry(self):
        response = Mock(status_code=429)
        login = Mock()
        with patch("requests.Session") as session, seat.SeatAPI(login, "token") as api:
            session.return_value.post.return_value = response
            with self.assertRaises(seat.SeatRateLimitError):
                api.post("seats")
            session.return_value.post.assert_called_once()
        response.json.assert_not_called()
        login.assert_not_called()

    def test_rate_limit_in_successful_http_response_stops_without_retry(self):
        response = Mock(status_code=200, json=Mock(return_value={
            "status": False, "code": 500, "message": "访问过于频繁 请稍后再试",
        }))
        login = Mock()
        with patch("requests.Session") as session, seat.SeatAPI(login, "token") as api:
            session.return_value.post.return_value = response
            with self.assertRaises(seat.SeatRateLimitError):
                api.post("rooms")
            session.return_value.post.assert_called_once()
        login.assert_not_called()

    def test_retries_expired_request_and_keeps_new_token(self):
        response = Mock()
        response.json.side_effect = [
            {"status": False, "code": 20003},
            {"status": True},
            {"status": True},
        ]
        with patch("requests.Session") as session:
            post = session.return_value.post
            post.return_value = response
            login = Mock(return_value="new-token")
            with seat.SeatAPI(login, "old-token") as api:
                self.assertTrue(api.post("first")["status"])
                self.assertTrue(api.post("second")["status"])
        session.return_value.close.assert_called_once_with()
        login.assert_called_once_with("old-token")
        self.assertEqual(
            [call.kwargs["headers"]["token"] for call in post.call_args_list],
            ["old-token", "new-token", "new-token"],
        )

    def test_stops_after_replacement_token_is_rejected(self):
        response = Mock()
        response.json.return_value = {"status": False, "code": 20003}
        with patch("requests.Session") as session:
            post = session.return_value.post
            post.return_value = response
            login = Mock(return_value="new-token")
            with seat.SeatAPI(login, "old-token") as api, self.assertRaises(seat.LoginError):
                api.post("first")
        login.assert_called_once_with("old-token")
        self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
