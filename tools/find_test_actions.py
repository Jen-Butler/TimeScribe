"""List Halo time entries attributed to the API application ("Test") instead
of the technician, with links, so they can be deleted and re-entered.

Run from the project root with the app's real config:
    python tools/find_test_actions.py [since-date]
"""
import json
import sys

from timescribe import appconfig
from timescribe.server import get_adapter

since = sys.argv[1] if len(sys.argv) > 1 else "2026-09-30"
a = get_adapter()
base = (appconfig.load().get("halo_base_url") or "").rstrip("/")


def rows_of(r, key):
    return r.get(key, r) if isinstance(r, dict) else r


# Candidate tickets: anything touched since `since` (open or closed).
tickets = {}
for params in ({"lastupdatefromdate": since, "pageinate": False, "includeclosed": True},
               {"startdate": since, "pageinate": False, "includeclosed": True},
               {"open_only": True, "pageinate": False, "agent_id": 27}):
    try:
        for t in rows_of(a._api_get("Tickets", params=params), "tickets") or []:
            tickets[t["id"]] = t
    except Exception as exc:
        print("TICKETS_ERR", params, str(exc)[:120])
print("CANDIDATES", len(tickets))

hits = []
for tid in sorted(tickets):
    try:
        acts = rows_of(a._api_get("Actions", params={"ticket_id": tid, "count": 200}), "actions") or []
    except Exception as exc:
        print("ACTIONS_ERR", tid, str(exc)[:100]); continue
    for x in acts:
        if x.get("who") != "Test" or not (x.get("timetaken") or 0) > 0:
            continue
        when = (x.get("datetime") or x.get("actiondatecreated") or "")[:16]
        if when and when < since:
            continue
        hits.append({
            "ticket": tid, "action": x.get("id"), "when": when,
            "hours": round(float(x.get("timetaken") or 0), 2),
            "client": tickets[tid].get("client_name"),
            "subject": (tickets[tid].get("summary") or "")[:60],
            "note": (x.get("note") or "").replace("\n", " ")[:80],
            "link": f"{base}/tickets?id={tid}&actionid={x.get('id')}",
        })
hits.sort(key=lambda h: h["when"])
for h in hits:
    print("HIT", json.dumps(h))
print("TOTAL", len(hits))
