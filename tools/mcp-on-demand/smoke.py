"""Focused lifecycle and transparent-protocol smoke check, using no real accounts."""

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from gateway import TemporaryDirectory


def fake_backend(trace_path):
    trace_lock = threading.Lock()
    output_lock = threading.Lock()
    waiting = {}
    threads = []

    def trace(event):
        with trace_lock, open(trace_path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "pid": os.getpid()}) + "\n")

    def send(message):
        with output_lock:
            print(json.dumps(message), flush=True)

    def tool(message):
        action = message.get("params", {}).get("arguments", {}).get("action", "normal")
        trace("call-" + action)
        if action == "crash":
            os._exit(3)
        if action == "slow":
            time.sleep(0.35)
        if action == "server-request":
            event = threading.Event()
            waiting["server-1"] = event
            send({"jsonrpc": "2.0", "id": "server-1", "method": "roots/list", "params": {}})
            assert event.wait(2), "Server request was not forwarded"
        if action == "pin":
            Path(os.environ["MCP_IDLE_STATE_DIR"], "no-idle").touch()
        if action == "unpin":
            Path(os.environ["MCP_IDLE_STATE_DIR"], "no-idle").unlink(missing_ok=True)
        send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": "test", "progress": 1}})
        send({"jsonrpc": "2.0", "id": message["id"], "result": {
            "content": [{"type": "text", "text": str(os.getpid())}],
            "_meta": {"opaque": "preserved"}, "structuredContent": {"pid": os.getpid(), "text": "日本語と中文と🛠"},
        }})
        trace("done-" + action)

    trace("start")
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fixture", "version": "1"},
                      "instructions": "日本語と中文と🛠", "_meta": {"opaque": [1, 2]}}
        elif method == "tools/list":
            result = {"tools": [{"name": "test", "description": "unchanged", "inputSchema": {"type": "object"},
                                  "annotations": {"readOnlyHint": False}, "_meta": {"test": True}}]}
        elif method == "prompts/list":
            result = {"prompts": []}
            if not message.get("params", {}).get("cursor"):
                result["nextCursor"] = "page-2"
        elif method in ("resources/list", "resources/templates/list"):
            send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Unsupported"}})
            continue
        elif method == "tools/call":
            thread = threading.Thread(target=tool, args=(message,))
            thread.start()
            threads.append(thread)
            continue
        elif method is None:
            if message.get("id") in waiting:
                waiting[message["id"]].set()
            continue
        elif method == "notifications/cancelled":
            trace("cancel")
            continue
        else:
            continue
        send({"jsonrpc": "2.0", "id": message["id"], "result": result})
    for thread in threads:
        thread.join()
    trace("exit")


class Client:
    def __init__(self, profile, profiles):
        self.process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("gateway.py")),
                                         "serve", profile, "--profiles", str(profiles)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.messages = queue.Queue()
        self.counter = 0
        self.notifications = []
        threading.Thread(target=lambda: [self.messages.put(json.loads(line)) for line in self.process.stdout], daemon=True).start()
        initial = self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                               "clientInfo": {"name": "smoke", "version": "1"}})
        assert initial["result"]["protocolVersion"] == "2025-03-26"
        assert initial["result"]["_meta"] == {"opaque": [1, 2]}
        assert initial["result"]["instructions"] == "日本語と中文と🛠"
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def begin(self, method, params=None):
        self.counter += 1
        self.send({"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params or {}})
        return self.counter

    def receive(self, expected):
        while True:
            message = self.messages.get(timeout=8)
            if "method" in message:
                if "id" in message:
                    self.send({"jsonrpc": "2.0", "id": message["id"], "result": {"roots": []}})
                else:
                    self.notifications.append(message)
                continue
            assert message.get("id") == expected, message
            return message

    def request(self, method, params=None):
        return self.receive(self.begin(method, params))

    def call(self, action="normal"):
        return self.request("tools/call", {"name": "test", "arguments": {"action": action}})

    def close(self):
        self.process.stdin.close()
        assert self.process.wait(timeout=8) == 0, self.process.stderr.read()


def run_smoke():
    with TemporaryDirectory(prefix="mcp-gateway-smoke-") as temporary:
        root = Path(temporary)
        trace = root / "trace.jsonl"
        profiles = root / "profiles.json"
        profile = {"command": [sys.executable, str(Path(__file__).resolve()), "--fake", str(trace)],
                   "idle_seconds": 0, "watch_files": [str(Path(__file__).resolve())]}
        profiles.write_text(json.dumps({"percall": profile, "idle": {**profile, "idle_seconds": 0.2}}), encoding="utf-8")
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("gateway.py")), "refresh", "all",
                                 "--profiles", str(profiles)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

        def events():
            return [json.loads(line) for line in trace.read_text().splitlines()]

        def count(kind):
            return sum(row["event"] == kind for row in events())

        starts = count("start")
        client = Client("percall", profiles)
        listed = client.request("tools/list")
        assert listed["result"]["tools"][0]["_meta"] == {"test": True}
        assert client.request("resources/list")["error"]["code"] == -32601
        assert client.request("prompts/list")["result"]["nextCursor"] == "page-2"
        assert "nextCursor" not in client.request("prompts/list", {"cursor": "page-2"})["result"]
        assert count("start") == starts, "Warm discovery spawned a backend"
        first = client.call()
        assert first["result"]["structuredContent"]["text"] == "日本語と中文と🛠"
        second = client.call("server-request")
        assert first["result"]["structuredContent"]["pid"] != second["result"]["structuredContent"]["pid"]
        assert second["result"]["_meta"] == {"opaque": "preserved"}
        assert client.notifications, "Worker notifications were lost"
        before = count("call-crash")
        assert "error" in client.call("crash")
        assert count("call-crash") == before + 1, "Failed call was retried"
        assert "result" in client.call(), "Failed worker blocked the next explicit call"
        client.call("pin")
        pinned_starts = count("start")
        time.sleep(0.25)
        client.call("unpin")
        assert count("start") == pinned_starts, "no-idle state protection was ignored"
        client.close()

        client = Client("idle", profiles)
        first = client.call()["result"]["structuredContent"]["pid"]
        second = client.call()["result"]["structuredContent"]["pid"]
        assert first == second, "Idle session did not reuse a worker"
        time.sleep(0.5)
        third = client.call()["result"]["structuredContent"]["pid"]
        assert first != third, "Idle worker did not stop"
        request_id = client.begin("tools/call", {"name": "test", "arguments": {"action": "slow"}})
        time.sleep(0.08)
        client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}})
        slow = client.receive(request_id)
        assert slow["result"]["structuredContent"]["pid"] == third, "Active call was stopped by idle or cancellation"
        assert count("cancel") == 1
        client.begin("tools/call", {"name": "test", "arguments": {"action": "slow"}})
        time.sleep(0.08)
        client.close()
        assert count("done-slow") == 2, "Client EOF interrupted an active call"
        assert count("exit") == count("start") - count("call-crash"), "A worker was not cleaned up"
        print("PASS: cached discovery; exact metadata/pages/errors; per-call/idle lifecycle; server requests; cancellation; no-idle; no retry; active-call EOF cleanup")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fake":
        fake_backend(sys.argv[2])
    else:
        run_smoke()
