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
from Crypto.Util.Padding import unpad

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

        TeraBox pp1/pp2 format:
          - pp2 (16 chars) = AES-128-CBC key (raw UTF-8 bytes)
          - pp1[:16]       = AES IV (first 16 chars of pp1 as raw UTF-8 bytes)
          - pp1[16:]       = URL-safe base64 encoded AES-CBC ciphertext
          - Decrypted plaintext is a PEM RSA public key (PKCS#1)
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

        resp_data = data.get("data", {})

        # ── Direct pubkey / key field (some API versions) ──
        for field in ("pubkey", "key"):
            if field in resp_data:
                try:
                    key = RSA.import_key(resp_data[field])
                    logger.info(f"RSA key from '{field}' field.")
                    return key
                except Exception as e:
                    logger.debug(f"Direct '{field}' import failed: {e}")

        pp1 = resp_data.get("pp1", "")
        pp2 = resp_data.get("pp2", "")

        if not pp1 or not pp2:
            raise RuntimeError(
                f"Missing pp1/pp2 in response. Keys: {list(resp_data.keys())}"
            )

        logger.info(f"pp1 length: {len(pp1)}, pp2 length: {len(pp2)}")

        # ── Primary method: IV = pp1[:16], ciphertext = b64(pp1[16:]) ──
        aes_key = pp2.encode("utf-8")
        iv = pp1[:16].encode("utf-8")
        ct_b64 = pp1[16:]
        ct = _url_safe_b64decode(ct_b64)

        logger.info(
            f"AES decrypt: key={len(aes_key)}B, iv={len(iv)}B, ct={len(ct)}B "
            f"(mod16={len(ct) % 16})"
        )

        if len(aes_key) in (16, 24, 32) and len(iv) == 16 and len(ct) % 16 == 0:
            try:
                cipher = AES.new(aes_key, AES.MODE_CBC, iv)
                decrypted = unpad(cipher.decrypt(ct), 16, style="pkcs7")
                pem_text = decrypted.decode("utf-8").strip()
                key = RSA.import_key(pem_text)
                logger.info(f"RSA-{key.size_in_bits()} key obtained (pp1-split method).")
                return key
            except Exception as e:
                logger.warning(f"Primary decrypt failed: {e}")

        # ── Fallback: full pp1 base64 decode, various splits ──
        logger.info("Primary method failed, trying fallback strategies...")

        pp1_bytes = _url_safe_b64decode(pp1)

        # Build candidate keys
        raw_pp2 = pp2.encode("utf-8")
        candidate_keys = []
        if len(raw_pp2) in (16, 24, 32):
            candidate_keys.append(("raw_pp2", raw_pp2))
        candidate_keys.append(("md5_pp2", hashlib.md5(raw_pp2).digest()))

        # Try direct import (DER, PEM)
        for name, try_fn in [
            ("DER", lambda: RSA.import_key(pp1_bytes)),
            ("PEM_PKCS8", lambda: RSA.import_key(
                f"-----BEGIN PUBLIC KEY-----\n{pp1}\n-----END PUBLIC KEY-----"
            )),
            ("PEM_PKCS1", lambda: RSA.import_key(
                f"-----BEGIN RSA PUBLIC KEY-----\n{pp1}\n-----END RSA PUBLIC KEY-----"
            )),
        ]:
            try:
                key = try_fn()
                logger.info(f"RSA key imported directly ({name}).")
                return key
            except Exception:
                pass

        # AES with full-decode splits
        for key_name, key_bytes in candidate_keys:
            for iv_name, iv_val, ct_val in [
                ("key_as_iv", key_bytes[:16], pp1_bytes),
                ("zeros_iv", b'\x00' * 16, pp1_bytes),
                ("pp1_12off", key_bytes[:16], pp1_bytes[12:]),
            ]:
                padded = ct_val
                if len(padded) % 16 != 0:
                    padded += b'\x00' * (16 - len(padded) % 16)
                try:
                    cipher = AES.new(key_bytes, AES.MODE_CBC, iv_val)
                    dec = cipher.decrypt(padded)
                    try:
                        dec = unpad(dec, 16, style="pkcs7")
                    except ValueError:
                        dec = dec.rstrip(b'\x00')
                    text = dec.decode("utf-8", errors="ignore").strip()
                    if "BEGIN" in text and "KEY" in text:
                        key = RSA.import_key(text)
                        logger.info(f"RSA key found: {key_name}/{iv_name}/CBC")
                        return key
                except Exception:
                    pass

        raise RuntimeError(
            f"Could not obtain RSA key. pp1 len={len(pp1)}, pp2 len={len(pp2)}. "
            f"Consider providing ndus cookie directly in config.json."
        )

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

        # Step 2: Pre-login (may fail on some accounts; proceed with empty challenge)
        challenge = {}
        try:
            challenge = self.prelogin(email)
        except Exception as e:
            logger.warning(f"Pre-login failed ({e}), continuing with empty challenge...")

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
    1. Check cached session (including manually provided ndus in config.json)
    2. Validate it with a lightweight API call
    3. If invalid, re-login using stored credentials
    Returns the ndus cookie string.
    """
    # Try cached session first (works for manually pasted ndus too)
    ndus = config.get_cached_session()
    if ndus:
        logger.info("Found cached/manual session, validating...")
        if validate_session(ndus):
            logger.info("Session is valid.")
            return ndus
        logger.info("Session expired, attempting re-login...")

    # Login with credentials
    try:
        email, password = config.get_credentials()
    except ValueError:
        raise ValueError(
            "No valid session and no credentials configured.\n"
            "Either:\n"
            "  1. Add your TeraBox email/password to config.json:\n"
            '     {"email": "you@example.com", "password": "yourpass"}\n'
            "  2. Or paste your ndus cookie directly into config.json:\n"
            '     {"ndus": "YOUR_NDUS_COOKIE_FROM_BROWSER"}\n'
            "\n"
            "To get your ndus cookie:\n"
            "  - Login to TeraBox in your browser\n"
            "  - Open DevTools (F12) → Application → Cookies\n"
            "  - Copy the value of the 'ndus' cookie"
        )
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
