"""Small helpers for reading typed settings from the environment.

Every helper raises ConfigError with the variable name, so a wrong value stops
the application at start-up with a clear message instead of failing later.
"""
import os


class ConfigError(RuntimeError):
    """A setting is missing or has an invalid value."""


class Unavailable(RuntimeError):
    """A backend (vCenter, Kubernetes, a CRD) is not configured or not present.

    The message is written by this application and is safe to show to clients.
    """


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_bool(name: str, default: bool) -> bool:
    raw = env_str(name).lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _env_number(name: str, default, minimum, cast):
    raw = env_str(name)
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {raw!r}")
    return value


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    return _env_number(name, default, minimum, int)


def env_float(name: str, default: float, minimum: float | None = None) -> float:
    return _env_number(name, default, minimum, float)


def env_list(name: str) -> list[str]:
    return [item.strip() for item in env_str(name).split(",") if item.strip()]
