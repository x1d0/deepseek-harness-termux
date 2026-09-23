# Termux deployment artifacts

English | [中文](README.zh.md)

This directory carries what a Termux deployment of dsh needs to be restored but does not live in the
ordinary source tree.

## `dsh` — launcher

Target location: `$PREFIX/bin/dsh` (Termux's `PREFIX`, usually `/data/data/com.termux/files/usr`;
requires `chmod +x`). Contents:

```bash
export CI=true
DSH_REPO="${DSH_REPO:-$HOME/build/deepseek-harness}"
if [ -f "$DSH_REPO/apps/cli/lib/bin.js" ]; then
  export NODE_COMPILE_CACHE="${NODE_COMPILE_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/dsh-node-compile}"
  mkdir -p "$NODE_COMPILE_CACHE" 2>/dev/null || true
  exec node --expose-internals "$DSH_REPO/apps/cli/lib/bin.js" "$@"
fi
exec node --expose-internals \
  --import "$DSH_REPO/node_modules/tsx/dist/esm/index.mjs" \
  "$DSH_REPO/apps/cli/src/bin.ts" "$@"
```

`DSH_REPO` defaults to `$HOME/build/deepseek-harness`; when the checkout lives elsewhere, override it
with the environment variable.

The launcher prefers the prebuilt CLI bundle (`apps/cli/lib/bin.js`) and falls back to `src/bin.ts`
through tsx only when that bundle is missing. tsx re-transpiles the entry on every boot (~0.4s of the
startup), so the prebuilt path is what keeps `dsh --profile sdk` responsive; source changes under
`apps/cli` therefore need `CI=true pnpm run build:lib:host` before the launcher picks them up.
`NODE_COMPILE_CACHE` reuses V8 bytecode across boots (Node 22+).

`--expose-internals` is required: `node-addon-require-builtin` has no android-arm64 prebuilt, so
profile resolution must reach the Node internal through the `require` branch of
`packages/boot/app-boot/src/profile-resolution/resolver.ts`.

## `native/system/prebuilds/android-arm64/system.node`

The flock native binding (bionic build, 11504 bytes). `native/system/.gitignore` ignores
`prebuilds/`; this repository tracks the file explicitly with `git add -f`. To rebuild:

```bash
clang -shared -fPIC native/system/packages/entry/src/flock.c \
  -I$PREFIX/include/node -o native/system/prebuilds/android-arm64/system.node
```

## Not tracked here

`~/.dsh/profiles/{sdk,web,headless}`, `~/.dsh/settings.yaml`, `~/.dsh/storages`,
`~/.config/xi/config.toml` and `~/.venvs/xi-probe` are machine-local state and need a separate backup.
