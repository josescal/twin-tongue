"""Shared bounded retry policy for transient provider operations."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderRetryPolicy:
    """Retry at most once after the initial attempt, with linear backoff."""

    max_attempts: int = 2
    base_delay_seconds: float = 0.75

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("Provider retry max_attempts must be at least 1.")
        if self.base_delay_seconds < 0:
            raise ValueError("Provider retry base_delay_seconds cannot be negative.")

    def delay_seconds(self, completed_attempts: int) -> float:
        return self.base_delay_seconds * completed_attempts
