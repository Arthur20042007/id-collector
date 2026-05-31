import sys
import asyncio
import os
import re
import json
import urllib.request
import urllib.parse
from collections import defaultdict
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "СЮДА_ВСТАВЬ_URL_ВЕБХУКА")
USER_EMAIL = os.getenv("USER_EMAIL", "твой_email")
USER_PASSWORD = os.getenv("USER_PASSWORD", "твой_пароль")
LOGIN_URL = "https://admin.100points.ru/login"

def clean_task_text(raw_text):
    """Очищает сырой текст с сайта от мусора сверху и снизу (как в основном боте)."""
    if "Файлы ученика" in raw_text:
        raw_text = raw_text.split("Файлы ученика")[0]
        
    for stop_word in ["Ссылка на видео", "Видеоразбор", "Ссылка на разбор"]:
        if stop_word in raw_text:
            raw_text = raw_text.split(stop_word)[0]
        
    lines = raw_text.strip().split('\n')
    cleaned_lines = []
    
    for line in lines:
        clean_line = line.strip()
        if re.match(r'^Задание\s*№\d+', clean_line, flags=re.IGNORECASE):
            continue
        if clean_line.lower() == "прикрепленный документ":
            continue
        if clean_line:
            cleaned_lines.append(clean_line)
        
    return "\n".join(cleaned_lines).strip()

def normalize_text(text):
    """Удаляет пробелы и знаки для точного сравнения."""
    if not text: return ""
    return re.sub(r'[\s\W_]+', '', str(text)).lower()

PARSE_PAGE_JS = '''() => {
    let headers = Array.from(document.querySelectorAll('h4')).filter(h => h.innerText && h.innerText.includes('Задание №'));
    let tasks = [];

    for (let i = 0; i < headers.length; i++) {
        let current = headers[i];
        let nodes = [];
        let n = current.nextElementSibling;

        while(n && n.tagName !== 'H4' && n.tagName !== 'HR') {
            nodes.push(n);
            n = n.nextElementSibling;
        }

        let taskText = current.innerText + "\\n";
        let answerId = null;
        let commentText = "";

        let allDesc = [];
        nodes.forEach(node => {
            if(node.querySelectorAll) {
                allDesc.push(node);
                allDesc.push(...node.querySelectorAll('*'));
                
                // === УМНЫЙ СБОР ТЕКСТА КАК В ОСНОВНОМ БОТЕ ===
                let panels = node.querySelectorAll('.panel');
                panels.forEach(p => p.style.display = 'block');

                let mathScripts = node.querySelectorAll('script[type^="math/tex"]');
                mathScripts.forEach(script => {
                    let latexText = document.createTextNode(' ' + script.innerHTML + ' ');
                    script.parentNode.insertBefore(latexText, script);
                });

                let mathJaxContainers = node.querySelectorAll('.MathJax_Preview, .MathJax_CHTML, .mjx-chtml');
                mathJaxContainers.forEach(container => container.style.display = 'none');
                
                if (node.innerText) { taskText += node.innerText + "\\n"; }
            } else if (node.innerText) {
                taskText += node.innerText + "\\n";
            }
        });

        // Ищем answerId
        for(let node of nodes) {
            let btn = node.querySelector ? node.querySelector('button[onclick*="saveAnswer"]') : null;
            if (!btn && node.tagName === 'BUTTON' && node.getAttribute('onclick') && node.getAttribute('onclick').includes('saveAnswer')) {
                btn = node;
            }
            if(btn) {
                let match = btn.getAttribute('onclick').match(/saveAnswer\\s*\\(\\s*(\\d+)/);
                if(match) { answerId = match[1]; break; }
            }
        }

        // Ищем комментарий
        let editor = allDesc.find(el => el.classList && el.classList.contains('note-editable'));
        if (editor) {
            commentText = editor.innerText;
        } else {
            let commentBlock = allDesc.find(el => el.innerText && el.innerText.includes('Оценка куратора'));
            if(commentBlock) {
                commentText = commentBlock.innerText;
            }
        }

        if (answerId) {
            tasks.push({
                answerId: answerId,
                commentText: commentText,
                taskText: taskText // Отдаем сырой текст, очистим его в Python
            });
        }
    }
    return tasks;
}'''


async def fetch_missing_ids():
    def sync_fetch():
        req = urllib.request.Request(WEBHOOK_URL)
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read())
    print("📥 Скачиваем базу заданий без ID...")
    return await asyncio.to_thread(sync_fetch)


async def send_updates(updates_batch):
    if not updates_batch: return
    data = json.dumps(updates_batch).encode('utf-8')
    req = urllib.request.Request(WEBHOOK_URL, data=data, headers={'Content-Type': 'application/json'})
    def sync_send():
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read())
    await asyncio.to_thread(sync_send)
    print(f"📤 Успешно сохранено {len(updates_batch)} ID в таблицу!")


async def run_collector():
    rows_to_process = await fetch_missing_ids()
    if not rows_to_process:
        print("✅ Все задания в датасете уже имеют answer_id.")
        return

    tasks_by_work = defaultdict(list)
    for row in rows_to_process:
        tasks_by_work[row['work_id']].append(row)
        
    print(f"📊 Найдено {len(rows_to_process)} пустых строк, страниц: {len(tasks_by_work)}.")

    async with async_playwright() as p:
        print(">>> Запускаю браузер...")
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()
        await stealth_async(page)
        
        print(">>> Выполняю вход...")
        await page.goto(LOGIN_URL)
        await page.fill('input[name="email"]', USER_EMAIL)
        await page.fill('input[name="password"]', USER_PASSWORD)
        await page.click('button[type="submit"]')
        await page.wait_for_load_state('domcontentloaded')
        print(">>> Успешный вход!")
        
        updates_to_send = []

        for work_id, dataset_rows in tasks_by_work.items():
            url = f"https://admin.100points.ru/student_homework/view/{work_id}?status=checked&from=from_homework"
            print(f"\n🔍 Перехожу на работу {work_id} ({len(dataset_rows)} заданий)...")
            
            try:
                await page.goto(url, timeout=60000)
                await page.wait_for_load_state('domcontentloaded')
                await asyncio.sleep(8) 
                
                parsed_tasks = await page.evaluate(PARSE_PAGE_JS)
                
                if not parsed_tasks:
                    print(f"   [!] На странице не найдены задания с answerId. Пропуск.")
                    continue

                for row in dataset_rows:
                    target_comment = normalize_text(row['ai_comment'])
                    target_condition = normalize_text(row['task_text']) # В таблице текст уже чистый
                    
                    matched_id = None
                    candidates = []
                    
                    for t in parsed_tasks:
                        page_comment = normalize_text(t['commentText'])
                        if target_comment and (target_comment in page_comment or page_comment in target_comment):
                            candidates.append(t)
                            
                    if len(candidates) == 1:
                        matched_id = candidates[0]['answerId']
                    elif len(candidates) > 1:
                        for t in candidates:
                            # 1. Применяем ту же самую очистку к сырому тексту со страницы
                            cleaned_page_condition = clean_task_text(t['taskText'])
                            # 2. Нормализуем для идеального сравнения
                            page_condition = normalize_text(cleaned_page_condition)
                            
                            if target_condition == page_condition:
                                matched_id = t['answerId']
                                break
                    else:
                        print(f"   [!] Для строки {row['row']} не найден комментарий.")

                    if matched_id:
                        print(f"   ✅ Строка {row['row']} -> Найден ID: {matched_id}")
                        updates_to_send.append({"row": row['row'], "answer_id": matched_id})
                        
                if len(updates_to_send) >= 15:
                    await send_updates(updates_to_send)
                    updates_to_send.clear()

            except Exception as e:
                print(f"   [ОШИБКА] Работа {work_id}: {e}")

        if updates_to_send:
            await send_updates(updates_to_send)

        print("\n🎉 Работа сборщика ID завершена!")

if __name__ == "__main__":
    asyncio.run(run_collector())
