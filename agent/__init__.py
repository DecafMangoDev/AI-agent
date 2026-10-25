"""Minimal external-model integration for the coding agent."""

from .client import ModelClient, ModelConfig, ModelError, OpenAICompatibleClient

__all__ = ["ModelClient", "ModelConfig", "ModelError", "OpenAICompatibleClient"]
