"""Aether — a continuous, cross-platform personal action agent."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("aether-agent")
except PackageNotFoundError:  # a source checkout that was never installed
    __version__ = "0.0.0"
