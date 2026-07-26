"""The lazy command table must not drift from the command classes.

Stage 1 renders the top-level ``--help`` and validates the subcommand name
from ``cli._COMMAND_TABLE`` alone - without importing any command module - so
the table duplicates each class's ``name`` / ``help`` on purpose
(design/imports.md section 2 item 4). These cases import everything and pin the
two sides against each other, and pin stage 1's rendering against the full
tree's.
"""

from __future__ import annotations

from boto3_s3_cli import cli


class TestCommandTable:
    def test_table_covers_the_documented_commands(self) -> None:
        # Literal pin (design/cli.md section 1): the class-consistency test
        # below reads the same table it checks, so a silently dropped command
        # would still pass it.
        assert sorted(cli._COMMAND_TABLE) == [
            "cp",
            "ls",
            "mb",
            "mv",
            "presign",
            "rb",
            "rm",
            "sync",
            "website",
        ]

    def test_table_matches_command_classes(self) -> None:
        for name, (module_name, class_name, help_text) in cli._COMMAND_TABLE.items():
            command_cls = cli._load_command(name)
            assert command_cls.__module__ == module_name
            assert command_cls.__name__ == class_name
            assert command_cls.name == name
            assert command_cls.help == help_text

    def test_stage1_help_matches_full_tree_help(self) -> None:
        # The stub tree and the full tree must render byte-identical top-level
        # help: the user cannot tell the lazy dispatch from the eager build.
        assert cli._build_stage1_parser().format_help() == cli.build_parser().format_help()

    def test_help_displays_the_subcommand_placeholder(self) -> None:
        # The displayed name is `<subcommand>`, matching what this command's
        # level is called in aws (`aws s3 <subcommand>`) and what the usage
        # block in every top-level error says. The rejection message says a
        # bare `subcommand` instead - argparse names a positional after its
        # dest - which TestTopLevelErrorText pins.
        for parser in (cli._build_stage1_parser(), cli.build_parser()):
            help_text = parser.format_help()
            assert "<subcommand> ...\n" in help_text
            assert "<command>" not in help_text
