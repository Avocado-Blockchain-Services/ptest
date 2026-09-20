/* Prepared-only, scoped basic-serial Vitest bridge. No tuple is qualified here. */
import { closeSync, constants, lstatSync, openSync, readFileSync, realpathSync, writeSync } from 'node:fs'
import { createRequire } from 'node:module'
import { basename, dirname, isAbsolute, relative, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const protocol = JSON.parse(readFileSync(new URL('./protocol-v1.json', import.meta.url), 'utf8'))
const WORKER_ENV = ['VITEST_MIN_THREADS', 'VITEST_MAX_THREADS', 'VITEST_MIN_FORKS', 'VITEST_MAX_FORKS']
const SCOPE_CONTROLS = ['config', 'configFile', 'root', 'dir', 'changed', 'related', 'standalone', 'shard']
const BLOCKED = new Set(['pool', 'workspace', 'project', 'watch', 'browser', 'api', 'typecheck', 'benchmark', 'runner', 'sequencer', 'maxworkers', 'minworkers', 'maxconcurrency', 'fileparallelism', ...SCOPE_CONTROLS.map(name => name.toLowerCase())])

function refused(message) { throw new Error(`bridge-refused: ${message}`) }

function identity() {
  const { PTEST_RUN_ID: run_id, PTEST_GRANT_NONCE: nonce, PTEST_VITEST_ATTEMPT: attempt_id,
    PTEST_VITEST_EXECUTION: execution_mode, PTEST_VITEST_PROFILE: effective_profile } = process.env
  if (!/^[a-f0-9]{32}$/.test(run_id ?? '') || !/^[a-f0-9]{64}$/.test(nonce ?? '')
      || !/^a00[1-9]$|^a010$/.test(attempt_id ?? '')
      || !['scoped', 'full', 'selected'].includes(execution_mode)
      || !['basic_serial', 'advanced'].includes(effective_profile)) refused('invalid prepared identity')
  if (effective_profile === 'basic_serial' && (process.env.PTEST_VITEST_WORKERS !== '1' || WORKER_ENV.some(name => process.env[name] !== '1'))) {
    refused('serial worker environment is not owned')
  }
  return { protocol: protocol.protocol, run_id, nonce, attempt_id, runner: 'vitest', execution_mode, effective_profile }
}

function openReport() {
  const path = process.env.PTEST_VITEST_REPORT_PATH
  if (!path || !isAbsolute(path)) refused('executor did not allocate a report')
  const parent = dirname(path)
  const state = lstatSync(parent)
  const outside = relative(resolve(process.cwd()), resolve(path))
  const expectedName = new RegExp(`^native-${process.env.PTEST_VITEST_ATTEMPT}-[a-f0-9]{32}\\.json$`)
  if (!state.isDirectory() || state.isSymbolicLink() || state.uid !== process.getuid()
      || (state.mode & 0o077) !== 0 || realpathSync(parent) !== parent
      || !expectedName.test(basename(path)) || (!outside.startsWith('../') && !isAbsolute(outside))) {
    refused('report directory or binding is not private')
  }
  return openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600)
}

function writeReport(fd, value) {
  const body = Buffer.from(`${JSON.stringify(value)}\n`)
  const tests = value?.inventory?.tests
  if (body.length > protocol.limits.native_report_max_bytes
      || (Array.isArray(tests) && (tests.length > protocol.limits.native_report_max_tests
          || tests.length + 1 > protocol.bridge_event.max_events))) refused('report exceeds bound')
  let offset = 0
  while (offset < body.length) {
    const written = writeSync(fd, body, offset, body.length - offset)
    if (written <= 0) refused('report write did not progress')
    offset += written
  }
}

function argumentsAfterDelimiter() {
  const delimiter = process.argv.indexOf('--')
  if (delimiter < 0) refused('bridge delimiter missing')
  return process.argv.slice(delimiter + 1)
}

function sameFiles(actual, expected) {
  return Array.isArray(actual) && actual.length === expected.length
    && expected.every((file, index) => actual[index] === file)
}

function scopedFiles(argv) {
  const raw = process.env.PTEST_VITEST_SCOPED_FILES
  if (!raw || Buffer.byteLength(raw, 'utf8') > protocol.control_frame.max_bytes) refused('invalid scoped files binding')
  const files = JSON.parse(raw)
  if (!Array.isArray(files) || files.length === 0 || files.length > 256
      || files.some(file => typeof file !== 'string' || !file
        || file.startsWith('-') || file.startsWith('/') || file.startsWith('\\')
        || file.includes('\0') || /[\uD800-\uDFFF]/u.test(file)
        || file.replaceAll('\\', '/').split('/').some(part => ['', '.', '..'].includes(part)))) {
    refused('invalid scoped files binding')
  }
  if (!sameFiles(argv.slice(-files.length), files)) refused('scoped files differ from argv suffix')
  return Object.freeze(files)
}

function controlName(token) {
  if (/^-[cr]/.test(token)) return token[1] === 'c' ? 'config' : 'root'
  if (!token.startsWith('--')) return null
  return token.slice(2).split(/[=.]/, 1)[0].replace(/^no-/, '').replaceAll('-', '').toLowerCase()
}

function rejectControls(argv) {
  for (const token of argv) {
    const name = controlName(token)
    if (name && (BLOCKED.has(name) || name.startsWith('pooloptions') || name.startsWith('configurevitest'))) refused('unowned Vitest control')
  }
}

function rejectScopeControls(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || SCOPE_CONTROLS.some(name => value[name] !== undefined && value[name] !== false)
      || (value.api !== undefined && value.api !== false)) refused('unowned Vitest scope or API control')
}

function validateResolved(value) {
  rejectScopeControls(value)
  if (value.workspace || (Array.isArray(value.projects) && value.projects.length)
      || value.watch || value.benchmark || value.runner || value.sequencer
      || (Array.isArray(value.poolMatchGlobs) && value.poolMatchGlobs.length)
      || (value.browser && value.browser.enabled !== false)
      || (value.typecheck && value.typecheck.enabled !== false)) refused('unowned resolved Vitest control')
  if (value.pool !== 'forks' || value.maxWorkers !== 1 || value.minWorkers !== 1
      || value.maxConcurrency !== 1 || value.fileParallelism !== false
      || value.poolOptions?.forks?.minForks !== 1 || value.poolOptions?.forks?.maxForks !== 1) refused('resolved serial controls differ')
}

function loadVitest() {
  const projectRequire = createRequire(resolve(process.cwd(), 'package.json'))
  return import(pathToFileURL(projectRequire.resolve('vitest/node')).href).then(api => ({
    ...api, version: projectRequire('vitest/package.json').version,
  }))
}

async function main() {
  let fd
  let binding
  let nativeVersion = '0.0.0'
  let complete = false
  let nativeExit = null
  try {
    binding = identity()
    fd = openReport()
    if (binding.effective_profile === 'advanced') refused('Vitest advanced native qualification is unavailable')
    if (process.env.NODE_OPTIONS || process.env.NODE_PATH) refused('unowned Node environment')
    const argv = argumentsAfterDelimiter()
    const files = scopedFiles(argv)
    rejectControls(argv)
    const native = await loadVitest()
    nativeVersion = native.version
    const parsed = native.parseCLI(['vitest', 'run', ...argv])
    if (!parsed || !sameFiles(parsed.filter, files)) refused('parsed Vitest scope differs from prepared files')
    rejectScopeControls(parsed.options)
    const workers = Number(process.env.PTEST_VITEST_WORKERS ?? '1')
    const owned = { ...parsed.options, watch: false, pool: 'forks', maxWorkers: workers, minWorkers: workers,
      maxConcurrency: workers, fileParallelism: false,
      poolOptions: { forks: { minForks: workers, maxForks: workers } } }
    const reporter = { onFinished(files, errors) {
      if (!Array.isArray(files) || !Array.isArray(errors)) refused('invalid terminal callback')
      nativeExit = errors.length || files.some(file => file.result?.state === 'fail') ? 1 : Number(process.exitCode ?? 0)
      complete = true
    } }
    const plugin = { name: 'ptest-basic-serial-terminal', configureVitest({ vitest, project }) {
      const plugins = project?.vite?.config?.plugins
      if (!Array.isArray(plugins) || plugins.some(item => item !== plugin && item?.configureVitest)) refused('foreign configureVitest hook')
      validateResolved(project.config)
      if (!Array.isArray(vitest.config.reporters)) refused('reporter inventory unavailable')
      vitest.config.reporters = [...vitest.config.reporters, reporter]
    } }
    const ctx = await native.createVitest('test', owned, { plugins: [plugin] })
    validateResolved(ctx.config)
    await ctx.start(files)
    await ctx.close()
    if (!complete) refused('terminal callback was not observed')
    nativeExit = nativeExit ?? Number(process.exitCode ?? 0)
  } catch {
    complete = false
  } finally {
    if (fd !== undefined && binding) {
      const bridge_exit_code = complete ? nativeExit : 70
      const report = { ...binding, observed_runtime_version: nativeVersion,
        terminal_complete: complete, native_exit_code: complete ? nativeExit : null,
        bridge_exit_code, problem: complete ? (nativeExit === 0 ? null : 'native-failure') : 'bridge-refused' }
      try { writeReport(fd, report) } finally { closeSync(fd) }
      process.exitCode = bridge_exit_code
    } else {
      process.exitCode = 70
    }
  }
}

main().catch(() => { process.exitCode = 70 })
