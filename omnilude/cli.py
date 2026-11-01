import argparse
import sys


def parser():
    result = argparse.ArgumentParser(
        prog="omnilude", description="Local research protocols and evidence"
    )
    commands = result.add_subparsers(dest="command")
    commands.add_parser(
        "research", help="manage protocols, local runs and evidence"
    )
    commands.add_parser("mcp", help="serve research tools over STDIO")
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "mcp":
        argparse.ArgumentParser(
            prog="omnilude mcp",
            description="Serve research tools over STDIO",
        ).parse_args(argv[1:])
        from .mcp import main as mcp_main

        return mcp_main()
    if argv and argv[0] == "research":
        from .research_cli import main as research_main

        return research_main(argv[1:])
    arguments = parser()
    arguments.parse_args(argv)
    arguments.print_help()
    return 0
