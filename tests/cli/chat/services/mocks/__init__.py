"""Shared mocks for spec-012 workflow tests."""

from tests.cli.chat.services.mocks.workflow_executor import (
    MockExecutor,
    MockExecutorSpec,
    install_mock_executor_factory,
)

__all__ = [
    "MockExecutor",
    "MockExecutorSpec",
    "install_mock_executor_factory",
]
