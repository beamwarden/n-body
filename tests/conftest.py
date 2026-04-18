"""Auto-mark tests as unit or integration based on module name."""
import pytest

_INTEGRATION_MODULES = {
    "test_main",
    "test_ingest",
    "test_ingest_n2yo",
    "test_conjunction_endpoint",
    "test_anomaly_history_endpoint",
    "test_track_endpoint",
    "test_replay",
    "test_seed_maneuver",
    "test_seed_conjunction",
}


def pytest_collection_modifyitems(items):
    for item in items:
        module = item.module.__name__.split(".")[-1]
        if module in _INTEGRATION_MODULES:
            item.add_marker(pytest.mark.integration)
        else:
            item.add_marker(pytest.mark.unit)
