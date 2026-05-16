import csv
import threading
import time
from pathlib import Path
from typing import Dict, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
USERS_DATA_DIR = ROOT_DIR / 'data' / 'users'
LOGIN_EXPL_CSV_FILENAME = "tmp_login_expl.csv"
LOGIN_LOGOUT_CSV_FILENAME = "tmp_login_logout.csv"
DEFAULT_CSV_FILENAME = LOGIN_EXPL_CSV_FILENAME
CSV_FIELDNAMES = ['email', 'password', 'status']
USED_STATUS = 'used'

users_lock = threading.Lock()
file_locks: Dict[Path, threading.Lock] = {}
worker_counters: Dict[Path, int] = {}


def _ensure_users_dir():
    USERS_DATA_DIR.mkdir(parents=True, exist_ok=True)


def _csv_file_path(csv_filename: str) -> Path:
    return USERS_DATA_DIR / csv_filename


def _get_file_lock(file_path: Path) -> threading.Lock:
    with users_lock:
        lock = file_locks.get(file_path)
        if lock is None:
            lock = threading.Lock()
            file_locks[file_path] = lock
        return lock


def _next_worker_metadata(file_path: Path) -> int:
    with users_lock:
        counter = worker_counters.get(file_path, 0) + 1
        worker_counters[file_path] = counter
    return counter


def _load_users(file_path: Path) -> list[Dict[str, str]]:
    if not file_path.exists():
        return []
    with file_path.open('r', encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            normalized = {field: (row.get(field) or '') for field in CSV_FIELDNAMES}
            rows.append(normalized)
        return rows


def _write_users(file_path: Path, rows: list[Dict[str, str]]):
    _ensure_users_dir()
    with file_path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, '') for field in CSV_FIELDNAMES})


def _available_user_count(file_path: Path, rows: Optional[list[Dict[str, str]]] = None) -> int:
    target_rows = rows if rows is not None else _load_users(file_path)
    return sum(1 for row in target_rows if (row.get('status') or '').strip().lower() != USED_STATUS)


def wait_for_user_batch(
        min_users: int = 1,
        timeout_seconds: int = 300,
        poll_interval: int = 2,
        csv_filename: str = DEFAULT_CSV_FILENAME,
) -> bool:
    file_path = _csv_file_path(csv_filename)
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        if not file_path.exists():
            print(f"[wait_for_user_batch] [*] Waiting for {file_path.name} to be created")
        else:
            try:
                available = _available_user_count(file_path)
                if available >= min_users:
                    print(
                        f"[wait_for_user_batch] [+] Found {available} available users in {file_path.name}, continuing")
                    return True
                print(
                    f"[wait_for_user_batch] [*] Waiting for registrations in {file_path.name}: {available}/{min_users}")
            except Exception as e:
                print(f"[wait_for_user_batch] [-] Error reading {file_path.name}: {e}")
        time.sleep(poll_interval)
    print(
        f"[wait_for_user_batch] [-] Timeout: did not reach {min_users} available users in {file_path.name} within {timeout_seconds}s")
    return False


def _next_available_index(rows: list[Dict[str, str]]) -> Optional[int]:
    for idx, row in enumerate(rows):
        status = (row.get('status') or '').strip().lower()
        if status != USED_STATUS:
            return idx
    return None


def _get_next_user(csv_filename: str, scenario_label: str) -> Optional[Dict[str, str]]:
    file_path = _csv_file_path(csv_filename)
    worker_number = _next_worker_metadata(file_path)
    prefix = f"[get_next_user:{scenario_label}] [WORKER #{worker_number}]"

    file_lock = _get_file_lock(file_path)

    while True:
        with file_lock:
            try:
                rows = _load_users(file_path)
                available_index = _next_available_index(rows) if rows else None

                if available_index is not None:
                    user_row = rows[available_index]
                    rows[available_index]['status'] = USED_STATUS
                    _write_users(file_path, rows)
                    remaining = _available_user_count(file_path, rows)
                    print(
                        f"{prefix} Took user: {user_row.get('email', '')} "
                        f"({remaining} available left)"
                    )
                    return {
                        'email': user_row.get('email', ''),
                        'password': user_row.get('password', ''),
                    }
            except Exception as e:
                print(f"{prefix} ERROR: {e}")

        print(f"{prefix} Waiting for free user in {file_path.name}...")
        time.sleep(60)


def get_login_exploring_user() -> Optional[Dict[str, str]]:
    return _get_next_user(LOGIN_EXPL_CSV_FILENAME, 'login_exploring')


def get_login_logout_user() -> Optional[Dict[str, str]]:
    return _get_next_user(LOGIN_LOGOUT_CSV_FILENAME, 'login_logout')


def get_next_user() -> Optional[Dict[str, str]]:
    return get_login_exploring_user()
