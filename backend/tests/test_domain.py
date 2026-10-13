from pathlib import Path
import sys
from datetime import datetime, timezone
from decimal import Decimal
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.schemas import classify
from app.main import api_json


def test_normal_warning_critical_boundaries():
    assert classify(34.999, 35, 100) == "NORMAL"
    assert classify(35, 35, 100) == "WARNING"
    assert classify(99.999, 35, 100) == "WARNING"
    assert classify(100, 35, 100) == "CRITICAL"


def test_zone_status_depends_on_latest_reading_not_incident_resolution():
    # Resolving an incident records its lifecycle state; it does not change the reading.
    assert classify(108, 35, 100) == "CRITICAL"
    assert classify(12, 35, 100) == "NORMAL"


def test_zone_thresholds_are_gas_specific():
    schema = Path(__file__).resolve().parents[2] / "database" / "migrations" / "001_initial.sql"
    text = schema.read_text(encoding="utf-8")
    assert ",'ppm',35,100" in text  # CO
    assert ",'% LEL',10,20" in text  # CH4 uses percent LEL, not ppm
    assert ",'ppm',10,20" in text  # H2S
    assert ",'ppm',25,50" in text  # NH3


def test_api_json_serializes_database_decimals_and_timestamps():
    payload = api_json({
        "warning_threshold": Decimal("35.000"),
        "current_reading": Decimal("36.125"),
        "last_updated": datetime(2026, 1, 2, tzinfo=timezone.utc),
    })

    assert payload["warning_threshold"] == 35.0
    assert isinstance(payload["warning_threshold"], float)
    assert payload["current_reading"] == 36.125
    assert payload["last_updated"] == "2026-01-02T00:00:00+00:00"
    assert json.loads(json.dumps(payload)) == payload
