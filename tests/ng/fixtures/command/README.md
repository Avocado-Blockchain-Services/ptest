`command.py` and `matrix.json` define literal-argv, exit 23, SIGTERM and missing
executable cases. All native cases are NOT RUN. Task11 must copy this directory
under a private explicit fixture-domain, replace `{python}` with its absolute
Python interpreter and `{project}` / `{run_output}` with run-owned paths, then
execute through candidate ptest only. No shell interpolation is permitted.

Before admission, Task11 must call `simple.requires_exclusive(config)` and put
the result in `AdmissionRequest.exclusive`. For command profiles it is true:
wait for zero active jobs and reserve the full machine budget. Test the scheduler
event trace with another admitted workload; the helper alone is not enforcement.
The frozen Grant has no exclusivity field, so `prepare()` cannot verify it.
Exclusive reservation does not prove or contain inner command parallelism.

Check exact literal JSON output, exit 23, raw native SIGTERM -15 / public 143,
and missing executable 127. Preserve unknown public counts. Guard cancellation,
draining, source revalidation and final result publication remain Task11 work.
