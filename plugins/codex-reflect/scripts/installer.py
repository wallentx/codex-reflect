"""User-level installer: plan first, preserve unrelated settings, own exact files."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

import providers

PACKAGE = Path(__file__).resolve().parents[1]
SKILLS = ("reflect", "reflect-skills", "view-queue", "skip-reflect")


def registry_path():
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return root.expanduser().absolute() / "reflect/installation.json"


def read_json(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object: " + str(path))
    return data


def encoded(data):
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def shell_command(args):
    if os.name == "nt":
        # cmd.exe still expands percent variables inside quoted paths. Refuse
        # ambiguous paths instead of installing a command with different meaning.
        if any(any(c in str(arg) for c in "%\r\n") for arg in args):
            raise ValueError("Hook paths containing %, CR or LF are unsupported on Windows")
        return subprocess.list2cmdline(list(map(str, args)))
    return " ".join(shlex.quote(str(arg)) for arg in args)


def hook_entries(name):
    result = {}
    script = PACKAGE / "scripts/reflect.py"
    for event in providers.EVENTS[name]:
        argv = [sys.executable, str(script), "--provider", name, "hook", event]
        if name == "copilot":
            entry = {"type": "command", "exec": sys.executable, "args": argv[1:], "timeoutSec": 5}
        elif name == "cursor":
            entry = {"command": shell_command(argv), "timeout": 5}
        else:
            entry = {"hooks": [{"type": "command", "command": shell_command(argv),
                                "timeout": 5000 if name == "gemini" else 5}]}
            if event == "PostToolUse":
                entry["matcher"] = "Bash"
        result[event] = [entry]
    return result


def opencode_plugin():
    # Use argv and stdin, never interpolate a prompt into a shell command.
    return ('''// reflect:managed -- regenerate with reflect init --provider opencode
import { spawn } from "node:child_process";
export const ReflectPlugin = async ({ directory }) => ({
  "chat.message": async (input, output) => {
    const parts = Array.isArray(output?.parts) ? output.parts : [];
    const prompt = parts.filter(p => p.type === "text" && !p.synthetic && !p.ignored && typeof p.text === "string")
      .map(p => p.text).join("\\n");
    if (!prompt || output?.message?.role !== "user") return;
    await new Promise(resolve => {
      const child = spawn(PYTHON, ARGS, { stdio: ["pipe", "ignore", "ignore"], shell: false });
      const timer = setTimeout(() => { child.kill(); resolve(); }, 5000);
      const done = () => { clearTimeout(timer); resolve(); };
      child.on("error", done);
      child.on("close", done);
      child.stdin.on("error", done);
      child.stdin.end(JSON.stringify({ cwd: directory, prompt,
        session_id: input.sessionID, turn_id: input.messageID || output.message.id || "" }));
    });
  }
});
'''.replace("PYTHON", json.dumps(sys.executable)).replace("ARGS", json.dumps([
        str(PACKAGE / "scripts/reflect.py"), "--provider", "opencode", "hook", "UserPromptSubmit"])) ).encode()


def skill_bytes(name, skill):
    source = (PACKAGE / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    runtime = PACKAGE / "scripts/reflect.py"
    workflow = providers.skill_home(name) / "reflect/references/review-workflow.md"
    source = source.replace("(../../references/review-workflow.md)", "(<" + workflow.as_posix() + ">)")
    source = source.replace("../../scripts/reflect.py", runtime.as_posix())
    binding = ("\n<!-- reflect:managed -->\n\nInstallation binding: **" + name + "**. "
               "Every helper command uses `" + shell_command([sys.executable, runtime, "--provider", name]) +
               " <action>`. Keep this provider argument, including when following the shared workflow.\n")
    # Keep YAML frontmatter first for every provider's skill loader.
    end = source.index("\n---", 3) + len("\n---")
    return (source[:end] + "\n" + binding + source[end:]).encode()


def file_plan(path, content, owned, writes, new_owned):
    path = Path(path)
    # Do not follow a symlink in the destination or any of its parents.
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Refusing symlink destination: " + str(path))
    before = path.read_bytes() if path.exists() else None
    known = owned.get(str(path))
    if before is not None and known != digest(before) and before != content:
        raise ValueError("Unmanaged or locally edited file: " + str(path))
    if before != content:
        writes[path] = content
    if content is not None:
        new_owned[str(path)] = digest(content)


def merge_hooks(path, previous, desired):
    data = read_json(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Invalid hooks object: " + str(path))
    for event in previous.keys() | desired.keys():
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            raise ValueError("Invalid hook event: " + event)
        entries = list(entries)
        for entry in previous.get(event, []):
            if entry not in entries:
                raise ValueError("Managed hook changed; review before reinstalling: " + str(path))
            entries.remove(entry)
        for entry in desired.get(event, []):
            if entry not in entries:
                entries.append(entry)
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    return data


def atomic_write(path, content):
    if content is None:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".reflect-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_writes(writes, dry_run=False):
    """Validate the entire plan, back up replaced bytes, roll back on failure."""
    before = {}
    for path, content in writes.items():
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("Refusing symlink destination: " + str(path))
        before[path] = path.read_bytes() if path.exists() else None
        print(("REMOVE " if content is None else "WRITE  ") + str(path))
    if dry_run or not writes:
        return
    backup = registry_path().parent / "install-backups" / uuid.uuid4().hex
    changed = []
    try:
        for index, (path, content) in enumerate(writes.items()):
            current = path.read_bytes() if path.exists() else None
            if current != before[path]:
                raise RuntimeError("File changed during installation: " + str(path))
            if current is not None:
                atomic_write(backup / (str(index) + ".bak"), current)
                atomic_write(backup / (str(index) + ".json"), encoded({"path": str(path)}))
            atomic_write(path, content)
            changed.append(path)
    except BaseException:
        for path in reversed(changed):
            atomic_write(path, before[path])
        raise


def local_plan(name, previous, remove=False):
    writes, owned = {}, {}
    old_files = previous.get("files", {})
    if remove:
        for path in old_files:
            file_plan(path, None, old_files, writes, owned)
    else:
        for skill in SKILLS:
            file_plan(providers.skill_home(name) / skill / "SKILL.md", skill_bytes(name, skill), old_files, writes, owned)
        workflow = (PACKAGE / "references/review-workflow.md").read_bytes()
        file_plan(providers.skill_home(name) / "reflect/references/review-workflow.md", workflow, old_files, writes, owned)
        if name == "opencode":
            file_plan(providers.provider_home(name) / "plugins/reflect.js", opencode_plugin(), old_files, writes, owned)
    spec = providers.PROVIDERS[name]
    hook_state = {}
    if spec["hooks_file"]:
        config = providers.provider_home(name) / spec["hooks_file"]
        desired = {} if remove else hook_entries(name)
        data = merge_hooks(config, previous.get("hooks", {}), desired)
        if name == "cursor":
            if data.get("version", 1) != 1:
                raise ValueError("Unsupported Cursor hooks version")
            data["version"] = 1
        content = encoded(data)
        if not config.exists() or config.read_bytes() != content:
            writes[config] = content
        hook_state = desired
    return writes, {"method": "local", "files": owned, "hooks": hook_state}


def add_init_parser(commands):
    parser = commands.add_parser("init", help="Choose providers or register explicitly, like panoptes init")
    parser.add_argument("--provider", dest="selected", action="append", choices=sorted(providers.PROVIDERS))
    parser.add_argument("--method", choices=("auto", "local", "marketplace"), default="auto")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing or running provider CLIs")
    parser.add_argument("--remove", action="store_true", help="Remove only the named managed integrations; retain queues")
    parser.add_argument("--list", action="store_true", help="Show detection, installation and capabilities")


def source_root():
    metadata = PACKAGE / "source.json"
    return Path(read_json(metadata)["checkout"]) if metadata.exists() else PACKAGE.parents[1]


def marketplace_commands(name, remove, installed=False):
    if not providers.PROVIDERS[name]["marketplace"]:
        raise ValueError("No marketplace installer for " + name + "; use --method local")
    if name == "codex":
        plugin = "codex-reflect@codex-reflect-marketplace"
        return [["codex", "plugin", "remove", plugin]] if remove else [
            ["codex", "plugin", "marketplace", "add", str(source_root())],
            ["codex", "plugin", "add", plugin]]
    plugin = "reflect@reflect-marketplace"
    return [["claude", "plugin", "uninstall", plugin, "--scope", "user"]] if remove else [
        ["claude", "plugin", "marketplace", "add", str(source_root())],
        ["claude", "plugin", "update" if installed else "install", plugin, "--scope", "user"]]


def marketplace_registered(name):
    """Inspect the native registry, so repeated setup does not re-add a marketplace."""
    result = subprocess.run([name, "plugin", "marketplace", "list", "--json"],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("Cannot inspect " + name + " marketplaces")
    data = json.loads(result.stdout)
    rows = data.get("marketplaces", []) if name == "codex" else data
    if not isinstance(rows, list):
        raise ValueError("Unrecognized " + name + " marketplace list format")
    expected = "codex-reflect-marketplace" if name == "codex" else "reflect-marketplace"
    return any(isinstance(row, dict) and row.get("name") == expected for row in rows)


def installed_marketplace(name):
    if name == "codex":
        path = providers.provider_home(name) / "config.toml"
        if path.exists():
            import re
            return bool(re.search(r'\[plugins\.[^\n]*codex-reflect@', path.read_text(encoding="utf-8")))
    if name == "claude":
        data = read_json(providers.provider_home(name) / "plugins/installed_plugins.json")
        return any(key.split("@")[0] in ("reflect", "claude-reflect") for key in data.get("plugins", {}))
    return False


def legacy_claude_installed():
    data = read_json(providers.provider_home("claude") / "plugins/installed_plugins.json")
    return any(key.split("@")[0] == "claude-reflect" for key in data.get("plugins", {}))


def initialize(args):
    # Serialize init/remove across providers sharing the registry. Dry runs and
    # listing must remain completely read-only, including no lock-file creation.
    if args.dry_run or args.list:
        return initialize_locked(args)
    lock = registry_path().with_suffix(".lock")
    if any(p.is_symlink() for p in (lock, *lock.parents)):
        raise ValueError("Refusing symlink destination: " + str(lock))
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise RuntimeError("Another installer is running (or left a stale lock): " + str(lock))
    try:
        os.close(fd)
        return initialize_locked(args)
    finally:
        lock.unlink()


def initialize_locked(args):
    registry = read_json(registry_path())
    installed = registry.setdefault("providers", {})
    if args.list or not args.selected:
        print("PROVIDER     DETECTED  INSTALLED    CAPTURE  HISTORY  MARKETPLACE")
        for name, spec in providers.PROVIDERS.items():
            method = installed.get(name, {}).get("method", "-")
            print("{:<12} {:<9} {:<12} {:<8} {:<8} {}".format(name,
                "yes" if providers.provider_home(name).exists() else "no", method,
                "manual" if name == "antigravity" else "hook", spec["history"],
                "yes" if spec["marketplace"] else "no"))
    if args.list:
        return 0
    if not args.selected:
        if not sys.stdin.isatty():
            raise ValueError("Provider selection needs a terminal; pass --provider NAME")
        # Additive selection: leaving out a provider never silently removes it.
        raw = input("Providers (comma-separated IDs; empty cancels): ").strip()
        if not raw:
            return 0
        args.selected = [value.strip() for value in raw.split(",")]
    names = list(dict.fromkeys(args.selected))
    if any(name not in providers.PROVIDERS for name in names):
        raise ValueError("Unknown provider selection")
    writes, commands = {}, []
    for name in names:
        previous = installed.get(name, {})
        method = args.method
        if method == "auto":
            method = previous.get("method") or ("marketplace" if providers.PROVIDERS[name]["marketplace"] else "local")
        if previous and method != previous["method"]:
            raise ValueError("Remove the existing integration before changing installation method for " + name)
        if args.remove and not previous:
            raise ValueError("No managed installation to remove for " + name)
        if method == "local":
            if not args.remove and installed_marketplace(name):
                raise ValueError("Marketplace Reflect is already registered for " + name + "; use --method marketplace")
            plan, entry = local_plan(name, previous, args.remove)
            writes.update(plan)
        else:
            if name == "claude" and not args.remove and legacy_claude_installed():
                raise ValueError("Legacy claude-reflect is installed. Remove it with Claude's plugin manager before installing reflect, to avoid duplicate capture. Its queue is not migrated.")
            if not args.remove and not (source_root() / ".agents/plugins/marketplace.json").is_file():
                raise ValueError("Marketplace installation needs the original Reflect checkout")
            planned = marketplace_commands(name, args.remove, bool(previous))
            if not args.dry_run and not shutil.which(planned[0][0]):
                raise ValueError("Provider executable not found: " + planned[0][0])
            commands.extend((name, command) for command in planned)
            entry = {"method": method}
        if args.remove:
            installed.pop(name, None)
        else:
            installed[name] = entry
        if name == "antigravity" and not args.remove:
            print("antigravity: skills + manual capture; no automatic hook adapter.")
        if providers.PROVIDERS[name]["history"] == "import" and not args.remove:
            print(name + ": historical scan requires --history FILE; queue review works directly.")
    new_registry = encoded(registry)
    if not registry_path().exists() or registry_path().read_bytes() != new_registry:
        writes[registry_path()] = new_registry
    # Validate all local destinations before executing any external mutation.
    if args.dry_run:
        apply_writes(writes, True)
        for _, command in commands:
            print("RUN    " + shell_command(command))
        return 0
    for path in writes:
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("Refusing symlink destination: " + str(path))
    for name, command in commands:
        if command[2:4] == ["marketplace", "add"] and marketplace_registered(name):
            print(name + ": marketplace already registered; using its configured source.")
            continue
        print("RUN    " + shell_command(command), flush=True)
        result = subprocess.run(command, check=False)
        if result.returncode:
            raise RuntimeError(name + " marketplace command failed; earlier CLI operations may have succeeded. Rerun init to reconcile.")
    apply_writes(writes)
    print("Provider setup complete. Restart selected providers and review hook trust prompts.")
    return 0
