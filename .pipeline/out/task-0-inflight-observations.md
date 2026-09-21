# In-flight observations — NOT a completed task verdict

Root read these while Muse was still implementing. Re-read final submitted bytes
and refute/close each before reporting a finding; worker may repair them itself.

- files.py read_regular initially opens FIFO with O_RDONLY before fstat and lacks
  O_NONBLOCK. Existing FIFO test directly calls it, so it can hang before rejecting.
- files.validate_single_name initially permits only ASCII alnum/._-. This rejects
  the approved macOS `Application Support` canonical component and ordinary source
  paths containing spaces/Unicode. Structural path safety is not that allowlist.
- files._walk_to_parent initially retains intermediate descriptors on successful
  nested traversal, with callers closing only final parent/root. Repeated scans
  can exhaust FDs. Check depth>2 path and repeated success/error evidence.
- test_command_summary_withholds_every_token_form initially computes `summary`
  from secret-bearing argv but never serializes that summary; it renders unrelated
  _run_data() with no sentinels. Negative assertions then prove nothing about argv.
- ControlFrame test fixtures initially use nonce `n`*64 although contract is hex64.
- Initial public payload tests/validators use shallow untyped dictionary islands
  (where capability string rather than Capability, doctor limits={entries:1},
  status/history lists unvalidated). Check complete design field types, generated
  schema descriptors and exact public allowlists, not only self-roundtrip success.
- Early shell pipelines pipe ptest through tail then echo $? without pipefail;
  that is not the test's exit code. Require unmasked raw scoped verification and
  meaningful assertion RED, not imports or a pipeline's zero status.

No code changes or reproduced runtime claims in this note. Final root mechanical
gate and independent Opus audit remain required.

## Runtime observation after the initial read

The first scoped files/contracts/storage run blocked in the FIFO case. Root
observed PID1239598, cwd exactly task-0, command python3, elapsed47s and Linux
wchan `wait_for_partner`. This matches read_regular's blocking O_RDONLY FIFO
open before fstat. Root sent SIGINT to that exact owned pytest process; no other
process or file was touched. The run is interrupted/failed evidence, not GREEN.
The old sentence above refers only to the initial read-only note.
