# Factories

Factories create fresh test records while keeping shared infrastructure explicit.
They help avoid mutable session fixtures and make per-test cleanup small and owned.
Keep assertions meaningful: a factory is not permission to weaken a test.
