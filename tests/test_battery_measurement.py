import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "measure-battery.py"
SPEC = importlib.util.spec_from_file_location("measure_battery", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
measure_battery = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = measure_battery
SPEC.loader.exec_module(measure_battery)


def write_supply(root, name, **values):
    supply = Path(root) / name
    supply.mkdir(parents=True, exist_ok=True)
    for key, value in values.items():
        (supply / key).write_text(str(value), encoding="ascii")
    return supply


class FakeClock:
    def __init__(self):
        self.boottime = 0.0
        self.monotonic = 0.0

    def __call__(self):
        return measure_battery.ClockReading(self.boottime, self.monotonic)

    def advance(self, seconds, *, monotonic_seconds=None):
        self.boottime += seconds
        self.monotonic += seconds if monotonic_seconds is None else monotonic_seconds


class SequenceSleeper:
    def __init__(self, clock, updates):
        self.clock = clock
        self.updates = list(updates)
        self.index = 0

    def __call__(self, seconds):
        self.clock.advance(seconds)
        if self.index < len(self.updates):
            self.updates[self.index]()
            self.index += 1


class BatteryMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "power_supply"
        self.root.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_energy_and_power_sysfs_units_are_converted_and_reported(self):
        battery = write_supply(
            self.root,
            "BAT0",
            type="Battery",
            status="Discharging",
            energy_now=50_000_000,
            power_now=10_000_000,
        )
        clock = FakeClock()
        energy_values = iter((49_950_000, 49_900_000))

        def update():
            (battery / "energy_now").write_text(str(next(energy_values)), encoding="ascii")

        report = measure_battery.collect_measurement(
            self.root,
            sample_count=3,
            interval_seconds=5,
            clock=clock,
            sleeper=SequenceSleeper(clock, (update, update)),
        )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["battery"], "BAT0")
        self.assertEqual(report["valid_intervals"], 2)
        self.assertAlmostEqual(report["measured_average_watts"], 36.0)
        self.assertEqual(report["samples"][0]["energy_now"], 50_000_000)
        self.assertEqual(report["samples"][0]["energy_now_unit"], "uWh")
        self.assertEqual(report["samples"][0]["power_now_unit"], "uW")
        self.assertEqual(report["samples"][0]["energy_source"], "energy_now")

    def test_charge_and_voltage_are_converted_to_energy_when_energy_now_is_absent(self):
        battery = write_supply(
            self.root,
            "BAT0",
            type="Battery",
            status="Discharging",
            charge_now=5_000_000,
            voltage_now=12_000_000,
        )
        clock = FakeClock()
        charge_values = iter((4_995_000, 4_990_000))

        def update():
            (battery / "charge_now").write_text(str(next(charge_values)), encoding="ascii")

        report = measure_battery.collect_measurement(
            self.root,
            sample_count=3,
            interval_seconds=5,
            clock=clock,
            sleeper=SequenceSleeper(clock, (update, update)),
        )

        self.assertEqual(report["samples"][0]["energy_now"], None)
        self.assertEqual(report["samples"][0]["charge_now_unit"], "uAh")
        self.assertEqual(report["samples"][0]["voltage_now_unit"], "uV")
        self.assertEqual(report["samples"][0]["energy_source"], "charge_now*voltage_now")
        self.assertAlmostEqual(report["samples"][0]["energy_wh"], 60.0)
        self.assertAlmostEqual(report["measured_average_watts"], 43.2)
        self.assertEqual(
            report["intervals"][0]["energy_basis"],
            "delta_charge_times_mean_voltage",
        )

    def test_source_change_retains_rejected_interval_without_nan(self):
        battery = write_supply(
            self.root, "BAT0", type="Battery", status="Discharging",
            charge_now=5_000_000, voltage_now=12_000_000,
        )
        clock = FakeClock()

        def change_source():
            (battery / "charge_now").unlink()
            (battery / "voltage_now").unlink()
            (battery / "energy_now").write_text("59900000")

        def discharge():
            (battery / "energy_now").write_text("59850000")

        report = measure_battery.collect_measurement(
            self.root, sample_count=3, interval_seconds=5, clock=clock,
            sleeper=SequenceSleeper(clock, (change_source, discharge)),
        )
        self.assertEqual(report["intervals"][0]["reason"], "energy_source_changed")
        self.assertIsNone(report["intervals"][0]["energy_drop_wh"])
        self.assertEqual(report["valid_duration_seconds"], 5)
        json.dumps(report, allow_nan=False)

    def test_fallback_voltage_sag_without_charge_drop_cannot_look_like_drain(self):
        battery = write_supply(
            self.root,
            "BAT0",
            type="Battery",
            status="Discharging",
            charge_now=5_000_000,
            voltage_now=12_000_000,
        )
        clock = FakeClock()
        voltage_values = iter((11_800_000, 11_600_000))

        def update():
            (battery / "voltage_now").write_text(str(next(voltage_values)), encoding="ascii")

        with self.assertRaises(measure_battery.MeasurementError) as raised:
            measure_battery.collect_measurement(
                self.root,
                sample_count=3,
                interval_seconds=5,
                clock=clock,
                sleeper=SequenceSleeper(clock, (update, update)),
            )

        error = raised.exception
        self.assertEqual(error.code, "no_positive_energy")
        self.assertEqual([interval["valid"] for interval in error.intervals], [True, True])
        self.assertEqual([interval["energy_drop_wh"] for interval in error.intervals], [0.0, 0.0])
        self.assertTrue(
            all(
                interval["energy_basis"] == "delta_charge_times_mean_voltage"
                for interval in error.intervals
            )
        )
        self.assertNotIn("measured_average_watts", measure_battery._error_payload(error))

    def test_staircase_energy_gauge_keeps_flat_discharging_time(self):
        battery = write_supply(
            self.root,
            "BAT0",
            type="Battery",
            status="Discharging",
            energy_now=50_000_000,
        )
        clock = FakeClock()
        energy_values = iter((50_000_000, 49_950_000, 49_950_000))

        def update():
            (battery / "energy_now").write_text(str(next(energy_values)), encoding="ascii")

        report = measure_battery.collect_measurement(
            self.root,
            sample_count=4,
            interval_seconds=5,
            clock=clock,
            sleeper=SequenceSleeper(clock, (update, update, update)),
        )

        self.assertEqual(report["valid_intervals"], 3)
        self.assertAlmostEqual(report["valid_duration_seconds"], 15.0)
        self.assertAlmostEqual(report["energy_consumed_wh"], 0.05)
        self.assertAlmostEqual(report["measured_average_watts"], 12.0)
        self.assertEqual(
            [interval["average_watts"] for interval in report["intervals"]],
            [0.0, 36.0, 0.0],
        )

    def test_intervals_during_ac_charging_are_excluded(self):
        battery = write_supply(
            self.root,
            "BAT0",
            type="Battery",
            status="Discharging",
            energy_now=50_000_000,
        )
        ac = write_supply(self.root, "AC", type="Mains", online=0)
        clock = FakeClock()
        energy_values = iter((49_950_000, 49_900_000, 49_850_000))

        def charging_update():
            (battery / "energy_now").write_text(str(next(energy_values)), encoding="ascii")
            (battery / "status").write_text("Charging", encoding="ascii")
            (ac / "online").write_text("1", encoding="ascii")

        def discharge_update():
            (battery / "energy_now").write_text(str(next(energy_values)), encoding="ascii")
            (battery / "status").write_text("Discharging", encoding="ascii")
            (ac / "online").write_text("0", encoding="ascii")

        report = measure_battery.collect_measurement(
            self.root,
            sample_count=4,
            interval_seconds=5,
            clock=clock,
            sleeper=SequenceSleeper(
                clock,
                (charging_update, discharge_update, discharge_update),
            ),
        )

        self.assertEqual(report["valid_intervals"], 1)
        self.assertAlmostEqual(report["measured_average_watts"], 36.0)
        self.assertEqual([interval["valid"] for interval in report["intervals"]], [False, False, True])
        self.assertTrue(all("average_watts" in interval for interval in report["intervals"] if interval["valid"]))
        self.assertTrue(all("average_watts" not in interval for interval in report["intervals"] if not interval["valid"]))

    def test_suspend_resume_gap_is_not_counted_as_discharge(self):
        battery = write_supply(
            self.root,
            "BAT0",
            type="Battery",
            status="Discharging",
            energy_now=50_000_000,
        )
        clock = FakeClock()

        def resume_update(seconds):
            (battery / "energy_now").write_text("49950000", encoding="ascii")
            clock.boottime += seconds + 15
            clock.monotonic += seconds

        with self.assertRaises(measure_battery.MeasurementError) as raised:
            measure_battery.collect_measurement(
                self.root,
                sample_count=2,
                interval_seconds=5,
                clock=clock,
                sleeper=resume_update,
            )

        self.assertEqual(raised.exception.code, "no_valid_discharging_intervals")
        self.assertEqual(raised.exception.intervals[0]["reason"], "suspend_resume_gap")
        self.assertNotIn("measured_average_watts", measure_battery._error_payload(raised.exception))

    def test_disappearing_battery_fails_without_partial_success(self):
        battery = write_supply(
            self.root,
            "BAT0",
            type="Battery",
            status="Discharging",
            energy_now=50_000_000,
        )
        clock = FakeClock()

        def remove_battery(seconds):
            clock.advance(seconds)
            shutil.rmtree(battery)

        with self.assertRaises(measure_battery.MeasurementError) as raised:
            measure_battery.collect_measurement(
                self.root,
                sample_count=2,
                interval_seconds=5,
                clock=clock,
                sleeper=remove_battery,
            )

        error = raised.exception
        self.assertEqual(error.code, "battery_disappeared")
        self.assertEqual(len(error.samples), 1)
        self.assertNotIn("measured_average_watts", measure_battery._error_payload(error))

    def test_missing_battery_cli_returns_json_error_without_zero_success(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = measure_battery.main(
                ["--sysfs-root", str(self.root), "--samples", "2", "--interval", "0"]
            )

        self.assertEqual(exit_code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error"], "no_battery")
        self.assertNotIn("measured_average_watts", payload)


if __name__ == "__main__":
    unittest.main()
