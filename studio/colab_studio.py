"""Colab Studio: cửa sổ Windows cho Colab CLI.

Backend chạy chính các lệnh `colab` thật (cùng gói colab_cli) trong tiến trình con, đọc kết quả từng dòng
và đẩy lên giao diện. Danh sách máy đọc từ tệp trạng thái của CLI (~/.config/colab-cli/sessions.json)
và đối chiếu với máy chủ Colab bằng `colab sessions`.

Bản exe: `Colab Studio.exe` mở cửa sổ; `Colab Studio.exe --cli <lệnh>` chạy đúng như lệnh `colab <lệnh>`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

APP_NAME = "Colab Studio"
FROZEN = getattr(sys, "frozen", False)
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
UI = BASE / "ui" / "index.html" if FROZEN else Path(__file__).resolve().parent / "ui" / "index.html"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: không bật cửa sổ đen khi chạy lệnh


def cli_argv(args: list[str]) -> list[str]:
    """Lệnh để chạy `colab <args>`: bản exe gọi lại chính nó với --cli; bản mã nguồn gọi Python."""
    if FROZEN:
        return [sys.executable, "--cli", *args]
    return [sys.executable, "-c", "from colab_cli.cli import main; main()", *args]


def cli_env() -> dict:
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "COLUMNS": "160", "NO_COLOR": "1", "TERM": "dumb"})
    return env


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
URL = re.compile(r"https://\S+")


class Job:
    def __init__(self, job_id: str, args: list[str], proc: subprocess.Popen):
        self.id = job_id
        self.args = args
        self.proc = proc
        self.started = time.time()
        self.lines: list[str] = []


class Api:
    """Các hàm giao diện gọi qua window.pywebview.api.*; mọi hàm trả dữ liệu JSON được.

    Thuộc tính nội bộ phải bắt đầu bằng _ : pywebview quét mọi thuộc tính công khai để đưa sang JS, gặp đối tượng
    cửa sổ / khoá thì treo cửa sổ (đã gặp 12/11/2026)."""

    def __init__(self):
        self._window = None
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # ---------- đẩy sự kiện lên giao diện ----------
    def _emit(self, kind: str, data: dict):
        if not self._window:
            return
        try:
            self._window.evaluate_js(f"window.studio && window.studio.on({json.dumps(kind)}, {json.dumps(data, ensure_ascii=False)})")
        except Exception:
            pass

    # ---------- thông tin chung ----------
    def info(self) -> dict:
        from colab_cli import __name__ as _pkg  # noqa: F401 — kiểm tra gói có trong bản build

        try:
            from importlib.metadata import version

            ver = version("google-colab-cli")
        except Exception:
            ver = "?"
        state_file = os.path.expanduser("~/.config/colab-cli/sessions.json")
        oauth = os.path.expanduser("~/.colab-cli-oauth-config.json")
        return {"app": APP_NAME, "cli": ver, "stateFile": state_file, "signedIn": self._signed_in(), "oauthConfig": os.path.exists(oauth)}

    def _signed_in(self) -> bool:
        """Đã có token Google trong Windows Credential Manager (chỗ CLI đọc) hay chưa; không đọc nội dung ra ngoài."""
        try:
            from colab_cli import auth

            return bool(auth._get_token_from_keyring()) or os.path.exists(auth.LEGACY_TOKEN_PATH)
        except Exception:
            return False

    # ---------- đăng nhập Google (kiểu dán mã, giống CLI) ----------
    def _client_config(self) -> dict:
        from importlib import resources

        custom = os.path.expanduser("~/.colab-cli-oauth-config.json")
        if os.path.exists(custom):
            return json.loads(Path(custom).read_text(encoding="utf-8"))
        return json.loads(resources.files("colab_cli").joinpath("oauth_config.json").read_text())

    def auth_start(self) -> dict:
        """Mở trang đăng nhập Google; Google hiện mã xác thực để người dùng dán vào app."""
        from google_auth_oauthlib.flow import InstalledAppFlow
        from colab_cli import auth

        flow = InstalledAppFlow.from_client_config(self._client_config(), auth.PUBLIC_SCOPES)
        flow.redirect_uri = auth.REMOTE_REDIRECT_URI
        url, _ = flow.authorization_url(prompt="consent", token_usage="remote")
        self._flow = flow
        self.open_url(url)
        return {"url": url}

    def auth_finish(self, code: str) -> dict:
        from colab_cli import auth

        flow = getattr(self, "_flow", None)
        if not flow:
            return {"ok": False, "error": "Bấm Đăng nhập Google trước."}
        try:
            flow.fetch_token(code=str(code).strip())
            auth._save_token_to_keyring(flow.credentials.to_json())
            self._flow = None
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:400]}

    def auth_logout(self) -> dict:
        try:
            import keyring
            from colab_cli import auth

            keyring.delete_password(auth.KEYRING_SERVICE_NAME, auth.KEYRING_USERNAME)
        except Exception:
            pass
        return {"ok": True, "signedIn": self._signed_in()}

    # ---------- máy (session) ----------
    def sessions(self) -> list[dict]:
        """Máy đã tạo từ máy tính này (đọc nhanh tệp trạng thái của CLI, không gọi mạng)."""
        try:
            from colab_cli.state import StateStore

            out = []
            for name, s in StateStore().list().items():
                out.append({
                    "name": name, "accelerator": s.accelerator, "variant": s.variant, "shape": s.machine_shape,
                    "busy": s.running, "endpoint": s.endpoint,
                    "last": list(s.last_execution) if s.last_execution else None,
                })
            return sorted(out, key=lambda x: x["name"])
        except Exception as e:
            return [{"error": str(e)}]

    # ---------- chạy lệnh colab ----------
    def run(self, args: list, stdin_text: str | None = None, label: str = "") -> dict:
        """Chạy `colab <args>` thật; kết quả từng dòng đẩy lên giao diện. Trả job id."""
        args = [str(a) for a in (args or [])]
        job_id = uuid.uuid4().hex[:8]
        proc = subprocess.Popen(
            cli_argv(args), stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=cli_env(), creationflags=NO_WINDOW,
            cwd=str(Path.home()),
        )
        job = Job(job_id, args, proc)
        with self._lock:
            self._jobs[job_id] = job
        self._emit("start", {"id": job_id, "args": args, "label": label})
        if stdin_text is not None:
            def feed():
                try:
                    proc.stdin.write(stdin_text.encode("utf-8"))
                    proc.stdin.close()
                except Exception:
                    pass
            threading.Thread(target=feed, daemon=True).start()
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        return {"id": job_id}

    def _pump(self, job: Job):
        buf = b""
        opened: set[str] = set()
        out = job.proc.stdout
        while True:
            chunk = out.read1(4096) if hasattr(out, "read1") else out.read(4096)
            if not chunk:
                break
            buf += chunk
            *lines, buf = re.split(rb"\r\n|\n|\r", buf)
            for raw in lines:
                self._line(job, raw, opened)
        if buf:
            self._line(job, buf, opened)
        code = job.proc.wait()
        self._emit("end", {"id": job.id, "code": code, "seconds": round(time.time() - job.started, 1), "args": job.args})
        with self._lock:
            self._jobs.pop(job.id, None)

    def _line(self, job: Job, raw: bytes, opened: set):
        text = ANSI.sub("", raw.decode("utf-8", "replace")).rstrip()
        if not text:
            return
        job.lines.append(text)
        # Đăng nhập lần đầu: CLI in đường dẫn Google để người dùng mở; app báo cho giao diện hiện nút mở
        for url in URL.findall(text):
            if "accounts.google.com" in url and url not in opened:
                opened.add(url)
                self._emit("auth", {"id": job.id, "url": url})
        self._emit("line", {"id": job.id, "text": text})

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return False
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/pid", str(job.proc.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
            else:
                job.proc.terminate()
        except Exception:
            pass
        return True

    def capture(self, args: list, timeout: float = 120) -> dict:
        """Chạy `colab <args>` và chờ xong, trả toàn bộ chữ (cho danh sách tệp, đối chiếu máy…)."""
        try:
            r = subprocess.run(cli_argv([str(a) for a in args]), capture_output=True, env=cli_env(), timeout=timeout,
                               creationflags=NO_WINDOW, stdin=subprocess.DEVNULL, cwd=str(Path.home()))
            text = ANSI.sub("", (r.stdout + r.stderr).decode("utf-8", "replace"))
            return {"code": r.returncode, "text": text}
        except subprocess.TimeoutExpired:
            return {"code": -1, "text": f"Quá {int(timeout)} giây chưa xong."}

    def server_sessions(self) -> dict:
        """Máy đang chạy trên tài khoản Colab (gọi máy chủ)."""
        r = self.capture(["sessions"], timeout=60)
        rows = []
        for line in r["text"].splitlines():
            m = re.match(r"\[(.+?)\]\s+(\S+)\s*\|\s*Hardware:\s*([^|]+)\|\s*Shape:\s*([^|]+)\|\s*Variant:\s*([^|]+)(?:\|\s*Status:\s*(.+))?", line.strip())
            if m:
                rows.append({"name": m.group(1).strip(), "endpoint": m.group(2), "accelerator": m.group(3).strip(),
                             "shape": m.group(4).strip(), "variant": m.group(5).strip(), "status": (m.group(6) or "").strip()})
        return {"code": r["code"], "rows": rows, "text": r["text"]}

    def list_files(self, session: str, path: str = "") -> dict:
        args = ["ls", "-s", session] + ([path] if path else [])
        r = self.capture(args, timeout=60)
        items = []
        if r["code"] == 0:
            for line in r["text"].splitlines():
                t = line.strip()
                if not t or t.startswith("[colab]"):
                    continue
                items.append({"name": t.rstrip("/"), "dir": t.endswith("/")})
        return {"code": r["code"], "items": items, "text": r["text"]}

    # ---------- hộp thoại tệp ----------
    def pick_file(self, kinds: str = "code") -> str | None:
        import webview

        types = {
            "code": ("Python / Notebook (*.py;*.ipynb)", "Tất cả (*.*)"),
            "req": ("requirements (*.txt)", "Tất cả (*.*)"),
            "any": ("Tất cả (*.*)",),
        }.get(kinds, ("Tất cả (*.*)",))
        r = self._window.create_file_dialog(webview.OPEN_DIALOG, file_types=types)
        return r[0] if r else None

    def save_file(self, suggested: str) -> str | None:
        import webview

        r = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename=os.path.basename(suggested or "tep"))
        if not r:
            return None
        return r if isinstance(r, str) else r[0]

    def read_text(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8", errors="replace")[:500_000]

    def open_path(self, path: str) -> bool:
        try:
            if os.name == "nt":
                os.startfile(path)  # noqa: S606 — mở tệp / đường dẫn người dùng chọn
            else:
                subprocess.Popen(["xdg-open", path])
            return True
        except Exception:
            return False

    def open_url(self, url: str) -> bool:
        if not str(url).startswith("https://"):
            return False
        import webbrowser

        return webbrowser.open(url)


def dark_titlebar(title: str):
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if hwnd:
            on = ctypes.c_int(1)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                    break
    except Exception:
        pass


def main():
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    # Chế độ dòng lệnh: Colab Studio.exe --cli new --gpu T4  ==  colab new --gpu T4
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        sys.argv = ["colab", *sys.argv[2:]]
        from colab_cli.cli import main as cli_main

        cli_main()
        return

    import webview

    api = Api()
    win = webview.create_window(APP_NAME, str(UI), js_api=api, width=1440, height=920, min_size=(1000, 640),
                                maximized=True, background_color="#000000", text_select=True)
    api._window = win
    win.events.shown += lambda: threading.Thread(target=dark_titlebar, args=(APP_NAME,), daemon=True).start()
    storage = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "colab-studio" / "webview"
    webview.start(gui="edgechromium" if os.name == "nt" else None, private_mode=False, storage_path=str(storage))
    # đóng cửa sổ: dừng các lệnh đang chạy dở
    for job_id in list(api._jobs):
        api.cancel(job_id)


if __name__ == "__main__":
    main()
