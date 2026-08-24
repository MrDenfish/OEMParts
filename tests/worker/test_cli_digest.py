"""CLI wiring test for the digest subcommand."""

from app.worker.cli import build_parser


def test_digest_subcommand_parses() -> None:
    args = build_parser().parse_args(["digest"])
    assert args.func.__name__ == "cmd_digest"
