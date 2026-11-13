"""relay_option on Windows path shapes, run against a virtual ntpath file system.

Nothing here needs Windows: relay_option's `os` is swapped for a namespace that
uses ntpath and a dict-backed file system, so the Windows branch (USERPROFILE
home, ';' list separator, backslash paths, mixed slashes) runs on any host.
Shapes were measured on 5090W (Windows, claude.exe 2.1.323, standalone Python
3.12): its inherited BOOTSTRAP_PYTHON uses forward slashes.
"""

import ntpath
import types

import pytest

import relay_option
from relay_option import RelayError

HOME = "C:\\Users\\u"
CACHE = HOME + "\\.claude\\plugins\\cache\\plugins-kit\\llm-kit"


def fake_nt(monkeypatch, files, dirs=()):
    """Make relay_option see an nt host whose only files/dirs are the given ones."""
    files = {ntpath.normcase(ntpath.normpath(f)) for f in files}
    dirset = {ntpath.normcase(ntpath.normpath(d)) for d in dirs}
    for f in list(files):
        d = ntpath.dirname(f)
        while d and d not in dirset and d != ntpath.dirname(d):
            dirset.add(d)
            d = ntpath.dirname(d)

    def norm(p):
        return ntpath.normcase(ntpath.normpath(p))

    def listdir(path):
        base = norm(path)
        return [ntpath.basename(d) for d in dirset if ntpath.dirname(d) == base]

    path = types.SimpleNamespace(**{n: getattr(ntpath, n) for n in (
        "join", "dirname", "basename", "isabs", "relpath", "normpath", "normcase", "expanduser")})
    path.isfile = lambda p: norm(p) in files
    path.isdir = lambda p: norm(p) in dirset
    monkeypatch.setattr(relay_option, "os", types.SimpleNamespace(
        name="nt", path=path, pathsep=";", listdir=listdir))


def llm_kit_files(root):
    return [root + "\\channel\\relay_channel.py", root + "\\channel\\mcp.json"]


def test_the_cache_under_userprofile_is_found_and_the_newest_version_wins(monkeypatch):
    fake_nt(monkeypatch, llm_kit_files(CACHE + "\\0.9.0") + llm_kit_files(CACHE + "\\0.10.0"))
    channel = relay_option.resolve_channel({"USERPROFILE": HOME})
    assert channel.script == CACHE + "\\0.10.0\\channel\\relay_channel.py"
    assert channel.mcp_config == CACHE + "\\0.10.0\\channel\\mcp.json"


def test_claudx_dirs_split_on_semicolon_and_match_the_plugin_name_ignoring_case(monkeypatch):
    work = "C:\\Dev\\plugins-kit\\plugins\\LLM-Kit"
    fake_nt(monkeypatch, llm_kit_files(work))
    env = {"CLAUDE_PLUGIN_TEST": "1", "USERPROFILE": HOME,
           "CLAUDE_PLUGIN_TEST_DIRS": "C:\\Dev\\plugins-kit\\plugins\\p4-kit;" + work + "\\"}
    channel = relay_option.resolve_channel(env)
    assert channel.script == work + "\\channel\\relay_channel.py"


def test_a_pinned_channel_script_with_forward_slashes_gives_one_separator_style(monkeypatch):
    fake_nt(monkeypatch, llm_kit_files("C:\\kit"))
    channel = relay_option.resolve_channel({"LLM_KIT_CHANNEL_SCRIPT": "C:/kit/channel/relay_channel.py"})
    assert channel.script == "C:\\kit\\channel\\relay_channel.py"
    assert channel.mcp_config == "C:\\kit\\channel\\mcp.json"


def test_a_forward_slash_bootstrap_python_is_passed_on_normalised(monkeypatch):
    fake_nt(monkeypatch, ["C:\\Users\\u\\.local\\share\\python-standalone\\python\\python.exe"])
    env = {"BOOTSTRAP_PYTHON": "C:/Users/u/.local/share/python-standalone/python/python.exe"}
    assert relay_option.resolve_python(env) == "C:\\Users\\u\\.local\\share\\python-standalone\\python\\python.exe"


def test_a_relative_or_missing_bootstrap_python_is_still_refused_on_nt(monkeypatch):
    fake_nt(monkeypatch, [])
    for bad in ("python.exe", "C:/nowhere/python.exe"):
        with pytest.raises(RelayError, match="BOOTSTRAP_PYTHON"):
            relay_option.resolve_python({"BOOTSTRAP_PYTHON": bad})
