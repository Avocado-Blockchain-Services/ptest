// Task 11 copies this fixture into its private domain and sets one named case.
// This file is fixture data; Task 6 never imports it or executes native Vitest.
import { defineConfig } from 'vitest/config'

const cases = {
  'pool-match-threads': { test: { poolMatchGlobs: [['**/alpha*', 'threads']] } },
  'pool-match-custom': { test: { poolMatchGlobs: [['**', './untrusted-pool.mjs']] } },
  'typecheck': { test: { typecheck: { enabled: true } } },
  'watch': { test: { watch: true } },
  'api': { test: { api: { port: 0 } } },
  'browser': { test: { browser: { enabled: true } } },
  'workspace': { test: { workspace: ['tests'] } },
  'project-threads': { test: { projects: [{ test: { pool: 'threads' } }] } },
  'foreign-hook': { plugins: [{ name: 'unknown-hook', configureVitest() {} }] },
  'spoofed-hook': { plugins: [{ name: 'ptest-vitest-reporter', configureVitest() {} }] },
  'reporter-removal': { plugins: [{ name: 'remove-reporter', configureVitest({ vitest }) {
    vitest.config.reporters = []
  } }] },
  'name-filter': { test: { testNamePattern: 'same title' } },
  'shard': { test: { shard: '1/2' } },
  'bail': { test: { bail: 1 } },
  'coverage-failure': { test: { coverage: {
    enabled: true, provider: 'v8', include: ['uncovered.mjs'],
    thresholds: { lines: 100, functions: 100, branches: 100, statements: 100 },
  } } },
}
const selected = cases[process.env.PTEST_NATIVE_CASE]
if (!selected) throw new Error('Task 11 must select a declared adversarial fixture case')
export default defineConfig(selected)
