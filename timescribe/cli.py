"""Temporary CLI for testing the PSA adapter. Real app UI comes in Phase 2."""
from __future__ import annotations
import argparse
import json
import sys

from timescribe.psa.halo import HaloPSAAdapter


def cmd_connect(args):
    a = HaloPSAAdapter(base_url=args.base_url, client_id=args.client_id)
    a.connect()
    print(f"Connected. Refresh token stored in keyring under service 'timescribe.halo'.")


def cmd_test_list(args):
    a = HaloPSAAdapter(base_url=args.base_url, client_id=args.client_id)
    if not a.is_authenticated():
        print("Not authenticated. Run: pad connect --client-id <id> --base-url <url>")
        sys.exit(2)
    tickets = a.list_open_tickets(agent_id=args.agent_id)
    print(f"Fetched {len(tickets)} open tickets")
    for t in tickets[:10]:
        print(f"  #{t.id}  [{t.client}]  [{t.status}]  {t.subject[:80]}")


def cmd_app(args):
    from timescribe.app import main as app_main
    app_main()


def main():
    p = argparse.ArgumentParser(prog="timescribe", description="TimeScribe Desktop (dev CLI)")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", required=True, help="e.g. https://yourcompany.halopsa.com")
    common.add_argument("--client-id", required=True, help="Halo OAuth application Client ID")

    p_c = sub.add_parser("connect", parents=[common], help="Run OAuth PKCE flow (opens browser)")
    p_c.set_defaults(func=cmd_connect)

    p_l = sub.add_parser("test-list", parents=[common], help="Fetch open tickets to smoke-test the adapter")
    p_l.add_argument("--agent-id", type=int, default=None)
    p_l.set_defaults(func=cmd_test_list)

    p_a = sub.add_parser("app", help="Launch the desktop app (tray + dashboard)")
    p_a.set_defaults(func=cmd_app)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
