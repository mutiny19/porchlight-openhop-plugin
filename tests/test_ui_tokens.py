"""The page's API-token handling (Connect, Remove), run in node against a fake openHop. The token has
administrator-level access, so the page must never forget one openHop may still accept. Skipped without node."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node not installed")


def block() -> str:
    page = (ROOT / "plugin" / "ui" / "index.html").read_text()
    a, b = page.index("// <token-lifecycle>"), page.index("// </token-lifecycle>")
    return page[a:b]


# A fake openHop: settings, tokens and the plugin runtime, with failures the scenario switches on.
HARNESS = r"""
const S = JSON.parse(process.argv[process.argv.length - 1]);
const state = { settings: S.settings, tokens: new Set(S.tokens), nextId: 100, calls: [], companion: !!S.companion };
const PLUGIN = "porchlight";
let cfg = null;
globalThis.setTimeout = (f) => Promise.resolve().then(f);
const fail = (status, msg) => Object.assign(new Error(msg), { status });
async function api(path, opts = {}) {
  const method = opts.method || "GET";
  state.calls.push(`${method} ${path}`);
  if (path.startsWith("/api/plugins/settings")) {
    if (method === "GET") return { config: JSON.parse(JSON.stringify(state.settings)) };
    if (S.saveFails) throw fail(500, "settings write failed");
    state.settings = JSON.parse(opts.body).config; return {};
  }
  if (path === "/api/auth/tokens" && method === "POST") {
    const id = state.nextId++; state.tokens.add(id); return { token: `tok${id}`, token_id: id };
  }
  if (path.startsWith("/api/auth/tokens/") && method === "DELETE") {
    const id = Number(decodeURIComponent(path.split("/").pop()));
    if (S.deleteFails) throw fail(500, "Internal Server Error");
    if (!state.tokens.has(id)) throw fail(404, "Token not found");
    state.tokens.delete(id); return {};
  }
  if (path.startsWith("/api/plugins/runtime")) return { runtime: { porch: S.porch } };
  if (path === "/api/identities") return { configured_companions: state.companion ? [{ name: "porchlight" }] : [] };
  if (path.startsWith("/api/delete_identity") && method === "DELETE") {
    if (S.companionStuck) throw fail(500, "busy");
    state.companion = false; return {};
  }
  throw fail(404, "no route " + path);
}
//BLOCK//
(async () => {
  let error = null;
  const note = { textContent: "" };
  try {
    if (S.run === "connect") await connect();
    else await finishRemove(S.requestId, note);
  } catch (e) { error = e.message; }
  console.log(JSON.stringify({ error, note: note.textContent, settings: state.settings, tokens: [...state.tokens], companion: state.companion }));
})();
"""


def run(**scenario) -> dict:
    scenario.setdefault("settings", {})
    scenario.setdefault("tokens", [])
    js = HARNESS.replace("//BLOCK//", block())
    out = subprocess.run([NODE, "-e", js, json.dumps(scenario)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


DONE = {"done_request": "r1", "steps": [{"text": "Companion 'Porchlight' removed from openHop", "ok": True}]}


def test_remove_revokes_the_token_then_forgets_it():
    r = run(run="remove", requestId="r1", porch=DONE, settings={"api_token": "tok7", "api_token_id": 7}, tokens=[7])
    assert r["error"] is None and r["note"].startswith("Removed")
    assert r["tokens"] == [] and r["settings"]["api_token"] == "" and r["settings"]["api_token_id"] is None


def test_a_failed_revoke_keeps_the_token_and_says_so():
    r = run(run="remove", requestId="r1", porch=DONE, settings={"api_token": "tok7", "api_token_id": 7}, tokens=[7],
            deleteFails=True)
    assert r["error"] and "couldn't revoke" in r["error"] and not r["note"].startswith("Removed")
    assert r["settings"] == {"api_token": "tok7", "api_token_id": 7}  # enough to retry
    assert r["tokens"] == [7]


def test_remove_retried_after_the_token_was_already_revoked():
    # An earlier Remove revoked the token but couldn't save the settings: openHop says 404, which counts as revoked.
    r = run(run="remove", requestId="r1", porch=DONE, settings={"api_token": "tok7", "api_token_id": 7}, tokens=[])
    assert r["error"] is None and r["settings"]["api_token_id"] is None


def test_remove_with_no_companion_finishes():
    porch = {"done_request": "r1", "steps": [{"text": "No companion 'Porchlight' in openHop: nothing to remove", "ok": True}]}
    r = run(run="remove", requestId="r1", porch=porch, settings={"api_token": "tok7", "api_token_id": 7}, tokens=[7])
    assert r["error"] is None and r["tokens"] == []


def test_a_failed_companion_remove_keeps_the_token():
    porch = {"done_request": "r1", "steps": [{"text": "Couldn't remove the companion: busy", "ok": False}]}
    r = run(run="remove", requestId="r1", porch=porch, settings={"api_token": "tok7", "api_token_id": 7}, tokens=[7])
    assert "busy" in r["error"] and r["tokens"] == [7] and r["settings"]["api_token_id"] == 7


def test_connect_saves_the_new_token():
    r = run(run="connect")
    assert r["error"] is None and r["settings"] == {"api_token": "tok100", "api_token_id": 100}
    assert r["tokens"] == [100]


def test_connect_revokes_the_new_token_when_the_settings_dont_save():
    r = run(run="connect", saveFails=True)
    assert r["error"] == "settings write failed"
    assert r["tokens"] == []  # nothing left active that the settings don't know about


REFUSED = {"done_request": "r1", "steps": [{"text": "openHop refused Porchlight's API token, so the page removes the companion",
                                            "ok": False, "token_refused": True}]}


def test_remove_finishes_when_the_token_was_revoked_outside_porchlight():
    # The backend can't reach openHop any more, so the page deletes the companion with the owner's sign-in.
    r = run(run="remove", requestId="r1", porch=REFUSED, settings={"api_token": "tok7", "api_token_id": 7}, tokens=[],
            companion=True)
    assert r["error"] is None and r["note"].startswith("Removed")
    assert r["companion"] is False and r["settings"]["api_token_id"] is None


def test_remove_says_so_when_the_page_cannot_delete_the_companion_either():
    r = run(run="remove", requestId="r1", porch=REFUSED, settings={"api_token": "tok7", "api_token_id": 7}, tokens=[],
            companion=True, companionStuck=True)
    assert "Press Remove again" in r["error"] and r["companion"] is True
    assert r["settings"]["api_token_id"] == 7
