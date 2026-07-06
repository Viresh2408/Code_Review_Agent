"""
backend/tests/test_model_backend_flag.py

Tests for the MODEL_BACKEND feature flag in agents/orchestrator.py:
  - MODEL_BACKEND=groq  → call_primary_model calls Groq, never vLLM
  - MODEL_BACKEND=vllm  → call_primary_model calls vLLM, not Groq
  - vLLM connection error in vllm mode → falls back to Groq (connection errors only)
  - Non-connection vLLM error in vllm mode → re-raises (does NOT silently fall back)
  - MODEL_BACKEND=groq regression → behavior identical to Phase 1-5 baseline
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import httpx
import pytest

root_path = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root_path))
sys.path.insert(0, str(root_path / "backend"))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_groq_response(content: str = '{"findings":[]}', prompt_tokens: int = 100, completion_tokens: int = 50):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.usage.prompt_tokens = prompt_tokens
    response.usage.completion_tokens = completion_tokens
    return response


def _settings_with_backend(backend: str):
    """Return a mock Settings object with the specified model_backend."""
    settings = MagicMock()
    settings.model_backend = backend
    settings.vllm_api_url = "http://localhost:8002/v1"
    settings.vllm_model = "qwen-test"
    settings.vllm_gpu_cost_per_token = 0.000001
    return settings


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_groq_mode_calls_groq_not_vllm():
    """
    With MODEL_BACKEND=groq, call_primary_model must call Groq and never httpx (vLLM).
    """
    from agents.orchestrator import call_primary_model

    groq_resp = _make_groq_response()

    with (
        patch("agents.orchestrator.get_settings", return_value=_settings_with_backend("groq")),
        patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}),
        patch("agents.orchestrator.Groq") as mock_groq_cls,
        patch("agents.orchestrator.call_vllm_api") as mock_vllm,
    ):
        mock_groq_instance = MagicMock()
        mock_groq_cls.return_value = mock_groq_instance
        mock_groq_instance.chat.completions.create.return_value = groq_resp

        provider, content, p_tok, c_tok = call_primary_model("test prompt")

        assert provider == "groq"
        mock_vllm.assert_not_called()
        mock_groq_instance.chat.completions.create.assert_called_once()


def test_vllm_mode_calls_vllm_not_groq():
    """
    With MODEL_BACKEND=vllm, call_primary_model must call vLLM, not Groq.
    """
    from agents.orchestrator import call_primary_model

    with (
        patch("agents.orchestrator.get_settings", return_value=_settings_with_backend("vllm")),
        patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}),
        patch("agents.orchestrator.Groq") as mock_groq_cls,
        patch("agents.orchestrator.call_vllm_api") as mock_vllm,
    ):
        mock_vllm.return_value = ('{"findings":[]}', 80, 30)

        provider, content, p_tok, c_tok = call_primary_model("test prompt")

        assert provider == "vllm"
        mock_vllm.assert_called_once_with("test prompt")
        mock_groq_cls.assert_not_called()


def test_vllm_connection_error_falls_back_to_groq():
    """
    If vLLM raises httpx.ConnectError (container not running), it should fall back to Groq.
    This is the ONLY class of error that gets a fallback.
    """
    from agents.orchestrator import call_primary_model

    groq_resp = _make_groq_response()

    with (
        patch("agents.orchestrator.get_settings", return_value=_settings_with_backend("vllm")),
        patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}),
        patch("agents.orchestrator.Groq") as mock_groq_cls,
        patch("agents.orchestrator.call_vllm_api") as mock_vllm,
    ):
        # Simulate vLLM container not running
        mock_vllm.side_effect = httpx.ConnectError("Connection refused")

        mock_groq_instance = MagicMock()
        mock_groq_cls.return_value = mock_groq_instance
        mock_groq_instance.chat.completions.create.return_value = groq_resp

        provider, content, p_tok, c_tok = call_primary_model("test prompt")

        # Falls back to Groq
        assert provider == "groq"
        mock_groq_instance.chat.completions.create.assert_called_once()


def test_vllm_non_connection_error_raises():
    """
    If vLLM raises a non-connection error (e.g., JSON decode error, 500), it must
    NOT silently fall back to Groq — it must raise RuntimeError so the failure surfaces.
    """
    from agents.orchestrator import call_primary_model

    with (
        patch("agents.orchestrator.get_settings", return_value=_settings_with_backend("vllm")),
        patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}),
        patch("agents.orchestrator.Groq"),
        patch("agents.orchestrator.call_vllm_api") as mock_vllm,
    ):
        mock_vllm.side_effect = ValueError("Invalid JSON response from vLLM")

        with pytest.raises(RuntimeError, match="non-connection error"):
            call_primary_model("test prompt")


def test_groq_mode_regression_matches_phase2_behavior():
    """
    Regression test: with MODEL_BACKEND=groq, the behavior must be identical to
    the Phase 1-5 baseline — Groq is called with GROQ_MODEL, same args.
    """
    from agents.orchestrator import call_primary_model, GROQ_MODEL

    groq_resp = _make_groq_response('{"findings":[{"line":5,"severity":"warning","message":"test","confidence":0.9,"suggested_fix":null}]}')

    with (
        patch("agents.orchestrator.get_settings", return_value=_settings_with_backend("groq")),
        patch.dict(os.environ, {"GROQ_API_KEY": "regression-test-key"}),
        patch("agents.orchestrator.Groq") as mock_groq_cls,
        patch("agents.orchestrator.call_vllm_api") as mock_vllm,
    ):
        mock_instance = MagicMock()
        mock_groq_cls.return_value = mock_instance
        mock_instance.chat.completions.create.return_value = groq_resp

        provider, content, p_tok, c_tok = call_primary_model("regression test prompt")

        # Must use the pinned GROQ_MODEL constant
        create_call = mock_instance.chat.completions.create.call_args
        assert create_call.kwargs["model"] == GROQ_MODEL
        # Must pass response_format={"type": "json_object"} for structured output
        assert create_call.kwargs["response_format"] == {"type": "json_object"}
        assert create_call.kwargs["temperature"] == 0.1
        assert provider == "groq"
        mock_vllm.assert_not_called()


def test_vllm_cost_uses_gpu_rate_not_zero():
    """
    With provider='vllm', log_llm_usage must compute cost using vllm_gpu_cost_per_token,
    not hardcoded 0.0.
    """
    from agents.orchestrator import log_llm_usage

    gpu_rate = 0.000002  # $2/M tokens

    with patch("agents.orchestrator.get_settings") as mock_settings:
        mock_settings.return_value.vllm_gpu_cost_per_token = gpu_rate

        cost = log_llm_usage("vllm", "qwen-test", prompt_tokens=1000, completion_tokens=200)

        expected = (1000 + 200) * gpu_rate
        assert abs(cost - expected) < 1e-10, f"Expected {expected:.8f}, got {cost:.8f}"
        assert cost > 0.0, "vLLM cost must not be zero — use amortized GPU rate"
