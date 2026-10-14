import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from log_inspector.main import create_app
from log_inspector.services.cognito import CognitoVerifier
from tests.conftest import make_settings

POOL = "us-east-1_TestPool"
CLIENT = "client123"
ISSUER = f"https://cognito-idp.us-east-1.amazonaws.com/{POOL}"


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def cognito_client(aws, private_key):
    app = create_app(make_settings(cognito_user_pool_id=POOL, cognito_client_id=CLIENT))
    # Serve our own public key instead of fetching the pool's JWKS.
    app.state.cognito_verifier._jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private_key.public_key())
    )
    with TestClient(app) as client:
        yield client


def token(private_key, **overrides) -> str:
    claims = {
        "sub": "sub-1",
        "iss": ISSUER,
        "token_use": "access",
        "client_id": CLIENT,
        "username": "akash",
        "exp": int(time.time()) + 3600,
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256")


def get_me(client, bearer):
    return client.get("/me", headers={"Authorization": f"Bearer {bearer}"})


def test_valid_token_creates_the_user_on_first_request(cognito_client, private_key):
    first = get_me(cognito_client, token(private_key))
    assert first.status_code == 200
    assert first.json()["name"] == "akash"

    second = get_me(cognito_client, token(private_key))
    assert second.json()["id"] == first.json()["id"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": 1},
        {"iss": "https://cognito-idp.us-east-1.amazonaws.com/other"},
        {"token_use": "id"},
        {"client_id": "someone-else"},
    ],
)
def test_bad_tokens_are_rejected(cognito_client, private_key, overrides):
    assert get_me(cognito_client, token(private_key, **overrides)).status_code == 401


def test_token_signed_by_another_key_is_rejected(cognito_client):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert get_me(cognito_client, token(other)).status_code == 401


def test_tokens_are_rejected_when_cognito_is_not_configured(aws, private_key):
    with TestClient(create_app(make_settings())) as client:
        assert get_me(client, token(private_key)).status_code == 401


def test_api_keys_still_work_with_cognito_configured(cognito_client):
    from tests.conftest import login

    login(cognito_client)
    assert cognito_client.get("/me").status_code == 200


def test_verifier_builds_the_pool_issuer():
    verifier = CognitoVerifier("us-east-1", POOL, CLIENT)
    assert verifier._issuer == ISSUER
