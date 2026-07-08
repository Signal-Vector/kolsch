"""Shared fakes for spec-012 workflow tests."""

from tests.cli.chat.services.fakes.workflow_executor import (
    FakeExecutor,
    FakeExecutorSpec,
    install_fake_executor_factory,
)

__all__ = [
    "FakeExecutor",
    "FakeExecutorSpec",
    "install_fake_executor_factory",
]
