import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, Optional

METRICS_DIR = Path(__file__).parent / 'metrics'
PROM_REMOTE_WRITE = os.environ.get('PROM_REMOTE_WRITE') or 'https://prometheus-prod-65-prod-eu-west-2.grafana.net/api/prom/push'
SCENARIO_METRICS_ENV = {
    'registration': ('REGISTRATION_METRICS_PORT', 9100),
    'login_exploring': ('LOGIN_EXPLORING_METRICS_PORT', 9101),
    'login_logout': ('LOGIN_LOGOUT_METRICS_PORT', 9102),
    'bet': ('BET_METRICS_PORT', 9103),
}


def _metrics_env_for(scenario_name: str) -> Dict[str, str]:
    env: Dict[str, str] = {}
    spec = SCENARIO_METRICS_ENV.get(scenario_name)
    if not spec:
        return env
    env_var, default_port = spec
    port_value = os.environ.get(env_var)
    if not port_value:
        port_value = str(default_port)
        env[env_var] = port_value
    else:
        env[env_var] = port_value
    if 'PROMETHEUS_METRICS_PORT' not in os.environ:
        env['PROMETHEUS_METRICS_PORT'] = port_value
    return env


def run_scenario(scenario_file: str, num_workers: int, duration: int = None, env_overrides: Optional[Dict[str, str]] = None):
    """
    Run a single scenario with specified number of workers
    Returns the subprocess
    """
    duration_seconds = duration * 60 if duration else 86400
    cmd = [
        'molotov',
        scenario_file,
        '-w', str(num_workers),
        '-d', str(duration_seconds),
        '-c',
        '-q',
        '--force-shutdown'
    ]
    print(f"[main] Running command: {' '.join(cmd)}", flush=True)
    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env
    )
    print(f"[main] Subprocess started with PID {proc.pid} for {scenario_file}", flush=True)
    return proc


def read_subprocess_output(proc, scenario_name):
    """Read and print subprocess output"""
    try:
        for line in iter(proc.stdout.readline, ''):
            if line:
                print(f"[{scenario_name}] {line.rstrip()}", flush=True)
    except Exception as e:
        print(f"[main] Error reading {scenario_name} output: {e}", flush=True)


def _read_metrics_file(path: Path, label: str) -> dict:
    try:
        with path.open('r', encoding='utf-8') as handle:
            data = json.load(handle)
            print(f"[main] Loaded metrics for {label} from {path}", flush=True)
            return data
    except FileNotFoundError:
        print(f"[main] Metrics file for {label} not found at {path}", flush=True)
    except json.JSONDecodeError as exc:
        print(f"[main] Failed to parse metrics for {label}: {exc}", flush=True)
    except Exception as exc:
        print(f"[main] Unexpected error reading metrics for {label}: {exc}", flush=True)
    return {}


def print_combined_summary():
    registration_path = METRICS_DIR / 'registration_metrics.json'
    login_exploring_path = METRICS_DIR / 'login_exploring_metrics.json'
    login_logout_path = METRICS_DIR / 'login_logout_metrics.json'

    registration_metrics = _read_metrics_file(registration_path, 'registration')
    login_exploring_metrics = _read_metrics_file(login_exploring_path, 'login_exploring')
    login_logout_metrics = _read_metrics_file(login_logout_path, 'login_logout')

    if not any([registration_metrics, login_exploring_metrics, login_logout_metrics]):
        print("[main] Combined summary unavailable (no metrics files)", flush=True)
        return

    total_registrations = registration_metrics.get('total_registrations', 0)
    total_successful_registrations = registration_metrics.get('successful_registrations', 0)
    total_failed_registrations = registration_metrics.get('failed_registrations', 0)

    total_login_attempts = login_exploring_metrics.get('total_logins', 0) + login_logout_metrics.get('total_logins', 0)
    total_login_successes = login_exploring_metrics.get('successful_logins', 0) + login_logout_metrics.get('successful_logins', 0)
    total_login_failures = login_exploring_metrics.get('failed_logins', 0) + login_logout_metrics.get('failed_logins', 0)

    total_logout_attempts = login_logout_metrics.get('total_logouts', 0)
    total_logout_successes = login_logout_metrics.get('successful_logouts', 0)
    total_logout_failures = login_logout_metrics.get('failed_logouts', 0)

    total_pages_explored = login_exploring_metrics.get('total_pages_explored', 0)

    total_setup_errors = (
        registration_metrics.get('setup_errors', 0)
        + login_exploring_metrics.get('setup_errors', 0)
        + login_logout_metrics.get('setup_errors', 0)
    )

    print("\n" + "=" * 70)
    print("[main] CONSOLIDATED SCENARIO SUMMARY")
    print("=" * 70)
    print(f"Registrations: total={total_registrations}, success={total_successful_registrations}, "
          f"failures={total_failed_registrations}")
    print(f"Logins (both scenarios): total={total_login_attempts}, success={total_login_successes}, "
          f"failures={total_login_failures}")
    print(f"Logouts: total={total_logout_attempts}, success={total_logout_successes}, "
          f"failures={total_logout_failures}")
    print(f"Pages explored: {total_pages_explored}")
    print(f"Setup errors across all scenarios: {total_setup_errors}")
    print("=" * 70 + "\n")


def run_single_scenario(scenario_name: str, num_workers: int, duration: int = None):
    """Run a single scenario"""
    scenario_map = {
        'registration': ('scenarios/registration_scenario.py', 'REGISTRATION_WORKERS'),
        'bet': ('scenarios/bet_scenario.py', 'BET_WORKERS'),
        'login_exploring': ('scenarios/login_and_exploring_scenario.py', 'LOGIN_EXPLORING_WORKERS'),
        'login_logout': ('scenarios/login_logout_scenario.py', 'LOGIN_LOGOUT_WORKERS'),
    }
    
    if scenario_name not in scenario_map:
        print(f"[main] ERROR: Unknown scenario '{scenario_name}'", flush=True)
        sys.exit(1)
    
    scenario_file, _ = scenario_map[scenario_name]
    print(f"[main] Starting single scenario: {scenario_name}", flush=True)
    metrics_env = _metrics_env_for(scenario_name)
    
    proc = run_scenario(scenario_file, num_workers, duration, env_overrides=metrics_env)
    output_thread = threading.Thread(target=read_subprocess_output, args=(proc, scenario_name), daemon=False)
    output_thread.start()
    
    def handle_interrupt(sig, frame):
        print(f"[main] Received interrupt signal", flush=True)
        proc.terminate()
    
    old_handler = signal.signal(signal.SIGINT, handle_interrupt)
    
    try:
        return_code = proc.wait()
        signal.signal(signal.SIGINT, old_handler)
        print(f"[main] Scenario {scenario_name} completed", flush=True)
        metrics_port = metrics_env.get('PROMETHEUS_METRICS_PORT') or os.environ.get('PROMETHEUS_METRICS_PORT', '8000')
        print(f"[main] Metrics pushed to {PROM_REMOTE_WRITE} (local scrape on :{metrics_port}/metrics)", flush=True)
        return return_code
    except KeyboardInterrupt:
        signal.signal(signal.SIGINT, old_handler)
        print(f"[main] Terminating subprocess...", flush=True)
        proc.terminate()
        return 1


def run_all_scenarios(registration_workers: int, login_exploring_workers: int, login_logout_workers: int, duration: int = None):
    """Run all three scenarios in parallel"""
    duration_seconds = duration * 60 if duration else 86400
    
    print("[main] Starting all scenarios in parallel...", flush=True)
    
    registration_env = _metrics_env_for('registration')
    login_exploring_env = _metrics_env_for('login_exploring')
    login_logout_env = _metrics_env_for('login_logout')
    registration_proc = run_scenario('scenarios/registration_scenario.py', registration_workers, duration, env_overrides=registration_env)
    login_exploring_proc = run_scenario('scenarios/login_and_exploring_scenario.py', login_exploring_workers, duration, env_overrides=login_exploring_env)
    login_logout_proc = run_scenario('scenarios/login_logout_scenario.py', login_logout_workers, duration, env_overrides=login_logout_env)
    
    output_threads = [
        threading.Thread(target=read_subprocess_output, args=(registration_proc, 'registration'), daemon=False),
        threading.Thread(target=read_subprocess_output, args=(login_exploring_proc, 'login_exploring'), daemon=False),
        threading.Thread(target=read_subprocess_output, args=(login_logout_proc, 'login_logout'), daemon=False),
    ]
    
    for thread in output_threads:
        thread.start()
    
    def handle_interrupt(sig, frame):
        print(f"[main] Received interrupt signal", flush=True)
        registration_proc.terminate()
        login_exploring_proc.terminate()
        login_logout_proc.terminate()
    
    old_handler = signal.signal(signal.SIGINT, handle_interrupt)
    
    try:
        registration_proc.wait()
        login_exploring_proc.wait()
        login_logout_proc.wait()
        
        signal.signal(signal.SIGINT, old_handler)
        print(f"[main] All scenarios completed", flush=True)
        for scenario_label, env_values in (
                ('registration', registration_env),
                ('login_exploring', login_exploring_env),
                ('login_logout', login_logout_env),
        ):
            port_value = env_values.get('PROMETHEUS_METRICS_PORT') or os.environ.get('PROMETHEUS_METRICS_PORT', '9103')
            print(f"[main] {scenario_label} metrics scraped at http://host.docker.internal:{port_value}/metrics (pushed to {PROM_REMOTE_WRITE})", flush=True)
        print_combined_summary()

        exit_code = 0
        for scenario_name, proc in (
                ("registration", registration_proc),
                ("login_exploring", login_exploring_proc),
                ("login_logout", login_logout_proc),
        ):
            proc_rc = proc.returncode or 0
            if proc_rc != 0:
                exit_code = 1
                print(f"[main] Scenario {scenario_name} exited with code {proc_rc}", flush=True)

        return exit_code
    except KeyboardInterrupt:
        signal.signal(signal.SIGINT, old_handler)
        print(f"[main] Terminating all subprocesses...", flush=True)
        registration_proc.terminate()
        login_exploring_proc.terminate()
        login_logout_proc.terminate()
        return 1


if __name__ == '__main__':
    base_url = os.environ.get('BASE_URL', None)
    if base_url:
        os.environ['BASE_URL'] = base_url
    
    duration = int(os.environ.get('DURATION', 5))
    scenario = os.environ.get('SCENARIO', None)
    
    bet_workers = int(os.environ.get('BET_WORKERS', 5))
    registration_workers = int(os.environ.get('REGISTRATION_WORKERS', 10))
    login_exploring_workers = int(os.environ.get('LOGIN_EXPLORING_WORKERS', 20))
    login_logout_workers = int(os.environ.get('LOGIN_LOGOUT_WORKERS', 30))
    
    print(f"[main] Configuration:", flush=True)
    print(f"[main]   Duration: {duration} minutes", flush=True)
    if scenario:
        print(f"[main]   Scenario: {scenario}", flush=True)
        if scenario == 'registration':
            print(f"[main]   Registration workers: {registration_workers}", flush=True)
        elif scenario == 'bet':
            print(f"[main]   Bet workers: {bet_workers}", flush=True)
        elif scenario == 'login_exploring':
            print(f"[main]   Login & Exploring workers: {login_exploring_workers}", flush=True)
        elif scenario == 'login_logout':
            print(f"[main]   Login & Logout workers: {login_logout_workers}", flush=True)
    else:
        print(f"[main]   Bet workers: {bet_workers}", flush=True)
        print(f"[main]   Registration workers: {registration_workers}", flush=True)
        print(f"[main]   Login & Exploring workers: {login_exploring_workers}", flush=True)
        print(f"[main]   Login & Logout workers: {login_logout_workers}", flush=True)
    if base_url:
        print(f"[main]   Base URL: {base_url}", flush=True)

    print("[main] Starting load test (metrics server will be started in molotov subprocesses)...", flush=True)
    
    if scenario:
        scenario_workers_map = {
            'registration': registration_workers,
            'bet': bet_workers,
            'login_exploring': login_exploring_workers,
            'login_logout': login_logout_workers,
        }
        num_workers = scenario_workers_map.get(scenario)
        if num_workers is None:
            print(f"[main] ERROR: Unknown scenario '{scenario}'", flush=True)
            sys.exit(1)
        exit_code = run_single_scenario(scenario, num_workers, duration)
    else:
        exit_code = run_all_scenarios(registration_workers, login_exploring_workers, login_logout_workers, duration)

    sys.exit(exit_code)
