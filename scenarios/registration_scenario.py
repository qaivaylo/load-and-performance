import asyncio
import csv
import json
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlencode

import aiohttp
from bs4 import BeautifulSoup
from molotov import scenario, global_setup, global_teardown, setup_session
from prometheus_client import Counter, Gauge, Histogram, start_http_server

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import config
from scenarios.utils.metrics import resolve_metrics_port
from scenarios.utils.registration_helper import Registration

YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

ROOT_DIR = Path(__file__).resolve().parent.parent
USERS_DATA_DIR = ROOT_DIR / "data" / "users"
LOGIN_EXPL_CSV_FILENAME = "tmp_login_expl.csv"
LOGIN_LOGOUT_CSV_FILENAME = "tmp_login_logout.csv"
CSV_TARGET_FILENAMES = {
    'login_exploring': LOGIN_EXPL_CSV_FILENAME,
    'login_logout': LOGIN_LOGOUT_CSV_FILENAME,
}
CSV_TARGET_ORDER = ['login_exploring', 'login_logout']
CSV_HEADERS = ["email", "password", "status"]


def _ensure_csv_schema(csv_path: Path):
    USERS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not csv_path.exists():
        with csv_path.open('w', newline='', encoding='utf-8') as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(CSV_HEADERS)
        print(f"[registration_scenario] {GREEN}Success CSV initialized at {csv_path}{RESET}")
        return

    with csv_path.open('r', newline='', encoding='utf-8') as csv_file:
        rows = list(csv.reader(csv_file))

    if not rows:
        with csv_path.open('w', newline='', encoding='utf-8') as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(CSV_HEADERS)
        print(f"[registration_scenario] {GREEN}Success CSV reinitialized at {csv_path}{RESET}")
        return

    header = [value.strip().lower() for value in rows[0]] if rows else []
    if len(header) >= len(CSV_HEADERS) and header[2] == 'status':
        return

    data_rows = rows[1:]
    with csv_path.open('w', newline='', encoding='utf-8') as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(CSV_HEADERS)
        for row in data_rows:
            email = row[0] if len(row) > 0 else ''
            password = row[1] if len(row) > 1 else ''
            status = row[2] if len(row) > 2 else ''
            writer.writerow([email, password, status])
    print(f"[registration_scenario] Upgraded success CSV schema at {csv_path}")


global_stats = {
    'total_workers': 0,
    'total_registrations': 0,
    'successful_registrations': 0,
    'failed_registrations': 0,
    'setup_errors': 0,
}

prometheus_metrics = {
    'total_workers': Gauge('fortuna_registration_workers', 'Total registration workers'),
    'total_registrations': Counter('fortuna_total_registrations', 'Total registration attempts'),
    'successful_registrations': Counter('fortuna_successful_registrations', 'Successful registrations'),
    'failed_registrations': Counter('fortuna_failed_registrations', 'Failed registrations'),
    'setup_errors': Counter('fortuna_registration_setup_errors', 'Registration setup errors', ['worker']),
    'registration_latency': Histogram('fortuna_registration_latency_seconds', 'Registration latency',
                                      ['worker', 'status'],
                                      buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0)),
    'registration_errors': Counter('fortuna_registration_errors_total', 'Registration errors by worker',
                                   ['worker', 'error_type']),
}

ramp_lock = asyncio.Lock()
ramp_state = {'next_worker_index': 0}


class RegistrationHelper:
    """Helper class to manage registration form parsing and user data mapping"""

    IGNORED_FIELDS = {
        'otp',
        'csrf',
        '_csrf_token',
        'csrf_token',
        'token',
        'captcha',
        'recaptcha_token',
        'submit',
        'save'
    }

    @staticmethod
    async def fetch_registration_form(session: aiohttp.ClientSession, base_url: str) -> dict[str, dict] | None:
        """
        Fetch registration form and extract all input field names, types, and metadata
        Uses provided aiohttp session to preserve cookies for subsequent requests
        Returns a mapping of field_name -> {type, required, options}
        """
        try:
            normalized_base = base_url.rstrip('/')
            registration_url = f"{normalized_base}/register"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
                'Referer': f"{normalized_base}/",
                'Sec-Ch-Ua': '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
                'Sec-Ch-Ua-Mobile': '?0',
                'Sec-Ch-Ua-Platform': '"Windows"',
                'Upgrade-Insecure-Requests': '1'
            }
            async with session.get(
                    registration_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                response.raise_for_status()
                html_content = await response.text()

            soup = BeautifulSoup(html_content, "html.parser")
            form_fields = {}

            for input_field in soup.find_all("input"):
                name = input_field.get("name")
                if name and name not in RegistrationHelper.IGNORED_FIELDS:
                    field_type = input_field.get("type", "text")
                    required = input_field.get("required") is not None
                    form_fields[name] = {
                        "type": field_type,
                        "required": required,
                        "checked": field_type == "checkbox" and input_field.get("checked") is not None
                    }
                    # print(f"[RegistrationHelper] Found input field: {name} (type: {field_type}, required: {required})")

            for textarea in soup.find_all("textarea"):
                name = textarea.get("name")
                if name and name not in RegistrationHelper.IGNORED_FIELDS:
                    required = textarea.get("required") is not None
                    form_fields[name] = {
                        "type": "textarea",
                        "required": required
                    }
                    print(f"[RegistrationHelper] Found textarea field: {name} (required: {required})")

            for select in soup.find_all("select"):
                name = select.get("name")
                if name and name not in RegistrationHelper.IGNORED_FIELDS:
                    required = select.get("required") is not None
                    options = [opt.get("value", opt.text) for opt in select.find_all("option")]
                    form_fields[name] = {
                        "type": "select",
                        "required": required,
                        "options": options
                    }
                    print(f"[RegistrationHelper] Found select field: {name} (required: {required}, options: {options})")

            print(f"\n[RegistrationHelper] Found {len(form_fields)} form fields: {list(form_fields.keys())}\n")
            return form_fields
        except Exception as e:
            print(f"[RegistrationHelper]{RED}Error fetching form: {e}{RESET}\n")
            return None

    @staticmethod
    def create_field_mapping() -> dict[str, callable]:
        """
        Create mapping between form field names and Registration generation methods
        Maps form field -> callable method from Registration class
        """
        mapping = {
            "email": Registration.email,
            "password": Registration.password,
            "FirstName": Registration.firstname,
            "LastName": Registration.lastname,
            "City": Registration.city,
            "State": Registration.state,
            "PostalCode": Registration.postal_code,
            "IdentificationNumber": Registration.identification_number,
            "CurrentAsset": Registration.current_asset,
            "Gender": Registration.gender,
            "Phone": Registration.phone,
            "Nationality": Registration.nationality,
            "StateCode": Registration.state_code,
            "DocumentType": Registration.document_type,
            "AskAgeConfirmation": Registration.ask_age_confirmation,
            "BirthDate": Registration.birth_date,
            "username": Registration.username,
            "StreetAddress": Registration.street_address,
            "DocumentNum": Registration.document_number,
            "PromoCode": Registration.promo_code,
            "PrivacyPolicyAndGDPRConsent": Registration.privacy_policy_and_gdpr_consent,
            "RegisteredGamblingAddictConsent": Registration.registered_gambling_addict_consent,
            "PoliticallyExposedPerson": Registration.politically_exposed_person,
            "MarketingConsent": Registration.marketing_consent,
            "TermConditions": Registration.term_conditions
        }
        return mapping

    @staticmethod
    def create_field_name_mapping() -> dict[str, str]:
        """
        Create string-to-string mapping of user_data field names to form field names
        Used for matching fields between generated user data and form structure
        """
        callable_mapping = RegistrationHelper.create_field_mapping()
        return {field_name: field_name for field_name in callable_mapping.keys()}

    @staticmethod
    def generate_user_data() -> Dict[str, str]:
        """
        Generate random user data by calling all Registration methods
        Returns dict with field_name -> generated_value
        """
        mapping = RegistrationHelper.create_field_mapping()
        user_data = {}

        for field_name, method in mapping.items():
            try:
                value = method()
                if value is not None:
                    user_data[field_name] = str(value)
            except Exception as e:
                print(f"[generate_user_data] {RED}Error generating {field_name}: {e}{RESET}")

        return user_data

    @staticmethod
    def generate_user_data_for_form(form_fields: dict[str, dict]) -> Dict[str, str]:
        """
        Generate random user data only for fields that exist in the form.
        This ensures we only generate data for fields that will actually be used.
        
        Args:
            form_fields: Dict of form fields from fetch_registration_form()
            
        Returns:
            Dict with field_name -> generated_value (only for form fields)
        """
        mapping = RegistrationHelper.create_field_mapping()
        user_data = {}

        for field_name, method in mapping.items():
            # Only generate data for fields present in the form
            if field_name not in form_fields:
                continue

            try:
                value = method()
                if value is not None:
                    user_data[field_name] = str(value)
            except Exception as e:
                print(f"[generate_user_data_for_form] {RED}Error generating {field_name}: {e}{RESET}")

        return user_data

    @staticmethod
    async def submit_form_via_api(
            session: aiohttp.ClientSession,
            submit_url: str,
            payload: Dict[str, str],
            email: str
    ) -> Dict[str, any]:
        """
        Submit form via POST API call with payload.
        Uses provided aiohttp session to preserve cookies from form fetch.
        Builds form-urlencoded data and submits via HTTP.
        Analyzes response for success indicators.
        
        Args:
            session: aiohttp.ClientSession with existing cookies
            submit_url: Full URL to submit form to
            payload: Form data payload to submit
            email: User email for logging
            
        Returns:
            Dict with submission result (success, status, response, message)
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
            # Convert payload to URL-encoded format
            form_data = urlencode(payload)
            print(f"\n[submit_form_via_api] ====== PAYLOAD DEBUG ======")
            print(f"[submit_form_via_api] Submitting {len(payload)} fields for {email}")
            print(f"[submit_form_via_api] URL: {submit_url}")
            print(f"[submit_form_via_api] Form data length: {len(form_data)} bytes")
            print(f"[submit_form_via_api] Payload fields:")
            for key, value in payload.items():
                value_preview = value if len(str(value)) <= 40 else f"{str(value)[:40]}..."
                print(f"  - {key}: {value_preview}")
            print(f"[submit_form_via_api] Form-encoded data:\n{form_data}\n")

            # Submit via POST with AJAX headers (as detected from browser)
            base_url = submit_url.split('/utility')[0]  # Extract https://stg.wild7.bet
            headers = {
                'Accept': '*/*',
                'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
                'Accept-Encoding': 'gzip, deflate, br, zstd',
                'Cache-Control': 'no-cache',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'Origin': base_url,
                'Referer': base_url + '/',
                'Pragma': 'no-cache',
                'Priority': 'u=1, i',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
                'X-Requested-With': 'XMLHttpRequest',
                'Sec-Ch-Ua': '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
                'Sec-Ch-Ua-Mobile': '?0',
                'Sec-Ch-Ua-Platform': '"Windows"',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin'
            }

            # print(f"[submit_form_via_api] DEBUG: POST headers:")
            # for k, v in headers.items():
            #     print(f"  {k}: {v}")

            # print(f"[submit_form_via_api] DEBUG: Session cookies:")
            # for cookie in session.cookie_jar:
            #     print(f"  {cookie.key}: {cookie.value}")

            async with session.post(
                    submit_url,
                    data=form_data,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                    allow_redirects=True
            ) as response:
                response_text = await response.text()
                result['status'] = response.status
                result['response'] = response_text

                # Analyze response for success
                success, message = RegistrationHelper._analyze_response(
                    response.status,
                    response_text,
                    email
                )

                result['success'] = success
                result['message'] = message

                status_indicator = "[+]" if success else "[-]"
                print(f"[submit_form_via_api] {status_indicator} Status: {response.status} - {message}")

                return result

        except aiohttp.ClientError as e:
            result['error'] = f"Connection error: {str(e)}"
            print(f"[submit_form_via_api] [-] {RED}Connection error: {str(e)}{RESET}")
            return result
        except Exception as e:
            result['error'] = f"Exception: {str(e)}"
            print(f"[submit_form_via_api] [-] {RED}Exception: {str(e)}{RESET}")
            return result

    @staticmethod
    def _analyze_response(status_code: int, response_text: str, email: str) -> tuple:
        """
        Analyze registration response to determine if registration was successful.
        Checks HTTP status and response content for success indicators.
        
        Returns:
            tuple of (success: bool, message: str)
        """
        # Check HTTP status codes
        if status_code in [200, 201]:
            # Try to parse JSON response for success indicators
            try:
                response_json = json.loads(response_text)

                # Check if "success" field exists (most reliable indicator)
                if "success" in response_json:
                    if response_json.get("success"):
                        message = response_json.get("message", f"{GREEN}Registration successful{RESET}")
                        return True, message
                    else:
                        # success: false - this is a registration failure
                        message = response_json.get("message", f"{RED}Registration failed{RESET}")
                        return False, message

                # Check other success/error indicators
                if response_json.get("status") == "success":
                    message = response_json.get("message", f"{GREEN}Registration successful{RESET}")
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

                return True, f"HTTP {status_code} - Likely successful"

            except json.JSONDecodeError:
                # Non-JSON response, check for success text patterns
                response_lower = response_text.lower()
                if any(keyword in response_lower for keyword in
                       ["success", "registration complete", "welcome", "account created"]):
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

            return False, f"{YELLOW}Bad request (HTTP 400){RESET}"

        elif status_code in [301, 302, 303, 307, 308]:
            return True, f"{GREEN}Redirect (HTTP {status_code}) - Likely successful redirect{RESET}"

        elif status_code >= 500:
            return False, f"{RED}Server error (HTTP {status_code}){RESET}"

        elif status_code >= 400:
            return False, f"{RED}Client error (HTTP {status_code}){RESET}"

        return False, f"{YELLOW}Unexpected status code: {status_code}{RESET}"


# Global state for registration test
registration_state = {
    'users_data': [],
    'user_index': 0,
    'field_mapping': {},
    'helper': RegistrationHelper(),
    'csv_paths': None,
    'csv_distribution_counter': 0,
    'form_fields': None
}

data_lock = None
form_fields_lock = None


def _get_frontoffice_base_url() -> str:
    fo_base = config.fo_base_url.strip()
    if not fo_base:
        raise RuntimeError("Frontoffice base URL is not configured")
    if fo_base.startswith("http://") or fo_base.startswith("https://"):
        normalized = fo_base
    else:
        normalized = f"https://{fo_base}"
    return normalized.rstrip('/')


def _init_success_csv_files() -> dict[str, Path]:
    csv_paths = {}
    for key in CSV_TARGET_ORDER:
        filename = CSV_TARGET_FILENAMES[key]
        path = USERS_DATA_DIR / filename
        _ensure_csv_schema(path)
        csv_paths[key] = path
    return csv_paths


def _ensure_csv_paths() -> dict[str, Path]:
    csv_paths = registration_state.get('csv_paths')
    if csv_paths:
        for path in csv_paths.values():
            _ensure_csv_schema(path)
        return csv_paths
    csv_paths = _init_success_csv_files()
    registration_state['csv_paths'] = csv_paths
    return csv_paths


def _write_success_csv(email: str, password: str | None):
    if not email:
        return
    csv_paths = _ensure_csv_paths()
    distribution_counter = registration_state.get('csv_distribution_counter', 0)
    target_key = 'login_exploring' if distribution_counter % 3 == 0 else 'login_logout'
    registration_state['csv_distribution_counter'] = distribution_counter + 1
    csv_path = csv_paths[target_key]
    with csv_path.open('a', newline='', encoding='utf-8') as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow([email, password or "", ""])
    print(f"[registration_scenario] Stored credentials for {email} at {csv_path}")


async def _get_next_registration_user() -> Dict[str, str]:
    global data_lock
    if data_lock is None:
        data_lock = asyncio.Lock()
    async with data_lock:
        if registration_state['user_index'] >= len(registration_state['users_data']):
            new_user = registration_state['helper'].generate_user_data()
            registration_state['users_data'].append(new_user)
        user_data = registration_state['users_data'][registration_state['user_index']]
        registration_state['user_index'] += 1
    return dict(user_data)


async def _prime_registration_session(session: aiohttp.ClientSession, base_url: str):
    register_url = f"{base_url.rstrip('/')}/register"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
        'Referer': f"{base_url.rstrip('/')}/",
        'Upgrade-Insecure-Requests': '1'
    }
    try:
        async with session.get(
                register_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
        ) as response:
            await response.read()
    except Exception as e:
        print(f"[registration_scenario] {YELLOW}WARNING: Could not prime registration session: {e}{RESET}")


async def _get_cached_form_fields(helper: RegistrationHelper, session: aiohttp.ClientSession, base_url: str) -> Dict[
    str, dict]:
    global form_fields_lock
    if registration_state['form_fields'] is None:
        if form_fields_lock is None:
            form_fields_lock = asyncio.Lock()
        async with form_fields_lock:
            if registration_state['form_fields'] is None:
                print("[registration_scenario] Fetching registration form for cache...")
                form_fields = await helper.fetch_registration_form(session, base_url)
                if not form_fields:
                    raise RuntimeError("Failed to fetch registration form")
                registration_state['form_fields'] = form_fields
                return deepcopy(form_fields)
    await _prime_registration_session(session, base_url)
    return deepcopy(registration_state['form_fields'])


@global_setup()
def setup_registration(args):
    """Initialize registration test with randomly generated user data"""
    global form_fields_lock
    metrics_port = resolve_metrics_port('REGISTRATION_METRICS_PORT')
    try:
        start_http_server(metrics_port)
        print(f"[setup_registration] Prometheus metrics server started on port {metrics_port}")
    except Exception as e:
        print(f"[setup_registration] {YELLOW}Warning: Could not start Prometheus server: {e}{RESET}")

    registration_state['helper'] = RegistrationHelper()

    users = []
    for i in range(100):
        user_data = registration_state['helper'].generate_user_data()
        users.append(user_data)
    registration_state['users_data'] = users

    registration_state['user_index'] = 0
    registration_state['field_mapping'] = registration_state['helper'].create_field_name_mapping()
    registration_state['csv_paths'] = _init_success_csv_files()
    registration_state['csv_distribution_counter'] = 0
    registration_state['form_fields'] = None
    form_fields_lock = None

    print(
        f"[setup_registration] Generated {len(registration_state['users_data'])} random users for registration testing")


@global_teardown()
def teardown_registration():
    """Cleanup after registration test"""
    print(f"[teardown_registration] {GREEN}Registration tests completed{RESET}")
    print("\n" + "=" * 70)
    print(f"STATISTIC SUMMARY - REGISTRATION SCENARIO")
    print("=" * 70)
    print(f"[teardown_registration] Total workers: {global_stats['total_workers']}")
    print(f"[teardown_registration] Total registrations: {global_stats['total_registrations']}, "
          f"{GREEN}Success: {global_stats['successful_registrations']}{RESET}, "
          f"{RED}Failures: {global_stats['failed_registrations']}{RESET}, "
          f"{RED}Setup errors: {global_stats['setup_errors']}{RESET}")

    metrics_dir = Path(__file__).parent.parent / 'metrics'
    metrics_dir.mkdir(parents=True, exist_ok=True)

    metrics_file = metrics_dir / 'registration_metrics.json'
    metrics_data = {
        'total_registrations': global_stats['total_registrations'],
        'successful_registrations': global_stats['successful_registrations'],
        'failed_registrations': global_stats['failed_registrations'],
        'setup_errors': global_stats['setup_errors'],
    }

    try:
        with open(metrics_file, 'w') as f:
            json.dump(metrics_data, f, indent=2)
        print(f"[teardown_registration] Metrics exported to {metrics_file}")
    except Exception as e:
        print(f"[teardown_registration] {RED}ERROR exporting metrics: {e}{RESET}")


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
    print(f"[setup_worker {worker_id}] Worker initialized (Total workers: {global_stats['total_workers']})")


@scenario()
async def registration_scenario(session):
    """
    Main registration scenario:
    1. Fetch registration form to analyze available fields
    2. Get next user from generated data
    3. Build form payload with field type handling
    4. Submit registration via POST API
    5. Analyze response to verify success
    """
    if not registration_state['users_data']:
        print(f"[registration_scenario] {RED}ERROR: No users data available{RESET}")
        raise RuntimeError(f"{RED}No users data available{RESET}")

    worker_id = session.worker_id if hasattr(session, 'worker_id') else 'unknown'
    helper = registration_state['helper']
    fo_base_url = _get_frontoffice_base_url()
    start_time = time.time()
    status = 'failed'

    try:
        # Step 1: Get next user from generated data (before network operations)
        print("[registration_scenario] >>> STEP 1: Loading user data...")
        user_data = await _get_next_registration_user()
        email = user_data.get('email', '')
        print(f"[registration_scenario] User: {email}")

        async with aiohttp.ClientSession() as aio_session:
            # Step 2: Load (or fetch once) registration form metadata
            print("[registration_scenario] >>> STEP 2: Loading cached registration form...")
            try:
                form_fields = await _get_cached_form_fields(helper, aio_session, fo_base_url)
            except Exception:
                print(f"[registration_scenario] {RED}ERROR: Could not fetch registration form{RESET}")
                prometheus_metrics['registration_errors'].labels(worker=worker_id, error_type='form_fetch_failed').inc()
                raise

            print(f"[registration_scenario] Using {len(form_fields)} cached form fields")

            # Step 3: Build form payload with type-aware handling
            print("[registration_scenario] >>> STEP 3: Building form payload...")
            payload = _build_registration_request(
                user_data,
                form_fields,
                registration_state['field_mapping'],
                helper
            )

            if not payload:
                print(f"[registration_scenario] {RED}ERROR: No data mapped for {email}{RESET}")
                prometheus_metrics['registration_errors'].labels(worker=worker_id,
                                                                 error_type='payload_build_failed').inc()
                raise RuntimeError(f"No data mapped for {email}")

            print(f"[registration_scenario] Built payload with {len(payload)} fields")

            # Step 4: Submit registration via API
            print("[registration_scenario] >>> STEP 4: Submitting registration...")
            register_submit_path = config.fo_url['register_submit']
            if not register_submit_path.startswith('/'):
                register_submit_path = f"/{register_submit_path}"
            registration_url = f"{fo_base_url}{register_submit_path}"

            submit_result = await helper.submit_form_via_api(aio_session, registration_url, payload, email)

        # Step 5: Check result
        print("\n[registration_scenario] >>> STEP 5: Analyzing response...")
        if submit_result['success']:
            _write_success_csv(email, user_data.get('password'))
            print(f"\n[registration_scenario] {GREEN}✓ REGISTRATION SUCCESSFUL for {email}{RESET}")
            print(f"[registration_scenario] Response: {submit_result['message']}")
            global_stats['successful_registrations'] += 1
            prometheus_metrics['successful_registrations'].inc()
            status = 'success'
        else:
            error_message = submit_result['message'] or submit_result['error'] or "Unknown error"
            response_preview = (submit_result['response'][:300] if submit_result['response'] else None)
            duplicate_email = 'email already exists' in error_message.lower()

            if duplicate_email:
                print(f"[registration_scenario] {YELLOW}⚠ DUPLICATE EMAIL for {email}: {error_message}{RESET}")
            else:
                print(f"[registration_scenario] {RED}✗ REGISTRATION FAILED for {email}{RESET}")
                print(f"[registration_scenario] Error: {error_message}")

            if response_preview:
                print(f"[registration_scenario] Response preview: {response_preview}")

            global_stats['failed_registrations'] += 1
            prometheus_metrics['failed_registrations'].inc()
            error_label = 'duplicate_email' if duplicate_email else 'registration_failed'
            prometheus_metrics['registration_errors'].labels(worker=worker_id, error_type=error_label).inc()
            status = 'duplicate' if duplicate_email else 'failed'

            if not duplicate_email:
                raise RuntimeError(f"{RED}Registration failed: {error_message}{RESET}")
            return
    except Exception as e:
        if status == 'failed':
            raise
        print(f"[registration_scenario] Exception: {str(e)}")
        prometheus_metrics['registration_errors'].labels(worker=worker_id, error_type='exception').inc()
        raise
    finally:
        global_stats['total_registrations'] += 1
        prometheus_metrics['total_registrations'].inc()
        elapsed = time.time() - start_time
        prometheus_metrics['registration_latency'].labels(worker=worker_id, status=status).observe(elapsed)


def _build_registration_request(
        user_data: Dict[str, str],
        form_fields: Dict[str, dict],
        field_mapping: Dict[str, str],
        helper: RegistrationHelper
) -> Dict[str, str]:
    """
    Dynamically build registration request based on form fields and their types.
    Handles different field types (text, checkbox, select, textarea) appropriately.
    """
    registration_data = {}

    print(f"\n[_build_registration_request] ====== BUILDING PAYLOAD ======")
    print(f"[_build_registration_request] Available form fields: {list(form_fields.keys())}")
    print(f"[_build_registration_request] Required form fields:")
    for fname, finfo in form_fields.items():
        if finfo.get("required"):
            print(f"  - {fname} (type: {finfo.get('type')}, required: True)")

    for field_name, form_field in field_mapping.items():
        # Skip ignored fields
        if form_field in helper.IGNORED_FIELDS:
            # print(f"[_build_registration_request] SKIP {form_field}: in IGNORED_FIELDS")
            continue

        # Skip fields not in the form
        if form_field not in form_fields:
            # print(f"[_build_registration_request] SKIP {form_field}: not in form_fields")
            continue

        # Skip if field not in user data
        if field_name not in user_data:
            # print(f"[_build_registration_request] SKIP {form_field}: field '{field_name}' not in user_data")
            continue

        value = user_data[field_name]
        if not value:
            # print(f"[_build_registration_request] SKIP {form_field}: empty value from user data")
            continue

        field_info = form_fields[form_field]
        field_type = field_info.get("type", "text")

        # Handle different field types
        if field_type == "checkbox":
            # For checkboxes: submit only if value indicates true/checked
            if value.lower() in ["true", "yes", "1", "on", "checked"]:
                registration_data[form_field] = "on"
            print(
                f"[_build_registration_request] Checkbox {form_field}: {value} -> {'on' if form_field in registration_data else 'skipped'}")

        elif field_type == "select":
            # For select fields: use value if in options, otherwise pick first non-empty option
            options = field_info.get("options", [])
            selected_value = value

            if options and value not in options:
                # Pick first non-empty option as fallback
                non_empty_options = [opt for opt in options if opt and opt.strip()]
                if non_empty_options:
                    selected_value = non_empty_options[0]
                    print(
                        f"[_build_registration_request] Select {form_field}: value '{value}' not in options, using '{selected_value}'")
                else:
                    # No non-empty options, use empty or skip
                    if options and options[0] == '':
                        selected_value = ''
                    else:
                        print(
                            f"[_build_registration_request] {YELLOW}Select {form_field}: no valid options available, skipping{RESET}")
                        continue

            registration_data[form_field] = selected_value
            print(f"[_build_registration_request] Select {form_field}: {selected_value}")

        elif field_type == "radio":
            # For radio buttons: add if value exists
            registration_data[form_field] = value
            print(f"[_build_registration_request] Radio {form_field}: {value}")

        else:
            # For text, email, password, textarea, etc.
            registration_data[form_field] = value
            print(
                f"[_build_registration_request] {field_type.capitalize()} {form_field}: {'*' * len(value) if field_type == 'password' else value[:30]}")

    print(f"\n[_build_registration_request] ====== PAYLOAD SUMMARY ======")
    print(f"[_build_registration_request] Total fields in payload: {len(registration_data)}")
    print(f"[_build_registration_request] Payload fields: {list(registration_data.keys())}")

    missing_required = []
    for fname, finfo in form_fields.items():
        if finfo.get("required") and fname not in registration_data:
            missing_required.append(fname)

    if missing_required:
        print(f"[_build_registration_request] {RED}MISSING REQUIRED FIELDS: {missing_required}{RESET}")
    else:
        print(f"[_build_registration_request] [+] All required fields present")

    print()
    return registration_data


async def test_registration_single_user():
    """Test registration for a single user - for manual testing"""
    print("\n" + "=" * 70)
    print("SINGLE USER REGISTRATION")
    print("=" * 70 + "\n")

    helper = RegistrationHelper()
    mapping = helper.create_field_name_mapping()
    fo_base_url = _get_frontoffice_base_url()

    # Create persistent session to preserve cookies across requests
    async with aiohttp.ClientSession() as session:
        # Step 1: Analyze form (GET request with cookies)
        print("[test_registration_single_user] >>> STEP 1: Analyzing form structure...\n")
        form_fields = await helper.fetch_registration_form(session, fo_base_url)
        if not form_fields:
            print(f"[test_registration_single_user] {RED}ERROR: Could not fetch form{RESET}")
            return

        print(f"[test_registration_single_user] Form fields detected: {list(form_fields.keys())}\n")

        # Step 2: Generate user data only for fields in the form
        print("[test_registration_single_user] >>> STEP 2: Generating user data for form fields...")
        user_data = helper.generate_user_data_for_form(form_fields)

        if not user_data:
            print(f"[test_registration_single_user] {RED}ERROR: Could not generate user data{RESET}")
            return

        email = user_data.get('email', 'unknown')
        print(f"[test_registration_single_user] Testing with user: {email}\n")

        # Step 3: Build payload
        print("[test_registration_single_user] >>> STEP 3: Building payload...")
        payload = _build_registration_request(user_data, form_fields, mapping, helper)

        if not payload:
            print(f"[test_registration_single_user] {RED}ERROR: Could not build payload{RESET}")
            return

        print(f"[test_registration_single_user] Payload fields: {list(payload.keys())}\n")

        # Step 4: Submit registration (POST request with same session/cookies)
        print("[test_registration_single_user] >>> STEP 4: Submitting registration...")
        register_submit_path = config.fo_url['register_submit']
        if not register_submit_path.startswith('/'):
            register_submit_path = f"/{register_submit_path}"
        registration_url = f"{fo_base_url}{register_submit_path}"
        submit_result = await helper.submit_form_via_api(session, registration_url, payload, email)

    # Step 5: Display result
    print(f"\n[test_registration_single_user] >>> STEP 5: Registration Result")
    print("=" * 70)
    print(f"User Email: {email}")
    print(f"HTTP Status: {submit_result['status']}")
    print(f"Success: {'[+] YES' if submit_result['success'] else '[-] NO'}")
    print(f"Message: {submit_result['message']}")
    if submit_result['success']:
        _write_success_csv(email, user_data.get('password'))
    if submit_result['error']:
        print(f"Error: {RED}{submit_result['error']}{RESET}")

    print("\n[test_registration_single_user] Full Response:")
    print("-" * 70)
    if submit_result['response']:
        resp_preview = submit_result['response'][:500]
        print(resp_preview)
        if len(submit_result['response']) > 500:
            print(f"... (truncated, total length: {len(submit_result['response'])} bytes)")
    print("=" * 70 + "\n")

    return submit_result


if __name__ == "__main__":
    asyncio.run(test_registration_single_user())
