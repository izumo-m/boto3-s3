"""The ``[plugins]`` section, loaded the way aws-cli loads it (design/cli.md section 1).

aws imports the plugins its config file names - only when ``[plugins]`` sets
``cli_legacy_plugin_path`` - and calls each one's ``awscli_initialize``
before it parses anything. Every expectation on reports and exit codes was
measured against the pinned aws-cli under the program-name mapping (the
leading blank line aws prints before each report is a class-1 rule of the
parity normalization, design/testing.md section 9):

- a plugin that does not import ends every invocation, ``--version`` and the
  help token included, with ``No module named '<name>'`` at rc 255;
- a module without ``awscli_initialize`` ends it with Python's own
  ``module '<name>' has no attribute 'awscli_initialize'`` at rc 255;
- a working plugin's handlers on botocore events fire on the run's requests.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from boto3_s3_cli import cli, clientfactory, configfiles, globalargs, plugins


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """A config file of this test's own, a plugin directory, and clean globals.

    The loader appends to ``sys.path`` and imports modules, and keeps the
    registrations for the sessions it later sees; all of that is undone here.
    """
    path = tmp_path / "config"
    monkeypatch.setenv("AWS_CONFIG_FILE", str(path))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(plugins, "_emitter", None)
    (tmp_path / "plugins").mkdir()
    yield path
    for name in [name for name in sys.modules if name.startswith("bs3plug")]:
        del sys.modules[name]


def _write_config(config: Path, *entries: str) -> None:
    plugin_dir = config.parent / "plugins"
    lines = ["[plugins]", f"cli_legacy_plugin_path = {plugin_dir}", *entries]
    config.write_text("\n".join(lines) + "\n")


def _write_plugin(config: Path, relative: str, body: str) -> None:
    target = config.parent / "plugins" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)


_RECORDING_PLUGIN = """
seen = []

def _before(params, **kwargs):
    seen.append(kwargs["event_name"])

def awscli_initialize(cli):
    cli.register("before-call.s3.ListObjectsV2", _before)
"""


class TestLoad:
    def test_nothing_is_imported_without_the_plugin_path(self, config: Path) -> None:
        # aws's loader pops cli_legacy_plugin_path first and imports nothing
        # at all without it - not even an entry that names a missing module.
        config.write_text("[plugins]\nmine = bs3plug_missing\n")
        plugins.load(configfiles.scan().plugins)
        assert plugins._emitter is None  # pyright: ignore[reportPrivateUsage]

    def test_a_plugin_that_does_not_import_raises(self, config: Path) -> None:
        _write_config(config, "mine = bs3plug_missing")
        with pytest.raises(ModuleNotFoundError, match="No module named 'bs3plug_missing'"):
            plugins.load(configfiles.scan().plugins)

    def test_a_plugin_without_an_initializer_raises(self, config: Path) -> None:
        _write_plugin(config, "bs3plug_noinit.py", "loaded = True\n")
        _write_config(config, "mine = bs3plug_noinit")
        with pytest.raises(AttributeError, match="has no attribute 'awscli_initialize'"):
            plugins.load(configfiles.scan().plugins)
        assert sys.modules["bs3plug_noinit"].loaded  # imported before failing

    def test_every_plugin_imports_before_any_initializes(self, config: Path) -> None:
        # aws imports the whole section first, then initializes in order: a
        # later entry that does not import stops the run before the first
        # entry's awscli_initialize has run.
        _write_plugin(
            config,
            "bs3plug_first.py",
            "ran = []\ndef awscli_initialize(cli):\n    ran.append(True)\n",
        )
        _write_config(config, "a = bs3plug_first", "b = bs3plug_missing")
        with pytest.raises(ModuleNotFoundError):
            plugins.load(configfiles.scan().plugins)
        assert sys.modules["bs3plug_first"].ran == []

    def test_a_dotted_entry_imports_the_module_it_names(self, config: Path) -> None:
        _write_plugin(config, "bs3plug_pkg/__init__.py", "")
        _write_plugin(config, "bs3plug_pkg/inner.py", _RECORDING_PLUGIN)
        _write_config(config, "mine = bs3plug_pkg.inner")
        plugins.load(configfiles.scan().plugins)
        emitter = plugins._emitter  # pyright: ignore[reportPrivateUsage]
        assert emitter is not None
        assert [(method, args[0]) for method, args, _ in emitter.calls] == [
            ("register", "before-call.s3.ListObjectsV2")
        ]

    def test_an_indented_path_fails_the_way_aws_does(self, config: Path) -> None:
        # The INI parse turns an indented block into a map; aws then calls
        # `.split` on it.
        config.write_text("[plugins]\ncli_legacy_plugin_path =\n  a = b\nmine = x\n")
        with pytest.raises(AttributeError, match="'dict' object has no attribute 'split'"):
            plugins.load(configfiles.scan().plugins)


class TestReports:
    @pytest.mark.parametrize("argv", [["--version"], ["help"], ["ls", "s3://bkt"]])
    def test_a_failing_plugin_ends_every_invocation_at_255(
        self, config: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
    ) -> None:
        _write_config(config, "mine = bs3plug_missing")
        assert cli.main(argv) == 255
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "boto3-s3: [ERROR]: No module named 'bs3plug_missing'\n"

    def test_a_credentials_file_section_is_not_read(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # In the credentials file every section is a profile: botocore never
        # puts it under full_config["plugins"].
        plugin_dir = config.parent / "plugins"
        (config.parent / "credentials").write_text(
            f"[plugins]\ncli_legacy_plugin_path = {plugin_dir}\nmine = bs3plug_missing\n"
        )
        assert cli.main(["--version"]) == 0
        assert capsys.readouterr().err == ""


class TestRegistrationChecks:
    """aws's emitter checks each registration as it is made, so a plugin aws
    refuses ends every invocation while the plugins load - ``--version``
    included - with botocore's own wording at rc 255 (measured)."""

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (
                "def _h():\n    pass\n"
                "def awscli_initialize(cli):\n    cli.register('before-call.s3', _h)\n",
                "must accept keyword arguments (**kwargs)",
            ),
            (
                "def awscli_initialize(cli):\n    cli.register('before-call.s3', 42)\n",
                "Event handler 42 must be callable.",
            ),
            (
                "def _h(**kwargs):\n    pass\n"
                "def awscli_initialize(cli):\n"
                "    cli.register('before-call.s3', _h, unique_id='x', unique_id_uses_count=True)\n"
                "    cli.register('before-call.s3', _h, unique_id='x')\n",
                "Initial registration of unique id x was specified to not use a counter.",
            ),
            (
                "def _h(**kwargs):\n    pass\n"
                "def awscli_initialize(cli):\n"
                "    cli.register('before-call.s3', _h, unique_id='x')\n"
                "    cli.unregister('before-call.s3', unique_id='x', unique_id_uses_count=True)\n",
                "Subsequent unregister calls to unique id must specify use of a counter",
            ),
        ],
        ids=["no-kwargs", "not-callable", "register-counter", "unregister-counter"],
    )
    def test_a_refused_registration_ends_the_run(
        self, config: Path, capsys: pytest.CaptureFixture[str], body: str, message: str
    ) -> None:
        _write_plugin(config, "bs3plug_bad.py", body)
        _write_config(config, "mine = bs3plug_bad")
        assert cli.main(["--version"]) == 255
        err = capsys.readouterr().err
        assert err.startswith("boto3-s3: [ERROR]: ")
        assert message in err

    def test_a_repeated_unique_id_is_recorded_once_by_botocore(self, config: Path) -> None:
        # The duplicate registration is no error (and no second handler):
        # the replay leaves botocore to drop it exactly as aws's emitter does.
        _write_plugin(
            config,
            "bs3plug_dup.py",
            "def _h(**kwargs):\n    pass\n"
            "def awscli_initialize(cli):\n"
            "    cli.register('before-call.s3', _h, unique_id='x')\n"
            "    cli.register('before-call.s3', _h, unique_id='x')\n",
        )
        _write_config(config, "mine = bs3plug_dup")
        plugins.load(configfiles.scan().plugins)
        from botocore.hooks import HierarchicalEmitter

        emitter = HierarchicalEmitter()
        plugins.attach(emitter)
        assert len(list(emitter._handlers.prefix_search("before-call.s3"))) == 1  # pyright: ignore[reportPrivateUsage]


class TestAttach:
    def test_a_plugin_handler_fires_on_the_sessions_requests(
        self, config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_plugin(config, "bs3plug_rec.py", _RECORDING_PLUGIN)
        _write_config(config, "mine = bs3plug_rec")
        plugins.load(configfiles.scan().plugins)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        args = _parse_globals(["--region", "us-east-1"])
        client = clientfactory.build_client(args)

        class _StopError(Exception):
            pass

        def stop(**_kwargs: Any) -> None:
            raise _StopError

        client.meta.events.register("before-send.s3.ListObjectsV2", stop)
        with pytest.raises(_StopError):
            client.list_objects_v2(Bucket="bkt")
        assert sys.modules["bs3plug_rec"].seen == ["before-call.s3.ListObjectsV2"]


def _parse_globals(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    globalargs.add_common_arguments(parser)
    return parser.parse_args(argv)
