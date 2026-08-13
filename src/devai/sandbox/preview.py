"""What the agent built, made lookable-at.

A sandbox run that produces an app produces something no score captures. The
preview finds the ports the run opened inside its own pod and serves them back
through the API, so a human can open the thing before anything merges.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# The workspace's own HTTP surface, plus the egress proxy — infrastructure, not
# something the agent built.
WORKSPACE_INTERNAL_PORTS = frozenset({8100, 8118})

_LISTEN = "0A"  # TCP_LISTEN in /proc/net/tcp
_PROC_TABLES = ("/proc/net/tcp", "/proc/net/tcp6")


def listening_ports(table: str) -> list[int]:
    """Ports in LISTEN state in a ``/proc/net/tcp`` table, infrastructure aside."""
    ports: set[int] = set()
    for line in table.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != _LISTEN:
            continue
        _, _, hexport = fields[1].rpartition(":")
        try:
            port = int(hexport, 16)
        except ValueError:
            continue
        if port not in WORKSPACE_INTERNAL_PORTS:
            ports.add(port)
    return sorted(ports)


def detect_ports() -> list[int]:
    """Read this pod's own listening ports; an unreadable table means none."""
    ports: set[int] = set()
    for table in _PROC_TABLES:
        try:
            ports.update(listening_ports(Path(table).read_text()))
        except OSError:
            continue
    return sorted(ports)


__all__ = ["WORKSPACE_INTERNAL_PORTS", "detect_ports", "listening_ports"]
