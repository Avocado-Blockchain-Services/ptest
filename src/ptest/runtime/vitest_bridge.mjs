/*
 * Local Vitest v3 bridge.  This program accepts only literal argv after `--`;
 * it never invokes a shell or package manager.  Native subprocess evidence is
 * exercised by candidate ptest in Task 11, not by this task's unit suite.
 */
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const SUPPORTED_ADVANCED = new Set(['3.2.7'])
const SUPPORTED_SERIAL = new Set(['3.1.4', '3.2.6', '3.2.7'])

async function loadProjectVitest() {
  const projectRequire = createRequire(resolve(process.cwd(), 'package.json'))
  const vitestNode = await import(pathToFileURL(projectRequire.resolve('vitest/node')).href)
  return {
    createVitest: vitestNode.createVitest,
    parseCLI: vitestNode.parseCLI,
    version: projectRequire('vitest/package.json').version,
  }
}

function fail(message) {
  throw new Error(`unsupported-capability: ${message}`)
}

function boundedWorkers() {
  const value = process.env.PTEST_VITEST_WORKERS
  if (!/^[1-9][0-9]?$/.test(value ?? '')) fail('missing or invalid admitted workers')
  const workers = Number(value)
  if (workers > 64) fail('admitted workers exceeds 64')
  return workers
}

function nativeArguments() {
  const delimiter = process.argv.indexOf('--')
  return delimiter < 0 ? [] : process.argv.slice(delimiter + 1)
}

function assertNoUserWorkerControls(argv) {
  const owned = new Set([
    '--maxWorkers', '--minWorkers', '--maxConcurrency', '--pool',
    '--poolOptions.forks.maxForks', '--poolOptions.forks.minForks',
  ])
  for (const token of argv) {
    const name = token.split('=', 1)[0]
    if (owned.has(name)) fail(`worker control ${name} is owned by ptest`)
  }
}

function requestedUnsafe(options) {
  return options.watch || options.browser || options.api || options.workspace
    || options.typecheck || (options.pool && options.pool !== 'forks')
}

function ownedOptions(options, workers) {
  if (requestedUnsafe(options)) fail('watch/browser/API/workspace/typecheck/custom-pool requested')
  return {
    ...options,
    watch: false,
    browser: { enabled: false },
    api: false,
    pool: 'forks',
    maxWorkers: workers,
    minWorkers: workers,
    maxConcurrency: workers,
    poolOptions: {
      ...(options.poolOptions ?? {}),
      forks: {
        ...(options.poolOptions?.forks ?? {}),
        maxForks: workers,
        minForks: workers,
      },
    },
  }
}

function additiveReporter() {
  return {
    onFinished(files, errors) {
      const terminal = { protocol: 1, event: 'terminal', complete: errors.length === 0,
        files: files.length }
      process.stdout.write(`${JSON.stringify(terminal)}\n`)
    },
  }
}

function ownedPlugin() {
  return {
    name: 'ptest-vitest-reporter',
    configureVitest({ vitest }) {
      const configured = vitest.config.reporters
      const current = Array.isArray(configured) ? configured : configured ? [configured] : []
      vitest.config.reporters = [...current, additiveReporter()]
    },
  }
}

function validateResolved(ctx, workers) {
  if (!Array.isArray(ctx.projects) || ctx.projects.length !== 1) fail('exactly one project is required')
  const config = ctx.config
  if (config.pool !== 'forks' || config.watch || config.browser?.enabled || config.api) {
    fail('unsafe resolved Vitest configuration')
  }
  if (config.maxWorkers !== workers || config.minWorkers !== workers
      || config.maxConcurrency !== workers
      || config.poolOptions?.forks?.maxForks !== workers
      || config.poolOptions?.forks?.minForks !== workers) {
    fail('resolved worker controls differ from admitted grant')
  }
}

function exactSpecifications(specifications, requestedFiles) {
  if (requestedFiles.length === 0) return specifications
  const wanted = new Set(requestedFiles.map((file) => resolve(process.cwd(), file)))
  const selected = specifications.filter((specification) => wanted.has(specification.moduleId))
  if (selected.length !== wanted.size) fail('requested test module is not an exact specification')
  return selected
}

async function main() {
  const { createVitest, parseCLI, version } = await loadProjectVitest()
  const nativeArgv = nativeArguments()
  assertNoUserWorkerControls(nativeArgv)
  const parsed = parseCLI(nativeArgv)
  const options = parsed.options ?? parsed
  const workers = boundedWorkers()
  const requestedFiles = parsed.filter ?? []
  const tier = SUPPORTED_ADVANCED.has(version) ? 'advanced'
    : SUPPORTED_SERIAL.has(version) && workers === 1 ? 'basic_serial' : null
  if (tier === null) fail('Vitest version/profile is not in the enumerated support table')
  const ctx = await createVitest('test', ownedOptions(options, workers), {
    plugins: [ownedPlugin()],
  })
  try {
    validateResolved(ctx, workers)
    if (tier === 'basic_serial') {
      await ctx.start(requestedFiles)
      return
    }
    const specifications = await ctx.globTestSpecifications()
    const selected = exactSpecifications(specifications, requestedFiles)
    await ctx.init()
    await ctx.runTestSpecifications(selected, requestedFiles.length === 0)
  } finally {
    await ctx.close()
  }
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`)
  process.exitCode = 70
})
