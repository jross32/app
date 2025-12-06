"""
apa_url_discovery.py
Selenium URL discovery with:
- APA login (env or CLI)
- Headless support
- Mid-crawl re-auth (URL + DOM based login detection)
- BFS crawling of internal URLs
- Per-run output folder
- Timeout + retry on page loads
- Pause/Resume via keypress (P to pause, R to resume, Q to quit+save)
- Graceful Ctrl+C handling with progress save & status
- Animated [..........] style dot-bar during long waits (page loads, saving)
"""

import os
import re
import json
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse, urljoin
from getpass import getpass
from datetime import datetime
from threading import Thread, Event

from dotenv import load_dotenv
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from webdriver_manager.chrome import ChromeDriverManager

# Optional Windows-only keyboard handling (for pause/resume)
try:
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False


# =====================================================
# CONFIG
# =====================================================

load_dotenv()

APA_EMAIL = os.getenv("APA_EMAIL") or ""
APA_PASSWORD = os.getenv("APA_PASSWORD") or ""

# Headless toggle:
#   Set APA_HEADLESS=1 in env to enable headless
HEADLESS = os.getenv("APA_HEADLESS", "0") == "1"

START_URLS = [
    "https://league.poolplayers.com/RioGrandeValley",
]

ALLOWED_DOMAINS = {
    "league.poolplayers.com",
}

# ---- Per-run output directory ----
BASE_DIR = Path("apa_url_discovery")
BASE_DIR.mkdir(exist_ok=True)

RUN_ID = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
OUT_DIR = BASE_DIR / RUN_ID
OUT_DIR.mkdir(parents=True, exist_ok=True)

OUT_ALL_URLS = OUT_DIR / "all_urls.txt"
OUT_GRAPH = OUT_DIR / "url_graph.json"
OUT_FAILED = OUT_DIR / "failed_urls.json"

SCREENSHOT_DIR = OUT_DIR / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

DANGEROUS_KEYWORDS = [
    "logout", "log-out", "signout", "sign-out",
    "delete", "remove", "cancel", "unenroll",
    "pay", "payment", "billing",
]

ONCLICK_URL_PATTERN = re.compile(
    r"""['"](https?://[^'"]+|/[^'"]+)['"]""",
    re.IGNORECASE,
)


# =====================================================
# SPINNER / DOT-BAR ANIMATION
# =====================================================

def start_spinner(label="[WAIT]"):
    """
    Start a background thread that draws a [..........] style bar
    over and over until stop_spinner() is called.
    Returns the Event used to stop it.
    """
    stop_event = Event()

    def spin():
        # Make sure label + bar overwrite nicely
        while not stop_event.is_set():
            for i in range(1, 11):
                if stop_event.is_set():
                    break
                bar = "[" + "." * i + " " * (10 - i) + "]"
                print(f"\r{label} {bar}", end="", flush=True)
                time.sleep(0.2)
            # loop: bar "restarts" at [.] again

        # clear the line when stopping
        print("\r" + " " * (len(label) + 20) + "\r", end="", flush=True)

    t = Thread(target=spin, daemon=True)
    t.start()
    return stop_event


def stop_spinner(stop_event, done_message=None):
    """Stop the spinner started by start_spinner()."""
    if stop_event is None:
        return
    stop_event.set()
    # tiny sleep so thread can clear line
    time.sleep(0.05)
    if done_message:
        print(done_message)


# =====================================================
# CREDENTIAL HANDLING (ENV + CLI)
# =====================================================

def prompt_for_credentials(current_email=None):
    """Ask user for email/password via CLI (works headless or not)."""
    print("\n🔐 Please enter APA login credentials.")
    if current_email:
        email_prompt = f"APA email [{current_email}]: "
    else:
        email_prompt = "APA email: "

    email = input(email_prompt).strip()
    if not email:
        email = (current_email or "").strip()

    password = getpass("APA password (input hidden): ").strip()

    if not email or not password:
        raise RuntimeError("Email and password are required.")

    return email, password


def ensure_global_credentials():
    """Make sure APA_EMAIL / APA_PASSWORD globals are populated."""
    global APA_EMAIL, APA_PASSWORD
    if not APA_EMAIL or not APA_PASSWORD:
        APA_EMAIL, APA_PASSWORD = prompt_for_credentials(APA_EMAIL)


# =====================================================
# SELENIUM SETUP & LOGIN
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

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    # Set page-load timeout in seconds (for "infinite load" protection)
    driver.set_page_load_timeout(40)

    return driver


def login_once(driver, email, password):
    """
    Perform a single login attempt with given credentials.
    No prompting, no retries. Higher-level code decides what to do.
    """
    APA_LOGIN_URL = "https://accounts.poolplayers.com/login"

    print(f"\n[AUTH] Opening login page as {email} ...")
    spinner = start_spinner("[WAIT] Loading login page")
    try:
        driver.get(APA_LOGIN_URL)
    finally:
        stop_spinner(spinner)

    wait = WebDriverWait(driver, 20)
    time.sleep(3)  # allow JS/React to render

    print("[AUTH] Locating email field...")
    email_box = wait.until(
        EC.presence_of_element_located((
            By.CSS_SELECTOR,
            "input[type='email'], input[name*='email' i], input[id*='email' i]"
        ))
    )

    print("[AUTH] Locating password field...")
    pass_box = wait.until(
        EC.presence_of_element_located((
            By.CSS_SELECTOR,
            "input[type='password'], input[name*='pass' i], input[id*='pass' i]"
        ))
    )

    print("[AUTH] Typing email/password...")
    email_box.clear()
    email_box.send_keys(email)
    pass_box.clear()
    pass_box.send_keys(password)

    print("[AUTH] Searching for login button...")
    candidates = [
        (By.XPATH, "//button[contains(., 'Log In')]"),
        (By.XPATH, "//button[contains(., 'LOG IN')]"),
        (By.XPATH, "//button[contains(., 'Sign in')]"),
        (By.XPATH, "//button[contains(., 'SIGN IN')]"),
        (By.CSS_SELECTOR, "button[type='submit']"),
    ]

    login_button = None
    for by, sel in candidates:
        try:
            login_button = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((by, sel))
            )
            print(f"[AUTH] Found login button via {by} {sel}")
            break
        except TimeoutException:
            continue

    if login_button:
        print("[AUTH] Clicking login button...")
        login_button.click()
    else:
        print("[AUTH] Could not auto-find login button on page.")

    print("[AUTH] Waiting after login attempt...")
    spinner = start_spinner("[WAIT] Processing login")
    time.sleep(5)
    stop_spinner(spinner)

    # Post-login "Continue" button for some accounts
    try:
        continue_btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//button[contains(., 'Continue') or contains(., 'CONTINUE')]"
            ))
        )
        print("[AUTH] Clicking 'Continue' button...")
        continue_btn.click()
        spinner = start_spinner("[WAIT] Continuing")
        time.sleep(3)
        stop_spinner(spinner)
    except TimeoutException:
        print("[AUTH] No 'Continue' button detected or already redirected.")


def is_login_url(url):
    """
    Broad auth-page detection based on URL alone.
    Catches things like:
      - https://accounts.poolplayers.com/login
      - https://accounts.poolplayers.com/login?redirect_uri=...
      - https://league.poolplayers.com/login/destroy
      - other poolplayers.com URLs with 'login' in them
    """
    url = (url or "").lower()
    if "poolplayers.com" not in url:
        return False
    return "login" in url


def looks_like_login_dom(driver):
    """
    DOM heuristic: does the current page *look* like a login form?
    - Has a password field
    - Has an email/username-like field
    - Has a 'log in' / 'sign in' button
    """
    try:
        page_html = driver.page_source.lower()
    except Exception:
        return False

    if "log in" not in page_html and "login" not in page_html and "sign in" not in page_html:
        return False

    # password field
    try:
        driver.find_element(
            By.CSS_SELECTOR,
            "input[type='password'], input[name*='pass' i], input[id*='pass' i]"
        )
    except Exception:
        return False

    # email or username field
    try:
        driver.find_element(
            By.CSS_SELECTOR,
            "input[type='email'], input[name*='email' i], input[id*='email' i]"
        )
    except Exception:
        try:
            driver.find_element(
                By.CSS_SELECTOR,
                "input[name*='user' i], input[id*='user' i]"
            )
        except Exception:
            return False

    # button text
    try:
        buttons = driver.find_elements(By.TAG_NAME, "button")
        for btn in buttons:
            text = (btn.text or "").strip().lower()
            if any(kw in text for kw in ("log in", "login", "sign in", "sign-in")):
                return True
    except Exception:
        pass

    return False


def login_with_retries(driver):
    """
    High-level login sequence:
    1. Use env/known credentials
    2. If still looks like login, prompt for CLI credentials and try again
    3. If still looks like login (and not headless), let user log in manually
    Returns:
        True  -> looks logged in (not login page)
        False -> still login page; caller decides to skip or abort
    """
    global APA_EMAIL, APA_PASSWORD

    ensure_global_credentials()

    # 1) Try with current globals
    login_once(driver, APA_EMAIL, APA_PASSWORD)

    url_now = (driver.current_url or "").lower()
    if not (is_login_url(url_now) or looks_like_login_dom(driver)):
        print("[AUTH] Logged in successfully with current credentials.\n")
        return True

    print("[AUTH] Still on login page after env-based login attempt.")
    print("[AUTH] Will prompt for credentials via CLI.\n")

    # 2) CLI credentials
    try:
        APA_EMAIL, APA_PASSWORD = prompt_for_credentials(APA_EMAIL)
    except RuntimeError as e:
        print(f"[AUTH] {e}")
        return False

    login_once(driver, APA_EMAIL, APA_PASSWORD)

    url_now = (driver.current_url or "").lower()
    if not (is_login_url(url_now) or looks_like_login_dom(driver)):
        print("[AUTH] Logged in successfully with CLI credentials.\n")
        return True

    print("[AUTH] Still looks like a login page after CLI credentials.")

    # 3) Manual browser login if not headless
    if HEADLESS:
        print("[AUTH] Headless mode: cannot do manual browser login.")
        return False

    print("⚠️  Please log in manually in the browser window.")
    input("    After logging in successfully, press Enter here to continue... ")
    spinner = start_spinner("[WAIT] Verifying manual login")
    time.sleep(3)
    stop_spinner(spinner)

    url_now = (driver.current_url or "").lower()
    if is_login_url(url_now) or looks_like_login_dom(driver):
        print("[AUTH] Still appears to be a login page even after manual login.")
        return False

    print("[AUTH] Manual login successful.\n")
    return True


def ensure_logged_in(driver):
    """
    For mid-crawl:
    If page looks like login (by URL or DOM), pause scraping and attempt re-login.
    Returns:
        True  -> we believe we're logged in now
        False -> still appears as login; caller should skip this URL
    """
    url = driver.current_url or ""
    if not (is_login_url(url) or looks_like_login_dom(driver)):
        return True

    print("\n===============================")
    print("⚠️  Detected login page / logged out mid-crawl.")
    print("    Current URL: {}".format(url))
    print("    Scraping paused – re-login sequence starting...")
    print("===============================\n")

    ok = login_with_retries(driver)

    if not ok:
        print("[AUTH] Re-login failed; remaining on login page. This URL will be skipped.\n")
        return False

    # one more sanity check
    url_after = driver.current_url or ""
    if is_login_url(url_after) or looks_like_login_dom(driver):
        print("[AUTH] Still appears to be a login page after re-login. Skipping this URL.\n")
        return False

    print("[AUTH] Re-login successful. Resuming crawl.\n")
    return True


# =====================================================
# URL + DOM HELPERS
# =====================================================

def normalize_url(url):
    if not url:
        return url
    parsed = urlparse(url)
    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    normalized = parsed._replace(path=path).geturl()
    return normalized


def is_internal_url(url):
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https", ""):
        return False
    if not parsed.netloc:
        return True
    return parsed.netloc in ALLOWED_DOMAINS


def is_safe_url(url):
    if not url:
        return False
    lower = url.lower()

    if "accounts.poolplayers.com/login" in lower:
        return False

    if "/post/" in lower:
        return False

    if any(bad in lower for bad in DANGEROUS_KEYWORDS):
        return False

    if lower.startswith("mailto:") or lower.startswith("javascript:"):
        return False

    return True


def make_screenshot_name(url, index):
    parsed = urlparse(url)
    raw = (parsed.netloc + parsed.path) or "root"
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", raw).strip("_")
    if not slug:
        slug = "page"
    filename = f"{index:04d}_{slug}.png"
    return SCREENSHOT_DIR / filename


# =====================================================
# PAUSE / RESUME HANDLER
# =====================================================

def check_for_pause():
    """
    Non-blocking pause/resume handler using msvcrt on Windows.
    - Press 'p' to pause
    - While paused:
        - Press 'r' to resume
        - Press 'q' to quit and save (raises KeyboardInterrupt)
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
            print("▶️  Resuming crawl.\n")
            return
        elif c == b'q':
            print("🛑 Quit requested by user (Q).")
            raise KeyboardInterrupt


# =====================================================
# DOM LINK DISCOVERY (per page)
# =====================================================

def discover_links_on_page(html, base_url):
    """
    Parse HTML and pull out possible nav links:
    - <a href="...">
    - elements with data-href / data-url
    - onclick with embedded URLs
    """
    s = BeautifulSoup(html, "html.parser")
    discovered = []

    # 1) <a href="...">
    for a in s.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href:
            continue
        full = urljoin(base_url, href)
        discovered.append(
            {
                "full_url": full,
                "raw": href,
                "source": "a[href]",
                "text": a.get_text(" ", strip=True),
            }
        )

    # 2) data-href / data-url
    for el in s.find_all(True):
        for attr_name in ("data-href", "data-url"):
            if el.has_attr(attr_name):
                raw = str(el.get(attr_name)).strip()
                if raw:
                    full = urljoin(base_url, raw)
                    discovered.append(
                        {
                            "full_url": full,
                            "raw": raw,
                            "source": attr_name,
                            "text": el.get_text(" ", strip=True),
                        }
                    )

    # 3) onclick URLs
    for el in s.find_all(True):
        if not el.has_attr("onclick"):
            continue
        onclick = str(el.get("onclick") or "")
        for match in ONCLICK_URL_PATTERN.findall(onclick):
            raw = match.strip()
            full = urljoin(base_url, raw)
            discovered.append(
                {
                    "full_url": full,
                    "raw": raw,
                    "source": "onclick",
                    "text": el.get_text(" ", strip=True),
                }
            )

    return discovered


# =====================================================
# BFS CRAWLER
# =====================================================

def crawl_all_urls(driver):
    """
    BFS over internal URLs starting from START_URLS.
    Returns:
        url_graph: url -> {url, depth, parents[], discovered_from[]}
        failed: list of {"url", "reason", "details"}
        interrupted: bool (True if KeyboardInterrupt caught)
    """
    url_graph = {}
    visited = set()
    failed = []
    q = deque()

    # seed queue
    for root in START_URLS:
        full = normalize_url(root)
        q.append((full, 0))
        url_graph[full] = {
            "url": full,
            "depth": 0,
            "parents": [],
            "discovered_from": [],
        }

    interrupted = False

    try:
        while q:
            check_for_pause()  # allow user to pause/resume

            current_url, depth = q.popleft()
            if current_url in visited:
                continue
            visited.add(current_url)

            print(f"\n🌐 Visiting [{len(visited)} visited] : {current_url}")

            # Try page load with retries (with spinner)
            loaded = False
            for attempt in range(2):
                try:
                    print(f"   [LOAD] Attempting to load (attempt {attempt + 1})...")
                    spinner = start_spinner("[WAIT] Loading page")
                    try:
                        driver.get(current_url)
                    finally:
                        stop_spinner(spinner)
                    loaded = True
                    break
                except TimeoutException:
                    print(f"   [TIMEOUT] Page load timed out for {current_url} (attempt {attempt + 1}).")
                    if attempt == 0:
                        print("   [LOAD] Retrying page load one more time...")
                except Exception as e:
                    print(f"   [WARN] Failed to load {current_url} (attempt {attempt + 1}): {e}")
                    if attempt == 0:
                        print("   [LOAD] Retrying page load one more time...")

            if not loaded:
                print("   [SKIP] Giving up on this URL due to repeated load failures.")
                failed.append({
                    "url": current_url,
                    "reason": "timeout",
                    "details": "Timed out or failed to load twice."
                })
                continue

            # If we got bounced to login or the page looks like login, pause + reauth
            if is_login_url(driver.current_url) or looks_like_login_dom(driver):
                ok = ensure_logged_in(driver)
                if not ok:
                    print("   [AUTH] Skipping this URL due to failed re-login.")
                    failed.append({
                        "url": current_url,
                        "reason": "auth",
                        "details": "Could not get past login page during re-auth."
                    })
                    continue

                # Try to load target URL again (with spinner)
                try:
                    print("   [LOAD] Reloading target URL after successful re-login...")
                    spinner = start_spinner("[WAIT] Loading page")
                    try:
                        driver.get(current_url)
                    finally:
                        stop_spinner(spinner)
                except TimeoutException:
                    print(f"   [TIMEOUT] Page load timed out for {current_url} after re-login.")
                    failed.append({
                        "url": current_url,
                        "reason": "timeout_after_auth",
                        "details": "Timed out loading page after successful re-auth."
                    })
                    continue
                except Exception as e:
                    print(f"   [AUTH] After reauth, failed to reload {current_url}: {e}")
                    failed.append({
                        "url": current_url,
                        "reason": "load_after_auth",
                        "details": f"Failed to load after re-auth: {e}"
                    })
                    continue

                # If still looks like login, bail on this URL
                if is_login_url(driver.current_url) or looks_like_login_dom(driver):
                    print("   [AUTH] Still seeing login after reauth; skipping this URL.")
                    failed.append({
                        "url": current_url,
                        "reason": "stuck_on_login",
                        "details": "Page still looks like login after re-auth."
                    })
                    continue

            time.sleep(2)

            # Screenshot
            try:
                shot_path = make_screenshot_name(current_url, len(visited))
                driver.save_screenshot(str(shot_path))
                print(f"   [SHOT] Saved screenshot -> {shot_path}")
            except Exception as e:
                print(f"   [SHOT] Failed to capture screenshot: {e}")

            # Parse DOM
            html = driver.page_source
            found = discover_links_on_page(html, base_url=current_url)
            print(f"   [LINKS] Found {len(found)} raw potential links on this page.")

            # Track newly discovered URLs (queue = "raw potential links remaining")
            new_links = 0
            for entry in found:
                full_url = normalize_url(entry["full_url"])
                if not is_internal_url(full_url):
                    continue
                if not is_safe_url(full_url):
                    continue

                node = url_graph.get(full_url)
                if not node:
                    node = {
                        "url": full_url,
                        "depth": depth + 1,
                        "parents": [],
                        "discovered_from": [],
                    }
                    url_graph[full_url] = node
                    q.append((full_url, depth + 1))
                    new_links += 1

                if current_url not in node["parents"]:
                    node["parents"].append(current_url)
                node["discovered_from"].append(
                    {
                        "from": current_url,
                        "source": entry["source"],
                        "raw": entry["raw"],
                        "text": entry.get("text", ""),
                    }
                )

            print(f"   [DISCOVERED] New unique URLs from this page: {new_links}")
            print(f"   [DISCOVERED] Total unique internal URLs so far: {len(url_graph)}")
            print(f"   [QUEUE] Remaining raw potential links (unvisited): {len(q)}")

    except KeyboardInterrupt:
        print("\n\n⛔ KeyboardInterrupt detected (Ctrl+C or Q).")
        interrupted = True

    return url_graph, failed, interrupted


# =====================================================
# MAIN
# =====================================================

def save_results(url_graph, failed, interrupted=False):
    """Save current results to disk with a little '[..........]' style animation."""
    all_urls = sorted(url_graph.keys())

    # animated saving bar
    print("\n💾 Saving crawl results...", end="", flush=True)
    for i in range(10):
        bar = "[" + "." * (i + 1) + " " * (10 - i - 1) + "]"
        print(f"\r💾 Saving crawl results... {bar}", end="", flush=True)
        time.sleep(0.15)
    print("\r💾 Saving crawl results... [##########] ✅")

    OUT_ALL_URLS.write_text("\n".join(all_urls), encoding="utf-8")
    with OUT_GRAPH.open("w", encoding="utf-8") as f:
        json.dump(url_graph, f, indent=2)

    if failed:
        with OUT_FAILED.open("w", encoding="utf-8") as f:
            json.dump(failed, f, indent=2)

    print(f"\n📁 Results saved in: {OUT_DIR}")
    print(f"   URLs list:    {OUT_ALL_URLS}")
    print(f"   URL graph:    {OUT_GRAPH}")
    print(f"   Failed URLs:  {OUT_FAILED}")
    print(f"   Screenshots:  {SCREENSHOT_DIR}")

    if interrupted:
        print("\n🛑 Crawl stopped by user (forced stop or quit).")
    else:
        print(f"\n✅ Crawl completed normally. Total URLs discovered: {len(all_urls)}")


def main():
    print("APA URL Discovery (Selenium) – building site map of league.poolplayers.com\n")
    print(f"HEADLESS mode: {HEADLESS}")
    print(f"Run folder: {OUT_DIR}\n")

    driver = start_driver()
    url_graph = {}
    failed = []
    interrupted = False

    try:
        # Initial login sequence with env + CLI fallback
        print("[AUTH] Starting initial login sequence...")
        ok = login_with_retries(driver)
        if not ok:
            print("[AUTH] Could not log in initially; crawl may be limited or empty.\n")

        url_graph, failed, interrupted = crawl_all_urls(driver)

    finally:
        print("Closing browser...")
        try:
            driver.quit()
        except Exception:
            pass

        # Save whatever we have (partial or full)
        save_results(url_graph, failed, interrupted=interrupted)


if __name__ == "__main__":
    main()
