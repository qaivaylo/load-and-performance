import os

DEFAULT_METRICS_PORT = 8000


def resolve_metrics_port(env_key: str | None = None, default: int = DEFAULT_METRICS_PORT) -> int:
    candidates: list[str | None] = []
    if env_key:
        candidates.append(os.environ.get(env_key))
    candidates.append(os.environ.get('PROMETHEUS_METRICS_PORT'))

    for value in candidates:
        if not value:
            continue
        try:
            return int(value)
        except ValueError:
            continue
    return default
