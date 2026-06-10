"""Tests for claude-explorer's HTTP server security guards.

Covers the graduation-gate properties recorded in the skill's SKILL.md:
Host-header allowlist (DNS-rebinding guard), path-traversal guard on /file,
and per-server (non-accumulating) allowed roots.

The module is imported by file path (spec_from_file_location) so the test does
not depend on the plugin being on sys.path. PyYAML is optional for the module
(regex fallback), so no skip is needed.
"""

import http.client
import importlib.util
import threading
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "prototypes" / "skills" / "claude-explorer" / "scripts"
    / "claude_explorer.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("claude_explorer_mod", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


explorer = _load_module()


@pytest.fixture
def server(tmp_path):
    """A running server whose project root is a tmp dir (port 0 = ephemeral)."""
    (tmp_path / "hello.md").write_text("# hi\n", encoding="utf-8")
    httpd = explorer.make_server(tmp_path, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(httpd, path, host=None):
    port = httpd.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {"Host": host} if host is not None else {}
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


class TestHostHeaderGuard:
    def test_default_host_is_allowed(self, server):
        httpd, _ = server
        status, body = _get(httpd, "/")  # http.client sends Host: 127.0.0.1:<port>
        assert status == 200
        assert b"claude-explorer" in body

    def test_localhost_with_port_is_allowed(self, server):
        httpd, _ = server
        port = httpd.server_address[1]
        status, _ = _get(httpd, "/", host=f"localhost:{port}")
        assert status == 200

    def test_foreign_host_is_rejected(self, server):
        httpd, _ = server
        status, body = _get(httpd, "/", host="evil.example:80")
        assert status == 403
        assert b"forbidden host" in body

    def test_rebound_hostname_on_right_port_is_rejected(self, server):
        httpd, _ = server
        port = httpd.server_address[1]
        status, _ = _get(httpd, "/", host=f"evil.example:{port}")
        assert status == 403

    def test_missing_host_header_is_rejected(self, server):
        httpd, _ = server
        status, _ = _get(httpd, "/", host="")
        assert status == 403


class TestFileEndpointGuard:
    def test_file_under_allowed_root_is_served(self, server):
        httpd, root = server
        status, body = _get(httpd, f"/file?path={root / 'hello.md'}")
        assert status == 200
        assert body == b"# hi\n"

    def test_file_outside_allowed_roots_is_forbidden(self, server):
        httpd, _ = server
        status, _ = _get(httpd, "/file?path=/etc/hosts")
        assert status == 403

    def test_traversal_out_of_root_is_forbidden(self, server):
        httpd, root = server
        status, _ = _get(httpd, f"/file?path={root}/../../etc/hosts")
        assert status == 403


class TestAllowedRootsAreInstanceState:
    def test_roots_do_not_accumulate_across_servers(self, tmp_path):
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        root_a.mkdir()
        root_b.mkdir()
        server_a = explorer.make_server(root_a, port=0)
        server_b = explorer.make_server(root_b, port=0)
        try:
            roots_a = server_a.RequestHandlerClass.allowed_roots
            roots_b = server_b.RequestHandlerClass.allowed_roots
            assert root_a.resolve() in roots_a
            assert root_b.resolve() not in roots_a
            assert root_b.resolve() in roots_b
            assert root_a.resolve() not in roots_b
            # base class stays clean
            assert explorer.Handler.allowed_roots == ()
        finally:
            server_a.server_close()
            server_b.server_close()
