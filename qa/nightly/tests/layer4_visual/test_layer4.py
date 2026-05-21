"""LAYER 4 — VISUAL REGRESSION (Minimal)
Only key pages. Threshold tolerance for minor pixel noise.
"""
import pytest, os, hashlib
from playwright.sync_api import sync_playwright
from pathlib import Path
import numpy as np
from PIL import Image

BASE = Path(__file__).resolve().parents[4]
BASE_URL = os.getenv('TEST_BASE_URL', 'http://127.0.0.1:5001')
BASELINE_DIR = BASE / 'qa' / 'nightly' / 'snapshots' / 'baseline'
CURRENT_DIR = BASE / 'qa' / 'nightly' / 'snapshots' / 'current'
DIFF_THRESHOLD_PX = 50000  # Allow dynamic content (timers, animations)

PAGES_TO_SNAPSHOT = [
    ('landing', '/', {'full_page': True}),
    ('drawer', '/', {'action': 'open_drawer', 'selector': 'article[onclick]'}),
]

def pixel_diff(img1_path, img2_path):
    """Count differing pixels between two images."""
    try:
        img1 = np.array(Image.open(img1_path).convert('RGB'))
        img2 = np.array(Image.open(img2_path).convert('RGB'))
        if img1.shape != img2.shape:
            return float('inf')  # Different sizes = major change
        diff = np.abs(img1.astype(int) - img2.astype(int))
        # Count pixels with significant difference (channel diff > 20)
        significant = np.any(diff > 20, axis=2)
        return int(np.sum(significant))
    except Exception:
        return float('inf')

@pytest.fixture(scope='session')
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()

@pytest.fixture
def page(browser):
    page = browser.new_page(viewport={'width': 1280, 'height': 900})
    yield page
    page.close()

class TestVisualRegression:
    """Detect catastrophic layout changes."""

    def test_landing_snapshot(self, page):
        page.goto(f'{BASE_URL}/')
        page.wait_for_load_state('networkidle', timeout=15000)
        snapshot_path = CURRENT_DIR / 'landing.png'
        page.screenshot(path=str(snapshot_path), full_page=True)
        baseline = BASELINE_DIR / 'landing.png'
        if baseline.exists():
            diff = pixel_diff(baseline, snapshot_path)
            assert diff < DIFF_THRESHOLD_PX, f'Landing visual diff: {diff} pixels > {DIFF_THRESHOLD_PX}'
        else:
            # First run — save baseline
            snapshot_path.rename(baseline)
            pytest.skip('Baseline saved — no comparison yet')

    def test_drawer_snapshot(self, page):
        page.goto(f'{BASE_URL}/')
        page.wait_for_load_state('networkidle', timeout=10000)
        # Open first event
        cards = page.locator('article[onclick]').all()
        if cards:
            cards[0].click()
            page.wait_for_selector('#drawer', timeout=5000)
            snapshot_path = CURRENT_DIR / 'drawer.png'
            page.screenshot(path=str(snapshot_path), full_page=False)
            baseline = BASELINE_DIR / 'drawer.png'
            if baseline.exists():
                diff = pixel_diff(baseline, snapshot_path)
                assert diff < DIFF_THRESHOLD_PX, f'Drawer visual diff: {diff} pixels'
            else:
                snapshot_path.rename(baseline)
                pytest.skip('Baseline saved')
