"""Recognize reservation sliders locally; let the official SDK issue capToken."""

import base64
import math
import time
from urllib.parse import urlsplit

from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout

from slider_motion import make_slider_motion


class CaptchaError(RuntimeError):
    """Verification did not produce a usable reservation token."""


class CaptchaAttemptError(CaptchaError):
    """A fresh challenge may succeed; the reservation task can keep running."""
    def __init__(self, message, *, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


MIN_CHALLENGE_INTERVAL = 1.5
RISK_RETRY_INTERVAL = 3.0
MAX_RISK_RETRY_INTERVAL = 6.0


SETTINGS = """() => {
    const info = JSON.parse(sessionStorage.getItem('jsq_p-systemInfo') || 'null');
    const user = JSON.parse(sessionStorage.getItem('jsq_p-userInfo') || 'null');
    if (!info || !user) return null;
    return {mode: info.mackCaptcha, publicKey: info.makeDataRemark, username: user.username};
}"""

OPEN_WIDGET = """({apiBase, publicKey, username}) => {
    window.__whutSeatCaptcha?.widget?.destroyWindow();
    document.getElementById('whut-seat-captcha')?.remove();
    const mount = document.createElement('div');
    mount.id = 'whut-seat-captcha';
    // The deployed SDK excludes the outermost fixed ancestor's offset when
    // recording pointer-down coordinates. Keep that ancestor at the origin.
    mount.style.cssText = 'position:fixed;inset:0 auto auto 0;z-index:2147483647';
    document.body.append(mount);
    const state = window.__whutSeatCaptcha = {status: 'pending'};
    TAC.enc.rsaPublicKey = publicKey;
    state.widget = new TAC({
        requestCaptchaDataUrl: apiBase + '/cap/cg/gen/SLIDER',
        validCaptchaUrl: apiBase + '/cap/cg/check',
        bindEl: '#whut-seat-captcha', customData: {username},
        validSuccess(response, element, widget) {
            state.token = response?.data?.token;
            state.status = typeof state.token === 'string' && state.token ? 'success' : 'failed';
            widget.destroyWindow();
        },
        validFail(response) {
            state.status = 'failed';
            state.code = response?.code;
            state.message = response?.msg || response?.message || '服务器未通过验证';
        },
        btnRefreshFun(element, widget) { widget.reloadCaptcha(); },
        btnCloseFun(element, widget) { state.status = 'closed'; widget.destroyWindow(); },
    }, {});
    state.widget.init();
}"""

IMAGES = """() => {
    const state = window.__whutSeatCaptcha;
    if (state?.status !== 'pending') return {status: state?.status, token: state?.token,
                                           code:state?.code, message:state?.message};
    const bg = document.getElementById('tianai-captcha-slider-bg-img');
    const piece = document.getElementById('tianai-captcha-slider-move-img');
    if (!bg?.complete || !piece?.complete || !bg.naturalWidth || !piece.naturalWidth) return false;
    const b = bg.getBoundingClientRect(), p = piece.getBoundingClientRect();
    if (!b.width || !b.height || !p.width) return false;
    const sdkLimit = state.widget?.C?.currentCaptchaData?.end;
    const maximum = Math.min(b.width - p.width,
        Number.isFinite(sdkLimit) && sdkLimit > 0 ? sdkLimit : b.width - p.width);
    return {background: bg.src, piece: piece.src, rendered_width: b.width,
            piece_left: p.left - b.left, piece_top: p.top - b.top, max_drag: maximum};
}"""

CLOSE_WIDGET = """() => {
    window.__whutSeatCaptcha?.widget?.destroyWindow();
    document.getElementById('whut-seat-captcha')?.remove();
    delete window.__whutSeatCaptcha;
}"""


VERIFY_START = """() => {
    const state = window.__whutSeatCaptcha;
    const button = document.getElementById('tianai-captcha-slider-move-btn');
    const point = state?.widget?.C?.currentCaptchaData?.trackList?.[0];
    const rect = button?.getBoundingClientRect();
    const ok = point?.type === 'down' && Number.isFinite(point.x) && Number.isFinite(point.y)
        && rect?.width > 0 && rect?.height > 0
        && point.x >= 0 && point.x <= rect.width && point.y >= 0 && point.y <= rect.height;
    if (!ok) {
        state?.widget?.destroyWindow();
        if (state) state.status = 'failed';
    }
    return !!ok;
}"""


def aligned_mask_distance(background, piece, x, y, scale, recognizer, minimum_confidence):
    """Align the supplied alpha silhouette to a confidently segmented gap."""
    import cv2
    import numpy as np

    predict = getattr(recognizer, 'predict', None)
    if not callable(predict):
        return None
    alpha = (piece[:, :, 3] > 128).astype(np.float32)
    rows, columns = np.where(alpha)
    if not len(columns):
        raise CaptchaError('验证码滑块轮廓为空。')
    ph, pw = alpha.shape
    height, width = background.shape[:2]
    boxes, masks = predict(background, conf=minimum_confidence, iou=.8)[0]
    candidates = []
    for box, mask in zip(boxes, masks):
        if len(box) < 5 or not np.isfinite(box[:5]).all() or box[4] < minimum_confidence:
            continue
        if not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
            continue
        target = np.asarray(mask, dtype=np.float32)
        if target.shape != (height, width) or not np.isfinite(target).all():
            raise CaptchaError('验证码分割模型输出尺寸或数值异常。')
        guess = round(float(box[0]) - int(columns.min()))
        low, high = max(x + 1, guess - 15), min(width - pw, guess + 15)
        if low > high:
            continue
        intersection = cv2.matchTemplate(target[y:y + ph], alpha, cv2.TM_CCORR)[0][low:high + 1]
        alpha_area, target_area = alpha.sum(), target.sum()
        intersection = np.clip(intersection, 0, min(alpha_area, target_area))
        scores = intersection / (alpha_area + target_area - intersection)
        best = float(scores.max())
        if best < .85:
            continue
        indices = np.flatnonzero(np.isclose(scores, best, rtol=1e-6))
        position = low + float(indices.mean())
        candidates.append((best, (position - x) * scale, float(box[4])))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < .05:
        raise CaptchaAttemptError('存在多个相似缺口，无法可靠定位')
    _, distance, confidence = candidates[0]
    return distance, confidence


def drag_distance(images, recognizer, minimum_confidence=0.8):
    """Measure the gap and initial piece in source pixels, then apply CSS scale."""
    import cv2
    import numpy as np

    def decode(source, flags):
        if not isinstance(source, str) or not source.startswith('data:image/') or ';base64,' not in source:
            raise CaptchaError('验证码图片格式有变化，请检查官网组件。')
        try:
            raw = base64.b64decode(source.split(',', 1)[1], validate=True)
            image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), flags)
        except (ValueError, cv2.error):
            image = None
        if image is None:
            raise CaptchaError('验证码图片无法读取。')
        return image

    background = decode(images['background'], cv2.IMREAD_COLOR)
    piece = decode(images['piece'], cv2.IMREAD_UNCHANGED)
    height, width = background.shape[:2]
    scale = float(images['rendered_width']) / width
    if not math.isfinite(scale) or scale <= 0 or piece.ndim != 3 or piece.shape[2] != 4:
        raise CaptchaError('验证码尺寸或透明滑块格式不正确。')
    x = round(float(images['piece_left']) / scale)
    y = round(float(images['piece_top']) / scale)
    ph, pw = piece.shape[:2]
    if x < 0 or y < 0 or x + pw > width or y + ph > height:
        raise CaptchaError('验证码滑块超出背景图片范围。')
    aligned = aligned_mask_distance(background, piece, x, y, scale, recognizer, minimum_confidence)
    if aligned is not None:
        return aligned
    composite = background.copy()
    alpha = piece[:, :, 3:4].astype(np.float32) / 255
    composite[y:y + ph, x:x + pw] = (
        piece[:, :, :3] * alpha + composite[y:y + ph, x:x + pw] * (1 - alpha)
    ).astype(np.uint8)
    box, confidence = recognizer.identify(source=composite, conf=minimum_confidence)
    if len(box) != 4 or not math.isfinite(float(confidence)) or confidence < minimum_confidence:
        raise CaptchaAttemptError('滑块缺口识别置信度不足')
    if not all(math.isfinite(float(v)) for v in box) or not (
        0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height
    ):
        raise CaptchaAttemptError('滑块缺口识别位置无效')
    offset, offset_confidence = recognizer.identify_offset(source=composite, conf=minimum_confidence)
    if not math.isfinite(float(offset)) or not math.isfinite(float(offset_confidence)) or (
        offset_confidence < minimum_confidence or not x <= offset < x + pw
    ):
        raise CaptchaAttemptError('滑块起始位置识别失败')
    distance = (box[0] - offset) * scale
    if abs(distance) < 1e-6:
        raise CaptchaAttemptError('未能区分滑块本体和目标缺口')
    if not 0 < distance < images['rendered_width']:
        raise CaptchaAttemptError('滑块移动距离无效')
    return distance, min(float(confidence), float(offset_confidence))


class ReservationCaptcha:
    def __init__(self, page, api_base, *, recognizer=None):
        self.page = page
        self.api_base = api_base
        self.recognizer = recognizer
        self.next_challenge_at = 0.0
        self.risk_failures = 0

    def settings(self):
        return self.page.wait_for_function(SETTINGS, timeout=15000).json_value()

    def token(self, api):
        try:
            settings = self.settings()
            mode = settings.get('mode')
            if mode in (0, '0'):
                return 'capToken'
            if mode in (1, '1'):
                result = api.post('cap/cg/checkHigh')
                if not isinstance(result, dict) or not result.get('status') or not isinstance(result.get('data'), bool):
                    raise CaptchaError('官网未能确认是否需要预约验证，本次预约已停止。')
                if not result['data']:
                    return 'capToken'
                # The peak check may have renewed authentication and replaced our page.
                settings = self.settings()
            elif mode not in (2, '2'):
                raise CaptchaError('官网预约验证配置有变化，本次预约已停止。')
            if not settings.get('publicKey') or not settings.get('username'):
                raise CaptchaError('官网预约验证配置不完整。')
            return self.solve(settings)
        except BrowserTimeout:
            raise CaptchaError('等待官网预约验证码超时，本次预约已停止。') from None
        except BrowserError:
            raise CaptchaError('官网预约验证组件交互失败，本次预约已停止。') from None

    def model(self):
        if self.recognizer is None:
            try:
                from captcha_recognizer.slider import Slider
                self.recognizer = Slider()
            except Exception as exc:
                raise CaptchaError('验证码识别模型无法加载，请运行 uv sync 并检查运行环境。') from exc
        return self.recognizer

    def solve(self, settings):
        page = self.page
        page.wait_for_function("() => typeof TAC === 'function'", timeout=15000)
        # Initialize before requesting a short-lived challenge.
        recognizer = self.model()
        rate_limited = False
        authentication_expired = False
        last_reason = '服务器未通过验证'

        def observe(response):
            nonlocal rate_limited, authentication_expired
            url = urlsplit(response.url)
            base = urlsplit(self.api_base)
            if (url.scheme, url.netloc) != (base.scheme, base.netloc) or not url.path.startswith(base.path + '/cap/cg/'):
                return
            if response.status == 429:
                rate_limited = True
            elif response.status == 200:
                try:
                    data = response.json()
                    if isinstance(data, dict):
                        if '过于频繁' in str(data.get('message') or data.get('msg') or ''):
                            rate_limited = True
                        if str(data.get('code')) == '20003':
                            authentication_expired = True
                except (BrowserError, ValueError):
                    pass

        page.on('response', observe)
        try:
            for attempt in range(2):
                confidence = None
                delay = self.next_challenge_at - time.monotonic()
                if delay > 0:
                    page.wait_for_timeout(delay * 1000)
                print(f'🧩 正在识别预约滑块验证码（{attempt + 1}/2）...')
                self.next_challenge_at = time.monotonic() + MIN_CHALLENGE_INTERVAL
                page.evaluate(OPEN_WIDGET, {
                    'apiBase': self.api_base, 'publicKey': settings['publicKey'], 'username': settings['username'],
                })
                try:
                    images = page.wait_for_function(IMAGES, timeout=15000).json_value()
                    if 'background' in images:
                        try:
                            distance, confidence = drag_distance(images, recognizer)
                        except CaptchaError:
                            raise
                        except Exception as exc:
                            raise CaptchaError('预约滑块图片识别失败，本次预约已停止。') from exc
                        slider = page.locator('#tianai-captcha-slider-move-btn')
                        # Image loading finishes before the SDK's entrance animation.
                        slider.hover(timeout=15000)
                        button = slider.bounding_box()
                        if not button:
                            raise CaptchaError('官网滑块按钮不可见。')
                        maximum = images['max_drag']
                        if not 0 < distance <= maximum:
                            raise CaptchaAttemptError('识别位置超过滑块可拖动范围')
                        motion = make_slider_motion(distance, maximum)
                        x, y = round(button['x'] + button['width'] / 2), round(button['y'] + button['height'] / 2)
                        page.mouse.move(x, y)
                        page.mouse.down()
                        try:
                            if not page.evaluate(VERIFY_START):
                                raise CaptchaAttemptError('SDK 起点坐标超出滑块按钮，已取消本次提交')
                            started = time.monotonic()
                            last_pointer = (x, y)
                            for point in motion:
                                remaining = point.time_ms - (time.monotonic() - started) * 1000
                                if remaining > 0:
                                    page.wait_for_timeout(remaining)
                                pointer = (min(math.floor(x + maximum), round(x + point.x)),
                                           round(y + point.y))
                                # Dispatch only a real integer-coordinate change;
                                # stationary samples represent elapsed time.
                                if pointer != last_pointer:
                                    page.mouse.move(*pointer)
                                    last_pointer = pointer
                        finally:
                            page.mouse.up()
                        state = page.wait_for_function(
                            "() => {const s = window.__whutSeatCaptcha; return s?.status !== 'pending' && {status:s.status, token:s.token, code:s.code, message:s.message};}",
                            timeout=15000,
                        ).json_value()
                    else:
                        state = images
                except CaptchaAttemptError as exc:
                    last_reason = str(exc)
                    print(f'⚠️ {last_reason}' + ('，更换验证码。' if attempt == 0 else '。'))
                    continue
                except BrowserTimeout:
                    last_reason = '当前验证码加载或验证超时'
                    print(f'⚠️ {last_reason}' + ('，更换验证码。' if attempt == 0 else '。'))
                    continue
                finally:
                    if rate_limited:
                        raise CaptchaError('官网验证码接口提示访问过于频繁，本次预约已停止。')
                    if authentication_expired:
                        raise CaptchaError('预约验证的登录状态已过期，请重新启动任务登录。')
                if state.get('status') == 'success' and state.get('token'):
                    self.risk_failures = 0
                    detail = f'（识别置信度 {confidence:.2f}）' if confidence is not None else ''
                    print(f'✅ 预约滑块验证通过{detail}')
                    return state['token']
                if state.get('status') == 'closed':
                    raise CaptchaError('预约验证码窗口已关闭。')
                last_reason = str(state.get('message') or '服务器未通过验证')[:160]
                code = state.get('code')
                if code is not None:
                    last_reason = f'{last_reason}（错误码 {code}）'
                if str(code) == '5004' or '风控' in last_reason:
                    self.risk_failures += 1
                    risk_delay = min(MAX_RISK_RETRY_INTERVAL, RISK_RETRY_INTERVAL * min(self.risk_failures, 2))
                    self.next_challenge_at = max(self.next_challenge_at, time.monotonic() + risk_delay)
                print(f'⚠️ {last_reason}' + ('，更换验证码。' if attempt == 0 else '。'))
            remaining = max(0, self.next_challenge_at - time.monotonic())
            raise CaptchaAttemptError(f'本轮验证码连续两次未通过：{last_reason}', retry_after=remaining)
        finally:
            page.remove_listener('response', observe)
            try:
                page.evaluate(CLOSE_WIDGET)
            except BrowserError:
                pass
