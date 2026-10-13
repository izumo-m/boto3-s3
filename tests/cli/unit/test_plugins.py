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
        assert [(method, args[0]) for method, args in emitter.calls] == [
            ("register", "before-call.s3.ListObjectsV2")
        ]

    def test_an_indented_path_fails_the_way_aws_does(self, config: Path) -> None:
        # The INI parse turns an indented block into a map; aws then calls
        # `.split` on it.
        config.write_text("[plugins]\ncli_legacy_plugin_path =\n  a = b\nmine = x\n")
        with pytest.raises(AttributeError, match="'dict' object has no attribute 'split'"):
            plugins.load(configfiles.scan().plugins)

    def test_an_indented_entry_fails_in_aws_s_interpreter_s_words(self, config: Path) -> None:
        # A map reaches `__import__`, which Python 3.10 refuses in other words
        # than the 3.14 aws runs on (measured: `module name must be a
        # string`).
        _write_config(config, "mine =\n  a = b")
        with pytest.raises(TypeError, match=r"^module name must be a string$"):
            plugins.load(configfiles.scan().plugins)

    def test_each_load_starts_from_no_registrations(self, config: Path) -> None:
        # aws builds a new emitter with every driver; a config without the
        # plugin path leaves nothing behind from an earlier load.
        _write_plugin(config, "bs3plug_once.py", _RECORDING_PLUGIN)
        _write_config(config, "mine = bs3plug_once")
        plugins.load(configfiles.scan().plugins)
        assert plugins._emitter is not None  # pyright: ignore[reportPrivateUsage]
        config.write_text("[plugins]\n")
        plugins.load(configfiles.scan().plugins)
        assert plugins._emitter is None  # pyright: ignore[reportPrivateUsage]


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

    @pytest.mark.parametrize(
        ("body", "rc", "message"),
        [
            # botocore's parameter-validation failure is aws's entry point's
            # first handler: 252 with its envelope (measured).
            (
                "from botocore.exceptions import ParamValidationError\n"
                "raise ParamValidationError(report='plugin says no')\n",
                252,
                "An error occurred (ParamValidation): Parameter validation failed:\nplugin says no",
            ),
            # The rest of the botocore family is that entry point's general
            # 255, its dedicated rc-253/254 handlers belonging to the driver.
            (
                "from botocore.exceptions import NoCredentialsError\nraise NoCredentialsError()\n",
                255,
                "Unable to locate credentials",
            ),
            ("raise AssertionError('plugin assert')\n", 255, "plugin assert"),
            # Outside the Exception family aws's chain claims nothing and the
            # run ends at rc 0 with nothing written; here it is the general
            # report (docs/cli/aws-differences.md).
            (
                "class Stop(BaseException):\n    pass\nraise Stop('base boom')\n",
                255,
                "base boom",
            ),
            ("raise GeneratorExit('gen')\n", 255, "gen"),
        ],
        ids=["param-validation", "no-credentials", "assertion", "base-exception", "generator-exit"],
    )
    def test_a_plugin_s_import_failure_is_aws_s_entry_point_report(
        self,
        config: Path,
        capsys: pytest.CaptureFixture[str],
        body: str,
        rc: int,
        message: str,
    ) -> None:
        _write_plugin(config, "bs3plug_raise.py", body)
        _write_config(config, "mine = bs3plug_raise")
        assert cli.main(["--version"]) == rc
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == f"boto3-s3: [ERROR]: {message}\n"

    def test_a_plugin_that_exits_ends_the_run_with_its_status(self, config: Path) -> None:
        # Python's own contract, kept: aws's chain lets the exit through to
        # rc 0 whatever the plugin asked (docs/cli/aws-differences.md).
        _write_plugin(
            config, "bs3plug_exit.py", "import sys\ndef awscli_initialize(cli):\n    sys.exit(7)\n"
        )
        _write_config(config, "mine = bs3plug_exit")
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--version"])
        assert excinfo.value.code == 7

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

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            # botocore's trie splits the name of every registration it keeps,
            # so a name that is no string fails where aws's emitter fails:
            # while the plugins load (measured: `--version` is rc 255).
            (
                "def _h(**kwargs):\n    pass\n"
                "def awscli_initialize(cli):\n    cli.register(None, _h)\n",
                "'NoneType' object has no attribute 'split'",
            ),
            (
                "def _h(**kwargs):\n    pass\n"
                "def awscli_initialize(cli):\n    cli.register(['before-call'], _h)\n",
                "'list' object has no attribute 'split'",
            ),
            # Python names the class that defines the method; aws's is
            # botocore's own.
            (
                "def _h(**kwargs):\n    pass\n"
                "def awscli_initialize(cli):\n    cli.register('before-call.s3', _h, priority=1)\n",
                "BaseEventHooks.register() got an unexpected keyword argument 'priority'",
            ),
            (
                "def awscli_initialize(cli):\n    cli.unregister()\n",
                "HierarchicalEmitter.unregister() missing 1 required positional argument: "
                "'event_name'",
            ),
        ],
        ids=["none-name", "list-name", "unknown-keyword", "missing-name"],
    )
    def test_a_call_botocore_s_emitter_refuses_ends_the_run_while_loading(
        self, config: Path, capsys: pytest.CaptureFixture[str], body: str, message: str
    ) -> None:
        _write_plugin(config, "bs3plug_refused.py", body)
        _write_config(config, "mine = bs3plug_refused")
        assert cli.main(["--version"]) == 255
        assert capsys.readouterr().err == f"boto3-s3: [ERROR]: {message}\n"

    def test_unregistering_an_sdk_handler_by_reference_takes_effect(
        self, config: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The common way to let an S3-compatible store's bucket names through
        # botocore: aws drops the check, so the request goes out (measured:
        # `Could not connect` at 255 on a dead endpoint, not the 252 of
        # `Invalid bucket name`). A longer name registered first leaves this
        # node without handlers in the plugin's own tree - aws's has the
        # SDK's there - and the load must not fail on it.
        _write_plugin(
            config,
            "bs3plug_vbn.py",
            "from botocore.handlers import validate_bucket_name\n"
            "def _h(**kwargs):\n    pass\n"
            "def awscli_initialize(cli):\n"
            "    cli.register('before-parameter-build.s3.PutObject', _h)\n"
            "    cli.unregister('before-parameter-build.s3', validate_bucket_name)\n",
        )
        _write_config(config, "mine = bs3plug_vbn")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")
        assert cli.main(["--version"]) == 0
        capsys.readouterr()
        assert cli.main(["ls", "s3://bad!bucket/", "--endpoint-url", "http://127.0.0.1:9"]) == 255
        assert "Could not connect to the endpoint URL" in capsys.readouterr().err

    def test_an_unregister_on_a_name_without_handlers_fails_at_the_first_client(
        self, config: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws fails this while the plugins load; here the name could hold the
        # SDK's handlers, so it is the replay that fails, on the first client.
        _write_plugin(
            config,
            "bs3plug_mid.py",
            "def _h(**kwargs):\n    pass\n"
            "def awscli_initialize(cli):\n"
            "    cli.register('bs3.events.deeper', _h)\n"
            "    cli.unregister('bs3.events', _h)\n",
        )
        _write_config(config, "mine = bs3plug_mid")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        assert cli.main(["--version"]) == 0
        capsys.readouterr()
        assert cli.main(["ls", "s3://bkt", "--endpoint-url", "http://127.0.0.1:9"]) == 255
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: 'NoneType' object has no attribute 'first'\n"
        )

    def test_a_call_aws_answers_without_its_handlers_is_not_replayed(self, config: Path) -> None:
        # aws returns before it reads the name of an unregister whose
        # unique_id was never registered, or of a registration repeating one;
        # replaying those onto stock botocore's aliasing emitter would split
        # the name there and fail the session aws runs (measured).
        _write_plugin(
            config,
            "bs3plug_noop.py",
            "def _h(**kwargs):\n    pass\n"
            "def awscli_initialize(cli):\n"
            "    cli.unregister(None, unique_id='never-registered')\n"
            "    cli.register('before-call.s3', _h, unique_id='u')\n"
            "    cli.register(None, _h, unique_id='u')\n",
        )
        _write_config(config, "mine = bs3plug_noop")
        plugins.load(configfiles.scan().plugins)
        import botocore.session

        session = botocore.session.Session()
        plugins.attach(session.get_component("event_emitter"))
        emitter = plugins._emitter  # pyright: ignore[reportPrivateUsage]
        assert emitter is not None
        assert [method for method, _args in emitter.calls] == ["register"]

    def test_emitting_reaches_the_plugins_own_handlers(
        self, config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws's emitter runs a plugin's handlers for an event the plugin
        # emits while it initializes (measured, every invocation alike).
        _write_plugin(
            config,
            "bs3plug_emit.py",
            "import sys\n"
            "def _h(**kwargs):\n"
            "    sys.stderr.write('ran %s\\n' % kwargs['event_name'])\n"
            "    return 'answer'\n"
            "def awscli_initialize(cli):\n"
            "    cli.register('bs3-event', _h)\n"
            "    sys.stderr.write('%d\\n' % len(cli.emit('bs3-event.sub', x=1)))\n"
            "    sys.stderr.write('%s\\n' % cli.emit_until_response('bs3-event')[1])\n",
        )
        _write_config(config, "mine = bs3plug_emit")
        assert cli.main(["--version"]) == 0
        assert capsys.readouterr().err == "ran bs3-event.sub\n1\nran bs3-event\nanswer\n"

    def test_a_copy_of_the_emitter_is_detached(self, config: Path) -> None:
        # botocore's copy keeps its own registrations: what a plugin puts on a
        # copy reaches no session.
        _write_plugin(
            config,
            "bs3plug_copy.py",
            "import copy\n"
            "def _h(**kwargs):\n    pass\n"
            "def awscli_initialize(cli):\n"
            "    copy.copy(cli).register('before-call.s3', _h)\n",
        )
        _write_config(config, "mine = bs3plug_copy")
        plugins.load(configfiles.scan().plugins)
        emitter = plugins._emitter  # pyright: ignore[reportPrivateUsage]
        assert emitter is not None
        assert emitter.calls == []

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


class TestHandlerFailures:
    """What a plugin's handler raises is reported as aws reports an Exception."""

    @pytest.mark.parametrize(
        "raised",
        [
            "AssertionError('plugin assert')",
            "type('Stop', (BaseException,), {})('plugin assert')",
            "GeneratorExit('plugin assert')",
        ],
        ids=["assertion", "base-exception", "generator-exit"],
    )
    def test_a_handler_s_failure_is_the_general_report(
        self,
        config: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        raised: str,
    ) -> None:
        # aws: `aws: [ERROR]: plugin assert` at 255 for an AssertionError
        # (measured); a bare one is this CLI's own invariant, so a plugin's
        # surfaced as a traceback. Outside the Exception family aws ends at
        # rc 0 with nothing written - a failure reported as a success - and
        # here it is the same report as an Exception.
        _write_plugin(
            config,
            "bs3plug_fail.py",
            f"def _h(**kwargs):\n    raise {raised}\n"
            "def awscli_initialize(cli):\n    cli.register('before-call.s3', _h)\n",
        )
        _write_config(config, "mine = bs3plug_fail")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        rc = cli.main(["ls", "s3://bkt", "--endpoint-url", "http://127.0.0.1:9"])
        assert rc == 255
        assert capsys.readouterr().err == "boto3-s3: [ERROR]: plugin assert\n"

    @pytest.mark.parametrize("raised", [KeyboardInterrupt, SystemExit])
    def test_an_interrupt_or_exit_passes_unchanged(
        self, config: Path, raised: type[BaseException]
    ) -> None:
        _write_plugin(
            config,
            "bs3plug_pass.py",
            f"def _h(**kwargs):\n    raise {raised.__name__}\n"
            "def awscli_initialize(cli):\n    cli.register('bs3-event', _h)\n",
        )
        _write_config(config, "mine = bs3plug_pass")
        plugins.load(configfiles.scan().plugins)
        from botocore.hooks import HierarchicalEmitter

        emitter = HierarchicalEmitter()
        plugins.attach(emitter)
        with pytest.raises(raised):
            emitter.emit("bs3-event")

    def test_an_unregister_reaches_the_handler_its_register_put_there(self, config: Path) -> None:
        # The replay wraps each handler once, so the plugin's unregister finds
        # what its register left on the session.
        _write_plugin(
            config,
            "bs3plug_unreg.py",
            "def _h(**kwargs):\n    raise AssertionError('still here')\n"
            "def awscli_initialize(cli):\n"
            "    cli.register('bs3-event', _h)\n"
            "    cli.unregister('bs3-event', _h)\n",
        )
        _write_config(config, "mine = bs3plug_unreg")
        plugins.load(configfiles.scan().plugins)
        from botocore.hooks import HierarchicalEmitter

        emitter = HierarchicalEmitter()
        plugins.attach(emitter)
        assert emitter.emit("bs3-event") == []


def _parse_globals(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    globalargs.add_common_arguments(parser)
    return parser.parse_args(argv)
