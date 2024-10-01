Mypy plugin for PYLSP
======================

.. image:: https://badge.fury.io/py/pylsp-mypy.svg
    :target: https://badge.fury.io/py/pylsp-mypy

.. image:: https://github.com/python-lsp/pylsp-mypy/workflows/Python%20package/badge.svg?branch=master
    :target: https://github.com/python-lsp/pylsp-mypy/

This is a plugin for the `Python LSP Server`_.

.. _`Python LSP Server`: https://github.com/python-lsp/python-lsp-server

It, like mypy, requires Python 3.8 or newer.


Installation
------------

Install into the same virtualenv as python-lsp-server itself.

``pip install pylsp-mypy``

Configuration
-------------
``pylsp-mypy`` supports the use of ``pyproject.toml`` for configuration. It can also be configuered using configs provided to the LSP server. The configuration keys are listed in the following.

.. list-table:: Configuration
   :header-rows: 1

   * - ``pyproject.toml`` key
     - LSP Configuration Key
     - Type
     - Description
     - Default
   * - ``live_mode``
     - ``pylsp.plugins.pylsp_mypy.live_mode``
     - ``boolean``
     - **Provides type checking as you type**. This writes to a tempfile every time a check is done. Turning off ``live_mode`` means you must save your changes for mypy diagnostics to update correctly.
     - true
   * - ``dmypy``
     - ``pylsp.plugins.pylsp_mypy.dmypy``
     - ``boolean``
     - **Executes via** ``dmypy run`` **rather than** ``mypy``. This uses the ``dmypy`` daemon and may dramatically improve the responsiveness of the ``pylsp`` server, however this currently does not work in ``live_mode``. Enabling this disables ``live_mode``, even for conflicting configs.
     - false
   * - ``strict``
     - ``pylsp.plugins.pylsp_mypy.strict``
     - ``boolean``
     - **Refers to the** ``strict`` **option of** ``mypy``. This option often is too strict to be useful.
     - false
   * - ``overrides``
     - ``pylsp.plugins.pylsp_mypy.overrides``
     - ``array`` of (``string`` items or ``true``)
     - **A list of alternate or supplemental command-line options**. This modifies the options passed to ``mypy`` or the mypy-specific ones passed to ``dmypy run``. When present, the special boolean member ``true`` is replaced with the command-line options that would've been passed had ``overrides`` not been specified.
     - ``[true]``
   * - ``dmypy_status_file``
     - ``pylsp.plugins.pylsp_mypy.dmypy_status_file``
     - ``string``
     - **Specifies which status file dmypy should use**. This modifies the ``--status-file`` option passed to ``dmypy`` given ``dmypy`` is active.
     - ``.dmypy.json``
   * - ``config_sub_paths``
     - ``pylsp.plugins.pylsp_mypy.config_sub_paths``
     - ``array`` of ``string`` items
     - **Specifies sub paths under which the mypy configuration file may be found**. For each directory searched for the mypy config file, this also searches the sub paths specified here.
     - ``[]``
   * - ``report_progress``
     - ``pylsp.plugins.pylsp_mypy.report_progress``
     - ``boolean``
     - **Report basic progress to the LSP client**. With this option, pylsp-mypy will report when mypy is running, given your editor supports LSP progress reporting. For small files this might produce annoying flashing in your editor, especially in ``live_mode``. For large projects, enabling this can be helpful to assure yourself whether mypy is still running.
     - false
   * - ``exclude``
     - ``pylsp.plugins.pylsp_mypy.exclude``
     - ``array`` of ``string`` items
     - **A list of regular expressions which should be ignored**. The ``mypy`` runner wil not be invoked when a document path is matched by one of the expressions. Note that this differs from the ``exclude`` directive of a ``mypy`` config which is only used for recursively discovering files when mypy is invoked on a whole directory. For both windows or unix platforms you should use forward slashes (``/``) to indicate paths.
     - ``[]``
   * - ``follow-imports``
     - ``pylsp.plugins.pylsp_mypy.follow-imports``
     - ``normal``, ``silent``, ``skip`` or ``error``
     - ``mypy`` **parameter** ``follow-imports``. In ``mypy`` this is ``normal`` by default. We set it ``silent``, to sort out unwanted results. This can cause cash invalidation if you also run ``mypy`` in other ways. Setting this to ``normal`` avoids this at the cost of a small performance penalty.
     - ``silent``
   * - ``mypy_command``
     - ``pylsp.plugins.pylsp_mypy.mypy_command``
     - ``array`` of ``string`` items
     - **The command to run mypy**. This is useful if you want to run mypy in a specific virtual environment.
     - ``[]``
   * - ``dmypy_command``
     - ``pylsp.plugins.pylsp_mypy.dmypy_command``
     - ``array`` of ``string`` items
     - **The command to run dmypy**. This is useful if you want to run dmypy in a specific virtual environment.
     - ``[]``

Using a ``pyproject.toml`` for configuration, which is in fact the preferred way, your configuration could look like this:

::

    [tool.pylsp-mypy]
    enabled = true
    live_mode = true
    strict = true
    exclude = ["tests/*"]

A ``pyproject.toml`` does not conflict with the legacy config file (deprecated) given that it does not contain a ``pylsp-mypy`` section. The following explanation uses the syntax of the legacy config file (deprecated). However, all these options also apply to the ``pyproject.toml`` configuration (note the lowercase bools).
Depending on your editor, the configuration (found in a file called pylsp-mypy.cfg in your workspace or a parent directory) should be roughly like this for a standard configuration:

::

    {
        "enabled": True,
        "live_mode": True,
        "strict": False,
        "exclude": ["tests/*"]
    }

With ``dmypy`` enabled your config should look like this:

::

    {
        "enabled": True,
        "live_mode": False,
        "dmypy": True,
        "strict": False
    }

With ``overrides`` specified (for example to tell mypy to use a different python than the currently active venv), your config could look like this:

::

    {
        "enabled": True,
        "overrides": ["--python-executable", "/home/me/bin/python", True]
    }

With ``dmypy_status_file`` your config could look like this:

::

    {
        "enabled": True,
        "live_mode": False,
        "dmypy": True,
        "strict": False,
        "dmypy_status_file": ".custom_dmypy_status_file.json"
    }

With ``config_sub_paths`` your config could look like this:

::

    {
        "enabled": True,
        "config_sub_paths": [".config"]
    }

With ``report_progress`` your config could look like this:

::

    {
        "enabled": True,
        "report_progress": True
    }

With ``mypy_command`` your config could look like this:

::

    {
        "enabled": True,
        "mypy_command": ["poetry", "run", "mypy"]
    }

With ``dmypy_command`` your config could look like this:

::

    {
        "enabled": True,
        "live_mode": False,
        "dmypy": True,
        "dmypy_command": ["/path/to/venv/bin/dmypy"]
    }

Developing
-------------

Install development dependencies with (you might want to create a virtualenv first):

::

   pip install -r requirements.txt

The project is formatted with `black`_. You can either configure your IDE to automatically format code with it, run it manually (``black .``) or rely on pre-commit (see below) to format files on git commit.

The project is formatted with `isort`_. You can either configure your IDE to automatically sort imports with it, run it manually (``isort .``) or rely on pre-commit (see below) to sort files on git commit.

The project uses two rst tests in order to assure uploadability to pypi: `rst-linter`_ as a pre-commit hook and `rstcheck`_ in a GitHub workflow. This does not catch all errors.

This project uses `pre-commit`_ to enforce code-quality. After cloning the repository install the pre-commit hooks with:

::

   pre-commit install

After that pre-commit will run `all defined hooks`_ on every ``git commit`` and keep you from committing if there are any errors.

.. _black: https://github.com/psf/black
.. _isort: https://github.com/PyCQA/isort
.. _rst-linter: https://github.com/Lucas-C/pre-commit-hooks-markup
.. _rstcheck: https://github.com/myint/rstcheck
.. _pre-commit: https://pre-commit.com/
.. _all defined hooks: .pre-commit-config.yaml
