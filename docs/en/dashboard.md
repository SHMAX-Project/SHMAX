# Dashboard

The dashboard combines MAX/Telegram state, queue failures, group configuration,
active mapping counts, administrator overview and independent network settings.
A validated group means its stored settings/permissions passed validation;
the shared Telegram connection state is displayed separately.

Graphs show Linux host CPU and memory from `/proc`, the SHMAX data filesystem,
and separate cgroup v2 container measurements/limits when available. Unavailable
measurements remain blank. Samples are collected every five seconds, bounded
to one hour in memory since application startup. Use the period selector beside the resource heading to choose 15 minutes or one hour. All graphs share that timeline and leave areas without samples blank. A restart starts a new history. No message content, root privileges or Docker
socket is needed. Long-term storage and threshold alerts are outside this release.
