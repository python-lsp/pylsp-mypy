# -*- coding: utf-8 -*-
"""
File that contains the python-lsp-server plugin pylsp-mypy.

Created on Fri Jul 10 09:53:57 2020

@author: Richard Kellnberger
"""
import ast
import atexit
import collections
import logging
import os
import os.path
import re
import shutil
import subprocess
import tempfile
from configparser import ConfigParser
from pathlib import Path
from typing import IO, Any, Dict, List, Optional

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from mypy import api as mypy_api
from pylsp import hookimpl
from pylsp.config.config import Config
from pylsp.workspace import Document, Workspace

line_pattern = re.compile(
    (
        r"^(?P<file>.+):(?P<start_line>\d+):(?P<start_col>\d*):(?P<end_line>\d*):(?P<end_col>\d*): "
        r"(?P<severity>\w+): (?P<message>.+?)(?: +\[(?P<code>.+)\])?$"
    )
)

whole_line_pattern = re.compile(  # certain mypy warnings do not report start-end ranges
    (
        r"^(?P<file>.+):(?P<start_line>\d+): "
        r"(?P<severity>\w+): (?P<message>.+?)(?: +\[(?P<code>.+)\])?$"
    )
)

log = logging.getLogger(__name__)

# A mapping from workspace path to config file path
mypyConfigFileMap: Dict[str, Optional[str]] = {}

settingsCache: Dict[str, Dict[str, Any]] = {}

tmpFile: Optional[IO[bytes]] = None

# In non-live-mode the file contents aren't updated.
# Returning an empty diagnostic clears the diagnostic result,
# so store a cache of last diagnostics for each file a-la the pylint plugin,
# so we can return some potentially-stale diagnostics.
# https://github.com/python-lsp/python-lsp-server/blob/v1.0.1/pylsp/plugins/pylint_lint.py#L55-L62
last_diagnostics: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)

# Windows started opening opening a cmd-like window for every subprocess call
# This flag prevents that.
# This flag is new in python 3.7
# This flag only exists on Windows
windows_flag: Dict[str, int] = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}  # type: ignore
)


def parse_line(line: str, document: Optional[Document] = None) -> Optional[Dict[str, Any]]:
    """
    Return a language-server diagnostic from a line of the Mypy error report.

    optionally, use the whole document to provide more context on it.


    Parameters
    ----------
    line : str
        Line of mypy output to be analysed.
    document : Optional[Document], optional
        Document in wich the line is found. The default is None.

    Returns
    -------
    Optional[Dict[str, Any]]
        The dict with the lint data.

    """
    result = line_pattern.match(line) or whole_line_pattern.match(line)

    if not result:
        return None

    file_path = result["file"]
    if file_path != "<string>":  # live mode
        # results from other files can be included, but we cannot return
        # them.
        if document and document.path and not document.path.endswith(file_path):
            log.warning("discarding result for %s against %s", file_path, document.path)
            return None

    lineno = int(result["start_line"]) - 1  # 0-based line number
    offset = int(result.groupdict().get("start_col", 1)) - 1  # 0-based offset
    end_lineno = int(result.groupdict().get("end_line", lineno + 1)) - 1
    end_offset = int(result.groupdict().get("end_col", 1))  # end is exclusive

    severity = result["severity"]
    if severity not in ("error", "note"):
        log.warning(f"invalid error severity '{severity}'")
    errno = 1 if severity == "error" else 3

    return {
        "source": "mypy",
        "range": {
            "start": {"line": lineno, "character": offset},
            "end": {"line": end_lineno, "character": end_offset},
        },
        "message": result["message"],
        "severity": errno,
        "code": result["code"],
    }


def apply_overrides(args: List[str], overrides: List[Any]) -> List[str]:
    """Replace or combine default command-line options with overrides."""
    overrides_iterator = iter(overrides)
    if True not in overrides_iterator:
        return overrides
    # If True is in the list, the if above leaves the iterator at the element after True,
    # therefore, the list below only contains the elements after the True
    rest = list(overrides_iterator)
    # slice of the True and the rest, add the args, add the rest
    return overrides[: -(len(rest) + 1)] + args + rest


def didSettingsChange(workspace: str, settings: Dict[str, Any]) -> None:
    """Handle relevant changes to the settings between runs."""
    configSubPaths = settings.get("config_sub_paths", [])
    if settingsCache[workspace].get("config_sub_paths", []) != configSubPaths:
        mypyConfigFile = findConfigFile(
            workspace,
            configSubPaths,
            ["mypy.ini", ".mypy.ini", "pyproject.toml", "setup.cfg"],
            True,
        )
        mypyConfigFileMap[workspace] = mypyConfigFile
        settingsCache[workspace] = settings.copy()


def match_exclude_patterns(document_path: str, exclude_patterns: list) -> bool:
    """Check if the current document path matches any of the configures exlude patterns."""
    document_path = document_path.replace(os.sep, "/")

    for pattern in exclude_patterns:
        try:
            if re.search(pattern, document_path):
                log.debug(f"{document_path} matches " f"exclude pattern '{pattern}'")
                return True
        except re.error as e:
            log.error(f"pattern {pattern} is not a valid regular expression: {e}")

    return False


def get_cmd(settings: Dict[str, Any], cmd: str) -> List[str]:
    """
    Get the command to run from settings, falling back to searching the PATH.
    If the command is not found in the settings and is not available on the PATH, an
    empty list is returned.
    """
    command_key = f"{cmd}_command"
    command: List[str] = settings.get(command_key, [])

    if not (command and os.getenv("PYLSP_MYPY_ALLOW_DANGEROUS_CODE_EXECUTION")):
        # env var is required to allow command from settings
        if shutil.which(cmd):  # Fallback to PATH
            log.debug(
                f"'{command_key}' not found in settings or not allowed, using '{cmd}' from PATH"
            )
            command = [cmd]
        else:  # Fallback to API
            command = []

    log.debug(f"Using {cmd} command: {command}")

    return command


@hookimpl
def pylsp_lint(
    config: Config, workspace: Workspace, document: Document, is_saved: bool
) -> List[Dict[str, Any]]:
    """
    Call the linter.

    Parameters
    ----------
    config : Config
        The pylsp config.
    workspace : Workspace
        The pylsp workspace.
    document : Document
        The document to be linted.
    is_saved : bool
        Weather the document is saved.

    Returns
    -------
    List[Dict[str, Any]]
        List of the linting data.

    """
    settings = config.plugin_settings("pylsp_mypy")
    oldSettings1 = config.plugin_settings("mypy-ls")
    oldSettings2 = config.plugin_settings("mypy_ls")
    if oldSettings1 != {} or oldSettings2 != {}:
        raise NameError(
            "Your configuration uses an old namespace (mypy-ls or mypy_ls)."
            + "This should be changed to pylsp_mypy"
        )
    if settings == {}:
        settings = oldSettings1
        if settings == {}:
            settings = oldSettings2

    didSettingsChange(workspace.root_path, settings)

    # Running mypy with a single file (document) ignores any exclude pattern
    # configured with mypy. We can now add our own exclude section like so:
    # [tool.pylsp-mypy]
    # exclude = ["tests/*"]
    exclude_patterns = settings.get("exclude", [])

    if match_exclude_patterns(document_path=document.path, exclude_patterns=exclude_patterns):
        log.debug(
            f"Not running because {document.path} matches " f"exclude patterns '{exclude_patterns}'"
        )
        return []

    if settings.get("report_progress", False):
        with workspace.report_progress("lint: mypy"):
            return get_diagnostics(workspace, document, settings, is_saved)
    else:
        return get_diagnostics(workspace, document, settings, is_saved)


def get_diagnostics(
    workspace: Workspace,
    document: Document,
    settings: Dict[str, Any],
    is_saved: bool,
) -> List[Dict[str, Any]]:
    """
    Lints.

    Parameters
    ----------
    workspace : Workspace
        The pylsp workspace.
    document : Document
        The document to be linted.
    is_saved : bool
        Weather the document is saved.

    Returns
    -------
    List[Dict[str, Any]]
        List of the linting data.

    """
    log.info(
        "lint settings = %s document.path = %s is_saved = %s",
        settings,
        document.path,
        is_saved,
    )

    live_mode = settings.get("live_mode", True)
    dmypy = settings.get("dmypy", False)

    if dmypy and live_mode:
        # dmypy can only be efficiently run on files that have been saved, see:
        # https://github.com/python/mypy/issues/9309
        log.warning("live_mode is not supported with dmypy, disabling")
        live_mode = False

    if dmypy:
        dmypy_status_file = settings.get("dmypy_status_file", ".dmypy.json")

    args = ["--show-error-end", "--no-error-summary", "--no-pretty"]

    global tmpFile
    if live_mode and not is_saved:
        if tmpFile:
            tmpFile = open(tmpFile.name, "wb")
        else:
            tmpFile = tempfile.NamedTemporaryFile("wb", delete=False)
        log.info("live_mode tmpFile = %s", tmpFile.name)
        tmpFile.write(bytes(document.source, "utf-8"))
        tmpFile.close()
        args.extend(["--shadow-file", document.path, tmpFile.name])
    elif not is_saved and document.path in last_diagnostics:
        # On-launch the document isn't marked as saved, so fall through and run
        # the diagnostics anyway even if the file contents may be out of date.
        log.info(
            "non-live, returning cached diagnostics len(cached) = %s",
            last_diagnostics[document.path],
        )
        return last_diagnostics[document.path]

    mypyConfigFile = mypyConfigFileMap.get(workspace.root_path)
    if mypyConfigFile:
        args.append("--config-file")
        args.append(mypyConfigFile)

    args.append(document.path)

    if settings.get("strict", False):
        args.append("--strict")

    overrides = settings.get("overrides", [True])
    exit_status = 0

    if not dmypy:
        args.extend(["--incremental", "--follow-imports", settings.get("follow-imports", "silent")])
        args = apply_overrides(args, overrides)

        mypy_command: List[str] = get_cmd(settings, "mypy")

        if mypy_command:
            # mypy exists on PATH or was provided by settings
            # -> use this mypy
            log.info("executing mypy args = %s on path", args)
            completed_process = subprocess.run(
                [*mypy_command, *args], capture_output=True, **windows_flag, encoding="utf-8"
            )
            report = completed_process.stdout
            errors = completed_process.stderr
            exit_status = completed_process.returncode
        else:
            # mypy does not exist on PATH and was not provided by settings,
            # but must exist in the env pylsp-mypy is installed in
            # -> use mypy via api
            log.info("executing mypy args = %s via api", args)
            report, errors, exit_status = mypy_api.run(args)
    else:
        # If dmypy daemon is non-responsive calls to run will block.
        # Check daemon status, if non-zero daemon is dead or hung.
        # If daemon is hung, kill will reset
        # If daemon is dead/absent, kill will no-op.
        # In either case, reset to fresh state

        dmypy_command: List[str] = get_cmd(settings, "dmypy")

        if dmypy_command:
            # dmypy exists on PATH or was provided by settings
            # -> use this dmypy
            completed_process = subprocess.run(
                [*dmypy_command, "--status-file", dmypy_status_file, "status"],
                capture_output=True,
                **windows_flag,
                encoding="utf-8",
            )
            errors = completed_process.stderr
            exit_status = completed_process.returncode
            if exit_status != 0:
                log.info(
                    "restarting dmypy from status: %s message: %s via path",
                    exit_status,
                    errors.strip(),
                )
                subprocess.run(
                    ["dmypy", "--status-file", dmypy_status_file, "restart"],
                    capture_output=True,
                    **windows_flag,
                    encoding="utf-8",
                )
        else:
            # dmypy does not exist on PATH and was not provided by settings,
            # but must exist in the env pylsp-mypy is installed in
            # -> use dmypy via api
            _, errors, exit_status = mypy_api.run_dmypy(
                ["--status-file", dmypy_status_file, "status"]
            )
            if exit_status != 0:
                log.info(
                    "restarting dmypy from status: %s message: %s via api",
                    exit_status,
                    errors.strip(),
                )
                mypy_api.run_dmypy(["--status-file", dmypy_status_file, "restart"])

        # run to use existing daemon or restart if required
        args = ["--status-file", dmypy_status_file, "run", "--"] + apply_overrides(args, overrides)
        if dmypy_command:
            # dmypy exists on PATH or was provided by settings
            # -> use this dmypy
            log.info("dmypy run args = %s via path", args)
            completed_process = subprocess.run(
                [*dmypy_command, *args], capture_output=True, **windows_flag, encoding="utf-8"
            )
            report = completed_process.stdout
            errors = completed_process.stderr
            exit_status = completed_process.returncode
        else:
            # dmypy does not exist on PATH and was not provided by settings,
            # but must exist in the env pylsp-mypy is installed in
            # -> use dmypy via api
            log.info("dmypy run args = %s via api", args)
            report, errors, exit_status = mypy_api.run_dmypy(args)

    log.debug("report:\n%s", report)
    log.debug("errors:\n%s", errors)

    diagnostics = []

    # Expose generic mypy error on the first line.
    if errors:
        diagnostics.append(
            {
                "source": "mypy",
                "range": {
                    "start": {"line": 0, "character": 0},
                    # Client is supposed to clip end column to line length.
                    "end": {"line": 0, "character": 1000},
                },
                "message": errors,
                "severity": 1 if exit_status != 0 else 2,  # Error if exited with error or warning.
            }
        )

    for line in report.splitlines():
        log.debug("parsing: line = %r", line)
        diag = parse_line(line, document)
        if diag:
            diagnostics.append(diag)

    log.info("pylsp-mypy len(diagnostics) = %s", len(diagnostics))

    last_diagnostics[document.path] = diagnostics
    return diagnostics


@hookimpl
def pylsp_settings(config: Config) -> Dict[str, Dict[str, Dict[str, str]]]:
    """
    Read the settings.

    Parameters
    ----------
    config : Config
        The pylsp config.

    Returns
    -------
    Dict[str, Dict[str, Dict[str, str]]]
        The config dict.

    """
    configuration = init(config._root_path)
    return {"plugins": {"pylsp_mypy": configuration}}


def init(workspace: str) -> Dict[str, str]:
    """
    Find plugin and mypy config files and creates the temp file should it be used.

    Parameters
    ----------
    workspace : str
        The path to the current workspace.

    Returns
    -------
    Dict[str, str]
        The plugin config dict.

    """
    log.info("init workspace = %s", workspace)

    configuration = {}
    path = findConfigFile(
        workspace, [], ["pylsp-mypy.cfg", "mypy-ls.cfg", "mypy_ls.cfg", "pyproject.toml"], False
    )
    if path:
        if "pyproject.toml" in path:
            with open(path, "rb") as file:
                configuration = tomllib.load(file).get("tool").get("pylsp-mypy")
        else:
            with open(path) as file:
                configuration = ast.literal_eval(file.read())

    configSubPaths = configuration.get("config_sub_paths", [])
    mypyConfigFile = findConfigFile(
        workspace, configSubPaths, ["mypy.ini", ".mypy.ini", "pyproject.toml", "setup.cfg"], True
    )
    mypyConfigFileMap[workspace] = mypyConfigFile
    settingsCache[workspace] = configuration.copy()

    log.info("mypyConfigFile = %s configuration = %s", mypyConfigFile, configuration)
    return configuration


def findConfigFile(
    path: str, configSubPaths: List[str], names: List[str], mypy: bool
) -> Optional[str]:
    """
    Search for a config file.

    Search for a file of a given name from the directory specifyed by path through all parent
    directories. The first file found is selected.

    Parameters
    ----------
    path : str
        The path where the search starts.
    configSubPaths : List[str]
        Additional sub search paths in which mypy configs might be located
    names : List[str]
        The file to be found (or alternative names).
    mypy : bool
        whether the config file searched is for mypy (plugin otherwise)

    Returns
    -------
    Optional[str]
        The path where the file has been found or None if no matching file has been found.

    """
    start = Path(path).joinpath(names[0])  # the join causes the parents to include path
    for parent in start.parents:
        for name in names:
            for subPath in [""] + configSubPaths:
                file = parent.joinpath(subPath).joinpath(name)
                if file.is_file():
                    if file.name in ["mypy-ls.cfg", "mypy_ls.cfg"]:
                        raise NameError(
                            f"{str(file)}: {file.name} is no longer supported, you should rename "
                            "your config file to pylsp-mypy.cfg or preferably use a pyproject.toml "
                            "instead."
                        )
                    if file.name == "pyproject.toml":
                        with open(file, "rb") as fileO:
                            configPresent = (
                                tomllib.load(fileO)
                                .get("tool", {})
                                .get("mypy" if mypy else "pylsp-mypy")
                                is not None
                            )
                        if not configPresent:
                            continue
                    if file.name == "setup.cfg":
                        config = ConfigParser()
                        config.read(str(file))
                        if "mypy" not in config:
                            continue
                    return str(file)
    # No config file found in the whole directory tree
    # -> check mypy default locations for mypy config
    if mypy:
        defaultPaths = ["~/.config/mypy/config", "~/.mypy.ini"]
        XDG_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME")
        if XDG_CONFIG_HOME:
            defaultPaths.insert(0, f"{XDG_CONFIG_HOME}/mypy/config")
        for path in defaultPaths:
            if Path(path).expanduser().exists():
                return str(Path(path).expanduser())
    return None


@hookimpl
def pylsp_code_actions(
    config: Config,
    workspace: Workspace,
    document: Document,
    range: Dict,
    context: Dict,
) -> List[Dict]:
    """
    Provide code actions to ignore errors.

    Parameters
    ----------
    config : pylsp.config.config.Config
        Current config.
    workspace : pylsp.workspace.Workspace
        Current workspace.
    document : pylsp.workspace.Document
        Document to apply code actions on.
    range : Dict
        Range argument given by pylsp.
    context : Dict
        CodeActionContext given as dict.

    Returns
    -------
      List of dicts containing the code actions.
    """
    actions = []
    # Code actions based on diagnostics
    for diagnostic in context.get("diagnostics", []):
        if diagnostic["source"] != "mypy":
            continue
        code = diagnostic["code"]
        lineNumberEnd = diagnostic["range"]["end"]["line"]
        line = document.lines[lineNumberEnd]
        endOfLine = len(line) - 1
        start = {"line": lineNumberEnd, "character": endOfLine}
        edit_range = {"start": start, "end": start}
        edit = {"range": edit_range, "newText": f"  # type: ignore[{code}]"}

        action = {
            "title": f"# type: ignore[{code}]",
            "kind": "quickfix",
            "diagnostics": [diagnostic],
            "edit": {"changes": {document.uri: [edit]}},
        }
        actions.append(action)
    if context.get("diagnostics", []) != []:
        return actions

    # Code actions based on current selected range
    for diagnostic in last_diagnostics[document.path]:
        lineNumberStart = diagnostic["range"]["start"]["line"]
        lineNumberEnd = diagnostic["range"]["end"]["line"]
        rStart = range["start"]["line"]
        rEnd = range["end"]["line"]
        if (rStart <= lineNumberStart and rEnd >= lineNumberStart) or (
            rStart <= lineNumberEnd and rEnd >= lineNumberEnd
        ):
            code = diagnostic["code"]
            line = document.lines[lineNumberEnd]
            endOfLine = len(line) - 1
            start = {"line": lineNumberEnd, "character": endOfLine}
            edit_range = {"start": start, "end": start}
            edit = {"range": edit_range, "newText": f"  # type: ignore[{code}]"}
            action = {
                "title": f"# type: ignore[{code}]",
                "kind": "quickfix",
                "edit": {"changes": {document.uri: [edit]}},
            }
            actions.append(action)

    return actions


@atexit.register
def close() -> None:
    """
    Deltes the tempFile should it exist.

    Returns
    -------
    None.

    """
    if tmpFile and tmpFile.name:
        os.unlink(tmpFile.name)
