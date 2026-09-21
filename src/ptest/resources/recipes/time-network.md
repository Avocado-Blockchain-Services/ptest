# Time and network

Prefer fake clocks and deterministic barriers to wall-clock sleeps. Use declared
local fakes for network behavior; live targets need explicit isolation and evidence.
Re-run the scoped ptest command after changing timing or network behavior.
