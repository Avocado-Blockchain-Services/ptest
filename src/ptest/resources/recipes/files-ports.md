# Files and ports

Use run/worker-owned temporary directories and OS-assigned ephemeral ports. Avoid
fixed names, shared writable paths, and port literals. Concurrent workers should
prove distinct identities and leave a neighbor sentinel untouched.
