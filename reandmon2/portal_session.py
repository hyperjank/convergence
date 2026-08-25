"""Encapsulated PipeWire portal session management."""
from __future__ import annotations

from typing import Optional, Tuple

try:
    from .portal import create_session, select_sources, start_session, close_session
except ImportError:
    from portal import create_session, select_sources, start_session, close_session


class PortalSession:
    """Manage the lifetime of a screen-cast portal session."""

    def __init__(self) -> None:
        self._session_handle: Optional[str] = None
        self._node_id: Optional[str] = None
        self._resolution: Tuple[Optional[int], Optional[int]] = (None, None)

    @property
    def node_id(self) -> Optional[str]:
        return self._node_id

    @property
    def resolution(self) -> Tuple[Optional[int], Optional[int]]:
        return self._resolution

    def start(self) -> Tuple[str, int, int]:
        """Create and start a new PipeWire capture session."""
        session = create_session()
        select_sources(session)
        node_id, width, height = start_session(session)

        self._session_handle = session
        self._node_id = node_id
        self._resolution = (width, height)
        return node_id, width, height

    def stop(self) -> None:
        """Close the portal session if it is active."""
        if self._session_handle:
            try:
                close_session(self._session_handle)
            finally:
                self._session_handle = None
                self._node_id = None
                self._resolution = (None, None)

    def active(self) -> bool:
        return self._session_handle is not None
