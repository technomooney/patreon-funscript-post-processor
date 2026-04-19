#!/usr/bin/env python3
"""
Standalone diagnostic for SpankBang login and video download link detection.

Usage:
    python test_spankbang_login.py [video_url]

The browser stays open for 60 seconds after each step so you can inspect.
"""
import sys
import time

from dotenv import load_dotenv
load_dotenv()

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from downloadContent import setup_driver, _get_secret, _spankbang_dismiss_age_gate

VIDEO_URL = sys.argv[1] if len(sys.argv) > 1 else None

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
print('\n--- Step 6: look for download toggle and links ---')
toggle_xp = ('//*[contains(@class,"download") and '
             '(self::button or self::a or self::div) and '
             'not(contains(@href,".mp4"))]')
toggles = driver.find_elements(By.XPATH, toggle_xp)
print(f'  Download toggle candidates: {len(toggles)}')
for el in toggles[:5]:
    print(f'    tag={el.tag_name}  class="{el.get_attribute("class")}"'
          f'  text="{el.text.strip()[:40]}"  displayed={el.is_displayed()}')
if toggles:
    driver.execute_script('arguments[0].click()', toggles[0])
    time.sleep(1)

links_xp = ('//a[contains(@href,".mp4") or '
            '(contains(@class,"download") and @href and @href != "#")]')
links = driver.find_elements(By.XPATH, links_xp)
print(f'  Download links found: {len(links)}')
for el in links[:10]:
    print(f'    href="{(el.get_attribute("href") or "")[:80]}"  text="{el.text.strip()[:30]}"')

print('\nBrowser open 60s — inspect the page.')
time.sleep(60)
driver.quit()
