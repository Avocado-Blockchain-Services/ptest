"""Only Task11's candidate-ptest fixture harness may execute this workload."""
import json
import os
import signal
import sys


def main():
    mode, *tokens = sys.argv[1:]
    if mode == "literal":
        print(json.dumps(tokens))
        return 0
    if mode == "exit":
        return int(tokens[0]) if tokens else 23
    if mode == "cwd":
        print(os.getcwd())
        return 0
    if mode == "marker":
        from pathlib import Path
        Path(tokens[0]).write_text("launched")
        return 0
    if mode == "modify-exit":
        from pathlib import Path
        Path(tokens[0]).write_text("runner modification")
        return int(tokens[1]) if len(tokens) > 1 else 0
    if mode == "cancel":
        signal.signal(signal.SIGINT, lambda *_: sys.exit(int(tokens[1])))
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(int(tokens[1])))
        with open(tokens[0], "wb", buffering=0) as ready:
            ready.write(b"ready")
        signal.pause()
        return 99
    if mode == "streams":
        sys.stdout.buffer.write(b"literal stdout\n")
        sys.stderr.buffer.write(b"literal stderr\n")
        return 0
    if mode == "signal":
        os.kill(os.getpid(), signal.SIGTERM)
        return 99
    raise ValueError("unknown fixture mode")


if __name__ == "__main__":
    raise SystemExit(main())
