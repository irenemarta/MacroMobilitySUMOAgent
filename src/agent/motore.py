"""
dtSumo logic for the AI agent: check, start and what-if scenarios.

- check_response: direct import and call to run_scenario(check_only=True);
- start: run.py as separated process for GUI and quick simulations, log can be found in the .exit file;
- comparison: compare.py.

Each scenario is called as <id>_r<round> in order to avoid overwriting any file in quick simulaiton mode.
Files of scenario and log in output/<DTSUMO_RUN>/agent/.
"""

import json, os, sys, re
import subprocess, threading
import yaml

import dtsumo.inputs.config as cfg
from dtsumo.whatif.run import run_scenario
from dtsumo.whatif.scenario import ScenarioError

VARIANT = "MA_with_TLS"  # default run.py
FOLDER = cfg.OUTPUT_BASE / "agent"
MODULO_CONFRONTO = "dtsumo.analysis.compare"  # to update if compare.py runs
ATTESA_CONFRONTO = 600  # seconds
ROUND_NEL_LOG = re.compile(
    r"/DAY: ROUND (\d+)"
)  # printed by marouter_cycle for each round
FINALE_NEL_LOG = (
    "Final meso simulation"  # printed by run.py before the end of the simulation
)


class ErroreDtsumo(Exception):

    def __init__(self, messaggio, log=""):
        super().__init__(messaggio)
        self.log = log


def name(ident, n_round):
    return f"{ident}_r{n_round}"


def _file_scenario(nome_dtsumo):
    return FOLDER / "scenarios" / f"{nome_dtsumo}.json"


def _log(nome_dtsumo):
    return FOLDER / "logs" / f"{nome_dtsumo}.log"


def _exit(nome_dtsumo):
    return FOLDER / "logs" / f"{nome_dtsumo}.exit"


def check_response(scenario):
    """{"ok": True, "interventions": [...], ...} o {"ok": False, "error": ...}, come run.py --check --json."""
    try:
        return {"ok": True, **run_scenario(scenario, VARIANT, check_only=True)}
    except ScenarioError as e:
        return {"ok": False, "error": str(e)}

### INTERFACE
def send_scenario(scenario, n_round):
    """Scenario name: <id>_r<round>."""
    name_dtsumo = name(scenario["id"], n_round)
    path_to_scenario = _file_scenario(name_dtsumo)
    path_to_scenario.parent.mkdir(parents=True, exist_ok=True)
    path_to_scenario.write_text(
        json.dumps({**scenario, "id": name_dtsumo}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path_to_scenario


def run_in_background(ident, n_round):
    """Runs run.py in background; output in <name>.log, exit code in <name>.exit when it ends."""
    nome_dtsumo = name(ident, n_round)
    log, exit_file = _log(nome_dtsumo), _exit(nome_dtsumo)
    log.parent.mkdir(parents=True, exist_ok=True)
    exit_file.unlink(missing_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "dtsumo.whatif.run",
        str(_file_scenario(nome_dtsumo)),
        "--variant",
        VARIANT,
        "--rounds",
        str(int(n_round)),
    ]
    with open(log, "w", encoding="utf-8") as f:
        proc = subprocess.Popen(
            cmd,
            cwd=cfg.BASE_PATH,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdin=subprocess.DEVNULL,
            stdout=f,
            stderr=subprocess.STDOUT,
        )
    # the child keeps its own copy of the log file, so closing it here is fine
    # to make it possible running the process in background
    threading.Thread(
        target=lambda: exit_file.write_text(str(proc.wait())), daemon=True
    ).start()


def sim_state(ident, n_round):
    """{"exit": code / None if running, "round": last started round / None,
    "final": True if the final simulation started, "log"}."""
    name_dtsumo = name(ident, n_round)
    log, exit = _log(name_dtsumo), _exit(name_dtsumo)
    log_content = (
        log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    )
    code = exit.read_text().strip() if exit.exists() else ""
    round_ok = ROUND_NEL_LOG.findall(log_content)
    return {
        "exit": int(code) if code else None,
        "round": int(round_ok[-1]) if round_ok else None,
        "final": FINALE_NEL_LOG in log_content,
        "log": "\n".join(log_content.splitlines()[-40:]),
    }


### SIMULATED SCENARIOS
def simulated_scenarios():
    """(id, round, seed) scenarios run by agent and recorded in scenarios.yaml.
    IDs run.py: <variant>_<id>_r<round>_seed<seed> vs ID from cmd (withouth _r<round>) are not listed."""
    registry = cfg.SCENARIOS_YAML
    if not registry.exists():
        return []
    recorded_scenarios = (
        yaml.safe_load(registry.read_text(encoding="utf-8")) or {}
    ).get("scenarios", [])
    simulated = []
    for s in recorded_scenarios:
        match = re.fullmatch(rf"{VARIANT}_(.+)_r(\d+)_seed(\d+)", s["id"])
        if match:
            simulated.append((match.group(1), int(match.group(2)), int(match.group(3))))
    return simulated


def baseline_ok(baseline_id, n_round):
    return any(i == baseline_id and r == n_round for i, r, _ in simulated_scenarios())


def comparison_bs_scen(ident, baseline_id, n_round):
    """JSON of compare.py: scenario vs baseline (see simula.confronta)."""
    try:
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                MODULO_CONFRONTO,
                name(ident, n_round),
                "--baseline",
                name(baseline_id, n_round),
                "--json",
            ],
            cwd=cfg.BASE_PATH,
            capture_output=True,
            text=True,
            timeout=ATTESA_CONFRONTO,
        )
    except subprocess.TimeoutExpired:
        raise ErroreDtsumo(f"il confronto non ha risposto entro {ATTESA_CONFRONTO} s")
    if r.returncode != 0:
        raise ErroreDtsumo(
            f"il confronto si è fermato con codice {r.returncode}",
            (r.stderr or r.stdout)[-3000:],
        )
    righe = [riga for riga in r.stdout.splitlines() if riga.strip()]
    try:
        return json.loads(righe[-1])
    except (IndexError, json.JSONDecodeError):
        raise ErroreDtsumo("risposta del confronto non in JSON", r.stdout[-3000:])
