"""Targeted real-browser checks, with step logs and screenshot evidence."""
import asyncio
from ui_probe import BASE, OUT
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
        c = await b.new_context(storage_state=str(OUT/'analyst.json'), accept_downloads=True, viewport={'width':1440,'height':1000})
        page = await c.new_page()
        page.set_default_timeout(20000)
        run_id=(OUT/'run_id').read_text()
        await page.goto(BASE+'/runs/'+run_id)
        await page.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
        await page.get_by_role('button', name='к коду').first.click()
        await page.wait_for_url('**/workspace?**')
        print('CODE', page.url, (await page.locator('.cm-content').inner_text())[:500], flush=True)
        await page.goto(BASE+'/runs/'+run_id+'/report')
        await page.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
        try:
            async with page.expect_download() as download_info:
                await page.get_by_role('button',name='Скачать HTML').click()
        except Exception:
            print('DOWNLOAD FAILED UI',await page.locator('body').inner_text(),flush=True)
            await page.screenshot(path=str(OUT/'download-failed.png'),full_page=True)
            raise
        download=await download_info.value
        path=OUT/'download.html'
        await download.save_as(path)
        print('DOWNLOAD', download.suggested_filename, path.stat().st_size, flush=True)
        standalone=await c.new_page()
        await standalone.goto(path.as_uri())
        await standalone.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
        print('STANDALONE', (await standalone.locator('body').inner_text())[:500], await standalone.locator('a').evaluate_all('(els)=>({count:els.length,origins:[...new Set(els.map(e=>new URL(e.href).origin))]})'), flush=True)
        await standalone.screenshot(path=str(OUT/'standalone.png'), full_page=True)
        await standalone.close()
        await page.goto(BASE+'/runs/'+run_id+'/explore')
        await page.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
        row=page.locator('.nicegui-row').filter(has=page.get_by_text('decisionResult.decision',exact=True)).filter(has=page.get_by_role('button',name='385',exact=True)).last
        await row.get_by_role('button',name='385',exact=True).click()
        await page.get_by_text('Уникальных заявок: 385 (показано 50)',exact=True).wait_for()
        app_links=page.locator('a[href*="/app/"]')
        print('DRILL',await app_links.count(),await app_links.first.inner_text(),flush=True)
        await app_links.first.click()
        await page.wait_for_url('**/app/**')
        print('APP', page.url, (await page.locator('body').inner_text())[:3500],flush=True)
        before=await page.get_by_text('SAME',exact=True).count()
        await page.get_by_role('switch',name='показывать совпадающие атрибуты').click()
        await page.wait_for_timeout(500)
        print('SAME',before,await page.get_by_text('SAME',exact=True).count(),flush=True)
        await page.get_by_text('Исходный JSON',exact=True).click()
        print('RAW BEFORE', (await page.locator('pre').inner_text())[:500],flush=True)
        await page.get_by_role('button',name='AFTER',exact=True).click()
        await page.wait_for_timeout(500)
        print('RAW AFTER',(await page.locator('pre').inner_text())[:500],flush=True)
        await page.screenshot(path=str(OUT/'app.png'),full_page=True)
        await page.goto(BASE+'/configs')
        await page.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
        print('CONFIG INPUTS',await page.locator('input').evaluate_all('(es)=>es.map(e=>({outer:e.outerHTML,value:e.value}))'),flush=True)
        await b.close()
asyncio.run(main())
