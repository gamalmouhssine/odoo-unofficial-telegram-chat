"""Browser check of the Telegram screen (Playwright, headless Chromium).

Runs the main user flows against a database seeded by seed_demo.py, fails
on any browser console error or uncaught exception, and saves screenshots
(also used for the Apps Store page).

    python ui_check.py <odoo base url> <database> <output dir>
"""
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE, DB, OUT = sys.argv[1].rstrip('/'), sys.argv[2], Path(sys.argv[3])
OUT.mkdir(parents=True, exist_ok=True)
APP = f'{BASE}/odoo/telegram-client'
problems = []


def watch(page, label):
    page.on('console', lambda m: m.type == 'error' and problems.append(f'[{label}] console: {m.text}'))
    page.on('pageerror', lambda e: problems.append(f'[{label}] page error: {e}'))


def login(context, login_name, password, label):
    page = context.new_page()
    watch(page, label)
    page.goto(f'{BASE}/web/login?db={DB}')
    page.fill('input[name="login"]', login_name)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_url('**/odoo**')
    return page


def step(name):
    print(f'  - {name}', flush=True)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch()

    print('Desktop, connected user')
    context = browser.new_context(viewport={'width': 1440, 'height': 900}, locale='en-US')
    page = login(context, 'demo', 'demo-check-1', 'desktop')
    page.goto(APP)
    items = page.locator('.o_telegram_chat_item')
    expect(items.first).to_be_visible(timeout=15000)
    step(f'chat list shows {items.count()} chats')
    assert items.count() == 6, items.count()
    expect(items.first).to_contain_text('Ahmed Karim')  # pinned first
    expect(items.first.locator('.badge')).to_have_text('2')
    page.screenshot(path=OUT / 'chats.png')

    page.fill('input[aria-label="Search chats"]', 'phase')
    expect(items).to_have_count(1)
    page.fill('input[aria-label="Search chats"]', '')
    step('search filters chats')

    items.filter(has_text='Site Team').click()
    expect(page.locator('.o_telegram_bubble').first).to_be_visible()
    expect(page.locator('.o_telegram_sender').first).to_be_visible()
    expect(page.locator('.o_telegram_media').first).to_contain_text('Photo')
    step('group chat shows senders and media placeholders')

    items.filter(has_text='Ahmed Karim').click()
    expect(page.locator('.o_telegram_bubble')).to_have_count(5)
    expect(page.locator('.o_telegram_ticks').first).to_have_text('✓✓')
    page.screenshot(path=OUT / 'conversation.png')
    step('private chat shows messages with read ticks')

    box = page.locator('textarea[aria-label="Message"]')
    box.fill('Confirmed, see you at 2 pm.')
    box.press('Enter')
    expect(page.locator('.o_telegram_bubble.o_outgoing').last).to_contain_text('Confirmed, see you at 2 pm.')
    expect(page.locator('.o_telegram_bubble.o_outgoing').last.locator('.fa-clock-o')).to_be_visible()
    expect(box).to_have_value('')
    step('sending adds a pending bubble and clears the composer')

    box.fill('line one')
    box.press('Shift+Enter')
    box.type('line two')
    expect(box).to_have_value('line one\nline two')
    box.fill('')
    step('Shift+Enter inserts a new line')

    page.click('button[aria-label="New chat"]')
    expect(page.locator('.o_telegram_contacts .o_telegram_chat_item')).to_have_count(5)
    page.fill('input[aria-label="Search contacts"]', 'lay')
    expect(page.locator('.o_telegram_contacts .o_telegram_chat_item')).to_have_count(1)
    page.fill('input[aria-label="Search contacts"]', '')
    expect(page.locator('.o_telegram_contacts .o_telegram_chat_item')).to_have_count(5)
    page.screenshot(path=OUT / 'contacts.png')
    page.click('.o_telegram_contacts button[aria-label="Back to chats"]')
    step('contacts panel lists and searches contacts')

    page.click('button[aria-label="Disconnect"]')
    dialog = page.locator('.modal-dialog')
    expect(dialog).to_contain_text('Disconnect Telegram')
    dialog.get_by_role('button', name='Cancel').click()
    expect(dialog).to_have_count(0)
    step('disconnect asks for confirmation')
    context.close()

    print('Phone, connected user')
    context = browser.new_context(viewport={'width': 390, 'height': 844}, locale='en-US', is_mobile=True, has_touch=True)
    page = login(context, 'demo', 'demo-check-1', 'phone')
    page.goto(APP)
    items = page.locator('.o_telegram_chat_item')
    expect(items.first).to_be_visible(timeout=15000)
    expect(page.locator('.o_telegram_main')).to_be_hidden()
    items.filter(has_text='Ahmed Karim').tap()
    expect(page.locator('.o_telegram_conversation')).to_be_visible()
    expect(page.locator('.o_telegram_sidebar')).to_be_hidden()
    page.screenshot(path=OUT / 'phone.png')
    page.locator('.o_telegram_conversation button[aria-label="Back to chats"]').tap()
    expect(page.locator('.o_telegram_sidebar')).to_be_visible()
    step('one pane at a time with a working back button')
    context.close()

    # Dark mode is an Odoo Enterprise feature (Community always renders the
    # light theme), so it is checked on Enterprise, not here.

    print('User not connected yet')
    context = browser.new_context(viewport={'width': 1440, 'height': 900}, locale='en-US')
    page = login(context, 'newcomer', 'newcomer-check-1', 'login')
    page.goto(APP)
    wizard = page.locator('.o_telegram_wizard')
    expect(wizard).to_contain_text('unofficial')
    expect(wizard.get_by_role('button', name='Log in with QR code')).to_be_visible()
    page.screenshot(path=OUT / 'login.png')
    wizard.get_by_role('button', name='Log in with phone number').click()
    expect(wizard.locator('input[type="tel"]')).to_be_visible()
    wizard.locator('input[type="tel"]').fill('not a number')
    wizard.get_by_role('button', name='Send code').click()
    expect(wizard.locator('.alert-danger')).to_contain_text('international format')
    step('login wizard: intro, phone step, input validation')
    context.close()

    print('Administrator settings')
    context = browser.new_context(viewport={'width': 1440, 'height': 900}, locale='en-US')
    page = login(context, 'admin', 'admin-check-1', 'settings')
    page.goto(f'{BASE}/odoo/settings#telegram_personal_client')
    expect(page.locator('#telegram_api_id_setting')).to_be_visible(timeout=15000)
    page.locator('#telegram_api_id_setting').scroll_into_view_if_needed()
    page.screenshot(path=OUT / 'settings.png')
    step('settings page shows the Telegram block')
    context.close()
    browser.close()

if problems:
    print('\nBROWSER PROBLEMS:')
    for problem in problems:
        print('  ', problem)
    sys.exit(1)
print('\nUI CHECK PASSED (no console errors)')
