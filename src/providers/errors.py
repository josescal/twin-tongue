"""Errors shared by all external provider integrations."""


class ProviderError(RuntimeError):
    """Base error for speech, translation, and synthesis providers."""


class ProviderConfigurationError(ProviderError):
    """Raised when provider selection or credentials are invalid."""
