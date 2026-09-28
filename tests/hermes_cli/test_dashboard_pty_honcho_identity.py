"""Dashboard login identity must survive the profile PTY/stdio boundary."""

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

import pytest
from starlette.testclient import TestClient


@pytest.mark.linux_only
def test_ticket_identity_reaches_honcho_across_profile_children(monkeypatch, tmp_path):
    import hermes_cli.web_server as web
    import hermes_cli.web_server_chat as chat
    import hermes_cli.web_routers.chat_ws as routes
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket

    monkeypatch.setattr(web, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    monkeypatch.setattr(web.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(web.app.state, "bound_host", "127.0.0.1", raising=False)
    captured = []

    class Bridge:
        def read(self, timeout):
            return None

        def close(self):
            pass

    def spawn(argv, **kwargs):
        captured.append(kwargs["env"])
        return Bridge()

    async def pump(ws, bridge):
        await ws.send_text("spawned")
        await ws.close()

    def resolve(**kwargs):
        home = tmp_path / kwargs["profile"]
        home.mkdir(exist_ok=True)
        return ["unused"], str(home), {**os.environ, "HERMES_HOME": str(home),
                                     "HERMES_TUI_GATEWAY_URL": "ws://internal-without-user"}

    monkeypatch.setattr(chat, "_resolve_chat_argv", resolve)
    monkeypatch.setattr(chat.PtyBridge, "spawn", spawn)
    monkeypatch.setattr(routes, "_legacy_pump", pump)
    probe = '''
import json, os, sys
from tui_gateway import server
from plugins.memory.honcho.client import HonchoClientConfig
from plugins.memory.honcho.session_peers import SessionPeersMixin
server._stdio_is_rpc_channel = True
uid = server._transport_auth_user_id(None)
assert server._transport_auth_user_id(object()) is None
assert "HERMES_TUI_AUTH_IDENTITY" not in os.environ
server._start_agent_build = lambda *a, **kw: None
server._resolve_model = lambda: "test-model"
response = server.handle_request({"id": "1", "method": "session.create",
    "params": {"cols": 80, "source": "cli", "cwd": os.environ["HERMES_HOME"]}})
record = server._sessions[response["result"]["session_id"]]
assert record["auth_user_id"] == uid
assert server._ensure_session_db_row(record)
assert server._get_db().get_session(response["result"]["stored_session_id"])["user_id"] == uid
m = SessionPeersMixin()
m._config = HonchoClientConfig()
m._runtime_user_peer_name = uid
m._runtime_user_peer_name_alt = None
try:
    peer = m._resolve_user_peer_id("data")
except RuntimeError:
    peer = None
print(json.dumps({"user": uid, "peer": peer}), file=sys.__stdout__)
'''
    client = TestClient(web.app, base_url="http://127.0.0.1")
    for user, profile in [("murray", "architect"), ("alice", "coder"), ("murray", "architect")]:
        ticket = mint_ticket(user_id=user, provider="basic")
        query = urlencode({"ticket": ticket, "profile": profile, "user_id": "forged"})
        with client.websocket_connect("ws://127.0.0.1/api/pty?" + query) as ws:
            assert ws.receive_text() == "spawned"
        result = subprocess.run([sys.executable, "-c", probe], env=captured[-1],
                                cwd=Path(__file__).parents[2], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.splitlines()[-1]) == {"user": f"basic:{user}", "peer": f"basic-{user}"}
        assert "HERMES_TUI_GATEWAY_URL" not in captured[-1]
