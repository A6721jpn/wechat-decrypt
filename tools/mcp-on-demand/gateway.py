"""Keep a small MCP connection alive while its workers sleep between requests."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field


DISCOVERY = ("tools/list", "resources/list", "resources/templates/list", "prompts/list")
CACHE_VERSION = 1


class TemporaryDirectory:
    def __init__(self, prefix):
        self.path = Path(tempfile.gettempdir()) / (prefix + uuid.uuid4().hex)
        self.path.mkdir()
        self.name = str(self.path)

    def cleanup(self):
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self):
        return self.name

    def __exit__(self, *_):
        self.cleanup()


def envelope(message):
    return {key: message[key] for key in ("result", "error") if key in message}


def params_key(params=None):
    normalized = {key: value for key, value in (params or {}).items() if key != "_meta"}
    if normalized.get("cursor") is None:
        normalized.pop("cursor", None)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def error_reply(request_id, message, code=-32000):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def emit(message):
    try:
        sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        return False
    return True


def read_messages(stream, events, owner):
    try:
        for line in stream:
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError):
                events.put((owner, "invalid", None))
                continue
            if isinstance(message, dict):
                events.put((owner, "message", message))
            else:
                events.put((owner, "invalid", None))
    finally:
        events.put((owner, "eof", None))


def forward_stderr(stream):
    try:
        for line in stream:
            sys.stderr.write(line)
            sys.stderr.flush()
    except (OSError, ValueError):
        pass


class WorkerTree:
    """A Windows job records ownership without killing workers on gateway exit."""

    def __init__(self, process):
        self.handle = None
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            self.api = ctypes.WinDLL("kernel32", use_last_error=True)
            self.api.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
            self.api.CreateJobObjectW.restype = wintypes.HANDLE
            self.api.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            self.api.AssignProcessToJobObject.restype = wintypes.BOOL
            self.api.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = self.api.CreateJobObjectW(None, None)
            if handle and self.api.AssignProcessToJobObject(handle, int(process._handle)):
                self.handle = handle
            elif handle:
                self.api.CloseHandle(handle)

    def resume(self, process):
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("id", wintypes.DWORD),
                        ("process", wintypes.DWORD), ("priority", wintypes.LONG),
                        ("delta", wintypes.LONG), ("flags", wintypes.DWORD)]

        self.api.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        self.api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.api.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
        self.api.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
        self.api.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.api.OpenThread.restype = wintypes.HANDLE
        self.api.ResumeThread.argtypes = (wintypes.HANDLE,)
        self.api.ResumeThread.restype = wintypes.DWORD
        snapshot = self.api.CreateToolhelp32Snapshot(4, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise OSError("Cannot inspect suspended worker thread")
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            found = self.api.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.process == process.pid:
                    thread = self.api.OpenThread(2, False, entry.id)
                    if thread:
                        try:
                            if self.api.ResumeThread(thread) != 0xFFFFFFFF:
                                return
                        finally:
                            self.api.CloseHandle(thread)
                found = self.api.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self.api.CloseHandle(snapshot)
        raise OSError("Cannot resume suspended worker thread")

    def stop_idle_python_processes(self):
        if self.handle is None:
            return
        import ctypes
        from ctypes import wintypes

        class ProcessIds(ctypes.Structure):
            _fields_ = [("assigned", wintypes.DWORD), ("count", wintypes.DWORD),
                        ("ids", ctypes.c_size_t * 256)]

        data = ProcessIds()
        self.api.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
        self.api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                                        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        self.api.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        if not self.api.QueryInformationJobObject(self.handle, 3, ctypes.byref(data), ctypes.sizeof(data), None):
            return
        for pid in list(data.ids)[:min(data.count, 256)]:
            handle = self.api.OpenProcess(0x1001, False, pid)
            if not handle:
                continue
            try:
                length = wintypes.DWORD(32768)
                path = ctypes.create_unicode_buffer(length.value)
                if self.api.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
                    if Path(path.value).name.lower() in ("python.exe", "pythonw.exe", "python3.exe"):
                        self.api.TerminateProcess(handle, 0)
            finally:
                self.api.CloseHandle(handle)

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None


class Backend:
    def __init__(self, profile, events, state_dir):
        env = os.environ.copy()
        env["MCP_IDLE_STATE_DIR"] = str(state_dir)
        self.process = subprocess.Popen(
            profile["command"], cwd=profile.get("cwd") or None, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) | (4 if os.name == "nt" else 0)),
        )
        self.tree = WorkerTree(self.process)
        try:
            self.tree.resume(self.process)
        except OSError:
            self.process.terminate()
            self.process.wait()
            self.tree.close()
            raise
        threading.Thread(target=read_messages, args=(self.process.stdout, events, self), daemon=True).start()
        threading.Thread(target=forward_stderr, args=(self.process.stderr,), daemon=True).start()

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def close_idle(self):
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.tree.stop_idle_python_processes()
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        self.tree.close()


def fingerprint(profile):
    digest = hashlib.sha256(json.dumps(profile, sort_keys=True).encode())
    digest.update(str(CACHE_VERSION).encode())
    for entry in profile.get("watch_files", []):
        path = Path(entry)
        paths = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for candidate in paths:
            if any(part in (".venv", "venv", "__pycache__", ".git") for part in candidate.parts):
                continue
            digest.update(str(candidate).encode())
            try:
                digest.update(candidate.read_bytes())
            except OSError:
                digest.update(b"<missing>")
    return digest.hexdigest()


def fetch_metadata(profile, cache_path):
    events = queue.Queue()
    counter = 0
    with TemporaryDirectory(prefix="mcp-discovery-") as state_dir:
        worker = Backend(profile, events, state_dir)

        def request(method, params=None):
            nonlocal counter
            counter += 1
            request_id = f"discovery-{counter}"
            worker.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            deadline = time.monotonic() + profile.get("startup_timeout", 30)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Discovery timed out for {method}")
                _, event, message = events.get(timeout=remaining)
                if event == "eof":
                    raise RuntimeError("Worker exited during discovery")
                if event != "message":
                    continue
                if message.get("id") == request_id and "method" not in message:
                    return envelope(message)
                if "method" in message and "id" in message:
                    worker.send(error_reply(message["id"], "Discovery client has no interactive capabilities", -32601))

        try:
            initial = request("initialize", {
                "protocolVersion": profile.get("protocol_version", "2025-06-18"),
                "capabilities": {}, "clientInfo": {"name": "mcp-on-demand-discovery", "version": "1"},
            })
            if "error" in initial:
                raise RuntimeError("Worker rejected discovery initialization")
            worker.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            metadata = {"version": CACHE_VERSION, "fingerprint": fingerprint(profile),
                        "initialize": initial, "discovery": {}}
            for method in DISCOVERY:
                pages = {}
                params = {}
                while True:
                    key = params_key(params)
                    if key in pages:
                        raise RuntimeError(f"Worker repeated a discovery cursor for {method}")
                    page = request(method, params)
                    pages[key] = page
                    cursor = page.get("result", {}).get("nextCursor")
                    if not cursor:
                        break
                    params = {"cursor": cursor}
                metadata["discovery"][method] = pages
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=cache_path.parent, delete=False) as output:
                json.dump(metadata, output, ensure_ascii=False)
                temporary = output.name
            os.replace(temporary, cache_path)
            return metadata
        finally:
            worker.close_idle()


def load_metadata(profile, cache_path):
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("version") == CACHE_VERSION and cached.get("fingerprint") == fingerprint(profile):
            return cached
    except (OSError, ValueError):
        pass
    return fetch_metadata(profile, cache_path)


@dataclass
class Session:
    worker: Backend | None = None
    phase: str = "sleeping"
    queued: list = field(default_factory=list)
    pending: dict = field(default_factory=dict)
    server_requests: dict = field(default_factory=dict)
    subscriptions: set = field(default_factory=set)
    pinned: bool = False
    deadline: float = 0
    client_open: bool = True
    initialize_params: dict | None = None
    sequence: int = 0

    def request_id(self):
        self.sequence += 1
        return f"gateway-{self.sequence}"


class Gateway:
    def __init__(self, profile, cache_path):
        self.profile = profile
        self.cache_path = cache_path
        self.metadata = load_metadata(profile, cache_path)
        self.events = queue.Queue()
        self.state = Session()
        self.storage = TemporaryDirectory(prefix="mcp-on-demand-")
        self.state_dir = Path(self.storage.name)
        self.retired = []

    def respond(self, message):
        if self.state.client_open and not emit(message):
            self.state.client_open = False

    def fail_worker(self, reason):
        state = self.state
        for original in list(state.pending.values()) + state.queued:
            if "id" in original:
                self.respond(error_reply(original["id"], reason + "; the operation was not retried"))
        state.pending.clear()
        state.queued.clear()
        state.server_requests.clear()
        state.subscriptions.clear()
        state.pinned = False
        if state.worker is not None:
            self.retired.append(state.worker)
        state.worker = None
        state.phase = "sleeping"

    def start(self):
        state = self.state
        state.worker = Backend(self.profile, self.events, self.state_dir)
        state.phase = "starting"
        state.deadline = time.monotonic() + self.profile.get("startup_timeout", 30)
        params = copy.deepcopy(state.initialize_params)
        params["protocolVersion"] = self.metadata["initialize"]["result"]["protocolVersion"]
        state.worker.send({"jsonrpc": "2.0", "id": "gateway-initialize", "method": "initialize", "params": params})

    def dispatch(self, message):
        state = self.state
        outbound = copy.deepcopy(message)
        if "id" in message:
            request_id = state.request_id()
            state.pending[request_id] = message
            outbound["id"] = request_id
        state.worker.send(outbound)
        state.phase = "running"
        state.deadline = time.monotonic() + self.profile.get("idle_seconds", 0)

    def from_client(self, message):
        state = self.state
        method = message.get("method")
        if method == "initialize":
            state.initialize_params = message.get("params", {})
            self.respond({"jsonrpc": "2.0", "id": message["id"], **self.metadata["initialize"]})
            return
        if method == "notifications/initialized":
            return
        if method is None:
            original_id = state.server_requests.pop(message.get("id"), None)
            if original_id is not None and state.worker is not None:
                state.worker.send({**message, "id": original_id})
            return
        if method == "notifications/cancelled":
            original_id = message.get("params", {}).get("requestId")
            for original in list(state.queued):
                if original.get("id") == original_id:
                    state.queued.remove(original)
                    self.respond(error_reply(original_id, "Request cancelled before dispatch", -32800))
                    return
            for internal_id, original in state.pending.items():
                if original.get("id") == original_id and state.worker is not None:
                    state.worker.send({**message, "params": {**message.get("params", {}), "requestId": internal_id}})
                    return
            return
        if state.initialize_params is None:
            if "id" in message:
                self.respond(error_reply(message["id"], "Initialize the MCP connection first", -32002))
            return
        if method in DISCOVERY:
            self.metadata = load_metadata(self.profile, self.cache_path)
            page = self.metadata["discovery"].get(method, {}).get(params_key(message.get("params")))
            if page is not None:
                self.respond({"jsonrpc": "2.0", "id": message["id"], **page})
                return
        if method == "ping":
            self.respond({"jsonrpc": "2.0", "id": message["id"], "result": {}})
            return
        if state.worker is None:
            state.queued.append(message)
            self.start()
        elif state.phase == "starting":
            state.queued.append(message)
        else:
            if "id" not in message:
                state.pinned = True
            self.dispatch(message)

    def from_worker(self, message):
        state = self.state
        if message.get("id") == "gateway-initialize" and "method" not in message:
            version = message.get("result", {}).get("protocolVersion")
            if "error" in message or version != self.metadata["initialize"]["result"]["protocolVersion"]:
                worker = state.worker
                self.fail_worker("Worker initialization failed or negotiated a different protocol")
                worker.close_idle()
                return
            state.worker.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            queued, state.queued = state.queued, []
            for item in queued:
                if "id" not in item:
                    state.pinned = True
                self.dispatch(item)
            state.phase = "running"
            return
        if "method" in message:
            if "id" in message:
                if not state.client_open:
                    state.worker.send(error_reply(message["id"], "MCP client disconnected"))
                    return
                frontend_id = state.request_id()
                state.server_requests[frontend_id] = message["id"]
                self.respond({**message, "id": frontend_id})
            else:
                if message.get("method") == "notifications/cancelled":
                    original_id = message.get("params", {}).get("requestId")
                    for frontend_id, backend_id in state.server_requests.items():
                        if backend_id == original_id:
                            message = {**message, "params": {**message["params"], "requestId": frontend_id}}
                            state.server_requests.pop(frontend_id)
                            break
                self.respond(message)
            return
        original = state.pending.pop(message.get("id"), None)
        if original is None:
            return
        if "result" in message:
            uri = original.get("params", {}).get("uri")
            if original.get("method") == "resources/subscribe":
                state.subscriptions.add(uri)
            elif original.get("method") == "resources/unsubscribe":
                state.subscriptions.discard(uri)
        self.respond({**message, "id": original["id"]})
        state.deadline = time.monotonic() + self.profile.get("idle_seconds", 0)

    def run(self):
        threading.Thread(target=read_messages, args=(sys.stdin, self.events, "client"), daemon=True).start()
        state = self.state
        try:
            while state.client_open or state.pending or state.queued:
                try:
                    owner, event, message = self.events.get(timeout=0.05)
                except queue.Empty:
                    owner, event, message = None, None, None
                if owner == "client":
                    if event == "eof":
                        state.client_open = False
                        if state.worker is not None:
                            for original_id in state.server_requests.values():
                                try:
                                    state.worker.send(error_reply(original_id, "MCP client disconnected"))
                                except OSError:
                                    pass
                        state.server_requests.clear()
                    elif event == "invalid":
                        self.respond(error_reply(None, "Expected a JSON-RPC object", -32700))
                    else:
                        try:
                            self.from_client(message)
                        except (OSError, RuntimeError, ValueError, queue.Empty) as exc:
                            if state.pending or state.queued:
                                self.fail_worker(f"Worker connection failed ({type(exc).__name__})")
                            elif "id" in message:
                                self.respond(error_reply(message["id"], f"Gateway request failed ({type(exc).__name__})"))
                elif owner is state.worker and owner is not None:
                    if event == "eof":
                        self.fail_worker("Worker exited before completing the request")
                    elif event == "message":
                        try:
                            self.from_worker(message)
                        except (OSError, RuntimeError):
                            self.fail_worker("Worker connection closed")
                now = time.monotonic()
                if state.phase == "starting" and now >= state.deadline:
                    worker = state.worker
                    self.fail_worker("Worker initialization timed out")
                    worker.close_idle()
                idle = (state.worker is not None and state.phase == "running" and not state.pending
                        and not state.queued and not state.server_requests)
                if idle and not state.pinned and not state.subscriptions and now >= state.deadline:
                    if not (self.state_dir / "no-idle").exists():
                        worker, state.worker = state.worker, None
                        state.phase = "sleeping"
                        worker.close_idle()
        finally:
            if state.worker is not None and not state.pending:
                state.worker.close_idle()
            for worker in self.retired:
                worker.tree.close()
            if all(worker.process.poll() is not None for worker in self.retired):
                self.storage.cleanup()
            else:
                print("An interrupted worker may still be finishing; its session state was retained.", file=sys.stderr)


def main():
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("serve", "refresh"))
    parser.add_argument("profile")
    parser.add_argument("--profiles", type=Path, default=Path(__file__).with_name("profiles.json"))
    args = parser.parse_args()
    document = json.loads(args.profiles.read_text(encoding="utf-8-sig"))
    profiles = document.get("profiles", document)
    names = list(profiles) if args.profile == "all" and args.action == "refresh" else [args.profile]
    for name in names:
        profile = profiles[name]
        cache_path = args.profiles.parent / "cache" / f"{name}.json"
        if args.action == "refresh":
            fetch_metadata(profile, cache_path)
            print(f"Refreshed {name}", file=sys.stderr)
        else:
            Gateway(profile, cache_path).run()


if __name__ == "__main__":
    main()
