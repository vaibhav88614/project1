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
        Tries every known TeraBox key format and decryption method.
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
        logger.info(f"getpubkey response keys: {list(resp_data.keys())}")

        # ── Direct pubkey field ──
        if "pubkey" in resp_data:
            pem_key = resp_data["pubkey"]
            logger.info("RSA public key returned directly (pubkey field).")
            return RSA.import_key(pem_key)

        # ── Direct key field ──
        if "key" in resp_data:
            try:
                key = RSA.import_key(resp_data["key"])
                logger.info("RSA key from 'key' field.")
                return key
            except Exception as e:
                logger.debug(f"Direct 'key' field import failed: {e}")

        pp1 = resp_data.get("pp1", "")
        pp2 = resp_data.get("pp2", "")

        if not pp1:
            raise RuntimeError(f"No pubkey/pp1 in response. Keys: {list(resp_data.keys())}")

        logger.info(f"pp1 length: {len(pp1)}, pp2 length: {len(pp2)}")
        logger.info(f"pp1 first 20 chars: {pp1[:20]}")

        # Decode pp1 from base64
        pp1_bytes = None
        for decode_name, decode_fn in [
            ("std_b64", lambda s: base64.b64decode(s + "=" * ((4 - len(s) % 4) % 4))),
            ("urlsafe_b64", _url_safe_b64decode),
        ]:
            try:
                pp1_bytes = decode_fn(pp1)
                logger.info(f"pp1 decoded ({decode_name}): {len(pp1_bytes)} bytes")
                break
            except Exception as e:
                logger.debug(f"pp1 decode ({decode_name}) failed: {e}")

        if pp1_bytes is None:
            raise RuntimeError("Could not base64-decode pp1")

        logger.info(f"pp1 first 8 bytes hex: {pp1_bytes[:8].hex()}")

        # ── Strategy A: pp1 is the key itself (not encrypted) ──
        for attempt_name, attempt_fn in [
            ("DER", lambda: RSA.import_key(pp1_bytes)),
            ("PEM_PKCS8", lambda: RSA.import_key(
                f"-----BEGIN PUBLIC KEY-----\n{pp1}\n-----END PUBLIC KEY-----"
            )),
            ("PEM_PKCS1", lambda: RSA.import_key(
                f"-----BEGIN RSA PUBLIC KEY-----\n{pp1}\n-----END RSA PUBLIC KEY-----"
            )),
            ("PEM_direct", lambda: RSA.import_key(pp1) if "BEGIN" in pp1 else None),
        ]:
            try:
                key = attempt_fn()
                if key:
                    logger.info(f"RSA key imported directly ({attempt_name}).")
                    return key
            except Exception as e:
                logger.debug(f"Direct import ({attempt_name}) failed: {e}")

        # ── Strategy B: pp1 bytes are raw RSA modulus (256 or 257 bytes) ──
        # Some API versions return the raw modulus; exponent is assumed 65537
        if len(pp1_bytes) in (256, 257, 512, 513):
            for start_offset in (0, 1):
                try:
                    mod_bytes = pp1_bytes[start_offset:]
                    n = int.from_bytes(mod_bytes, byteorder='big')
                    e = 65537
                    key = RSA.construct((n, e))
                    logger.info(
                        f"RSA key constructed from raw modulus "
                        f"(offset={start_offset}, len={len(mod_bytes)})."
                    )
                    return key
                except Exception as ex:
                    logger.debug(f"Raw modulus (offset={start_offset}) failed: {ex}")

        # ── Strategy C: AES decryption (stream/block modes) with pp2 ──
        logger.info("Direct key import failed, trying AES decryption...")

        raw_pp2 = pp2.encode("utf-8")
        candidate_keys = []

        if len(raw_pp2) in (16, 24, 32):
            candidate_keys.append(("raw_pp2", raw_pp2))
        try:
            d = base64.b64decode(pp2 + "=" * ((4 - len(pp2) % 4) % 4))
            if len(d) in (16, 24, 32):
                candidate_keys.append(("b64_pp2", d))
        except Exception:
            pass
        candidate_keys.append(("md5_pp2", hashlib.md5(raw_pp2).digest()))

        def _try_import(raw: bytes) -> RSA.RsaKey | None:
            """Try to import raw bytes as RSA key in various ways."""
            # As DER
            try:
                return RSA.import_key(raw)
            except Exception:
                pass
            # As PEM text
            try:
                text = raw.decode("utf-8", errors="ignore").strip()
                if "BEGIN" in text and "KEY" in text:
                    return RSA.import_key(text)
            except Exception:
                pass
            # As base64-encoded DER (double-encoded)
            try:
                text = raw.decode("ascii", errors="ignore").strip()
                der = base64.b64decode(text + "=" * ((4 - len(text) % 4) % 4))
                return RSA.import_key(der)
            except Exception:
                pass
            # Wrap as PEM
            try:
                text = raw.decode("ascii", errors="ignore").strip()
                pem = f"-----BEGIN PUBLIC KEY-----\n{text}\n-----END PUBLIC KEY-----"
                return RSA.import_key(pem)
            except Exception:
                pass
            return None

        for key_name, key_bytes in candidate_keys:
            iv_list = [
                ("key_as_iv", key_bytes[:16]),
                ("zeros_iv", b'\x00' * 16),
            ]
            if len(pp1_bytes) > 16:
                iv_list.insert(0, ("pp1_prefix_iv", pp1_bytes[:16]))

            for iv_name, iv in iv_list:
                ct = pp1_bytes[16:] if iv_name == "pp1_prefix_iv" else pp1_bytes

                # --- Block modes (need padding to 16) ---
                padded_ct = ct
                if len(padded_ct) % 16 != 0:
                    padded_ct += b'\x00' * (16 - len(padded_ct) % 16)

                for mode_name, make_cipher in [
                    ("CBC", lambda: AES.new(key_bytes, AES.MODE_CBC, iv)),
                    ("ECB", lambda: AES.new(key_bytes, AES.MODE_ECB)),
                ]:
                    try:
                        decrypted = make_cipher().decrypt(padded_ct)
                        # Try PKCS7 unpad
                        try:
                            decrypted = unpad(decrypted, 16, style='pkcs7')
                        except ValueError:
                            decrypted = decrypted.rstrip(b'\x00')

                        key = _try_import(decrypted)
                        if key:
                            logger.info(f"AES key found: {key_name}/{iv_name}/{mode_name}")
                            return key
                    except Exception as e:
                        logger.debug(f"  {key_name}/{iv_name}/{mode_name}: {e}")

                # --- Stream modes (no padding needed) ---
                for mode_name, make_cipher in [
                    ("CTR", lambda: AES.new(key_bytes, AES.MODE_CTR, nonce=iv[:8])),
                    ("CFB", lambda: AES.new(key_bytes, AES.MODE_CFB, iv=iv)),
                    ("OFB", lambda: AES.new(key_bytes, AES.MODE_OFB, iv=iv)),
                ]:
                    try:
                        decrypted = make_cipher().decrypt(ct)
                        key = _try_import(decrypted)
                        if key:
                            logger.info(f"AES key found: {key_name}/{iv_name}/{mode_name}")
                            return key
                    except Exception as e:
                        logger.debug(f"  {key_name}/{iv_name}/{mode_name}: {e}")

        # ── Strategy D: XOR with pp2 ──
        try:
            xor_result = bytes(pp1_bytes[i] ^ raw_pp2[i % len(raw_pp2)]
                               for i in range(len(pp1_bytes)))
            key = _try_import(xor_result)
            if key:
                logger.info("RSA key found via XOR with pp2.")
                return key
        except Exception as e:
            logger.debug(f"XOR failed: {e}")

        raise RuntimeError(
            f"Could not obtain RSA key from pp1/pp2. "
            f"pp1 decoded len={len(pp1_bytes)}, pp2 len={len(pp2)}, "
            f"pp1 first 8 bytes hex={pp1_bytes[:8].hex()}, "
            f"keys tried: {[k[0] for k in candidate_keys]}. "
            f"Response data keys: {list(resp_data.keys())}. "
            f"Consider providing your ndus cookie directly in config.json."
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
