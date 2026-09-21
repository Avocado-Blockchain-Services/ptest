# Processes

Keep child processes in the foreground process group owned by the test run. Do not
detach children. Cancellation must reap only owned descendants and must not affect
neighbor processes.
