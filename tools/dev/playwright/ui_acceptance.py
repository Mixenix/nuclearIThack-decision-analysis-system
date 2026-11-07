"""Remaining acceptance cases: layout changes, exact membership, JSON, errors."""
import asyncio
import json
from ui_probe import BASE, OUT
from playwright.async_api import async_playwright

async def settle(page):
    await page.wait_for_timeout(700)

async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
        c=await b.new_context(storage_state=str(OUT/'analyst.json'),viewport={'width':1440,'height':1000})
        page=await c.new_page()
        page.set_default_timeout(20000)
        run_id=(OUT/'run_id').read_text()
        await page.goto(BASE+'/runs/'+run_id+'/explore');await settle(page)
        for label,dimension in [('Строки','CLASS'),('Колонки','SOURCE')]:
            field=page.locator('.q-field').filter(has=page.get_by_role('combobox',name=label,exact=True))
            await field.locator('.q-chip__icon--remove').click()
            await page.get_by_role('combobox',name=label,exact=True).click()
            await page.get_by_role('option',name=dimension,exact=True).click()
            await page.keyboard.press('Escape')
            await settle(page)
            print('LAYOUT',label,dimension,flush=True)
        await page.get_by_text('BEFORE',exact=True).last.wait_for()
        text=await page.locator('body').inner_text()
        assert 'BEFORE' in text and 'AFTER' in text
        assert 'Не удалось построить сводную' not in text
        print('MATRIX',text[-2000:],flush=True)
        # The first nonempty cell is a small enough dimension group to traverse.
        cell=page.locator('button').filter(has_text='2 002').first
        await cell.click()
        await page.get_by_text('Уникальных заявок: 2 002 (показано 50)',exact=True).wait_for()
        print('LAYOUT DRILL COUNT 2002',flush=True)
        await page.screenshot(path=str(OUT/'dimensions.png'),full_page=True)
        # Use the decision-change cell to verify all pages, not only the first 50.
        await page.goto(BASE+'/runs/'+run_id+'/explore');await settle(page)
        row=page.locator('.nicegui-row').filter(has=page.get_by_text('decisionResult.decision',exact=True)).filter(has=page.get_by_role('button',name='385',exact=True)).last
        await row.get_by_role('button',name='385',exact=True).click()
        await page.get_by_text('Уникальных заявок: 385 (показано 50)',exact=True).wait_for()
        ids=[]
        for n in range(1,9):
            await page.get_by_text(f'стр. {n} / 8',exact=True).wait_for()
            ids.extend(await page.locator('a[href*="/app/"]').all_text_contents())
            if n<8:
                await page.get_by_role('button',name='▶',exact=True).click()
        assert len(ids)==len(set(ids))==385
        (OUT/'drill_ids.json').write_text(json.dumps({'run_id':run_id,'app_ids':ids}))
        print('ALL DRILL PAGES',len(ids),'unique',len(set(ids)),flush=True)
        await page.locator('a[href*="/app/"]').first.click()
        await page.wait_for_url('**/app/**');await settle(page)
        await page.get_by_text('Исходный JSON',exact=True).click()
        before=json.loads(await page.locator('pre').inner_text())
        await page.get_by_role('button',name='AFTER',exact=True).click()
        await settle(page)
        after=json.loads(await page.locator('pre').inner_text())
        print('RAW BOTH',before['decisionResult']['decision'],after['decisionResult']['decision'],flush=True)
        await page.screenshot(path=str(OUT/'raw-json.png'),full_page=True)
        await page.reload();await settle(page)
        assert await page.get_by_text('Продукты по срезам',exact=True).is_visible()
        print('APP REFRESH PASS',flush=True)
        # Exercise invalid JSON: rejection instead of silently replacing params.
        await page.goto((OUT/'config_url').read_text());await settle(page)
        await page.get_by_label('Параметры (JSON)',exact=True).first.fill('{bad')
        await page.get_by_role('button',name='Сохранить',exact=True).click()
        await page.get_by_text('Не сохранено: Нормализация: некорректный JSON',exact=True).wait_for()
        print('CONFIG INVALID JSON REJECTED',flush=True)
        # A deliberate technical failure is test data, submitted only via the UI.
        await page.goto(BASE+'/workspace');await settle(page)
        await page.get_by_label('Название',exact=True).fill('Техническая проверка UI')
        await page.locator('.cm-content').fill('''def run(mart, params=None):
    raise RuntimeError("UI verification deliberate technical error")

CHECK = {"id": "ui_verify_error", "label": "Техническая проверка UI", "params": {}, "entrypoint": run}
''')
        await page.get_by_role('combobox',name='Готовый прогон',exact=True).click()
        await page.get_by_role('option').filter(has_text='Полный прогон').first.click()
        await page.get_by_role('button',name='Выполнить на прогоне',exact=True).click()
        await page.get_by_text('Проверка поставлена в очередь',exact=False).wait_for()
        await page.get_by_role('button',name='Открыть отчёт',exact=True).click()
        await page.wait_for_url('**/report');await settle(page)
        for n in range(40):
            errors=page.get_by_text('Техническая ошибка:',exact=False)
            if await errors.count():
                break
            await page.wait_for_timeout(1000)
            await page.reload();await settle(page)
        assert await errors.count()
        print('TECHNICAL BLOCK',await errors.all_text_contents(),flush=True)
        assert await page.get_by_text('UI verification deliberate technical error',exact=False).count()
        technical_card=page.locator('.q-card').filter(has=errors.first)
        assert 'FAIL' not in await technical_card.inner_text()
        print('TECHNICAL NEVER FAIL',flush=True)
        await page.screenshot(path=str(OUT/'technical.png'),full_page=True)
        await b.close()
asyncio.run(main())
