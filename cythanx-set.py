#!/usr/bin/env python3
"""CYX Prompt Auto - interactive XMRig/RandomX controller.

Interactive controller for a locally installed XMRig executable. It manages a
small JSON config, launches/stops XMRig, reports status, and can generate
RandomX test jobs through the existing cyx-randomx-jobs-nanswap.py module.

It does not download software, alter system startup, or run hidden/background
persistence. Mining starts only after an explicit `start` command.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "cyx-xmrig-config.json"
STATE_PATH = APP_DIR / "cyx-prompt-state.json"
DEFAULT_POOL = "xmrig.nanswap.com:3333"
DEFAULT_ALGO = "rx/0"
DEFAULT_PASS = "x"
CYTHANX_MODULE_NAME = "cyx_pool_genesis_runtime"


def locate_cythanx_source() -> Path | None:
    """Find the CYTHANX source without assuming the controller's directory."""
    candidates = [
        APP_DIR / "cyx-pool-genesis.py",
        Path.cwd() / "cyx-pool-genesis.py",
        APP_DIR.parent / "cyx-pool-genesis.py",
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            return candidate
    return None


DEFAULT_CONFIG: dict[str, Any] = {
    "autosave": True,
    "cpu": True,
    "opencl": False,
    "cuda": False,
    "pools": [{
        "algo": DEFAULT_ALGO,
        "coin": "monero",
        "url": DEFAULT_POOL,
        "user": "",
        "pass": DEFAULT_PASS,
        "keepalive": True,
        "tls": False,
    }],
}


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        with path.open("r", encoding="utf-8") as f:
            value = json.load(f)
        if not isinstance(value, dict):
            raise ValueError("JSON root must be an object")
        return value
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Config warning: {exc}; using defaults.")
        return json.loads(json.dumps(default))


def save_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def pool(cfg: dict[str, Any]) -> dict[str, Any]:
    pools = cfg.setdefault("pools", [])
    if not pools:
        pools.append(dict(DEFAULT_CONFIG["pools"][0]))
    return pools[0]


def find_xmrig(explicit: str | None = None) -> str | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend([
        APP_DIR / "xmrig",
        APP_DIR / "xmrig.exe",
        Path.cwd() / "xmrig",
        Path.cwd() / "xmrig.exe",
    ])
    for p in candidates:
        if p.is_file():
            return str(p)
    import shutil
    return shutil.which("xmrig") or shutil.which("xmrig.exe")


def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status(cfg: dict[str, Any], proc: subprocess.Popen | None) -> None:
    p = pool(cfg)
    running = bool(proc and proc.poll() is None)
    pid = proc.pid if running and proc else None
    print(f"\nXMRig: {'RUNNING' if running else 'STOPPED'}" + (f" (PID {pid})" if pid else ""))
    print(f"Pool:  {p.get('url', '')}")
    print(f"Algo:  {p.get('algo', '')}")
    print(f"User:  {'set' if p.get('user') else 'NOT SET'}")
    print(f"TLS:   {bool(p.get('tls', False))}")
    print(f"Config: {CONFIG_PATH}")


def validate(cfg: dict[str, Any]) -> list[str]:
    errors = []
    p = pool(cfg)
    for key in ("url", "user", "pass"):
        if not str(p.get(key, "")).strip():
            errors.append(f"pool.{key} is empty")
    if not str(p.get("algo", "")).lower().startswith("rx"):
        errors.append("pool.algo must be a RandomX algorithm such as rx/0")
    return errors


def save_state(pid: int | None = None) -> None:
    save_json(STATE_PATH, {"pid": pid, "updated": int(time.time())})


def start(cfg: dict[str, Any], proc: subprocess.Popen | None, xmrig: str | None) -> subprocess.Popen | None:
    if proc and proc.poll() is None:
        print("Already running.")
        return proc
    errors = validate(cfg)
    if errors:
        print("Cannot start:")
        for e in errors:
            print(f"  - {e}")
        print("Use `wallet`, `pool`, or `set` to configure it first.")
        return None
    xmrig = find_xmrig(xmrig)
    if not xmrig:
        print("XMRig executable not found. Put xmrig/xmrig.exe beside this script or use `xmrig <path>`.")
        return None
    save_json(CONFIG_PATH, cfg)
    print(f"Starting {xmrig} with {CONFIG_PATH} ...")
    try:
        kwargs: dict[str, Any] = {"cwd": str(Path(xmrig).resolve().parent)}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen([xmrig, "--config", str(CONFIG_PATH)], **kwargs)
        save_state(proc.pid)
        print(f"Started PID {proc.pid}.")
        return proc
    except OSError as exc:
        print(f"Start failed: {exc}")
        return None


def stop(proc: subprocess.Popen | None) -> subprocess.Popen | None:
    if not proc or proc.poll() is not None:
        print("XMRig is not running from this session.")
        save_state(None)
        return None
    print(f"Stopping PID {proc.pid} ...")
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
    save_state(None)
    print("Stopped.")
    return None


def set_value(cfg: dict[str, Any], key: str, value: str) -> None:
    """Set miner or CYTHANX organism parameters from the prompt.

    Organism parameters are stored under cfg["organism"] so they can be
    reused by later organism commands without altering pool settings.
    """
    p = pool(cfg)
    aliases = {
        "pool": "url", "wallet": "user", "password": "pass",
        "algorithm": "algo", "coin": "coin", "organism_command": "organism.command",
        "coin": "coin",
        "organism_action": "organism.command",
        "organism_payload": "organism.payload",
        "organism_previous": "organism.previous",
    }
    raw_key = key.strip().lower()
    key = aliases.get(raw_key, raw_key)

    if key.startswith("organism."):
        o = cfg.setdefault("organism", {})
        field = key.split(".", 1)[1]
        if field == "pool":
            # Accept either HOST:PORT or a URL-like endpoint. Keep this
            # separate from the XMRig RandomX pool: CYTHANX jobs require the
            # source-defined mining.notify consensus fields.
            endpoint = value.strip()
            if not endpoint:
                print("organism.pool cannot be empty")
                return
            o["pool"] = endpoint
            save_json(CONFIG_PATH, cfg)
            print(f"Set organism.pool = {endpoint}")
            return
        if field not in {"command", "payload", "previous", "pool"}:
            print("Organism parameters: organism.command, organism.payload, organism.previous, organism.pool")
            return
        o[field] = value
        save_json(CONFIG_PATH, cfg)
        print(f"Set {key} = {o[field]}")
        return

    key = aliases.get(key, key)
    if key not in {"url", "user", "pass", "algo", "coin", "tls", "keepalive"}:
        print("Settable keys: pool, wallet, password, algorithm, coin, tls, keepalive, "
              "organism.command, organism.payload, organism.previous, organism.pool")
        return
    if key in {"tls", "keepalive"}:
        value = value.lower() in {"1", "true", "yes", "on"}
    if key == "coin" and value.strip().lower() in {"", "auto", "none", "null"}:
        p.pop("coin", None)
        save_json(CONFIG_PATH, cfg)
        print("Cleared XMRig coin hint (automatic detection).")
        return
    p[key] = value.strip() if isinstance(value, str) else value
    save_json(CONFIG_PATH, cfg)
    print(f"Set {key} = {p[key]}")


def run_configured_organism(cfg: dict[str, Any]) -> None:
    """Execute the organism action stored by set organism.command."""
    o = cfg.get("organism", {})
    command = str(o.get("command", "status")).strip().lower()
    payload = str(o.get("payload", ""))
    previous = str(o.get("previous", ""))

    # The interactive organism functions remain available for commands that
    # need arbitrary input. Configured payload/previous values are supplied
    # through temporary stdin-free wrappers below.
    if command in {"status", "info", "show"}:
        organism_status()
    elif command in {"vector"}:
        organism_vector()
    elif command in {"genesis", "job"}:
        organism_genesis_job()
    elif command in {"cythanize", "cythan", "transform", "transmogrify", "joatt"}:
        print(f"Configured organism command: {command}")
        print("Use the corresponding organism command to supply/execute payload state.")
        if payload:
            print(f"payload={payload}")
        if previous:
            print(f"previous={previous}")
    else:
        print("Unknown organism.command. Use status, vector, cythanize, transform, joatt, or genesis.")



def load_cythanx():
    """Load the existing CYTHANX organism/consensus implementation."""
    source = locate_cythanx_source()
    if source is None:
        raise FileNotFoundError(
            "Missing cyx-pool-genesis.py; place it beside cyx-prompt-auto.py "
            "or in the current working directory."
        )
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location(CYTHANX_MODULE_NAME, source)
    if not spec or not spec.loader:
        raise RuntimeError("unable to load CYTHANX source")
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _organism_payload(prompt: str) -> bytes:
    raw = input(prompt).strip()
    if raw.lower().startswith("0x"):
        return bytes.fromhex(raw[2:])
    return raw.encode("utf-8")


def _organism_previous(prompt: str, size: int = 32) -> bytes:
    raw = input(f"{prompt} [zero]: ").strip()
    if not raw:
        return b"\x00" * size
    if raw.lower().startswith("0x"):
        raw = raw[2:]
    value = bytes.fromhex(raw)
    if len(value) != size:
        raise ValueError(f"expected {size} bytes, got {len(value)}")
    return value


def organism_vector() -> None:
    try:
        mod = load_cythanx()
        vector = mod.cythan_vector()
        print(json.dumps({"vector": vector["vector"].hex(), "joaat": f'{vector["joaat"]:08x}'}, indent=2))
    except Exception as exc:
        print(f"Organism vector failed: {exc}")


def organism_cythanize() -> None:
    try:
        mod = load_cythanx()
        payload = _organism_payload("CYTHAN payload (text or 0xhex): ")
        previous = _organism_previous("Previous state hex")
        print(json.dumps(mod.cythanize(payload, previous), indent=2))
    except Exception as exc:
        print(f"CYTHANIZE failed: {exc}")


def organism_transform() -> None:
    """Run the source-defined CYTHAN transmogrification pipeline."""
    try:
        mod = load_cythanx()
        payload = _organism_payload("Organism payload (text or 0xhex): ")
        previous = _organism_previous("Previous state hex")
        print(json.dumps(mod.transmogrify(payload, previous), indent=2))
    except Exception as exc:
        print(f"Organism transform failed: {exc}")


def organism_joatt() -> None:
    """Run the source-defined JOATT state derivation."""
    try:
        mod = load_cythanx()
        payload = _organism_payload("JOATT payload (text or 0xhex): ")
        previous = _organism_previous("Previous JOATT state hex")
        print(json.dumps(mod.joatt(payload, previous), indent=2))
    except Exception as exc:
        print(f"JOATT derivation failed: {exc}")


def organism_genesis_job() -> None:
    """Emit the deterministic CYTHANX genesis mining job from the source."""
    try:
        mod = load_cythanx()
        result = mod.pool_genesis_payload()
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(f"Genesis job generation failed: {exc}")


def organism_command(args2: list[str]) -> None:
    """Dispatch the explicit CYTHANX organism command family."""
    sub = args2[0].lower() if args2 else "status"
    aliases = {
        "status": "status", "info": "status", "show": "status",
        "vector": "vector", "cythanize": "cythanize", "cythan": "cythanize",
        "transform": "transform", "transmogrify": "transform",
        "joatt": "joatt", "genesis": "genesis", "job": "genesis",
        "help": "help", "list": "help",
    }
    action = aliases.get(sub)
    if action == "status":
        organism_status()
    elif action == "vector":
        organism_vector()
    elif action == "cythanize":
        organism_cythanize()
    elif action == "transform":
        organism_transform()
    elif action == "joatt":
        organism_joatt()
    elif action == "genesis":
        organism_genesis_job()
    elif action == "help":
        print("Organism commands: status, vector, cythanize, transform, joatt, genesis")
    else:
        print("Organism commands: status, vector, cythanize, transform, joatt, genesis")


def organism_status() -> None:
    """Show the CYTHANX organism primitives and current local chain state."""
    try:
        mod = load_cythanx()
        vector = mod.cythan_vector()
        print("\nCYTHANX ORGANISM")
        print(f"Network:   {getattr(mod, 'NETWORK', 'unknown')}")
        print(f"Chain ID:  {getattr(mod, 'CHAIN_ID', 'unknown')}")
        print(f"Vector:    {vector['vector'].hex()}")
        print(f"JOAAT:     {vector['joaat']:08x}")
        print(f"Protocol:  {getattr(mod, 'POOL_PROTOCOL', 'unknown')}")
        chain_cls = getattr(mod, 'CythanMainnet', None)
        if chain_cls:
            chain = chain_cls()
            print(f"Height:    {len(chain.chain) - 1}")
            print(f"Tip:       {chain.latest.hash}")
            print(f"Target:    {chain.latest.target}")
    except Exception as exc:
        print(f"Organism status failed: {exc}")

def generate_job() -> None:
    try:
        from importlib.util import spec_from_file_location, module_from_spec
        source = APP_DIR / "cyx-randomx-jobs-nanswap.py"
        if not source.exists():
            print(f"Missing {source}")
            return
        spec = spec_from_file_location("cyx_randomx_jobs_nanswap", source)
        if not spec or not spec.loader:
            raise RuntimeError("unable to load job module")
        mod = module_from_spec(spec)
        spec.loader.exec_module(mod)
        height = int(input("Block height [1]: ").strip() or "1")
        job = mod.RandomXJobGenerator().generate(height=height)
        print(json.dumps(job.notify_params(), indent=2))
    except Exception as exc:
        print(f"Job generation failed: {exc}")



def edit_config(cfg: dict[str, Any]) -> None:
    """Interactively edit the managed XMRig + CYTHANX organism config."""
    p = pool(cfg)
    o = cfg.setdefault("organism", {})
    fields = [
        ("pool", p.get("url", ""), "XMRig pool"),
        ("user", p.get("user", ""), "XMRig wallet/user"),
        ("pass", p.get("pass", "x"), "XMRig password"),
        ("algo", p.get("algo", "rx/0"), "XMRig algorithm"),
        ("coin", p.get("coin", "monero"), "XMRig coin identifier (blank keeps current)"),
        ("tls", "on" if p.get("tls", False) else "off", "XMRig TLS"),
        ("keepalive", "on" if p.get("keepalive", True) else "off", "XMRig keepalive"),
        ("organism.pool", o.get("pool", ""), "CYTHANX organism pool"),
        ("organism.command", o.get("command", "status"), "organism command"),
        ("organism.payload", o.get("payload", ""), "organism payload"),
        ("organism.previous", o.get("previous", ""), "organism previous state"),
    ]
    print("Edit config — press Enter to keep the current value.")
    for key, current, label in fields:
        value = input(f"{label} [{current}]: ").strip()
        if not value:
            continue
        set_value(cfg, key, value)
    save_json(CONFIG_PATH, cfg)
    print(f"Saved {CONFIG_PATH}")

def help_text() -> None:
    print("""
Commands:
  start                         Start XMRig using the managed config
  stop                          Stop XMRig started by this controller
  restart                       Stop then start XMRig
  status                        Show miner/config status
  pool [HOST:PORT]              Set pool endpoint
  wallet [ADDRESS]              Set payout/user address
  password [VALUE]              Set pool password
  algorithm [rx/0]              Set XMRig algorithm
  coin [monero]                 Set XMRig coin identifier
  set KEY VALUE                 Set miner or CYTHANX organism parameters
  set coin NAME                 Set XMRig coin hint (e.g. monero; blank/auto to omit)
  set coin VALUE                Set XMRig coin identifier
  set organism pool HOST:PORT   Set the CYTHANX organism pool endpoint
  tls on|off                    Toggle TLS in generated config
  config                        Print the current XMRig JSON config
  edit config                   Interactively edit XMRig + organism settings
  save                          Save current config
  job                           Generate a synthetic RandomX test job
  organism status               Show CYTHANX organism/chain state
  organism vector               Show CYTHAN vector and JOAAT seed
  organism cythanize            Derive source-defined CYTHAN state
  organism transform            Run CYTHAN transmogrification
  organism joatt                Derive source-defined JOATT state
  organism genesis              Emit deterministic CYTHANX genesis job
  xmrig [PATH]                  Set the XMRig executable path for this session
  debug                         Print validation diagnostics
  help                          Show commands
  quit                          Exit (does not stop a running miner)

Prompts also accept natural aliases such as:
  "start mining", "stop mining", "show status", "configure pool",
  "set wallet", and "generate job".
""")


def main() -> int:
    global CONFIG_PATH
    ap = argparse.ArgumentParser(description="Prompt-driven XMRig/RandomX controller")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--xmrig", default=None, help="path to xmrig executable")
    args = ap.parse_args()

    CONFIG_PATH = Path(args.config).expanduser().resolve()
    cfg = load_json(CONFIG_PATH, DEFAULT_CONFIG)
    proc: subprocess.Popen | None = None
    xmrig = args.xmrig

    print("CYX Prompt Auto — type `help` for commands.")
    status(cfg, proc)
    while True:
        try:
            raw = input("\ncyx> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            print(f"Input error: {exc}")
            continue
        cmd = parts[0].lower()
        args2 = parts[1:]
        normalized = raw.lower()

        if cmd in {"quit", "exit", "q"}:
            break
        if cmd in {"help", "?"}:
            help_text(); continue
        if cmd == "start" or "start mining" in normalized:
            proc = start(cfg, proc, xmrig); continue
        if cmd == "stop" or "stop mining" in normalized:
            proc = stop(proc); continue
        if cmd == "restart":
            proc = stop(proc); proc = start(cfg, proc, xmrig); continue
        if cmd == "status" or "show status" in normalized:
            status(cfg, proc); continue
        if cmd == "config":
            print(json.dumps(cfg, indent=2)); continue
        if (cmd == "edit" and args2 and args2[0].lower() == "config") or cmd == "edit-config":
            edit_config(cfg); continue
        if cmd == "save":
            save_json(CONFIG_PATH, cfg); print(f"Saved {CONFIG_PATH}"); continue
        if cmd == "job" or "generate job" in normalized:
            generate_job(); continue
        if cmd in {"organism", "cythanx"} or "show organism" in normalized:
            organism_command(args2)
            continue
        if cmd == "xmrig":
            if args2: xmrig = args2[0]
            print(f"XMRig path: {xmrig or find_xmrig() or 'not found'}"); continue
        if cmd in {"pool", "wallet", "password", "algorithm", "coin", "tls", "keepalive"}:
            key = "pool" if cmd == "pool" else cmd
            value = args2[0] if args2 else input(f"{key}: ").strip()
            if cmd in {"tls", "keepalive"} and not args2:
                value = input(f"{key} on/off: ").strip()
            set_value(cfg, key, value); continue
        if cmd == "set" and args2 and args2[0].lower() == "organism" and len(args2) >= 3 and args2[1].lower() == "pool":
            set_value(cfg, "organism.pool", " ".join(args2[2:])); continue
        if cmd == "set" and args2 and args2[0].lower() in {"organism", "organism.command", "organism_action"} and len(args2) >= 2:
            # `set organism <command>` selects the organism action.
            if args2[0].lower() == "organism" and len(args2) == 2:
                set_value(cfg, "organism.command", args2[1])
            else:
                set_value(cfg, args2[0], " ".join(args2[1:]))
            continue
        if cmd == "set" and len(args2) >= 2:
            set_value(cfg, args2[0], " ".join(args2[1:])); continue
        if cmd in {"organism-run", "run-organism"}:
            run_configured_organism(cfg); continue
        if cmd == "debug":
            print("Validation:")
            errors = validate(cfg)
            print("  OK" if not errors else "  " + "\n  ".join(errors))
            print(f"XMRig discovered: {find_xmrig(xmrig) or 'NO'}")
            print(f"Config path: {CONFIG_PATH}")
            source = locate_cythanx_source()
            print(f"CYTHANX source: {source or 'MISSING'}")
            continue
        print("Unknown command. Type `help`.")

    print("Exiting controller. Any XMRig process already started is left running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
