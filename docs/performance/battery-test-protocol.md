# Physical battery measurement protocol

`scripts/measure-battery.py` is a bounded, read-only measurement harness for a
real laptop. It reads Linux power-supply class attributes and reports the
average battery-side discharge power during intervals that remain in a
discharging state. It does not estimate runtime, qualify battery life, or
turn a VM result into a hardware result.

## Kernel interface and output contract

The Linux power-supply class exports attributes through
`/sys/class/power_supply/<name>`. The kernel documentation defines these
units:

| Attribute | Kernel unit | Harness use |
| --- | --- | --- |
| `energy_now` | µWh | Preferred remaining-energy reading; converted to Wh. |
| `charge_now` | µAh | Used with `voltage_now` when `energy_now` is unavailable. |
| `voltage_now` | µV | Used with `charge_now`; the product is an estimated Wh value. |
| `power_now` | µW | Preserved as a raw instantaneous context reading; it is not substituted for an energy measurement. |
| `status` | text | An interval must start and end as `Discharging`. |
| `online` on AC/USB supplies | 0 or 1 | An interval with an online or unknown external supply is excluded. |

The preferred sample conversion is `energy_now / 1,000,000`. The fallback
sample conversion is `charge_now * voltage_now / 1,000,000,000,000`; it is
clearly labelled as a derived estimate in the JSON. For fallback intervals,
the measured drop is instead `delta_charge_now * mean(endpoint_voltage_now)`,
so voltage sag with unchanged charge cannot look like energy drain. The
reported value is calculated from valid discharging elapsed time and a
non-negative interval drop; the aggregate drop must be positive:

`measured_average_watts = sum(energy_drop_wh) / sum(valid_elapsed_seconds) * 3600`

Raw samples include the source values, units, normalized energy in Wh, battery
status, charger state and elapsed timestamp. Intervals rejected because of AC
charging, a non-discharging status, an increasing gauge, a clock gap or a
source change remain visible with a rejection reason. A flat gauge interval
with a discharging battery remains valid and contributes its elapsed time with
zero energy, which prevents a staircase gauge from biasing the average high.
Missing attributes are never replaced with zero.

The command exits successfully only when valid intervals contain a positive
aggregate energy drop. No battery, an unreadable or disappearing battery, or a
window with no valid discharging interval produces a JSON error and a non-zero
exit status without `measured_average_watts`. A window containing only flat
discharging intervals also fails because its aggregate energy drop is not
positive; it never becomes a fabricated zero-watt success.

The default 60-second window with 5-second samples is long enough to provide
multiple intervals. A longer window is preferable for comparisons:

```text
python3 scripts/measure-battery.py --duration 300 --interval 5 > battery.json
```

Use the same bounded window and workload for every comparison. The
`--sysfs-root` option exists for disposable fake-sysfs tests; a production run
uses `/sys/class/power_supply` and requires no root access, installer, daemon or
power-policy change.

## Preparation and controls

Record the laptop model, battery identity and reported full-charge capacity,
firmware, kernel, display, refresh rate, power profile, wireless path and
measurement date. Keep the same physical device, firmware, kernel and display
configuration for baseline and Zeus OS runs.

Before each run:

1. Warm the laptop to the same settled state. Charge to the selected starting
   state of charge, record it, then disconnect AC and verify that the battery
   status is `Discharging`.
2. Set a fixed, recorded brightness and refresh rate. Disable automatic
   brightness and keep the display, external monitor and screen timeout
   settings consistent.
3. Set the same radios and network path: Wi-Fi association, Bluetooth,
   cellular/tethering, VPN and wired networking. Do not change radio state
   between the before and after portions of a comparison.
4. Close unrelated applications, indexing, synchronization and update jobs.
   Use the identical workload sequence in every run, recording whether it is
   terminal-only, browser-based, or another declared workload. Keep workload
   activity and application windows the same before and after the change.
5. Record room temperature and other relevant ambient conditions. Avoid a run
   while the charger is connected, the machine is suspended, or a thermal or
   system update event is in progress.

Run at least three baseline and three Zeus OS samples after the same warm-up.
Retain each JSON file, its exact command line and the start/end state of
charge. Compare medians and variability across the raw valid intervals; do not
select the fastest run. Reconnect AC or stop the comparison if the machine
leaves the declared workload or the battery status changes unexpectedly.

## Interpretation and limits

An AC transition is expected to remove the intervals touching that transition.
If every interval is excluded, the harness fails and the run needs to be
repeated under controlled discharge. A suspend/resume clock gap excludes the
affected interval; any reported average covers only the reported valid duration.
Repeat runs with excluded intervals before using them for controlled comparisons.
A battery directory disappearance fails the run. Flat intervals remain in the denominator for a valid
discharge window; an all-flat window fails for lack of positive aggregate
energy.

The result is a bounded average power observation, not a runtime prediction.
Battery gauges are quantized, and the `charge_now` plus `voltage_now` fallback
is an approximation based on charge change and mean endpoint voltage; use
repeated same-hardware runs and report these limits with any comparison. A
software-rendered or software-decoded workload in a VM can
exercise guest code, but it cannot prove physical battery draw, battery life,
display power, suspend drain, radio power or GPU behavior. VM results therefore
remain VM activity evidence and must not be reported as battery measurements.

## Primary source

The [Linux power supply class documentation](https://www.kernel.org/doc/html/v6.6/power/power_supply_class.html)
(accessed 2026-09-09) documents that attributes are available through sysfs,
that drivers export the class units, that energy is µWh, charge is µAh,
voltage is µV, and `status` identifies charging versus discharging. It also
notes that individual attributes may be absent. The [Linux ABI attribute
index](https://www.kernel.org/doc/html/next/admin-guide/abi-testing-files.html)
lists the power-supply sysfs attributes, including `voltage_now`.
