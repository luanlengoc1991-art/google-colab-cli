"""Tests for the Colab Studio desktop backend (studio/colab_studio.py)."""

import importlib.util
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("colab_studio", ROOT / "studio" / "colab_studio.py")
studio = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(studio)


# ---------- paths: the Jupyter contents API is rooted at "/", the app shows /content ----------
@pytest.mark.parametrize(
    "cwd,name,expected",
    [
        ("", "", "content"),
        ("", "a.txt", "content/a.txt"),
        ("sub", "", "content/sub"),
        ("sub/deep", "x.py", "content/sub/deep/x.py"),
        ("/sub/", "/x.py", "content/sub/x.py"),
    ],
)
def test_content_path(cwd, name, expected):
    assert studio.content_path(cwd, name) == expected


def test_list_files_lists_inside_content(mocker):
    api = studio.Api()
    cap = mocker.patch.object(api, "capture", return_value={"code": 0, "text": "data/\nmodel.pt\n[colab] note\n"})
    r = api.list_files("s1", "sub")
    assert cap.call_args[0][0] == ["ls", "-s", "s1", "content/sub"]
    assert r["items"] == [{"name": "data", "dir": True}, {"name": "model.pt", "dir": False}]


# ---------- `colab sessions` parsing, including orphaned server-side machines ----------
SESSIONS_TEXT = """\
[gpu1] gpu-t4-s-abc | Hardware: T4 | Shape: Standard | Variant: GPU
[?] m-s-orphan | Hardware: CPU | Shape: High-RAM | Variant: DEFAULT
"""


def test_parse_sessions_marks_orphans():
    rows = studio.parse_sessions(SESSIONS_TEXT)
    assert [r["name"] for r in rows] == ["gpu1", "?"]
    assert rows[0]["endpoint"] == "gpu-t4-s-abc" and rows[0]["accelerator"] == "T4"
    assert rows[0]["orphan"] is False and rows[1]["orphan"] is True
    assert rows[1]["shape"] == "High-RAM"


# ---------- stopping a machine by endpoint (orphans have no local name) ----------
def test_unassign_calls_client_and_drops_local_state(mocker):
    api = studio.Api()
    client = mocker.MagicMock()
    mocker.patch.object(api, "_client", return_value=client)
    store = mocker.MagicMock()
    local = mocker.MagicMock(endpoint="ep-1")
    store.list.return_value = {"box": local}
    mocker.patch.object(api, "_store", return_value=store)
    r = api.unassign("ep-1")
    client.unassign.assert_called_once_with("ep-1")
    store.remove.assert_called_once_with("box")
    assert r["ok"] is True


def test_unassign_reports_errors(mocker):
    api = studio.Api()
    client = mocker.MagicMock()
    client.unassign.side_effect = RuntimeError("boom")
    mocker.patch.object(api, "_client", return_value=client)
    mocker.patch.object(api, "_store", return_value=mocker.MagicMock(list=lambda: {}))
    r = api.unassign("ep-x")
    assert r["ok"] is False and "boom" in r["error"]


# ---------- interactive jobs: drivemount / auth wait for Enter or a pasted code ----------
ECHO = "import sys\nline = sys.stdin.readline()\nprint('got:' + line.strip())\n"


def _wait_end(api, job_id, events):
    done = threading.Event()

    def emit(kind, data):
        events.append((kind, data))
        if kind == "end" and data["id"] == job_id:
            done.set()

    api._emit = emit
    return done


def test_interactive_job_receives_input(mocker):
    mocker.patch.object(studio, "cli_argv", lambda args: [sys.executable, "-c", ECHO])
    api = studio.Api()
    events = []
    api._emit = lambda k, d: events.append((k, d))
    job = api.run(["drivemount"], None, "", True)
    done = _wait_end(api, job["id"], events)
    assert api.send_input(job["id"], "hello") is True
    assert done.wait(20)
    lines = [d["text"] for k, d in events if k == "line"]
    assert "got:hello" in lines


def test_non_interactive_job_rejects_input(mocker):
    mocker.patch.object(studio, "cli_argv", lambda args: [sys.executable, "-c", "import time; time.sleep(1)"])
    api = studio.Api()
    api._emit = lambda k, d: None
    job = api.run(["status"])
    assert api.send_input(job["id"], "x") is False
    api.cancel(job["id"])


def test_send_input_unknown_job():
    assert studio.Api().send_input("nope", "x") is False


def test_job_stdin_text_still_supported(mocker):
    mocker.patch.object(studio, "cli_argv", lambda args: [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"])
    api = studio.Api()
    events = []
    api._emit = lambda k, d: events.append((k, d))
    job = api.run(["exec"], "abc")
    done = _wait_end(api, job["id"], events)
    assert done.wait(20)
    assert "ABC" in [d["text"] for k, d in events if k == "line"]


# ---------- links printed by the CLI (Drive consent, GCP auth) become clickable ----------
def test_any_google_link_is_announced():
    api = studio.Api()
    events = []
    api._emit = lambda k, d: events.append((k, d))
    job = studio.Job("j1", ["drivemount"], None)
    api._line(job, b"Please visit: https://accounts.google.com/o/oauth2/auth?x=1", set())
    api._line(job, b"visit https://colab.research.google.com/drive-auth?y=2 now", set())
    links = [d["url"] for k, d in events if k == "link"]
    assert links == ["https://accounts.google.com/o/oauth2/auth?x=1", "https://colab.research.google.com/drive-auth?y=2"]


# ---------- kernel-side helper code is built with repr() (safe for any path) ----------
def test_fs_code_quotes_paths():
    code = studio.fs_code("mkdir", "content/a b/it's")
    assert "os.makedirs('/content/a b/it\\'s', exist_ok=True)" in code or 'os.makedirs("/content/a b/it\'s", exist_ok=True)' in code
    assert "shutil.rmtree" in studio.fs_code("rmdir", "content/x")
    with pytest.raises(ValueError):
        studio.fs_code("rmdir", "content")  # never wipe /content itself
    with pytest.raises(ValueError):
        studio.fs_code("format", "content/x")


# ---------- remote editor: download -> edit -> upload ----------
def test_read_and_write_remote(mocker, tmp_path):
    api = studio.Api()
    calls = []

    def fake_capture(args, timeout=120, stdin_text=None):
        calls.append(args)
        if args[0] == "download":
            Path(args[-1]).write_bytes(b"print(1)\n")
        if args[0] == "upload":
            calls.append(("uploaded", Path(args[3]).read_bytes().decode("utf-8")))
        return {"code": 0, "text": ""}

    mocker.patch.object(api, "capture", side_effect=fake_capture)
    r = api.read_remote("s1", "content/a.py")
    assert r == {"ok": True, "text": "print(1)\n"}
    assert calls[0][:4] == ["download", "-s", "s1", "content/a.py"]
    w = api.write_remote("s1", "content/a.py", "print(2)\n")
    assert w["ok"] is True
    assert calls[1][:3] == ["upload", "-s", "s1"] and calls[1][4] == "content/a.py"
    assert ("uploaded", "print(2)\n") in calls


def test_read_remote_refuses_huge_or_binary(mocker):
    api = studio.Api()

    def fake_capture(args, timeout=120, stdin_text=None):
        Path(args[-1]).write_bytes(b"\x00\x01\x02" * 10)
        return {"code": 0, "text": ""}

    mocker.patch.object(api, "capture", side_effect=fake_capture)
    r = api.read_remote("s1", "content/blob.bin")
    assert r["ok"] is False
