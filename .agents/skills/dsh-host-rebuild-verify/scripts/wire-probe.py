#!/usr/bin/env python3
"""线协议验证：dsh SDK 的 session/list、session/history、session/resume。

只验协议本身（服务端真的回什么），不验 xi 界面。只依赖标准库。
先读同目录的 BUILD-VERIFY-session-surface.md。

    python3 ~/dsh-handoff/wire-probe.py --mode old   # 重建前：三个方法应回 unknown method
    python3 ~/dsh-handoff/wire-probe.py --mode new   # 重建后：三个方法应真的工作

它自己起 mock LLM（不花 token）、自建临时 DSH_HOME、结束时清理干净。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 本脚本住在 <repo>/.agents/skills/<skill>/scripts/ 下：仓库根从脚本位置往上推，
# 换机器/换 checkout 位置都不用改；XI_PROBE_REPO 可显式覆盖。
_SKILL_DIR = Path(__file__).resolve().parents[1]
REPO = Path(os.environ.get("XI_PROBE_REPO") or _SKILL_DIR.parents[2])
MOCK_SRC = HERE / "probe-mock-llm.mts"
MOCK_DST = REPO / "apps" / "cli" / "xi-probe-mock.mts"
DSH_BIN = os.environ.get("XI_PROBE_DSH", "dsh")
PROVIDER = "deepseek-official"
MODEL = "deepseek-v4-flash"
API_KEY = "xi-probe-key"
MARK_ONE = "MARK-ONE first turn"
MARK_TWO = "MARK-TWO second turn"

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""), flush=True)


class Missed:
    """一次取值失败：只带错误信息，由调用方转成一行 FAIL。"""

    __slots__ = ("error",)

    def __init__(self, error: str) -> None:
        self.error = error


def safe(thunk):
    """跑一个发请求的取值操作；失败返回 Missed（不记录、不中断）。"""
    try:
        return thunk()
    except ProbeError as exc:
        return Missed(exc.message)


def bad(value) -> bool:
    return isinstance(value, Missed)


class ProbeError(Exception):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class Runtime:
    """一个 `dsh --profile sdk` 子进程 + 它的 NDJSON JSON-RPC 通道。"""

    def __init__(self, label: str, home: Path, cwd: Path, base_url: str) -> None:
        self.label = label
        self.cwd = cwd
        env = os.environ.copy()
        env["DSH_HOME"] = str(home)
        env["DSH_TELEMETRY_DISABLED"] = "1"
        env["DEEPSEEK_API_KEY"] = API_KEY
        env["DEEPSEEK_BASE_URL"] = base_url
        env.pop("NO_COLOR", None)
        self.proc = subprocess.Popen(
            [DSH_BIN, "--profile", "sdk"],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.lines: queue.Queue[dict] = queue.Queue()
        self.notifications: list[dict] = []
        self.stderr_tail: list[str] = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                self.lines.put(json.loads(raw))
            except json.JSONDecodeError:
                self.stderr_tail.append(f"[非 JSON 输出] {raw[:200]}")

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            self.stderr_tail.append(raw.rstrip())
            del self.stderr_tail[:-40]

    def _next(self, deadline: float) -> dict | None:
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return None
            try:
                return self.lines.get(timeout=min(remain, 0.5))
            except queue.Empty:
                if self.proc.poll() is not None and self.lines.empty():
                    return None

    def call(self, method: str, params: dict | None = None, timeout: float = 180.0) -> object:
        rid = str(uuid.uuid4())
        message: dict = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            message["params"] = params
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            item = self._next(deadline)
            if item is None:
                raise ProbeError(
                    f"{method}: 等响应超时或子进程已退出（rc={self.proc.poll()}）{self._tail()}"
                )
            if item.get("id") != rid:
                self.notifications.append(item)
                continue
            if "error" in item:
                err = item.get("error") or {}
                raise ProbeError(str(err.get("message", "?")), err.get("code"))
            return item.get("result")

    def mark(self) -> int:
        return len(self.notifications)

    def wait_idle(self, session_id: str, since: int = 0, timeout: float = 240.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            for note in self.notifications[since:]:
                params = note.get("params") or {}
                if (
                    note.get("method") == "session.status"
                    and params.get("sessionId") == session_id
                    and params.get("status") == "idle"
                ):
                    return
            item = self._next(deadline)
            if item is None:
                raise ProbeError(f"{session_id}: 等 session.status=idle 超时{self._tail()}")
            self.notifications.append(item)

    def saw_text(self, needle: str, since: int = 0) -> bool:
        return needle in json.dumps(self.notifications[since:], ensure_ascii=False)

    def _tail(self) -> str:
        tail = "\n    ".join(self.stderr_tail[-12:])
        return f"\n    stderr 尾部:\n    {tail}" if tail else ""

    def shutdown(self) -> None:
        try:
            if self.proc.poll() is None:
                self.call("shutdown", {}, timeout=30)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


def start_mock(base_url_file: Path, dump_file: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["XI_MOCK_URL_FILE"] = str(base_url_file)
    env["XI_MOCK_DUMP"] = str(dump_file)
    tsx = REPO / "node_modules" / "tsx" / "dist" / "esm" / "index.mjs"
    proc = subprocess.Popen(
        ["node", "--import", str(tsx), str(MOCK_DST)],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if base_url_file.exists():
            url = base_url_file.read_text().strip()
            if url:
                return proc
        if proc.poll() is not None:
            err = (proc.stderr.read() if proc.stderr else "")[-800:]
            raise ProbeError(f"mock LLM 起不来（rc={proc.returncode}）:\n{err}")
        time.sleep(0.2)
    raise ProbeError("mock LLM 90s 内没有写出 base URL")


def expect_error(label: str, rt: Runtime, method: str, params: dict, needle: str) -> None:
    try:
        result = rt.call(method, params)
    except ProbeError as exc:
        if "unknown DeepSeek Harness SDK runtime method" in exc.message:
            rec(label, False, f"这个方法在运行时不存在（产物没重建？）: {exc.message}")
            return
        ok = needle.lower() in exc.message.lower()
        rec(label, ok, f"错误信息: {exc.message}" + ("" if ok else f"（应包含 {needle!r}）"))
        return
    rec(label, False, f"本应报错，却回了 {json.dumps(result, ensure_ascii=False)[:200]}")


def probe_method(label: str, rt: Runtime, method: str, params: dict, new: bool) -> object:
    """老产物上应该 unknown method；新产物上应该正常返回。"""
    try:
        result = rt.call(method, params)
    except ProbeError as exc:
        if not new and "unknown" in exc.message.lower():
            rec(label, True, f"重建前预期如此: {exc.message}")
            return None
        rec(label, False, f"调用报错: {exc.message}")
        return None
    if not new:
        rec(label, False, "重建前不该有这个方法，却成功了")
        return None
    rec(label, True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["old", "new"], required=True,
                        help="old=重建前（方法应缺失）；new=重建后（方法应工作）")
    parser.add_argument("--keep", action="store_true", help="保留临时目录，便于排查")
    args = parser.parse_args()
    new = args.mode == "new"

    if not REPO.is_dir():
        print(f"dsh checkout 不存在: {REPO}", file=sys.stderr)
        return 2
    if not MOCK_DST.exists():
        MOCK_DST.write_text(MOCK_SRC.read_text(), encoding="utf-8")
        print(f"[setup] 已复制 mock 到 {MOCK_DST}", flush=True)

    root = Path(tempfile.mkdtemp(prefix="xi-probe-"))
    home = root / "home" / ".dsh"
    home.mkdir(parents=True)
    (home / "settings.yaml").write_text("llm-deepseek:\n  protocol: chat-completions\n", encoding="utf-8")
    project = root / "project"
    project.mkdir()
    elsewhere = root / "elsewhere"
    elsewhere.mkdir()
    url_file = root / "mock-url"
    dump_file = root / "mock-dump.json"
    print(f"[setup] 临时树 {root}（模式 {args.mode}）", flush=True)

    mock: subprocess.Popen | None = None
    runtimes: list[Runtime] = []
    try:
        mock = start_mock(url_file, dump_file)
        base_url = url_file.read_text().strip()
        print(f"[setup] mock LLM: {base_url}", flush=True)

        # ---- 进程 A：建会话、跑一个完整回合，然后列会话 / 读历史 ----
        a = Runtime("A", home, project, base_url)
        runtimes.append(a)
        info = a.call("initialize", {"cwd": str(project), "provider": PROVIDER, "model": MODEL})
        rec("A initialize", isinstance(info, dict), json.dumps(info, ensure_ascii=False)[:160])

        sid = f"session-{uuid.uuid4().hex}"
        mark = a.mark()
        prompt = a.call("session/prompt", {"sessionId": sid, "contentBlocks": [{"type": "text", "text": MARK_ONE}]})
        rec("A session/prompt 收下消息", isinstance(prompt, dict) and prompt.get("messageId"),
            f"sessionId={sid} messageId={(prompt or {}).get('messageId')}")
        a.wait_idle(sid, since=mark)
        turn_ok = a.saw_text("MOCK-REPLY", mark)
        rec("A 回合真的跑完（模型回复到达）", turn_ok, "看到 MOCK-REPLY" if turn_ok else "没看到回复文本")

        listing = probe_method("A session/list 可用", a, "session/list", {}, new)
        if new and isinstance(listing, dict):
            entries = listing.get("sessions") or []
            mine = [e for e in entries if e.get("sessionId") == sid]
            rec("A session/list 含本会话", bool(mine),
                f"共 {len(entries)} 条；本会话条目={json.dumps(mine[0], ensure_ascii=False) if mine else '缺失'}")
            if mine:
                entry = mine[0]
                rec("A 列表条目字段齐全", isinstance(entry.get("createdAt"), int),
                    f"cwd={entry.get('cwd')} live={entry.get('live')} persisted={entry.get('persisted')} "
                    f"title={entry.get('title')!r}")
            filtered = safe(lambda: a.call("session/list", {"cwd": str(elsewhere)}))
            rec("A session/list 按 cwd 过滤",
                not bad(filtered) and not (filtered.get("sessions") or []),
                filtered.error if bad(filtered)
                else f"别的目录过滤后剩 {len(filtered.get('sessions') or [])} 条")

        history = probe_method("A session/history 可用", a, "session/history", {"sessionId": sid}, new)
        if new and isinstance(history, dict):
            events = history.get("events") or []
            rec("A 历史里有事件且不截断", len(events) > 0 and history.get("truncated") is False,
                f"{len(events)} 条事件，truncated={history.get('truncated')}")
            rec("A 历史里能找到第一轮文本",
                MARK_ONE in json.dumps(events, ensure_ascii=False), "MARK-ONE 在事件里")
            rec("A 历史返回会话身份", (history.get("session") or {}).get("sessionId") == sid,
                json.dumps(history.get("session"), ensure_ascii=False))
            tail = safe(lambda: a.call("session/history", {"sessionId": sid, "limit": 3}))
            tail_events = [] if bad(tail) else (tail.get("events") or [])
            rec("A session/history limit 生效并标 truncated",
                not bad(tail) and len(tail_events) == 3 and tail.get("truncated") is True,
                tail.error if bad(tail) else f"{len(tail_events)} 条，truncated={tail.get('truncated')}")
        renamed = probe_method("A session/rename 可用", a, "session/rename",
                               {"sessionId": sid, "title": "  改名   试验  "}, new)
        fresh_id = f"session-{uuid.uuid4().hex}"
        if new and isinstance(renamed, dict):
            rec("A rename 回被接受的规范化标题",
                renamed.get("sessionId") == sid and renamed.get("title") == "改名 试验",
                json.dumps(renamed, ensure_ascii=False))
            titled = safe(lambda: a.call("session/history", {"sessionId": sid}))
            titled_text = "" if bad(titled) else json.dumps(titled, ensure_ascii=False)
            rec("A rename 落成用户来源的 title 事件",
                not bad(titled) and '"session/title"' in titled_text
                and '"kind": "user"' in titled_text and "改名 试验" in titled_text,
                titled.error if bad(titled) else f"events={len(titled.get('events') or [])}")
            expect_error("A rename 空白标题被拒绝", a, "session/rename",
                         {"sessionId": sid, "title": "   "}, "visible")
            # 回归：未 prompt 过的新 id 也能先改名（服务端会先把它创建出来）。
            fresh = safe(lambda: a.call("session/rename", {"sessionId": fresh_id, "title": "新建会话名"}))
            rec("A 未 prompt 过的新 id rename 也能成（先创建再改名）",
                not bad(fresh) and (fresh or {}).get("title") == "新建会话名",
                fresh.error if bad(fresh) else json.dumps(fresh, ensure_ascii=False))
            fresh_hist = safe(lambda: a.call("session/history", {"sessionId": fresh_id}))
            fresh_text = "" if bad(fresh_hist) else json.dumps(fresh_hist, ensure_ascii=False)
            rec("A 新会话历史里有 title 事件",
                not bad(fresh_hist) and '"session/title"' in fresh_text and "新建会话名" in fresh_text,
                fresh_hist.error if bad(fresh_hist) else f"events={len(fresh_hist.get('events') or [])}")

        # ---- 中止面：停掉正在跑的回合（这里只探空闲语义；真中断慢回合见 xi E2E）----
        aborted = probe_method("A session/abort 可用", a, "session/abort", {"sessionId": sid}, new)
        if new and isinstance(aborted, dict):
            rec("A 空闲会话 abort 是 no-op（aborted=false）",
                aborted.get("sessionId") == sid and aborted.get("aborted") is False,
                json.dumps(aborted, ensure_ascii=False))
            expect_error("A 不活跃会话 abort 被拒（不 resume 不新建）", a, "session/abort",
                         {"sessionId": "session-ghost-abort"}, "not live")

        # ---- 归档面：registry-global 归档集（纯可见性标记，与 web 共享）----
        archived = probe_method("A session/archive 可用", a, "session/archive", {"sessionId": sid}, new)
        if new and isinstance(archived, dict):
            rec("A archive 回 archived=true 与本会话 id",
                archived.get("sessionId") == sid and archived.get("archived") is True,
                json.dumps(archived, ensure_ascii=False))
            listed = safe(lambda: a.call("session/list", {}))
            entry = None if bad(listed) else next(
                (e for e in (listed.get("sessions") or []) if e.get("sessionId") == sid), None)
            rec("A 归档后 session/list 带 archived=true 标志",
                not bad(listed) and entry is not None and entry.get("archived") is True,
                listed.error if bad(listed) else json.dumps(entry, ensure_ascii=False))
            again_arch = safe(lambda: a.call("session/archive", {"sessionId": sid}))
            rec("A 重复归档幂等（仍 archived=true）",
                not bad(again_arch) and (again_arch or {}).get("archived") is True,
                again_arch.error if bad(again_arch) else json.dumps(again_arch, ensure_ascii=False))
            expect_error("A 未知 id 归档被拒（不偷偷新建）", a, "session/archive",
                         {"sessionId": "session-并不存在"}, "no such session")
            unarch = safe(lambda: a.call("session/unarchive", {"sessionId": sid}))
            rec("A session/unarchive 找回（archived=false）",
                not bad(unarch) and (unarch or {}).get("archived") is False,
                unarch.error if bad(unarch) else json.dumps(unarch, ensure_ascii=False))
            listed2 = safe(lambda: a.call("session/list", {}))
            entry2 = None if bad(listed2) else next(
                (e for e in (listed2.get("sessions") or []) if e.get("sessionId") == sid), None)
            rec("A 找回后 session/list 带 archived=false 标志",
                not bad(listed2) and entry2 is not None and entry2.get("archived") is False,
                listed2.error if bad(listed2) else json.dumps(entry2, ensure_ascii=False))
        a.shutdown()

        # ---- 进程 B：新进程 resume 同一个会话，追问一轮，看上下文是否接上 ----
        b = Runtime("B", home, project, base_url)
        runtimes.append(b)
        b.call("initialize", {"cwd": str(project), "provider": PROVIDER, "model": MODEL})
        resumed = probe_method("B session/resume 可用", b, "session/resume", {"sessionId": sid}, new)
        if new:
            rec("B resume 报告 resumed=true", (resumed or {}).get("resumed") is True,
                json.dumps(resumed, ensure_ascii=False))
            again = safe(lambda: b.call("session/resume", {"sessionId": sid}))
            rec("B 重复 resume 幂等（resumed=false）",
                not bad(again) and (again or {}).get("resumed") is False,
                again.error if bad(again) else json.dumps(again, ensure_ascii=False))
            mark2 = b.mark()

            def second_turn() -> bool:
                b.call("session/prompt", {"sessionId": sid, "contentBlocks": [{"type": "text", "text": MARK_TWO}]})
                b.wait_idle(sid, since=mark2)
                return b.saw_text("MOCK-REPLY", mark2)

            second = safe(second_turn)
            rec("B 续接后回合跑完", second is True,
                second.error if bad(second) else "看到 MOCK-REPLY")
            time.sleep(1.0)
            if dump_file.exists():
                requests = json.loads(dump_file.read_text())
                bodies = json.dumps(requests, ensure_ascii=False)
                rec("B 续接真的带着上一轮上下文（模型请求里有 MARK-ONE）",
                    MARK_ONE in bodies and len(requests) >= 2,
                    f"mock 收到 {len(requests)} 次请求")
            else:
                rec("B 续接真的带着上一轮上下文（模型请求里有 MARK-ONE）", False, "mock dump 文件不存在")
            hist2 = safe(lambda: b.call("session/history", {"sessionId": sid}))
            hist2_text = "" if bad(hist2) else json.dumps(hist2, ensure_ascii=False)
            rec("B 续接后的历史含两轮",
                not bad(hist2) and MARK_ONE in hist2_text and MARK_TWO in hist2_text,
                hist2.error if bad(hist2)
                else f"{len(hist2.get('events') or [])} 条事件；"
                     f"含第一轮={MARK_ONE in hist2_text} 含第二轮={MARK_TWO in hist2_text}")
        b.shutdown()

        # ---- 负例 ----
        if new:
            c = Runtime("C", home, elsewhere, base_url)
            runtimes.append(c)
            c.call("initialize", {"cwd": str(elsewhere), "provider": PROVIDER, "model": MODEL})
            expect_error("C 换目录 resume 被拒绝（不把工具跑错地方）", c, "session/resume",
                         {"sessionId": sid}, "was created in")
            expect_error("C 未知会话 resume 报错（绝不偷偷新建）", c, "session/resume",
                         {"sessionId": f"session-{uuid.uuid4().hex}"}, "")
            expect_error("C session/history 空 id 被拒绝", c, "session/history", {"sessionId": ""}, "sessionId")
            expect_error("C session/history 未知 id 报错", c, "session/history",
                         {"sessionId": f"session-{uuid.uuid4().hex}"}, "")
            expect_error("C session/list limit=0 被拒绝", c, "session/list", {"limit": 0}, "limit")
            expect_error("C 换目录 rename 被拒（改名也先 resume，cwd 检查拦住）", c, "session/rename",
                         {"sessionId": sid, "title": "x"}, "was created in")
            info_c = safe(lambda: c.call("session/list", {}))
            ids = [] if bad(info_c) else [e.get("sessionId") for e in (info_c.get("sessions") or [])]
            rec("C 没有偷偷新建会话（列表里只有 A 建的）",
                not bad(info_c) and set(ids) == {sid, fresh_id},
                info_c.error if bad(info_c) else f"列表 {ids}")
            c.shutdown()
        else:
            a2 = Runtime("A2", home, project, base_url)
            runtimes.append(a2)
            a2.call("initialize", {"cwd": str(project), "provider": PROVIDER, "model": MODEL})
            probe_method("A2 session/resume 可用", a2, "session/resume", {"sessionId": sid}, new)
            a2.shutdown()
    finally:
        for rt in runtimes:
            try:
                rt.shutdown()
            except Exception:
                pass
        if mock is not None and mock.poll() is None:
            mock.kill()
            mock.wait(timeout=10)
        MOCK_DST.unlink(missing_ok=True)
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print(f"[cleanup] 保留 {root}", flush=True)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print()
    print(f"== {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过（模式 {args.mode}）==")
    for name in failed:
        print(f"   FAIL {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
