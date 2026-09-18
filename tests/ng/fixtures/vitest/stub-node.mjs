// A deliberately local API double, never a Vitest installation or test runner.
// parseCLI's leading-command/mutation contract comes from Vitest 3.2.6 cac.
import { appendFileSync } from 'node:fs'
import { resolve } from 'node:path'

const scenario = JSON.parse(process.env.PTEST_STUB_CASE ?? '{}')
const trace = (event, data = {}) => appendFileSync(process.env.PTEST_STUB_TRACE,
  `${JSON.stringify({ event, ...data })}\n`)

export function parseCLI(argv) {
  trace('parse', { argv: [...argv] })
  if (argv[0] !== 'vitest') throw new Error('Expected "vitest" as the first argument')
  argv[0] = '/index.js'
  argv.unshift('node')
  return { options: scenario.options ?? {}, filter: scenario.filters ?? [] }
}

export async function createVitest(mode, options, overrides) {
  trace('create', { mode, options })
  const config = {
    ...options, reporters: ['default'], teardownTimeout: 20,
    ...scenario.root,
  }
  const plugins = [...overrides.plugins]
  if (scenario.foreignHook) plugins.push({
    name: scenario.foreignHook, configureVitest() { trace('foreign-hook') },
  })
  const vite = { config: { plugins, test: config } }
  const project = { config: { ...config, ...scenario.project }, vite }
  let reporters = []
  const ctx = {
    config, projects: [project], vite,
    async globTestSpecifications(filters) {
      trace('glob', { filters })
      return (scenario.specs ?? ['tests/alpha.test.mjs', 'tests/alphabet.test.mjs']).map(
        moduleId => ({ moduleId: resolve(moduleId), project, pool: scenario.specPool ?? 'forks' }))
    },
    async init() {
      trace('init')
      for (const reporter of reporters) await reporter.onInit?.(ctx)
    },
    async runTestSpecifications(specs, allTestsRun) {
      trace('run', { specs: specs.map(s => s.moduleId), allTestsRun })
      if (scenario.runHang) await new Promise(() => {})
      await finish()
    },
    async start(filters) {
      trace('start', { filters })
      await ctx.init()
      await finish()
    },
    async close() {
      trace('close')
      if (scenario.closeError) throw new Error('private teardown detail')
      if (scenario.closeHang) await new Promise(() => {})
      if (scenario.keepAlive) setInterval(() => {}, 1000)
    },
  }
  async function finish() {
    process.stdout.write('native reporter output\n')
    process.stderr.write('native reporter stderr\n')
    if (scenario.runError) throw new Error('private runner detail')
    const files = [{ result: { state: scenario.failed ? 'fail' : 'pass' } }]
    for (const reporter of reporters) {
      await reporter.onFinished?.(files, scenario.unhandled ? [new Error('private error')] : [])
    }
    if (scenario.exitCode) process.exitCode = scenario.exitCode
    if (scenario.removeAfterRun) config.reporters = []
  }
  for (const plugin of plugins) await plugin.configResolved?.(vite.config)
  for (const plugin of plugins) await plugin.configureVitest?.({ vitest: ctx, project })
  if (scenario.removeReporter) config.reporters = ['default']
  reporters = config.reporters.filter(reporter => typeof reporter === 'object')
  if (scenario.removeInstantiated) reporters = []
  trace('reporters', { userRetained: config.reporters.includes('default') })
  return ctx
}
