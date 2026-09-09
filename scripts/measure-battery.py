#!/usr/bin/env python3
"""Measure bounded physical battery discharge from Linux power-supply sysfs.

The command only reads the power-supply class.  It deliberately needs a
decreasing energy value and a positive elapsed interval before it reports a
power measurement; a missing battery, charger interval, clock gap, or flat
gauge cannot turn into a successful zero-watt result.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys
import time
from typing import Any, Callable, Iterable, Optional


DEFAULT_DURATION_SECONDS = 60.0
DEFAULT_INTERVAL_SECONDS = 5.0
MAX_DURATION_SECONDS = 3600.0
MAX_SAMPLES = 721
RESUME_TOLERANCE_SECONDS = 1.0

MICRO = 1_000_000.0
MICRO_SQUARED = MICRO * MICRO

UNITS = {
    "energy_now": "uWh",
    "power_now": "uW",
    "charge_now": "uAh",
    "voltage_now": "uV",
}


class MeasurementError(RuntimeError):
    """An expected measurement failure which can be represented as JSON."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}
        self.samples: list[dict[str, Any]] = []
        self.intervals: list[dict[str, Any]] = []


@dataclass(frozen=True)
class ClockReading:
    """A pair of clocks used to identify suspend/resume gaps."""

    boottime_seconds: float
    monotonic_seconds: float


@dataclass
class BatterySample:
    clock: ClockReading
    battery: str
    status: Optional[str]
    ac_online: Optional[bool]
    ac_sources: list[dict[str, Any]]
    energy_wh: float
    energy_source: str
    energy_now: Optional[int | float]
    charge_now: Optional[int | float]
    voltage_now: Optional[int | float]
    power_now: Optional[int | float]

    def as_dict(self, first_clock: ClockReading) -> dict[str, Any]:
        """Return raw sysfs values plus canonical values for JSON output."""

        elapsed = self.clock.boottime_seconds - first_clock.boottime_seconds
        return {
            "elapsed_seconds": _rounded(elapsed),
            "battery": self.battery,
            "status": self.status,
            "ac_online": self.ac_online,
            "ac_sources": self.ac_sources,
            "energy_now": self.energy_now,
            "energy_now_unit": UNITS["energy_now"] if self.energy_now is not None else None,
            "charge_now": self.charge_now,
            "charge_now_unit": UNITS["charge_now"] if self.charge_now is not None else None,
            "voltage_now": self.voltage_now,
            "voltage_now_unit": UNITS["voltage_now"] if self.voltage_now is not None else None,
            "power_now": self.power_now,
            "power_now_unit": UNITS["power_now"] if self.power_now is not None else None,
            "energy_wh": _rounded(self.energy_wh, 9),
            "energy_source": self.energy_source,
        }


def _rounded(value: float, digits: int = 6) -> float:
    """Round finite numeric output while keeping JSON free of NaN/Infinity."""

    if not math.isfinite(value):
        raise MeasurementError("invalid_numeric_value", "A measured value was not finite")
    return round(value, digits)


def _default_clock() -> ClockReading:
    monotonic = time.monotonic()
    if hasattr(time, "CLOCK_BOOTTIME"):
        try:
            boottime = time.clock_gettime(time.CLOCK_BOOTTIME)
        except (AttributeError, OSError):
            boottime = monotonic
    else:
        boottime = monotonic
    return ClockReading(float(boottime), float(monotonic))


def _coerce_clock(value: ClockReading | float | int | tuple[float, float]) -> ClockReading:
    """Accept a simple numeric clock in tests while using both clocks in production."""

    if isinstance(value, ClockReading):
        reading = value
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        reading = ClockReading(float(value[0]), float(value[1]))
    else:
        numeric = float(value)
        reading = ClockReading(numeric, numeric)
    if not (
        math.isfinite(reading.boottime_seconds)
        and math.isfinite(reading.monotonic_seconds)
    ):
        raise MeasurementError("clock_unavailable", "The sampling clock returned a non-finite value")
    return reading


def _read_text(path: Path) -> Optional[str]:
    """Read one sysfs value, treating a racing removal as missing."""

    try:
        return path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as error:
        raise MeasurementError(
            "battery_unreadable",
            f"Cannot decode power-supply attribute {path.name}",
        ) from error
    except OSError as error:
        raise MeasurementError(
            "battery_unreadable",
            f"Cannot read power-supply attribute {path.name}",
        ) from error


def _parse_number(text: str, field: str, *, allow_negative: bool = False) -> int | float:
    """Parse the bare numeric representation used by power-supply sysfs."""

    try:
        number = float(text)
    except (TypeError, ValueError) as error:
        raise MeasurementError(
            "battery_unreadable",
            f"Power-supply attribute {field} is not numeric",
        ) from error
    if not math.isfinite(number):
        raise MeasurementError(
            "battery_unreadable",
            f"Power-supply attribute {field} is not finite",
        )
    if not allow_negative and number < 0:
        raise MeasurementError(
            "battery_unreadable",
            f"Power-supply attribute {field} is negative",
        )
    if number.is_integer():
        return int(number)
    return number


def _read_number(
    path: Path,
    field: str,
    *,
    allow_missing: bool = True,
    allow_negative: bool = False,
) -> Optional[int | float]:
    text = _read_text(path)
    if text is None:
        if allow_missing:
            return None
        raise MeasurementError(
            "battery_unreadable",
            f"Required power-supply attribute {field} is missing",
        )
    return _parse_number(text, field, allow_negative=allow_negative)


def _try_read_number(
    path: Path,
    field: str,
    *,
    allow_negative: bool = False,
) -> tuple[Optional[int | float], Optional[str]]:
    """Read an optional attribute without hiding a valid alternate source."""

    try:
        return _read_number(path, field, allow_negative=allow_negative), None
    except MeasurementError as error:
        return None, str(error)


def _canonical_status(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = " ".join(value.split()).casefold()
    known = {
        "charging": "Charging",
        "discharging": "Discharging",
        "full": "Full",
        "not charging": "Not charging",
        "unknown": "Unknown",
    }
    return known.get(normalized, value.strip() or None)


def _parse_online(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "online"}:
        return True
    if normalized in {"0", "false", "no", "offline"}:
        return False
    raise ValueError(value)


class BatteryMonitor:
    """Read one stable, energy-reporting Battery power-supply directory."""

    def __init__(self, root: Path | str, battery_name: Optional[str] = None) -> None:
        self.root = Path(root)
        self.requested_battery = battery_name
        self.battery_path: Optional[Path] = None
        self.battery_name: Optional[str] = None

    def _supply_dirs(self) -> list[Path]:
        if not self.root.exists() or not self.root.is_dir():
            raise MeasurementError(
                "power_supply_unavailable",
                f"Power-supply directory is unavailable: {self.root}",
            )
        try:
            return sorted(path for path in self.root.iterdir() if path.is_dir())
        except OSError as error:
            raise MeasurementError(
                "power_supply_unavailable",
                "Cannot enumerate the power-supply directory",
            ) from error

    @staticmethod
    def _supply_type(path: Path) -> Optional[str]:
        value = _read_text(path / "type")
        return value.strip() if value else None

    def _battery_dirs(self) -> list[Path]:
        battery_dirs = []
        for path in self._supply_dirs():
            supply_type = self._supply_type(path)
            if supply_type and supply_type.casefold() == "battery":
                battery_dirs.append(path)
        return battery_dirs

    @staticmethod
    def _energy_data(path: Path) -> tuple[
        float,
        str,
        Optional[int | float],
        Optional[int | float],
        Optional[int | float],
    ]:
        """Return energy in Wh from energy_now or charge_now*voltage_now."""

        energy_now, energy_error = _try_read_number(path / "energy_now", "energy_now")
        charge_now, charge_error = _try_read_number(path / "charge_now", "charge_now")
        voltage_now, voltage_error = _try_read_number(path / "voltage_now", "voltage_now")

        if energy_now is not None:
            energy_wh = float(energy_now) / MICRO
            if math.isfinite(energy_wh) and energy_wh >= 0:
                return energy_wh, "energy_now", energy_now, charge_now, voltage_now
            energy_error = "energy_now is outside the supported range"

        if charge_now is not None and voltage_now is not None and float(voltage_now) > 0:
            energy_wh = float(charge_now) * float(voltage_now) / MICRO_SQUARED
            if math.isfinite(energy_wh) and energy_wh >= 0:
                return (
                    energy_wh,
                    "charge_now*voltage_now",
                    None,
                    charge_now,
                    voltage_now,
                )

        available = {
            "energy_now": energy_error or "missing",
            "charge_now": charge_error or "missing",
            "voltage_now": voltage_error or "missing",
        }
        raise MeasurementError(
            "battery_data_unavailable",
            "Battery has no usable energy_now or charge_now plus voltage_now",
            details={"attributes": available},
        )

    def _select_battery(self) -> Path:
        battery_dirs = self._battery_dirs()
        if not battery_dirs:
            raise MeasurementError(
                "no_battery",
                "No Battery power-supply device is available",
            )

        if self.requested_battery is not None:
            requested = self.root / self.requested_battery
            battery_dirs = [path for path in battery_dirs if path == requested]
            if not battery_dirs:
                raise MeasurementError(
                    "no_battery",
                    f"Requested battery is unavailable: {self.requested_battery}",
                )

        unusable: list[str] = []
        for path in battery_dirs:
            try:
                self._energy_data(path)
            except MeasurementError as error:
                unusable.append(f"{path.name}: {error}")
                continue
            self.battery_path = path
            self.battery_name = path.name
            return path

        raise MeasurementError(
            "battery_data_unavailable",
            "Battery device has no usable energy attributes",
            details={"devices": unusable},
        )

    def _ac_state(self) -> tuple[Optional[bool], list[dict[str, Any]]]:
        """Read online state for AC/USB/etc. supplies, if they expose it."""

        sources: list[dict[str, Any]] = []
        for path in self._supply_dirs():
            if self.battery_path is not None and path == self.battery_path:
                continue
            online_path = path / "online"
            if not online_path.exists():
                continue
            source_type = self._supply_type(path) or "Unknown"
            try:
                online_text = _read_text(online_path)
                online = None if online_text is None else _parse_online(online_text)
            except (MeasurementError, ValueError):
                online = None
            sources.append(
                {
                    "name": path.name,
                    "type": source_type,
                    "online": online,
                }
            )

        if not sources:
            return False, sources
        if any(source["online"] is None for source in sources):
            return None, sources
        return any(bool(source["online"]) for source in sources), sources

    def read_sample(self, clock: ClockReading) -> BatterySample:
        if self.battery_path is None:
            path = self._select_battery()
        else:
            path = self.battery_path
            if not path.exists() or not path.is_dir():
                raise MeasurementError(
                    "battery_disappeared",
                    f"Battery device disappeared during measurement: {self.battery_name}",
                )
            supply_type = self._supply_type(path)
            if not supply_type or supply_type.casefold() != "battery":
                raise MeasurementError(
                    "battery_disappeared",
                    f"Battery device is no longer a Battery supply: {self.battery_name}",
                )

        try:
            energy_wh, energy_source, energy_now, charge_now, voltage_now = self._energy_data(path)
        except MeasurementError:
            if not path.exists() or not path.is_dir():
                raise MeasurementError(
                    "battery_disappeared",
                    f"Battery device disappeared during measurement: {self.battery_name}",
                )
            raise

        status = _canonical_status(_read_text(path / "status"))
        power_now, _power_error = _try_read_number(
            path / "power_now",
            "power_now",
            allow_negative=True,
        )
        ac_online, ac_sources = self._ac_state()
        return BatterySample(
            clock=clock,
            battery=self.battery_name or path.name,
            status=status,
            ac_online=ac_online,
            ac_sources=ac_sources,
            energy_wh=energy_wh,
            energy_source=energy_source,
            energy_now=energy_now,
            charge_now=charge_now,
            voltage_now=voltage_now,
            power_now=power_now,
        )


def _sample_is_discharging(sample: BatterySample) -> bool:
    return sample.status == "Discharging" and sample.ac_online is False


def _energy_drop(previous: BatterySample, current: BatterySample) -> tuple[float, str]:
    """Calculate an interval drop without treating fallback voltage sag as drain."""

    if previous.energy_source == "charge_now*voltage_now":
        if (
            previous.charge_now is None
            or current.charge_now is None
            or previous.voltage_now is None
            or current.voltage_now is None
        ):
            return float("nan"), "delta_charge_times_mean_voltage"
        charge_drop = float(previous.charge_now) - float(current.charge_now)
        mean_voltage = (float(previous.voltage_now) + float(current.voltage_now)) / 2.0
        return (
            charge_drop * mean_voltage / MICRO_SQUARED,
            "delta_charge_times_mean_voltage",
        )
    return previous.energy_wh - current.energy_wh, "delta_energy_now"


def _interval(
    previous: BatterySample,
    current: BatterySample,
    first_clock: ClockReading,
    *,
    expected_interval_seconds: Optional[float],
) -> dict[str, Any]:
    boot_delta = current.clock.boottime_seconds - previous.clock.boottime_seconds
    monotonic_delta = current.clock.monotonic_seconds - previous.clock.monotonic_seconds
    start_elapsed = previous.clock.boottime_seconds - first_clock.boottime_seconds
    end_elapsed = current.clock.boottime_seconds - first_clock.boottime_seconds
    energy_drop, energy_basis = _energy_drop(previous, current)

    reason: Optional[str] = None
    if not math.isfinite(boot_delta) or boot_delta <= 0:
        reason = "non_positive_elapsed"
    elif not math.isfinite(monotonic_delta) or monotonic_delta < 0:
        reason = "clock_went_backwards"
    elif boot_delta - monotonic_delta > RESUME_TOLERANCE_SECONDS:
        reason = "suspend_resume_gap"
    elif (
        expected_interval_seconds is not None
        and expected_interval_seconds > 0
        and boot_delta > max(30.0, expected_interval_seconds * 4.0)
    ):
        reason = "sampling_gap"
    elif previous.battery != current.battery:
        reason = "battery_changed"
    elif previous.energy_source != current.energy_source:
        reason = "energy_source_changed"
    elif not _sample_is_discharging(previous) or not _sample_is_discharging(current):
        reason = "not_discharging_or_ac_online"
    elif not math.isfinite(energy_drop) or energy_drop < 0:
        reason = "energy_increased"

    valid = reason is None
    interval: dict[str, Any] = {
        "start_elapsed_seconds": _rounded(start_elapsed),
        "end_elapsed_seconds": _rounded(end_elapsed),
        "elapsed_seconds": _rounded(boot_delta),
        "energy_drop_wh": _rounded(energy_drop, 9) if math.isfinite(energy_drop) else None,
        "energy_basis": energy_basis,
        "valid": valid,
    }
    if valid:
        interval["average_watts"] = _rounded(energy_drop / boot_delta * 3600.0, 9)
    else:
        interval["reason"] = reason
    return interval


def _validate_options(
    duration_seconds: Optional[float],
    interval_seconds: float,
    sample_count: Optional[int],
) -> None:
    if not math.isfinite(interval_seconds) or interval_seconds < 0:
        raise MeasurementError("invalid_arguments", "Sample interval must be finite and non-negative")
    if sample_count is not None:
        if sample_count < 2 or sample_count > MAX_SAMPLES:
            raise MeasurementError(
                "invalid_arguments",
                f"Sample count must be between 2 and {MAX_SAMPLES}",
            )
        return
    if duration_seconds is None or not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise MeasurementError("invalid_arguments", "Duration must be finite and greater than zero")
    if duration_seconds > MAX_DURATION_SECONDS:
        raise MeasurementError(
            "invalid_arguments",
            f"Duration must be at most {MAX_DURATION_SECONDS:g} seconds",
        )
    if interval_seconds <= 0:
        raise MeasurementError(
            "invalid_arguments",
            "Sample interval must be greater than zero when duration controls the window",
        )
    planned_samples = math.ceil(duration_seconds / interval_seconds) + 1
    if planned_samples > MAX_SAMPLES:
        raise MeasurementError(
            "invalid_arguments",
            f"Requested window would exceed the {MAX_SAMPLES}-sample safety bound",
        )


def collect_measurement(
    root: Path | str = "/sys/class/power_supply",
    *,
    duration_seconds: Optional[float] = DEFAULT_DURATION_SECONDS,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    sample_count: Optional[int] = None,
    battery_name: Optional[str] = None,
    clock: Callable[[], ClockReading | float | int | tuple[float, float]] = _default_clock,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Collect a bounded sample window and return a successful JSON report.

    ``clock`` and ``sleeper`` are injectable solely for deterministic tests;
    production callers use Linux boot time and ``time.sleep``.
    """

    _validate_options(duration_seconds, interval_seconds, sample_count)
    monitor = BatteryMonitor(root, battery_name=battery_name)
    first_clock = _coerce_clock(clock())
    samples: list[BatterySample] = []
    intervals: list[dict[str, Any]] = []

    def public_samples() -> list[dict[str, Any]]:
        return [sample.as_dict(first_clock) for sample in samples]

    def append_sample(sample_clock: ClockReading) -> None:
        sample = monitor.read_sample(sample_clock)
        if samples:
            intervals.append(
                _interval(
                    samples[-1],
                    sample,
                    first_clock,
                    expected_interval_seconds=interval_seconds,
                )
            )
        samples.append(sample)

    try:
        append_sample(first_clock)
        if sample_count is not None:
            for _index in range(1, sample_count):
                if interval_seconds > 0:
                    sleeper(interval_seconds)
                append_sample(_coerce_clock(clock()))
        else:
            assert duration_seconds is not None
            deadline = first_clock.boottime_seconds + duration_seconds
            while True:
                now = _coerce_clock(clock())
                remaining = deadline - now.boottime_seconds
                if remaining <= 0:
                    break
                if len(samples) >= MAX_SAMPLES:
                    raise MeasurementError(
                        "sample_limit_reached",
                        "Sampling stopped at the safety bound before the requested window ended",
                    )
                sleeper(min(interval_seconds, remaining))
                append_sample(_coerce_clock(clock()))
    except MeasurementError as error:
        error.samples = public_samples()
        error.intervals = intervals
        raise

    valid_intervals = [interval for interval in intervals if interval["valid"]]
    if not valid_intervals:
        error = MeasurementError(
            "no_valid_discharging_intervals",
            "No interval had positive elapsed time, decreasing energy, and a discharging battery",
        )
        error.samples = public_samples()
        error.intervals = intervals
        raise error

    total_seconds = sum(float(interval["elapsed_seconds"]) for interval in valid_intervals)
    total_energy_wh = sum(float(interval["energy_drop_wh"]) for interval in valid_intervals)
    if not (math.isfinite(total_seconds) and total_seconds > 0):
        error = MeasurementError(
            "invalid_measurement",
            "Valid intervals did not produce a positive finite elapsed time",
        )
        error.samples = public_samples()
        error.intervals = intervals
        raise error
    if not (math.isfinite(total_energy_wh) and total_energy_wh > 0):
        error = MeasurementError(
            "no_positive_energy",
            "Discharging intervals were observed, but their aggregate energy drop was not positive",
        )
        error.samples = public_samples()
        error.intervals = intervals
        raise error
    measured_average = total_energy_wh / total_seconds * 3600.0
    if not (math.isfinite(measured_average) and measured_average > 0):
        error = MeasurementError(
            "invalid_measurement",
            "Measured average power was not positive and finite",
        )
        error.samples = public_samples()
        error.intervals = intervals
        raise error

    observed_window = samples[-1].clock.boottime_seconds - first_clock.boottime_seconds
    return {
        "schema_version": 1,
        "status": "ok",
        "measurement": "physical_battery_discharge",
        "battery": monitor.battery_name,
        "requested_duration_seconds": (
            _rounded(float(duration_seconds)) if duration_seconds is not None else None
        ),
        "requested_sample_interval_seconds": _rounded(float(interval_seconds)),
        "requested_sample_count": sample_count,
        "observed_window_seconds": _rounded(observed_window),
        "sample_count": len(samples),
        "samples": public_samples(),
        "intervals": intervals,
        "valid_intervals": len(valid_intervals),
        "valid_duration_seconds": _rounded(total_seconds),
        "energy_consumed_wh": _rounded(total_energy_wh, 9),
        "measured_average_watts": _rounded(measured_average, 9),
        "units": {
            "energy_now": "uWh",
            "charge_now": "uAh",
            "voltage_now": "uV",
            "power_now": "uW",
            "derived_energy": "Wh",
            "derived_power": "W",
        },
        "average_basis": "decreasing battery energy divided by valid elapsed time; fallback uses charge delta times mean endpoint voltage",
        "power_now_note": "power_now is retained as a raw instantaneous context value; it is not used for the average",
    }


def _error_payload(error: MeasurementError) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": "error",
        "error": error.code,
        "message": str(error),
    }
    if error.details:
        payload["details"] = error.details
    if error.samples:
        payload["samples"] = error.samples
    if error.intervals:
        payload["intervals"] = error.intervals
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read a physical Linux battery and report bounded discharge power as JSON"
    )
    parser.add_argument(
        "--sysfs-root",
        "--power-supply-root",
        dest="root",
        default="/sys/class/power_supply",
        help="power-supply class directory (default: /sys/class/power_supply)",
    )
    parser.add_argument(
        "--battery",
        dest="battery_name",
        help="specific Battery directory name; otherwise choose the first usable one",
    )
    parser.add_argument(
        "--duration",
        "--window",
        dest="duration_seconds",
        type=float,
        default=DEFAULT_DURATION_SECONDS,
        help=f"sampling window in seconds (default: {DEFAULT_DURATION_SECONDS:g}, max: {MAX_DURATION_SECONDS:g})",
    )
    parser.add_argument(
        "--interval",
        "--sample-interval",
        dest="interval_seconds",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"seconds between samples (default: {DEFAULT_INTERVAL_SECONDS:g})",
    )
    parser.add_argument(
        "--samples",
        dest="sample_count",
        type=int,
        help=f"collect an exact count instead of a duration window (2-{MAX_SAMPLES})",
    )
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        report = collect_measurement(
            args.root,
            duration_seconds=args.duration_seconds,
            interval_seconds=args.interval_seconds,
            sample_count=args.sample_count,
            battery_name=args.battery_name,
        )
    except MeasurementError as error:
        print(json.dumps(_error_payload(error), indent=2, allow_nan=False))
        return 2
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
