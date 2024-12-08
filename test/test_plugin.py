import collections
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict
from unittest.mock import Mock, patch

import pytest
from mypy import api as mypy_api
from pylsp import _utils, uris
from pylsp.config.config import Config
from pylsp.workspace import Document, Workspace

from pylsp_mypy import plugin

# TODO using these file as a document is a bad idea as tests can break by adding new tests
DOC_URI = f"file:/{Path(__file__)}"
DOC_TYPE_ERR = """{}.append(3)
"""

# Mypy 1.7 changed <nothing> into "Never", so make this a regex to be compatible
# with multiple versions of mypy
TYPE_ERR_MSG_REGEX = (
    r'"Dict\[(?:(?:<nothing>)|(?:Never)), (?:(?:<nothing>)|(?:Never))\]" has no attribute "append"'
)

TEST_LINE = 'test_plugin.py:279:8:279:16: error: "Request" has no attribute "id"  [attr-defined]'
TEST_LINE_NOTE = (
    'test_plugin.py:124:1:129:77: note: Use "-> None" if function does not return a value'
)

windows_flag: Dict[str, int] = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}  # type: ignore
)


@pytest.fixture
def last_diagnostics_monkeypatch(monkeypatch):
    # gets called before every test altering last_diagnostics in order to reset it
    monkeypatch.setattr(plugin, "last_diagnostics", collections.defaultdict(list))
    return monkeypatch


@pytest.fixture
def workspace(tmpdir):
    """Return a workspace."""
    ws = Workspace(uris.from_fs_path(str(tmpdir)), Mock())
    ws._config = Config(ws.root_uri, {}, 0, {})
    return ws


class FakeConfig(object):
    def __init__(self, path):
        self._root_path = path

    def plugin_settings(self, plugin, document_path=None):
        return {}


def test_settings(tmpdir):
    config = Config(uris.from_fs_path(str(tmpdir)), {}, 0, {})
    settings = plugin.pylsp_settings(config)
    assert settings == {"plugins": {"pylsp_mypy": {}}}


def test_plugin(workspace, last_diagnostics_monkeypatch):
    doc = Document(DOC_URI, workspace, DOC_TYPE_ERR)
    plugin.pylsp_settings(workspace._config)
    diags = plugin.pylsp_lint(workspace._config, workspace, doc, is_saved=False)

    assert len(diags) == 1
    diag = diags[0]
    assert re.fullmatch(TYPE_ERR_MSG_REGEX, diag["message"])
    assert diag["range"]["start"] == {"line": 0, "character": 0}
    # Running mypy in 3.7 produces wrong error ends this can be removed when 3.7 reaches EOL
    if sys.version_info < (3, 8):
        assert diag["range"]["end"] == {"line": 0, "character": 1}
    else:
        assert diag["range"]["end"] == {"line": 0, "character": 9}
    assert diag["severity"] == 1
    assert diag["code"] == "attr-defined"


@pytest.mark.parametrize("doc_source", ["a = 1\nb\n", "a = 1\r\nb\r\n", "a = 1\rb\r"])
def test_handling_of_line_endings(workspace, last_diagnostics_monkeypatch, doc_source):
    # setup
    doc = Document(DOC_URI, workspace, doc_source)
    plugin.pylsp_settings(workspace._config)

    # run
    diags = plugin.pylsp_lint(workspace._config, workspace, doc, is_saved=False)

    # assert
    undefined_name_diags = list(filter(lambda diag: diag["code"] == "name-defined", diags))
    assert len(undefined_name_diags) == 1
    diag = undefined_name_diags[0]
    assert diag["message"] == 'Name "b" is not defined'
    assert diag["range"]["start"] == {"line": 1, "character": 0}
    assert diag["range"]["end"] == {"line": 1, "character": 1}


def test_parse_full_line(workspace):
    diag = plugin.parse_line(TEST_LINE)  # TODO parse a document here
    assert diag["message"] == '"Request" has no attribute "id"'
    assert diag["range"]["start"] == {"line": 278, "character": 7}
    assert diag["range"]["end"] == {"line": 278, "character": 16}
    assert diag["severity"] == 1
    assert diag["code"] == "attr-defined"


def test_parse_note_line(workspace):
    diag = plugin.parse_line(TEST_LINE_NOTE)
    assert diag["message"] == 'Use "-> None" if function does not return a value'
    assert diag["range"]["start"] == {"line": 123, "character": 0}
    assert diag["range"]["end"] == {"line": 128, "character": 77}
    assert diag["severity"] == 3
    assert diag["code"] is None


def test_multiple_workspaces(tmpdir, last_diagnostics_monkeypatch):
    DOC_SOURCE = """
def foo():
    return
    unreachable = 1
"""
    DOC_ERR_MSG = "Statement is unreachable"

    # Initialize two workspace folders.
    folder1 = tmpdir.mkdir("folder1")
    folder2 = tmpdir.mkdir("folder2")

    # Create configuration file for workspace folder 1.
    mypy_config = folder1.join("mypy.ini")
    mypy_config.write("[mypy]\nwarn_unreachable = True\ncheck_untyped_defs = True")

    ws1 = Workspace(uris.from_fs_path(str(folder1)), Mock())
    ws1._config = Config(ws1.root_uri, {}, 0, {})
    ws2 = Workspace(uris.from_fs_path(str(folder2)), Mock())
    ws2._config = Config(ws2.root_uri, {}, 0, {})

    # Initialize settings for both folders.
    plugin.pylsp_settings(ws1._config)
    plugin.pylsp_settings(ws2._config)

    # Test document in workspace 1 (uses mypy.ini configuration).
    doc1 = Document(DOC_URI, ws1, DOC_SOURCE)
    diags = plugin.pylsp_lint(ws1._config, ws1, doc1, is_saved=False)
    assert len(diags) == 1
    diag = diags[0]
    assert diag["message"] == DOC_ERR_MSG
    assert diag["code"] == "unreachable"

    # Test document in workspace 2 (without mypy.ini configuration)
    doc2 = Document(DOC_URI, ws2, DOC_SOURCE)
    diags = plugin.pylsp_lint(ws2._config, ws2, doc2, is_saved=False)
    assert len(diags) == 0


def test_apply_overrides():
    assert plugin.apply_overrides(["1", "2"], []) == []
    assert plugin.apply_overrides(["1", "2"], ["a"]) == ["a"]
    assert plugin.apply_overrides(["1", "2"], ["a", True]) == ["a", "1", "2"]
    assert plugin.apply_overrides(["1", "2"], [True, "a"]) == ["1", "2", "a"]
    assert plugin.apply_overrides(["1"], ["a", True, "b"]) == ["a", "1", "b"]


@pytest.mark.skipif(os.name == "nt", reason="Not working on Windows due to test design.")
def test_option_overrides(tmpdir, last_diagnostics_monkeypatch, workspace):
    import sys
    from stat import S_IRWXU
    from textwrap import dedent

    sentinel = tmpdir / "ran"

    source = dedent(
        """\
        #!{}
        import os, sys, pathlib
        pathlib.Path({!r}).touch()
        os.execv({!r}, sys.argv)
        """
    ).format(sys.executable, str(sentinel), sys.executable)

    wrapper = tmpdir / "bin/wrapper"
    wrapper.write(source, ensure=True)
    wrapper.chmod(S_IRWXU)

    overrides = ["--python-executable", wrapper.strpath, True]
    last_diagnostics_monkeypatch.setattr(
        FakeConfig,
        "plugin_settings",
        lambda _, p: {"overrides": overrides} if p == "pylsp_mypy" else {},
    )

    config = FakeConfig(uris.to_fs_path(workspace.root_uri))
    plugin.pylsp_settings(config)

    assert not sentinel.exists()

    diags = plugin.pylsp_lint(
        config=config,
        workspace=workspace,
        document=Document(DOC_URI, workspace, DOC_TYPE_ERR),
        is_saved=False,
    )
    assert len(diags) == 1
    assert sentinel.exists()


def test_option_overrides_dmypy(last_diagnostics_monkeypatch, workspace):
    overrides = ["--python-executable", "/tmp/fake", True]
    last_diagnostics_monkeypatch.setattr(
        FakeConfig,
        "plugin_settings",
        lambda _, p: (
            {
                "overrides": overrides,
                "dmypy": True,
                "live_mode": False,
            }
            if p == "pylsp_mypy"
            else {}
        ),
    )

    m = Mock(wraps=lambda a, **_: Mock(returncode=0, **{"stdout": ""}))
    last_diagnostics_monkeypatch.setattr(plugin.subprocess, "run", m)

    document = Document(DOC_URI, workspace, DOC_TYPE_ERR)

    config = FakeConfig(uris.to_fs_path(workspace.root_uri))
    plugin.pylsp_settings(config)

    plugin.pylsp_lint(
        config=config,
        workspace=workspace,
        document=document,
        is_saved=False,
    )
    expected = [
        "dmypy",
        "--status-file",
        ".dmypy.json",
        "run",
        "--",
        "--python-executable",
        "/tmp/fake",
        "--show-error-end",
        "--no-error-summary",
        "--no-pretty",
        document.path,
    ]
    m.assert_called_with(expected, capture_output=True, **windows_flag, encoding="utf-8")


def test_dmypy_status_file(tmpdir, last_diagnostics_monkeypatch, workspace):
    statusFile = tmpdir / ".custom_dmypy_status_file.json"

    last_diagnostics_monkeypatch.setattr(
        FakeConfig,
        "plugin_settings",
        lambda _, p: (
            {
                "dmypy": True,
                "live_mode": False,
                "dmypy_status_file": str(statusFile),
            }
            if p == "pylsp_mypy"
            else {}
        ),
    )

    document = Document(DOC_URI, workspace, DOC_TYPE_ERR)

    config = FakeConfig(uris.to_fs_path(workspace.root_uri))
    plugin.pylsp_settings(config)

    assert not statusFile.exists()

    try:
        plugin.pylsp_lint(
            config=config,
            workspace=workspace,
            document=document,
            is_saved=False,
        )

        assert statusFile.exists()
    finally:
        mypy_api.run_dmypy(["--status-file", str(statusFile), "stop"])


def test_config_sub_paths(tmpdir, last_diagnostics_monkeypatch):
    DOC_SOURCE = """
def foo():
    return
    unreachable = 1
"""
    DOC_ERR_MSG = "Statement is unreachable"

    config_sub_paths = [".config"]

    # Create configuration file for workspace.
    plugin_config = tmpdir.join("pyproject.toml")
    plugin_config.write(f"[tool.pylsp-mypy]\nenabled = true\nconfig_sub_paths = {config_sub_paths}")
    config_dir = tmpdir.mkdir(".config")
    mypy_config = config_dir.join("mypy.ini")
    mypy_config.write("[mypy]\nwarn_unreachable = True\ncheck_untyped_defs = True")

    # Initialize workspace.

    ws = Workspace(uris.from_fs_path(str(tmpdir)), Mock())
    ws._config = Config(ws.root_uri, {}, 0, {})

    # Update settings for workspace.
    settings = plugin.pylsp_settings(ws._config)
    ws._config._plugin_settings = _utils.merge_dicts(ws._config._plugin_settings, settings)

    # Test document to make sure it uses .config/mypy.ini configuration.
    doc = Document(DOC_URI, ws, DOC_SOURCE)
    diags = plugin.pylsp_lint(ws._config, ws, doc, is_saved=False)
    assert len(diags) == 1
    diag = diags[0]
    assert diag["message"] == DOC_ERR_MSG
    assert diag["code"] == "unreachable"


def test_config_sub_paths_config_changed(tmpdir, last_diagnostics_monkeypatch):
    DOC_SOURCE = """
def foo():
    return
    unreachable = 1
"""
    DOC_ERR_MSG = "Statement is unreachable"

    # Create configuration file for workspace.
    config_dir = tmpdir.mkdir(".config")
    mypy_config = config_dir.join("mypy.ini")
    mypy_config.write("[mypy]\nwarn_unreachable = True\ncheck_untyped_defs = True")

    config_sub_paths = [".config"]

    # Initialize workspace.
    ws = Workspace(uris.from_fs_path(str(tmpdir)), Mock())
    ws._config = Config(ws.root_uri, {}, 0, {})

    # Update settings for workspace.
    plugin.pylsp_settings(ws._config)
    ws.update_config({"pylsp": {"plugins": {"pylsp_mypy": {"config_sub_paths": config_sub_paths}}}})

    # Test document to make sure it uses .config/mypy.ini configuration.
    doc = Document(DOC_URI, ws, DOC_SOURCE)
    diags = plugin.pylsp_lint(ws._config, ws, doc, is_saved=False)
    assert len(diags) == 1
    diag = diags[0]
    assert diag["message"] == DOC_ERR_MSG
    assert diag["code"] == "unreachable"


@pytest.mark.parametrize(
    "document_path,pattern,os_sep,pattern_matched",
    (
        ("/workspace/my-file.py", "/someting-else", "/", False),
        ("/workspace/my-file.py", "^/workspace$", "/", False),
        ("/workspace/my-file.py", "/workspace", "/", True),
        ("/workspace/my-file.py", "^/workspace(.*)$", "/", True),
        # This is a broken regex (missing ')'), but should not choke
        ("/workspace/my-file.py", "/((workspace)", "/", False),
        # Windows paths are tricky with all those \\ and unintended escape,
        # characters but they should 'just' work
        ("d:\\a\\my-file.py", "/a", "\\", True),
        (
            "d:\\a\\pylsp-mypy\\pylsp-mypy\\test\\test_plugin.py",
            "/a/pylsp-mypy/pylsp-mypy/test/test_plugin.py",
            "\\",
            True,
        ),
    ),
)
def test_match_exclude_patterns(document_path, pattern, os_sep, pattern_matched):
    with patch("os.sep", new=os_sep):
        assert (
            plugin.match_exclude_patterns(document_path=document_path, exclude_patterns=[pattern])
            is pattern_matched
        )


def test_config_exclude(tmpdir, workspace):
    """When exclude is set in config then mypy should not run for that file."""
    doc = Document(DOC_URI, workspace, DOC_TYPE_ERR)

    plugin.pylsp_settings(workspace._config)
    workspace.update_config({"pylsp": {"plugins": {"pylsp_mypy": {}}}})
    diags = plugin.pylsp_lint(workspace._config, workspace, doc, is_saved=False)
    assert re.search(TYPE_ERR_MSG_REGEX, diags[0]["message"])

    # Add the path of our document to the exclude patterns
    exclude_path = doc.path.replace(os.sep, "/")
    workspace.update_config({"pylsp": {"plugins": {"pylsp_mypy": {"exclude": [exclude_path]}}}})
    diags = plugin.pylsp_lint(workspace._config, workspace, doc, is_saved=False)
    assert diags == []


@pytest.mark.parametrize(
    ("command", "settings", "cmd_on_path", "environmentVariableSet", "expected"),
    [
        ("mypy", {}, ["/bin/mypy"], True, ["mypy"]),
        ("mypy", {}, None, True, []),
        ("mypy", {"mypy_command": ["/path/to/mypy"]}, "/bin/mypy", True, ["/path/to/mypy"]),
        ("mypy", {"mypy_command": ["/path/to/mypy"]}, None, True, ["/path/to/mypy"]),
        ("dmypy", {}, "/bin/dmypy", True, ["dmypy"]),
        ("dmypy", {}, None, True, []),
        ("dmypy", {"dmypy_command": ["/path/to/dmypy"]}, "/bin/dmypy", True, ["/path/to/dmypy"]),
        ("dmypy", {"dmypy_command": ["/path/to/dmypy"]}, None, True, ["/path/to/dmypy"]),
        ("mypy", {}, ["/bin/mypy"], False, ["mypy"]),
        ("mypy", {}, None, False, []),
        ("mypy", {"mypy_command": ["/path/to/mypy"]}, "/bin/mypy", False, ["mypy"]),
        ("mypy", {"mypy_command": ["/path/to/mypy"]}, None, False, []),
        ("dmypy", {}, "/bin/dmypy", False, ["dmypy"]),
        ("dmypy", {}, None, False, []),
        ("dmypy", {"dmypy_command": ["/path/to/dmypy"]}, "/bin/dmypy", False, ["dmypy"]),
        ("dmypy", {"dmypy_command": ["/path/to/dmypy"]}, None, False, []),
    ],
)
def test_get_cmd(command, settings, cmd_on_path, environmentVariableSet: bool, expected):
    with patch("shutil.which", return_value=cmd_on_path):
        if environmentVariableSet:
            os.environ["PYLSP_MYPY_ALLOW_DANGEROUS_CODE_EXECUTION"] = "Does not matter at all"
        else:
            os.environ.pop("PYLSP_MYPY_ALLOW_DANGEROUS_CODE_EXECUTION", None)
        assert plugin.get_cmd(settings, command) == expected


def test_config_overrides_mypy_command(last_diagnostics_monkeypatch, workspace):
    last_diagnostics_monkeypatch.setattr(
        FakeConfig,
        "plugin_settings",
        lambda _, p: (
            {
                "mypy_command": ["/path/to/mypy"],
            }
            if p == "pylsp_mypy"
            else {}
        ),
    )

    m = Mock(wraps=lambda a, **_: Mock(returncode=0, **{"stdout": ""}))
    last_diagnostics_monkeypatch.setattr(plugin.subprocess, "run", m)

    document = Document(DOC_URI, workspace, DOC_TYPE_ERR)

    config = FakeConfig(uris.to_fs_path(workspace.root_uri))
    os.environ["PYLSP_MYPY_ALLOW_DANGEROUS_CODE_EXECUTION"] = "Does not matter at all"
    plugin.pylsp_settings(config)

    plugin.pylsp_lint(
        config=config,
        workspace=workspace,
        document=document,
        is_saved=False,
    )

    called_argv = m.call_args.args[0]
    called_cmd = called_argv[0]
    assert called_cmd == "/path/to/mypy"


def test_config_overrides_dmypy_command(last_diagnostics_monkeypatch, workspace):
    last_diagnostics_monkeypatch.setattr(
        FakeConfig,
        "plugin_settings",
        lambda _, p: (
            {
                "dmypy": True,
                "live_mode": False,
                "dmypy_command": ["poetry", "run", "dmypy"],
            }
            if p == "pylsp_mypy"
            else {}
        ),
    )

    m = Mock(wraps=lambda a, **_: Mock(returncode=0, **{"stdout": ""}))
    last_diagnostics_monkeypatch.setattr(plugin.subprocess, "run", m)

    document = Document(DOC_URI, workspace, DOC_TYPE_ERR)

    config = FakeConfig(uris.to_fs_path(workspace.root_uri))
    os.environ["PYLSP_MYPY_ALLOW_DANGEROUS_CODE_EXECUTION"] = "Does not matter at all"
    plugin.pylsp_settings(config)

    plugin.pylsp_lint(
        config=config,
        workspace=workspace,
        document=document,
        is_saved=False,
    )

    called_argv = m.call_args.args[0]
    called_cmd = called_argv[:3]
    assert called_cmd == ["poetry", "run", "dmypy"]
