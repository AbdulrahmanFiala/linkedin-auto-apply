from dataclasses import dataclass

from .models import ApplicationStatus


@dataclass(slots=True)
class ApplicationService:
    """Extension point for future apply workflows. Current behavior is manual review only."""

    def queue_for_review(self, job_id: int) -> ApplicationStatus:
        return ApplicationStatus.QUEUED
