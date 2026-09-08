# Preview 1 measurement protocol

Declared before the first desktop boot. These are preview targets, not measured claims.

- Environment: VM 115, 4 host vCPUs, fixed 8 GiB RAM, VirtIO display, 64 GiB disk. Record resolution, rendering backend and host contention with results.
- Boot to login: target at most 45 seconds from VM start. Also record systemd userspace and total boot time; those are different clocks.
- Settled idle: wait at least 60 seconds after closing foreground applications; sample five times over 20 seconds. Target less than 2% aggregate guest CPU and at most 2 GiB non-cache guest memory. Report actual values and misses.
- Interaction: terminal launch, search, dock and lock/unlock should visibly respond within 2 seconds. Operator/console timings include remote console overhead and are not compositor frame-time benchmarks.
- Fallback comparison: use the same guest and package set, disable the Zeus extension and presentation defaults, restart the session, then repeat the idle sampling. This is a stock-GNOME configuration comparison, not a separately installed clean Fedora Workstation image. Restore the Zeus configuration afterward.
- No background redraw: inspect settled GNOME Shell CPU and visual behavior; a static screenshot cannot prove a lack of redraw. Record compositor frame tracing as unverified if not measured.
- Record native login, notifications/privacy settings, keyboard shortcuts, Files/Terminal/Settings/Browser launch, wallpaper/dock rendering, safe fallback and owner-file preservation. VM results do not qualify physical laptop battery, suspend or input hardware.

An internal review build can remain available with an explicitly recorded measurement miss. Do not label it performance-qualified or stable until its gates pass.
