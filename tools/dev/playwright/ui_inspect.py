import asyncio
import json
from ui_probe import BASE, OUT
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
        c = await b.new_context(storage_state=str(OUT/'analyst.json'), viewport={'width':1440,'height':1000})
        page = await c.new_page()
        await page.goto(BASE+'/')
        await page.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
        await page.get_by_role('row').filter(has_text='2 002').filter(has_text='готов').filter(has_text='Полный прогон').last.click()
        await page.wait_for_timeout(1500)
        print('OPEN URL', page.url, flush=True)
        run_id = page.url.rsplit('/',1)[-1]
        (OUT/'run_id').write_text(run_id)
        for route in ['', '/report', '/explore']:
            await page.goto(BASE+'/runs/'+run_id+route)
            await page.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
            await page.wait_for_timeout(1800)
            print('ROUTE',route, (await page.locator('body').inner_text())[:18000], flush=True)
            await page.screenshot(path=str(OUT/('run'+route.replace('/','-')+'.png')), full_page=True)
        await b.close()
asyncio.run(main())
