#!/usr/bin/env python3
"""
Standalone diagnostic for SpankBang login and video download link detection.

Usage:
    .venv/bin/python test_spankbang_login.py "https://fr.spankbang.com/4qa07/video/big+hearts+bigger+loads?"

The browser stays open for 60 seconds after each step so you can inspect.

============================================================
CONTEXT FOR NEXT SESSION — READ THIS BEFORE STARTING
============================================================
Goal: rewrite download_spankbang() in downloadContent.py (around line 2101).

WHAT WE KNOW ABOUT THE PAGE:
- Download trigger:   <div class="dl">  (click this to open modal — no href, JS listener)
- Modal:              <div id="download-remodal" data-remodal-id="download" class="remodal download-remodal">
- Quality buttons:    <p class="pl b_1080p ft-button-bordered" data-download-button="">Download in 1080p quality</p>
                      Classes for resolution: b_4k (2160p), b_1080p, b_720p, b_480p, b_240p
                      These have NO href — JS populates the URL on click.
- Two sections in modal:
    #download-options-modal  (shown when logged in)
    #download-promo          (shown when guest — prompts to log in)

WHAT WE NEED THE TEST TO TELL US (Steps 7-8 output):
After clicking a quality button (<p data-download-button>), the video URL appears via ONE of:
  a) A new browser tab opens with the .mp4 URL
  b) The <p> element's href attribute gets populated (unlikely, it's a <p> not <a>)
  c) A new <a> element with a .mp4 href appears in the modal
  d) The page navigates to the .mp4 URL directly
  e) Some other mechanism (check Step 8 output carefully)

REWRITE PLAN FOR download_spankbang() after test output is known:
  1. driver.get(url)  →  _spankbang_dismiss_age_gate(driver)
  2. Wait for div.dl clickable, click it
  3. Wait for #download-remodal visibility
  4. Check #download-options-modal is shown (not #download-promo) → means logged in
  5. Pick best quality <p data-download-button> by class priority: b_4k > b_1080p > b_720p > b_480p > b_240p
  6. Record original window handles, click the button
  7. Handle URL via whichever mechanism Step 8 reveals:
     - If new tab: switch to it, grab current_url, close tab, _direct_fetch()
     - If .mp4 link appears: grab href, _direct_fetch()
     - etc.

LOGIN BUTTON XPATH FIX ALSO NEEDED in _spankbang_login() (~line 2024):
  Current XPath tries to match class "login" — real button has class "bt_signin auth"
  Use instead: //a[contains(@class,"bt_signin")] or text-based match
============================================================
"""
import sys
import time

from dotenv import load_dotenv
load_dotenv()

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from downloadContent import setup_driver, _get_secret, _spankbang_dismiss_age_gate, _spankbang_normalize_url

VIDEO_URL = _spankbang_normalize_url(sys.argv[1]) if len(sys.argv) > 1 else None
if len(sys.argv) > 1 and VIDEO_URL != sys.argv[1]:
    print(f'URL normalized: {sys.argv[1]}\n           -> {VIDEO_URL}')

import tempfile
driver = setup_driver(tempfile.gettempdir())

# ---------------------------------------------------------------------------
# Step 1: Home page + age gate
# ---------------------------------------------------------------------------
print('\n--- Step 1: navigate to spankbang.com ---')
driver.get('https://spankbang.com/')
time.sleep(2)
_spankbang_dismiss_age_gate(driver)
print(f'Current URL: {driver.current_url}')

# ---------------------------------------------------------------------------
# Step 2: Find the login button
# ---------------------------------------------------------------------------
print('\n--- Step 2: locate login button ---')
xpaths = [
    '//*[@data-remodal-target="auth"]',
    '//*[@href="#auth"]',
    '//*[contains(@class,"login") and not(ancestor::form)]',
    '//button[contains(translate(text(),"LOGIN","login"),"login")]',
    '//a[contains(translate(text(),"LOGIN","login"),"login")]',
]
login_btn = None
for xp in xpaths:
    els = driver.find_elements(By.XPATH, xp)
    visible = [e for e in els if e.is_displayed()]
    if visible:
        login_btn = visible[0]
        print(f'  Found with XPath: {xp}')
        print(f'  tag={login_btn.tag_name}  text="{login_btn.text.strip()}"'
              f'  class="{login_btn.get_attribute("class")}"')
        break
    elif els:
        print(f'  Found (hidden) with XPath: {xp} — tag={els[0].tag_name}')

if login_btn is None:
    print('  No login button found. Printing all <a> and <button> elements:')
    for tag in ('a', 'button'):
        for el in driver.find_elements(By.TAG_NAME, tag)[:20]:
            txt = el.text.strip()[:40]
            cls = (el.get_attribute('class') or '')[:60]
            href = (el.get_attribute('href') or el.get_attribute('data-remodal-target') or '')[:60]
            if txt or href:
                print(f'    <{tag}> text="{txt}"  class="{cls}"  href/target="{href}"')
    print('\nBrowser open 60s — inspect the page.')
    time.sleep(60)
    driver.quit()
    sys.exit(1)

# ---------------------------------------------------------------------------
# Step 3: Click login, fill form
# ---------------------------------------------------------------------------
print('\n--- Step 3: open login modal ---')
driver.execute_script('arguments[0].click()', login_btn)
time.sleep(1.5)

# Look for form fields
print('Searching for username/password fields...')
for fid in ('log_username', 'username', 'email', 'user', 'login'):
    els = driver.find_elements(By.ID, fid)
    if els:
        print(f'  Found field id="{fid}"  displayed={els[0].is_displayed()}')
for fid in ('log_password', 'password', 'pass'):
    els = driver.find_elements(By.ID, fid)
    if els:
        print(f'  Found field id="{fid}"  displayed={els[0].is_displayed()}')

# Dump all visible inputs
print('All visible <input> elements:')
for inp in driver.find_elements(By.TAG_NAME, 'input'):
    if inp.is_displayed():
        print(f'  id="{inp.get_attribute("id")}"  name="{inp.get_attribute("name")}"'
              f'  type="{inp.get_attribute("type")}"'
              f'  placeholder="{inp.get_attribute("placeholder")}"')

username = _get_secret('SPANKBANG_USERNAME').strip()
password = _get_secret('SPANKBANG_PASSWORD').strip()
if not username or not password:
    print('\nNo credentials in keyring/.env — skipping login attempt.')
    print('Browser open 60s.')
    time.sleep(60)
    driver.quit()
    sys.exit(0)

print(f'\nAttempting login as: {username}')
try:
    wait = WebDriverWait(driver, 8)
    user_field = wait.until(EC.visibility_of_element_located((By.ID, 'log_username')))
    user_field.click(); time.sleep(0.2); user_field.clear()
    for c in username:
        user_field.send_keys(c); time.sleep(0.04)

    pw_field = driver.find_element(By.ID, 'log_password')
    pw_field.click(); time.sleep(0.2)
    for c in password:
        pw_field.send_keys(c); time.sleep(0.04)

    time.sleep(0.3)
    login_form = driver.find_element(By.ID, 'auth_login_form')
    submit = login_form.find_element(By.XPATH, './/button[@type="submit"]')
    print(f'  Submit button: text="{submit.text.strip()}"')
    driver.execute_script('arguments[0].click()', submit)
    time.sleep(3)
    print(f'  URL after submit: {driver.current_url}')
except (TimeoutException, WebDriverException) as e:
    print(f'  Login form interaction failed: {e}')
    print('  Browser open 60s — inspect manually.')
    time.sleep(60)
    driver.quit()
    sys.exit(1)

# ---------------------------------------------------------------------------
# Step 4: Verify login
# ---------------------------------------------------------------------------
print('\n--- Step 4: verify login state ---')
modal = driver.find_elements(By.ID, 'auth-remodal')
if modal:
    vis = modal[0].value_of_css_property('visibility')
    print(f'  auth-remodal visibility: {vis}')
profile_els = driver.find_elements(By.XPATH,
    '//*[contains(@class,"user-nav") or contains(@class,"profile-btn") or contains(@href,"/users/")]')
print(f'  Profile/user-nav elements found: {len(profile_els)}')
for el in profile_els[:3]:
    print(f'    tag={el.tag_name}  class="{el.get_attribute("class")}"  href="{el.get_attribute("href")}"')

# ---------------------------------------------------------------------------
# Step 5: Navigate to video URL
# ---------------------------------------------------------------------------
if not VIDEO_URL:
    print('\nNo video URL provided — skipping video step.')
    print('Browser open 60s.')
    time.sleep(60)
    driver.quit()
    sys.exit(0)

print(f'\n--- Step 5: navigate to video: {VIDEO_URL} ---')
driver.get(VIDEO_URL)
time.sleep(3)
_spankbang_dismiss_age_gate(driver)
print(f'  Current URL: {driver.current_url}')

# ---------------------------------------------------------------------------
# Step 6: Find download links
# ---------------------------------------------------------------------------
print('\n--- Step 6: click div.dl to open download modal ---')
try:
    dl_btn = WebDriverWait(driver, 6).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, 'div.dl'))
    )
    print(f'  div.dl found  displayed={dl_btn.is_displayed()}')
    original_handles = set(driver.window_handles)
    driver.execute_script('arguments[0].click()', dl_btn)
    time.sleep(2)
except TimeoutException:
    print('  div.dl not found — printing all divs with short classes:')
    for el in driver.find_elements(By.TAG_NAME, 'div'):
        cls = el.get_attribute('class') or ''
        if cls and len(cls) < 20 and el.is_displayed():
            print(f'    div class="{cls}"')
    print('Browser open 60s.')
    time.sleep(60)
    driver.quit()
    sys.exit(1)

print('\n--- Step 7: check download modal and quality buttons ---')
modal = driver.find_elements(By.ID, 'download-remodal')
if modal:
    vis = driver.execute_script("return window.getComputedStyle(arguments[0]).visibility", modal[0])
    disp = driver.execute_script("return window.getComputedStyle(arguments[0]).display", modal[0])
    print(f'  #download-remodal  visibility={vis}  display={disp}')
else:
    print('  #download-remodal not found')

quality_btns = driver.find_elements(By.CSS_SELECTOR, '[data-download-button]')
print(f'  [data-download-button] elements: {len(quality_btns)}')
for btn in quality_btns:
    cls = btn.get_attribute('class') or ''
    print(f'    tag={btn.tag_name}  class="{cls}"  text="{btn.text.strip()[:40]}"'
          f'  displayed={btn.is_displayed()}')

if not quality_btns:
    print('Browser open 60s — inspect the modal.')
    time.sleep(60)
    driver.quit()
    sys.exit(1)

print('\n--- Step 8: click first quality button, observe result ---')
best_btn = quality_btns[0]
print(f'  Clicking: class="{best_btn.get_attribute("class")}"')
original_handles = set(driver.window_handles)
driver.execute_script('arguments[0].click()', best_btn)
time.sleep(3)

new_handles = set(driver.window_handles) - original_handles
print(f'  New tabs opened: {len(new_handles)}')
for h in new_handles:
    driver.switch_to.window(h)
    print(f'    new tab URL: {driver.current_url}')
    driver.close()
driver.switch_to.window(list(original_handles)[0])

print(f'  Current URL after click: {driver.current_url}')
mp4_links = driver.find_elements(By.XPATH, '//a[contains(@href,".mp4")]')
print(f'  .mp4 links in page: {len(mp4_links)}')
for el in mp4_links[:5]:
    print(f'    {(el.get_attribute("href") or "")[:80]}')

# Check if button href got populated
href = best_btn.get_attribute('href') or ''
print(f'  Button href after click: "{href[:80]}"')

print('\nBrowser open 60s — inspect the page.')
time.sleep(60)
driver.quit()
