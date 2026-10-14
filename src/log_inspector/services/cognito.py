"""Checking Amazon Cognito access tokens.

Cognito signs access tokens with the user pool's private keys and publishes the public keys
at a JWKS URL. We verify the signature, expiry, issuer, token type and app client, so only
tokens issued to our frontend by our pool are accepted.
"""

import jwt
from jwt import PyJWKClient


class InvalidToken(Exception):
    pass


class CognitoVerifier:
    def __init__(self, region: str, user_pool_id: str, client_id: str):
        self._client_id = client_id
        self._issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        # Caches the keys, so the JWKS is fetched once per cold start, not per request.
        self._jwks = PyJWKClient(f"{self._issuer}/.well-known/jwks.json")

    def verify(self, token: str) -> dict:
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                options={"require": ["exp", "iss", "sub"], "verify_aud": False},
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(str(exc)) from exc
        # Access tokens carry client_id instead of aud. ID tokens must not be accepted.
        if claims.get("token_use") != "access" or claims.get("client_id") != self._client_id:
            raise InvalidToken("Not an access token for this app")
        return claims
