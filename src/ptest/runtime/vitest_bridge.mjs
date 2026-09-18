/*
 * Candidate Vitest v3 bridge. Native acceptance is owned by Task 11.
 * The executor must bind PreparedRun.report_path to PTEST_VITEST_REPORT_PATH
 * in a private run directory outside the checkout before launching this file.
 * Output from native reporters stays on its original stdout/stderr streams.
 */
import { closeSync, constants, lstatSync, openSync, readFileSync, realpathSync, writeSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, isAbsolute, relative, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const SUPPORTED_ADVANCED = new Set(['3.2.7'])
const SUPPORTED_SERIAL = new Set(['3.1.4', '3.2.6', '3.2.7'])
const protocol = JSON.parse(readFileSync(new URL('./protocol-v1.json', import.meta.url), 'utf8'))

function fail(message) {
  throw new Error(`unsupported-capability: ${message}`)
}

function identity() {
  const run_id = process.env.PTEST_RUN_ID
  const nonce = process.env.PTEST_GRANT_NONCE
  const attempt_id = process.env.PTEST_VITEST_ATTEMPT
  const execution = process.env.PTEST_VITEST_EXECUTION
  if (!/^[a-f0-9]{32}$/.test(run_id ?? '') || !/^[a-f0-9]{64}$/.test(nonce ?? '')
      || !/^a00[1-9]$|^a010$/.test(attempt_id ?? '')
      || !['full', 'selected', 'scoped'].includes(execution)) fail('invalid attempt identity')
  return { protocol: protocol.bridge_event.protocol, run_id, nonce, event: 'terminal', attempt_id, execution }
}

function openReport() {
  const path = process.env.PTEST_VITEST_REPORT_PATH
  if (!path || !isAbsolute(path)) fail('executor must bind a private report path')
  const parent = dirname(path)
  const stamp = lstatSync(parent)
  const inside = relative(realpathSync(process.cwd()), resolve(path))
  if (!stamp.isDirectory() || stamp.isSymbolicLink() || stamp.uid !== process.getuid()
      || (stamp.mode & 0o077) !== 0 || realpathSync(parent) !== parent
      || (!inside.startsWith('../') && !isAbsolute(inside))) fail('report directory is not private and external')
  return openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600)
}

function writeTerminal(fd, event) {
  const bytes = Buffer.from(`${JSON.stringify(event)}\n`)
  if (bytes.length > protocol.bridge_event.max_line_bytes
      || bytes.length > protocol.limits.native_report_max_bytes) fail('terminal report exceeds protocol bounds')
  let offset = 0
  while (offset < bytes.length) {
    const written = writeSync(fd, bytes, offset, bytes.length - offset)
    if (written <= 0) throw new Error('report write did not progress')
    offset += written
  }
}

async function loadProjectVitest() {
  const projectRequire = createRequire(resolve(process.cwd(), 'package.json'))
  const vitestNode = await import(pathToFileURL(projectRequire.resolve('vitest/node')).href)
  return { ...vitestNode, projectRequire, version: projectRequire('vitest/package.json').version }
}

function boundedWorkers() {
  const value = process.env.PTEST_VITEST_WORKERS
  if (!/^[1-9][0-9]?$/.test(value ?? '') || Number(value) > 64) fail('invalid admitted workers')
  const workers = Number(value)
  for (const name of ['VITEST_MAX_FORKS', 'VITEST_MIN_FORKS', 'VITEST_MAX_THREADS', 'VITEST_MIN_THREADS']) {
    if (process.env[name] !== value) fail('worker environment differs from admitted grant')
  }
  return workers
}

function nativeArguments() {
  const delimiter = process.argv.indexOf('--')
  return delimiter < 0 ? [] : process.argv.slice(delimiter + 1)
}

function assertNoUserWorkerControls(argv, options = {}) {
  const owned = new Set(['maxworkers', 'minworkers', 'maxconcurrency', 'pool', 'fileparallelism'])
  for (const token of argv) {
    if (!token.startsWith('--')) continue
    const name = token.split('=', 1)[0].replace(/^--(?:no-)?/, '').replaceAll('-', '').toLowerCase()
    if (owned.has(name) || name.startsWith('pooloptions')) fail('worker controls are owned by ptest')
  }
  if (['maxWorkers', 'minWorkers', 'maxConcurrency', 'pool', 'poolOptions', 'fileParallelism']
    .some(key => options[key] !== undefined)) fail('worker controls are owned by ptest')
}

function enabled(value) {
  return typeof value === 'object' && value !== null ? value.enabled !== false : Boolean(value)
}

function nonempty(value) {
  return Array.isArray(value) ? value.length > 0 : Boolean(value)
}

function validateModes(config) {
  if (config.watch || enabled(config.browser) || config.api || nonempty(config.workspace)
      || nonempty(config.projects) || enabled(config.typecheck) || nonempty(config.poolMatchGlobs)
      || (config.pool && config.pool !== 'forks') || config.standalone || config.mergeReports) {
    fail('unsafe Vitest mode, workspace, typecheck or pool')
  }
}

function validateNarrowing(config, execution) {
  if (execution === 'scoped') return
  if (['testNamePattern', 'testPathPattern', 'filenamePattern', 'shard', 'changed', 'related', 'project', 'bail']
    .some(key => nonempty(config[key]))) fail('narrowing is forbidden for full or selected execution')
}

function ownedOptions(options, workers) {
  validateModes(options)
  return {
    ...options, watch: false, browser: { enabled: false }, api: false, pool: 'forks',
    maxWorkers: workers, minWorkers: workers, maxConcurrency: workers,
    poolOptions: { forks: { maxForks: workers, minForks: workers } },
  }
}

function validateConfig(config, workers, execution) {
  validateModes(config)
  validateNarrowing(config, execution)
  if (config.pool !== 'forks' || config.maxWorkers !== workers || config.minWorkers !== workers
      || config.maxConcurrency !== workers || config.poolOptions?.forks?.maxForks !== workers
      || config.poolOptions?.forks?.minForks !== workers) fail('resolved worker controls differ from admitted grant')
}

function assertOwnedHooks(viteConfig, plugin) {
  if (!Array.isArray(viteConfig?.plugins)) fail('resolved plugin inventory is unavailable')
  if (viteConfig.plugins.some(candidate => candidate !== plugin && candidate.configureVitest != null)) {
    fail('untested configureVitest hook owner')
  }
}

function ownedPlugin(reporter, workers, execution) {
  const plugin = {
    name: 'ptest-vitest-reporter',
    configResolved(config) {
      // Inspection only: reporter injection remains in the public hook below.
      assertOwnedHooks(config, plugin)
      validateModes(config.test ?? {})
      validateNarrowing(config.test ?? {}, execution)
    },
    configureVitest({ vitest, project }) {
      assertOwnedHooks(project.vite.config, plugin)
      validateConfig(project.config, workers, execution)
      const current = vitest.config.reporters
      if (!Array.isArray(current)) fail('resolved reporter inventory is unavailable')
      vitest.config.reporters = [...current, reporter]
    },
  }
  return plugin
}

function validateResolved(ctx, workers, execution, plugin, reporter, native) {
  if (!Array.isArray(ctx.projects) || ctx.projects.length !== 1) fail('exactly one project is required')
  for (const config of [ctx.config, ...ctx.projects.map(project => project.config)]) {
    validateConfig(config, workers, execution)
    if (config.coverage?.enabled) {
      if (config.coverage.provider !== 'v8'
          || native.projectRequire('@vitest/coverage-v8/package.json').version !== native.version) {
        fail('coverage-v8 must match the supported Vitest version')
      }
    }
  }
  for (const vite of [ctx.vite, ...ctx.projects.map(project => project.vite)]) assertOwnedHooks(vite?.config, plugin)
  if (!ctx.config.reporters?.includes(reporter)) fail('owned reporter was removed')
}

function exactSpecifications(specifications, requestedFiles) {
  if (requestedFiles.length === 0) fail('selected execution requires exact files')
  const wanted = new Set(requestedFiles.map(file => resolve(process.cwd(), file)))
  const selected = specifications.filter(spec => wanted.has(spec.moduleId))
  if (selected.length !== wanted.size || new Set(selected.map(spec => spec.moduleId)).size !== wanted.size) {
    fail('requested module does not have one exact specification')
  }
  return selected
}

async function main() {
  const binding = identity()
  const fd = openReport()
  const facts = { profile: null, finished: false, initialized: false, closed: false,
    failed: false, files: null, problem: false, nativeExitCode: 0 }
  let ctx
  let closeTimer
  let finalized = false
  const markProblem = () => {
    if (!facts.problem) facts.nativeExitCode = Number(process.exitCode ?? 0)
    facts.problem = true
    process.exitCode ||= 70
  }
  const finalize = () => {
    if (finalized) return
    finalized = true
    clearTimeout(closeTimer)
    if (!facts.finished || !facts.closed) markProblem()
    const exit_code = Number(process.exitCode ?? 0)
    try {
      writeTerminal(fd, {
        ...binding, profile: facts.profile, selection: false, baseline_eligible: false,
        complete: facts.finished && !facts.problem,
        status: facts.failed || facts.nativeExitCode !== 0 ? 'failed'
          : facts.problem ? 'incomplete' : exit_code === 0 ? 'passed' : 'failed',
        exit_code, files: facts.files, all_tests_run: binding.execution === 'full' && facts.finished && !facts.problem,
      })
    } catch { markProblem(); process.stderr.write('ptest: terminal report unavailable\n') }
    finally { closeSync(fd) }
  }
  // Wait for event-loop drain so late coverage/teardown failures cannot follow
  // success. The external guard remains responsible for group quiescence.
  process.once('beforeExit', finalize)
  const reporter = {
    onInit() { facts.initialized = true },
    onFinished(files, errors) {
      if (facts.finished || !Array.isArray(files) || !Array.isArray(errors)
          || files.length > protocol.limits.native_report_max_tests) fail('invalid terminal callback')
      facts.finished = true
      facts.files = files.length
      facts.failed = errors.length > 0 || files.some(file => file.result?.state === 'fail')
    },
  }
  try {
    const workers = boundedWorkers()
    const nativeArgv = nativeArguments()
    assertNoUserWorkerControls(nativeArgv)
    const native = await loadProjectVitest()
    const { createVitest, parseCLI, version } = native
    const parsed = parseCLI(['vitest', 'run', ...nativeArgv])
    const options = parsed.options
    const requestedFiles = parsed.filter
    if (!options || !Array.isArray(requestedFiles)) fail('unsupported parseCLI return shape')
    assertNoUserWorkerControls([], options)
    validateNarrowing(options, binding.execution)
    if (binding.execution === 'full' && requestedFiles.length) fail('full execution forbids positional filters')
    facts.profile = SUPPORTED_ADVANCED.has(version) ? 'advanced'
      : SUPPORTED_SERIAL.has(version) && workers === 1 ? 'basic_serial' : null
    if (facts.profile === null) fail('Vitest version/profile is outside the enumerated table')
    if (facts.profile === 'basic_serial' && binding.execution === 'selected') fail('basic serial cannot select tests')
    const plugin = ownedPlugin(reporter, workers, binding.execution)
    ctx = await createVitest('test', ownedOptions(options, workers), { plugins: [plugin] })
    validateResolved(ctx, workers, binding.execution, plugin, reporter, native)
    if (facts.profile === 'basic_serial') {
      await ctx.start(requestedFiles)
    } else {
      const specs = await ctx.globTestSpecifications(binding.execution === 'scoped' ? requestedFiles : undefined)
      if (!Array.isArray(specs) || specs.length === 0) fail('no test specifications were collected')
      if (specs.some(spec => spec.project !== ctx.projects[0] || spec.pool !== 'forks')) {
        fail('test specification is outside the admitted project or pool')
      }
      const selected = binding.execution === 'selected' ? exactSpecifications(specs, requestedFiles) : specs
      await ctx.init()
      if (!facts.initialized) fail('owned reporter was not instantiated')
      await ctx.runTestSpecifications(selected, binding.execution === 'full')
    }
    validateResolved(ctx, workers, binding.execution, plugin, reporter, native)
    if (!facts.initialized || !facts.finished) fail('owned reporter terminal evidence is missing')
    if (facts.failed) process.exitCode ||= 1
  } catch {
    if (facts.failed) process.exitCode ||= 1
    markProblem()
    // Native reporters own details; never echo arguments, paths or nonce here.
    process.stderr.write('ptest: unsupported-capability or incomplete Vitest bridge\n')
  } finally {
    if (ctx) {
      const configured = ctx.config.teardownTimeout
      const timeout = Number.isFinite(configured) && configured > 0 ? Math.min(configured, 10000) : 10000
      closeTimer = setTimeout(() => {
        markProblem()
        finalize()
        process.exit(Number(process.exitCode))
      }, timeout)
      try { await ctx.close(); facts.closed = true }
      catch { markProblem(); process.stderr.write('ptest: Vitest close failed\n') }
      // A pending close stays alive; after close only leaked handles keep the
      // watchdog alive. A clean loop emits beforeExit immediately.
      closeTimer.unref()
    }
  }
}

main().catch(() => {
  process.stderr.write('ptest: Vitest bridge could not establish private attempt reporting\n')
  process.exitCode ||= 70
})
