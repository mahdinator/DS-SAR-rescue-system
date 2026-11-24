"""Swappable parts of the system, as Python Protocols.

Each Protocol has one implementation in step 1; later steps add alternatives
that the scenario file selects by name.
"""
from __future__ import annotations

from typing import Protocol


class Transport(Protocol):
    """Moves packed envelopes between nodes (step 1: ZeroMQ PUB/SUB bus;
    step 2 adds the emulated radio link model)."""

    def start(self) -> None: ...

    async def send(self, data: bytes) -> None: ...

    async def recv(self) -> bytes: ...


class SectorPlanner(Protocol):
    """Cuts the search area into sectors and gives each a priority."""

    def plan(self, grid, terrain, table, base) -> list: ...


class SensorModel(Protocol):
    """Decides what a drone sees when it passes over a cell."""

    def sense(self, cell, mode, victims_in_cell, rng) -> list: ...
