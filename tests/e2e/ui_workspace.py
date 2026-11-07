import asyncio
from datetime import datetime
from ui_probe import BASE, OUT
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
        c=await b.new_context(storage_state=str(OUT/'analyst.json'),viewport={'width':1440,'height':1000})
        page=await c.new_page()
        page.set_default_timeout(15000)
        await page.goto(BASE+'/workspace')
        await page.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
        name='Проверка редактора UI '+datetime.now().strftime('%H:%M:%S')
        await page.get_by_label('Название',exact=True).fill(name)
        editor=page.locator('.cm-content')
        code='\n'.join(await editor.locator('.cm-line').all_text_contents())
        code=code.replace('approve_to_reject','ui_verify_check').replace('Одобренные заявки ушли в отказ','Проверка UI: переход в отказ')
        await editor.fill(code)
        await page.get_by_label('Параметры (JSON)',exact=True).fill('{"app_ids_limit": 3, "low_confidence_cnt": 1000}')
        await page.get_by_role('button',name='Проверить контракт',exact=True).click()
        print('VALIDATE', (await page.locator('body').inner_text())[-500:],flush=True)
        await page.get_by_role('button',name='Сохранить',exact=True).click()
        await page.wait_for_url('**/workspace?procedure_id=*')
        (OUT/'procedure_url').write_text(page.url)
        await page.reload()
        await page.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
        assert '\n'.join(await editor.locator('.cm-line').all_text_contents()) == code
        assert await page.get_by_label('Название',exact=True).input_value()==name
        print('PERSISTED', page.url,flush=True)
        await page.get_by_role('combobox',name='Готовый прогон',exact=True).click()
        print('RUN OPTIONS',await page.get_by_role('option').all_text_contents(),flush=True)
        # Pick the UI-created run when available; otherwise pick the original completed run.
        options=page.get_by_role('option').filter(has_text='Проверка UI')
        if await options.count():
            await options.first.click()
        else:
            await page.get_by_role('option').filter(has_text='Полный прогон').first.click()
        await page.get_by_role('button',name='Выполнить на прогоне',exact=True).click()
        await page.get_by_text('Проверка поставлена в очередь',exact=False).wait_for()
        print('QUEUED', (await page.locator('body').inner_text())[-700:],flush=True)
        await page.get_by_role('button',name='Открыть отчёт',exact=True).click()
        await page.wait_for_url('**/report')
        (OUT/'workspace_report_url').write_text(page.url)
        for i in range(25):
            if await page.get_by_text('Проверка UI: переход в отказ',exact=True).count():
                print('REPORT RESULT', (await page.locator('body').inner_text())[-1400:],flush=True)
                break
            await page.wait_for_timeout(1000)
            await page.reload()
            await page.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
        else:
            raise AssertionError('New check did not appear')
        await page.screenshot(path=str(OUT/'workspace-report.png'),full_page=True)
        # Independent context, no copied cookies or browser storage.
        c2=await b.new_context(viewport={'width':1440,'height':1000})
        other=await c2.new_page()
        await other.goto(BASE+'/login')
        await other.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
        await other.get_by_label('Логин',exact=True).fill('analyst2')
        await other.get_by_label('Пароль',exact=True).fill('analyst2')
        await other.get_by_role('button',name='Войти',exact=True).click()
        await other.wait_for_url(BASE+'/')
        for suffix in ['', '/report','/explore','/app/I32554354']:
            run_id=(OUT/'run_id').read_text()
            await other.goto(BASE+'/runs/'+run_id+suffix)
            await other.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
            text=await other.locator('body').inner_text()
            assert 'принадлежит другому пользователю' in text or 'Заявка недоступна' in text, text
            assert 'ML_MICRO_LOAN' not in text and '289 533' not in text
            print('DENIED',suffix,text[-250:],flush=True)
        await other.goto((OUT/'procedure_url').read_text())
        await other.wait_for_timeout(500)  # Allow the NiceGUI websocket to connect.
        assert 'ui_verify_check' not in await other.locator('.cm-content').inner_text()
        print('PROCEDURE DENIED',flush=True)
        await other.screenshot(path=str(OUT/'denied.png'),full_page=True)
        await b.close()
asyncio.run(main())
