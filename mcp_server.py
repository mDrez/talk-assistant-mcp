import os
import logging
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route, Mount
import uvicorn
import requests.utils

def _safe_cookie_token(token: str) -> str:
    return requests.utils.quote(token, safe='')


# НАСТРОЙКА ЛОГИРОВАНИЯ

LOG_DIR = "/app/logs"
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=[
        logging.FileHandler(f"{LOG_DIR}/mcp_server.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("mcp-server")

load_dotenv()


# КОНФИГУРАЦИЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ

session_token = os.getenv("KTALK_SESSION_TOKEN")
base_url = os.getenv("KTALK_BASE_URL")

smtp_host = os.getenv("SMTP_HOST")
smtp_port = int(os.getenv("SMTP_PORT", 587))
smtp_user = os.getenv("SMTP_USER")
smtp_password = os.getenv("SMTP_PASSWORD")
smtp_from = os.getenv("SMTP_FROM")


# СОЗДАНИЕ MCP-СЕРВЕРА

mcp = MCPServer("talk-assistant")



# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ

def _extract_login(full_login: str) -> str:
    """Извлекает логин из формата domain\\username."""
    if "\\" in full_login:
        return full_login.split("\\")[-1]
    return full_login


def _get_email_from_login(login: str) -> str:
    """Формирует email из логина."""
    return f"{login}@skbkontur.ru"



# ИНСТРУМЕНТ 1: ПОЛУЧЕНИЕ СПИСКА ЗАПИСЕЙ

@mcp.tool()
def get_recordings(limit: int = 5) -> str:
    """
    Возвращает список последних записей встреч из Толка.
    """
    logger.info("Запрос записей, лимит=%d", limit)

    url = f"{base_url}/api/recordings"
    headers = {"Cookie": f"sessionToken={_safe_cookie_token(session_token)}"}
    params = {"limit": limit}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()

        data = response.json()
        items = data.get("recordings", [])

        if not items:
            logger.info("Записи не найдены")
            return "Записи не найдены."

        result = [f"Найдено записей: {len(items)}"]
        for rec in items[:min(limit, len(items))]:
            result.append(
                f"  - {rec.get('title', 'Без названия')} "
                f"(ID: {rec.get('id', 'N/A')}, "
                f"создана: {rec.get('createdDate', 'N/A')}, "
                f"статус: {rec.get('status', 'N/A')})"
            )
        return "\n".join(result)

    except requests.exceptions.RequestException as e:
        logger.error("Ошибка API: %s", e)
        return f"Ошибка API: {e}"
    except Exception as e:
        logger.exception("Неожиданная ошибка в get_recordings")
        return f"Неожиданная ошибка: {e}"



# ИНСТРУМЕНТ 2: ПОЛУЧЕНИЕ САММАРИ ВСТРЕЧИ

@mcp.tool()
def get_meeting_summary(recording_id: str) -> str:
    """
    Возвращает краткое саммари встречи по её ID.
    """
    logger.info("Запрос саммари для записи %s", recording_id)

    headers = {"Cookie": f"sessionToken={_safe_cookie_token(session_token)}"}
    summary = None

    # 1. Пробуем получить саммари через /summary
    summary_url = f"{base_url}/api/recordings/{recording_id}/summary"
    try:
        response = requests.get(summary_url, headers=headers, timeout=30)
        if response.status_code == 200:
            data = response.json()

            # Проверяем наличие chunks
            chunks = data.get('summary', {}).get('chunks', [])
            if chunks:
                import json
                parts = []
                for chunk in chunks:
                    try:
                        chunk_text = json.loads(chunk.get('text', '{}'))
                        headline = chunk_text.get('headline', '')
                        text = chunk_text.get('summary', '')
                        if headline:
                            parts.append(f"### {headline}")
                        if text:
                            parts.append(text)
                    except:
                        parts.append(chunk.get('text', ''))
                summary = "\n\n".join(parts)

            # Если нет chunks, пробуем другие поля
            if not summary:
                summary = data.get('summary') or data.get('text') or data.get('content')

    except Exception as e:
        logger.warning(f"Could not fetch summary from /summary: {e}")

    # 2. Если не получилось - пробуем из основной записи
    if not summary:
        try:
            url = f"{base_url}/api/recordings/{recording_id}"
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()

            summary = data.get('summary') or data.get('aiSummary') or data.get('description')
        except Exception as e:
            logger.error("Ошибка получения записи: %s", e)
            return f"Ошибка: {e}"

    # 3. ГАРАНТИРУЕМ, что summary - строка
    if summary is None:
        summary = "Саммари отсутствует."
    elif not isinstance(summary, str):
        logger.warning("Преобразую summary из %s в строку", type(summary))
        summary = str(summary)

    # 4. Получаем участников
    try:
        url = f"{base_url}/api/recordings/{recording_id}"
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()

        participants = data.get('participants', [])
        names = []
        for p in participants:
            user_info = p.get('userInfo', {})
            first = user_info.get('firstname', '')
            last = user_info.get('surname', '')
            full = f"{first} {last}".strip()
            if full:
                names.append(full)

        result = [
            f"Встреча: {data.get('title', 'Без названия')}",
            f"Участники: {', '.join(names) if names else 'Не указаны'}",
            "",
            "--- Саммари ---",
            summary[:2000]  # Теперь безопасно - summary точно строка
        ]
        return "\n".join(result)

    except Exception as e:
        logger.exception("Ошибка в get_meeting_summary")
        return f"Ошибка: {str(e)}"



# ИНСТРУМЕНТ 3: ОТПРАВКА ПИСЬМА

@mcp.tool()
def send_email_to(recipient: str, subject: str, body: str) -> str:
    """
    Отправляет письмо через корпоративный SMTP.
    """
    logger.info("Отправка письма на %s", recipient)

    try:
        msg = MIMEMultipart()
        msg["From"] = smtp_from
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)

        logger.info("Письмо успешно отправлено на %s", recipient)
        return f"Письмо отправлено на {recipient}"

    except smtplib.SMTPException as e:
        logger.error("SMTP ошибка для %s: %s", recipient, e)
        raise RuntimeError(f"SMTP ошибка: {e}")
    except Exception as e:
        logger.exception("Неожиданная ошибка при отправке письма на %s", recipient)
        raise RuntimeError(f"Неожиданная ошибка: {e}")



# ИНСТРУМЕНТ 4: ПОЛУЧЕНИЕ EMAIL УЧАСТНИКОВ

# ИНСТРУМЕНТ 4: ПОЛУЧЕНИЕ EMAIL УЧАСТНИКОВ

@mcp.tool()
def get_participant_emails(recording_id: str) -> str:
    """
    Возвращает список email-адресов участников встречи.
    """
    logger.info("Запрос email участников для %s", recording_id)

    url = f"{base_url}/api/recordings/{recording_id}"
    headers = {"Cookie": f"sessionToken={_safe_cookie_token(session_token)}"}

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        data = response.json()
        participants = data.get("participants", [])

        if not participants:
            logger.info("Участники не найдены для %s", recording_id)
            return "Участники не найдены."

        result = [f"Участники ({len(participants)}):"]
        emails = []

        for p in participants:
            user_info = p.get("userInfo", {})
            full_login = user_info.get("login", "")
            name = f"{user_info.get('firstname', '')} {user_info.get('surname', '')}".strip()

            login = _extract_login(full_login)

            if login:
                email = _get_email_from_login(login)
                emails.append(email)
                result.append(f"  - {name or login} -> {email}")
            else:
                result.append(f"  - {name or 'Неизвестно'} -> нет логина")

        result.append("")
        result.append("Email-адреса для отправки:")
        result.extend(f"  - {e}" for e in emails)

        return "\n".join(result)

    except requests.exceptions.RequestException as e:
        logger.error("Ошибка API для %s: %s", recording_id, e)
        return f"Ошибка API: {e}"
    except Exception as e:
        logger.exception("Неожиданная ошибка в get_participant_emails")
        return f"Неожиданная ошибка: {e}"

# ИНСТРУМЕНТ 5: ОТПРАВКА САММАРИ ВСЕМ УЧАСТНИКАМ

@mcp.tool()
def send_summary_to_participants(recording_id: str) -> str:
    """
    Отправляет саммари встречи всем участникам на email в HTML формате.
    """
    logger.info("Отправка саммари для %s всем участникам", recording_id)

    # Получаем саммари в формате markdown
    summary_result = get_meeting_summary(recording_id)

    # Извлекаем название встречи
    url = f"{base_url}/api/recordings/{recording_id}"
    headers = {"Cookie": f"sessionToken={_safe_cookie_token(session_token)}"}

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()

        title = data.get('title', 'Без названия')
        participants = data.get('participants', [])

        if not participants:
            logger.info("Нет участников для %s", recording_id)
            return "Нет участников для отправки."

        # Собираем emails участников
        emails = []
        for p in participants:
            user_info = p.get('userInfo', {})
            full_login = user_info.get('login', '')
            login = _extract_login(full_login)
            if login:
                emails.append(_get_email_from_login(login))

        if not emails:
            logger.warning("Не найдено email-адресов для %s", recording_id)
            return "Не найдено email-адресов участников."

        # Конвертируем markdown в HTML
        html_summary = markdown.markdown(summary_result, extensions=['extra'])

        # Формируем красивое HTML письмо
        html_body = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }}
                h1 {{ color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }}
                h2 {{ color: #2c3e50; margin-top: 20px; }}
                h3 {{ color: #34495e; }}
                ul {{ padding-left: 20px; }}
                li {{ margin-bottom: 8px; }}
                .header {{ background: #f8f9fa; padding: 15px; border-radius: 5px; margin-bottom: 20px; }}
                .footer {{ margin-top: 30px; padding-top: 10px; border-top: 1px solid #ddd; color: #7f8c8d; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1>📋 Протокол встречи</h1>
                <p><strong>Название:</strong> {title}</p>
                <p><strong>Дата:</strong> {data.get('createdDate', 'Не указана')}</p>
                <p><strong>Участников:</strong> {len(participants)}</p>
            </div>

            {html_summary}

            <div class="footer">
                <p>Это письмо сгенерировано автоматически через Толк MCP Бот.</p>
                <p>Для ответа используйте обычную почту.</p>
            </div>
        </body>
        </html>
        """

        # Отправляем всем участникам
        subject = f"Протокол встречи: {title}"
        sent = 0
        errors = []

        for email in emails:
            try:
                # Используем существующую функцию send_email_to, но с HTML
                msg = MIMEMultipart()
                msg["From"] = smtp_from
                msg["To"] = email
                msg["Subject"] = subject
                msg.attach(MIMEText(html_body, "html"))

                with smtplib.SMTP(smtp_host, smtp_port) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_password)
                    server.send_message(msg)

                sent += 1
                logger.info("Письмо отправлено на %s", email)
            except Exception as e:
                logger.error("Ошибка отправки на %s: %s", email, e)
                errors.append(email)

        result = [f"Отправлено: {sent} из {len(emails)}"]
        if errors:
            result.append(f"Не удалось отправить: {', '.join(errors)}")

        logger.info("Отправка саммари завершена для %s: %d/%d", recording_id, sent, len(emails))
        return "\n".join(result)

    except Exception as e:
        logger.exception("Ошибка в send_summary_to_participants")
        return f"Ошибка: {str(e)}"






if __name__ == "__main__":
    logger.info("Starting MCP server on port 9999")
    logger.info("Health-check available at: /healthz")
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=9999
    )