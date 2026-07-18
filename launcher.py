"""Frozen entry point.

    TimeScribe.exe          -> desktop app (tray + dashboard)
    TimeScribe.exe mcp      -> stdio MCP server (for Claude Desktop/Cowork)
"""
import sys

if len(sys.argv) > 1 and sys.argv[1] == "mcp":
    from timescribe.mcp_server import main as mcp_main
    mcp_main()
else:
    from timescribe.app import main
    main()
