"""Run inside Hermes as its runtime user; exercises actual tool dispatch."""
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import quote

from model_tools import get_tool_definitions, handle_function_call
from tools.browser_tool_session import _run_browser_command
from tools.browser_use_cli import _backend_cache_key, _base_subprocess_env, _find_cli
from tools.browser_supervisor import SUPERVISOR_REGISTRY

session = "codex-local-smoke"
task = "codex-local-smoke"
definitions = get_tool_definitions(
    enabled_toolsets=["hermes-api-server"], quiet_mode=True,
    skip_tool_search_assembly=True,
)
names = [d.get("name", d.get("function", {}).get("name")) for d in definitions]
assert "browser_exec" in names, "browser_exec missing from API toolset"

html = '''<!doctype html><title>Hermes browser regression</title>
<h1 id="rendered"></h1><input id="name"><button id="go">Apply</button>
<p id="result"></p><iframe srcdoc="<p>IFRAME_OK</p>"></iframe>
<script>document.querySelector('#rendered').textContent='JS_RENDER_OK';
document.querySelector('#go').onclick=()=>document.querySelector('#result').textContent=
'CLICK_OK:'+document.querySelector('#name').value;</script>'''
url = "data:text/html," + quote(html)
code = f'''# Verify local JavaScript, form interaction, iframe and screenshot
new_tab({url!r})
wait_for_load()
assert js("document.querySelector('#rendered').textContent") == 'JS_RENDER_OK'
fill_input('#name', 'Murray')
box = js("JSON.stringify(document.querySelector('#go').getBoundingClientRect().toJSON())")
import json
box = json.loads(box)
click_at_xy(box['x'] + box['width']/2, box['y'] + box['height']/2)
assert js("document.querySelector('#result').textContent") == 'CLICK_OK:Murray'
assert js("document.querySelector('iframe').contentDocument.body.innerText.trim()") == 'IFRAME_OK'
print(capture_screenshot())
print('BROWSER_EXEC_LOCAL_OK')
'''
try:
    raw = handle_function_call(
        "browser_exec", {"code": code, "session": session, "timeout_s": 60},
        task_id=task, enabled_toolsets=["hermes-api-server"],
    )
    result = json.loads(raw) if isinstance(raw, str) else raw
    # Text-only models return JSON; multimodal results carry it in a text block.
    if isinstance(result, dict) and "content" in result:
        result = json.loads(next(c["text"] for c in result["content"] if c.get("type") == "text"))
    assert result.get("success"), result
    assert "BROWSER_EXEC_LOCAL_OK" in result.get("output", ""), result
    shot = result.get("screenshot_path")
    assert shot and Path(shot).stat().st_size > 100, result
    print(json.dumps({"success": True, "toolset": "hermes-api-server",
                      "checks": ["JS", "fill", "click", "iframe", "screenshot"],
                      "screenshot": shot, "output": result["output"]}, ensure_ascii=False))
finally:
    SUPERVISOR_REGISTRY.stop(task)
    env = _base_subprocess_env()
    env["BU_NAME"] = session
    subprocess.run(_find_cli() + ["--reload"], env=env, capture_output=True,
                   text=True, timeout=20, check=True)
    _run_browser_command(_backend_cache_key(task, session), "close")
