# -*- coding: utf-8 -*-
"""The report sender works without the Telegram connector, until it sends.

Two claims, and the second is the one that was broken.

**Everything except sending is independent of LazyTools.** Importing the
module, resolving which report is the latest, and ``--dry-run`` must all work
in a bare install. That is checked in a subprocess with the connector made
unimportable, because checking it in this process would prove only that the
module had already been imported successfully by something else.

**Asking to send without the extra says what to install.** Not a bare
``ModuleNotFoundError`` from somewhere inside the file, but a message naming
``pip install -e ".[telegram]"`` and the Python floor.

Nothing here patches ``sys.modules`` or ``sys.path`` for the whole session:
the isolation is a separate process with its own ``-P`` clean path, so a test
cannot leave the interpreter in a state the next one depends on.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "send_telegram_report.py"


def _run_isolated(body: str, *, block_lazytools: bool) -> subprocess.CompletedProcess:
    """Run a snippet in a fresh interpreter, optionally with LazyTools hidden.

    Hiding is done with a meta path finder that refuses exactly one top-level
    name, installed inside the child. It is the honest way to reproduce "the
    extra is not installed" on a machine where it is.
    """
    blocker = textwrap.dedent(
        """
        import sys
        class _NoLazyTools:
            def find_spec(self, name, path=None, target=None):
                if name == "lazytools" or name.startswith("lazytools."):
                    raise ModuleNotFoundError(f"No module named {name!r}", name=name)
                return None
        sys.meta_path.insert(0, _NoLazyTools())
        """
    ) if block_lazytools else ""
    source = f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n{blocker}\n{textwrap.dedent(body)}"
    return subprocess.run([sys.executable, "-c", source], capture_output=True, text=True,
                          cwd=str(ROOT))


@pytest.fixture()
def reports(tmp_path: Path) -> Path:
    directory = tmp_path / "dalio_v2"
    directory.mkdir()
    for name in ("dalio_v2_2026-08-10.html", "dalio_v2_2026-08-12.html"):
        (directory / name).write_text("<html>report</html>", encoding="utf-8")
    # mtime decides which is latest, and the two writes above can land in the
    # same clock tick on Windows.
    import os
    os.utime(directory / "dalio_v2_2026-08-10.html", (1_700_000_000, 1_700_000_000))
    os.utime(directory / "dalio_v2_2026-08-12.html", (1_800_000_000, 1_800_000_000))
    return directory


class TestWithoutTheExtra:
    def test_the_module_imports(self):
        result = _run_isolated("import send_telegram_report; print('IMPORTED')",
                               block_lazytools=True)
        assert "IMPORTED" in result.stdout, result.stderr
        assert result.returncode == 0

    def test_importing_it_does_not_pull_lazytools_in(self):
        result = _run_isolated(
            "import send_telegram_report, sys\n"
            "print('LOADED:' + ','.join(n for n in sys.modules if n.startswith('lazytools')))",
            block_lazytools=False)
        assert "LOADED:" in result.stdout, result.stderr
        assert result.stdout.strip().endswith("LOADED:"), "importing the module pulled LazyTools in"

    def test_dry_run_resolves_the_latest_report(self, reports: Path):
        result = _run_isolated(
            "import send_telegram_report as m, sys\n"
            f"sys.argv = ['send_telegram_report.py', '--report-dir', {str(reports)!r}, '--dry-run']\n"
            "raise SystemExit(m.main())",
            block_lazytools=True)
        assert result.returncode == 0, result.stderr
        assert "dalio_v2_2026-08-12.html" in result.stdout
        assert "Would send" in result.stdout

    def test_asking_to_send_says_what_to_install(self, reports: Path):
        result = _run_isolated(
            "import send_telegram_report as m\n"
            f"m.send_report_document(__import__('pathlib').Path({str(reports / 'dalio_v2_2026-08-12.html')!r}),"
            " token='t', chat_id='c', caption='x')",
            block_lazytools=True)
        assert result.returncode != 0
        assert "ModuleNotFoundError" in result.stderr
        assert '.[telegram]' in result.stderr
        assert "3.11" in result.stderr

    def test_a_broken_lazytools_is_not_reported_as_a_missing_extra(self):
        """An installed connector that raises on import is a different fault,
        and must not be dressed up as advice to install something."""
        result = _run_isolated(
            "import sys, types\n"
            "pkg = types.ModuleType('lazytools'); pkg.__path__ = []\n"
            "conn = types.ModuleType('lazytools.connectors'); conn.__path__ = []\n"
            "sys.modules['lazytools'] = pkg; sys.modules['lazytools.connectors'] = conn\n"
            "class _Boom:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'lazytools.connectors.telegram':\n"
            "            raise ModuleNotFoundError(\"No module named 'httpx'\", name='httpx')\n"
            "        return None\n"
            "sys.meta_path.insert(0, _Boom())\n"
            "import send_telegram_report as m\n"
            "m._telegram_client_class()",
            block_lazytools=False)
        assert result.returncode != 0
        assert "httpx" in result.stderr
        assert '.[telegram]' not in result.stderr


class TestReportSelection:
    def test_the_newest_file_wins(self, reports: Path):
        sys.path.insert(0, str(ROOT))
        try:
            import send_telegram_report as m

            assert m._latest_report(reports).name == "dalio_v2_2026-08-12.html"
        finally:
            sys.path.remove(str(ROOT))

    def test_an_empty_directory_says_what_to_run(self, tmp_path: Path):
        sys.path.insert(0, str(ROOT))
        try:
            import send_telegram_report as m

            with pytest.raises(FileNotFoundError, match="run_dalio_v2.py"):
                m._latest_report(tmp_path)
        finally:
            sys.path.remove(str(ROOT))


def test_the_script_declares_the_extra_it_asks_people_to_install():
    """The message tells the reader to install `.[telegram]`; the extra has to
    exist, or the advice sends them nowhere.

    Skipped below 3.11, where tomllib is not in the standard library and this
    repository has no TOML parser among its dependencies. The same assertion
    runs on 3.11 and 3.12 in the same CI, so the claim is still checked --
    it is a fact about a file, not about the interpreter reading it.
    """
    tomllib = pytest.importorskip("tomllib")

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    assert "telegram" in extras
    assert any("LazyTools" in item for item in extras["telegram"])
    # And it must not leak into what everyone installs.
    assert not any("lazytool" in item.lower() for item in data["project"]["dependencies"])
