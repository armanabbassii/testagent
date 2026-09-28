from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import MagicMock

from src.observability.observability import Observability

def test_enabled_property(langfuse_settings):
    obs = Observability(langfuse_settings)

    assert obs.enabled is True


def test_disabled_property(disabled_settings):
    obs = Observability(disabled_settings)

    assert obs.enabled is False


def test_get_client_is_lazy(langfuse_settings, mocker):
    mock_langfuse = mocker.patch(
        "src.observability.observability.Langfuse"
    )

    obs = Observability(langfuse_settings)

    client1 = obs.get_client()
    client2 = obs.get_client()

    assert client1 is client2

    mock_langfuse.assert_called_once_with(
        public_key="pk-test",
        secret_key="sk-test",
        host="http://localhost:3000",
    )


def test_get_client_disabled(disabled_settings):
    obs = Observability(disabled_settings)

    assert obs.get_client() is None


def test_callback_handler_is_lazy(langfuse_settings, mocker):
    callback_cls = mocker.patch(
        "src.observability.observability.CallbackHandler"
    )

    obs = Observability(langfuse_settings)

    cb1 = obs.get_callback_handler()
    cb2 = obs.get_callback_handler()

    assert cb1 is cb2

    callback_cls.assert_called_once_with()


def test_callback_handler_disabled(disabled_settings):
    obs = Observability(disabled_settings)

    assert obs.get_callback_handler() is None


def test_callback_handler_disabled(disabled_settings):
    obs = Observability(disabled_settings)

    assert obs.get_callback_handler() is None


def test_graph_config(trace_context, langfuse_settings, mocker):
    callback = MagicMock()

    obs = Observability(langfuse_settings)

    mocker.patch.object(
        obs,
        "get_callback_handler",
        return_value=callback,
    )

    config = obs.graph_config(trace_context)

    assert config["configurable"]["thread_id"] == "thread-123"

    assert config["callbacks"] == [callback]


def test_graph_config(trace_context, langfuse_settings, mocker):
    callback = MagicMock()

    obs = Observability(langfuse_settings)

    mocker.patch.object(
        obs,
        "get_callback_handler",
        return_value=callback,
    )

    config = obs.graph_config(trace_context)

    assert config["configurable"]["thread_id"] == "thread-123"

    assert config["callbacks"] == [callback]


def test_trace(trace_context, langfuse_settings, mocker):
    propagate = mocker.patch(
        "src.observability.observability.propagate_attributes"
    )

    obs = Observability(langfuse_settings)

    obs.trace(trace_context)

    propagate.assert_called_once_with(
        user_id="user-123",
        session_id="session-123",
        trace_name="code-review",
        metadata={
            "mr_iid": 42,
        },
    )


def test_trace_disabled(disabled_settings, trace_context):
    obs = Observability(disabled_settings)

    ctx = obs.trace(trace_context)

    assert isinstance(ctx, type(nullcontext()))


def test_trace_disabled(disabled_settings, trace_context):
    obs = Observability(disabled_settings)

    ctx = obs.trace(trace_context)

    assert isinstance(ctx, type(nullcontext()))


def test_flush_disabled(disabled_settings):
    obs = Observability(disabled_settings)

    obs.flush()