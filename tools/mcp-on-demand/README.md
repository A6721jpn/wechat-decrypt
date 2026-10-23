# On-demand WeChat MCP

This optional gateway keeps the MCP connection available while stopping the
WeChat backend after each completed request. It uses only the Python standard
library and requires Python 3.10 or later. The existing CLI remains available.

The first discovery request briefly starts the backend to cache its tool and
resource definitions. Later discovery requests use that metadata until watched
source files change. Tool results and chat content are not cached by the gateway.
A small gateway process remains running for each MCP connection.

## Configure a source checkout

Complete the normal WeChat setup first. Keep this checkout at a stable path.
Store the following JSON outside the repository, for example at
`~/.codex/tools/mcp-on-demand/profiles.json`. Replace the example paths with
absolute paths for your machine. Forward slashes work on Windows too.

```json
{
  "wechat": {
    "command": ["C:/path/to/wechat-decrypt/.venv/Scripts/python.exe", "-u", "C:/path/to/wechat-decrypt/server.py"],
    "idle_seconds": 0,
    "startup_timeout": 30,
    "protocol_version": "2025-06-18",
    "watch_files": ["C:/path/to/wechat-decrypt/server.py", "C:/path/to/wechat-decrypt/config.py"]
  }
}
```

On macOS, use the checkout's `.venv/bin/python` and its absolute checkout path.
If your existing server registration sets a working directory, also copy it to
the profile's `cwd` field. Keep any existing profiles for other servers.

Back up the Codex configuration. In the existing `[mcp_servers.wechat]` section,
replace only `command` and `args` with the following values after substituting
your paths. Keep the server name, environment variables, permissions, and other
settings. Use a plain Python installation for the gateway to minimize idle
memory; the backend continues using its own virtual environment.

```toml
[mcp_servers.wechat]
command = 'C:/path/to/python.exe'
args = ['-u', 'C:/path/to/wechat-decrypt/tools/mcp-on-demand/gateway.py', 'serve', 'wechat', '--profiles', 'C:/path/to/profiles.json']
```

Restart Codex to reconnect existing chats. Running `setup.ps1` or `setup.sh`
registers the original direct backend again; reapply these two settings after
setup. To undo on-demand mode, restore the original `command` and `args` from
your backup. Do not remove and recreate the MCP registration to switch modes.

## Lifecycle

The backend exits only when no request, server callback, or subscription needs
it. Cancellation does not force-stop an active operation. An unknown operation
outcome is returned as an error and is never automatically retried. Closing
the MCP connection lets work already in progress finish before cleanup.

The gateway inherits the existing MCP environment. It neither extracts keys nor
changes access permissions. Metadata cache files live beside the profiles file;
temporary connection state is removed after a clean shutdown. Keep generated
profiles and caches outside Git along with all private WeChat data.

Windows lifecycle and protocol behavior have been checked. macOS execution is
supported by the script but has not been verified for this addition.

## Offline check

Run `python tools/mcp-on-demand/smoke.py` from the repository root. The check uses
a fake backend and requires neither a WeChat account nor decrypted chat data.
