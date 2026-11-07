"""Read-only final smoke after all edits; uses artifacts from earlier browser steps."""
import asyncio
import json
from ui_probe import BASE, OUT
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
        c=await b.new_context(storage_state=str(OUT/'analyst.json'),accept_downloads=True,viewport={'width':1440,'height':1000})
        page=await c.new_page()
        page.set_default_timeout(15000)
        rid=(OUT/'new_run_id').read_text()
        await page.goto(BASE+'/runs/'+rid)
        await page.wait_for_timeout(700)
        if page.url.endswith('/login'):
            await page.get_by_label('Логин',exact=True).fill('analyst')
            await page.get_by_label('Пароль',exact=True).fill('analyst')
            await page.get_by_role('button',name='Войти',exact=True).click()
            await page.wait_for_url(BASE+'/')
            await c.storage_state(path=str(OUT/'analyst.json'))
            await page.goto(BASE+'/runs/'+rid)
            await page.wait_for_timeout(700)
        print('FINAL RUN BODY',await page.locator('body').inner_text(),flush=True)
        for number in ['2 002','502','385','289 533','21 601']:
            assert await page.get_by_text(number,exact=True).count(), number
        assert await page.get_by_text('Обработано 2 002 из 2 002 пар',exact=True).is_visible()
        assert await page.get_by_role('progressbar').count()==1
        assert await page.get_by_role('button',name='к коду',exact=True).count()==3
        await page.reload();await page.wait_for_timeout(700)
        print('NEW RUN FINISHED AND REFRESHED',page.url,flush=True)
        await page.screenshot(path=str(OUT/'final-run.png'),full_page=True)
        await page.get_by_role('button',name='Отчёт',exact=True).click()
        await page.wait_for_url('**/report');await page.wait_for_timeout(700)
        assert await page.get_by_text('Сменилась целевая модель решения',exact=True).count()
        assert await page.get_by_text('FAIL · низкая уверенность',exact=True).count()
        print('CONFIG CHECK PARAMS EFFECT: LOW FAIL',flush=True)
        await page.screenshot(path=str(OUT/'final-low.png'),full_page=True)
        original=(OUT/'run_id').read_text()
        await page.goto(BASE+'/runs/'+original+'/report');await page.wait_for_timeout(700)
        assert await page.get_by_text('Не настроено:',exact=False).count()
        assert await page.locator('.bg-red-100').count()==31
        print('ORIGINAL REPORT 31 HIGH FAIL ROWS',flush=True)
        async with page.expect_download() as pending:
            await page.get_by_role('button',name='Скачать HTML',exact=True).click()
        dl=await pending.value
        path=OUT/'final-download.html';await dl.save_as(path)
        standalone=await c.new_page();await standalone.goto(path.as_uri())
        links=await standalone.locator('a').evaluate_all('(es)=>es.map(e=>e.href)')
        assert links and all(link.startswith(BASE+'/') for link in links)
        assert '2002' in await standalone.locator('body').inner_text()
        print('FINAL STANDALONE DOWNLOAD',path.stat().st_size,'bytes',len(links),'absolute app links',flush=True)
        await page.screenshot(path=str(OUT/'final-high.png'),full_page=True)
        await standalone.close()
        await page.goto(BASE+'/runs/'+original+'/explore');await page.wait_for_timeout(700)
        await page.get_by_label('PATH начинается с',exact=True).fill('characteristics')
        row=page.locator('.nicegui-row').filter(has=page.get_by_text('decisionResult.decision',exact=True)).filter(has=page.get_by_role('button',name='385',exact=True)).last
        await row.get_by_role('button',name='385',exact=True).click()
        await page.get_by_text('Уникальных заявок: 385 (показано 50)',exact=True).wait_for()
        print('DRILL USES DISPLAYED MATRIX FILTER SNAPSHOT',flush=True)
        await page.goto(BASE+'/runs/'+original+'/explore');await page.wait_for_timeout(700)
        for label in ['Строки','Колонки']:
            field=page.locator('.q-field').filter(has=page.get_by_role('combobox',name=label,exact=True))
            await field.locator('.q-chip__icon--remove').click()
            await page.wait_for_timeout(700)
        assert await page.get_by_role('button',name='2 002',exact=True).count()==1
        await page.get_by_role('button',name='2 002',exact=True).click()
        await page.get_by_text('Уникальных заявок: 2 002 (показано 50)',exact=True).wait_for()
        print('ZERO DIMENSIONS: SINGLE TOTAL CELL AND DRILL',flush=True)
        # Record the service-layer regression, without hiding it in the UI.
        revised=(OUT/'workspace_report_url').read_text().split('/runs/')[1].split('/')[0]
        await page.goto(BASE+'/runs/'+revised+'/explore');await page.wait_for_timeout(1000)
        empty=await page.get_by_text('В выборке нет заявок — измените фильтры',exact=True).count()
        print('KNOWN REVISION REGRESSION EMPTY PIVOT',bool(empty),page.url,flush=True)
        await page.screenshot(path=str(OUT/'revision-regression.png'),full_page=True)
        await b.close()
        assert not empty, 'Service revision regression: pivot becomes empty after checks-only execution'
asyncio.run(main())
