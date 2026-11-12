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
URL = re.compile(r"https://[^\s'\"<>]+")
SESSION_LINE = re.compile(
    r"\[(.+?)\]\s+(\S+)\s*\|\s*Hardware:\s*([^|]+)\|\s*Shape:\s*([^|]+)\|\s*Variant:\s*([^|]+)(?:\|\s*Status:\s*(.+))?"
)


def content_path(cwd: str = "", name: str = "") -> str:
    """Đường dẫn cho Jupyter Contents API của Colab. API tính từ gốc "/" của máy ảo, còn app chỉ hiện /content,
    nên mọi đường dẫn đều nối sau "content"."""
    return "/".join(["content"] + [x for x in f"{cwd}/{name}".split("/") if x])


def parse_sessions(text: str) -> list[dict]:
    """Đọc kết quả `colab sessions`. Tên "?" là máy mồ côi: vẫn chạy (và tính tiền) trên Colab nhưng máy tính
    này không còn hồ sơ của nó."""
    rows = []
    for line in text.splitlines():
        m = SESSION_LINE.match(line.strip())
        if m:
            name = m.group(1).strip()
            rows.append({"name": name, "endpoint": m.group(2), "accelerator": m.group(3).strip(),
                         "shape": m.group(4).strip(), "variant": m.group(5).strip(),
                         "status": (m.group(6) or "").strip(), "orphan": name == "?"})
    return rows


def fs_code(op: str, path: str) -> str:
    """Code Python chạy trên máy Colab để tạo / xoá thư mục (Contents API không xoá được thư mục còn tệp).
    Đường dẫn đưa vào bằng repr() nên tên có dấu cách, dấu nháy, chữ Việt đều an toàn."""
    full = "/" + path.strip("/")
    if op == "mkdir":
        return f"import os\nos.makedirs({full!r}, exist_ok=True)\nprint('@@OK@@')"
    if op == "rmdir":
        if full.rstrip("/") in ("", "/content"):
            raise ValueError("Không xoá thư mục gốc /content")
        return f"import shutil\nshutil.rmtree({full!r})\nprint('@@OK@@')"
    raise ValueError(f"Thao tác không hỗ trợ: {op}")


class Job:
    def __init__(self, job_id: str, args: list[str], proc: subprocess.Popen, interactive: bool = False):
        self.id = job_id
        self.args = args
        self.proc = proc
        self.interactive = interactive
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

    # ---------- tài khoản: gói, đơn vị tính toán, GPU được dùng (đọc thẳng máy chủ Colab) ----------
    def _client(self):
        from colab_cli.common import state

        return state.client

    def account(self) -> dict:
        """Số dư / tốc độ tiêu đơn vị tính toán, máy đang chạy, GPU · TPU được dùng, email.
        Gọi /tun/m/ccu-info và /tun/m/assignments của Colab (giống CLI) nhưng giữ cả các trường CLI bỏ qua."""
        if not self._signed_in():
            return {"signedIn": False}
        from urllib.parse import urljoin

        try:
            cl = self._client()
            raw = cl._issue_request(urljoin(cl.colab_domain, "/tun/m/ccu-info"), schema=dict) or {}
            asg = (cl._issue_request(urljoin(cl.colab_domain, "/tun/m/assignments"), schema=dict) or {}).get("assignments") or []
        except Exception as e:
            return {"signedIn": True, "error": str(e)[:400]}
        bal = float(raw.get("currentBalance") or 0)
        rate = float(raw.get("consumptionRateHourly") or 0)
        gpus = [str(g) for g in raw.get("eligibleGpus") or []]
        tpus = [str(t) for t in raw.get("eligibleTpus") or []]
        # Colab không trả tên gói: suy ra từ quyền dùng GPU và số dư (ghi rõ là ước đoán)
        if "A100" in gpus or "H100" in gpus:
            plan = "Có trả phí — Colab Pro / Pro+ hoặc mua thêm đơn vị (dùng được GPU cao cấp)"
        elif bal > 0:
            plan = "Có đơn vị tính toán trả phí"
        else:
            plan = "Miễn phí (GPU T4 theo hạn mức của Google)"
        machines = []
        for a in asg:
            machines.append({k: a.get(k) for k in ("endpoint", "accelerator", "variant", "machineShape") if k in a})
        return {
            "signedIn": True, "email": self._email(), "balance": round(bal, 2), "rate": round(rate, 2),
            "hoursLeft": round(bal / rate, 1) if rate > 0 else None, "assignments": int(raw.get("assignmentsCount") or len(asg)),
            "machines": machines, "eligibleGpus": gpus, "ineligibleGpus": [str(g) for g in raw.get("ineligibleGpus") or []],
            "eligibleTpus": tpus, "ineligibleTpus": [str(t) for t in raw.get("ineligibleTpus") or []],
            "plan": plan, "planGuessed": True,
            "extra": {k: v for k, v in raw.items() if k not in ("currentBalance", "consumptionRateHourly", "assignmentsCount", "eligibleGpus", "ineligibleGpus", "eligibleTpus", "ineligibleTpus")},
            "at": time.strftime("%H:%M:%S"),
        }

    def _email(self) -> str | None:
        """Email tài khoản Google đang đăng nhập (userinfo, dùng phiên đã xác thực của CLI)."""
        if getattr(self, "_email_cache", None):
            return self._email_cache
        try:
            r = self._client().session.get("https://www.googleapis.com/oauth2/v3/userinfo", timeout=15)
            self._email_cache = r.json().get("email") if r.ok else None
        except Exception:
            self._email_cache = None
        return self._email_cache

    STATS_CODE = r"""
import json, os, shutil, subprocess
def _mb(n): return round(n / 1048576)
out = {"cpus": os.cpu_count()}
try:
    t = {}
    for line in open('/proc/meminfo'):
        k, v = line.split(':'); t[k] = int(v.split()[0]) * 1024
    out["ramTotal"], out["ramUsed"] = _mb(t['MemTotal']), _mb(t['MemTotal'] - t.get('MemAvailable', t['MemFree']))
except Exception: pass
try:
    d = shutil.disk_usage('/content'); out["diskTotal"], out["diskUsed"] = _mb(d.total), _mb(d.used)
except Exception: pass
try:
    q = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total,memory.used,utilization.gpu', '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=10).stdout.strip()
    out["gpus"] = [dict(zip(['name', 'memTotal', 'memUsed', 'util'], [x.strip() for x in l.split(',')])) for l in q.splitlines() if l.strip()]
except Exception: out["gpus"] = []
print('@@STATS@@' + json.dumps(out))
"""

    def machine_stats(self, session: str) -> dict:
        """RAM, ổ đĩa /content, GPU (bộ nhớ, % dùng) của một máy: chạy một đoạn code nhỏ trên máy đó."""
        r = self.capture(["exec", "-s", session, "--timeout", "60"], timeout=120, stdin_text=self.STATS_CODE)
        m = re.search(r"@@STATS@@(\{.*\})", r["text"])
        if not m:
            return {"ok": False, "text": r["text"][-600:]}
        data = json.loads(m.group(1))
        data["ok"] = True
        return data

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
    def run(self, args: list, stdin_text: str | None = None, label: str = "", interactive: bool = False) -> dict:
        """Chạy `colab <args>` thật; kết quả từng dòng đẩy lên giao diện. Trả job id.

        interactive=True giữ stdin mở để người dùng gửi Enter / mã xác thực: gắn Drive và cấp quyền GCP chờ đúng
        việc này (trước đây stdin bị đóng nên hai lệnh đó không thể xong)."""
        args = [str(a) for a in (args or [])]
        job_id = uuid.uuid4().hex[:8]
        interactive = bool(interactive) and stdin_text is None
        proc = subprocess.Popen(
            cli_argv(args), stdin=subprocess.PIPE if (stdin_text is not None or interactive) else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=cli_env(), creationflags=NO_WINDOW,
            cwd=str(Path.home()),
        )
        job = Job(job_id, args, proc, interactive)
        with self._lock:
            self._jobs[job_id] = job
        self._emit("start", {"id": job_id, "args": args, "label": label, "interactive": interactive})
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
        # Đường dẫn CLI in ra (đăng nhập, cấp quyền Drive / GCP): giao diện hiện thành nút bấm mở được
        for url in URL.findall(text):
            url = url.rstrip(".,;)")
            if url not in opened and ("google.com" in url or "googleapis.com" in url):
                opened.add(url)
                self._emit("link", {"id": job.id, "url": url})
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

    def send_input(self, job_id: str, text: str = "") -> bool:
        """Gửi một dòng (Enter, mã xác thực…) vào lệnh đang chờ nhập."""
        with self._lock:
            job = self._jobs.get(job_id)
        if not job or not job.interactive or job.proc.stdin is None:
            return False
        try:
            job.proc.stdin.write((str(text) + "\n").encode("utf-8"))
            job.proc.stdin.flush()
            return True
        except Exception:
            return False

    def capture(self, args: list, timeout: float = 120, stdin_text: str | None = None) -> dict:
        """Chạy `colab <args>` và chờ xong, trả toàn bộ chữ (cho danh sách tệp, đối chiếu máy…)."""
        try:
            r = subprocess.run(cli_argv([str(a) for a in args]), capture_output=True, env=cli_env(), timeout=timeout,
                               creationflags=NO_WINDOW, cwd=str(Path.home()),
                               **({"input": stdin_text.encode("utf-8")} if stdin_text is not None else {"stdin": subprocess.DEVNULL}))
            text = ANSI.sub("", (r.stdout + r.stderr).decode("utf-8", "replace"))
            return {"code": r.returncode, "text": text}
        except subprocess.TimeoutExpired:
            return {"code": -1, "text": f"Quá {int(timeout)} giây chưa xong."}

    def server_sessions(self) -> dict:
        """Máy đang chạy trên tài khoản Colab (gọi máy chủ)."""
        r = self.capture(["sessions"], timeout=60)
        return {"code": r["code"], "rows": parse_sessions(r["text"]), "text": r["text"]}

    def _store(self):
        from colab_cli.state import StateStore

        return StateStore()

    def unassign(self, endpoint: str) -> dict:
        """Tắt máy theo endpoint, kể cả máy mồ côi (CLI không có lệnh tắt theo endpoint)."""
        endpoint = str(endpoint)
        try:
            self._client().unassign(endpoint)
        except Exception as e:
            return {"ok": False, "error": str(e)[:400]}
        try:
            store = self._store()
            for name, s in list(store.list().items()):
                if getattr(s, "endpoint", None) == endpoint:
                    store.remove(name)
        except Exception:
            pass
        return {"ok": True}

    def list_files(self, session: str, path: str = "") -> dict:
        """Liệt kê /content/<path> trên máy Colab."""
        r = self.capture(["ls", "-s", session, content_path(path)], timeout=60)
        items = []
        if r["code"] == 0:
            for line in r["text"].splitlines():
                t = line.strip()
                if not t or t.startswith("[colab]"):
                    continue
                items.append({"name": t.rstrip("/"), "dir": t.endswith("/")})
        return {"code": r["code"], "items": items, "text": r["text"]}

    # ---------- sửa tệp trên máy Colab (như `colab edit`, nhưng sửa ngay trong app) ----------
    EDIT_LIMIT = 2_000_000

    def _tmp(self, name: str) -> Path:
        d = Path(os.environ.get("TEMP") or Path.home()) / "colab-studio-edit"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{uuid.uuid4().hex[:6]}-{Path(name).name or 'tep'}"

    def read_remote(self, session: str, path: str) -> dict:
        tmp = self._tmp(path)
        try:
            r = self.capture(["download", "-s", session, path, str(tmp)], timeout=300)
            if r["code"] != 0 or not tmp.exists():
                return {"ok": False, "error": r["text"].strip()[-400:] or "Không tải được tệp"}
            data = tmp.read_bytes()
            if len(data) > self.EDIT_LIMIT:
                return {"ok": False, "error": "Tệp quá lớn để sửa trong app (trên 2 MB), hãy dùng Tải về."}
            if b"\x00" in data[:8192]:
                return {"ok": False, "error": "Đây là tệp nhị phân, không sửa dạng chữ được, hãy dùng Tải về."}
            return {"ok": True, "text": data.decode("utf-8", "replace")}
        finally:
            tmp.unlink(missing_ok=True)

    def write_remote(self, session: str, path: str, text: str) -> dict:
        tmp = self._tmp(path)
        try:
            tmp.write_bytes(str(text).encode("utf-8"))
            r = self.capture(["upload", "-s", session, str(tmp), path], timeout=300)
            return {"ok": r["code"] == 0, "error": "" if r["code"] == 0 else r["text"].strip()[-400:]}
        finally:
            tmp.unlink(missing_ok=True)

    def fs_op(self, session: str, op: str, path: str) -> dict:
        """Tạo / xoá thư mục trên máy Colab."""
        try:
            code = fs_code(op, path)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        r = self.capture(["exec", "-s", session, "--timeout", "120"], timeout=180, stdin_text=code)
        ok = "@@OK@@" in r["text"]
        return {"ok": ok, "error": "" if ok else r["text"].strip()[-400:]}

    # ---------- lối tắt Windows ----------
    def create_shortcuts(self) -> dict:
        """Tạo lối tắt Colab Studio trên Desktop và Start Menu (chỉ bản exe)."""
        if not FROZEN or os.name != "nt":
            return {"ok": False, "error": "Chỉ tạo được từ bản Colab Studio.exe"}
        exe = sys.executable
        q = lambda v: "'" + str(v).replace("'", "''") + "'"  # noqa: E731 — chuỗi PowerShell
        ps = (
            "$s=New-Object -ComObject WScript.Shell;"
            "foreach($d in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))){"
            "$l=$s.CreateShortcut((Join-Path $d 'Colab Studio.lnk'));"
            f"$l.TargetPath={q(exe)};$l.WorkingDirectory={q(Path(exe).parent)};"
            f"$l.IconLocation={q(exe + ',0')};$l.Description='Colab Studio';$l.Save()}}"
        )
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                               creationflags=NO_WINDOW, timeout=60)
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}
        return {"ok": r.returncode == 0, "error": r.stderr.decode("utf-8", "replace")[-300:]}

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

    def pick_files(self) -> list[str]:
        import webview

        r = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True, file_types=("Tất cả (*.*)",))
        return list(r or [])

    def write_text(self, path: str, text: str) -> bool:
        Path(path).write_text(str(text), encoding="utf-8")
        return True

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
