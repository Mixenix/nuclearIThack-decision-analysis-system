import asyncio
import json
from datetime import datetime
from ui_probe import BASE, OUT
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
        c=await b.new_context(storage_state=str(OUT/'analyst.json'),viewport={'width':1440,'height':1000})
        page=await c.new_page()
        await page.goto(BASE+'/configs')
        await page.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
        name='Проверка UI '+datetime.now().strftime('%H:%M:%S')
        await page.get_by_label('Название конфигурации',exact=True).fill(name)
        # Explicitly select the available 2002-pair dataset via its rendered option.
        await page.get_by_role('combobox',name='Датасет',exact=True).click()
        print('DATASET OPTIONS',await page.get_by_role('option').all_text_contents(),flush=True)
        await page.get_by_role('option').filter(has_text='2002 пар').first.click()
        await page.get_by_text('Пар заявок: 2002 (BEFORE 2002 / AFTER 2002)',exact=True).wait_for()
        for i in range(3):
            await page.get_by_role('button',name='Рабочая область',exact=True).nth(i).click()
            print('WORKSPACE SOURCE',i,await page.get_by_role('combobox',name='Моя процедура',exact=True).count(),flush=True)
            await page.get_by_role('button',name='Файл .py',exact=True).nth(i).click()
        await page.get_by_role('switch',name='Сменилась целевая модель решения',exact=True).click()
        await page.get_by_label('параметры',exact=True).nth(4).fill('{"low_confidence_cnt": 1000, "app_ids_limit": 3}')
        await page.get_by_role('button',name='Сохранить',exact=True).click()
        await page.get_by_text('Конфигурация сохранена',exact=True).first.wait_for()
        await page.wait_for_url('**/configs?config_id=*')
        (OUT/'config_url').write_text(page.url)
        await page.reload()
        await page.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
        assert await page.get_by_label('Название конфигурации',exact=True).input_value() == name
        assert json.loads(await page.get_by_label('параметры',exact=True).nth(4).input_value()) == {'low_confidence_cnt': 1000, 'app_ids_limit': 3}
        assert await page.get_by_role('switch',name='Сменилась целевая модель решения',exact=True).get_attribute('aria-checked') == 'true'
        print('SAVED AND REFRESHED',page.url,flush=True)
        await page.get_by_role('button',name='Сохранить и запустить',exact=True).click()
        await page.wait_for_url('**/runs/*')
        run_id=page.url.rsplit('/',1)[-1]
        (OUT/'new_run_id').write_text(run_id)
        print('CREATED',run_id,flush=True)
        events=[]
        for i in range(600):
            text=await page.locator('body').inner_text()
            if not events or text != events[-1]['text']:
                events.append({'second':i,'text':text})
                print('LIVE',i,text[-650:],flush=True)
            if await page.get_by_role('button',name='Отчёт',exact=True).is_visible() or 'Server error' in text:
                break
            await page.wait_for_timeout(1000)
        assert await page.get_by_role('button',name='Отчёт',exact=True).is_visible(), 'Run did not finish within 600 seconds'
        (OUT/'live.json').write_text(json.dumps(events,ensure_ascii=False,indent=2))
        await page.screenshot(path=str(OUT/'new-run.png'),full_page=True)
        print('FINAL',await page.locator('body').inner_text(),flush=True)
        await b.close()
asyncio.run(main())
