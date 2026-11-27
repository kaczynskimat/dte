import json
import unittest

from src.state_bridge.quality import (
    BOOLEAN_FIELDS,
    PERIODIC_FIELDS,
    PHYSICAL_RANGES,
    evaluate_quality,
)
from src.validation.fidelity import FidelityReport, compare_snapshot


def valid_telemetry() -> dict[str, object]:
    telemetry: dict[str, object] = {}
    for field in PERIODIC_FIELDS:
        if field in BOOLEAN_FIELDS:
            telemetry[field] = 0
        else:
            lower, upper = PHYSICAL_RANGES[field]
            telemetry[field] = (lower + upper) / 2
    return telemetry


class QualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.telemetry = valid_telemetry()
        self.sensor_times = {field: 100 for field in PERIODIC_FIELDS}

    def test_complete_current_snapshot_is_valid(self) -> None:
        result = evaluate_quality(self.telemetry, self.sensor_times, 100)

        self.assertEqual(result["quality_status"], "valid")
        self.assertEqual(json.loads(result["quality_flags"]), [])
        self.assertEqual(result["quality_missing_count"], 0)
        self.assertEqual(result["quality_stale_count"], 0)
        self.assertEqual(result["quality_invalid_count"], 0)

    def test_missing_gnss_is_reported_as_invalid(self) -> None:
        del self.telemetry["latitude"]

        result = evaluate_quality(self.telemetry, self.sensor_times, 100)

        self.assertEqual(result["quality_status"], "invalid")
        self.assertIn("missing:latitude", json.loads(result["quality_flags"]))

    def test_previous_tick_sensor_is_reported_as_stale(self) -> None:
        self.sensor_times["speed_kmh"] = 99

        result = evaluate_quality(self.telemetry, self.sensor_times, 100)

        self.assertEqual(result["quality_status"], "warning")
        self.assertIn("stale:speed_kmh", json.loads(result["quality_flags"]))

    def test_out_of_range_value_is_reported_as_invalid(self) -> None:
        self.telemetry["heading_deg"] = 999

        result = evaluate_quality(self.telemetry, self.sensor_times, 100)

        self.assertEqual(result["quality_status"], "invalid")
        self.assertIn("out_of_range:heading_deg", json.loads(result["quality_flags"]))

    def test_event_fields_are_not_treated_as_periodic(self) -> None:
        self.telemetry.pop("itcs_stopName", None)
        self.telemetry.pop("itcs_busRoute", None)

        result = evaluate_quality(self.telemetry, self.sensor_times, 100)

        self.assertEqual(result["quality_status"], "valid")


class FidelityTests(unittest.TestCase):
    def test_snapshot_comparison_reports_missing_and_mismatched_fields(self) -> None:
        expected = valid_telemetry()
        actual = {field: str(value) for field, value in expected.items()}
        del actual["latitude"]
        actual["speed_kmh"] = "999"

        missing, mismatched = compare_snapshot(expected, actual)

        self.assertEqual(missing, ["latitude"])
        self.assertEqual(mismatched, ["speed_kmh"])

    def test_report_calculates_match_rate_and_latency(self) -> None:
        report = FidelityReport()
        report.record([], [], 2.0, "valid")
        report.record(["latitude"], ["speed_kmh"], 4.0, "invalid")

        summary = report.as_dict()

        self.assertEqual(summary["snapshots"], 2)
        self.assertEqual(summary["field_comparisons"], 44)
        self.assertEqual(summary["matching_fields"], 42)
        self.assertEqual(summary["match_rate_percent"], 95.4545)
        self.assertEqual(summary["invalid_snapshots"], 1)
        self.assertEqual(summary["latency_mean_ms"], 3.0)
        self.assertEqual(summary["latency_max_ms"], 4.0)


if __name__ == "__main__":
    unittest.main()
