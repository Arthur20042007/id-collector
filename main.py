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

# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================
WEBHOOK_URL = os.getenv("GOOGLE_WEBHOOK_URL")
USER_EMAIL = os.getenv("USER_EMAIL", "твой_email")
USER_PASSWORD = os.getenv("USER_PASSWORD", "твой_пароль")
LOGIN_URL = "https://admin.100points.ru/login"

# JS скрипт для вытаскивания ID и Комментариев прямо со страницы
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
            }
            if (node.innerText) {
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

        // Ищем текст комментария куратора (ИИ)
        let editor = allDesc.find(el => el.classList && el.classList.contains('note-editable'));
        if (editor) {
            commentText = editor.innerText;
        } else {
            // Если работа проверена и редактора нет, текст может быть в простом блоке
            let commentBlock = allDesc.find(el => el.innerText && el.innerText.includes('Оценка куратора'));
            if(commentBlock) {
                commentText = commentBlock.innerText;
            }
        }

        if (answerId) {
            tasks.push({
                answerId: answerId,
                commentText: commentText,
                taskText: taskText
            });
        }
    }
    return tasks;
}'''


def normalize_text(text):
    """Удаляет пробелы, переносы и пунктуацию для 100% точного сравнения строк."""
    if not text:
        return ""
    return re.sub(r'[\s\W_]+', '', str(text)).lower()


async def fetch_missing_ids():
    """Забирает из Гугл Таблицы строки без answer_id"""
    def sync_fetch():
        req = urllib.request.Request(WEBHOOK_URL)
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read())
    print("📥 Скачиваем базу заданий без ID...")
    return await asyncio.to_thread(sync_fetch)


async def send_updates(updates_batch):
    """Отправляет найденные answer_id обратно в Гугл Таблицу"""
    if not updates_batch: return
    
    data = json.dumps(updates_batch).encode('utf-8')
    req = urllib.request.Request(WEBHOOK_URL, data=data, headers={'Content-Type': 'application/json'})
    
    def sync_send():
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read())
            
    res = await asyncio.to_thread(sync_send)
    print(f"📤 Успешно сохранено {len(updates_batch)} ID в таблицу!")


async def run_collector():
    # 1. Получаем данные из Гугл Таблицы
    rows_to_process = await fetch_missing_ids()
    if not rows_to_process:
        print("✅ Все задания в датасете уже имеют answer_id. Работа завершена!")
        return

    # 2. Группируем задания по work_id, чтобы не заходить на одну страницу 10 раз
    tasks_by_work = defaultdict(list)
    for row in rows_to_process:
        tasks_by_work[row['work_id']].append(row)
        
    print(f"📊 Найдено {len(rows_to_process)} пустых строк, сгруппировано в {len(tasks_by_work)} уникальных страниц (work_id).")

    # 3. Запускаем браузер
    async with async_playwright() as p:
        print(">>> Запускаю браузер...")
        browser = await p.chromium.launch(headless=False) # Можно поставить True
        context = await browser.new_context()
        page = await context.new_page()
        await stealth_async(page)
        
        # Логинимся
        print(">>> Выполняю вход...")
        await page.goto(LOGIN_URL)
        await page.fill('input[name="email"]', USER_EMAIL)
        await page.fill('input[name="password"]', USER_PASSWORD)
        await page.click('button[type="submit"]')
        await page.wait_for_load_state('domcontentloaded')
        print(">>> Успешный вход!")
        
        updates_to_send = []

        # 4. Проходимся по каждой работе (work_id)
        for work_id, dataset_rows in tasks_by_work.items():
            url = f"https://admin.100points.ru/student_homework/view/{work_id}?status=checked&from=from_homework"
            print(f"\n🔍 Перехожу на работу {work_id} ({len(dataset_rows)} заданий нужно найти)...")
            
            try:
                await page.goto(url, timeout=60000)
                await page.wait_for_load_state('domcontentloaded')
                await asyncio.sleep(2) # Даем прогрузиться JS на сайте
                
                # Парсим все задания на странице
                parsed_tasks = await page.evaluate(PARSE_PAGE_JS)
                
                if not parsed_tasks:
                    print(f"   [!] На странице не найдено ни одного answerId. Пропуск.")
                    continue

                # 5. Сопоставляем задания из датасета с заданиями на сайте
                for row in dataset_rows:
                    target_comment = normalize_text(row['ai_comment'])
                    target_condition = normalize_text(row['task_text'])
                    
                    matched_id = None
                    candidates = []
                    
                    # Ищем совпадение по комментарию
                    for t in parsed_tasks:
                        page_comment = normalize_text(t['commentText'])
                        if target_comment and (target_comment in page_comment or page_comment in target_comment):
                            candidates.append(t)
                            
                    if len(candidates) == 1:
                        # Если совпадение только одно - это 100% наше задание
                        matched_id = candidates[0]['answerId']
                    elif len(candidates) > 1:
                        # Если комментарии одинаковые (например, два раза "Все верно"), проверяем текст условия
                        for t in candidates:
                            page_condition = normalize_text(t['taskText'])
                            # Сверяем первые 50 символов (этого хватит, чтобы отличить условия)
                            if target_condition[:50] in page_condition or page_condition[:50] in target_condition:
                                matched_id = t['answerId']
                                break
                    else:
                        print(f"   [!] Для строки {row['row']} не найден комментарий на странице.")

                    if matched_id:
                        print(f"   ✅ Строка {row['row']} -> Найден answer_id: {matched_id}")
                        updates_to_send.append({"row": row['row'], "answer_id": matched_id})
                        
                # 6. Отправляем батч каждые N работ (чтобы данные сохранялись по ходу)
                if len(updates_to_send) >= 15:
                    await send_updates(updates_to_send)
                    updates_to_send.clear()

            except Exception as e:
                print(f"   [ОШИБКА] Не удалось обработать работу {work_id}: {e}")

        # Отправляем остатки
        if updates_to_send:
            await send_updates(updates_to_send)

        print("\n🎉 Работа сборщика ID завершена!")


if __name__ == "__main__":
    asyncio.run(run_collector())
