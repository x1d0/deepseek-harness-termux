# Termux 部署件

[English](README.md) | 中文

本目录承载在 Termux 上恢复 dsh 部署所需、但不在常规源码树里的产物。

## `dsh` — 启动器

目标位置：`$PREFIX/bin/dsh`（Termux 的 `PREFIX`，通常是 `/data/data/com.termux/files/usr`；需 `chmod +x`）。内容：

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

`DSH_REPO` 默认取 `$HOME/build/deepseek-harness`，checkout 在别处时用环境变量覆盖即可。

启动器优先跑预编译的 `apps/cli/lib/bin.js`，只有该产物不存在时才回落到 tsx 跑 `src/bin.ts`。
tsx 每次启动都要重转译入口（约占启动的 0.4s），预编译路径才是 dsh 保持响应快的那条；因此改了
`apps/cli` 下的源码要先 `CI=true pnpm run build:lib:host` 才会被启动器采纳。`NODE_COMPILE_CACHE`
复用跨启动的 V8 字节码（Node 22+）。

`--expose-internals` 是必需项：`node-addon-require-builtin` 没有 android-arm64 预编译产物，profile 解析必须经
`packages/boot/app-boot/src/profile-resolution/resolver.ts` 的 `require` 分支取 Node internal。

## `native/system/prebuilds/android-arm64/system.node`

flock 的原生 binding（bionic 编译，11504 字节）。`native/system/.gitignore` 忽略 `prebuilds/`，本仓库以
`git add -f` 显式纳管。重建：

```bash
clang -shared -fPIC native/system/packages/entry/src/flock.c \
  -I$PREFIX/include/node -o native/system/prebuilds/android-arm64/system.node
```

## 未纳入仓库

`~/.dsh/profiles/{sdk,web,headless}`、`~/.dsh/settings.yaml`、`~/.dsh/storages`、`~/.config/xi/config.toml`、
`~/.venvs/xi-probe` 都是本机状态，需单独备份。
