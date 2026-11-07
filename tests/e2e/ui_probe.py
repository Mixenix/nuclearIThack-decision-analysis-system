"""Real Chromium UI probe. Run from app with PLAYWRIGHT_BROWSERS_PATH set."""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

BASE = 'http://127.0.0.1:8091'
OUT = Path('/tmp/ui-verify')
OUT.mkdir(exist_ok=True)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
        context = await browser.new_context(accept_downloads=True, viewport={'width':1440,'height':1000})
        page = await context.new_page()
        page.on('pageerror', lambda e: print('PAGEERROR', e))
        await page.goto(BASE+'/login')
        await page.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
        await page.get_by_label('Логин', exact=True).fill('analyst')
        await page.get_by_label('Пароль', exact=True).fill('wrong-password')
        await page.get_by_role('button', name='Войти', exact=True).click()
        await page.get_by_text('Неверный логин или пароль', exact=True).wait_for()
        assert page.url.endswith('/login')
        print('PASS wrong password')
        await page.get_by_label('Пароль', exact=True).fill('analyst')
        await page.get_by_role('button', name='Войти', exact=True).click()
        await page.wait_for_url(BASE+'/')
        await page.get_by_text('Мои прогоны',exact=True).wait_for()
        await page.wait_for_timeout(1000)
        print('RUNS', await page.locator('body').inner_text())
        await page.screenshot(path=str(OUT/'runs.png'), full_page=True)
        await context.storage_state(path=str(OUT/'analyst.json'))
        for route in ['/configs','/workspace']:
            await page.goto(BASE+route)
            await page.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
            await page.wait_for_timeout(1500)
            print(route, await page.locator('body').inner_text())
            await page.screenshot(path=str(OUT/(route[1:]+'.png')), full_page=True)
        await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
