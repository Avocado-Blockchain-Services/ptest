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
        return 23
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
