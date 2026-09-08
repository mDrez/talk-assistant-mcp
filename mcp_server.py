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


# НАСТРОЙКА ЛОГИРОВАНИЯ

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=[logging.StreamHandler()]
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
    headers = {"Cookie": f"sessionToken={session_token}"}
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

    url = f"{base_url}/api/recordings/{recording_id}"
    headers = {"Cookie": f"sessionToken={session_token}"}

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        data = response.json()
        summary = (
            data.get("summary")
            or data.get("description")
            or data.get("aiSummary")
            or "Саммари отсутствует."
        )

        participants = data.get("participants", [])
        names = []
        for p in participants:
            user_info = p.get("userInfo", {})
            first = user_info.get("firstname", "")
            last = user_info.get("surname", "")
            full = f"{first} {last}".strip()
            if full:
                names.append(full)

        result = [
            f"Встреча: {data.get('title', 'Без названия')}",
            f"Участники: {', '.join(names) if names else 'Не указаны'}",
            "",
            "--- Саммари ---",
            summary[:1000]
        ]
        return "\n".join(result)

    except requests.exceptions.RequestException as e:
        logger.error("Ошибка API для записи %s: %s", recording_id, e)
        return f"Ошибка API: {e}"
    except Exception as e:
        logger.exception("Неожиданная ошибка в get_meeting_summary")
        return f"Неожиданная ошибка: {e}"



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

@mcp.tool()
def get_participant_emails(recording_id: str) -> str:
    """
    Возвращает список email-адресов участников встречи.
    """
    logger.info("Запрос email участников для %s", recording_id)

    url = f"{base_url}/api/recordings/{recording_id}"
    headers = {"Cookie": f"sessionToken={session_token}"}

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
    Отправляет саммари встречи всем участникам на email.
    """
    logger.info("Отправка саммари для %s всем участникам", recording_id)

    summary = get_meeting_summary(recording_id)

    url = f"{base_url}/api/recordings/{recording_id}"
    headers = {"Cookie": f"sessionToken={session_token}"}

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        data = response.json()
        participants = data.get("participants", [])

        if not participants:
            logger.info("Нет участников для %s", recording_id)
            return "Нет участников для отправки."

        emails = []
        for p in participants:
            user_info = p.get("userInfo", {})
            full_login = user_info.get("login", "")
            login = _extract_login(full_login)
            if login:
                emails.append(_get_email_from_login(login))

        if not emails:
            logger.warning("Не найдено email-адресов для %s", recording_id)
            return "Не найдено email-адресов участников."

        title = data.get("title", "Без названия")
        subject = f"Протокол встречи: {title}"
        body = summary

        sent = 0
        errors = []

        for email in emails:
            try:
                send_email_to(email, subject, body)
                sent += 1
            except Exception as e:
                logger.error("Ошибка отправки на %s: %s", email, e)
                errors.append(email)

        result = [f"Отправлено: {sent} из {len(emails)}"]
        if errors:
            result.append(f"Не удалось отправить: {', '.join(errors)}")

        logger.info("Отправка саммари завершена для %s: %d/%d", recording_id, sent, len(emails))
        return "\n".join(result)

    except requests.exceptions.RequestException as e:
        logger.error("Ошибка API для %s: %s", recording_id, e)
        return f"Ошибка API: {e}"
    except Exception as e:
        logger.exception("Неожиданная ошибка в send_summary_to_participants")
        return f"Неожиданная ошибка: {e}"



# HEALTH CHECK (НА ТОМ ЖЕ ПОРТУ)

async def healthz(request):
    return Response("OK", status_code=200)

# Создаём ASGI-приложение с health-check и MCP
mcp_app = mcp.streamable_http_app()
app = Starlette(routes=[
    Route("/healthz", healthz),
    Mount("/", app=mcp_app),
])



# ТОЧКА ВХОДА

if __name__ == "__main__":
    logger.info("Запуск MCP-сервера на порту 9999")
    logger.info("Health-check доступен по адресу: /healthz")
    uvicorn.run(app, host="0.0.0.0", port=9999)