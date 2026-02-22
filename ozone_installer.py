#!/usr/bin/env python3

# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

# Optional nicer interactive prompts (fallback to built-in prompts if unavailable)
try:
    import click  # type: ignore
except Exception:
    click = None  # type: ignore

ANSIBLE_ROOT = Path(__file__).resolve().parent
ANSIBLE_CFG = ANSIBLE_ROOT / "ansible.cfg"
PLAYBOOKS_DIR = ANSIBLE_ROOT / "playbooks"
LOGS_DIR = ANSIBLE_ROOT / "logs"
LAST_FAILED_FILE = LOGS_DIR / "last_failed_task.txt"
LAST_RUN_FILE = LOGS_DIR / "last_run.json"

DEFAULTS = {
    "install_base": "/opt/ozone",
    "data_base": "/data/ozone",
    "ozone_version": "2.0.0",
    "jdk_major": 17,
    "service_user": "ozone",
    "service_group": "ozone",
    "dl_url": "https://dlcdn.apache.org/ozone",
    "JAVA_MARKER": "Apache Ozone Installer Java Home",
    "ENV_MARKER": "Apache Ozone Installer Env",
    "start_after_install": True,
    "use_sudo": True,
}

def get_logger(log_path: Optional[Path] = None) -> logging.Logger:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    logger = logging.getLogger("ozone_installer")
    logger.setLevel(logging.INFO)
    dest = log_path or (LOGS_DIR / "ansible.log")
    if not logger.handlers:
        fh = logging.FileHandler(dest)
        fh.setLevel(logging.INFO)
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        sh = logging.StreamHandler(sys.stdout)
        logger.addHandler(sh)
    elif log_path:
        # Replace FileHandler when a new run-specific path is requested
        for h in list(logger.handlers):
            if isinstance(h, logging.FileHandler):
                logger.removeHandler(h)
                h.close()
        fh = logging.FileHandler(dest)
        fh.setLevel(logging.INFO)
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    return logger

def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Ozone Ansible Installer (Python trigger) - mirrors bash installer flags"
    )
    p.add_argument("-H", "--host", help="Target host(s). Non-HA: host. HA: comma-separated or brace expansion host{1..n}")
    p.add_argument("-F", "--host-file", help="Host file. Plain list = all-in-one. Use [masters] and [workers] sections for master/worker split")
    p.add_argument("-m", "--auth-method", choices=["password", "key"], default=None)
    p.add_argument("-p", "--password", help="SSH password (for --auth-method=password)")
    p.add_argument("-k", "--keyfile", help="SSH private key file (for --auth-method=key)")
    p.add_argument("-v", "--version", help="Ozone version (e.g., 2.0.0) or 'local'")
    p.add_argument("-i", "--install-dir", help=f"Install root (default: {DEFAULTS['install_base']})")
    p.add_argument("-d", "--data-dir", help=f"Data root(s), comma-separated or brace expansion e.g. /data/ozone{{1..3}} (default: {DEFAULTS['data_base']})")
    p.add_argument("-s", "--start", action="store_true", help="Initialize and start after install")
    p.add_argument("-M", "--cluster-mode", choices=["non-ha", "ha"], help="Force cluster mode (default: auto by host count)")
    p.add_argument("-r", "--role-file", help="Role file (YAML) for HA mapping (optional)")
    p.add_argument("-j", "--jdk-version", type=int, choices=[17, 21], help="JDK major version (default: 17)")
    p.add_argument("-c", "--config-dir", help="Config dir (optional, templates are used by default)")
    p.add_argument("-x", "--clean", action="store_true", help="(Reserved) Cleanup before install [not yet implemented]")
    p.add_argument("--stop-only", action="store_true", help="Stop Ozone processes and exit (no cleanup)")
    p.add_argument("--stop-and-clean", action="store_true", help="Stop Ozone processes, remove install and data dirs, then exit")
    p.add_argument("-l", "--ssh-user", help="SSH username (default: root)")
    p.add_argument("-S", "--use-sudo", action="store_true", help="Run remote commands via sudo (default)")
    p.add_argument("-u", "--service-user", help="Service user (default: ozone)")
    p.add_argument("-g", "--service-group", help="Service group (default: ozone)")
    # Local extras
    p.add_argument("--local-path", help="Path to local Ozone build (contains bin/ozone)")
    p.add_argument("--dl-url", help="Upstream download base URL")
    p.add_argument("--python-interpreter", help="Python interpreter for managed nodes (e.g., /usr/bin/python3.9). If not specified, Ansible will auto-detect.")
    p.add_argument("--verbose", "-V", action="store_true", help="Verbose output (passes -vvv to Ansible for detailed debugging)")
    p.add_argument("--yes", action="store_true", help="Non-interactive; accept defaults for missing values")
    p.add_argument("-R", "--resume", action="store_true", help="Resume play at last failed task (if available)")
    return p.parse_args(argv)

def _validate_local_ozone_dir(path: Path) -> bool:
    """
    Returns True if 'path/bin/ozone' exists and is executable.
    """
    ozone_bin = path / "bin" / "ozone"
    try:
        return ozone_bin.exists() and os.access(str(ozone_bin), os.X_OK)
    except OSError:
        return False

def prompt(prompt_text: str, default: Optional[str] = None, secret: bool = False, yes_mode: bool = False) -> Optional[str]:
    if yes_mode:
        return default
    if click is not None and sys.stdout.isatty():
        try:
            display = prompt_text
            # logger.info(f"prompt_text: {prompt_text} , default: {default}")
            if default:
                display = f"{prompt_text} [default={default}]"
            if secret:
                return click.prompt(display, default=default, hide_input=True, show_default=False)
            return click.prompt(display, default=default, show_default=False)
        except (EOFError, KeyboardInterrupt):
            return default
    # Fallback to built-in input/getpass
    try:
        text = f"{prompt_text}: "
        if default:
            text = f"{prompt_text} [default={default}]: "
        if secret:
            import getpass
            val = getpass.getpass(text)
        else:
            val = input(text)
        if not val and default is not None:
            return default
        return val
    except EOFError:
        return default

def _semver_key(v: str) -> Tuple[int, int, int, str]:
    """
    Convert version like '2.0.0' or '2.1.0-RC0' to a sortable key.
    Pre-release suffix sorts before final.
    """
    try:
        core, *rest = v.split("-", 1)
        major, minor, patch = core.split(".")
        suffix = rest[0] if rest else ""
        return (int(major), int(minor), int(patch), suffix)
    except Exception:
        return (0, 0, 0, v)

def _render_table(rows: List[Tuple[str, str]]) -> str:
    """
    Returns a simple two-column table string without extra dependencies.
    """
    if not rows:
        return ""
    col1_width = max(len(k) for k, _ in rows)
    col2_width = max(len(str(v)) for _, v in rows)
    sep = f"+-{'-' * col1_width}-+-{'-' * col2_width}-+"
    out = [sep, f"| {'Field'.ljust(col1_width)} | {'Value'.ljust(col2_width)} |", sep]
    for k, v in rows:
        out.append(f"| {k.ljust(col1_width)} | {str(v).ljust(col2_width)} |")
    out.append(sep)
    return "\n".join(out)

def _confirm_summary(rows: List[Tuple[str, str]], yes_mode: bool) -> bool:
    """
    Print the input summary table and ask user to continue. Returns True if confirmed.
    """
    logger = get_logger()
    table = _render_table(rows)
    if click is not None:
        logger.info(table)
        if yes_mode:
            return True
        return click.confirm("Proceed with these settings?", default=True)
    else:
        logger.info(table)
        if yes_mode:
            return True
        answer = prompt("Proceed with these settings? (Y/n)", default="Y", yes_mode=False)
        return str(answer or "Y").strip().lower() in ("y", "yes")

def fetch_available_versions(dl_url: str, limit: int = 30) -> List[str]:
    """
    Fetch available Ozone versions from the download base. Returns newest-first.
    """
    try:
        import urllib.request
        with urllib.request.urlopen(dl_url, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        # Apache directory listing usually has anchors like href="2.0.0/"
        candidates = set(m.group(1) for m in re.finditer(r'href="([0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9]+)?)\/"', html))
        versions = sorted(candidates, key=_semver_key, reverse=True)
        if limit and len(versions) > limit:
            versions = versions[:limit]
        return versions
    except Exception:
        return []

def choose_version_interactive(versions: List[str], default_version: str, yes_mode: bool) -> Optional[str]:
    """
    Present a numbered list and prompt user to choose a version.
    Returns selected version string or None if not chosen.
    """
    if not versions:
        return None
    if yes_mode:
        return versions[0]
    # Use click when available and interactive; otherwise fallback to basic prompt
    if click is not None and sys.stdout.isatty():
        click.echo("Available Ozone versions (newest first):")
        for idx, ver in enumerate(versions, start=1):
            click.echo(f"  {idx}) {ver}")
        while True:
            choice = prompt(
                "Select number, type a version (e.g., 2.1.0) or 'local'",
                default="1",
                yes_mode=False,
            )
            if choice is None:
                return versions[0]
            choice = str(choice).strip()
            if choice == "":
                return versions[0]
            if choice.lower() == "local":
                return "local"
            if choice.isdigit():
                i = int(choice)
                if 1 <= i <= len(versions):
                    return versions[i - 1]
            if re.match(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9]+)?$", choice):
                return choice
            click.echo("Invalid selection. Enter a number, a valid version, or 'local'.")
    else:
        logger = get_logger()
        logger.info("Available Ozone versions:")
        for idx, ver in enumerate(versions, start=1):
            logger.info(f"  {idx}) {ver}")
        while True:
            choice = prompt("Select number, type a version (e.g., 2.1.0) or 'local'", default="1", yes_mode=False)
            if choice is None or str(choice).strip() == "":
                return versions[0]
            choice = str(choice).strip()
            if choice.lower() == "local":
                return "local"
            if choice.isdigit():
                i = int(choice)
                if 1 <= i <= len(versions):
                    return versions[i - 1]
            # allow typing a specific version not listed
            if re.match(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9]+)?$", choice):
                return choice
            logger.info("Invalid selection. Please enter a number from the list, a valid version (e.g., 2.1.0) or 'local'.")

def expand_braces(expr: str) -> List[str]:
    # Supports simple pattern like prefix{1..N}suffix
    if not expr or "{" not in expr or ".." not in expr or "}" not in expr:
        return [expr]
    m = re.search(r"(.*)\{(\d+)\.\.(\d+)\}(.*)", expr)
    if not m:
        return [expr]
    pre, a, b, post = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
    return [f"{pre}{i}{post}" for i in range(a, b + 1)]

def parse_data_dirs(data_raw: Optional[str]) -> str:
    """
    Accepts comma-separated data dirs; each may contain brace expansion (e.g. /data/ozone{1..3}).
    Returns comma-separated string of expanded paths.
    """
    if not data_raw:
        return data_raw or ""
    out = []
    for token in data_raw.split(","):
        token = token.strip()
        expanded = expand_braces(token)
        out.extend(expanded)
    return ",".join(out)

def parse_hosts(hosts_raw: Optional[str]) -> List[dict]:
    """
    Accepts comma-separated hosts; each may contain brace expansion.
    Returns list of dicts: {host, user, port}
    """
    if not hosts_raw:
        return []
    out = []
    for token in hosts_raw.split(","):
        token = token.strip()
        expanded = expand_braces(token)
        for item in expanded:
            user = None
            hostport = item
            if "@" in item:
                user, hostport = item.split("@", 1)
            host = hostport
            port = None
            if ":" in hostport:
                host, port = hostport.split(":", 1)
            out.append({"host": host, "user": user, "port": port})
    return out

def read_hosts_from_file(filepath: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Reads hosts from a file.

    Two formats supported:
    1) Master/worker: [masters] and [workers] sections (INI-style). Returns (masters_csv, workers_csv).
    2) Legacy: plain list, one host per line. Returns (hosts_csv, None).

    Lines starting with # are comments. Empty lines ignored. Supports user@host:port.
    """
    logger = get_logger()
    try:
        path = Path(filepath)
        if not path.exists():
            logger.error(f"Host file not found: {filepath}")
            return (None, None)
        masters: List[str] = []
        workers: List[str] = []
        flat: List[str] = []
        current_section: Optional[str] = None
        with path.open('r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith('[') and line.endswith(']'):
                    current_section = line[1:-1].lower()
                    continue
                if current_section == "masters":
                    masters.append(line)
                elif current_section == "workers":
                    workers.append(line)
                elif current_section is None:
                    flat.append(line)
        if masters and workers:
            logger.info(f"Read {len(masters)} master(s) and {len(workers)} worker(s) from {filepath}")
            return (','.join(masters), ','.join(workers))
        if flat:
            logger.info(f"Read {len(flat)} host(s) from {filepath}")
            return (','.join(flat), None)
        logger.error(f"No valid hosts found in {filepath}")
        return (None, None)
    except Exception as e:
        logger.error(f"Error reading host file {filepath}: {e}")
        return (None, None)

def auto_cluster_mode(hosts: List[dict], forced: Optional[str] = None, master_count: Optional[int] = None) -> str:
    if forced in ("non-ha", "ha"):
        return forced
    n = master_count if master_count is not None else len(hosts)
    return "ha" if n >= 3 else "non-ha"

def build_inventory(
    hosts: Optional[List[dict]] = None,
    master_hosts: Optional[List[dict]] = None,
    worker_hosts: Optional[List[dict]] = None,
    ssh_user: Optional[str] = None,
    keyfile: Optional[str] = None,
    password: Optional[str] = None,
    cluster_mode: str = "non-ha",
    python_interpreter: Optional[str] = None,
) -> str:
    """
    Returns INI inventory text for our groups: [om], [scm], [datanodes], [recon], [s3g]

    Either (hosts) for all-in-one, or (master_hosts, worker_hosts) for master/worker split.
    Masters run SCM, OM, Recon. Workers run Datanode, S3G.
    """
    use_master_worker = master_hosts is not None and worker_hosts is not None
    if use_master_worker:
        if not master_hosts or not worker_hosts:
            return ""
        # Master/worker: masters -> OM, SCM, Recon; workers -> Datanodes, S3G
        om = master_hosts[:3] if cluster_mode == "ha" and len(master_hosts) >= 3 else master_hosts[:1]
        scm = master_hosts[:3] if cluster_mode == "ha" and len(master_hosts) >= 3 else master_hosts[:1]
        recon = [master_hosts[0]]
        dn = worker_hosts
        s3g = worker_hosts
        return _render_inv_groups(
            om=om, scm=scm, dn=dn, recon=recon, s3g=s3g,
            ssh_user=ssh_user, keyfile=keyfile, password=password, python_interpreter=python_interpreter
        )
    # Legacy: single host list, all roles derived from it
    if not hosts:
        return ""
    if cluster_mode == "non-ha":
        h = hosts[0]
        return _render_inv_groups(
            om=[h], scm=[h], dn=hosts, recon=[h], s3g=[h],
            ssh_user=ssh_user, keyfile=keyfile, password=password, python_interpreter=python_interpreter
        )
    om = hosts[:3] if len(hosts) >= 3 else hosts
    scm = hosts[:3] if len(hosts) >= 3 else hosts
    dn = hosts
    recon = [hosts[0]]
    s3g = [hosts[0]]
    return _render_inv_groups(om=om, scm=scm, dn=dn, recon=recon, s3g=s3g,
                              ssh_user=ssh_user, keyfile=keyfile, password=password, python_interpreter=python_interpreter)

def _render_inv_groups(om: List[dict], scm: List[dict], dn: List[dict], recon: List[dict], s3g: List[dict], ssh_user: Optional[str] = None, keyfile: Optional[str] = None, password: Optional[str] = None, python_interpreter: Optional[str] = None) -> str:
    def hostline(hd):
        parts = [hd["host"]]
        if ssh_user or hd.get("user"):
            parts.append(f"ansible_user={(ssh_user or hd.get('user'))}")
        if hd.get("port"):
            parts.append(f"ansible_port={hd['port']}")
        if keyfile:
            parts.append(f"ansible_ssh_private_key_file={shlex.quote(str(keyfile))}")
        if password:
            parts.append(f"ansible_password={shlex.quote(password)}")
        if python_interpreter:
            parts.append(f"ansible_python_interpreter={python_interpreter}")
        return " ".join(parts)

    sections = []
    sections.append("[om]")
    sections += [hostline(h) for h in om]
    sections.append("\n[scm]")
    sections += [hostline(h) for h in scm]
    sections.append("\n[datanodes]")
    sections += [hostline(h) for h in dn]
    sections.append("\n[recon]")
    sections += [hostline(h) for h in recon]
    sections.append("\n[s3g]")
    sections += [hostline(h) for h in s3g]
    sections.append("\n")
    return "\n".join(sections)

def run_playbook(playbook: Path, inventory_path: Path, extra_vars_path: Path, ask_pass: bool = False, become: bool = True, start_at_task: Optional[str] = None, tags: Optional[List[str]] = None, verbose: bool = False) -> int:
    cmd = [
        "ansible-playbook",
        "-i", str(inventory_path),
        str(playbook),
        "-e", f"@{extra_vars_path}",
    ]
    if verbose:
        cmd.append("-vvv")
    if ask_pass:
        cmd.append("-k")
    if become:
        cmd.append("--become")
    if start_at_task:
        cmd += ["--start-at-task", str(start_at_task)]
    if tags:
        cmd += ["--tags", ",".join(tags)]
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_CFG)
    # Route Ansible logs to the same file as the Python logger
    log_path = LOGS_DIR / "ansible.log"
    try:
        logger = get_logger()
        for h in logger.handlers:
            if isinstance(h, logging.FileHandler):
                # type: ignore[attr-defined]
                log_path = Path(getattr(h, "baseFilename"))  # type: ignore
                break
    except Exception:
        pass
    env["ANSIBLE_LOG_PATH"] = str(log_path)
    logger = get_logger()
    if start_at_task:
        logger.info(f"Resuming from task: {start_at_task}")
    if tags:
        logger.info(f"Using tags: {','.join(tags)}")
    logger.info(f"Running: {' '.join(shlex.quote(c) for c in cmd)}")
    return subprocess.call(cmd, env=env)

def main(argv: List[str]) -> int:
    args = parse_args(argv)
    # Resume mode: reuse last provided configs and suppress prompts when possible
    resuming = bool(getattr(args, "resume", False))
    yes = True if resuming else bool(args.yes)
    last_cfg = None
    if resuming and LAST_RUN_FILE.exists():
        try:
            last_cfg = json.loads(LAST_RUN_FILE.read_text(encoding="utf-8"))
        except Exception:
            last_cfg = None

    # Gather inputs: from host file ([masters]/[workers] sections) or -H (legacy)
    masters_raw = None
    workers_raw = None
    hosts_raw = None
    master_hosts: List[dict] = []
    worker_hosts: List[dict] = []
    hosts: List[dict] = []
    host_file_path = args.host_file or (last_cfg.get("host_file") if last_cfg else None)

    if host_file_path:
        file_masters, file_workers = read_hosts_from_file(host_file_path)
        if file_masters is None and file_workers is None:
            logger = get_logger()
            logger.error(f"Error: Could not read hosts from file: {host_file_path}")
            return 2
        if file_workers is not None:
            # File has [masters] and [workers] sections
            masters_raw = file_masters
            workers_raw = file_workers
            master_hosts = parse_hosts(masters_raw) if masters_raw else []
            worker_hosts = parse_hosts(workers_raw) if workers_raw else []
        else:
            # Legacy: plain host list
            hosts_raw = file_masters
            hosts = parse_hosts(hosts_raw) if hosts_raw else []
    else:
        hosts_raw_default = (last_cfg.get("hosts_raw") if last_cfg else None)
        hosts_raw = args.host or hosts_raw_default or prompt("Target host(s) [non-ha: host | HA: h1,h2,h3 or brace expansion]", default="", yes_mode=yes)
        hosts = parse_hosts(hosts_raw) if hosts_raw else []

    use_master_worker = bool(masters_raw is not None and workers_raw is not None)
    # Initialize per-run logger as soon as we have host info
    try:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        raw_hosts_for_name = (hosts_raw or masters_raw or workers_raw or "").strip()
        safe_hosts = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw_hosts_for_name)[:80] or "hosts"
        run_log_path = LOGS_DIR / f"ansible-{ts}-{safe_hosts}.log"
        logger = get_logger(run_log_path)
        logger.info(f"Logging to: {run_log_path}")
    except Exception:
        run_log_path = LOGS_DIR / "ansible.log"
        logger = get_logger(run_log_path)
        logger.info(f"Logging to: {run_log_path} (fallback)")

    if use_master_worker:
        if not master_hosts or not worker_hosts:
            logger.error("Error: Host file must have both [masters] and [workers] sections with at least one host each.")
            return 2
    else:
        if not hosts:
            logger.error("Error: No hosts provided (-H/--host or -F/--host-file).")
            return 2

    stop_only = bool(getattr(args, "stop_only", False))
    stop_and_clean = bool(getattr(args, "stop_and_clean", False))

    # Decide HA vs Non-HA with user input; default depends on master count
    master_count = len(master_hosts) if use_master_worker else len(hosts)
    resume_cluster_mode = (last_cfg.get("cluster_mode") if last_cfg else None)
    if args.cluster_mode:
        cluster_mode = args.cluster_mode
    elif resume_cluster_mode:
        cluster_mode = resume_cluster_mode
    else:
        default_mode = auto_cluster_mode(hosts or [], master_count=master_count)
        selected = prompt("Deployment type (option: ha or non-ha)", default=default_mode, yes_mode=yes)
        cluster_mode = (selected or default_mode).strip().lower()
        if cluster_mode not in ("ha", "non-ha"):
            cluster_mode = default_mode
    if cluster_mode == "ha" and master_count < 3:
        logger.error("Error: HA requires at least 3 master hosts (to map 3 OMs and 3 SCMs).")
        return 2

    # Auth (needed for all modes: install, stop-only, stop-and-clean)
    auth_method = args.auth_method or (last_cfg.get("auth_method") if last_cfg else None) \
        or prompt("SSH authentication method (key or password)", default="password", yes_mode=yes)
    if auth_method not in ("key", "password"):
        auth_method = "password"
    ssh_user = args.ssh_user or (last_cfg.get("ssh_user") if last_cfg else None) \
        or prompt("SSH username", default="root", yes_mode=yes)
    password = args.password or (last_cfg.get("password") if last_cfg else None)
    keyfile = args.keyfile or (last_cfg.get("keyfile") if last_cfg else None)
    if auth_method == "password" and not password:
        password = prompt("SSH password", default="", secret=True, yes_mode=yes)
    if auth_method == "key" and not keyfile:
        keyfile = prompt("Path to SSH private key", default=str(Path.home() / ".ssh" / "id_ed25519"), yes_mode=yes)
    if auth_method == "password":
        keyfile = None
    elif auth_method == "key":
        password = None

    # Stop modes: gather minimal config, then fall through to common inventory + playbook run
    python_interpreter = None
    if stop_only:
        playbook = PLAYBOOKS_DIR / "stop.yml"
        extra_vars = {"cluster_mode": cluster_mode, "ssh_user": ssh_user, "controller_logs_dir": str(LOGS_DIR)}
        logger.info("Running stop only on cluster...")
    elif stop_and_clean:
        install_base = args.install_dir or (last_cfg.get("install_base") if last_cfg else None) or prompt("Install directory to remove", default=DEFAULTS["install_base"], yes_mode=yes)
        data_base_raw = args.data_dir or (last_cfg.get("data_base") if last_cfg else None) or prompt("Data directory to remove", default=DEFAULTS["data_base"], yes_mode=yes)
        data_base = parse_data_dirs(data_base_raw) if data_base_raw else (data_base_raw or DEFAULTS["data_base"])
        playbook = PLAYBOOKS_DIR / "stop_and_clean.yml"
        extra_vars = {"cluster_mode": cluster_mode, "ssh_user": ssh_user, "install_base": install_base, "data_base": data_base, "controller_logs_dir": str(LOGS_DIR)}
        logger.info("Running stop and clean on cluster...")
    else:
        # Full install: resolve version, paths, service config, etc.
        playbook = None  # set later
        extra_vars = None  # set later

    if not (stop_only or stop_and_clean):
        # Resolve download base early for version selection
        dl_url = args.dl_url or (last_cfg.get("dl_url") if last_cfg else None) or DEFAULTS["dl_url"]
        ozone_version = args.version or (last_cfg.get("ozone_version") if last_cfg else None)
        if not ozone_version:
            versions = fetch_available_versions(dl_url or DEFAULTS["dl_url"])
            selected = choose_version_interactive(versions, DEFAULTS["ozone_version"], yes_mode=yes)
            if selected:
                ozone_version = selected
            else:
                ozone_version = prompt("Ozone version (e.g., 2.1.0 | local)", default=DEFAULTS["ozone_version"], yes_mode=yes)
        jdk_major = args.jdk_version if args.jdk_version is not None else ((last_cfg.get("jdk_major") if last_cfg else None))
        if jdk_major is None:
            _jdk_val = prompt("JDK major (option: 17 or 21)", default=str(DEFAULTS["jdk_major"]), yes_mode=yes)
            try:
                jdk_major = int(str(_jdk_val)) if _jdk_val is not None else DEFAULTS["jdk_major"]
            except Exception:
                jdk_major = DEFAULTS["jdk_major"]
        install_base = args.install_dir or (last_cfg.get("install_base") if last_cfg else None) \
            or prompt("Install directory (base directory path to store ozone binaries, configs and logs)", default=DEFAULTS["install_base"], yes_mode=yes)
        data_base_raw = args.data_dir or (last_cfg.get("data_base") if last_cfg else None) \
            or prompt("Data directory (base path(s), comma-separated or brace expansion e.g. /data/ozone{1..3})", default=DEFAULTS["data_base"], yes_mode=yes)
        data_base = parse_data_dirs(data_base_raw) if data_base_raw else (data_base_raw or DEFAULTS["data_base"])

        service_user = args.service_user or (last_cfg.get("service_user") if last_cfg else None) \
            or prompt("Service user name ", default=DEFAULTS["service_user"], yes_mode=yes)
        service_group = args.service_group or (last_cfg.get("service_group") if last_cfg else None) \
            or prompt("Service group name", default=DEFAULTS["service_group"], yes_mode=yes)
        dl_url = args.dl_url or (last_cfg.get("dl_url") if last_cfg else None) or DEFAULTS["dl_url"]
        start_after_install = (args.start or (last_cfg.get("start_after_install") if last_cfg else None)
                               or DEFAULTS["start_after_install"])
        use_sudo = (args.use_sudo or (last_cfg.get("use_sudo") if last_cfg else None)
                    or DEFAULTS["use_sudo"])

        # Local specifics (single path to local build)
        local_path = (getattr(args, "local_path", None) or (last_cfg.get("local_path") if last_cfg else None))
        local_shared_path = None
        local_oz_dir = None
        if ozone_version and ozone_version.lower() == "local":
            candidate = None
            if local_path:
                candidate = Path(local_path).expanduser().resolve()
            else:
                legacy_shared = (last_cfg.get("local_shared_path") if last_cfg else None)
                legacy_dir = (last_cfg.get("local_ozone_dirname") if last_cfg else None)
                if legacy_shared and legacy_dir:
                    candidate = Path(legacy_shared).expanduser().resolve() / legacy_dir

            def ask_for_path():
                val = prompt("Path to local Ozone build", default="", yes_mode=yes)
                return Path(val).expanduser().resolve() if val else None

            if candidate is None or not _validate_local_ozone_dir(candidate):
                if yes:
                    logger.error("Error: For -v local, a valid Ozone build path containing bin/ozone is required.")
                    return 2
                while True:
                    maybe = ask_for_path()
                    if maybe and _validate_local_ozone_dir(maybe):
                        candidate = maybe
                        break
                    logger.warning("Invalid path. Expected an Ozone build directory with bin/ozone. Please try again.")

            local_shared_path = str(candidate.parent)
            local_oz_dir = candidate.name
            local_path = str(candidate)

        # Build a human-friendly summary table of inputs before continuing
        if use_master_worker:
            m_count = len(master_hosts)
            w_count = len(worker_hosts)
            summary_host_rows: List[Tuple[str, str]] = [
                ("Masters", f"{m_count} host{'s' if m_count != 1 else ''}"),
                ("Workers", f"{w_count} host{'s' if w_count != 1 else ''}"),
            ]
        else:
            h_count = len(hosts)
            summary_host_rows = [("Hosts", f"{h_count} host{'s' if h_count != 1 else ''}")]

        summary_rows = summary_host_rows + [
            ("Cluster mode", cluster_mode),
            ("Ozone version", str(ozone_version)),
            ("JDK major", str(jdk_major)),
            ("Install directory", str(install_base)),
            ("Data directory", str(data_base)),
            ("SSH user", str(ssh_user)),
            ("SSH auth method", str(auth_method))
        ]
        if keyfile:
            summary_rows.append(("Key file", str(keyfile)))
        summary_rows.extend([
            ("Use sudo", str(bool(use_sudo))),
            ("Service user", str(service_user)),
            ("Service group", str(service_group)),
            ("Start after install", str(bool(start_after_install))),
        ])
        if ozone_version and str(ozone_version).lower() == "local":
            summary_rows.append(("Local Ozone path", str(local_path or "")))
        if not _confirm_summary(summary_rows, yes_mode=yes):
            logger.info("Aborted by user.")
            return 1

        python_interpreter = args.python_interpreter or (last_cfg.get("python_interpreter") if last_cfg else None)
        if python_interpreter:
            logger.info(f"Using Python interpreter: {python_interpreter}")
        else:
            logger.info("Python interpreter will be auto-detected by playbook")

        do_cleanup = False
        if args.clean:
            do_cleanup = True
        else:
            answer = prompt(f"Cleanup existing install at {install_base} (if present)? (y/N)", default="n", yes_mode=yes)
            if str(answer).strip().lower().startswith("y"):
                do_cleanup = True

        extra_vars = {
            "cluster_mode": cluster_mode,
            "install_base": install_base,
            "data_base": data_base,
            "jdk_major": jdk_major,
            "service_user": service_user,
            "service_group": service_group,
            "dl_url": dl_url,
            "ozone_version": ozone_version,
            "start_after_install": bool(start_after_install),
            "use_sudo": bool(use_sudo),
            "do_cleanup": bool(do_cleanup),
            "JAVA_MARKER": DEFAULTS["JAVA_MARKER"],
            "ENV_MARKER": DEFAULTS["ENV_MARKER"],
            "controller_logs_dir": str(LOGS_DIR),
        }
        if python_interpreter:
            extra_vars["ansible_python_interpreter"] = python_interpreter
            extra_vars["ansible_python_interpreter_discovery"] = "explicit"
        if ozone_version and ozone_version.lower() == "local":
            extra_vars.update({
                "local_shared_path": local_shared_path or "",
                "local_ozone_dirname": local_oz_dir or "",
            })
        playbook = PLAYBOOKS_DIR / "cluster.yml"

    # Common: build inventory and run playbook (same for install, stop-only, stop-and-clean)
    inventory_text = build_inventory(
        master_hosts=master_hosts if use_master_worker else None,
        worker_hosts=worker_hosts if use_master_worker else None,
        hosts=hosts if not use_master_worker else None,
        ssh_user=ssh_user, keyfile=keyfile, password=password,
        cluster_mode=cluster_mode,
        python_interpreter=python_interpreter,
    )
    ask_pass = auth_method == "password" and not password

    if stop_only or stop_and_clean:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False) as inv_f:
            inv_f.write(inventory_text or "")
            inv_path = Path(inv_f.name)
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as ev_f:
                ev_f.write(json.dumps(extra_vars, indent=2))
                ev_path = Path(ev_f.name)
            try:
                return run_playbook(playbook, inv_path, ev_path, ask_pass=ask_pass, become=True, verbose=args.verbose)
            finally:
                try:
                    ev_path.unlink()
                except Exception:
                    pass
        finally:
            try:
                inv_path.unlink()
            except Exception:
                pass

    # Full install: persist config and run cluster playbook
    with tempfile.TemporaryDirectory() as tdir:
        inv_path = Path(tdir) / "hosts.ini"
        ev_path = Path(tdir) / "vars.json"
        inv_path.write_text(inventory_text or "", encoding="utf-8")
        ev_path.write_text(json.dumps(extra_vars, indent=2), encoding="utf-8")
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
            persisted_inv = LOGS_DIR / "last_inventory.ini"
            persisted_ev = LOGS_DIR / "last_vars.json"
            persisted_inv.write_text(inventory_text or "", encoding="utf-8")
            persisted_ev.write_text(json.dumps(extra_vars, indent=2), encoding="utf-8")
            inv_path = persisted_inv
            ev_path = persisted_ev
            last_run = {
                "host_file": host_file_path if host_file_path else None,
                "hosts_raw": hosts_raw,
                "cluster_mode": cluster_mode,
                "ozone_version": ozone_version,
                "jdk_major": jdk_major,
                "install_base": install_base,
                "data_base": data_base,
                "auth_method": auth_method,
                "ssh_user": ssh_user,
                "password": password if auth_method == "password" else None,
                "keyfile": str(keyfile) if keyfile else None,
                "service_user": service_user,
                "service_group": service_group,
                "dl_url": dl_url,
                "start_after_install": bool(start_after_install),
                "use_sudo": bool(use_sudo),
                "local_shared_path": local_shared_path or "",
                "local_ozone_dirname": local_oz_dir or "",
                "python_interpreter": python_interpreter or "",
            }
            if use_master_worker:
                last_run["masters_raw"] = masters_raw
                last_run["workers_raw"] = workers_raw
            LAST_RUN_FILE.write_text(json.dumps(last_run, indent=2), encoding="utf-8")
        except Exception:
            pass

        start_at = None
        use_tags = None
        if args.resume:
            if LAST_FAILED_FILE.exists():
                try:
                    # use first line (task name)
                    contents = LAST_FAILED_FILE.read_text(encoding="utf-8").splitlines()
                    start_at = contents[0].strip() if contents else None
                    # derive role tag if present
                    role_line = next((l for l in contents if l.startswith("# role:")), None)
                    if role_line:
                        role_name = role_line.split(":", 1)[1].strip()
                        if role_name:
                            use_tags = [role_name]
                except Exception:
                    start_at = None
        rc = run_playbook(playbook, inv_path, ev_path, ask_pass=ask_pass, become=True, start_at_task=start_at, tags=use_tags, verbose=args.verbose)
        if rc != 0:
            return rc

        # Successful completion: remove last_* persisted files so a fresh run starts clean
        try:
            for f in LOGS_DIR.glob("last_*"):
                try:
                    f.unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    # Best-effort cleanup; ignore failures
                    pass
        except Exception:
            pass

        try:
            example_host = (master_hosts[0]["host"] if use_master_worker and master_hosts else hosts[0]["host"] if hosts else "HOSTNAME")
            logger.info(f"To view process logs: ssh to the node and read {install_base}/current/logs/ozone-{service_user}-<process>-<host>.log "
                        f"(e.g., {install_base}/current/logs/ozone-{service_user}-recon-{example_host}.log)")
        except Exception:
            pass
    logger.info("All done.")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))


