import asyncio
import csv
import json
import random
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urljoin

import aiohttp
from bs4 import BeautifulSoup
from molotov import scenario, global_setup, global_teardown, setup_session
from prometheus_client import Counter, Gauge, Histogram, start_http_server, REGISTRY, generate_latest

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import config
from scenarios.utils.metrics import resolve_metrics_port
from scenarios.utils.user_manager import get_login_exploring_user

YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

global_stats = {
    'total_workers': 0,
    'total_logins': 0,
    'successful_logins': 0,
    'failed_logins': 0,
    'total_pages_explored': 0,
    'setup_errors': 0,
}

prometheus_metrics = {
    'total_workers': Gauge('fortuna_login_exploring_workers', 'Total login exploring workers'),
    'total_logins': Counter('fortuna_total_logins', 'Total login attempts'),
    'successful_logins': Counter('fortuna_successful_logins', 'Successful logins'),
    'failed_logins': Counter('fortuna_failed_logins', 'Failed logins'),
    'pages_explored': Counter('fortuna_pages_explored', 'Total pages explored'),
    'setup_errors': Counter('fortuna_login_exploring_setup_errors', 'Login exploring setup errors', ['worker']),
    'login_latency': Histogram('fortuna_login_latency_seconds', 'Login latency', ['worker', 'status'],
                               buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0)),
    'login_errors': Counter('fortuna_login_errors_total', 'Login errors by worker', ['worker', 'error_type']),
}

ramp_lock = asyncio.Lock()
ramp_state = {'next_worker_index': 0}


class LoginHelper:
    """Helper class to manage login form parsing and user credential handling"""

    IGNORED_FIELDS = {
        'csrf',
        '_csrf_token',
        'csrf_token',
        'token',
        'captcha',
        'recaptcha_token',
        'submit',
        'save',
        'remember'
    }

    @staticmethod
    def _log_prefix(component: str, worker_label: str = "") -> str:
        return f"[{component}]{worker_label or ''}"

    @staticmethod
    def load_random_user() -> Optional[Dict[str, str]]:
        """
        Load random user credentials from one of the CSV files in data/users/
        Returns dict with 'email' and 'password' or None if no users found
        """
        data_dir = Path(__file__).parent.parent / 'data' / 'users'
        csv_files = list(data_dir.glob('*.csv'))

        if not csv_files:
            print(f"[LoginHelper] {RED}ERROR: No CSV files found in data/users/{RESET}")
            return None

        selected_file = random.choice(csv_files)
        print(f"[LoginHelper] Loading users from {selected_file.name}")

        try:
            with open(selected_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                users = list(reader)

            if not users:
                print(f"[LoginHelper] {RED}ERROR: No users found in {selected_file.name}{RESET}")
                return None

            user = random.choice(users)
            print(f"[LoginHelper] Selected user: {user.get('email', 'unknown')}")
            return user

        except Exception as e:
            print(f"[LoginHelper] {RED}ERROR loading users: {e}{RESET}")
            return None

    @staticmethod
    async def fetch_login_form(session: aiohttp.ClientSession, login_url: str) -> Optional[Dict[str, Any]]:
        """
        Fetch login form, extract fields, and capture submit target metadata
        Returns dict with keys: fields, action, method
        """
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1'
            }
            async with session.get(login_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                response.raise_for_status()
                html_content = await response.text()

            soup = BeautifulSoup(html_content, "html.parser")
            form_fields = {}

            login_form = soup.find("form", {"id": "login-form"}) or soup.find("form", {"id": "form-login"})
            if not login_form:
                for candidate in soup.find_all("form"):
                    id_attr = candidate.get("id", "")
                    name_attr = candidate.get("name", "")
                    class_attr = " ".join(candidate.get("class", []))
                    descriptor = " ".join([id_attr, name_attr, class_attr]).lower()
                    has_password = candidate.find("input", {"type": "password"}) is not None
                    if has_password or any(keyword in descriptor for keyword in ["login", "signin"]):
                        login_form = candidate
                        break

            if not login_form:
                print(f"[LoginHelper] {RED}ERROR: Could not locate a login form{RESET}")
                return None

            action_attr = (login_form.get("action") or "").strip()
            method = (login_form.get("method") or "POST").upper()
            submit_url = urljoin(login_url, action_attr) if action_attr else login_url

            print("[LoginHelper] Found login form, parsing fields...")

            for input_field in login_form.find_all("input"):
                name = input_field.get("name")
                if not name:
                    continue

                field_type = input_field.get("type", "text")
                value = input_field.get("value", "")
                required = input_field.get("required") is not None

                if name in LoginHelper.IGNORED_FIELDS:
                    if name in ['_csrf_token', 'csrf_token', 'csrf', 'token']:
                        form_fields[name] = {
                            "type": "hidden",
                            "required": False,
                            "value": value,
                            "is_csrf": True
                        }
                        print(f"[LoginHelper] Found CSRF token: {name} = {value[:40]}...")
                    continue

                form_fields[name] = {
                    "type": field_type,
                    "required": required,
                    "value": value
                }
                print(f"[LoginHelper] Found input field: {name} (type: {field_type}, required: {required})")

            for textarea in login_form.find_all("textarea"):
                name = textarea.get("name")
                if name and name not in LoginHelper.IGNORED_FIELDS:
                    required = textarea.get("required") is not None
                    form_fields[name] = {
                        "type": "textarea",
                        "required": required
                    }
                    print(f"[LoginHelper] Found textarea field: {name} (required: {required})")

            for select in login_form.find_all("select"):
                name = select.get("name")
                if name and name not in LoginHelper.IGNORED_FIELDS:
                    required = select.get("required") is not None
                    options = [opt.get("value", opt.text) for opt in select.find_all("option")]
                    form_fields[name] = {
                        "type": "select",
                        "required": required,
                        "options": options
                    }
                    print(f"[LoginHelper] Found select field: {name} (required: {required})")

            print(f"\n[LoginHelper] Found {len(form_fields)} form fields: {list(form_fields.keys())}\n")
            return {
                "fields": form_fields,
                "action": submit_url,
                "method": method
            }

        except Exception as e:
            print(f"[LoginHelper] {RED}ERROR fetching login form: {e}{RESET}")
            return None

    @staticmethod
    def build_login_payload(
            user: Dict[str, str],
            form_fields: Dict[str, dict],
            worker_label: str = ""
    ) -> Dict[str, str]:
        """
        Dynamically build login payload based on form fields and user credentials
        Maps email/username and password fields based on field names
        Includes CSRF token if present in form
        """
        payload = {}

        email = user.get('email', '')
        password = user.get('password', '')
        prefix = LoginHelper._log_prefix('build_login_payload', worker_label)

        print(f"\n{prefix} ====== BUILDING PAYLOAD ======")
        print(f"{prefix} Available form fields: {list(form_fields.keys())}")
        print(f"{prefix} User email: {email}")

        for field_name, field_info in form_fields.items():
            if field_info.get('is_csrf'):
                csrf_value = field_info.get('value', '')
                payload[field_name] = csrf_value
                print(f"{prefix} CSRF Token {field_name}: {csrf_value[:40]}...")

            elif any(keyword in field_name.lower() for keyword in ['_username', 'username', '_email', 'email']):
                if not any(kw in field_name.lower() for kw in ['confirm', 'promo', 'marketing']):
                    payload[field_name] = email
                    print(f"{prefix} Email/Username field {field_name}: {email}")

            elif any(keyword in field_name.lower() for keyword in ['_password', 'password']):
                if not any(kw in field_name.lower() for kw in ['confirm', 'old']):
                    payload[field_name] = password
                    print(f"{prefix} Password field {field_name}: {'*' * len(password)}")

        print(f"\n{prefix} ====== PAYLOAD SUMMARY ======")
        print(f"{prefix} Total fields in payload: {len(payload)}")
        print(f"{prefix} Payload fields: {list(payload.keys())}\n")

        return payload

    @staticmethod
    async def submit_login(
            session: aiohttp.ClientSession,
            login_submit_url: str,
            payload: Dict[str, str],
            email: str,
            worker_label: str = ""
    ) -> Dict[str, any]:
        """
        Submit login via POST API call with payload
        Returns dict with submission result (success, status, response, message)
        """
        result = {
            'success': False,
            'status': None,
            'response': None,
            'payload': payload,
            'error': None,
            'message': None
        }

        try:
            form_data = urlencode(payload)
            prefix = LoginHelper._log_prefix('submit_login', worker_label)
            print(f"\n{prefix} ====== SUBMITTING LOGIN ======")
            print(f"{prefix} Submitting login for {email}")
            print(f"{prefix} URL: {login_submit_url}")
            print(f"{prefix} Form data length: {len(form_data)} bytes")
            print(f"\n{prefix} ====== PAYLOAD DETAILS ======")
            for key, value in payload.items():
                value_preview = value if len(str(value)) <= 50 else f"{str(value)[:50]}..."
                print(f"{prefix}   {key}: {value_preview}")
            print(f"{prefix} ====== ENCODED FORM DATA ======")
            print(f"{prefix} {form_data}\n")

            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1'
            }

            async with session.post(
                    login_submit_url,
                    data=form_data,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                response_text = await response.text()
                result['status'] = response.status
                result['response'] = response_text

                success, message = LoginHelper._analyze_login_response(
                    response.status,
                    response_text,
                    email
                )

                result['success'] = success
                result['message'] = message

                status_indicator = "[+]" if success else "[-]"
                print(f"{prefix} {status_indicator} Status: {response.status} - {message}")

                return result

        except aiohttp.ClientError as e:
            result['error'] = f"Connection error: {str(e)}"
            prefix = LoginHelper._log_prefix('submit_login', worker_label)
            print(f"{prefix} [-] {RED}Connection error: {str(e)}{RESET}")
            return result
        except Exception as e:
            result['error'] = f"Exception: {str(e)}"
            prefix = LoginHelper._log_prefix('submit_login', worker_label)
            print(f"{prefix} [-] {RED}Exception: {str(e)}{RESET}")
            return result

    @staticmethod
    def _analyze_login_response(status_code: int, response_text: str, email: str) -> tuple:
        """
        Analyze login response to determine if login was successful
        Checks HTTP status and response content for success indicators
        Returns: tuple of (success: bool, message: str)
        """
        if status_code in [200, 201]:
            try:
                response_json = json.loads(response_text)

                if "success" in response_json:
                    if response_json.get("success"):
                        message = response_json.get("message", f"{GREEN}Login successful{RESET}")
                        return True, message
                    else:
                        message = response_json.get("message", f"{RED}Login failed{RESET}")
                        return False, message

                if response_json.get("status") == "success":
                    message = response_json.get("message", f"{GREEN}Login successful{RESET}")
                    return True, message

                if response_json.get("error") or response_json.get("status") == "error":
                    message = response_json.get("message", response_json.get("error", "Unknown error"))
                    return False, message

                if response_json.get("errors"):
                    errors = response_json.get("errors")
                    if isinstance(errors, dict):
                        error_msg = "; ".join([f"{k}: {v}" for k, v in errors.items()])
                    else:
                        error_msg = str(errors)
                    return False, f"{RED}Validation errors: {error_msg}{RESET}"

                return True, f"{GREEN}HTTP {status_code} - Likely successful{RESET}"

            except json.JSONDecodeError:
                response_lower = response_text.lower()
                if any(keyword in response_lower for keyword in
                       ["success", "login successful", "welcome", "dashboard"]):
                    return True, f"HTTP {status_code} - Found success indicators"
                return True, f"HTTP {status_code}"

        elif status_code == 400:
            try:
                response_json = json.loads(response_text)
                if response_json.get("errors"):
                    errors = response_json.get("errors")
                    if isinstance(errors, dict):
                        error_msg = "; ".join([f"{k}: {v}" for k, v in errors.items()])
                    else:
                        error_msg = str(errors)
                    return False, f"{RED}Validation error: {error_msg}{RESET}"
                if response_json.get("message"):
                    return False, response_json.get("message")
            except json.JSONDecodeError:
                pass

            return False, f"{RED}Bad request (HTTP 400){RESET}"

        elif status_code in [301, 302, 303, 307, 308]:
            return True, f"{GREEN}Redirect (HTTP {status_code}) - Likely successful redirect{RESET}"

        elif status_code >= 500:
            return False, f"{RED}Server error (HTTP {status_code}){RESET}"

        elif status_code >= 400:
            return False, f"{RED}Client error (HTTP {status_code}){RESET}"

        return False, f"{RED}Unexpected status code: {status_code}{RESET}"

    @staticmethod
    def extract_links_from_html(html_content: str, base_url: str) -> list:
        """
        Extract all href links from HTML content and convert relative links to absolute
        Returns list of unique full URLs that point to the same domain
        """
        soup = BeautifulSoup(html_content, "html.parser")
        links = set()

        for link in soup.find_all("a", href=True):
            href = link.get("href")
            if not href or href.startswith("#") or href.startswith("javascript"):
                continue

            full_url = None
            if href.startswith("http"):
                full_url = href
            elif href.startswith("/"):
                full_url = f"{base_url}{href}"

            if full_url and base_url in full_url:
                links.add(full_url)

        return list(links)

    @staticmethod
    async def explore_casino(
            session: aiohttp.ClientSession,
            explore_urls: list,
            duration_seconds: int = 60,
            worker_label: str = ""
    ) -> Dict[str, any]:
        """
        Explore casino by making random GET requests to provided pages
        for a specified duration. Returns stats about explored pages.
        """
        prefix = LoginHelper._log_prefix('explore_casino', worker_label)
        if not explore_urls:
            print(f"{prefix} ERROR: No URLs provided for exploration")
            return {
                'total_requests': 0,
                'successful_requests': 0,
                'failed_requests': 0,
                'pages_visited': [],
                'start_time': time.time(),
                'end_time': time.time(),
                'duration': duration_seconds,
                'errors': ['No URLs provided']
            }

        stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'pages_visited': [],
            'start_time': time.time(),
            'end_time': None,
            'duration': duration_seconds,
            'errors': []
        }

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }

        print(f"\n{prefix} >>> Starting casino exploration for {duration_seconds} seconds...")
        print(f"{prefix} Available pages: {len(explore_urls)}")

        while time.time() - stats['start_time'] < duration_seconds:
            url = random.choice(explore_urls)
            page_name = url.split('/')[-2] if url.endswith('/') else url.split('/')[-1]

            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    stats['total_requests'] += 1

                    if response.status == 200:
                        stats['successful_requests'] += 1
                        page_info = {
                            'page': page_name,
                            'url': url,
                            'status': response.status,
                            'timestamp': time.time() - stats['start_time']
                        }
                        stats['pages_visited'].append(page_info)
                        print(f"{prefix} [+] {page_name}: HTTP {response.status} ({len(await response.read())} bytes)")
                    else:
                        stats['failed_requests'] += 1
                        error_msg = f"{RED}{page_name}: HTTP {response.status}{RESET}"
                        stats['errors'].append(error_msg)
                        print(f"{prefix} [-] {RED}{error_msg}{RESET}")

            except asyncio.TimeoutError:
                stats['failed_requests'] += 1
                error_msg = f"{page_name}: Timeout"
                stats['errors'].append(error_msg)
                print(f"{prefix} [-] {RED}{error_msg}{RESET}")
            except (asyncio.CancelledError, ConnectionError, OSError) as e:
                stats['failed_requests'] += 1
                error_type = type(e).__name__
                error_msg = f"{page_name}: {error_type}"
                stats['errors'].append(error_msg)
                print(f"{prefix} [-] {RED}{error_msg}{RESET}")
            except Exception as e:
                stats['failed_requests'] += 1
                error_msg = f"{page_name}: {type(e).__name__}"
                stats['errors'].append(error_msg)
                print(f"{prefix} [-] {RED}{error_msg}{RESET}")

            await asyncio.sleep(random.uniform(0.5, 2.0))

        stats['end_time'] = time.time()
        actual_duration = stats['end_time'] - stats['start_time']

        print(f"\n{prefix} >>> Exploration Complete")
        print(f"{prefix} Total requests: {stats['total_requests']}")
        print(f"{prefix} {GREEN}Successful: {stats['successful_requests']}{RESET}")
        print(f"{prefix} {RED}Failed: {stats['failed_requests']}{RESET}")
        print(f"{prefix} Actual duration: {actual_duration:.2f}s\n")

        return stats


login_state = {
    'helper': LoginHelper(),
    'form_fields': None,
    'form_action': None
}

form_cache_lock = None


async def _get_cached_login_form(helper: LoginHelper, session: aiohttp.ClientSession, login_url: str) -> tuple[
    dict, str]:
    global form_cache_lock
    cached_fields = login_state.get('form_fields')
    cached_action = login_state.get('form_action')
    if cached_fields is not None and cached_action is not None:
        return deepcopy(cached_fields), cached_action
    if form_cache_lock is None:
        form_cache_lock = asyncio.Lock()
    async with form_cache_lock:
        cached_fields = login_state.get('form_fields')
        cached_action = login_state.get('form_action')
        if cached_fields is None or cached_action is None:
            form_info = await helper.fetch_login_form(session, login_url)
            if not form_info:
                raise RuntimeError(f"{RED}Failed to fetch login form{RESET}")
            login_state['form_fields'] = form_info['fields']
            login_state['form_action'] = form_info.get('action') or login_url
            cached_fields = login_state['form_fields']
            cached_action = login_state['form_action']
    return deepcopy(cached_fields), cached_action


@global_setup()
def setup_login(args):
    """Initialize login test"""
    global form_cache_lock
    global_stats['start_time'] = time.time()
    metrics_port = resolve_metrics_port('LOGIN_EXPLORING_METRICS_PORT')
    try:
        start_http_server(metrics_port)
    except OSError as e:
        if "Address already in use" in str(e):
            print(f"[global_setup] Metrics server already running on {metrics_port}")
        else:
            raise

    print(f"[global_setup] Metrics server started on 0.0.0.0:{metrics_port}")
    print("[setup_login] Waiting 20 seconds before starting login workers")
    time.sleep(20)

    login_state['helper'] = LoginHelper()
    login_state['form_fields'] = None
    login_state['form_action'] = None
    form_cache_lock = None
    print("[setup_login] Login scenario initialized")


@global_teardown()
def teardown_login():
    """Cleanup after login test"""
    global_stats['end_time'] = time.time()
    elapsed = global_stats['end_time'] - global_stats['start_time']
    print("\n" + "=" * 70)
    print(f"STATISTIC SUMMARY - LOGIN AND EXPLORING SCENARIO")
    print("=" * 70)
    print(f"[global_teardown] Total test time: {elapsed:.2f}s")
    print(f"[global_teardown] Total logins: {global_stats['total_logins']}, "
          f"{GREEN}Success: {global_stats['successful_logins']},{RESET} "
          f"{RED}Failures: {global_stats['failed_logins']}{RESET}, "
          f"Pages explored: {global_stats['total_pages_explored']}, "
          f"{RED}Setup errors: {global_stats['setup_errors']}{RESET}")

    metrics_dir = Path(__file__).parent.parent / 'metrics'
    metrics_dir.mkdir(parents=True, exist_ok=True)

    try:
        metrics_text_file = metrics_dir / 'login_exploring_metrics.txt'
        with open(metrics_text_file, 'w') as f:
            f.write(generate_latest(REGISTRY).decode('utf-8'))
        print(f"[global_teardown] Metrics exported to {metrics_text_file}")
    except Exception as e:
        print(f"[global_teardown] {RED}ERROR exporting metrics: {e}{RESET}")

    metrics_json = {
        'total_workers': global_stats['total_workers'],
        'total_logins': global_stats['total_logins'],
        'successful_logins': global_stats['successful_logins'],
        'failed_logins': global_stats['failed_logins'],
        'total_pages_explored': global_stats['total_pages_explored'],
        'setup_errors': global_stats['setup_errors'],
    }

    try:
        metrics_json_file = metrics_dir / 'login_exploring_metrics.json'
        with open(metrics_json_file, 'w') as f:
            json.dump(metrics_json, f, indent=2)
        print(f"[global_teardown] JSON metrics exported to {metrics_json_file}")
    except Exception as e:
        print(f"[global_teardown] {RED}ERROR exporting JSON metrics: {e}{RESET}")


@setup_session()
async def setup_worker(worker_id, session):
    """Setup per worker"""
    async with ramp_lock:
        worker_index = ramp_state['next_worker_index']
        ramp_state['next_worker_index'] += 1
    delay = worker_index * 5
    if delay > 0:
        print(f"[setup_worker {worker_id}] {YELLOW}Ramp-up delay: waiting {delay}s{RESET}")
        await asyncio.sleep(delay)
    session.worker_id = worker_id
    global_stats['total_workers'] += 1
    prometheus_metrics['total_workers'].set(global_stats['total_workers'])
    print(f"[setup_worker {worker_id}] Worker initialized")


def _print_worker_login_summary(worker_id: str, email: str, login_result: Dict[str, Any],
                                explore_stats: Optional[Dict[str, Any]] = None):
    print("\n" + "=" * 70)
    print(f"WORKER {worker_id} RESULT")
    print("=" * 70)
    print(f"User Email: {email}")
    status = login_result.get('status')
    if status is not None:
        print(f"HTTP Status: {status}")
    success = login_result.get('success')
    print(f"Success: {'[+] YES' if success else '[-] NO'}")
    message = login_result.get('message') or login_result.get('error') or 'No message'
    print(f"Message: {message}")
    if explore_stats:
        total_requests = explore_stats.get('total_requests')
        successful = explore_stats.get('successful_requests')
        failed = explore_stats.get('failed_requests')
        if total_requests is not None:
            print(f"Total page requests: {total_requests}")
        if successful is not None:
            print(f"{GREEN}Successful: {successful}{RESET}")
        if failed is not None:
            print(f"{RED}Failed: {failed}{RESET}")
    print("=" * 70 + "\n")


def _build_login_result(success: bool, message: str, status: Optional[int] = None, error: Optional[str] = None) -> Dict[
    str, Any]:
    return {
        'success': success,
        'message': message,
        'status': status,
        'error': error
    }


@scenario()
async def login_and_exploring_scenario(session):
    """
    Main login and exploring scenario:
    1. Load user from CSV file with rate limiting
    2. Get login URL from config
    3. Fetch login form to analyze available fields
    4. Build login payload dynamically based on form fields
    5. Submit login via POST API
    6. Analyze response to verify success
    """
    worker_id = getattr(session, 'worker_id', None)
    if not worker_id or worker_id == 'unknown':
        worker_id = f"session-{id(session)}"
        try:
            session.worker_id = worker_id
        except Exception:
            pass
    start_time = time.time()
    helper = login_state['helper']
    worker_str = str(worker_id)
    worker_label = f"[WORKER {worker_str}]"

    def worker_log(message: str):
        print(f"[login_and_exploring_scenario]{worker_label} {message}")

    worker_log(">>> STEP 1: Loading user credentials with rate limiting...")
    user = get_login_exploring_user()
    if not user:
        worker_log(f"{RED}ERROR: Could not load user{RESET}")
        failure_result = _build_login_result(False, f"{RED}Failed to load user credentials{RESET}",
                                             error="no_available_users")
        _print_worker_login_summary(worker_str, "unknown", failure_result)
        raise RuntimeError(f"{RED}Failed to load user credentials{RESET}")

    email = user.get('email', '')
    worker_log(f"User loaded: {email}")

    async with aiohttp.ClientSession() as aio_session:
        worker_log(">>> STEP 2: Getting login URL from config...")
        login_url = config.fo_url['login']
        worker_log(f"Login URL: {login_url}")

        worker_log(">>> STEP 3: Loading cached login form metadata...")
        try:
            form_fields, login_submit_url = await _get_cached_login_form(helper, aio_session, login_url)
        except Exception as e:
            message = f"Exception loading cached login form: {e}"
            failure_result = _build_login_result(False, message, error=str(e))
            _print_worker_login_summary(worker_str, email, failure_result)
            raise

        worker_log(f"Login submit URL: {login_submit_url}")
        worker_log(f"Using {len(form_fields)} cached form fields")

        worker_log(">>> STEP 4: Building login payload...")
        payload = helper.build_login_payload(user, form_fields, worker_label)

        if not payload:
            worker_log(f"{RED}ERROR: No payload created for {email}{RESET}")
            failure_result = _build_login_result(False, f"{RED}Failed to build login payload{RESET}",
                                                 error="No form fields mapped")
            _print_worker_login_summary(worker_str, email, failure_result)
            raise RuntimeError(f"No payload created for {email}")

        worker_log(f"Built payload with {len(payload)} fields")

        worker_log(">>> STEP 5: Submitting login...")
        login_result = await helper.submit_login(aio_session, login_submit_url, payload, email, worker_label)

        worker_log(">>> STEP 6: Analyzing response...")
        latency = time.time() - start_time
        global_stats['total_logins'] += 1
        prometheus_metrics['total_logins'].inc()

        if login_result['success']:
            worker_log(f"{GREEN}✓ LOGIN SUCCESSFUL for {email}{RESET}")
            worker_log(f"Response: {login_result['message']}")

            global_stats['successful_logins'] += 1
            prometheus_metrics['successful_logins'].inc()
            prometheus_metrics['login_latency'].labels(worker=worker_str, status='success').observe(latency)

            worker_log(">>> STEP 7: Extracting navigation links from page...")
            explore_urls = helper.extract_links_from_html(login_result['response'], f"https://{config.fo_base_url}")
            worker_log(f"Extracted {len(explore_urls)} links from page")
            worker_log("Found URLs:")
            for url in sorted(explore_urls):
                worker_log(f"  - {url}")

            summary_explore_stats = None
            if explore_urls:
                worker_log(">>> STEP 8: Starting casino exploration...")
                explore_stats = await helper.explore_casino(aio_session, explore_urls, duration_seconds=120,
                                                            worker_label=worker_label)
                summary_explore_stats = explore_stats

                worker_log(">>> STEP 9: Exploration complete")
                worker_log(f"Visited {explore_stats['successful_requests']} pages successfully")
                global_stats['total_pages_explored'] += explore_stats['successful_requests']
                prometheus_metrics['pages_explored'].inc(explore_stats['successful_requests'])
            else:
                worker_log(f"{YELLOW}WARNING: No links extracted from page, skipping exploration{RESET}")
                summary_explore_stats = None

            _print_worker_login_summary(worker_str, email, login_result, summary_explore_stats)
        else:
            worker_log(f"{RED}✗ LOGIN FAILED for {email}{RESET}")
            worker_log(f"{RED}Error: {login_result['message'] or login_result['error']}{RESET}")
            if login_result['response']:
                worker_log(f"Response preview: {login_result['response'][:300]}")

            global_stats['failed_logins'] += 1
            prometheus_metrics['failed_logins'].inc()
            prometheus_metrics['login_latency'].labels(worker=worker_str, status='failure').observe(latency)
            error_msg = login_result['message'] or login_result['error']
            prometheus_metrics['login_errors'].labels(worker=worker_str, error_type=error_msg[:30]).inc()
            _print_worker_login_summary(worker_str, email, login_result)
            raise RuntimeError(f"Login failed: {error_msg}")


async def login_scenario_single_user():
    """Test login for a single user - for manual testing"""
    print("\n" + "=" * 70)
    print("SINGLE USER LOGIN TEST")
    print("=" * 70 + "\n")

    helper = LoginHelper()
    single_worker_label = "[WORKER SINGLE]"

    print("[login_scenario_single_user] >>> STEP 1: Loading user credentials...")
    user = get_login_exploring_user()
    if not user:
        print(f"[login_scenario_single_user] {RED}ERROR: Could not load user{RESET}")
        return

    email = user.get('email', '')
    password = user.get('password', '')
    print(f"[login_scenario_single_user] Testing with user: {email}\n")

    async with aiohttp.ClientSession() as session:
        print("[login_scenario_single_user] >>> STEP 2: Fetching login form...")
        login_url = config.fo_url['login']
        form_info = await helper.fetch_login_form(session, login_url)
        if not form_info:
            print(f"[login_scenario_single_user] {RED}ERROR: Could not fetch form{RESET}")
            return

        form_fields = form_info['fields']
        print(f"[login_scenario_single_user] Form fields detected: {list(form_fields.keys())}\n")

        print("[login_scenario_single_user] >>> STEP 3: Building payload...")
        payload = helper.build_login_payload(user, form_fields, single_worker_label)

        if not payload:
            print(f"[login_scenario_single_user] {RED}ERROR: Could not build payload{RESET}")
            return

        print(f"[login_scenario_single_user] Payload fields: {list(payload.keys())}\n")

        print("[login_scenario_single_user] >>> STEP 4: Submitting login...")
        login_submit_url = form_info.get('action') or config.fo_url['login_submit']
        login_result = await helper.submit_login(session, login_submit_url, payload, email, single_worker_label)

        print(f"\n[login_scenario_single_user] >>> STEP 5: Login Result")
        print("=" * 70)
        print(f"User Email: {email}")
        print(f"HTTP Status: {login_result['status']}")
        print(f"Success: {'[+] YES' if login_result['success'] else '[-] NO'}")
        print(f"Message: {login_result['message']}")
        if login_result['error']:
            print(f"{RED}Error: {login_result['error']}{RESET}")
        print("=" * 70)

        if login_result['success']:
            print("\n[login_scenario_single_user] >>> STEP 6: Extracting navigation links from page...")
            explore_urls = helper.extract_links_from_html(login_result['response'], f"https://{config.fo_base_url}")
            print(f"[login_scenario_single_user] Extracted {len(explore_urls)} links from page")
            print(f"[login_scenario_single_user] Found URLs:")
            for url in sorted(explore_urls):
                print(f"[login_scenario_single_user]   - {url}")
            print()

            if explore_urls:
                print("[login_scenario_single_user] >>> STEP 7: Starting casino exploration...")
                # ToDo: Get Duration time from the GitLab variable! Add by default 5 min.
                explore_stats = await helper.explore_casino(session, explore_urls, duration_seconds=60,
                                                            worker_label=single_worker_label)

                print("[login_scenario_single_user] >>> STEP 8: Exploration Summary")
                print("=" * 70)
                print(f"Total page requests: {explore_stats['total_requests']}")
                print(f"{GREEN}Successful: {explore_stats['successful_requests']}{RESET}")
                print(f"{RED}Failed: {explore_stats['failed_requests']}{RESET}")
                if explore_stats['total_requests'] > 0:
                    print(
                        f"Success rate: {(explore_stats['successful_requests'] / explore_stats['total_requests'] * 100):.1f}%")
                print("=" * 70 + "\n")
            else:
                print(
                    f"[login_scenario_single_user] {YELLOW}WARNING: No links extracted from page, skipping exploration{RESET}\n")

    return login_result


if __name__ == "__main__":
    asyncio.run(login_scenario_single_user())
