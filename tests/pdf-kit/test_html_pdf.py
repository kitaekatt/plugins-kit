"""Tests for pdf-kit's html-pdf converter pure helpers.

The module is imported by file path so the test does not depend on the plugin
being on sys.path. The conversion itself needs Playwright + Chromium and is not
unit-tested here; these tests cover the pure, OS-/browser-free logic:
output-path defaulting and Windows default-browser command parsing.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "pdf-kit" / "skills" / "html-pdf" / "scripts" / "html_to_pdf.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("pdf_html_to_pdf", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


html_to_pdf = _load_module()


class TestDefaultOutputPath:
    def test_swaps_html_suffix_for_pdf(self):
        assert html_to_pdf.default_output_path("a/b/page.html") == Path("a/b/page.pdf")

    def test_handles_uppercase_suffix(self):
        assert html_to_pdf.default_output_path("X.HTML") == Path("X.pdf")

    def test_no_suffix_gets_pdf(self):
        assert html_to_pdf.default_output_path("report") == Path("report.pdf")


class TestParseScale:
    @pytest.mark.parametrize("value,expected", [
        ("0.8", 0.8),
        ("80%", 0.8),
        ("80", 0.8),
        ("1.0", 1.0),
        ("100%", 1.0),
        ("100", 1.0),
        ("150%", 1.5),
        ("1.5", 1.5),
        ("200%", 2.0),
    ])
    def test_accepts_fraction_and_percent(self, value, expected):
        assert html_to_pdf.parse_scale(value) == pytest.approx(expected)

    def test_clamps_below_minimum(self):
        assert html_to_pdf.parse_scale("5%") == 0.1   # 0.05 -> clamped up

    def test_clamps_above_maximum(self):
        assert html_to_pdf.parse_scale("300%") == 2.0  # 3.0 -> clamped down

    def test_invalid_raises(self):
        with pytest.raises(SystemExit):
            html_to_pdf.parse_scale("abc")


class TestExeFromCommand:
    def test_quoted_path_with_args(self):
        cmd = r'"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --single-argument %1'
        assert (
            html_to_pdf.exe_from_command(cmd)
            == r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        )

    def test_unquoted_path_no_spaces(self):
        cmd = r"C:\Windows\system32\rundll32.exe shell,Open %1"
        assert html_to_pdf.exe_from_command(cmd) == r"C:\Windows\system32\rundll32.exe"

    def test_unterminated_quote_returns_none(self):
        assert html_to_pdf.exe_from_command('"C:\\broken\\path %1') is None

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_empty_returns_none(self, value):
        assert html_to_pdf.exe_from_command(value) is None


def test_main_wraps_convert_errors(monkeypatch, tmp_path):
    source = tmp_path / "page.html"
    source.write_text("<html />", encoding="utf-8")
    monkeypatch.setattr(html_to_pdf, "convert", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(sys, "argv", ["html_to_pdf.py", str(source), "--no-open"])

    with pytest.raises(SystemExit, match=r"ERROR RuntimeError: boom"):
        html_to_pdf.main()


def test_equal_input_output_fails_before_convert(monkeypatch, tmp_path):
    source = tmp_path / "page.html"
    source.write_text("<html />", encoding="utf-8")
    called = []
    monkeypatch.setattr(html_to_pdf, "convert", lambda *args, **kwargs: called.append(True) or tmp_path / "out.pdf")
    monkeypatch.setattr(sys, "argv", ["html_to_pdf.py", str(source), str(source)])

    with pytest.raises(SystemExit, match="ERROR"):
        html_to_pdf.main()
    assert called == []


def test_main_wires_no_open_and_scale(monkeypatch, tmp_path, capsys):
    source = tmp_path / "page.html"
    source.write_text("<html />", encoding="utf-8")
    calls = []
    monkeypatch.setattr(html_to_pdf, "convert", lambda *args, **kwargs: calls.append((args, kwargs)) or tmp_path / "page.pdf")
    monkeypatch.setattr(sys, "argv", ["html_to_pdf.py", str(source), "--scale", "80%", "--no-open"])

    html_to_pdf.main()

    assert calls[0][1]["scale"] == pytest.approx(0.8)
    assert "OPENED" not in capsys.readouterr().out


def test_a4_pdf_kwargs_do_not_prefer_css(monkeypatch, tmp_path):
    class Page:
        def goto(self, *args, **kwargs): pass
        def pdf(self, **kwargs): self.kwargs = kwargs

    class Browser:
        def new_page(self): return page
        def close(self): pass

    class Chromium:
        def launch(self): return Browser()

    class Playwright:
        chromium = Chromium()

    page = Page()
    class Manager:
        def __enter__(self): return Playwright()
        def __exit__(self, *args): pass

    monkeypatch.setitem(sys.modules, "playwright", type("M", (), {})())
    sync = type("M", (), {"sync_playwright": lambda: Manager()})
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync)
    source = tmp_path / "page.html"
    source.write_text("<html />", encoding="utf-8")

    html_to_pdf.convert(source, tmp_path / "page.pdf", a4=True)

    assert page.kwargs["format"] == "A4"
    assert not page.kwargs["prefer_css_page_size"]


def test_open_browser_falls_back_after_popen_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(html_to_pdf.sys, "platform", "win32")
    monkeypatch.setattr(html_to_pdf, "_windows_default_browser", lambda: "browser.exe")
    monkeypatch.setattr(html_to_pdf.subprocess, "Popen", lambda *args: (_ for _ in ()).throw(OSError("no")))
    opened = []
    monkeypatch.setattr(html_to_pdf.webbrowser, "open", lambda uri: opened.append(uri) or True)

    assert html_to_pdf.open_in_default_browser(tmp_path / "x.pdf")
    assert opened


def test_open_browser_returns_after_windows_popen(monkeypatch, tmp_path):
    monkeypatch.setattr(html_to_pdf.sys, "platform", "win32")
    monkeypatch.setattr(html_to_pdf, "_windows_default_browser", lambda: "browser.exe")
    calls = []
    monkeypatch.setattr(html_to_pdf.subprocess, "Popen", lambda argv: calls.append(argv))
    monkeypatch.setattr(html_to_pdf.webbrowser, "open", lambda uri: pytest.fail("fallback"))

    assert html_to_pdf.open_in_default_browser(tmp_path / "x.pdf")
    assert calls
