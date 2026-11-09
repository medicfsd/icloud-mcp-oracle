"""Errors that are safe to show to the model. Never put a secret in one of these."""


class ServiceError(Exception):
    """A failure the tool should report in its own words."""


class ConfigError(Exception):
    """The env file is missing something, or a value is not usable."""


def redact(text: str, secrets: list[str]) -> str:
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned
