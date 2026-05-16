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
from scenarios.utils.user_manager import get_login_logout_user

YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

global_stats = {
    'total_workers': 0,
    'total_logins': 0,
    'successful_logins': 0,
    'failed_logins': 0,
    'total_logouts': 0,
    'successful_logouts': 0,
    'failed_logouts': 0,
    'setup_errors': 0,
}

prometheus_metrics = {
    'total_workers': Gauge('fortuna_login_logout_workers', 'Total login logout workers'),
    'total_logins': Counter('fortuna_login_logout_total_logins', 'Total login attempts'),
    'successful_logins': Counter('fortuna_login_logout_successful_logins', 'Successful logins'),
    'failed_logins': Counter('fortuna_login_logout_failed_logins', 'Failed logins'),
    'total_logouts': Counter('fortuna_total_logouts', 'Total logout attempts'),
    'successful_logouts': Counter('fortuna_successful_logouts', 'Successful logouts'),
    'failed_logouts': Counter('fortuna_failed_logouts', 'Failed logouts'),
    'setup_errors': Counter('fortuna_login_logout_setup_errors', 'Login logout setup errors', ['worker']),
    'login_logout_latency': Histogram('fortuna_login_logout_latency_seconds', 'Login/Logout latency',
                                      ['worker', 'status'],
                                      buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0)),
    'login_logout_errors': Counter('fortuna_login_logout_errors_total', 'Login/Logout errors by worker',
                                   ['worker', 'error_type']),
}

ramp_lock = asyncio.Lock()
ramp_state = {'next_worker_index': 0}


class LoginLogoutHelper:
    """Helper class to manage login and logout operations"""

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
    def load_random_user() -> Optional[Dict[str, str]]:
        """
        Load random user credentials from one of the CSV files in data/users/
        Returns dict with 'email' and 'password' or None if no users found
        """
        data_dir = Path(__file__).parent.parent / 'data' / 'users'
        csv_files = list(data_dir.glob('*.csv'))

        if not csv_files:
            print(f"[LoginLogoutHelper] {RED}ERROR: No CSV files found in data/users/{RESET}")
            return None

        selected_file = random.choice(csv_files)
        print(f"[LoginLogoutHelper] Loading users from {selected_file.name}")

        try:
            with open(selected_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                users = list(reader)

            if not users:
                print(f"[LoginLogoutHelper] {RED}ERROR: No users found in {selected_file.name}{RESET}")
                return None

            user = random.choice(users)
            print(f"[LoginLogoutHelper] Selected user: {user.get('email', 'unknown')}")
            return user

        except Exception as e:
            print(f"[LoginLogoutHelper] {RED}ERROR loading users: {e}{RESET}")
            return None

    @staticmethod
    async def fetch_login_form(session: aiohttp.ClientSession, login_url: str) -> Optional[Dict[str, Any]]:
        """
        Fetch login form, extract fields, and provide submit metadata (action/method)
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

            login_form = soup.find("form", {"id": "login-form"})
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
                print(f"[LoginLogoutHelper] {RED}ERROR: Could not locate a login form{RESET}")
                return None

            action_attr = (login_form.get("action") or "").strip()
            method = (login_form.get("method") or "POST").upper()
            submit_url = urljoin(login_url, action_attr) if action_attr else login_url

            print("[LoginLogoutHelper] Found login form, parsing fields...")

            for input_field in login_form.find_all("input"):
                name = input_field.get("name")
                if not name:
                    continue

                field_type = input_field.get("type", "text")
                value = input_field.get("value", "")
                required = input_field.get("required") is not None

                if name in LoginLogoutHelper.IGNORED_FIELDS:
                    if name in ['_csrf_token', 'csrf_token', 'csrf', 'token']:
                        form_fields[name] = {
                            "type": "hidden",
                            "required": False,
                            "value": value,
                            "is_csrf": True
                        }
                        print(f"[LoginLogoutHelper] Found CSRF token: {name} = {value[:40]}...")
                    continue

                form_fields[name] = {
                    "type": field_type,
                    "required": required,
                    "value": value
                }
                print(f"[LoginLogoutHelper] Found input field: {name} (type: {field_type}, required: {required})")

            for textarea in login_form.find_all("textarea"):
                name = textarea.get("name")
                if name and name not in LoginLogoutHelper.IGNORED_FIELDS:
                    required = textarea.get("required") is not None
                    form_fields[name] = {
                        "type": "textarea",
                        "required": required
                    }
                    print(f"[LoginLogoutHelper] Found textarea field: {name} (required: {required})")

            for select in login_form.find_all("select"):
                name = select.get("name")
                if name and name not in LoginLogoutHelper.IGNORED_FIELDS:
                    required = select.get("required") is not None
                    options = [opt.get("value", opt.text) for opt in select.find_all("option")]
                    form_fields[name] = {
                        "type": "select",
                        "required": required,
                        "options": options
                    }
                    print(f"[LoginLogoutHelper] Found select field: {name} (required: {required})")

            print(f"\n[LoginLogoutHelper] Found {len(form_fields)} form fields: {list(form_fields.keys())}\n")
            return {
                "fields": form_fields,
                "action": submit_url,
                "method": method
            }

        except Exception as e:
            print(f"[LoginLogoutHelper] {RED}ERROR fetching login form: {e}{RESET}")
            return None

    @staticmethod
    def build_login_payload(
            user: Dict[str, str],
            form_fields: Dict[str, dict]
    ) -> Dict[str, str]:
        """
        Dynamically build login payload based on form fields and user credentials
        Maps email/username and password fields based on field names
        Includes CSRF token if present in form
        """
        payload = {}

        email = user.get('email', '')
        password = user.get('password', '')

        print(f"\n[build_login_payload] ====== BUILDING PAYLOAD ======")
        print(f"[build_login_payload] Available form fields: {list(form_fields.keys())}")
        print(f"[build_login_payload] User email: {email}")

        for field_name, field_info in form_fields.items():
            field_type = field_info.get('type', 'text')

            if field_info.get('is_csrf'):
                csrf_value = field_info.get('value', '')
                payload[field_name] = csrf_value
                print(f"[build_login_payload] CSRF Token {field_name}: {csrf_value[:40]}...")

            elif any(keyword in field_name.lower() for keyword in ['_username', 'username', '_email', 'email']):
                if not any(kw in field_name.lower() for kw in ['confirm', 'promo', 'marketing']):
                    payload[field_name] = email
                    print(f"[build_login_payload] Email/Username field {field_name}: {email}")

            elif any(keyword in field_name.lower() for keyword in ['_password', 'password']):
                if not any(kw in field_name.lower() for kw in ['confirm', 'old']):
                    payload[field_name] = password
                    print(f"[build_login_payload] Password field {field_name}: {'*' * len(password)}")

        print(f"\n[build_login_payload] ====== PAYLOAD SUMMARY ======")
        print(f"[build_login_payload] Total fields in payload: {len(payload)}")
        print(f"[build_login_payload] Payload fields: {list(payload.keys())}\n")

        return payload

    @staticmethod
    async def submit_login(
            session: aiohttp.ClientSession,
            login_submit_url: str,
            payload: Dict[str, str],
            email: str
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
            print(f"\n[submit_login] ====== SUBMITTING LOGIN ======")
            print(f"[submit_login] Submitting login for {email}")
            print(f"[submit_login] URL: {login_submit_url}")
            print(f"[submit_login] Form data length: {len(form_data)} bytes")
            print(f"\n[submit_login] ====== PAYLOAD DETAILS ======")
            for key, value in payload.items():
                value_preview = value if len(str(value)) <= 50 else f"{str(value)[:50]}..."
                print(f"[submit_login]   {key}: {value_preview}")
            print(f"[submit_login] ====== ENCODED FORM DATA ======")
            print(f"[submit_login] {form_data}\n")

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

                success, message = LoginLogoutHelper._analyze_login_response(
                    response.status,
                    response_text,
                    email
                )

                result['success'] = success
                result['message'] = message

                status_indicator = "[+]" if success else "[-]"
                print(f"[submit_login] {status_indicator} Status: {response.status} - {message}")

                return result

        except aiohttp.ClientError as e:
            result['error'] = f"Connection error: {str(e)}"
            print(f"[submit_login] [-] Connection error: {str(e)}")
            return result
        except Exception as e:
            result['error'] = f"Exception: {str(e)}"
            print(f"[submit_login] [-] Exception: {str(e)}")
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
                        message = response_json.get("message", "Login successful")
                        return True, message
                    else:
                        message = response_json.get("message", "Login failed")
                        return False, message

                if response_json.get("status") == "success":
                    message = response_json.get("message", "Login successful")
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
                return True, f"{GREEN}HTTP {status_code}{RESET}"

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
    async def submit_logout(
            session: aiohttp.ClientSession,
            logout_url: str,
            email: str
    ) -> Dict[str, any]:
        """
        Submit logout via GET/POST request
        Returns dict with logout result (success, status, message)
        """
        result = {
            'success': False,
            'status': None,
            'error': None,
            'message': None
        }

        try:
            print(f"\n[submit_logout] ====== SUBMITTING LOGOUT ======")
            print(f"[submit_logout] Logging out user: {email}")
            print(f"[submit_logout] URL: {logout_url}")

            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
            }

            async with session.get(
                    logout_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                result['status'] = response.status

                if response.status in [200, 301, 302, 303, 307, 308]:
                    result['success'] = True
                    result['message'] = f"Logout successful (HTTP {response.status})"
                    print(f"[submit_logout] [+] Status: {response.status} - Logout successful")
                else:
                    result['message'] = f"Unexpected status code: {response.status}"
                    print(f"[submit_logout] [-] Status: {response.status} - {result['message']}")

                return result

        except aiohttp.ClientError as e:
            result['error'] = f"Connection error: {str(e)}"
            print(f"[submit_logout] [-] {RED}Connection error: {str(e)}{RESET}")
            return result
        except Exception as e:
            result['error'] = f"Exception: {str(e)}"
            print(f"[submit_logout] [-] {RED}Exception: {str(e)}{RESET}")
            return result


login_logout_state = {
    'helper': LoginLogoutHelper(),
    'form_fields': None,
    'form_action': None
}

form_cache_lock = None


async def _get_cached_login_form(helper: LoginLogoutHelper, session: aiohttp.ClientSession, login_url: str) -> tuple[
    dict, str]:
    global form_cache_lock
    cached_fields = login_logout_state.get('form_fields')
    cached_action = login_logout_state.get('form_action')
    if cached_fields is not None and cached_action is not None:
        return deepcopy(cached_fields), cached_action
    if form_cache_lock is None:
        form_cache_lock = asyncio.Lock()
    async with form_cache_lock:
        cached_fields = login_logout_state.get('form_fields')
        cached_action = login_logout_state.get('form_action')
        if cached_fields is None or cached_action is None:
            form_info = await helper.fetch_login_form(session, login_url)
            if not form_info:
                raise RuntimeError(f"{RED}Failed to fetch login form{RESET}")
            login_logout_state['form_fields'] = form_info['fields']
            login_logout_state['form_action'] = form_info.get('action') or login_url
            cached_fields = login_logout_state['form_fields']
            cached_action = login_logout_state['form_action']
    return deepcopy(cached_fields), cached_action


def _print_worker_login_logout_summary(worker_id: str, email: str, login_result: Dict[str, Any],
                                       logout_result: Optional[Dict[str, Any]] = None):
    print("\n" + "=" * 70)
    print(f"WORKER {worker_id} LOGIN/LOGOUT RESULT")
    print("=" * 70)
    print(f"User Email: {email or 'unknown'}")
    login_status = login_result.get('status')
    if login_status is not None:
        print(f"Login HTTP Status: {login_status}")
    print(f"Login Success: {'[+] YES' if login_result.get('success') else '[-] NO'}")
    login_message = login_result.get('message') or login_result.get('error') or 'No message'
    print(f"Login Message: {login_message}")
    if logout_result:
        logout_status = logout_result.get('status')
        if logout_status is not None:
            print(f"Logout HTTP Status: {logout_status}")
        print(f"Logout Success: {'[+] YES' if logout_result.get('success') else '[-] NO'}")
        logout_message = logout_result.get('message') or logout_result.get('error') or 'No message'
        print(f"Logout Message: {logout_message}")
    else:
        print("Logout not attempted")
    print("=" * 70 + "\n")


@global_setup()
def setup_login_logout(args):
    """Initialize login logout test"""
    global form_cache_lock
    global_stats['start_time'] = time.time()
    metrics_port = resolve_metrics_port('LOGIN_LOGOUT_METRICS_PORT')
    try:
        start_http_server(metrics_port)
    except OSError as e:
        if "Address already in use" in str(e):
            print(f"[global_setup] Metrics server already running on {metrics_port}")
        else:
            raise

    print(f"[global_setup] Metrics server started on 0.0.0.0:{metrics_port}")
    print("[setup_login_logout] Waiting 20 seconds before starting login/logout workers")
    time.sleep(20)

    login_logout_state['helper'] = LoginLogoutHelper()
    login_logout_state['form_fields'] = None
    login_logout_state['form_action'] = None
    form_cache_lock = None
    print("[setup_login_logout] Login/Logout scenario initialized")


@global_teardown()
def teardown_login_logout():
    """Cleanup after login logout test"""
    global_stats['end_time'] = time.time()
    elapsed = global_stats['end_time'] - global_stats['start_time']
    print("\n" + "=" * 70)
    print(f"STATISTIC SUMMARY - LOGIN LOGOUT SCENARIO")
    print("=" * 70)
    print(f"[global_teardown] Total test time: {elapsed:.2f}s")
    print(f"[global_teardown] Total logins: {global_stats['total_logins']}, "
          f"{GREEN}Success: {global_stats['successful_logins']}{RESET}, "
          f"{RED}Failures: {global_stats['failed_logins']}{RESET}")
    print(f"[global_teardown] Total logouts: {global_stats['total_logouts']}, "
          f"{GREEN}Success: {global_stats['successful_logouts']}{RESET}, "
          f"{RED}Failures: {global_stats['failed_logouts']}{RESET}, "
          f"{RED}Setup errors: {global_stats['setup_errors']}{RESET}")

    metrics_dir = Path(__file__).parent.parent / 'metrics'
    metrics_dir.mkdir(parents=True, exist_ok=True)

    try:
        metrics_text_file = metrics_dir / 'login_logout_metrics.txt'
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
        'total_logouts': global_stats['total_logouts'],
        'successful_logouts': global_stats['successful_logouts'],
        'failed_logouts': global_stats['failed_logouts'],
        'setup_errors': global_stats['setup_errors'],
    }

    try:
        metrics_json_file = metrics_dir / 'login_logout_metrics.json'
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
        print(f"[setup_worker {worker_id}] Ramp-up delay: waiting {delay}s")
        await asyncio.sleep(delay)
    session.worker_id = worker_id
    global_stats['total_workers'] += 1
    prometheus_metrics['total_workers'].set(global_stats['total_workers'])
    print(f"[setup_worker {worker_id}] Worker initialized")


@scenario()
async def login_logout_scenario(session):
    """
    Main login and logout scenario:
    1. Load user from CSV file with rate limiting
    2. Get login URL from config
    3. Fetch login form to analyze available fields
    4. Build login payload dynamically based on form fields
    5. Submit login via POST API
    6. Analyze response to verify success
    7. Wait 15-20 seconds
    8. Submit logout request
    """
    worker_id = getattr(session, 'worker_id', None)
    if not worker_id or worker_id == 'unknown':
        worker_id = f"session-{id(session)}"
        try:
            session.worker_id = worker_id
        except Exception:
            pass
    start_time = time.time()
    helper = login_logout_state['helper']
    worker_label = str(worker_id)
    logout_result = None

    print("[login_logout_scenario] >>> STEP 1: Loading user credentials with rate limiting...")
    user = get_login_logout_user()
    if not user:
        print(f"[login_logout_scenario] {RED}ERROR: Could not load user{RESET}")
        raise RuntimeError("Failed to load user credentials")

    email = user.get('email', '')
    print(f"[login_logout_scenario] User loaded: {email}")

    async with aiohttp.ClientSession() as aio_session:
        print("[login_logout_scenario] >>> STEP 2: Getting login URL from config...")
        login_url = config.fo_url['login']
        logout_url = config.fo_url['logout']
        print(f"[login_logout_scenario] Login URL: {login_url}")
        print(f"[login_logout_scenario] Logout URL: {logout_url}")

        print("[login_logout_scenario] >>> STEP 3: Loading cached login form metadata...")
        form_fields, login_submit_url = await _get_cached_login_form(helper, aio_session, login_url)
        print(f"[login_logout_scenario] Login submit URL: {login_submit_url}")
        print(f"[login_logout_scenario] Using {len(form_fields)} cached form fields")

        print("[login_logout_scenario] >>> STEP 4: Building login payload...")
        payload = helper.build_login_payload(user, form_fields)

        if not payload:
            print(f"[login_logout_scenario] {RED}ERROR: No payload created for {email}{RESET}")
            raise RuntimeError(f"No payload created for {email}")

        print(f"[login_logout_scenario] Built payload with {len(payload)} fields")

        print("[login_logout_scenario] >>> STEP 5: Submitting login...")
        login_result = await helper.submit_login(aio_session, login_submit_url, payload, email)

        print("[login_logout_scenario] >>> STEP 6: Analyzing login response...")
        latency = time.time() - start_time
        global_stats['total_logins'] += 1
        prometheus_metrics['total_logins'].inc()

        if login_result['success']:
            print(f"[login_logout_scenario] {GREEN}✓ LOGIN SUCCESSFUL for {email}{RESET}")
            print(f"[login_logout_scenario] Response: {login_result['message']}")

            global_stats['successful_logins'] += 1
            prometheus_metrics['successful_logins'].inc()
            prometheus_metrics['login_logout_latency'].labels(worker=str(worker_id), status='login_success').observe(
                latency)

            wait_time = random.uniform(15, 20)
            print(f"\n[login_logout_scenario] >>> STEP 7: Waiting {wait_time:.2f} seconds before logout...")
            await asyncio.sleep(wait_time)
            print(f"[login_logout_scenario] Wait complete, proceeding to logout")

            print("[login_logout_scenario] >>> STEP 8: Submitting logout...")
            logout_start = time.time()
            logout_result = await helper.submit_logout(aio_session, logout_url, email)
            logout_latency = time.time() - logout_start

            global_stats['total_logouts'] += 1
            prometheus_metrics['total_logouts'].inc()

            if logout_result['success']:
                print(f"[login_logout_scenario] {GREEN}✓ LOGOUT SUCCESSFUL for {email}{RESET}")
                print(f"[login_logout_scenario] Response: {logout_result['message']}")

                global_stats['successful_logouts'] += 1
                prometheus_metrics['successful_logouts'].inc()
                prometheus_metrics['login_logout_latency'].labels(worker=str(worker_id),
                                                                  status='logout_success').observe(logout_latency)
                _print_worker_login_logout_summary(worker_label, email, login_result, logout_result)
            else:
                print(f"[login_logout_scenario] {RED}✗ LOGOUT FAILED for {email}{RESET}")
                print(f"[login_logout_scenario] Error: {logout_result['message'] or logout_result['error']}")

                global_stats['failed_logouts'] += 1
                prometheus_metrics['failed_logouts'].inc()
                prometheus_metrics['login_logout_latency'].labels(worker=str(worker_id),
                                                                  status='logout_failure').observe(logout_latency)
                error_msg = logout_result['message'] or logout_result['error']
                prometheus_metrics['login_logout_errors'].labels(worker=str(worker_id), error_type=error_msg[:30]).inc()
                _print_worker_login_logout_summary(worker_label, email, login_result, logout_result)
                raise RuntimeError(f"Logout failed: {error_msg}")

        else:
            print(f"[login_logout_scenario] {RED}✗ LOGIN FAILED for {email}{RESET}")
            print(f"[login_logout_scenario] Error: {login_result['message'] or login_result['error']}")
            if login_result['response']:
                print(f"[login_logout_scenario] Response preview: {login_result['response'][:300]}")

            global_stats['failed_logins'] += 1
            prometheus_metrics['failed_logins'].inc()
            prometheus_metrics['login_logout_latency'].labels(worker=str(worker_id), status='login_failure').observe(
                latency)
            error_msg = login_result['message'] or login_result['error']
            prometheus_metrics['login_logout_errors'].labels(worker=str(worker_id), error_type=error_msg[:30]).inc()
            _print_worker_login_logout_summary(worker_label, email, login_result)
            raise RuntimeError(f"Login failed: {error_msg}")


async def login_logout_scenario_single_user():
    """Test login and logout for a single user - for manual testing"""
    print("\n" + "=" * 70)
    print("SINGLE USER LOGIN/LOGOUT TEST")
    print("=" * 70 + "\n")

    helper = LoginLogoutHelper()

    print("[login_logout_scenario_single_user] >>> STEP 1: Loading user credentials...")
    user = get_login_logout_user()
    if not user:
        print(f"[login_logout_scenario_single_user] {RED}ERROR: Could not load user{RESET}")
        return

    email = user.get('email', '')
    password = user.get('password', '')
    print(f"[login_logout_scenario_single_user] Testing with user: {email}\n")

    async with aiohttp.ClientSession() as session:
        print("[login_logout_scenario_single_user] >>> STEP 2: Fetching login form...")
        login_url = config.fo_url['login']
        form_info = await helper.fetch_login_form(session, login_url)
        if not form_info:
            print(f"[login_logout_scenario_single_user] {RED}ERROR: Could not fetch form{RESET}")
            return

        form_fields = form_info['fields']
        print(f"[login_logout_scenario_single_user] Form fields detected: {list(form_fields.keys())}\n")

        print("[login_logout_scenario_single_user] >>> STEP 3: Building payload...")
        payload = helper.build_login_payload(user, form_fields)

        if not payload:
            print(f"[login_logout_scenario_single_user] {RED}ERROR: Could not build payload{RESET}")
            return

        print(f"[login_logout_scenario_single_user] Payload fields: {list(payload.keys())}\n")

        print("[login_logout_scenario_single_user] >>> STEP 4: Submitting login...")
        login_submit_url = form_info.get('action') or config.fo_url['login_submit']
        login_result = await helper.submit_login(session, login_submit_url, payload, email)

        print(f"\n[login_logout_scenario_single_user] >>> STEP 5: Login Result")
        print("=" * 70)
        print(f"User Email: {email}")
        print(f"HTTP Status: {login_result['status']}")
        print(f"Success: {'[+] YES' if login_result['success'] else '[-] NO'}")
        print(f"Message: {login_result['message']}")
        if login_result['error']:
            print(f"Error: {login_result['error']}")
        print("=" * 70)

        if login_result['success']:
            wait_time = random.uniform(15, 20)
            print(f"\n[login_logout_scenario_single_user] >>> STEP 6: Waiting {wait_time:.2f} seconds before logout...")
            await asyncio.sleep(wait_time)
            print(f"[login_logout_scenario_single_user] Wait complete, proceeding to logout\n")

            print("[login_logout_scenario_single_user] >>> STEP 7: Submitting logout...")
            logout_url = config.fo_url['logout']
            logout_result = await helper.submit_logout(session, logout_url, email)

            print(f"\n[login_logout_scenario_single_user] >>> STEP 8: Logout Result")
            print("=" * 70)
            print(f"User Email: {email}")
            print(f"HTTP Status: {logout_result['status']}")
            print(f"Success: {'[+] YES' if logout_result['success'] else '[-] NO'}")
            print(f"Message: {logout_result['message']}")
            if logout_result['error']:
                print(f"{RED}Error: {logout_result['error']}{RESET}")
            print("=" * 70 + "\n")

    return login_result, logout_result


if __name__ == "__main__":
    asyncio.run(login_logout_scenario_single_user())
