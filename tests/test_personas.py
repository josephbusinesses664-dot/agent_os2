import os
from unittest import mock
import pytest

from agentos.agents.personas import (
    branch_for,
    branch_channel_for,
    username_for,
    token_for_agent,
    PERSONAS,
    USERNAMES,
    BRANCH_OF,
    BRANCH_CHANNELS,
)


def test_constants_populated():
    """Ensure our basic dictionaries are populated with at least some basic data."""
    assert "executive" in PERSONAS
    assert "executive" in USERNAMES
    assert "executive" in BRANCH_OF
    # BRANCH_CHANNELS is derived from BRANCH_OF values
    assert "executive" in BRANCH_CHANNELS


def test_branch_for():
    """Test standard and edge cases for branch_for."""
    # Known agent
    assert branch_for("cto") == "engineering"
    assert branch_for("executive") == "executive"

    # Unknown agent defaults to "operations"
    assert branch_for("nonexistent-agent-id") == "operations"


def test_branch_channel_for():
    """Test standard and edge cases for branch_channel_for."""
    # Known agent
    assert branch_channel_for("cto") == "branch-engineering"
    assert branch_channel_for("executive") == "branch-executive"

    # Unknown agent gets the channel for the default branch ("operations")
    assert branch_channel_for("nonexistent-agent-id") == "branch-operations"


def test_username_for():
    """Test username formatting and fallback logic."""
    # Explicit override for executive
    assert username_for("executive") == "alex.ceo"

    # Standard format: <persona>.<role-slug>
    assert username_for("cto") == "jordan.cto"

    # Check fallback when <persona>.<role-slug> exceeds 22 chars
    # "requirements-analyst" persona is "Quinn"
    # "quinn.requirements-analyst" length = 5 + 1 + 20 = 26 > 22
    assert len("quinn.requirements-analyst") > 22
    assert username_for("requirements-analyst") == "quinn"

    # Unknown agent falls back to the exact agent_id
    assert username_for("some-new-agent") == "some-new-agent"


def test_token_for_agent():
    """Test personal access token retrieval from the environment."""
    # With token set
    with mock.patch.dict(os.environ, {"AGENT_TOKEN_MY_AGENT": "secret-token-123"}):
        assert token_for_agent("my-agent") == "secret-token-123"
        # Also test with different casing
        assert token_for_agent("MY-AGENT") == "secret-token-123"

    # With no token set (fallback)
    with mock.patch.dict(os.environ, clear=True):
        assert token_for_agent("my-agent") == ""
