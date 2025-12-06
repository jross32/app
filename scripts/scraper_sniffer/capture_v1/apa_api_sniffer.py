# apa_api_sniffer.py
"""
Selenium-based APA API sniffer.

Pipeline:

1. Choose URL source:
   - One of the runs from apa_url_discovery/<run>/all_urls.txt
   - OR a manual seed file: apa_api_captures/seed_urls.txt

2. Each URL file can contain filter comments at the top (or anywhere):

   # FILTER_INCLUDE: example-league-site.com
   # FILTER_INCLUDE: /thursday/
   # FILTER_EXCLUDE: /Account/Login
   # FILTER_EXCLUDE: /standings/old/

   - FILTER_INCLUDE: only keep URLs containing at least one include substring
   - FILTER_EXCLUDE: drop URLs containing any exclude substring
   - Non-comment lines are treated as actual URLs.

3. Script behavior:
   - Load URLs from chosen file and apply filters.
   - Load APA_EMAIL / APA_PASSWORD from .env (fallback to CLI).
   - Headless toggle via APA_HEADLESS=1 (same as apa_url_discovery.py).
   - Start Chrome with CDP hook so sniffer JS runs on every new document.
   - Login with retries (env → CLI → manual if not headless).
   - For each page:
       - driver.get(url) with retry on timeout.
       - If bounced to login, re-auth and reload once.
       - Wait a bit for APIs to fire.
       - Pull window.__capturedRequests.
       - Keep only poolplayers.com JSON / GraphQL-ish responses.
   - Save all captured calls to apa_api_captures/<RUN_ID>/api_dump_v2.json

New:
   - Pause/Resume via keypress (Windows):
       - Press 'p' to pause
       - While paused:
           - 'r' to resume
           - 'q' to quit & save (graceful KeyboardInterrupt)
"""

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime
from getpass import getpass

from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
    JavascriptException,
)

# msvcrt is Windows-only (for non-blocking key reads)
try:
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    msvcrt = None
    HAS_MSVCRT = False

load_dotenv()

# Headless toggle (same pattern as apa_url_discovery.py)
HEADLESS = os.getenv("APA_HEADLESS", "0") == "1"

APA_LOGIN_URL = "https://accounts.poolplayers.com/login"

# Where apa_url_discovery.py writes its runs
DISCOVERY_BASE_DIR = Path("apa_url_discovery")

# Where this script writes its own runs
API_BASE_DIR = Path("apa_api_captures")
API_BASE_DIR.mkdir(exist_ok=True)

RUN_ID = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
RUN_DIR = API_BASE_DIR / RUN_ID
RUN_DIR.mkdir(parents=True, exist_ok=True)

API_DUMP_FILE = RUN_DIR / "api_dump_v2.json"
URLS_USED_FILE = RUN_DIR / "page_urls_used.txt"

print(f"APA API Sniffer (Selenium) – Capturing JSON / GraphQL calls from APA pages")
print(f"HEADLESS mode: {HEADLESS}")
print(f"Output run folder: {RUN_DIR}\n")


# =====================================================
# SIMPLE SPINNER
# =====================================================

def start_spinner(label="[WAIT]"):
    """
    Simple console spinner that prints a dot-bar while some operation runs.
    Returns a callable `stopper()` that stops the spinner and optionally
    prints a final message.
    """
    from threading import Thread, Event

    stop_event = Event()

    def spin():
        while not stop_event.is_set():
            for i in range(1, 11):
                if stop_event.is_set():
                    break
                bar = "[" + "." * i + " " * (10 - i) + "]"
                print(f"\r{label} {bar}", end="", flush=True)
                time.sleep(0.2)
        print("\r" + " " * (len(label) + 20) + "\r", end="", flush=True)

    t = Thread(target=spin, daemon=True)
    t.start()

    def stopper(done_message=None):
        stop_event.set()
        time.sleep(0.05)
        if done_message:
            print(done_message)

    return stopper


# =====================================================
# URL FILTERING
# =====================================================

def is_login_url(url: str) -> bool:
    """Return True if this looks like an APA login URL."""
    if not url:
        return False
    url = url.lower()
    return "accounts.poolplayers.com" in url and "/login" in url


def is_poolplayers_url(url: str) -> bool:
    """Simple domain check for poolplayers.com URLs."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    return host.endswith("poolplayers.com")


def parse_url(line: str) -> str:
    """Trim and return a URL if it looks usable, else ''."""
    line = line.strip()
    if not line or line.startswith("#"):
        return ""
    return line


def apply_filters(urls, includes, excludes):
    """
    Apply FILTER_INCLUDE / FILTER_EXCLUDE rules.

    - includes: list of substrings; if non-empty → keep only URLs containing at least one.
    - excludes: list of substrings; always drop URLs containing any exclude substring.
    """
    filtered = []
    for url in urls:
        if excludes and any(x in url for x in excludes):
            continue
        if includes and not any(x in url for x in includes):
            continue
        filtered.append(url)
    return filtered


def parse_url_file(path: Path):
    """
    Parse a URL file, which may contain FILTER_INCLUDE / FILTER_EXCLUDE comments.

    Returns: (urls, includes, excludes)
    """
    includes = []
    excludes = []
    urls = []

    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw:
            continue

        if raw.startswith("#"):
            # filter directives
            if raw.upper().startswith("# FILTER_INCLUDE:"):
                inc = raw.split(":", 1)[1].strip()
                if inc:
                    includes.append(inc)
            elif raw.upper().startswith("# FILTER_EXCLUDE:"):
                exc = raw.split(":", 1)[1].strip()
                if exc:
                    excludes.append(exc)
            continue

        url = parse_url(raw)
        if url:
            urls.append(url)

    return urls, includes, excludes


# =====================================================
# PAUSE / RESUME HANDLER (mirrors apa_url_discovery)
# =====================================================

def check_for_pause():
    """
    Non-blocking pause/resume handler using msvcrt on Windows.
    - Press 'p' to pause
    - While paused:
        - Press 'r' to resume
        - Press 'q' to quit and save (raises KeyboardInterrupt)

    Note: This listens for plain 'p', 'r', 'q' keys (no Ctrl+ combos).
    """
    if not HAS_MSVCRT:
        return

    if not msvcrt.kbhit():
        return

    ch = msvcrt.getch().lower()
    if ch != b'p':
        return

    print("\n⏸️  Pause requested. Press 'R' to resume or 'Q' to quit and save.\n")
    while True:
        c = msvcrt.getch().lower()
        if c == b'r':
            print("▶️  Resuming API sniff.\n")
            return
        elif c == b'q':
            print("🛑 Quit requested by user (Q).")
            raise KeyboardInterrupt


# =====================================================
# CREDENTIAL HANDLING (ENV + CLI)
# =====================================================

def prompt_for_credentials(current_email=None):
    """Ask user for email/password via CLI."""
    print("\n🔐 Please enter APA login credentials for API sniffer.")
    if current_email:
        email_prompt = f"APA email [{current_email}]: "
    else:
        email_prompt = "APA email: "

    email = input(email_prompt).strip()
    if not email:
        email = (current_email or "").strip()

    if not email:
        raise RuntimeError("No APA email provided; cannot continue.")

    password = getpass("APA password: ")
    if not password:
        raise RuntimeError("No APA password provided; cannot continue.")

    return email, password


APA_EMAIL = os.getenv("APA_EMAIL", "").strip()
APA_PASSWORD = os.getenv("APA_PASSWORD", "")


def ensure_global_credentials():
    """Make sure APA_EMAIL / APA_PASSWORD globals are populated."""
    global APA_EMAIL, APA_PASSWORD
    if not APA_EMAIL or not APA_PASSWORD:
        APA_EMAIL, APA_PASSWORD = prompt_for_credentials(APA_EMAIL)


# =====================================================
# SELENIUM SETUP & LOGIN (inspired by apa_url_discovery)
# =====================================================

def start_driver():
    options = Options()
    options.add_argument("--window-size=1400,900")
    options.add_argument("--disable-blink-features=AutomationControlled")

    if HEADLESS:
        options.add_argument("--headless=new")

    prefs = {
        "profile.default_content_setting_values.notifications": 2
    }
    options.add_experimental_option("prefs", prefs)

    service = Service()
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(30)
    return driver


def looks_like_login_dom(driver):
    """Best-effort check for login-like DOM (username/password fields)."""
    try:
        html = driver.page_source.lower()
    except WebDriverException:
        return False

    return ("password" in html and "email" in html) or ("login" in html and "password" in html)


def login_once(driver, email, password):
    """Perform a single login attempt on the current page."""
    try:
        wait = WebDriverWait(driver, 20)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    except TimeoutException:
        print("[AUTH] Timeout waiting for login page body.")
        return False

    try:
        email_field = driver.find_element(By.NAME, "email")
        password_field = driver.find_element(By.NAME, "password")
    except Exception:
        # Try by id as well
        try:
            email_field = driver.find_element(By.ID, "email")
            password_field = driver.find_element(By.ID, "password")
        except Exception:
            print("[AUTH] Could not find email/password fields on page.")
            return False

    email_field.clear()
    email_field.send_keys(email)
    password_field.clear()
    password_field.send_keys(password)

    # Try to submit form
    try:
        password_field.submit()
    except Exception:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, "button[type=submit], button[type=button]")
            btn.click()
        except Exception:
            print("[AUTH] Could not find a submit button. Please login manually.")
            return False

    # Wait for redirect or DOM change
    try:
        time.sleep(3)
    except Exception:
        pass

    current = (driver.current_url or "").lower()
    if is_login_url(current) or looks_like_login_dom(driver):
        print("[AUTH] Still looks like login page after submit; please check credentials.")
        return False

    return True


def login_with_retries(driver, max_attempts=2):
    """Try env credentials, then CLI, then manual if still stuck."""
    ensure_global_credentials()

    for attempt in range(max_attempts):
        print(f"[AUTH] Attempt {attempt + 1}/{max_attempts} using env/CLI credentials...")
        try:
            driver.get(APA_LOGIN_URL)
        except TimeoutException:
            print("[AUTH] Timeout loading login page; retrying...")
            continue

        ok = login_once(driver, APA_EMAIL, APA_PASSWORD)
        if ok:
            print("[AUTH] Login successful via env/CLI.\n")
            return True

    # If still looks like login, ask user to log in manually (if not headless)
    try:
        url_now = (driver.current_url or "").lower()
    except Exception:
        url_now = ""

    if HEADLESS:
        print("[AUTH] Headless mode and automated login failed; cannot continue.")
        return False

    print("[AUTH] Automated login failed. Please log in manually in the browser.")
    print("       Press Enter here when you're done.")
    input(">> ")

    # Quick check again
    try:
        url_now = (driver.current_url or "").lower()
    except Exception:
        url_now = ""

    if is_login_url(url_now) or looks_like_login_dom(driver):
        print("[AUTH] Still looks like login; proceeding anyway (may capture nothing).")
        return False

    print("[AUTH] Manual login appears to have succeeded.\n")
    return True


def ensure_logged_in(driver):
    """
    Called after we detect we've been bounced to login.
    Re-uses env/CLI credentials, then gives the user a chance to fix manually.
    """
    print("[AUTH] Re-login flow triggered...")

    ensure_global_credentials()
    try:
        driver.get(APA_LOGIN_URL)
    except TimeoutException:
        print("[AUTH] Timeout loading login page during re-login.")
        return False

    if login_once(driver, APA_EMAIL, APA_PASSWORD):
        print("[AUTH] Re-login via env/CLI succeeded.")
    else:
        print("[AUTH] Automated re-login failed. Please log in manually.")
        if not HEADLESS:
            print("       Press Enter here when you're done.")
            input(">> ")
        else:
            print("[AUTH] Headless mode and re-login failed; continuing anyway may capture nothing.")
            return False

    # Quick check
    url_now = (driver.current_url or "").lower()
    if is_login_url(url_now) or looks_like_login_dom(driver):
        print("[AUTH] Still appears to be a login page after re-login.")
        return False

    return True


def dismiss_member_popup_once(driver):
    """
    Some APA pages show a 'member' popup after login.
    Try to close it once, ignore failures.
    """
    try:
        time.sleep(2)
        html = driver.page_source.lower()
        if "member" not in html and "dismiss" not in html:
            return

        # naive click on a close button
        candidates = driver.find_elements(By.CSS_SELECTOR, "button, div[role=button]")
        for el in candidates:
            txt = el.text.lower().strip()
            if "close" in txt or "dismiss" in txt or "✕" in txt:
                try:
                    el.click()
                    print("[POPUP] Dismissed a member popup.")
                    break
                except Exception:
                    continue
    except Exception:
        # Not fatal
        pass


# =====================================================
# JAVASCRIPT INJECTION FOR NETWORK CAPTURE
# =====================================================

SNIFFER_JS = r"""
// Basic hook into fetch() and XMLHttpRequest to capture JSON / GraphQL-ish payloads
(function() {
  if (window.__capturedRequests) {
    return;
  }
  window.__capturedRequests = [];

  function recordRequest(data) {
    try {
      window.__capturedRequests.push(data);
    } catch (e) {
      console.error("Failed to push captured request:", e);
    }
  }

  function wrapFetch(originalFetch) {
    return function(input, init) {
      var url = (typeof input === 'string') ? input : (input && input.url) || '';
      var method = (init && init.method) || 'GET';
      var body = (init && init.body) || null;

      var start = Date.now();
      return originalFetch(input, init).then(function(response) {
        var clone = response.clone();
        clone.text().then(function(text) {
          var status = response.status;
          var headers = {};
          try {
            response.headers.forEach(function(value, key) {
              headers[key] = value;
            });
          } catch (e) {}

          recordRequest({
            type: "fetch",
            url: url,
            method: method,
            status: status,
            durationMs: Date.now() - start,
            requestBody: body,
            responseBody: text,
            headers: headers
          });
        }).catch(function(e) {
          console.error("Error reading fetch response text:", e);
        });
        return response;
      });
    }
  }

  function wrapXHR() {
    var XHR = window.XMLHttpRequest;
    if (!XHR) return;

    var open = XHR.prototype.open;
    var send = XHR.prototype.send;

    XHR.prototype.open = function(method, url) {
      this.__method = method;
      this.__url = url;
      return open.apply(this, arguments);
    };

    XHR.prototype.send = function(body) {
      var xhr = this;
      var start = Date.now();

      this.addEventListener("loadend", function() {
        var status = xhr.status;
        var headers = {};
        try {
          var rawHeaders = xhr.getAllResponseHeaders().split("\\r\\n");
          rawHeaders.forEach(function(line) {
            var parts = line.split(":");
            if (parts.length >= 2) {
              var key = parts[0].trim();
              var val = parts.slice(1).join(":").trim();
              if (key) headers[key.toLowerCase()] = val;
            }
          });
        } catch (e) {}

        recordRequest({
          type: "xhr",
          url: xhr.__url || "",
          method: xhr.__method || "GET",
          status: status,
          durationMs: Date.now() - start,
          requestBody: body,
          responseBody: xhr.responseText,
          headers: headers
        });
      });

      return send.apply(this, arguments);
    };
  }

  if (window.fetch) {
    window.fetch = wrapFetch(window.fetch);
  }
  wrapXHR();
})();
"""


def inject_sniffer_on_new_document(driver):
    """
    Use Chrome DevTools Protocol (CDP) to inject SNIFFER_JS on every new document.
    """
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": SNIFFER_JS}
        )
        print("[CDP] Sniffer JS will be injected on every new document.")
    except Exception as e:
        print(f"[CDP] Failed to register sniffer JS: {e}")


def pull_and_clear_captured_requests(driver):
    """
    Pull the window.__capturedRequests array and then clear it.
    Returns a Python list of recorded request dicts.
    """
    try:
        script = """
        var reqs = window.__capturedRequests || [];
        var copy = reqs.slice();
        window.__capturedRequests = [];
        return copy;
        """
        data = driver.execute_script(script)
        if not isinstance(data, list):
            return []
        return data
    except JavascriptException:
        return []
    except WebDriverException:
        return []


# =====================================================
# URL SOURCE SELECTION (AUTO-LATEST)
# =====================================================

def create_seed_template(seed_path: Path):
    """
    Create a minimal seed file template for manual seeding if none exists.
    """
    if seed_path.exists():
        return
    seed_path.write_text(
        "# Manual seed URLs for apa_api_sniffer.py\n"
        "# You can add FILTER_INCLUDE / FILTER_EXCLUDE directives here.\n"
        "# Example:\n"
        "# FILTER_INCLUDE: /thursday/\n"
        "# FILTER_EXCLUDE: /standings/old/\n"
        "# https://league.poolplayers.com/RioGrandeValley/team/12345678\n",
        encoding="utf-8",
    )
    print(f"\nCreated seed URL file template at: {seed_path}\n")


def choose_url_source():
    """
    Auto-select the latest URL source file used for APA API sniffing.

    Behavior:
      - If there are apa_url_discovery runs under DISCOVERY_BASE_DIR,
        automatically pick the latest run's all_urls.txt (no interactive prompt).
      - Otherwise, fall back to a manual seed file: apa_api_captures/seed_urls.txt.

    Returns (selected_file_path: Path).
    """
    discovery_files = []

    if DISCOVERY_BASE_DIR.exists():
        for sub in DISCOVERY_BASE_DIR.iterdir():
            if not sub.is_dir():
                continue
            candidate = sub / "all_urls.txt"
            if candidate.exists():
                discovery_files.append(candidate)

    seed_file = API_BASE_DIR / "seed_urls.txt"

    if discovery_files:
        discovery_files.sort()
        latest = discovery_files[-1]
        print(f"Found {len(discovery_files)} discovery run(s) in {DISCOVERY_BASE_DIR}.")
        print(f"Auto-selecting latest discovery run: {latest.parent.name}")
        print(f"Using URL file: {latest}\n")
        return latest

    # No discovery runs → seed file fallback
    print(f"No apa_url_discovery runs found in {DISCOVERY_BASE_DIR}.")
    print(f"Using manual seed file: {seed_file}")

    if not seed_file.exists():
        create_seed_template(seed_file)

    return seed_file


def load_page_urls():
    """
    Choose a URL source file, parse it, apply filters, and return URLs.

    Returns: (urls, source_file)
    """
    source_file = choose_url_source()
    print(f"\nUsing URL file: {source_file}\n")

    urls_raw, includes, excludes = parse_url_file(source_file)
    if not urls_raw:
        raise RuntimeError(f"No URLs found in {source_file} (after ignoring comments).")

    urls_filtered = apply_filters(urls_raw, includes, excludes)

    if not urls_filtered:
        raise RuntimeError(
            f"All URLs were removed by filters in {source_file}. "
            f"Adjust FILTER_INCLUDE / FILTER_EXCLUDE lines or add more URLs."
        )

    return urls_filtered, source_file


# =====================================================
# MAIN
# =====================================================

def main():
    page_urls, source_file = load_page_urls()
    print(f"Total URLs to visit: {len(page_urls)} (source: {source_file})")

    # Persist URLs we actually used into the run folder
    URLS_USED_FILE.write_text(
        "\n".join(page_urls),
        encoding="utf-8"
    )

    driver = start_driver()
    all_records = []
    interrupted = False

    try:
        # Install sniffer on every new document BEFORE login
        inject_sniffer_on_new_document(driver)

        # Login + popup handling
        if not login_with_retries(driver):
            print("⚠️ Could not log in; API sniffing will likely be empty.")
        else:
            dismiss_member_popup_once(driver)

        try:
            for idx, url in enumerate(page_urls, start=1):
                # Allow user to pause/resume/quit between pages
                check_for_pause()

                print(f"\n=== Visiting page {idx}/{len(page_urls)}: {url} ===")

                # Load page with simple retry + spinner
                loaded = False
                for attempt in range(2):
                    try:
                        stop = start_spinner("[WAIT] Loading page")
                        try:
                            driver.get(url)
                        finally:
                            stop()
                        loaded = True
                        break
                    except TimeoutException:
                        print(f"   [TIMEOUT] Page load timed out for {url} (attempt {attempt + 1}).")
                        if attempt == 0:
                            print("   [LOAD] Retrying page load one more time...")
                    except Exception as e:
                        print(f"   [WARN] Failed to load {url} (attempt {attempt + 1}): {e}")
                        if attempt == 0:
                            print("   [LOAD] Retrying page load one more time...")

                if not loaded:
                    print("   [SKIP] Giving up on this URL due to repeated load failures.")
                    continue

                # If bounced to login, re-auth then reload once
                if is_login_url(driver.current_url) or looks_like_login_dom(driver):
                    ok = ensure_logged_in(driver)
                    if not ok:
                        print("   [AUTH] Skipping this URL due to failed re-login.")
                        continue

                    try:
                        stop = start_spinner("[WAIT] Reloading target page after re-login")
                        try:
                            driver.get(url)
                        finally:
                            stop()
                    except Exception as e:
                        print(f"   [AUTH] After reauth, failed to reload {url}: {e}")
                        continue

                    if is_login_url(driver.current_url) or looks_like_login_dom(driver):
                        print("   [AUTH] Still seeing login after reauth; skipping this URL.")
                        continue

                # Give the page some time to fire its APIs
                time.sleep(5)

                page_url = driver.current_url
                print(f"   Landed on: {page_url}")

                # Pull & clear captured calls *after* this page
                page_requests = pull_and_clear_captured_requests(driver)
                print(f"   Captured {len(page_requests)} raw requests on this page.")

                # Filter for JSON/GraphQL-ish content
                kept_here = 0
                for rec in page_requests:
                    api_url = rec.get("url") or ""
                    if not api_url:
                        continue

                    if not is_poolplayers_url(api_url):
                        continue

                    # Try to keep only JSON / GraphQL calls
                    headers = rec.get("headers", {}) or {}
                    ct = headers.get("content-type", "").lower()

                    if "application/json" not in ct and "graphql" not in ct:
                        # Also allow explicit /graphql path
                        if "/graphql" not in api_url.lower():
                            continue

                    record = {
                        "page_url": page_url,
                        "api_url": api_url,
                        "type": rec.get("type"),
                        "method": rec.get("method"),
                        "status": rec.get("status"),
                        "request_body": rec.get("requestBody"),
                        "response_body": rec.get("responseBody"),
                        "headers": rec.get("headers", {}),
                    }
                    all_records.append(record)
                    kept_here += 1
                    print(f"   [+] API: {record['method']} {record['api_url']} (status {record['status']})")

                print(f"   [SUMMARY] Kept {kept_here} API calls from this page.")

        except KeyboardInterrupt:
            print("\n\n⛔ KeyboardInterrupt detected (Ctrl+C or Q).")
            interrupted = True

        # Save everything for this run (even if interrupted)
        saver = start_spinner("[SAVE] Writing api_dump_v2.json")
        try:
            API_DUMP_FILE.write_text(
                json.dumps(all_records, indent=2),
                encoding="utf-8"
            )
            time.sleep(0.2)
        finally:
            saver("\n💾 Saved API calls to api_dump_v2.json.\n")

        print(f"   URLs used for this run: {URLS_USED_FILE}")
        print(f"   API dump file        : {API_DUMP_FILE}")

        if interrupted:
            print("\n🛑 API sniffer stopped by user (forced stop or quit).")
            print("   You can re-run apa_api_sniffer.py later to continue from another URL set.")
        else:
            print(f"\n✅ API sniffer completed normally. Total API calls captured: {len(all_records)}")

    finally:
        print("Closing browser...")
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
