"""
TeraBox authentication module.
Handles automated login via email/password to obtain the ndus session cookie.

Flow:
  1. Bootstrap — GET login page to obtain jsToken, pcftoken, csrf, browserid
  2. Get RSA public key — GET /passport/getpubkey → AES decrypt to get PEM key
  3. Pre-login — POST /passport/prelogin → get challenge (seval, random, timestamp)
  4. Login — RSA-encrypt password, compute prand, POST /passport/login → get ndus cookie
"""

import base64
import hashlib
import logging
import re
import time

import requests
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA

import config

logger = logging.getLogger(__name__)

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _url_safe_b64decode(s: str) -> bytes:
    """Decode URL-safe base64 (pad if necessary)."""
    s = s.replace("-", "+").replace("_", "/")
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.b64decode(s)


def _url_safe_b64encode(data: bytes) -> str:
    """Encode bytes to URL-safe base64 (no padding)."""
    return base64.b64encode(data).decode().replace("+", "-").replace("/", "_").rstrip("=")


def _md5(text: str) -> str:
    """Return hex MD5 digest of a string."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _sha1(text: str) -> str:
    """Return hex SHA1 digest of a string."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ─── Login Steps ─────────────────────────────────────────────────────────────


class TeraBoxAuth:
    """Handles the full TeraBox login flow."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        })
        self.host = config.get_host()
        self.js_token = ""
        self.pcf_token = ""
        self.csrf = ""
        self.browser_id = ""

    def _update_host(self, response: requests.Response):
        """Update host if TeraBox redirects to a different domain."""
        if response.url:
            from urllib.parse import urlparse
            parsed = urlparse(response.url)
            new_host = f"{parsed.scheme}://{parsed.netloc}"
            if new_host != self.host:
                logger.info(f"Host updated: {self.host} -> {new_host}")
                self.host = new_host
                config.save_host(new_host)

    def bootstrap(self):
        """
        Step 0: Visit login page to get tokens and cookies.
        Extracts jsToken, pcftoken, csrf, and browserid cookie.
        """
        url = f"{self.host}/wap/outlogin/login"
        params = {
            "app_id": config.APP_ID,
            "web": config.WEB,
            "channel": config.CHANNEL,
            "clienttype": config.CLIENTTYPE,
        }

        logger.info("Bootstrap: fetching login page...")
        resp = self.session.get(url, params=params, allow_redirects=True, timeout=30)
        self._update_host(resp)
        text = resp.text

        # Extract jsToken
        match = re.search(r'fn%28%22(.+?)%22%29', text)
        if not match:
            # Try alternative pattern
            match = re.search(r'jsToken\s*[=:]\s*["\']([^"\']+)["\']', text)
        if match:
            self.js_token = match.group(1)
            logger.info(f"Got jsToken: {self.js_token[:20]}...")
        else:
            logger.warning("Could not extract jsToken from login page")

        # Extract pcftoken
        match = re.search(r'"pcftoken"\s*:\s*"([^"]*)"', text)
        if match:
            self.pcf_token = match.group(1)
            logger.info(f"Got pcftoken: {self.pcf_token[:20]}..." if self.pcf_token else "pcftoken is empty")

        # Extract csrf
        match = re.search(r'"csrf"\s*:\s*"([^"]*)"', text)
        if match:
            self.csrf = match.group(1)

        # Extract browserid from cookies
        self.browser_id = self.session.cookies.get("browserid", "")
        if self.browser_id:
            logger.info(f"Got browserid: {self.browser_id[:20]}...")

        return bool(self.js_token or self.pcf_token)

    def get_public_key(self) -> RSA.RsaKey:
        """
        Step 1: Get RSA public key from /passport/getpubkey.
        The key is AES-128-CBC encrypted: decrypt pp1 using pp2 as key.
        """
        url = f"{self.host}/passport/getpubkey"
        params = {
            "app_id": config.APP_ID,
            "web": config.WEB,
            "channel": config.CHANNEL,
            "clienttype": config.CLIENTTYPE,
        }

        logger.info("Fetching RSA public key...")
        resp = self.session.get(url, params=params, timeout=30)
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(f"getpubkey failed: {data}")

        pp1 = data["data"]["pp1"]
        pp2 = data["data"]["pp2"]

        logger.info(f"pp1 length: {len(pp1)}, pp2 length: {len(pp2)}")

        # AES-128-CBC decrypt pp1 using pp2
        # pp2 may be base64-encoded or a raw string key.
        try:
            key_bytes = _url_safe_b64decode(pp2)
        except Exception:
            key_bytes = pp2.encode("utf-8")

        if len(key_bytes) not in (16, 24, 32):
            # Use MD5 of pp2 to derive a 16-byte AES-128 key
            key_bytes = hashlib.md5(pp2.encode("utf-8")).digest()
            logger.info("Derived AES key via MD5 (pp2 decoded len was not 16/24/32)")

        cipher_bytes = _url_safe_b64decode(pp1)

        # Try multiple IV strategies: pp1 may or may not have IV prepended.
        pem_key = None

        # Strategy 1: IV is first 16 bytes of cipher_bytes
        if len(cipher_bytes) > 16 and (len(cipher_bytes) - 16) % 16 == 0:
            iv = cipher_bytes[:16]
            encrypted = cipher_bytes[16:]
            try:
                cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
                decrypted = cipher.decrypt(encrypted)
                pad_len = decrypted[-1]
                if 0 < pad_len <= 16:
                    pem_key = decrypted[:-pad_len].decode("utf-8")
                    logger.info("Decrypted with IV from pp1 prefix.")
            except Exception as e:
                logger.debug(f"Strategy 1 (IV from pp1) failed: {e}")

        # Strategy 2: IV = key itself (first 16 bytes), entire pp1 is ciphertext
        if pem_key is None:
            iv = key_bytes[:16]
            encrypted = cipher_bytes
            # Pad ciphertext to 16-byte boundary if needed
            if len(encrypted) % 16 != 0:
                encrypted = encrypted + b'\x00' * (16 - len(encrypted) % 16)
            try:
                cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
                decrypted = cipher.decrypt(encrypted)
                pad_len = decrypted[-1]
                if 0 < pad_len <= 16:
                    pem_key = decrypted[:-pad_len].decode("utf-8")
                else:
                    pem_key = decrypted.rstrip(b'\x00').decode("utf-8")
                logger.info("Decrypted with IV from key bytes.")
            except Exception as e:
                logger.debug(f"Strategy 2 (IV from key) failed: {e}")

        # Strategy 3: Zero IV, entire pp1 is ciphertext
        if pem_key is None:
            iv = b'\x00' * 16
            encrypted = cipher_bytes
            if len(encrypted) % 16 != 0:
                encrypted = encrypted + b'\x00' * (16 - len(encrypted) % 16)
            try:
                cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
                decrypted = cipher.decrypt(encrypted)
                pad_len = decrypted[-1]
                if 0 < pad_len <= 16:
                    pem_key = decrypted[:-pad_len].decode("utf-8")
                else:
                    pem_key = decrypted.rstrip(b'\x00').decode("utf-8")
                logger.info("Decrypted with zero IV.")
            except Exception as e:
                logger.debug(f"Strategy 3 (zero IV) failed: {e}")

        if pem_key is None or "KEY" not in pem_key.upper():
            raise RuntimeError(
                f"Could not decrypt RSA key from pp1/pp2. "
                f"pp1 decoded len={len(cipher_bytes)}, key len={len(key_bytes)}"
            )

        logger.info("RSA public key obtained.")
        return RSA.import_key(pem_key)

    def prelogin(self, email: str) -> dict:
        """
        Step 2: Pre-login to get challenge values.
        Returns dict with seval, random, timestamp.
        """
        url = f"{self.host}/passport/prelogin"
        data = {
            "client": "web",
            "pass_version": "2.8",
            "clientfrom": "h5",
            "pcftoken": self.pcf_token,
            "email": email,
        }

        logger.info("Pre-login...")
        resp = self.session.post(url, data=data, timeout=30)
        result = resp.json()

        if result.get("code") != 0:
            raise RuntimeError(f"Prelogin failed: {result}")

        challenge = result.get("data", {})
        logger.info("Pre-login successful, got challenge values.")
        return challenge

    def login(self, email: str, password: str) -> str:
        """
        Full login flow. Returns the ndus session cookie.
        """
        # Step 0: Bootstrap
        if not self.bootstrap():
            logger.warning("Bootstrap returned no tokens, proceeding anyway...")

        # Step 1: Get RSA public key
        try:
            rsa_key = self.get_public_key()
        except Exception as e:
            logger.error(f"Failed to get public key: {e}")
            raise

        # Step 2: Pre-login
        try:
            challenge = self.prelogin(email)
        except Exception as e:
            logger.error(f"Pre-login failed: {e}")
            raise

        seval = challenge.get("seval", "")
        random_val = challenge.get("random", "")
        timestamp = challenge.get("timestamp", "")

        # Step 3: Encrypt password
        pwd_md5 = _md5(password)

        # RSA encrypt the MD5 hash with PKCS1 v1.5
        cipher_rsa = PKCS1_v1_5.new(rsa_key)
        encrypted_pwd = cipher_rsa.encrypt(pwd_md5.encode("utf-8"))
        enc_pwd = _url_safe_b64encode(encrypted_pwd)

        # Compute prand = SHA1("web-{seval}-{encpwd}-{email}-{browserid}-{random}")
        prand_input = f"web-{seval}-{enc_pwd}-{email}-{self.browser_id}-{random_val}"
        prand = _sha1(prand_input)

        # Step 4: Login request
        url = f"{self.host}/passport/login"
        form_data = {
            "client": "web",
            "pass_version": "2.8",
            "clientfrom": "h5",
            "pcftoken": self.pcf_token,
            "prand": prand,
            "email": email,
            "pwd": enc_pwd,
            "seval": seval,
            "random": random_val,
            "timestamp": timestamp,
        }

        logger.info("Sending login request...")
        resp = self.session.post(url, data=form_data, timeout=30)
        result = resp.json()

        if result.get("code") != 0:
            error_msg = result.get("msg", result.get("message", "Unknown error"))
            raise RuntimeError(f"Login failed (code {result.get('code')}): {error_msg}")

        # Extract ndus cookie
        ndus = self.session.cookies.get("ndus", "")
        if not ndus:
            # Check all cookies
            for cookie in self.session.cookies:
                if cookie.name == "ndus":
                    ndus = cookie.value
                    break

        if not ndus:
            # Sometimes ndus is in a different domain
            all_cookies = {c.name: c.value for c in self.session.cookies}
            logger.warning(f"ndus not found directly. All cookies: {list(all_cookies.keys())}")
            # Try ndut_fmt as fallback
            ndus = all_cookies.get("ndut_fmt", "")

        if ndus:
            logger.info("Login successful! ndus cookie obtained.")
            config.save_session(ndus)
            return ndus
        else:
            raise RuntimeError("Login succeeded but ndus cookie not found in response.")


def ensure_session() -> str:
    """
    Ensure we have a valid ndus session cookie.
    1. Check cached session
    2. Validate it with a lightweight API call
    3. If invalid, re-login using stored credentials
    Returns the ndus cookie string.
    """
    # Try cached session first
    ndus = config.get_cached_session()
    if ndus:
        logger.info("Found cached session, validating...")
        if validate_session(ndus):
            logger.info("Cached session is valid.")
            return ndus
        logger.info("Cached session expired, re-logging in...")

    # Login with credentials
    email, password = config.get_credentials()
    auth = TeraBoxAuth()
    ndus = auth.login(email, password)
    return ndus


def validate_session(ndus: str) -> bool:
    """Check if an ndus session cookie is still valid."""
    host = config.get_host()
    url = f"{host}/api/user/getinfo"
    params = {
        "app_id": config.APP_ID,
        "web": config.WEB,
        "channel": config.CHANNEL,
        "clienttype": config.CLIENTTYPE,
    }
    headers = {
        "User-Agent": config.USER_AGENT,
        "Cookie": f"ndus={ndus}",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        data = resp.json()
        return data.get("errno", -1) == 0
    except Exception as e:
        logger.warning(f"Session validation failed: {e}")
        return False
