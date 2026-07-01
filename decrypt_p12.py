#!/usr/bin/env python3
"""Decrypt the committed Developer ID cert (devid.p12.enc, AES-256-GCM) using the
P12_PASS env var, writing the passwordless .p12 to /tmp/devid.p12 for signing.
The blob is safe to keep in the (public) repo — it's useless without P12_PASS,
which lives only as a Codemagic secret."""
import os, base64
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

blob = base64.b64decode(open("devid.p12.enc", "rb").read())
salt, nonce, ct = blob[:16], blob[16:28], blob[28:]
key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                 iterations=200000).derive(os.environ["P12_PASS"].encode())
open("/tmp/devid.p12", "wb").write(AESGCM(key).decrypt(nonce, ct, None))
print("decrypted p12 OK ->", os.path.getsize("/tmp/devid.p12"), "bytes")
