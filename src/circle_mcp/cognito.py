"""
AWS Cognito user-pool sign-in (SRP), stdlib only.

Circle accounts created before the Aura migration live in a Cognito user pool.
Signing in there yields an *id token* which the Circle auth service accepts as
the `jwt` of the admin grant, exactly like an Aura access token.

The flow is the standard Cognito USER_SRP_AUTH exchange against the
unauthenticated `cognito-idp` JSON endpoint:

1. InitiateAuth(USER_SRP_AUTH, USERNAME, SRP_A)         -> PASSWORD_VERIFIER challenge
2. RespondToAuthChallenge(PASSWORD_VERIFIER, signature) -> IdToken / AccessToken / RefreshToken
3. InitiateAuth(REFRESH_TOKEN_AUTH, REFRESH_TOKEN)      -> fresh IdToken / AccessToken

The SRP arithmetic follows RFC 5054 (3072-bit group, g = 2) with Cognito's
hashing conventions (SHA-256, hex padding, HKDF info "Caldera Derived Key").
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional

# RFC 5054 3072-bit prime, as Cognito uses it.
_N_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D04507A33"
    "A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864"
    "D87602733EC86A64521F2B18177B200CBBE117577A615D6C770988C0BAD946E2"
    "08E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF"
)
_N = int(_N_HEX, 16)
_G = 2
_INFO_BITS = b"Caldera Derived Key" + b"\x01"

Poster = Callable[[str, Dict], Dict]


class CognitoError(RuntimeError):
    """A cognito-idp call was rejected. `code` is the AWS exception type."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message

    @property
    def user_not_found(self) -> bool:
        return self.code == "UserNotFoundException"


# -----------------------------
# Number/hash helpers
# -----------------------------


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().zfill(64)


def _hex_hash(hex_str: str) -> str:
    return _sha256_hex(bytes.fromhex(hex_str))


def _pad_hex(value) -> str:
    """Hex with an even length and a leading 00 when the top bit is set (two's complement positive)."""
    text = value if isinstance(value, str) else format(value, "x")
    if len(text) % 2:
        return "0" + text
    if text[0] in "89abcdefABCDEF":
        return "00" + text
    return text


def _hkdf_key(shared_secret: int, u: int) -> bytes:
    ikm = bytes.fromhex(_pad_hex(shared_secret))
    salt = bytes.fromhex(_pad_hex(u))
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, _INFO_BITS, hashlib.sha256).digest()[:16]


def _k_value() -> int:
    return int(_hex_hash("00" + _N_HEX + "0" + format(_G, "x")), 16)


def _timestamp(now: Optional[datetime.datetime] = None) -> str:
    """Cognito's challenge timestamp: e.g. 'Mon Nov 2 17:04:09 UTC 2026' (day not zero-padded)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return f"{now:%a %b} {now.day} {now:%H:%M:%S} UTC {now.year}"


# -----------------------------
# SRP client
# -----------------------------


class CognitoSrp:
    """One Cognito user-pool app client; `sign_in` runs the SRP password exchange."""

    def __init__(self, pool_id: str, client_id: str, region: str = "", rand: Optional[Callable[[], int]] = None):
        if "_" not in pool_id:
            raise ValueError(f"malformed Cognito pool id {pool_id!r}")
        self.pool_id = pool_id
        self.client_id = client_id
        self.region = region or pool_id.split("_", 1)[0]
        self.pool_name = pool_id.split("_", 1)[1]
        self._rand = rand or (lambda: int.from_bytes(os.urandom(128), "big"))
        self._k = _k_value()

    @property
    def endpoint(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/"

    def _ephemeral(self):
        """Client private/public SRP values (a, A); A must not be 0 mod N."""
        while True:
            a = self._rand() % _N
            A = pow(_G, a, _N)
            if A % _N != 0:
                return a, A

    def password_signature(
        self,
        password: str,
        user_id: str,
        a: int,
        A: int,
        B: int,
        salt_hex: str,
        secret_block_b64: str,
        timestamp: str,
    ) -> str:
        """The PASSWORD_CLAIM_SIGNATURE for a PASSWORD_VERIFIER challenge."""
        if B % _N == 0:
            raise CognitoError("InvalidSrpB", "server sent SRP_B equal to zero")
        u = int(_hex_hash(_pad_hex(A) + _pad_hex(B)), 16)
        if u == 0:
            raise CognitoError("InvalidSrpU", "u value is zero")
        identity = f"{self.pool_name}{user_id}:{password}".encode()
        x = int(_hex_hash(_pad_hex(salt_hex) + _sha256_hex(identity)), 16)
        base_term = B - self._k * pow(_G, x, _N)
        shared = pow(base_term, a + u * x, _N)
        key = _hkdf_key(shared, u)
        message = (
            self.pool_name.encode()
            + user_id.encode()
            + base64.b64decode(secret_block_b64)
            + timestamp.encode()
        )
        return base64.b64encode(hmac.new(key, message, hashlib.sha256).digest()).decode()

    def sign_in(self, username: str, password: str, post: Optional[Poster] = None) -> Dict[str, str]:
        """USER_SRP_AUTH -> {"id_token", "access_token", "refresh_token"}."""
        post = post or self._post
        a, A = self._ephemeral()
        started = post(
            "InitiateAuth",
            {
                "AuthFlow": "USER_SRP_AUTH",
                "ClientId": self.client_id,
                "AuthParameters": {"USERNAME": username, "SRP_A": format(A, "x")},
            },
        )
        challenge = started.get("ChallengeName")
        if challenge != "PASSWORD_VERIFIER":
            raise CognitoError("UnexpectedChallenge", f"expected PASSWORD_VERIFIER, got {challenge!r}")
        params = started.get("ChallengeParameters") or {}
        user_id = params.get("USER_ID_FOR_SRP") or params.get("USERNAME") or username
        timestamp = _timestamp()
        signature = self.password_signature(
            password=password,
            user_id=user_id,
            a=a,
            A=A,
            B=int(params["SRP_B"], 16),
            salt_hex=params["SALT"],
            secret_block_b64=params["SECRET_BLOCK"],
            timestamp=timestamp,
        )
        answered = post(
            "RespondToAuthChallenge",
            {
                "ChallengeName": "PASSWORD_VERIFIER",
                "ClientId": self.client_id,
                "ChallengeResponses": {
                    "TIMESTAMP": timestamp,
                    "USERNAME": user_id,
                    "PASSWORD_CLAIM_SECRET_BLOCK": params["SECRET_BLOCK"],
                    "PASSWORD_CLAIM_SIGNATURE": signature,
                },
            },
        )
        return self._tokens(answered)

    def refresh(self, refresh_token: str, post: Optional[Poster] = None) -> Dict[str, str]:
        """REFRESH_TOKEN_AUTH -> fresh id/access tokens (the refresh token itself is reused)."""
        post = post or self._post
        answered = post(
            "InitiateAuth",
            {
                "AuthFlow": "REFRESH_TOKEN_AUTH",
                "ClientId": self.client_id,
                "AuthParameters": {"REFRESH_TOKEN": refresh_token},
            },
        )
        tokens = self._tokens(answered)
        tokens["refresh_token"] = tokens.get("refresh_token") or refresh_token
        return tokens

    @staticmethod
    def _tokens(answer: Dict) -> Dict[str, str]:
        result = answer.get("AuthenticationResult")
        if not result:
            challenge = answer.get("ChallengeName") or "none"
            raise CognitoError(
                "UnexpectedChallenge",
                f"sign-in needs an extra step ({challenge}) this client does not handle",
            )
        return {
            "id_token": (result.get("IdToken") or "").strip(),
            "access_token": (result.get("AccessToken") or "").strip(),
            "refresh_token": (result.get("RefreshToken") or "").strip(),
        }

    def _post(self, target: str, payload: Dict, timeout_s: int = 30) -> Dict:
        """POST one cognito-idp operation; AWS error bodies become CognitoError."""
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/x-amz-json-1.1",
                "X-Amz-Target": f"AWSCognitoIdentityProviderService.{target}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {}
            code = str(body.get("__type") or f"HTTP{e.code}").split("#")[-1]
            raise CognitoError(code, str(body.get("message") or raw[:200])) from e
