"""Abstract interface for PSA (Halo, ConnectWise, etc.) integrations.

The rest of the app knows nothing about which PSA is in use -- it only
talks to an instance of PSAAdapter. Each PSA implementation slots in
by satisfying this contract.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict, Any


@dataclass
class Ticket:
    id: int
    client: str
    subject: str
    status: str
    priority: str = ""
    last_action_date: Optional[datetime] = None
    project_id: Optional[int] = None
    recent_actions: List[Dict[str, Any]] = field(default_factory=list)
    raw: Optional[Dict[str, Any]] = None    # keep the source object for edge cases


@dataclass
class TimeEntry:
    """A time entry to create. ticket_id=None means a standalone
    'Quick Time' entry not attached to any ticket."""
    ticket_id: Optional[int]
    start_local: datetime
    end_local: datetime
    note: str
    charge_rate: Optional[float] = None    # rate ID; 0 = no charge
    billable: bool = True
    private_note: Optional[str] = None     # agent-only note on the action


@dataclass
class CalendarEvent:
    id: str
    start_local: datetime
    end_local: datetime
    subject: str
    all_day: bool = False
    ticket_id: Optional[int] = None    # some PSAs link cal events to tickets
    is_private: bool = False


class PSAAdapter(ABC):
    """Every PSA integration implements this."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short display name: 'HaloPSA', 'ConnectWise Manage', ..."""

    @abstractmethod
    def is_authenticated(self) -> bool:
        """Do we have valid credentials right now?"""

    @abstractmethod
    def connect(self) -> None:
        """Run the auth flow (browser OAuth for Halo, key-pair prompt for CW).
        Should be idempotent -- calling on an already-authenticated adapter is a no-op."""

    @abstractmethod
    def list_open_tickets(self, agent_id: Optional[int] = None,
                          include_recent_actions: bool = True) -> List[Ticket]:
        """Return open tickets (optionally filtered to a specific agent)."""

    @abstractmethod
    def create_time_entry(self, entry: TimeEntry) -> str:
        """Post a time entry against a ticket. Returns the created entry's PSA ID."""

    @abstractmethod
    def list_calendar_events(self, from_dt: datetime, to_dt: datetime) -> List[CalendarEvent]:
        """Return calendar events (appointments) between two local datetimes."""

    @abstractmethod
    def create_calendar_event(self, event: CalendarEvent) -> str:
        """Create a calendar event, optionally linked to a ticket. Returns the created event's PSA ID."""
