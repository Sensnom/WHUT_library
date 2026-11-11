"""Reservation verification must succeed before a booking is submitted."""

import base64
import random
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np
from playwright.sync_api import sync_playwright

import reservation_captcha as captcha_module
from reservation_captcha import CaptchaError, ReservationCaptcha, drag_distance

from tests.test_whut_login import seat


CANDIDATE = {
    "seat_id": "seat-1", "seat_label": "010", "seat_name": "010",
    "room_name": "room", "start": 540, "end": 600,
}


def data_image(image):
    ok, encoded = cv2.imencode('.png', image)
    assert ok
    return 'data:image/png;base64,' + base64.b64encode(encoded).decode()


def sample_images():
    background = np.full((360, 600, 3), 30, dtype=np.uint8)
    piece = np.zeros((360, 110, 4), dtype=np.uint8)
    piece[30:130, 10:110] = (200, 100, 50, 255)
    return dict(background=data_image(background), piece=data_image(piece),
                rendered_width=300, piece_left=0, piece_top=0)


def predictor():
    model = Mock(spec=['identify', 'identify_offset'])
    model.identify.return_value = ([466, 30, 566, 130], .96)
    model.identify_offset.return_value = (10, .98)
    return model


WIDGET = r"""
<script>
window.widgetCount = 0;
class TAC {
    static enc = {};
    constructor(options) { this.options = options; }
    init() {
        window.widgetCount++;
        this.C = {currentCaptchaData:{end:window.sliderMaximum ?? 245}};
        const mount = document.querySelector(this.options.bindEl);
        if (window.serverExempt) {
            this.options.validSuccess({data:{token:'server-issued-token'}}, null, this);
            return;
        }
        mount.innerHTML = `<div style="position:relative;width:300px;height:220px">
            <img id="tianai-captcha-slider-bg-img" style="width:300px;height:180px;display:block">
            <img id="tianai-captcha-slider-move-img" style="position:absolute;left:0;top:0;width:55px;height:180px">
            <div id="tianai-captcha-slider-move-btn" style="position:absolute;left:0;top:185px;width:30px;height:30px;background:gray"></div>
        </div>`;
        document.querySelector('#tianai-captcha-slider-bg-img').src = window.sample.background;
        document.querySelector('#tianai-captcha-slider-move-img').src = window.sample.piece;
        const button = document.querySelector('#tianai-captcha-slider-move-btn');
        if (window.animate) {
            mount.style.transform = 'translateX(-500px)';
            mount.style.transition = 'transform .3s';
            setTimeout(() => {mount.style.transform = 'translateX(0)';}, 50);
        }
        let active = false, start = 0, distance = 0, startY = 0, started = 0, tracks = [], localDown = null;
        this.move = e => { if (active) {
            distance = e.clientX - start;
            tracks.push({x:distance,y:e.clientY-startY,time:performance.now()-started});
        }};
        this.up = () => {
            if (!active) return;
            active = false;
            fetch(this.options.validCaptchaUrl, {method:'POST', headers:{'Content-Type':'application/json'},
                body:JSON.stringify({distance, tracks, elapsed:performance.now()-started,
                    username:this.options.customData.username,
                    checkStartOrigin:!!window.checkStartOrigin, localDown})})
                .then(r => r.json()).then(r => {
                    if (r.status) this.options.validSuccess(r, null, this);
                    else this.options.validFail(r, null, this);
                });
        };
        button.addEventListener('mousedown', e => {
            active = true; start = e.clientX; startY = e.clientY; started = performance.now(); tracks = [];
            // The deployed SDK excludes its outermost fixed ancestor's offset.
            let element=button, left=0, top=0;
            do {
                left += element.offsetLeft; top += element.offsetTop;
                element = element.offsetParent;
            } while(element?.offsetParent);
            localDown = {x:e.clientX-left,y:e.clientY-top,width:button.offsetWidth,height:button.offsetHeight};
            this.C.currentCaptchaData.trackList = [{type:'down', x:localDown.x,
                y:localDown.y+(window.badStartOffset||0), t:0}];
        });
        document.addEventListener('mousemove', this.move);
        document.addEventListener('mouseup', this.up);
    }
    destroyWindow() {
        document.querySelector(this.options.bindEl)?.replaceChildren();
        document.removeEventListener('mousemove', this.move);
        document.removeEventListener('mouseup', this.up);
    }
}
</script>
"""


class RecognitionTests(unittest.TestCase):
    def test_mask_alignment_preserves_display_scale_and_both_element_offsets(self):
        piece = np.zeros((180, 110, 4), dtype=np.uint8)
        piece[30:130, 10:110] = (200, 100, 50, 255)
        mask = np.zeros((360, 600), dtype=bool)
        mask[60:160, 466:566] = True
        for scale in (.25, .5):
            with self.subTest(scale=scale):
                images = {**sample_images(), 'piece':data_image(piece),
                          'rendered_width':600*scale, 'piece_left':20*scale, 'piece_top':30*scale}
                model = predictor()
                model.predict = Mock(return_value=[(
                    np.array([[469, 62, 568, 162, .96, 0]]), np.array([mask]),
                )])
                self.assertEqual(drag_distance(images, model)[0], 436*scale)

    def test_unreliable_segmentation_falls_back_without_lowering_confidence(self):
        for confidence, area in ((.4, (30,130,466,566)), (.96, (30,40,466,476))):
            with self.subTest(confidence=confidence):
                model = predictor()
                mask = np.zeros((360, 600), dtype=bool)
                y0,y1,x0,x1 = area
                mask[y0:y1,x0:x1] = True
                model.predict = Mock(return_value=[(
                    np.array([[466, 30, 566, 130, confidence, 0]]), np.array([mask]),
                )])
                self.assertEqual(drag_distance(sample_images(), model), (228, .96))
                model.identify.assert_called_once()

    def test_two_matching_gap_masks_are_rejected_as_ambiguous(self):
        masks = np.zeros((2, 360, 600), dtype=bool)
        masks[0,30:130,166:266] = True
        masks[1,30:130,466:566] = True
        model = predictor()
        model.predict = Mock(return_value=[(
            np.array([[166,30,266,130,.96,0], [466,30,566,130,.96,0]]), masks,
        )])
        with self.assertRaisesRegex(captcha_module.CaptchaAttemptError, '多个相似缺口'):
            drag_distance(sample_images(), model)
        model.identify.assert_not_called()

    def test_invalid_segmentation_mask_size_is_fatal(self):
        model = predictor()
        model.predict = Mock(return_value=[(
            np.array([[466,30,566,130,.96,0]]), np.zeros((1,20,20)),
        )])
        with self.assertRaisesRegex(CaptchaError, '分割模型输出尺寸'):
            drag_distance(sample_images(), model)

    def test_segmentation_aligns_the_piece_instead_of_subtracting_biased_boxes(self):
        images = sample_images()
        model = predictor()
        # The gap mask is exactly 456 source pixels from the initial piece,
        # while both detector box edges include independent rounding errors.
        mask = np.zeros((360, 600), dtype=bool)
        mask[30:130, 466:566] = True
        model.predict = Mock(return_value=[(
            np.array([[469, 32, 568, 132, .96, 0]]), np.array([mask]),
        )])
        model.identify.return_value = ([469, 32, 568, 132], .96)
        model.identify_offset.return_value = (7, .98)
        distance, confidence = drag_distance(images, model)
        self.assertEqual(distance, 228)
        self.assertEqual(confidence, .96)
        model.predict.assert_called_once()
        model.identify.assert_not_called()

    def test_scale_and_detected_offset_are_both_applied(self):
        model = predictor()
        distance, confidence = drag_distance(sample_images(), model)
        self.assertEqual(distance, 228)
        self.assertEqual(confidence, .96)
        composite = model.identify.call_args.kwargs['source']
        self.assertEqual(composite.shape, (360, 600, 3))
        self.assertEqual(composite[50, 50].tolist(), [200, 100, 50])
        self.assertEqual(composite[50, 300].tolist(), [30, 30, 30])

    def test_piece_element_initial_offset_is_preserved_when_compositing(self):
        images = sample_images()
        images['piece_left'] = 10
        model = predictor()
        model.identify_offset.return_value = (30, .98)
        self.assertEqual(drag_distance(images, model)[0], 218)
        composite = model.identify.call_args.kwargs['source']
        self.assertEqual(composite[50, 15].tolist(), [30, 30, 30])
        self.assertEqual(composite[50, 35].tolist(), [200, 100, 50])

    def test_low_confidence_does_not_generate_a_guess(self):
        model = predictor()
        model.identify.return_value = ([466, 30, 566, 130], .4)
        with self.assertRaisesRegex(CaptchaError, '置信度不足'):
            drag_distance(sample_images(), model)
        model.identify_offset.assert_not_called()

    def test_missing_piece_detection_cannot_be_mistaken_for_target(self):
        model = predictor()
        model.identify_offset.return_value = (466, .96)
        with self.assertRaisesRegex(CaptchaError, '起始位置'):
            drag_distance(sample_images(), model)

    def test_same_detection_for_gap_and_piece_is_reported_as_ambiguous(self):
        model = predictor()
        model.identify.return_value = ([10, 30, 110, 130], .96)
        with self.assertRaisesRegex(captcha_module.CaptchaAttemptError, '区分滑块本体和目标缺口'):
            drag_distance(sample_images(), model)

    def test_invalid_boxes_and_images_are_rejected(self):
        for box in ([float('nan'), 0, 50, 50], [-2, 30, 566, 130], [466, 30, 666, 130], []):
            with self.subTest(box=box):
                model = predictor()
                model.identify.return_value = (box, .96)
                with self.assertRaises(CaptchaError):
                    drag_distance(sample_images(), model)
        images = sample_images()
        images['piece'] = 'data:image/png;base64,broken'
        with self.assertRaises(CaptchaError):
            drag_distance(images, predictor())


class CooldownTests(unittest.TestCase):
    def test_repeated_risk_uses_a_capped_delay_and_success_resets_it(self):
        page = Mock()
        rejected = {'status':'failed', 'code':5004, 'message':'风控预警'}
        page.wait_for_function.side_effect = [Mock(),
            Mock(json_value=Mock(return_value=rejected)), Mock(json_value=Mock(return_value=rejected))]
        captcha = ReservationCaptcha(page, seat.API_BASE, recognizer=predictor())
        now = [100.0]
        page.wait_for_timeout.side_effect = lambda ms: now.__setitem__(0, now[0] + ms/1000)
        with patch.object(captcha_module.time, 'monotonic', side_effect=lambda:now[0]):
            with self.assertRaises(captcha_module.CaptchaAttemptError) as raised:
                captcha.solve({'publicKey':'key', 'username':'fixture'})
            self.assertEqual(raised.exception.retry_after, 6)
            self.assertEqual(captcha.risk_failures, 2)
            page.wait_for_function.side_effect = [Mock(),
                Mock(json_value=Mock(return_value={'status':'success','token':'server-token'}))]
            self.assertEqual(captcha.solve({'publicKey':'key', 'username':'fixture'}), 'server-token')
        self.assertEqual(captcha.risk_failures, 0)

    def test_consumed_risk_delay_is_not_added_again_after_low_confidence(self):
        page = Mock()
        page.wait_for_function.side_effect = [
            Mock(),
            Mock(json_value=Mock(return_value={
                'status': 'failed', 'code': 5004, 'message': '滑动触发风控预警 请重试',
            })),
            Mock(json_value=Mock(return_value=sample_images())),
        ]
        model = predictor()
        model.identify.return_value = ([], 0)
        captcha = ReservationCaptcha(page, seat.API_BASE, recognizer=model)
        now = [100.0]
        page.wait_for_timeout.side_effect = lambda milliseconds: now.__setitem__(0, now[0] + milliseconds / 1000)
        with patch.object(captcha_module.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(captcha_module.CaptchaAttemptError) as raised:
                captcha.solve({'publicKey':'fixture-key', 'username':'fixture-user'})
        self.assertEqual(now[0], 100 + captcha_module.RISK_RETRY_INTERVAL)
        self.assertEqual(raised.exception.retry_after, captcha_module.MIN_CHALLENGE_INTERVAL)


class WidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver = sync_playwright().start()
        cls.addClassCleanup(driver.stop)
        cls.browser = driver.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)
        intervals = patch.multiple(captcha_module, MIN_CHALLENGE_INTERVAL=0, RISK_RETRY_INTERVAL=0)
        intervals.start()
        cls.addClassCleanup(intervals.stop)

    def widget_page(self, mode=2, *, failures=0, status=200, failure_code=None, message=None):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.set_default_timeout(4000)
        page.route('https://zw.whut.edu.cn/jsq-v/**', lambda route: route.fulfill(
            content_type='text/html', body=WIDGET,
        ))
        checks = []

        def check(route):
            request = route.request.post_data_json
            checks.append(request)
            accepted = len(checks) > failures and abs(request['distance'] - 228) < 1
            if request.get('checkStartOrigin'):
                down = request['localDown']
                accepted = accepted and 0 <= down['x'] <= down['width'] and 0 <= down['y'] <= down['height']
            route.fulfill(status=status, json={
                'status': accepted, 'data': {'token': 'server-issued-token'} if accepted else {},
                'code': failure_code if not accepted else 200,
                'message': message,
            })

        page.route('https://zw.whut.edu.cn/jsq/static/cap/cg/check', check)
        page.goto('https://zw.whut.edu.cn/jsq-v/')
        page.evaluate('''({mode, sample}) => {
            window.sample = sample;
            sessionStorage.setItem('jsq_p-systemInfo', JSON.stringify({mackCaptcha:mode, makeDataRemark:'fixture-key'}));
            sessionStorage.setItem('jsq_p-userInfo', JSON.stringify({username:'fixture-user'}));
        }''', dict(mode=mode, sample=sample_images()))
        return page, checks

    def test_disabled_mode_never_loads_model_or_opens_widget(self):
        page, checks = self.widget_page(mode=0)
        captcha = ReservationCaptcha(page, seat.API_BASE)
        with patch.object(captcha, 'model') as model:
            self.assertEqual(captcha.token(Mock()), 'capToken')
        model.assert_not_called()
        self.assertEqual(page.evaluate('window.widgetCount'), 0)
        self.assertEqual(checks, [])

    def test_peak_mode_skips_widget_outside_peak(self):
        page, checks = self.widget_page(mode=1)
        api = Mock()
        api.post.return_value = {'status': True, 'data': False}
        captcha = ReservationCaptcha(page, seat.API_BASE)
        self.assertEqual(captcha.token(api), 'capToken')
        api.post.assert_called_once_with('cap/cg/checkHigh')
        self.assertEqual(page.evaluate('window.widgetCount'), 0)

    def test_peak_mode_reloads_settings_after_api_renews_page(self):
        old_page, _ = self.widget_page(mode=1)
        new_page, checks = self.widget_page(mode=1)
        captcha = ReservationCaptcha(old_page, seat.API_BASE, recognizer=predictor())
        api = Mock()

        def peak(*args):
            captcha.page = new_page
            return {'status': True, 'data': True}

        api.post.side_effect = peak
        self.assertEqual(captcha.token(api), 'server-issued-token')
        self.assertEqual(len(checks), 1)
        self.assertEqual(old_page.evaluate('window.widgetCount'), 0)
        self.assertEqual(new_page.evaluate('window.widgetCount'), 1)

    def test_always_mode_uses_widget_token_and_cleans_up(self):
        page, checks = self.widget_page()
        captcha = ReservationCaptcha(page, seat.API_BASE, recognizer=predictor())
        api = Mock()
        self.assertEqual(captcha.token(api), 'server-issued-token')
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]['username'], 'fixture-user')
        self.assertAlmostEqual(checks[0]['distance'], 228, delta=1)
        self.assertEqual(page.locator('#whut-seat-captcha').count(), 0)
        api.post.assert_not_called()

    def test_submitted_pointer_down_origin_lies_inside_the_slider_button(self):
        page, checks = self.widget_page()
        page.evaluate('window.checkStartOrigin = true')
        captcha = ReservationCaptcha(page, seat.API_BASE, recognizer=predictor())
        try:
            token = captcha.token(Mock())
        except CaptchaError as exc:
            self.fail(f'The SDK must receive a pointer-down inside its button: {exc}')
        self.assertEqual(token, 'server-issued-token')
        self.assertEqual(len(checks), 1)

    def test_invalid_sdk_start_is_cancelled_before_server_submission(self):
        page, checks = self.widget_page()
        page.evaluate('window.badStartOffset = 80')
        captcha = ReservationCaptcha(page, seat.API_BASE, recognizer=predictor())
        with self.assertRaisesRegex(captcha_module.CaptchaAttemptError, '起点坐标'):
            captcha.token(Mock())
        self.assertEqual(checks, [])

    def test_drag_varies_speed_and_returns_after_a_small_overshoot(self):
        from slider_motion import make_slider_motion

        page, checks = self.widget_page()
        captcha = ReservationCaptcha(page, seat.API_BASE, recognizer=predictor())
        with patch.object(captcha_module, 'make_slider_motion', side_effect=lambda d, m:
                          make_slider_motion(d, m, rng=random.Random(7))):
            self.assertEqual(captcha.token(Mock()), 'server-issued-token')
        tracks = checks[0]['tracks']
        self.assertGreater(max(p['x'] for p in tracks), checks[0]['distance'] + 1)
        self.assertTrue(any(b['x'] < a['x'] for a, b in zip(tracks, tracks[1:])))
        self.assertTrue(any(abs(p['y']) > .2 for p in tracks))
        self.assertLess(abs(checks[0]['distance']-228), .5)

    def test_integer_pointer_events_do_not_repeat_stationary_positions(self):
        from slider_motion import make_slider_motion

        page, checks = self.widget_page()
        captcha = ReservationCaptcha(page, seat.API_BASE, recognizer=predictor())
        with patch.object(captcha_module, 'make_slider_motion', side_effect=lambda d, m:
                          make_slider_motion(d, m, rng=random.Random(7))):
            self.assertEqual(captcha.token(Mock()), 'server-issued-token')
        tracks = [{'x':0, 'y':0}] + checks[0]['tracks']
        self.assertTrue(all((a['x'],a['y']) != (b['x'],b['y']) for a,b in zip(tracks,tracks[1:])))
        self.assertGreaterEqual(checks[0]['elapsed'] - tracks[-1]['time'], 30)

    def test_sdk_track_limit_is_respected_before_dragging(self):
        page, checks = self.widget_page()
        page.evaluate('window.sliderMaximum = 230')
        model = predictor()
        model.identify.return_value = ([490, 30, 590, 130], .96)
        captcha = ReservationCaptcha(page, seat.API_BASE, recognizer=model)
        with self.assertRaisesRegex(captcha_module.CaptchaAttemptError, '可拖动范围'):
            captcha.token(Mock())
        self.assertEqual(checks, [])

    def test_sdk_server_exemption_must_still_supply_a_token(self):
        page, checks = self.widget_page()
        page.evaluate('window.serverExempt = true')
        captcha = ReservationCaptcha(page, seat.API_BASE, recognizer=predictor())
        try:
            token = captcha.token(Mock())
        except CaptchaError as exc:
            self.fail(f'The official SDK already supplied a valid token: {exc}')
        self.assertEqual(token, 'server-issued-token')
        self.assertEqual(checks, [])

    def test_widget_entrance_animation_finishes_before_drag(self):
        page, checks = self.widget_page()
        page.evaluate('window.animate = true')
        captcha = ReservationCaptcha(page, seat.API_BASE, recognizer=predictor())
        try:
            token = captcha.token(Mock())
        except CaptchaError as exc:
            self.fail(f'The slider must become actionable before dragging: {exc}')
        self.assertEqual(token, 'server-issued-token')
        self.assertEqual(len(checks), 1)

    def test_failed_attempt_can_retry_once(self):
        page, checks = self.widget_page(failures=1)
        captcha = ReservationCaptcha(page, seat.API_BASE, recognizer=predictor())
        self.assertEqual(captcha.token(Mock()), 'server-issued-token')
        self.assertEqual(len(checks), 2)

    def test_repeated_failure_is_bounded_and_never_books(self):
        page, checks = self.widget_page(failures=10)
        api = Mock(captcha=ReservationCaptcha(page, seat.API_BASE, recognizer=predictor()))
        with self.assertRaisesRegex(CaptchaError, '连续两次'):
            seat.book_candidate(api, CANDIDATE, '2026-11-11')
        self.assertEqual(len(checks), 2)
        api.post.assert_not_called()

    def test_server_rejection_details_are_kept_for_the_next_round(self):
        page, checks = self.widget_page(failures=10, failure_code=5004, message='滑动触发风控预警 请重试')
        captcha = ReservationCaptcha(page, seat.API_BASE, recognizer=predictor())
        with self.assertRaises(CaptchaError) as raised:
            captcha.token(Mock())
        self.assertIsInstance(raised.exception, captcha_module.CaptchaAttemptError)
        self.assertIn('5004', str(raised.exception))
        self.assertIn('风控预警', str(raised.exception))
        self.assertEqual(len(checks), 2)

    def test_authentication_expiry_stops_without_another_challenge(self):
        page, checks = self.widget_page(failures=10, failure_code=20003, message='登录过期')
        captcha = ReservationCaptcha(page, seat.API_BASE, recognizer=predictor())
        with self.assertRaisesRegex(CaptchaError, '登录状态已过期') as raised:
            captcha.token(Mock())
        self.assertNotIsInstance(raised.exception, captcha_module.CaptchaAttemptError)
        self.assertEqual(len(checks), 1)

    def test_rate_limit_stops_after_first_attempt(self):
        page, checks = self.widget_page(failures=10, status=429)
        captcha = ReservationCaptcha(page, seat.API_BASE, recognizer=predictor())
        with self.assertRaisesRegex(CaptchaError, '过于频繁'):
            captcha.token(Mock())
        self.assertEqual(len(checks), 1)

    def test_low_confidence_never_drags_or_submits(self):
        page, checks = self.widget_page()
        model = predictor()
        model.identify.return_value = ([], 0)
        captcha = ReservationCaptcha(page, seat.API_BASE, recognizer=model)
        with self.assertRaisesRegex(CaptchaError, '置信度不足'):
            captcha.token(Mock())
        self.assertEqual(checks, [])

    def test_low_confidence_refreshes_the_image_and_can_succeed(self):
        page, checks = self.widget_page()
        model = predictor()
        model.identify.side_effect = [([], 0), ([466, 30, 566, 130], .96)]
        captcha = ReservationCaptcha(page, seat.API_BASE, recognizer=model)
        try:
            token = captcha.token(Mock())
        except CaptchaError as exc:
            self.fail(f'A low-confidence image should be replaced: {exc}')
        self.assertEqual(token, 'server-issued-token')
        self.assertEqual(page.evaluate('window.widgetCount'), 2)
        self.assertEqual(len(checks), 1)

    def test_unknown_mode_and_failed_peak_check_stop(self):
        for mode, result in [(9, None), (1, {'status': False}), (1, {'status': True, 'data': None})]:
            with self.subTest(mode=mode, result=result):
                page, checks = self.widget_page(mode=mode)
                api = Mock()
                api.post.return_value = result
                with self.assertRaises(CaptchaError):
                    ReservationCaptcha(page, seat.API_BASE).token(api)
                self.assertEqual(checks, [])

    def test_authentication_renewal_reuses_browser_and_replaces_context(self):
        old_page, _ = self.widget_page()
        login = object.__new__(seat.SeatLogin)
        login.username, login.password = 'fixture-user', 'fixture-password'
        with tempfile.TemporaryDirectory() as directory:
            login.cache_path = Path(directory) / 'state.json'
            with patch.object(seat, 'authenticate_page', return_value='new-auth') as authenticate:
                replacement, token = login.renew_page(old_page, 'old-auth')
            self.addCleanup(replacement.context.close)
            self.assertEqual(token, 'new-auth')
            self.assertIs(replacement.context.browser, self.browser)
            self.assertTrue(old_page.is_closed())
            self.assertTrue(login.cache_path.is_file())
            authenticate.assert_called_once_with(replacement, 'fixture-user', 'fixture-password')

    def test_rejected_renewal_preserves_old_page_and_closes_new_context(self):
        old_page, _ = self.widget_page()
        login = object.__new__(seat.SeatLogin)
        login.username, login.password = 'fixture-user', 'fixture-password'
        count = len(self.browser.contexts)
        with patch.object(seat, 'authenticate_page', return_value='old-auth'):
            with self.assertRaises(seat.LoginError):
                login.renew_page(old_page, 'old-auth')
        self.assertFalse(old_page.is_closed())
        self.assertEqual(len(self.browser.contexts), count)


class BookingCaptchaTests(unittest.TestCase):
    def test_existing_unfinished_booking_stops_without_more_verification(self):
        api = Mock()
        api.captcha.token.return_value = 'server-issued-token'
        api.post.return_value = {'status':False, 'message':'所选时段存在未结束的预约'}
        args = Namespace(date='2026-11-11', floor=7, start='17:00', duration=30,
                         max_attempts=3, auto_confirm=True, interval=3)
        with patch.object(seat, 'get_rooms', return_value=[{'id':'r','name':'room'}]), \
                patch.object(seat, 'resolve_start_end', return_value=(1020,1050)), \
                patch.object(seat, 'find_candidate', return_value=(True,CANDIDATE)) as find, \
                patch.object(seat.time, 'sleep') as sleep:
            self.assertFalse(seat.run_reservation(api, args, confirm=None))
        api.captcha.token.assert_called_once()
        api.post.assert_called_once()
        find.assert_called_once()
        sleep.assert_not_called()

    def test_verification_time_counts_toward_the_poll_interval(self):
        api = Mock()
        now = [100.0]
        calls = [0]

        def token(_api):
            calls[0] += 1
            if calls[0] == 1:
                now[0] += 4
                raise captcha_module.CaptchaAttemptError('本轮识别失败', retry_after=.8)
            return 'server-issued-token'

        api.captcha.token.side_effect = token
        api.post.return_value = {'status':True, 'data':{}}
        args = Namespace(date='2026-11-11', floor=6, start='09:00', duration=60,
                         max_attempts=0, auto_confirm=True, interval=3)
        with patch.object(seat, 'get_rooms', return_value=[{'id':'r','name':'room'}]), \
                patch.object(seat, 'resolve_start_end', return_value=(540,600)), \
                patch.object(seat, 'find_candidate', return_value=(True,CANDIDATE)), \
                patch.object(seat.time, 'monotonic', side_effect=lambda:now[0]), \
                patch.object(seat.random, 'uniform', return_value=0), \
                patch.object(seat.time, 'sleep') as sleep:
            self.assertTrue(seat.run_reservation(api, args, confirm=None))
        sleep.assert_called_once_with(.8)

    def test_risk_retry_waits_before_trying_again(self):
        self.assertTrue(hasattr(captcha_module, 'CaptchaAttemptError'))
        api = Mock()
        api.captcha.token.side_effect = [
            captcha_module.CaptchaAttemptError('滑动触发风控预警', retry_after=8), 'server-issued-token',
        ]
        api.post.return_value = {'status': True, 'data': {}}
        args = Namespace(date='2026-11-11', floor=6, start='09:00', duration=60,
                         max_attempts=0, auto_confirm=True, interval=3)
        with patch.object(seat, 'get_rooms', return_value=[{'id':'r', 'name':'room'}]), \
                patch.object(seat, 'resolve_start_end', return_value=(540, 600)), \
                patch.object(seat, 'find_candidate', return_value=(True, CANDIDATE)), \
                patch.object(seat.time, 'sleep') as sleep:
            self.assertTrue(seat.run_reservation(api, args, confirm=None))
        sleep.assert_called_once_with(8)

    def test_a_failed_verification_round_does_not_end_infinite_reservation(self):
        self.assertTrue(hasattr(captcha_module, 'CaptchaAttemptError'))
        api = Mock()
        api.captcha.token.side_effect = [
            captcha_module.CaptchaAttemptError('本轮验证码未通过'), 'server-issued-token',
        ]
        api.post.return_value = {'status': True, 'data': {}}
        args = Namespace(date='2026-11-11', floor=6, start='09:00', duration=60,
                         max_attempts=0, auto_confirm=True, interval=3)
        with patch.object(seat, 'get_rooms', return_value=[{'id':'r', 'name':'room'}]), \
                patch.object(seat, 'resolve_start_end', return_value=(540, 600)), \
                patch.object(seat, 'find_candidate', return_value=(True, CANDIDATE)) as find, \
                patch.object(seat.time, 'sleep') as sleep:
            self.assertTrue(seat.run_reservation(api, args, confirm=None))
        self.assertEqual(find.call_count, 2)
        self.assertEqual(api.captcha.token.call_count, 2)
        api.post.assert_called_once()
        sleep.assert_called_once()

    def test_failed_verification_obeys_the_configured_attempt_limit(self):
        self.assertTrue(hasattr(captcha_module, 'CaptchaAttemptError'))
        api = Mock()
        api.captcha.token.side_effect = captcha_module.CaptchaAttemptError('本轮验证码未通过')
        args = Namespace(date='2026-11-11', floor=6, start='09:00', duration=60,
                         max_attempts=1, auto_confirm=True, interval=3)
        with patch.object(seat, 'get_rooms', return_value=[{'id':'r', 'name':'room'}]), \
                patch.object(seat, 'resolve_start_end', return_value=(540, 600)), \
                patch.object(seat, 'find_candidate', return_value=(True, CANDIDATE)), \
                patch.object(seat.time, 'sleep'):
            self.assertFalse(seat.run_reservation(api, args, confirm=None))
        api.captcha.token.assert_called_once()
        api.post.assert_not_called()

    def test_fatal_verification_errors_still_stop_reservation(self):
        api = Mock()
        api.captcha.token.side_effect = CaptchaError('官网验证码接口提示访问过于频繁')
        args = Namespace(date='2026-11-11', floor=6, start='09:00', duration=60,
                         max_attempts=0, auto_confirm=True, interval=3)
        with patch.object(seat, 'get_rooms', return_value=[{'id':'r', 'name':'room'}]), \
                patch.object(seat, 'resolve_start_end', return_value=(540, 600)), \
                patch.object(seat, 'find_candidate', return_value=(True, CANDIDATE)):
            with self.assertRaisesRegex(CaptchaError, '过于频繁'):
                seat.run_reservation(api, args, confirm=None)
        api.post.assert_not_called()

    def test_expired_authentication_regenerates_verification_before_retry(self):
        captcha = Mock()
        captcha.token.side_effect = ['initial-cap-token', 'fresh-cap-token']
        response = Mock(status_code=200)
        response.json.side_effect = [
            {'status': False, 'code': 20003}, {'status': True, 'data': {}},
        ]
        with patch('requests.Session') as session:
            session.return_value.post.return_value = response
            with seat.SeatAPI(Mock(return_value='new-auth'), 'old-auth', captcha=captcha) as api:
                self.assertTrue(seat.book_candidate(api, CANDIDATE, '2026-11-11')[0])
        requests = session.return_value.post.call_args_list
        self.assertTrue(requests[0].args[0].endswith('?capToken=initial-cap-token'))
        self.assertTrue(requests[1].args[0].endswith('?capToken=fresh-cap-token'))
        self.assertEqual([r.kwargs['headers']['token'] for r in requests], ['old-auth', 'new-auth'])
        self.assertEqual(captcha.token.call_count, 2)

    def test_server_issued_token_is_encoded_and_forwarded_to_booking(self):
        api = Mock()
        api.captcha.token.return_value = "verified+/=token"
        api.post.return_value = {"status": True, "data": {"message": "booked"}}
        success, result = seat.book_candidate(api, CANDIDATE, "2026-11-11")
        self.assertTrue(success)
        self.assertEqual(result["message"], "booked")
        api.captcha.token.assert_called_once_with(api)
        api.post.assert_called_once_with(
            "frontApi/make/freeBook/seat-1/2026-11-11/540/600?capToken=verified%2B%2F%3Dtoken",
        )

    def test_failed_verification_prevents_booking(self):
        api = Mock()
        api.captcha.token.side_effect = RuntimeError("verification failed")
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            seat.book_candidate(api, CANDIDATE, "2026-11-11")
        api.post.assert_not_called()

    def test_declined_manual_confirmation_never_starts_verification(self):
        api = Mock()
        args = Namespace(date='2026-11-11', floor=6, start='09:00', duration=60,
                         max_attempts=1, auto_confirm=False, interval=3)
        with patch.object(seat, 'get_rooms', return_value=[{'id':'r', 'name':'room'}]), \
                patch.object(seat, 'resolve_start_end', return_value=(540, 600)), \
                patch.object(seat, 'find_candidate', return_value=(True, CANDIDATE)), \
                patch.object(seat.time, 'sleep'):
            seat.run_reservation(api, args, confirm=lambda candidate, day: False)
        api.captcha.token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
