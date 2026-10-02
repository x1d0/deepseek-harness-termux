/**
 * Failure-path tests for the ripgrep executable resolution. The success path
 * (the real `@vscode/ripgrep` module) is exercised throughout tools.spec.ts;
 * here the module is mocked to throw at evaluation — the shape a missing
 * platform package produces (`--omit=optional`, a partial install, or a
 * platform with no published `@vscode/ripgrep-<platform>-<arch>`) — so the
 * calls run against the host-`rg` fallback and pin where the executable comes
 * from, that a missing platform package surfaces at the first search call
 * rather than at composition load, and that both attempts are named when
 * neither lands.
 */

import { describe, expect, it, vi } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import { ToolCallId } from '@deepseek-ai/dsh-llm'
import type { SubprocessSpawnSpec } from '@deepseek-ai/dsh-subprocess'
import type { ToolExecution } from '@deepseek-ai/dsh-tools'
import { resolveRgPath, runRipgrep } from '@deepseek-ai/dsh-tool-fs-search'

// Any access to the mocked module's surface throws — the shape a missing
// platform package produces at module evaluation.
vi.mock('@vscode/ripgrep', () => new Proxy({}, {
  get() {
    throw new Error('platform package @vscode/ripgrep-win32-x64 is not installed')
  },
}))

/**
 * A context whose subprocess service only implements what executable
 * resolution and one scripted run need: `resolveExecutable` is the test's
 * host-`rg` stand-in, and `spawn` records the spec and settles as "searched,
 * zero matches" (exit 1) so the call returns instead of parsing output.
 */
function scriptedContext(resolveExecutable: (signal?: AbortSignal) => Promise<string>) {
  const spawns: SubprocessSpawnSpec[] = []
  const ctx = {
    subprocess: {
      resolveExecutable,
      spawn(spec: SubprocessSpawnSpec) {
        spawns.push(spec)
        return {
          collected: {
            stdout: { readFrom: () => ({ text: '', nextOffset: 0, lossy: false }) },
            stderr: { readFrom: () => ({ text: '', nextOffset: 0, lossy: false }) },
          },
          done: Promise.resolve({ exitCode: 1, signal: null }),
          terminate: () => {},
          waitForExit: () => Promise.resolve(true),
        }
      },
    },
  } as unknown as Context
  return { ctx, spawns }
}

function toolExec(signal: AbortSignal): ToolExecution {
  return { signal, name: 'grep', callId: ToolCallId('rg-fallback') } as unknown as ToolExecution
}

describe('ripgrep executable resolution', () => {
  it('spawns the host rg the subprocess seam resolves when no packaged binary exists', async () => {
    const { ctx, spawns } = scriptedContext(async () => '/host/bin/rg')

    const run = await runRipgrep(ctx, toolExec(new AbortController().signal), 'grep', ['--json'], 1_000_000, 3_000, 64 * 1024)

    expect(spawns[0]?.argv?.[0]).toBe('/host/bin/rg')
    expect(run.noMatches).toBe(true)
  })

  it('fails the search with both attempts named when neither a packaged binary nor a host rg exists', async () => {
    const { ctx } = scriptedContext(async () => { throw new Error('command "rg" was not found on PATH') })

    const failure = await runRipgrep(ctx, toolExec(new AbortController().signal), 'grep', ['--json'], 1_000_000, 3_000, 64 * 1024)
      .then(() => undefined, (error: unknown) => error)

    expect(failure).toMatchObject({ name: 'SearchError', code: 'SEARCH_FAILED' })
    expect((failure as Error).message).toContain('no packaged ripgrep binary')
    expect((failure as Error).message).toContain('was not found on PATH')
  })

  it('reports SEARCH_ABORTED when the fallback lookup is cancelled', async () => {
    const controller = new AbortController()
    const { ctx } = scriptedContext(async () => {
      controller.abort()
      throw new Error('lookup cancelled')
    })

    await expect(runRipgrep(ctx, toolExec(controller.signal), 'grep', ['--json'], 1_000_000, 3_000, 64 * 1024))
      .rejects.toMatchObject({ name: 'SearchError', code: 'SEARCH_ABORTED' })
  })

  it('keeps the packaged resolution memoized (one rejected resolution per process)', async () => {
    await expect(resolveRgPath()).rejects.toThrow(/platform package/)
    await expect(resolveRgPath()).rejects.toThrow(/platform package/)
  })
})
