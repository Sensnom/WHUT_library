import argparse
import errno
import json
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import find_spec
from pathlib import Path
from threading import Thread

from .configuration import Configuration
from .jobs import ROOT, JobRunner, reservation_options

DEFAULT_PORT = 8766
STATIC = Path(__file__).resolve().parent / "static"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


def make_server(port, runner, config=None):
    config = config if config is not None else Configuration(ROOT / ".env")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, content_type, body):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status, data):
            self.send(status, "application/json; charset=utf-8", json.dumps(data).encode("utf-8"))

        def valid_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.valid_host():
                self.send_json(403, {"error": "请使用启动时显示的本地地址。"})
            elif self.path == "/api/status":
                self.send_json(200, runner.snapshot())
            elif self.path == "/api/availability":
                self.send_json(200, runner.availability_snapshot())
            elif self.path == "/api/config":
                try:
                    settings = config.public()
                except ImportError:
                    self.send_json(500, {"error": "当前 Python 缺少 python-dotenv。请用项目的 .venv 重新启动工作台。"})
                except (OSError, ValueError):
                    self.send_json(500, {"error": "无法读取 .env，请检查文件权限和编码。"})
                else:
                    self.send_json(200, settings)
            elif self.path in ASSETS:
                filename, content_type = ASSETS[self.path]
                self.send(200, content_type, (STATIC / filename).read_bytes())
            else:
                self.send_json(404, {"error": "页面不存在。"})

        def do_POST(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if not self.valid_host() or self.headers.get("Origin") != origin:
                self.send_json(403, {"error": "仅接受本地管理台发出的操作。"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096 or self.headers.get_content_type() != "application/json":
                    raise ValueError("请求须为 JSON，且长度不能超过 4 KB。")
                try:
                    data = json.loads(self.rfile.read(length))
                except ValueError:
                    raise ValueError("请求的 JSON 格式有误。") from None
                if not isinstance(data, dict):
                    raise ValueError("请求格式有误。")

                if self.path in ("/api/start", "/api/login", "/api/availability"):
                    kind = {"/api/start": "reservation", "/api/login": "login",
                            "/api/availability": "availability"}[self.path]
                    options = reservation_options(data)
                    if kind == "availability":
                        step = data.get("time_step", 30)
                        if type(step) is not int or step not in (0, 15, 30):
                            raise ValueError("请选择 15 分钟、30 分钟网格或逐座精确查询。")
                        options["time_step"] = step
                        resume = data.get("resume", False)
                        if type(resume) is not bool:
                            raise ValueError("继续查询的参数有误。")
                        options["resume"] = resume
                        refresh = data.get("refresh", False)
                        if type(refresh) is not bool:
                            raise ValueError("刷新参数有误。")
                        options["refresh"] = refresh
                        options["config_version"] = config.path.stat().st_mtime_ns if config.path.exists() else 0
                    command = [sys.executable, "-u", "-m", "webui.worker", kind, json.dumps(options)]
                    state = runner.start(command, kind)
                elif self.path == "/api/config":
                    state = config.save(data)
                elif self.path == "/api/confirm":
                    state = runner.confirm(data.get("id"), data.get("decision"))
                elif self.path == "/api/stop":
                    state = runner.stop(data.get("id"))
                else:
                    self.send_json(404, {"error": "操作不存在。"})
                    return
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            except ImportError:
                self.send_json(500, {"error": "当前 Python 缺少 python-dotenv。请用项目的 .venv 重新启动工作台。"})
            except OSError:
                message = "无法写入 .env，请检查项目目录的写入权限。" if self.path == "/api/config" else "无法控制任务进程。"
                self.send_json(500, {"error": message})
            else:
                self.send_json(200, state)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description="启动南湖图书馆本地预约工作台")
    parser.add_argument("--port", type=int, help=f"指定端口，默认优先使用 {DEFAULT_PORT}")
    parser.add_argument("--no-browser", action="store_true", help="启动服务时不自动打开页面")
    args = parser.parse_args()
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("端口须为 1–65535。")

    if find_spec("dotenv") is None:
        venv = ROOT / ".venv"
        interpreter = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if interpreter.is_file() and Path(sys.prefix).resolve() != venv.resolve():
            os.execv(str(interpreter), [str(interpreter), "-m", "webui", *sys.argv[1:]])
        parser.error("缺少 python-dotenv，请在运行工作台的 Python 环境中安装项目依赖。")

    runner = JobRunner()
    try:
        server = make_server(args.port or DEFAULT_PORT, runner)
    except OSError as exc:
        if args.port is not None or exc.errno != errno.EADDRINUSE:
            parser.error(f"无法启动本地服务：{exc}")
        server = make_server(0, runner)
    with server:
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"预约工作台：{url}", flush=True)
        if not args.no_browser:
            Thread(target=webbrowser.open_new_tab, args=(url,), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            runner.close()


if __name__ == "__main__":
    main()
