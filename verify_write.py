"""Core of verify-write.

Pure functions: no Hermes imports, so this module is unit-testable on its own.

The problem it solves: an agent tool returning success is not the same as the
external system having changed. `DOM.setFileInputFiles` returns ok with
`input.files.length == 0`; a Play Console "save" toast can sit on an unsaved
draft; `gh api PUT` can 200 with a body nobody read back. This module records
every mutation it recognises, runs a cheap read-back, and hands the result to
the next turn where the model can actually see it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

PROBE_TIMEOUT_S = 8.0
MAX_ENTRIES_PER_SESSION = 60

VERIFIED = "verified"
FAILED = "failed"
UNVERIFIED = "unverified"

MUTATING_TOOLS = frozenset({"write_file", "patch", "append_file", "edit_file"})

_GH_WRITE = re.compile(r"\bgh\s+api\b")
_GH_METHOD = re.compile(r"(?:-X|--method)[\s=]+(\w+)", re.IGNORECASE)
_GH_PATH = re.compile(r"\bgh\s+api\b[^|;&]*?((?:/|repos/|orgs/|users/)\S+)")
_CURL_WRITE = re.compile(r"\bcurl\b")
_CURL_METHOD = re.compile(r"(?:-X|--request)[\s=]+(\w+)", re.IGNORECASE)
_CURL_URL = re.compile(r"https?://[^\s'\"]+")
_ADB_INSTALL = re.compile(r"\badb\b[^|;&]*?\binstall\b")
_GIT_PUSH = re.compile(r"\bgit\b[^|;&]*?\bpush\b")
_GIT_C = re.compile(r"\bgit\s+-C\s+(\S+)")

_WRITE_METHODS = frozenset({"PUT", "POST", "PATCH", "DELETE"})


@dataclass
class Entry:
    tool: str
    target: str
    verdict: str
    detail: str
    when: float = field(default_factory=time.time)
    session_id: str = ""

    def line(self) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(self.when))
        return f"- {self.verdict.upper()} {self.tool} {self.target} ({stamp}): {self.detail}"


_ledger: dict[str, list[Entry]] = {}
_lock = threading.Lock()


def reset(session_id: str = "") -> None:
    with _lock:
        if session_id:
            _ledger.pop(session_id, None)
        else:
            _ledger.clear()


def entries(session_id: str) -> list[Entry]:
    with _lock:
        return list(_ledger.get(session_id, ()))


def _record(session_id: str, entry: Entry) -> None:
    entry.session_id = session_id
    with _lock:
        bucket = _ledger.setdefault(session_id, [])
        bucket.append(entry)
        if len(bucket) > MAX_ENTRIES_PER_SESSION:
            del bucket[: len(bucket) - MAX_ENTRIES_PER_SESSION]


def _run(cmd: list[str], cwd: Optional[str] = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
        return proc.returncode, (proc.stdout or proc.stderr or "").strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "probe timed out"
    except Exception as exc:  # pragma: no cover - defensive
        return 1, f"probe error: {exc}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _expand(value: str) -> str:
    return os.path.expanduser(value) if value.startswith("~") else value


# ---------------------------------------------------------------- verifiers


def verify_file(tool: str, args: dict[str, Any], result: Any) -> Optional[Entry]:
    """write_file/patch already hash-verify on write; distrust it and look again."""
    raw = args.get("path")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(_expand(raw))
    target = str(path)
    if not path.exists():
        return Entry(tool, target, FAILED, "file does not exist after the write")
    if tool == "write_file" and isinstance(args.get("content"), str):
        expected = hashlib.sha256(args["content"].encode("utf-8")).hexdigest()
        actual = _sha256(path)
        if expected != actual:
            return Entry(tool, target, FAILED, f"sha mismatch on read-back ({actual[:12]} != {expected[:12]})")
        return Entry(tool, target, VERIFIED, f"sha256 {actual[:12]} matches")
    size = path.stat().st_size
    return Entry(tool, target, VERIFIED, f"exists, {size} bytes")


def verify_gh_api(tool: str, args: dict[str, Any], result: Any) -> Optional[Entry]:
    cmd = str(args.get("command") or "")
    if not _GH_WRITE.search(cmd):
        return None
    method_match = _GH_METHOD.search(cmd)
    method = (method_match.group(1).upper() if method_match else ("POST" if re.search(r"(?:^|\s)-[fF]\b", cmd) else "GET"))
    if method not in _WRITE_METHODS:
        return None
    path_match = _GH_PATH.search(cmd)
    if "graphql" in cmd:
        return Entry(tool, "gh api graphql", UNVERIFIED, "graphql writes have no generic read-back")
    if not path_match:
        return Entry(tool, "gh api", UNVERIFIED, "write call without a resolvable path")
    api_path = path_match.group(1).rstrip("'\"")
    code, out = _run(["gh", "api", api_path])
    if code == 127:
        return Entry(tool, api_path, UNVERIFIED, "gh not found; cannot read back")
    if code != 0:
        return Entry(tool, api_path, FAILED, f"read-back failed (exit {code}): {out[:160]}")
    proof = ""
    try:
        body = json.loads(out)
        if isinstance(body, dict):
            proof = body.get("sha") or body.get("html_url") or body.get("id") or ""
    except Exception:
        proof = out[:80]
    return Entry(tool, api_path, VERIFIED, f"read-back ok {proof}".strip())


def verify_curl_write(tool: str, args: dict[str, Any], result: Any) -> Optional[Entry]:
    cmd = str(args.get("command") or "")
    if not _CURL_WRITE.search(cmd):
        return None
    method_match = _CURL_METHOD.search(cmd)
    if not method_match or method_match.group(1).upper() not in _WRITE_METHODS:
        return None
    url_match = _CURL_URL.search(cmd)
    if not url_match:
        return None
    url = url_match.group(0)
    code, out = _run(["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "-m", "8", url])
    status = out.strip()[-3:]
    if code == 127:
        return Entry(tool, url, UNVERIFIED, "curl not found; cannot read back")
    if code != 0:
        return Entry(tool, url, FAILED, f"read-back request failed: {out[:120]}")
    if status.startswith("2"):
        return Entry(tool, url, VERIFIED, f"GET {status}")
    return Entry(tool, url, FAILED, f"GET {status} after {method_match.group(1).upper()}")


def verify_adb_install(tool: str, args: dict[str, Any], result: Any) -> Optional[Entry]:
    cmd = str(args.get("command") or "")
    if not _ADB_INSTALL.search(cmd):
        return None
    apk = next((tok for tok in cmd.split() if tok.endswith(".apk")), "")
    if not apk:
        return Entry(tool, "adb install", UNVERIFIED, "no .apk path in the command")
    apk_path = _expand(apk.strip("'\""))
    aapt2 = _find_aapt2()
    if not aapt2:
        return Entry(tool, apk_path, UNVERIFIED, "aapt2 not found; package id unknown")
    code, out = _run([aapt2, "dump", "badging", apk_path])
    if code != 0:
        return Entry(tool, apk_path, UNVERIFIED, f"aapt2 could not read the apk: {out[:120]}")
    pkg = re.search(r"package: name='([^']+)'", out)
    ver = re.search(r"versionCode='([^']+)'", out)
    if not pkg:
        return Entry(tool, apk_path, UNVERIFIED, "no package id in the apk manifest")
    wanted = ver.group(1) if ver else ""
    adb = _find_adb()
    if not adb:
        return Entry(tool, apk_path, UNVERIFIED, "adb not found; cannot read the device back")
    code, out = _run([adb, "shell", "dumpsys", "package", pkg.group(1)])
    if code != 0:
        return Entry(tool, pkg.group(1), FAILED, f"device did not report the package: {out[:120]}")
    found = re.search(r"versionCode=(\d+)", out)
    if not found:
        return Entry(tool, pkg.group(1), FAILED, "package installed but no versionCode reported")
    if wanted and found.group(1) != wanted:
        return Entry(tool, pkg.group(1), FAILED, f"device has versionCode {found.group(1)}, apk is {wanted}")
    return Entry(tool, pkg.group(1), VERIFIED, f"versionCode {found.group(1)} on device")


def verify_git_push(tool: str, args: dict[str, Any], result: Any) -> Optional[Entry]:
    cmd = str(args.get("command") or "")
    if not _GIT_PUSH.search(cmd):
        return None
    cwd_match = _GIT_C.search(cmd)
    if not cwd_match:
        return Entry(tool, "git push", UNVERIFIED, "no `git -C <dir>` in the command; repo unknown")
    repo = _expand(cwd_match.group(1).strip("'\""))
    code, head = _run(["git", "-C", repo, "rev-parse", "HEAD"])
    if code == 127:
        return Entry(tool, repo, UNVERIFIED, "git not found; cannot read back")
    if code != 0:
        return Entry(tool, repo, FAILED, f"cannot read HEAD: {head[:120]}")
    code, status = _run(["git", "-C", repo, "status", "-sb"])
    if code != 0:
        return Entry(tool, repo, UNVERIFIED, "cannot read branch status")
    first = status.splitlines()[0] if status else ""
    if "ahead" in first:
        return Entry(tool, repo, FAILED, f"still ahead of upstream ({first[:80]}) head={head[:8]}")
    return Entry(tool, repo, VERIFIED, f"head={head[:8]} {first[:60]}".strip())


_VERIFIERS = (verify_file, verify_gh_api, verify_curl_write, verify_adb_install, verify_git_push)


def _find_adb() -> Optional[str]:
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or os.path.expanduser("~/Library/Android/sdk")
    candidate = Path(sdk) / "platform-tools" / "adb"
    return str(candidate) if candidate.is_file() else shutil.which("adb")


def _find_aapt2() -> Optional[str]:
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or os.path.expanduser("~/Library/Android/sdk")
    tools = Path(sdk) / "build-tools"
    if tools.is_dir():
        for candidate in sorted(tools.iterdir(), reverse=True):
            binary = candidate / "aapt2"
            if binary.is_file():
                return str(binary)
    return shutil.which("aapt2")


def observe(tool_name: str, args: dict[str, Any], result: Any) -> Optional[Entry]:
    """Run the first verifier that claims this call. Never raises."""
    for verifier in _VERIFIERS:
        try:
            entry = verifier(tool_name, args, result)
        except Exception as exc:  # pragma: no cover - defensive
            entry = Entry(tool_name, "?", UNVERIFIED, f"verifier crashed: {exc}")
        if entry is not None:
            return entry
    return None


def record(tool_name: str, args: dict[str, Any], result: Any, session_id: str) -> Optional[Entry]:
    entry = observe(tool_name, args, result)
    if entry is not None:
        _record(session_id, entry)
    return entry


# ---------------------------------------------------------------- reporting


def summary(session_id: str, limit: int = 6) -> str:
    """Context block for the next turn. Empty string when everything checked out."""
    pending = [e for e in entries(session_id) if e.verdict != VERIFIED]
    checked = [e for e in entries(session_id) if e.verdict == VERIFIED]
    if not pending:
        return ""
    lines = [f"[verify-write] {len(pending)} action(s) this session did NOT read back clean:"]
    for entry in pending[-limit:]:
        lines.append(entry.line())
    if len(pending) > limit:
        lines.append(f"- ... and {len(pending) - limit} more")
    if checked:
        lines.append(f"({len(checked)} other write(s) verified ok)")
    lines.append("Do not report these as done until you have re-checked them yourself.")
    return "\n".join(lines)
