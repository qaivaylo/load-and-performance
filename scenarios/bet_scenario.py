import asyncio
import aiohttp
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from molotov import scenario, global_setup, global_teardown, setup_session
from prometheus_client import Counter, Gauge, Histogram, start_http_server, REGISTRY, CollectorRegistry, generate_latest

from scenarios.utils.metrics import resolve_metrics_port
from src.app import login, deposit, get_game_id, generate_token, make_bet_request, get_total_players

YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

global_stats = {
    'total_workers': 0,
    'total_bets': 0,
    'successful_bets': 0,
    'failed_bets': 0,
    'setup_errors': 0,
    'worker_games': defaultdict(str),
    'game_stats': defaultdict(lambda: {'bets': 0, 'successes': 0, 'failures': 0})
}

prometheus_metrics = {
    'total_workers': Gauge('fortuna_total_workers', 'Total number of workers'),
    'total_bets': Counter('fortuna_total_bets', 'Total bets made'),
    'successful_bets': Counter('fortuna_successful_bets', 'Successful bets'),
    'failed_bets': Counter('fortuna_failed_bets', 'Failed bets'),
    'setup_errors': Counter('fortuna_setup_errors', 'Worker setup errors'),
    'bets_per_game': Counter('fortuna_bets_per_game', 'Bets by game', ['game']),
    'success_per_game': Counter('fortuna_success_per_game', 'Successful bets by game', ['game']),
    'failure_per_game': Counter('fortuna_failure_per_game', 'Failed bets by game', ['game']),
    'bet_latency': Histogram('fortuna_bet_latency_seconds', 'Bet response latency in seconds', ['game', 'status'],
                             buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0))
}

ramp_lock = asyncio.Lock()
ramp_state = {'next_worker_index': 0}


@global_setup()
def setup_molotov(args):
    global_stats['start_time'] = time.time()
    metrics_port = resolve_metrics_port('BET_METRICS_PORT')
    start_http_server(metrics_port)

    prometheus_metrics['total_bets']._value.get()
    prometheus_metrics['successful_bets']._value.get()
    prometheus_metrics['failed_bets']._value.get()
    prometheus_metrics['setup_errors']._value.get()

    print(f"[global_setup] Metrics server started on 0.0.0.0:{metrics_port}", flush=True)


@global_teardown()
def teardown_molotov():
    global_stats['end_time'] = time.time()
    elapsed = global_stats['end_time'] - global_stats['start_time']
    print(f"[global_teardown] {YELLOW}Total test time: {elapsed:.2f}s{RESET}", flush=True)
    print(f"Total bets: {global_stats['total_bets']}, {GREEN}Success: {global_stats['successful_bets']}{RESET}, "
          f"{RED}Failures: {global_stats['failed_bets']}{RESET}", flush=True)

    metrics_dir = Path(__file__).parent.parent / 'metrics'
    metrics_dir.mkdir(parents=True, exist_ok=True)

    try:
        metrics_text_file = metrics_dir / 'bet_metrics.txt'
        with open(metrics_text_file, 'w') as f:
            f.write(generate_latest(REGISTRY).decode('utf-8'))
        print(f"[global_teardown] Metrics exported to {metrics_text_file}", flush=True)
    except Exception as e:
        print(f"[global_teardown] ERROR exporting metrics: {e}", flush=True)

    metrics_json = {
        'total_workers': global_stats['total_workers'],
        'total_bets': global_stats['total_bets'],
        'successful_bets': global_stats['successful_bets'],
        'failed_bets': global_stats['failed_bets'],
        'setup_errors': global_stats['setup_errors'],
    }

    try:
        metrics_json_file = metrics_dir / 'bet_metrics.json'
        with open(metrics_json_file, 'w') as f:
            json.dump(metrics_json, f, indent=2)
        print(f"[global_teardown] JSON metrics exported to {metrics_json_file}", flush=True)
    except Exception as e:
        print(f"[global_teardown] ERROR exporting JSON metrics: {e}", flush=True)

    if global_stats['failed_bets'] > 0:
        print(f"[global_teardown] {RED}Detected {global_stats['failed_bets']} failed bets. Exiting with status 1{RESET}",
            flush=True)
        sys.exit(1)


@setup_session()
async def setup_worker(worker_id, session):
    cookie = await login()
    if not cookie:
        print(f"[worker {worker_id}] Login failed", flush=True)
        global_stats['setup_errors'] += 1
        prometheus_metrics['setup_errors'].inc()
        return

    session.cookie = cookie

    async with aiohttp.ClientSession() as aio_session:
        worker_index = None
        total_players = None
        wait_start = time.time()

        while True:
            total_players = await get_total_players(cookie, aio_session)
            print(f"[worker {worker_id}] fetched total_players={total_players}", flush=True)
            if total_players is None or total_players == "":
                print(f"[worker {worker_id}] total_players unavailable; aborting setup", flush=True)
                global_stats['setup_errors'] += 1
                prometheus_metrics['setup_errors'].inc()
                sys.exit(1)

            async with ramp_lock:
                required_workers = global_stats['total_workers'] + 1
                print(f"[worker {worker_id}] total_players={total_players}, required_workers={required_workers}",
                      flush=True)
                if total_players >= required_workers:
                    worker_index = ramp_state['next_worker_index']
                    ramp_state['next_worker_index'] += 1
                    global_stats['total_workers'] += 1
                    prometheus_metrics['total_workers'].set(global_stats['total_workers'])
                    print(f"[worker {worker_id}] capacity ok, starting worker_index={worker_index}", flush=True)
                    break

            print(f"[worker {worker_id}] Waiting for total_players >= workers (have {total_players}, "
                  f"workers {global_stats['total_workers']})", flush=True)
            await asyncio.sleep(2)
            if time.time() - wait_start >= 1800:
                print(f"[worker {worker_id}] Timeout waiting for capacity (total_players={total_players}, "
                      f"workers {global_stats['total_workers']})", flush=True)
                sys.exit(1)

        delay = worker_index * 5 if worker_index is not None else 0
        if delay > 0:
            print(f"[worker {worker_id}] Ramp-up delay: waiting {delay}s", flush=True)
            await asyncio.sleep(delay)

        print(f"[worker {worker_id}] Starting setup", flush=True)

        game_id = await get_game_id(cookie, aio_session)
        if not game_id:
            print(f"[worker {worker_id}] Failed to get game_id", flush=True)
            global_stats['setup_errors'] += 1
            prometheus_metrics['setup_errors'].inc()
            return

        session.game_id = game_id
        global_stats['worker_games'][worker_id] = str(game_id)
        print(f"[worker {worker_id}] Assigned game_id: {game_id}", flush=True)

        used_players = set()
        max_attempts = total_players

        for attempt in range(max_attempts):
            user = worker_id + random.randint(1, total_players)
            if user in used_players:
                print(f"[worker {worker_id}] User {user} already used in this session, retrying", flush=True)
                continue
            used_players.add(user)
            try:
                if await deposit(user, cookie, aio_session):
                    session.user_id = user
                    print(f"[worker {worker_id}] Deposit OK for user {user}", flush=True)
                    return
            except Exception as e:
                print(f"[worker {worker_id}] Deposit attempt {attempt + 1} failed: {e}", flush=True)

        print(f"[worker {worker_id}] Failed to find valid user after {max_attempts} attempts", flush=True)
        global_stats['setup_errors'] += 1
        prometheus_metrics['setup_errors'].inc()


@scenario()
async def betting_scenario(session):
    if not session.user_id or not session.game_id or not session.cookie:
        raise RuntimeError("Invalid session data")

    token = generate_token()
    game_id = str(session.game_id)
    start_time = time.time()

    async with aiohttp.ClientSession() as aio_session:
        response, ok = await make_bet_request(session.user_id, token, session.cookie, session.game_id, aio_session)
        latency = time.time() - start_time

        global_stats['total_bets'] += 1
        global_stats['game_stats'][game_id]['bets'] += 1
        prometheus_metrics['total_bets'].inc()
        prometheus_metrics['bets_per_game'].labels(game=game_id).inc()
        prometheus_metrics['bet_latency'].labels(game=game_id, status='success' if ok else 'failure').observe(latency)

        if ok:
            global_stats['successful_bets'] += 1
            global_stats['game_stats'][game_id]['successes'] += 1
            prometheus_metrics['successful_bets'].inc()
            prometheus_metrics['success_per_game'].labels(game=game_id).inc()
        else:
            global_stats['failed_bets'] += 1
            global_stats['game_stats'][game_id]['failures'] += 1
            prometheus_metrics['failed_bets'].inc()
            prometheus_metrics['failure_per_game'].labels(game=game_id).inc()
