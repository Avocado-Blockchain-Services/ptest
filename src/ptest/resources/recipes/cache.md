# Caches

Prefix every Redis, Valkey, or in-memory cache key with the run and worker identity.
Cleanup deletes only that prefix. Never call a global flush operation. Keep a
neighbor key in the test fixture and verify it survives teardown.
