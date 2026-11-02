"""Cognito SRP sign-in (B.1.5): the exchange shape, the SRP arithmetic against a
simulated pool, and how the provider picks between Cognito and Aura."""

import base64
import hashlib
import hmac
import os
import re

import pytest

from circle_mcp import cognito
from circle_mcp.cognito import CognitoError, CognitoSrp
from circle_mcp.provider import CircleAuthError, CircleClient

POOL = "us-west-2_TiQsgtChT"
CLIENT = "fd7ail96985e3p3omb792ko0f"


class FakePool:
    """Server side of USER_SRP_AUTH, enough to verify a client's password claim."""

    def __init__(self, username, password, user_id="sub-1234"):
        self.username = username
        self.password = password
        self.user_id = user_id
        self.salt_hex = "deadbeefcafe"
        self.secret_block = base64.b64encode(b"opaque-secret-block").decode()
        self.calls = []
        self.k = cognito._k_value()
        identity = f"{POOL.split('_', 1)[1]}{user_id}:{password}".encode()
        x = int(cognito._hex_hash(cognito._pad_hex(self.salt_hex) + cognito._sha256_hex(identity)), 16)
        self.v = pow(cognito._G, x, cognito._N)
        self.b = int.from_bytes(os.urandom(64), "big") % cognito._N
        self.B = (self.k * self.v + pow(cognito._G, self.b, cognito._N)) % cognito._N
        self.A = None

    def __call__(self, target, payload):
        self.calls.append((target, payload))
        if target == "InitiateAuth" and payload["AuthFlow"] == "USER_SRP_AUTH":
            if payload["AuthParameters"]["USERNAME"] != self.username:
                raise CognitoError("UserNotFoundException", "User does not exist.")
            self.A = int(payload["AuthParameters"]["SRP_A"], 16)
            return {
                "ChallengeName": "PASSWORD_VERIFIER",
                "ChallengeParameters": {
                    "SRP_B": format(self.B, "x"),
                    "SALT": self.salt_hex,
                    "SECRET_BLOCK": self.secret_block,
                    "USER_ID_FOR_SRP": self.user_id,
                    "USERNAME": self.user_id,
                },
            }
        if target == "InitiateAuth" and payload["AuthFlow"] == "REFRESH_TOKEN_AUTH":
            assert payload["AuthParameters"]["REFRESH_TOKEN"] == "refresh-1"
            return {"AuthenticationResult": {"IdToken": "id-2", "AccessToken": "access-2"}}
        if target == "RespondToAuthChallenge":
            answers = payload["ChallengeResponses"]
            u = int(cognito._hex_hash(cognito._pad_hex(self.A) + cognito._pad_hex(self.B)), 16)
            shared = pow(self.A * pow(self.v, u, cognito._N), self.b, cognito._N)
            key = cognito._hkdf_key(shared, u)
            message = (
                POOL.split("_", 1)[1].encode()
                + self.user_id.encode()
                + base64.b64decode(answers["PASSWORD_CLAIM_SECRET_BLOCK"])
                + answers["TIMESTAMP"].encode()
            )
            expected = base64.b64encode(hmac.new(key, message, hashlib.sha256).digest()).decode()
            if answers["PASSWORD_CLAIM_SIGNATURE"] != expected:
                raise CognitoError("NotAuthorizedException", "Incorrect username or password.")
            return {
                "AuthenticationResult": {"IdToken": "id-1", "AccessToken": "access-1", "RefreshToken": "refresh-1"}
            }
        raise AssertionError(f"unexpected call {target} {payload}")


def test_srp_exchange_signs_the_password_claim_the_pool_accepts():
    pool = FakePool("parent@example.com", "hunter2-correct")
    tokens = CognitoSrp(POOL, CLIENT).sign_in("parent@example.com", "hunter2-correct", post=pool)
    assert tokens == {"id_token": "id-1", "access_token": "access-1", "refresh_token": "refresh-1"}
    initiate, respond = pool.calls
    assert initiate[1]["ClientId"] == CLIENT
    assert respond[1]["ChallengeName"] == "PASSWORD_VERIFIER"
    answers = respond[1]["ChallengeResponses"]
    assert answers["USERNAME"] == "sub-1234"  # the pool's id, not the email
    assert answers["PASSWORD_CLAIM_SECRET_BLOCK"] == pool.secret_block
    assert re.fullmatch(r"[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2} \d\d:\d\d:\d\d UTC \d{4}", answers["TIMESTAMP"])


def test_wrong_password_is_rejected_by_the_pool():
    pool = FakePool("parent@example.com", "hunter2-correct")
    with pytest.raises(CognitoError) as info:
        CognitoSrp(POOL, CLIENT).sign_in("parent@example.com", "hunter2-wrong", post=pool)
    assert info.value.code == "NotAuthorizedException"
    assert not info.value.user_not_found


def test_unknown_user_is_distinguishable():
    pool = FakePool("parent@example.com", "pw")
    with pytest.raises(CognitoError) as info:
        CognitoSrp(POOL, CLIENT).sign_in("other@example.com", "pw", post=pool)
    assert info.value.user_not_found


def test_refresh_keeps_the_refresh_token_when_the_pool_omits_it():
    pool = FakePool("parent@example.com", "pw")
    tokens = CognitoSrp(POOL, CLIENT).refresh("refresh-1", post=pool)
    assert tokens == {"id_token": "id-2", "access_token": "access-2", "refresh_token": "refresh-1"}
    assert pool.calls[0][1]["AuthFlow"] == "REFRESH_TOKEN_AUTH"


def test_extra_challenge_is_reported_not_guessed():
    def post(target, payload):
        if target == "InitiateAuth":
            return {"ChallengeName": "SMS_MFA", "ChallengeParameters": {}}
        raise AssertionError

    with pytest.raises(CognitoError) as info:
        CognitoSrp(POOL, CLIENT).sign_in("u", "p", post=post)
    assert "SMS_MFA" in str(info.value)


def test_pool_id_gives_region_and_pool_name():
    srp = CognitoSrp(POOL, CLIENT)
    assert srp.region == "us-west-2"
    assert srp.pool_name == "TiQsgtChT"
    assert srp.endpoint == "https://cognito-idp.us-west-2.amazonaws.com/"
    with pytest.raises(ValueError):
        CognitoSrp("nounderscore", CLIENT)


def test_pad_hex_matches_cognito_two_s_complement_rule():
    assert cognito._pad_hex("abc") == "0abc"
    assert cognito._pad_hex("8abc") == "008abc"
    assert cognito._pad_hex("7abc") == "7abc"
    assert cognito._pad_hex(0xFF) == "00ff"


# -----------------------------
# Provider: backend selection and the grant
# -----------------------------


class GrantHTTP:
    """Stub for CircleClient._http_json: records the grant and answers host discovery."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, method="GET", headers=None, body=None):
        self.calls.append({"url": url, "method": method, "headers": headers or {}, "body": body})
        if url.endswith("/api/v2/grant/admin"):
            return {"ok": True, "token": "admin-jwt", "circleid": "circle-42"}
        if "/api/v1.0/host" in url:
            return {"host": "vc.meetcircle-blue.co"}
        if url.endswith("/auth/session/signin"):
            return {"access_token": "aura-access", "refresh_token": "aura-refresh"}
        raise AssertionError(f"unexpected {method} {url}")


def make_client(auth_method, cognito_post):
    client = CircleClient(
        username="parent@example.com",
        password="pw",
        aura_public_key="circle_mobile_auth:test",
        auth_method=auth_method,
    )
    client._http_json = GrantHTTP()
    client._cognito._post = cognito_post
    return client


def test_cognito_id_token_becomes_the_grant_jwt():
    pool = FakePool("parent@example.com", "pw")
    client = make_client("cognito", pool)
    assert client.ensure_admin_token() == "admin-jwt"
    grant = [c for c in client._http_json.calls if c["url"].endswith("/grant/admin")][0]
    assert grant["body"] == {"deviceid": "circle-mcp", "jwt": "id-1"}
    assert client.auth_backend == "cognito"
    assert not [c for c in client._http_json.calls if c["url"].endswith("/signin")]


def test_auto_prefers_cognito_when_the_pool_knows_the_user():
    pool = FakePool("parent@example.com", "pw")
    client = make_client("auto", pool)
    assert client.ensure_admin_token() == "admin-jwt"
    assert client.auth_backend == "cognito"


def test_auto_falls_back_to_aura_for_an_unknown_pool_user():
    pool = FakePool("someone-else@example.com", "pw")
    client = make_client("auto", pool)
    assert client.ensure_admin_token() == "admin-jwt"
    assert client.auth_backend == "aura"
    grant = [c for c in client._http_json.calls if c["url"].endswith("/grant/admin")][0]
    assert grant["body"]["jwt"] == "aura-access"
    assert len(pool.calls) == 1  # one probe, never a password claim


def test_auto_does_not_fall_back_on_a_wrong_password():
    pool = FakePool("parent@example.com", "other-pw")
    client = make_client("auto", pool)
    with pytest.raises(CircleAuthError) as info:
        client.ensure_admin_token()
    assert "NotAuthorizedException" in str(info.value)
    assert client.auth_backend is None
    assert not [c for c in client._http_json.calls if c["url"].endswith("/signin")]


def test_aura_only_never_touches_cognito():
    def never(target, payload):
        raise AssertionError("cognito called")

    client = make_client("aura", never)
    assert client.ensure_admin_token() == "admin-jwt"
    assert client.auth_backend == "aura"


def test_unknown_auth_method_is_refused():
    with pytest.raises(ValueError):
        CircleClient(username="u", password="p", auth_method="magic")
