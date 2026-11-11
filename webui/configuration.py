"""Read and update the WebUI's allowlisted .env settings."""

import json
import os
import shutil
import tempfile
from pathlib import Path
from threading import Lock

from .jobs import booking_date, reservation_options


def parse_bool(value):
    value = value.lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError("请输入布尔值。")


DEFAULT_FIELDS = {
    "floor": int,
    "date": str,
    "start": str,
    "duration": int,
    "interval": float,
    "max_attempts": int,
    "auto_confirm": parse_bool,
    "once": parse_bool,
    "session": str,
}


def env_key(field):
    return f"WHUT_{field.upper()}"


def validate_defaults(defaults):
    # Save the attempt count even when “once” currently overrides it.
    options = reservation_options({**defaults, "once": False})
    once = defaults.get("once", False)
    if type(once) is not bool:
        raise ValueError("请选择有效的尝试模式。")
    return {**options, "once": once}


class Configuration:
    def __init__(self, path):
        self.path = path
        self.lock = Lock()

    def public(self):
        with self.lock:
            return self._public()

    def _public(self):
        from dotenv import dotenv_values

        values = dotenv_values(self.path, interpolate=False)
        defaults = validate_defaults({})
        invalid = []
        for field, parse in DEFAULT_FIELDS.items():
            key = env_key(field)
            if key not in values:
                continue
            try:
                value = parse(values[key] or "")
                if field == "date":
                    if value in ("", booking_date("today")):
                        value = "today"
                    elif value == booking_date("tomorrow"):
                        value = "tomorrow"
                    elif value not in ("today", "tomorrow"):
                        raise ValueError("预约日期请选择今天或明天。")
                defaults = validate_defaults({**defaults, field: value})
            except ValueError:
                invalid.append(key)

        defaults["date"] = defaults["date"] or "today"
        return {
            "username": values.get("WHUT_USERNAME") or "",
            "password_configured": bool(values.get("WHUT_PASSWORD")),
            "defaults": defaults,
            "invalid": invalid,
        }

    def save(self, data):
        from dotenv import dotenv_values, set_key, unset_key

        defaults = data.get("defaults")
        username = data.get("username")
        password = data.get("password", "")
        clear_password = data.get("clear_password", False)
        if not isinstance(defaults, dict) or not isinstance(username, str):
            raise ValueError("配置格式有误。")
        if not isinstance(password, str) or type(clear_password) is not bool:
            raise ValueError("密码配置格式有误。")
        if any(char in username or char in password for char in ("\r", "\n", "\0")):
            raise ValueError("账号和密码须为单行文本。")
        if password and clear_password:
            raise ValueError("请在更新密码和清除密码之间选择一项。")
        defaults = validate_defaults(defaults)
        changes = {"WHUT_USERNAME": username.strip()}
        for field, value in defaults.items():
            changes[env_key(field)] = "" if value is None else str(value)
        if any("\n" in value or "\r" in value or "\0" in value for value in changes.values()):
            raise ValueError("配置须为单行文本。")

        with self.lock:
            fd, filename = tempfile.mkstemp(dir=self.path.parent, prefix=".env.")
            os.close(fd)
            temp = Path(filename)
            try:
                old_password = None
                if self.path.exists():
                    shutil.copyfile(self.path, temp)
                    old_password = dotenv_values(temp, interpolate=False).get("WHUT_PASSWORD")
                    if old_password is not None:
                        unset_key(str(temp), "WHUT_PASSWORD")
                for key, value in changes.items():
                    set_key(str(temp), key, json.dumps(value, ensure_ascii=False), quote_mode="never")
                saved_password = password if password or clear_password else old_password
                if saved_password is not None:
                    # A trailing backslash in a quoted value can make dotenv
                    # consume the next quoted line. Keep the password at EOF.
                    set_key(str(temp), "WHUT_PASSWORD", json.dumps(saved_password, ensure_ascii=False),
                            quote_mode="never")
                    changes["WHUT_PASSWORD"] = saved_password
                written = dotenv_values(temp, interpolate=False)
                if any(written.get(key) != value for key, value in changes.items()):
                    raise ValueError("当前配置无法写入 .env，请检查文本中的引号和反斜杠。")
                temp.chmod(0o600)
                temp.replace(self.path)
            finally:
                temp.unlink(missing_ok=True)
            return self._public()
