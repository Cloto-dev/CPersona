"""Common argument validation helpers for MCP tool handlers.

All validators return a safe default on type mismatch (graceful degradation).
"""


def validate_bool(arguments: dict, key: str, default: bool = False) -> bool:
    """Extract a boolean value, returning *default* if missing or wrong type."""
    val = arguments.get(key, default)
    if not isinstance(val, bool):
        return default
    return val


def validate_str(arguments: dict, key: str, default: str = "") -> str:
    """Extract a string value, returning *default* if missing or wrong type."""
    val = arguments.get(key, default)
    if not isinstance(val, str):
        return default
    return val


def validate_int(arguments: dict, key: str, default: int = 0) -> int:
    """Extract an integer value, returning *default* if missing or wrong type.

    ``bool`` is explicitly excluded (``isinstance(True, int)`` is ``True``).
    """
    val = arguments.get(key, default)
    if isinstance(val, bool) or not isinstance(val, int):
        return default
    return val


def validate_dict(arguments: dict, key: str, default: dict | None = None) -> dict:
    """Extract a dict value, returning *default* (or ``{}``) if missing or wrong type."""
    if default is None:
        default = {}
    val = arguments.get(key, default)
    if not isinstance(val, dict):
        return default
    return val


def validate_list(arguments: dict, key: str, default: list | None = None) -> list:
    """Extract a list value, returning *default* (or ``[]``) if missing or wrong type."""
    if default is None:
        default = []
    val = arguments.get(key, default)
    if not isinstance(val, list):
        return default
    return val
