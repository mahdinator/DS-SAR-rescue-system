"""Ground-station replication: one ordered log of commands, two interchangeable algorithms.

core.py holds the transport-free algorithms (driven by tick() and handle()),
so they can be tested on a simulated network as well as run inside a station.
"""
