"""Tests for backend/main.py API endpoints."""
import pytest


def test_get_catalog_returns_list() -> None:
    """GET /catalog returns a JSON list."""
    pytest.skip("not implemented")


def test_get_object_history_returns_list() -> None:
    """GET /object/{norad_id}/history returns a JSON list."""
    pytest.skip("not implemented")


def test_websocket_connects() -> None:
    """WebSocket /ws/live accepts a connection."""
    pytest.skip("not implemented")


def test_websocket_receives_state_update() -> None:
    """Connected WebSocket receives state_update messages."""
    pytest.skip("not implemented")
