"""Exception types. Messages must never contain secrets or untrusted output verbatim."""


class AeoError(Exception):
    """Base class for all aeo errors."""


class ValidationError(AeoError):
    """Untrusted input (spec, asset tree, verifier output, CLI argument) was rejected."""


class UnsupportedExecutionError(AeoError):
    """The input is understood as metadata, but aeo cannot execute it."""


class DockerUnavailableError(AeoError):
    """The docker CLI or daemon could not be reached."""
