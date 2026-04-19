#!/usr/bin/env python3
"""
Standalone test for the SpankBang age-gate dismissal.

Usage:
    python test_spankbang_agegate.py [url]

If no URL is given, opens https://spankbang.com/ which always shows the gate.
The browser stays open for 30 seconds so you can see the result.
"""
import sys
import time

from dotenv import load_dotenv
load_dotenv()

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

URL = sys.argv[1] if len(sys.argv) > 1 else 'https://spankbang.com/'

print(f'Opening: {URL}')
options = uc.ChromeOptions()
# Run windowed so you can see what happens
driver = uc.Chrome(options=options)
driver.get(URL)
time.sleep(3)

# --- Step 1: check whether #age-check is present and visible ---
try:
    modal = driver.find_element(By.ID, 'age-check')
    display = driver.execute_script(
        "return window.getComputedStyle(arguments[0]).display", modal
    )
    visibility = driver.execute_script(
        "return window.getComputedStyle(arguments[0]).visibility", modal
    )
    print(f'#age-check found  display={display}  visibility={visibility}')
except WebDriverException:
    print('#age-check NOT found in DOM')

# --- Step 2: check whether the button exists ---
try:
    btn = driver.find_element(By.ID, 'age-check-yes')
    print(f'#age-check-yes found  text="{btn.text.strip()}"  displayed={btn.is_displayed()}')
except WebDriverException:
    print('#age-check-yes NOT found in DOM')

# --- Step 3: attempt dismissal ---
print('\nAttempting dismissal via accept_warning_modal() JS...')
try:
    WebDriverWait(driver, 5).until(
        EC.visibility_of_element_located((By.ID, 'age-check'))
    )
    try:
        driver.execute_script('accept_warning_modal()')
        print('  accept_warning_modal() called OK')
    except WebDriverException as e:
        print(f'  JS call failed: {e} — falling back to element click')
        btn = driver.find_element(By.ID, 'age-check-yes')
        driver.execute_script('arguments[0].click()', btn)
        print('  element click executed')
    time.sleep(1)
except TimeoutException:
    print('  #age-check never became visible — modal may not have appeared')

# --- Step 4: check modal is gone ---
try:
    modal = driver.find_element(By.ID, 'age-check')
    display = driver.execute_script(
        "return window.getComputedStyle(arguments[0]).display", modal
    )
    print(f'\nAfter dismissal: #age-check display={display}')
    if display == 'none':
        print('SUCCESS — modal is hidden')
    else:
        print('FAILED — modal is still visible')
except WebDriverException:
    print('\nAfter dismissal: #age-check gone from DOM — SUCCESS')

print('\nBrowser stays open for 30s so you can inspect the page.')
time.sleep(30)
driver.quit()
