"""/healthz names the deployed commit: DOCIST_COMMIT, else Railway's
RAILWAY_GIT_COMMIT_SHA, cut to 12 characters, else null."""
import pytest

SHA_A = "0123456789abcdef0123456789abcdef01234567"
SHA_B = "fedcba9876543210fedcba9876543210fedcba98"


@pytest.fixture
def no_commit_env(monkeypatch):
    monkeypatch.delenv("DOCIST_COMMIT", raising=False)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    return monkeypatch


def commit(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok"
    return body["commit"]


def test_docist_commit_wins_and_is_cut_to_12(client, no_commit_env):
    no_commit_env.setenv("DOCIST_COMMIT", SHA_A)
    no_commit_env.setenv("RAILWAY_GIT_COMMIT_SHA", SHA_B)
    assert commit(client) == SHA_A[:12]


def test_railway_sha_when_docist_commit_is_unset(client, no_commit_env):
    no_commit_env.setenv("RAILWAY_GIT_COMMIT_SHA", SHA_B)
    assert commit(client) == SHA_B[:12]


def test_an_empty_docist_commit_counts_as_unset(client, no_commit_env):
    no_commit_env.setenv("DOCIST_COMMIT", "  ")
    no_commit_env.setenv("RAILWAY_GIT_COMMIT_SHA", SHA_B)
    assert commit(client) == SHA_B[:12]


def test_null_when_neither_is_set(client, no_commit_env):
    response = client.get("/healthz")
    assert "commit" in response.get_json()
    assert commit(client) is None


def test_a_short_value_is_kept_whole(client, no_commit_env):
    no_commit_env.setenv("DOCIST_COMMIT", "v1.2.0")
    assert commit(client) == "v1.2.0"
