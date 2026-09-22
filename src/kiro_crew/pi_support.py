"""Runtime glue for the optional Pi ACP backend.

``pi-acp`` owns the ACP↔Pi RPC translation.  This module keeps the KiroCrew-
specific pieces small: resolve the adapter, pass the selected agent prompt and
MCP configuration to Pi, and launch Pi with the mandatory approval extension.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from kiro_crew import platform_compat
from kiro_crew.config.paths import data_home, kiro_agents_dir
from kiro_crew.env import augmented_path
from kiro_crew.platform_compat import chmod_safe, make_owner_only_dir, restrict_to_owner

PI_ACP_BIN = "pi-acp"
PI_ACP_NPM_PKG = "pi-acp"
PI_LAUNCHER_BIN = "kirocrew-pi"
PI_BIN = "pi"
PI_MCP_ADAPTER_PKG = "pi-mcp-adapter"
PI_STATE_ENV = "KIROCREW_PI_STATE"
PI_EFFORT_ENV = "KIROCREW_PI_EFFORT"

# Pi's model registry uses these levels and its `getSupportedThinkingLevels()`
# helper derives the model-specific subset from `reasoning` + `thinkingLevelMap`.
# Keep the order identical so the dashboard slider and Pi's own selector agree.
_PI_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
_PI_METADATA_WAIT_SECS = 2.0


def resolve_pi_acp_bin() -> str | None:
    """Resolve the ``pi-acp`` executable without downloading at runtime."""
    override = os.environ.get("KIROCREW_PI_ACP_BIN", "").strip()
    if override:
        path = Path(override).expanduser().absolute()
        return str(path) if path.is_file() else None
    found = shutil.which(PI_ACP_BIN, path=augmented_path(os.environ.get("PATH", "")))
    return os.path.abspath(found) if found else None


def _new_pi_state_paths(agent: str, session_key: str) -> tuple[Path, Path]:
    """Allocate private per-spawn files for Pi model metadata and level intent."""
    digest = hashlib.sha256(
        f"{agent}\0{session_key}\0{os.getpid()}\0{secrets.token_hex(16)}".encode()
    ).hexdigest()[:32]
    directory = data_home() / "run" / "pi-state"
    make_owner_only_dir(directory)
    base = directory / digest
    return base.with_suffix(".json"), base.with_suffix(".effort")


def _read_pi_state(path: Path | None) -> dict[str, Any]:
    """Read the Pi RPC metadata sidecar; a missing or malformed sidecar is a miss."""
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_private_text(path: Path, value: str) -> None:
    """Atomically write an owner-only sidecar without exposing partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.{secrets.token_hex(4)}.tmp")
    temp.write_text(value, encoding="utf-8")
    try:
        restrict_to_owner(temp)
    except OSError:
        with suppress(OSError):
            temp.unlink()
        raise
    os.replace(temp, path)


def _update_pi_state(path: Path, patch: dict[str, Any]) -> None:
    state = _read_pi_state(path)
    state.update(patch)
    _write_private_text(path, json.dumps(state, separators=(",", ":")))


def _set_pi_requested_effort(path: Path | None, level: str) -> None:
    """Publish the KiroCrew level that the inner Pi RPC proxy must apply."""
    if path is None:
        return
    if level not in _PI_THINKING_LEVELS:
        raise ValueError(f"invalid Pi thinking level: {level!r}")
    _write_private_text(path, level + "\n")


def _clear_pi_requested_effort(path: Path | None) -> None:
    if path is not None:
        with suppress(OSError):
            path.unlink()


def _read_pi_requested_effort(path: Path) -> str:
    try:
        level = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return level if level in _PI_THINKING_LEVELS else ""


def _pi_thinking_levels(state: object, model_id: str | None = None) -> list[str] | None:
    """Mirror Pi's model-aware thinking-level calculation from RPC metadata.

    ``None`` means the sidecar has not identified a model yet; an empty list is
    a known non-reasoning model. Keeping those cases distinct lets callers fall
    back to the adapter's legacy config only while metadata is genuinely absent.
    """
    if not isinstance(state, dict) or not isinstance(state.get("models"), list):
        return None
    target = "" if not model_id or model_id == "auto" else model_id
    if not target:
        current = state.get("current_model")
        target = current if isinstance(current, str) else ""
    selected: dict[str, Any] | None = None
    for raw in state["models"]:
        if not isinstance(raw, dict):
            continue
        provider = raw.get("provider")
        model = raw.get("id")
        qualified = f"{provider}/{model}" if provider and model else ""
        if not target or target not in (qualified, model):
            continue
        selected = raw
        break
    if selected is None:
        return None
    if not bool(selected.get("reasoning")):
        return []
    mapping = selected.get("thinkingLevelMap")
    if not isinstance(mapping, dict):
        mapping = {}
    levels: list[str] = []
    for level in _PI_THINKING_LEVELS:
        mapped = mapping.get(level)
        if mapped is None and level in mapping:
            continue
        if level in ("xhigh", "max") and level not in mapping:
            continue
        levels.append(level)
    return levels


def _record_pi_models(path: Path, raw_models: object) -> None:
    if not isinstance(raw_models, list):
        return
    models: list[dict[str, Any]] = []
    for raw in raw_models:
        if not isinstance(raw, dict):
            continue
        provider = raw.get("provider")
        model = raw.get("id")
        if not isinstance(provider, str) or not isinstance(model, str) or not provider or not model:
            continue
        record: dict[str, Any] = {
            "provider": provider,
            "id": model,
            "reasoning": bool(raw.get("reasoning")),
        }
        mapping = raw.get("thinkingLevelMap")
        if isinstance(mapping, dict):
            record["thinkingLevelMap"] = {
                level: mapping[level]
                for level in _PI_THINKING_LEVELS
                if level in mapping and (mapping[level] is None or isinstance(mapping[level], str))
            }
        models.append(record)
    if models:
        _update_pi_state(path, {"models": models})


def _record_pi_rpc_response(path: Path, response: object, pending_levels: dict[str, str]) -> None:
    """Capture only bounded model/level metadata while forwarding Pi's raw response."""
    if not isinstance(response, dict) or response.get("type") != "response":
        return
    command = response.get("command")
    data = response.get("data")
    if command == "get_available_models" and isinstance(data, dict):
        _record_pi_models(path, data.get("models"))
    elif command == "get_state" and isinstance(data, dict):
        model = data.get("model")
        if isinstance(model, dict):
            provider = model.get("provider")
            model_id = model.get("id")
            if isinstance(provider, str) and isinstance(model_id, str) and provider and model_id:
                _update_pi_state(path, {"current_model": f"{provider}/{model_id}"})
        level = data.get("thinkingLevel")
        if isinstance(level, str) and level in _PI_THINKING_LEVELS:
            _update_pi_state(path, {"current_level": level})
    elif command == "set_model" and isinstance(data, dict):
        provider = data.get("provider")
        model_id = data.get("id")
        if isinstance(provider, str) and isinstance(model_id, str) and provider and model_id:
            _update_pi_state(path, {"current_model": f"{provider}/{model_id}"})
    elif command == "set_thinking_level" and response.get("success"):
        request_id = response.get("id")
        level = pending_levels.pop(str(request_id), "")
        if level:
            _update_pi_state(path, {"current_level": level})


class _PiModeCompletion:
    """Bridge pure extension commands without settling an overlapping model turn."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._busy = False
        self._busy_request_id: str | None = None

    def request(self, message: dict[str, Any]) -> None:
        with self._lock:
            if message.get("type") == "abort":
                self._pending.clear()
            if message.get("type") != "prompt":
                return
            text = message.get("message")
            # Match Pi's literal-space command parser, not Python whitespace splitting.
            token = text.partition(" ")[0] if isinstance(text, str) else ""
            request_id = message.get("id")
            if (
                token in ("/ponytail", "/caveman")
                and isinstance(request_id, str)
                and not message.get("images")
                and not self._busy
                and not self._pending
            ):
                self._pending.add(request_id)
            else:
                # A second prompt makes uncorrelated terminal events ambiguous.
                self._pending.clear()
                if not self._busy and isinstance(request_id, str):
                    self._busy_request_id = request_id
                self._busy = True

    def response(self, message: dict[str, Any]) -> bool:
        with self._lock:
            kind = message.get("type")
            if kind == "agent_start":
                self._busy = True
                self._pending.clear()
            elif kind == "agent_settled":
                self._busy = False
                self._busy_request_id = None
                self._pending.clear()
            elif kind == "response" and message.get("command") == "prompt":
                request_id = message.get("id")
                success = message.get("success")
                if (
                    isinstance(request_id, str)
                    and request_id == self._busy_request_id
                    and isinstance(success, bool)
                    and not success
                ):
                    self._busy = False
                    self._busy_request_id = None
                if isinstance(request_id, str) and request_id in self._pending:
                    self._pending.remove(request_id)
                    return isinstance(success, bool) and success and not self._busy
            return False


def _run_pi_rpc_proxy(pi_bin: str, args: list[str], state_path: Path, effort_path: Path) -> int:
    """Proxy Pi RPC so KiroCrew can retain model metadata pi-acp 0.0.33 drops.

    The published adapter exposes a fixed six-level ACP list and rejects ``max``
    before it reaches Pi. KiroCrew sends ``max`` as the adapter's accepted
    ``xhigh`` token, while this inner proxy restores ``max`` for Pi only when the
    selected model's own ``thinkingLevelMap`` advertises it.
    """
    command: str | list[str] = [pi_bin, *args]
    use_shell = os.name == "nt" and Path(pi_bin).suffix.lower() in {".cmd", ".bat"}
    if use_shell:
        command = subprocess.list2cmdline(command)
    child_env = dict(os.environ)
    # The sidecars are control-plane files for this proxy, not agent inputs.
    child_env.pop(PI_STATE_ENV, None)
    child_env.pop(PI_EFFORT_ENV, None)
    child = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        env=child_env,
        shell=use_shell,
        # Keep Pi in the adapter's process group on POSIX so KiroCrew's tree
        # kill reaches both processes. Windows taskkill /T handles the child tree.
        start_new_session=False,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    input_stream: Any = getattr(sys.stdin, "buffer", sys.stdin)
    output_stream: Any = getattr(sys.stdout, "buffer", sys.stdout)
    child_stdin: Any = child.stdin
    child_stdout: Any = child.stdout
    pending_levels: dict[str, str] = {}
    mode_completion = _PiModeCompletion()
    metadata_ready = threading.Event()

    def stream_bytes(value: object) -> bytes:
        return value if isinstance(value, bytes) else str(value).encode("utf-8")

    def forward_input() -> None:
        try:
            for raw in input_stream:
                if not raw.strip() or child_stdin is None:
                    continue
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    child_stdin.write(stream_bytes(raw))
                    child_stdin.flush()
                    continue
                if isinstance(message, dict):
                    mode_completion.request(message)
                if isinstance(message, dict) and message.get("type") == "set_thinking_level":
                    request_id = message.get("id")
                    desired = _read_pi_requested_effort(effort_path)
                    if desired == "max":
                        levels = _pi_thinking_levels(_read_pi_state(state_path))
                        if levels is None:
                            # The adapter asks for model/state in parallel; wait
                            # for that metadata before deciding whether max is
                            # genuinely supported instead of racing the response.
                            metadata_ready.wait(_PI_METADATA_WAIT_SECS)
                            levels = _pi_thinking_levels(_read_pi_state(state_path))
                        if levels is not None and "max" in levels:
                            message = {**message, "level": "max"}
                    if request_id is not None and isinstance(message.get("level"), str):
                        pending_levels[str(request_id)] = message["level"]
                child_stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
                child_stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            if child_stdin is not None:
                with suppress(OSError):
                    child_stdin.close()

    def forward_output() -> None:
        if child_stdout is None:
            return
        try:
            for raw in child_stdout:
                settled = False
                with suppress(TypeError, ValueError, OSError):
                    message = json.loads(raw)
                    if isinstance(message, dict):
                        settled = mode_completion.response(message)
                    _record_pi_rpc_response(state_path, message, pending_levels)
                if _read_pi_state(state_path).get("models"):
                    metadata_ready.set()
                try:
                    output_stream.write(stream_bytes(raw))
                    if settled:
                        # pi-acp waits for this event; native mode commands only
                        # acknowledge their RPC prompt and never start a model turn.
                        output_stream.write(b'{"type":"agent_settled"}\n')
                    output_stream.flush()
                except (BrokenPipeError, OSError):
                    with suppress(OSError):
                        child.terminate()
                    break
        finally:
            with suppress(OSError):
                output_stream.flush()

    def forward_signal(_signum: int, _frame: object) -> None:
        with suppress(OSError):
            child.terminate()

    old_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in old_handlers:
        with suppress(ValueError):
            signal.signal(sig, forward_signal)
    input_thread = threading.Thread(target=forward_input, name="kirocrew-pi-rpc-in", daemon=True)
    output_thread = threading.Thread(target=forward_output, name="kirocrew-pi-rpc-out", daemon=True)
    input_thread.start()
    output_thread.start()
    try:
        return child.wait()
    finally:
        for sig, handler in old_handlers.items():
            with suppress(ValueError):
                signal.signal(sig, handler)
        if child.poll() is None:
            with suppress(OSError):
                child.terminate()
        input_thread.join(timeout=1)
        output_thread.join(timeout=1)


def resolve_pi_mcp_adapter() -> Path | None:
    """Find the installed pi-mcp-adapter extension entry point."""
    override = os.environ.get("KIROCREW_PI_MCP_ADAPTER", "").strip()
    if override:
        path = Path(override).expanduser().absolute()
        return path if path.is_file() else None

    agent_dir = (
        Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent")))
        .expanduser()
        .absolute()
    )
    roots = (
        agent_dir / "npm" / "node_modules",
        Path.home() / ".npm-packages" / "lib" / "node_modules",
        Path("/opt/homebrew/lib/node_modules"),
        Path("/usr/local/lib/node_modules"),
    )
    for root in roots:
        entry = root / PI_MCP_ADAPTER_PKG / "index.ts"
        if entry.is_file():
            return entry.absolute()
    return None


def resolve_pi_mode_resources() -> tuple[Path, Path, Path]:
    """Resolve installed Ponytail and Crew's versioned native Caveman copy."""
    agent_dir = Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent")))
    root = agent_dir.expanduser().absolute() / "npm" / "node_modules"
    ponytail = root / "@dietrichgebert" / "ponytail"
    resources = (
        ponytail / "pi-extension" / "index.js",
        Path(__file__).resolve().parent / "config" / "pi-caveman.ts",
        ponytail / "skills",
    )
    if not resources[1].is_file():
        raise RuntimeError("Bundled Caveman extension is missing. Reinstall Kiro Crew.")
    if not resources[0].is_file() or not resources[2].is_dir():
        raise RuntimeError(
            "Native Pi modes require Ponytail. "
            "Run `pi install npm:@dietrichgebert/ponytail`, then retry."
        )
    return resources


def _read_agent_json(path: Path) -> dict[str, Any] | None:
    """Read a materialized agent spec without turning a stale file into a crash."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _referenced_mcp_servers(spec: dict[str, Any]) -> set[str]:
    """Return MCP server names mounted by an agent's closed ``tools`` list."""
    tools = spec.get("tools")
    if not isinstance(tools, list):
        return set()
    refs: set[str] = set()
    for raw in tools:
        if not isinstance(raw, str) or not raw.startswith("@"):
            continue
        name = raw[1:].split("/", 1)[0]
        if name:
            refs.add(name)
    return refs


def _load_agent_spec(agent: str) -> dict[str, Any]:
    if not agent or Path(agent).name != agent:
        raise RuntimeError(f"Invalid Pi agent name: {agent!r}")

    # Local imports avoid adding another config.loader ↔ agent import edge at
    # module import time; AcpClient already imports both modules during startup.
    from kiro_crew.agent import (
        _MANAGED_MCP_SERVERS,
        build_agent_config,
        ensure_agent_materialized,
    )

    spec: dict[str, Any]
    if agent == "kirocrew":
        # Keep the pure builder as the base so current dynamic fields and user
        # overrides win.  The materialized spec carries user-assigned MCP
        # servers that the builder intentionally does not reconstruct.
        spec = build_agent_config()
        ensure_agent_materialized(agent)
        materialized = _read_agent_json(kiro_agents_dir() / f"{agent}.json")
        if materialized:
            installed_servers = materialized.get("mcpServers")
            assigned = _referenced_mcp_servers(materialized)
            current_servers = spec.get("mcpServers")
            if not isinstance(current_servers, dict):
                current_servers = {}
                spec["mcpServers"] = current_servers
            if isinstance(installed_servers, dict) and assigned:
                for name, raw in installed_servers.items():
                    if (
                        isinstance(name, str)
                        and name in assigned
                        and name not in _MANAGED_MCP_SERVERS
                        and name not in current_servers
                        and isinstance(raw, dict)
                    ):
                        current_servers[name] = dict(raw)
    else:
        ensure_agent_materialized(agent)
        path = kiro_agents_dir() / f"{agent}.json"
        if not path.is_file():
            raise RuntimeError(f"Agent configuration not found for Pi backend: {path}")
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Agent configuration not found for Pi backend: {path}") from exc
        if not isinstance(loaded, dict):
            raise RuntimeError(f"Agent configuration not found for Pi backend: {path}")
        spec = loaded
    # A Pi process needs a system prompt. Background/"lite" agents ship with an
    # empty prompt; inherit the default agent's prompt so they can still start.
    if not spec.get("prompt"):
        spec = {**spec, "prompt": build_agent_config().get("prompt")}
    return spec


def _pi_oauth_server_entry(server: dict[str, Any]) -> dict[str, Any]:
    """Translate Kiro's remote-MCP OAuth keys to the Pi adapter's schema."""
    out = dict(server)
    has_scopes = "oauthScopes" in out
    raw_scopes = out.pop("oauthScopes", None)
    top_level_client_id = out.pop("clientId", None)
    out.pop("scopes", None)

    raw_oauth = out.get("oauth")
    if isinstance(raw_oauth, bool) and not raw_oauth:
        return out
    oauth = dict(raw_oauth) if isinstance(raw_oauth, dict) else {}

    if has_scopes:
        if (
            isinstance(raw_scopes, list)
            and raw_scopes
            and all(isinstance(scope, str) and scope.strip() for scope in raw_scopes)
        ):
            oauth["scope"] = " ".join(scope.strip() for scope in raw_scopes)
        else:
            # An explicitly empty or malformed Kiro hint must not leave a stale
            # Pi scope in the emitted session config.
            oauth.pop("scope", None)
    if isinstance(top_level_client_id, str) and top_level_client_id.strip():
        oauth["clientId"] = top_level_client_id
    elif top_level_client_id is not None:
        oauth.pop("clientId", None)

    if oauth:
        out["oauth"] = oauth
    elif isinstance(raw_oauth, dict):
        out.pop("oauth", None)
    return out


def _write_inline_prompt(prompt: str) -> Path:
    """Materialize an inline agent prompt for Pi's file-based prompt option."""
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:24]
    directory = Path(tempfile.gettempdir()) / "kirocrew-pi-prompts"
    make_owner_only_dir(directory)
    target = directory / f"{digest}.md"
    try:
        if target.read_text(encoding="utf-8") == prompt:
            return target
    except OSError:
        pass
    temp = target.with_suffix(f".{os.getpid()}.{secrets.token_hex(4)}.tmp")
    temp.write_text(prompt, encoding="utf-8")
    try:
        restrict_to_owner(temp)
    except OSError:
        with suppress(OSError):
            temp.unlink()
        raise
    os.replace(temp, target)
    return target


def _prompt_path(spec: dict[str, Any]) -> Path:
    raw = spec.get("prompt")
    if not isinstance(raw, str) or not raw:
        raise RuntimeError("Selected agent has no prompt configured")
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path)).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"Selected agent prompt does not exist: {path}")
    else:
        candidate = Path(raw).expanduser()
        try:
            path = candidate.resolve() if candidate.is_file() else _write_inline_prompt(raw)
        except OSError:
            # Inline prompts can be longer than a filesystem path. Do not feed them to
            # Path.resolve(), which turns the prompt into a bogus cwd-relative path and
            # raises ENAMETOOLONG before Pi ever starts.
            path = _write_inline_prompt(raw)

    from kiro_crew.security import is_sensitive_path

    if is_sensitive_path(str(path)):
        raise RuntimeError(f"Selected agent prompt is a protected path: {path}")
    return path


def _pi_mcp_config(spec: dict[str, Any]) -> dict[str, Any]:
    raw_servers = spec.get("mcpServers")
    servers: dict[str, dict[str, Any]] = {}
    if isinstance(raw_servers, dict):
        for name, raw in raw_servers.items():
            if not isinstance(name, str) or not isinstance(raw, dict):
                continue
            server = _pi_oauth_server_entry(raw)
            # Translate Kiro's visibility controls; the Pi adapter calls this
            # field ``excludeTools`` when registering direct tools.
            disabled = server.pop("disabledTools", [])
            excluded = [
                tool
                for tools in (server.get("excludeTools", []), disabled)
                if isinstance(tools, list)
                for tool in tools
                if isinstance(tool, str)
            ]
            if excluded:
                server["excludeTools"] = list(dict.fromkeys(excluded))
            server.pop("autoApprove", None)
            server["approveTools"] = False
            server["directTools"] = True
            servers[name] = server
    return {
        "settings": {
            "disableProxyTool": True,
            "scriptMode": False,
        },
        "mcpServers": servers,
    }


def _write_mcp_config(agent: str, session_key: str, spec: dict[str, Any]) -> Path:
    key = hashlib.sha256(f"{agent}\0{session_key}".encode()).hexdigest()[:24]
    directory = data_home() / "run" / "pi-mcp"
    make_owner_only_dir(directory)
    target = directory / f"{key}.json"
    temp = target.with_suffix(f".{os.getpid()}.{secrets.token_hex(4)}.tmp")
    payload = json.dumps(_pi_mcp_config(spec), indent=2, sort_keys=True) + "\n"
    temp.write_text(payload, encoding="utf-8")
    try:
        restrict_to_owner(temp)
    except OSError:
        with suppress(OSError):
            temp.unlink()
        raise
    os.replace(temp, target)
    return target


def _write_pi_launcher() -> Path:
    """Create the single-executable shim expected by ``PI_ACP_PI_COMMAND``."""
    directory = data_home() / "pi" / "bin"
    make_owner_only_dir(directory)
    if os.name == "nt":
        target = directory / f"{PI_LAUNCHER_BIN}.cmd"
        payload = f'@"{sys.executable}" -m kiro_crew.pi_support %*\r\n'
    else:
        target = directory / PI_LAUNCHER_BIN
        payload = "#!/bin/sh\n" f'exec {shlex.quote(sys.executable)} -m kiro_crew.pi_support "$@"\n'
    try:
        if target.read_text(encoding="utf-8") == payload:
            chmod_safe(target, 0o700)
            return target
    except OSError:
        pass
    temp = target.with_suffix(f".{os.getpid()}.{secrets.token_hex(4)}.tmp")
    temp.write_text(payload, encoding="utf-8")
    chmod_safe(temp, 0o700)
    os.replace(temp, target)
    return target


def prepare_pi_environment(env: dict[str, str], *, agent: str, session_key: str) -> None:
    """Populate the environment consumed by :func:`main` below."""
    spec = _load_agent_spec(agent)
    adapter = resolve_pi_mcp_adapter()
    if adapter is None:
        raise RuntimeError(
            "pi-mcp-adapter is required for Kiro Crew's managed tools. Install it with "
            "`pi install npm:pi-mcp-adapter`, then retry."
        )

    pi_bin = _resolve_pi_bin()
    if pi_bin is None:
        raise RuntimeError(
            "Pi is not installed. Install @earendil-works/pi-coding-agent and retry."
        )

    resolve_pi_mode_resources()
    state_path, effort_path = _new_pi_state_paths(agent, session_key)
    # Dashboard MCP results stay inline; MCP UI resources must not launch a local browser window.
    env["MCP_UI_VIEWER"] = "none"
    env["PI_ACP_PI_COMMAND"] = str(_write_pi_launcher())
    env["KIROCREW_PI_BIN"] = pi_bin
    env["KIROCREW_PI_PROMPT"] = str(_prompt_path(spec))
    env["KIROCREW_PI_MCP_CONFIG"] = str(_write_mcp_config(agent, session_key, spec))
    env["KIROCREW_PI_MCP_ADAPTER"] = str(adapter)
    env[PI_STATE_ENV] = str(state_path)
    env[PI_EFFORT_ENV] = str(effort_path)


def _resolve_pi_bin() -> str | None:
    override = os.environ.get("KIROCREW_PI_BIN", "").strip()
    if override:
        path = Path(override).expanduser().absolute()
        return str(path) if path.is_file() else None
    search_path = os.environ.get("PATH", "")
    found = shutil.which(PI_BIN, path=search_path) or shutil.which(
        PI_BIN, path=augmented_path(search_path)
    )
    return os.path.abspath(found) if found else None


def pi_backend_available() -> bool:
    """Whether all optional Pi backend executables and extensions are present."""
    return bool(resolve_pi_acp_bin() and _resolve_pi_bin() and resolve_pi_mcp_adapter())


def main() -> None:
    """Launch Pi for pi-acp with only Kiro Crew's trusted extensions loaded."""
    pi_bin = _resolve_pi_bin()
    if not pi_bin:
        raise SystemExit(
            "Pi is not installed. Install @earendil-works/pi-coding-agent and authenticate it first."
        )

    package_dir = Path(__file__).resolve().parent
    gate = package_dir / "config" / "pi-tool-gate.ts"
    prompt_path = Path(os.environ.get("KIROCREW_PI_PROMPT", ""))
    mcp_config = Path(os.environ.get("KIROCREW_PI_MCP_CONFIG", ""))
    mcp_adapter = Path(os.environ.get("KIROCREW_PI_MCP_ADAPTER", ""))
    for required in (gate, prompt_path, mcp_config, mcp_adapter):
        if not required.is_file():
            raise SystemExit(f"Required Pi integration file is missing: {required}")

    args = sys.argv[1:]
    try:
        ponytail, caveman, mode_skills = resolve_pi_mode_resources()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    # Pi's resource loader accepts a prompt source path and resolves it before the RPC
    # session starts. Passing the file contents makes the whole prompt look like a
    # project-relative path and fails with ENAMETOOLONG on long prompts.
    argv = [
        pi_bin,
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-themes",
        "--extension",
        str(gate),
        "--extension",
        str(mcp_adapter),
        "--extension",
        str(ponytail),
        "--extension",
        str(caveman),
        "--skill",
        str(mode_skills),
        "--mcp-config",
        str(mcp_config),
        "--system-prompt",
        str(prompt_path),
        *args,
    ]
    try:
        mode_index = args.index("--mode")
    except ValueError:
        mode_index = -1
    state_raw = os.environ.get(PI_STATE_ENV, "")
    effort_raw = os.environ.get(PI_EFFORT_ENV, "")
    if (
        mode_index >= 0
        and mode_index + 1 < len(args)
        and args[mode_index + 1] == "rpc"
        and state_raw
        and effort_raw
    ):
        # Keep the trusted extension set on the proxy path too.  Omitting these
        # flags makes Pi load the operator's global extensions, whose unrelated
        # UI prompts arrive as ACP permission requests without KiroCrew's
        # tool-call correlation marker.
        raise SystemExit(_run_pi_rpc_proxy(pi_bin, argv[1:], Path(state_raw), Path(effort_raw)))

    use_shell = os.name == "nt" and Path(pi_bin).suffix.lower() in {".cmd", ".bat"}
    command: str | list[str] = argv
    if use_shell:
        command = subprocess.list2cmdline(argv)
    result = subprocess.run(command, env=os.environ, shell=use_shell, check=False)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
