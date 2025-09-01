from playwright.sync_api import sync_playwright
EMAIL = input("Email: ")
PASS  = input("Password: ")
with sync_playwright() as p:
    b = p.chromium.launch(headless=False)
    ctx = b.new_context()
    page = ctx.new_page()
    page.goto("https://pocketoption.com/en/login")
    try: page.get_by_title("Close").click(timeout=2000)
    except: pass
    page.get_by_role("textbox", name="Email *").fill(EMAIL)
    page.get_by_role("textbox", name="Password *").fill(PASS)
    page.get_by_role("button", name="Sign In").click()
    print("\nSolve CAPTCHA, open the Trading screen, then return here.")
    input("Press ENTER once the Trading screen is visible...")
    ctx.storage_state(path="po_storage.json")
    b.close()
    print("Saved po_storage.json")
