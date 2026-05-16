# Fortuna Load and Performance

A load testing tool for the casino platform that simulates multiple players making deposits and placing bets. Built with **Molotov** (async load testing), **Prometheus** + **Grafana** for real-time monitoring, and **Docker** for containerization.

## Features

- Dynamic player and bet count configuration
- Automatic user ID validation with retry logic
- Token generation for each player session
- Concurrent async request handling
- Staggered player start times (5-second intervals)
- Optional pipeline duration limit (in minutes)
- **Real-time metrics collection** with Prometheus
- **Interactive Grafana dashboard** with 8 monitoring panels
- **Bet latency tracking** (p95, p50, min, max)
- Docker containerization with service orchestration
- GitLab CI/CD pipeline integration
- Persistent metrics server after test completion

## Local Setup

### Requirements

- Python 3.13+
- pip or poetry

### Installation

1. Create and activate virtual environment:
```bash
python -m venv .venv
.venv\Scripts\activate  # Windows
source .venv/bin/activate  # macOS/Linux
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

### Running Locally

```bash
# Run with default configuration (5 bet, 10 registration, 20 exploring, 30 logout workers)
python main.py

# Run with custom worker counts for 5 minutes
BET_WORKERS=10 REGISTRATION_WORKERS=20 LOGIN_EXPLORING_WORKERS=30 LOGIN_LOGOUT_WORKERS=40 python main.py

# Run with custom configuration and 10 minute duration
BET_WORKERS=10 REGISTRATION_WORKERS=20 LOGIN_EXPLORING_WORKERS=30 LOGIN_LOGOUT_WORKERS=40 DURATION=10 python main.py
```

**Environment Variables:**
- `BET_WORKERS`: Number of workers for bet scenario (default: 5)
- `REGISTRATION_WORKERS`: Number of workers for registration scenario (default: 10)
- `LOGIN_EXPLORING_WORKERS`: Number of workers for login and exploring scenario (default: 20)
- `LOGIN_LOGOUT_WORKERS`: Number of workers for login and logout scenario (default: 30)
- `NUM_BETS`: Number of bets per worker (default: 20)
- `DURATION`: Pipeline duration in minutes (default: 5)
- `BASE_URL`: Target environment base URL (default: qa.client-env.com)

**Behavior:**
- Each scenario's workers start at 5-second intervals (Worker 1 starts immediately, Worker 2 after 5s, Worker 3 after 10s, etc.)
- If `DURATION` is specified, all scenarios stop after that time has elapsed
- If `DURATION` is not specified, the default is 5 minutes

## Docker

### Build Image

```bash
docker build -t fortuna-bot .
```

### Run Container

```bash
# Run with default configuration
docker run --rm fortuna-bot

# Run with custom worker counts
docker run --rm \
  -e BET_WORKERS=10 \
  -e REGISTRATION_WORKERS=20 \
  -e LOGIN_EXPLORING_WORKERS=30 \
  -e LOGIN_LOGOUT_WORKERS=40 \
  fortuna-bot

# Run with custom configuration and 10 minute duration
docker run --rm \
  -e BET_WORKERS=10 \
  -e REGISTRATION_WORKERS=20 \
  -e LOGIN_EXPLORING_WORKERS=30 \
  -e LOGIN_LOGOUT_WORKERS=40 \
  -e DURATION=10 \
  fortuna-bot
```

#### Run a single scenario container

Start scenarios independently (registration must run first so CSV creds exist). Mount `data/users` so all containers share the same `tmp_users.csv`:

```bash
# Registration
docker run --rm --name fortuna-registration \
  -e SCENARIO=registration \
  -v $(pwd)/data/users:/app/data/users \
  fortuna-bot

# Login + Exploring
docker run --rm --name fortuna-login-exploring \
  -e SCENARIO=login_exploring \
  -v $(pwd)/data/users:/app/data/users \
  fortuna-bot

# Login + Logout
docker run --rm --name fortuna-login-logout \
  -e SCENARIO=login_logout \
  -v $(pwd)/data/users:/app/data/users \
  fortuna-bot
```

Run each command in its own terminal (or append `&`) to keep containers alive simultaneously. Use `docker logs -f <container_name>` to follow per-scenario output.

## Monitoring with Grafana & Prometheus

The project includes integrated **Prometheus** + **Grafana** stack for real-time monitoring.

### Quick Start

1. **Create the Grafana API token file** (Prometheus mounts it at runtime):
```bash
mkdir -p secrets
printf '%s' "<YOUR_GRAFANA_CLOUD_API_KEY>" > secrets/grafana_api_token.txt
```
   - The token must have at least the `metrics:write` scope.
   - Keep the file out of source control (already covered by `.gitignore`).

2. **Start the stack:**
```bash
docker-compose up -d
```

This launches:
- **Grafana Cloud**: https://clientplatform.grafana.net/ (Grafana Cloud login)
- **Prometheus remote write**: https://prometheus-prod-65-prod-eu-west-2.grafana.net/api/prom/push
- **Fortuna Bot metrics**: http://host.docker.internal:9103/metrics

3. **Access Grafana Dashboard:**
   - Navigate to https://clientplatform.grafana.net/
   - Sign in with your Grafana Cloud account
   - Dashboard "Fortuna Bot Dashboard" is auto-provisioned

### Grafana Cloud Remote Write

Prometheus already contains a `remote_write` block that ships metrics to Grafana Cloud. Supply valid credentials to keep pushes healthy:
1. In Grafana Cloud open **Security → Access Policies**, ensure the policy used by this stack includes `metrics:write`, and generate a token (format `glc_...`).
2. Store the token in `secrets/grafana_api_token.txt` (single line, no newline).
3. From the same stack page copy the **Instance ID** and **Remote write endpoint** shown under **Prometheus → Send Metrics**. These values are already reflected in `prometheus.yml`; update them only if your stack changes.
4. Restart Prometheus after rotating the token:
```bash
docker-compose up -d prometheus
```
5. Tail logs via `docker-compose logs -f prometheus` to confirm `remote_write` batches succeed.
6. Import `grafana/provisioning/dashboards/fortuna-dashboard.json` into Grafana Cloud (Dashboards → New → Import) to reuse the local dashboard.

### Monitoring Features

**Real-time Metrics:**
- Total number of bots (workers)
- Total bets placed (successful & failed)
- Bets distribution by game
- Setup errors and worker status
- Success rate and performance metrics
- Execution time tracking

**Available Endpoints:**
- `/metrics` - Prometheus format metrics
- `/stats` - JSON format statistics
- `/` - Redirects to Prometheus/Grafana

### Dashboard Panels

1. **Total Bets Over Time** - Line chart showing cumulative bet count over time (updates every 5s)
2. **Number of Workers** - Gauge showing total initialized workers with status indicators
3. **Successful vs Failed Bets** - Time series comparing successful (green) and failed (red) bets
4. **Bets per Game** - Line chart showing bet distribution across different games
5. **Success Rate** - Gauge showing success percentage (red: 0-49%, yellow: 50-79%, green: 80%+)
6. **Setup Errors** - Gauge showing worker setup failures (green: 0, yellow: ≥1, red: ≥5)
7. **Bet Latency Per Worker** - Histogram showing p95 latency with min/max/mean per game and status

### Environment Variables

Configure the bot via environment variables in `docker-compose.yml`:

| Variable | Description | Default | Example |
|----------|-------------|---------|---------|
| `BET_WORKERS` | Number of workers for bet scenario | `5` | `10`, `50`, `100` |
| `REGISTRATION_WORKERS` | Number of workers for registration scenario | `10` | `10`, `20`, `50` |
| `LOGIN_EXPLORING_WORKERS` | Number of workers for login and exploring scenario | `20` | `20`, `50`, `100` |
| `LOGIN_LOGOUT_WORKERS` | Number of workers for login and logout scenario | `30` | `30`, `50`, `100` |
| `NUM_BETS` | Number of bets per worker | `20` | `50`, `100`, `500` |
| `DURATION` | Test duration in minutes | `5` | `1`, `10`, `30` |
| `BASE_URL` | Target environment base URL | `qa.client-env.com` | `qa.client-env.com`, `stg.client-env.com` |

**Example docker-compose.yml configuration:**

```yaml
environment:
  - BET_WORKERS=5                    # Run 5 bet scenario workers
  - REGISTRATION_WORKERS=10          # Run 10 registration workers
  - LOGIN_EXPLORING_WORKERS=20       # Run 20 login & exploring workers
  - LOGIN_LOGOUT_WORKERS=30          # Run 30 login & logout workers
  - NUM_BETS=50                      # Each worker places 50 bets
  - DURATION=5                       # Test runs for 5 minutes
  - BASE_URL=qa.gaming-ent.com       # Target the QA environment
```

### Test Execution Flow

1. **Setup Phase** (per worker):
   - Login to the platform and retrieve authentication cookies
   - Fetch available games and select one randomly
   - Attempt deposit for a random user ID (retries up to 10 times with different user IDs)

2. **Betting Phase** (concurrent):
   - Each worker generates a random token
   - Places bets concurrently on assigned games
   - Measures response latency for each bet
   - Collects success/failure metrics

3. **Monitoring**:
   - Metrics are collected in real-time during test execution
   - After test completion, metrics server stays alive for 60+ seconds to allow Prometheus scraping
   - Metrics remain accessible at `http://host.docker.internal:9103/metrics` and `/stats`

### Query Metrics Directly

**Prometheus format:**
```bash
curl http://host.docker.internal:9103/metrics
```

**JSON format:**
```bash
curl http://host.docker.internal:9103/stats
```

### Cleanup

Stop the stack:
```bash
docker-compose down
```

Reset all data:
```bash
docker-compose down -v
```

### Available Metrics

**Counter Metrics** (cumulative, always increase):

| Metric | Labels | Description |
|--------|--------|-------------|
| `fortuna_total_bets` | — | Total bets placed |
| `fortuna_successful_bets` | — | Total successful bets |
| `fortuna_failed_bets` | — | Total failed bets |
| `fortuna_setup_errors` | — | Total worker setup errors |
| `fortuna_bets_per_game` | `game` | Bets per game ID |
| `fortuna_success_per_game` | `game` | Successful bets per game |
| `fortuna_failure_per_game` | `game` | Failed bets per game |

**Gauge Metrics** (current value):

| Metric | Description |
|--------|-------------|
| `fortuna_total_workers` | Total initialized workers |

**Histogram Metrics** (latency tracking):

| Metric | Labels | Description |
|--------|--------|-------------|
| `fortuna_bet_latency_seconds` | `game`, `status` | Bet response latency (buckets: 0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0 seconds) |

For detailed monitoring configuration and troubleshooting, see [MONITORING.md](MONITORING.md).

## GitLab CI/CD Setup

### Configuration

**Optional variables:**

**Project Settings → CI/CD → Variables**

| Variable | Description | Example | Default |
|----------|-------------|---------|---------|
| `BET_WORKERS` | Number of workers for bet scenario | `10` | `5` |
| `REGISTRATION_WORKERS` | Number of workers for registration scenario | `10` | `10` |
| `LOGIN_EXPLORING_WORKERS` | Number of workers for login and exploring scenario | `20` | `20` |
| `LOGIN_LOGOUT_WORKERS` | Number of workers for login and logout scenario | `30` | `30` |
| `NUM_BETS` | Number of bets per worker | `50` | `20` |
| `DURATION` | Pipeline duration in minutes | `5` | `5` |
| `CI_REGISTRY_IMAGE` | Container registry image path | Auto-set by GitLab | No (auto) |

**Note:** All worker count variables are optional and have sensible defaults. `DURATION` is also optional.

### Pipeline Flow

1. **Build Stage** (automatic)
   - Builds Docker image
   - Pushes to GitLab Container Registry

2. **Run Stage** (manual trigger)
   - Runs the bot with configured `NUM_USERS` and `NUM_BETS`

### Running Pipeline

1. Push code to `main` or create a merge request
2. Go to **CI/CD → Pipelines**
3. Wait for build stage to complete
4. Click **Run** on the `run_bot` job
5. Override variables if needed (optional):
   - `BET_WORKERS`: Number of bet scenario workers (default: 5)
   - `REGISTRATION_WORKERS`: Number of registration workers (default: 10)
   - `LOGIN_EXPLORING_WORKERS`: Number of login & exploring workers (default: 20)
   - `LOGIN_LOGOUT_WORKERS`: Number of login & logout workers (default: 30)
   - `NUM_BETS`: Number of bets per worker (default: 20)
   - `DURATION`: Pipeline duration in minutes (default: 5)
6. Click **Run pipeline**

### Example: Run with custom worker counts for 10 minutes

1. Navigate to the manual job `run_bot`
2. Set `BET_WORKERS = 10`
3. Set `REGISTRATION_WORKERS = 20`
4. Set `LOGIN_EXPLORING_WORKERS = 30`
5. Set `LOGIN_LOGOUT_WORKERS = 40`
6. Set `NUM_BETS = 100`
7. Set `DURATION = 10`
8. Trigger pipeline

### Example: Run with default configuration

1. Navigate to the manual job `run_bot`
2. Leave all variables at their default values
3. Trigger pipeline (will use: 5 bet, 10 registration, 20 exploring, 30 logout workers for 5 minutes)

## Configuration Files

### config/config.py

Contains API endpoints, credentials, games, and deposit settings. Endpoints are dynamically built from `BASE_URL` environment variable:

**API Endpoints** (auto-generated from BASE_URL):

```python
base_url = os.getenv('BASE_URL', 'stg.client-url.com')

url = {
    'url': f'https://{base_url}',
    'login': f'https://backoffice.{base_url}/login/',
    'bet': f'https://backoffice.{base_url}/admin/management/test/casino-bet/',
    'deposit': f'https://backoffice.{base_url}/admin/management/test/deposit/',
    'games': f'https://backoffice.{base_url}/admin/game/search/'
}
```

**Authentication Credentials**:

```python
creds = {
    '_username': '**@client-env.com',
    '_password': '*********'
}
```

**Deposit Configuration** (applied to each user during setup):

```python
deposit = {
    'user_id': 1,          # Dynamically set per worker
    'amount_coin': 100,    # Deposit amount
    'payment_method': '',
    'currency': 5
}
```

**Game Configuration** (randomly selected per worker):

```python
game_name = [
    "2Wild2Die - Hacksaw Gaming",
    "Cash Crew - Hacksaw Gaming",
    "Dawn of Kings - Hacksaw Gaming",
    "Duel at Dawn - Hacksaw Gaming"
]
```

Each worker randomly selects one game from this list during the setup phase. Only modify this if you want to add/remove games for testing.

## Technology Stack

- **Load Testing**: [Molotov](https://molotov.readthedocs.io/) - Async load testing framework with concurrency support
- **Metrics**: [Prometheus Client](https://github.com/prometheus/client_python) - Metrics collection and exposure
- **Monitoring**: [Prometheus](https://prometheus.io/) + [Grafana](https://grafana.com/) - Time-series metrics and dashboarding
- **HTTP Client**: [aiohttp](https://docs.aiohttp.org/) - Async HTTP client for concurrent requests
- **Containerization**: Docker + Docker Compose - Service orchestration
- **CI/CD**: GitLab CI/CD - Pipeline automation
- **Runtime**: Python 3.13+ with async/await support

## How It Works

1. **Login**: Authenticates with the platform
2. **Player Generation**: Creates N random player IDs (1-500)
3. **Staggered Start**: Players start at 5-second intervals
   - Player 1 starts immediately
   - Player 2 starts after 5 seconds
   - Player 3 starts after 10 seconds, etc.
4. **Deposit Phase** (for each player): 
   - Attempts deposit for each player
   - Retries with new user ID if deposit fails
   - Validates success response
5. **Bet Phase**: Places M concurrent bets for each valid player
6. **Duration Control**: If duration is specified, all players stop after that time
7. **Logging**: Prints status for each operation

## Troubleshooting

### "Player ID was not found!"
- This is normal - the bot will automatically retry with a new player ID
- If retries exceed 5 attempts, the player task exits

### Connection errors
- Verify API endpoints in `config/config.py`
- Check network connectivity to the target server
- Ensure credentials in config are correct

### Docker build fails
- Verify Python 3.13 is available: `docker --version`
- Check pip cache: `docker build --no-cache -t fortuna-bot .`

## Project Structure

```
fortuna-load/
├── config/
│   ├── __init__.py
│   └── config.py                          # API endpoints and settings
├── src/
│   ├── __init__.py
│   └── app.py                             # Molotov entrypoint
├── scenarios/
│   ├── __init__.py
│   ├── bet_scenario.py                    # Bet scenario with metrics
│   ├── login_and_exploring_scenario.py    # Login + exploring scenario
│   ├── login_logout_scenario.py           # Login + logout scenario
│   ├── registration_scenario.py           # User registration scenario
│   └── utils/
│       ├── __init__.py
│       ├── bet_request.py                 # Bet payload generation
│       ├── metrics.py                     # Metrics helpers
│       ├── registration_helper.py         # Registration helpers
│       └── user_manager.py                # User file management
├── data/
│   └── users/                             # Shared user CSVs
├── scripts/
│   ├── run_scenarios.sh                   # Run registered scenarios
│   └── start_monitoring_stack.sh          # Start Prometheus + Grafana locally
├── docker-compose.yml                     # Prometheus + Grafana + Bot stack
├── prometheus.yml                         # Prometheus configuration
├── requirements.txt                       # Python dependencies
├── Dockerfile                             # Docker build
├── .gitlab-ci.yml                         # GitLab CI/CD pipeline
├── Makefile                               # Utility targets
├── main.py                                # Scenario orchestrator
└── README.md                              # This file
```
