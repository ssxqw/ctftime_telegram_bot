"""CTFtime notifications and team RSVP for a Telegram forum chat."""

from __future__ import annotations

import html
import json
import logging
import os
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bee_phrases import BEE_PHRASES

LOG = logging.getLogger("ctf_bot")
CTFTIME = "https://ctftime.org/api/v1"
TELEGRAM = "https://api.telegram.org/bot"
TEAM_NAMES = {451320: "Shalena Bdj1lka", 376454: "Shalena Bdjilka"}


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value else None


def parse_destination(parts: list[str]) -> tuple[int, int | None]:
    """Accept numeric IDs or a Telegram link to the root of a forum topic."""
    if len(parts) == 3:
        chat_id, topic_id = int(parts[1]), int(parts[2])
    elif len(parts) == 2 and parts[1].startswith(("https://", "http://")):
        link = urlsplit(parts[1])
        if link.scheme != "https" or link.hostname not in {"t.me", "telegram.me"}:
            raise ValueError("Нужна ссылка https://t.me/c/…")
        path = link.path.strip("/").split("/")
        if len(path) not in {2, 3, 4} or path[0] != "c" or not all(x.isdecimal() for x in path[1:]):
            raise ValueError("Нужна ссылка на приватный чат или топик: https://t.me/c/CHAT/TOPIC")
        chat_id = int("-100" + path[1])
        topic_id = int(path[2]) if len(path) >= 3 else None
    elif len(parts) == 2:
        chat_id, topic_id = int(parts[1]), None
    else:
        raise ValueError("Укажите ссылку на топик или ID чата и топика")
    if chat_id == 0 or (topic_id is not None and topic_id <= 0):
        raise ValueError("Неверный ID чата или топика")
    return chat_id, topic_id


def help_text() -> str:
    return (
        "🐝 <b>Настройка CTF-бота</b>\n\n"
        "<b>Быстрый способ</b>\n"
        "Напишите команду прямо в нужном топике:\n"
        "<code>/setevents</code> — анонсы CTF\n"
        "<code>/setachievements</code> — результаты команд\n"
        "<code>/setboth</code> — всё в одном топике\n\n"
        "<b>Настройка в личном чате</b>\n"
        "Пришлите ссылку на корень топика: бот предложит кнопки выбора. "
        "Или добавьте ссылку после команды:\n"
        "<code>/setevents https://t.me/c/2567137543/13162</code>\n"
        "<code>/setachievements https://t.me/c/2567137543/13155</code>\n"
        "<code>/setboth https://t.me/c/2567137543/13162</code>\n\n"
        "Для публикации в основной чат укажите его ID: "
        "<code>/setboth -1002567137543</code>. "
        "ID также можно использовать для топика: "
        "<code>/setevents CHAT_ID TOPIC_ID</code>.\n\n"
        "<b>Полезные команды</b>\n"
        "<code>/chats</code> — найденные чаты\n"
        "<code>/topics CHAT_ID</code> — замеченные топики\n"
        "<code>/status</code> — текущие назначения\n"
        "<code>/where</code> — ID текущего чата и топика\n"
        "<code>/nametopic Название</code> — запомнить имя старого топика\n"
        "<code>/all</code> или <code>/все</code> — пчелиный сбор в группе (для администратора)"
    )


PRIVATE_COMMANDS = [
    {"command": "start", "description": "Начать настройку бота"},
    {"command": "help", "description": "Инструкция и команды"},
    {"command": "chats", "description": "Найденные чаты"},
    {"command": "topics", "description": "Замеченные топики"},
    {"command": "status", "description": "Текущие настройки"},
    {"command": "setevents", "description": "Куда писать анонсы CTF"},
    {"command": "setachievements", "description": "Куда писать результаты"},
    {"command": "setboth", "description": "Один чат или топик для всего"},
    {"command": "where", "description": "ID чата и топика"},
]
GROUP_COMMANDS = [
    {"command": "all", "description": "Позвать участников пчелиными именами"},
    {"command": "where", "description": "ID чата и топика"},
    {"command": "setevents", "description": "Анонсы в этот топик"},
    {"command": "setachievements", "description": "Результаты в этот топик"},
    {"command": "setboth", "description": "Всё в этот топик"},
    {"command": "nametopic", "description": "Сохранить название этого топика"},
    {"command": "help", "description": "Инструкция и команды"},
]


def parse_time(value: str | int | float) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def request_json(url: str, payload: dict | None = None, timeout: int = 30):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"User-Agent": "CTF-team-Telegram-notifier/1.0", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(url, data=body, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read(500).decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Сеть недоступна: {exc.reason}") from exc


def telegram(token: str, method: str, **params):
    result = request_json(f"{TELEGRAM}{token}/{method}", params, timeout=40)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method}: {result.get('description', result)}")
    return result["result"]


def upcoming_events(now: datetime, days: int) -> list[dict]:
    """Fetch short windows so the CTFtime result limit cannot hide nearby events."""
    found: dict[int, dict] = {}
    cursor = now
    end = now + timedelta(days=days)
    while cursor < end:
        window_end = min(cursor + timedelta(days=7), end)
        query = urlencode({
            "limit": 100,
            "start": int(cursor.timestamp()),
            "finish": int(window_end.timestamp()),
        })
        payload = request_json(f"{CTFTIME}/events/?{query}")
        if not isinstance(payload, list):
            raise ValueError("CTFtime вернул неожиданный список событий")
        if len(payload) >= 100:
            LOG.warning("Окно CTFtime достигло лимита в 100 событий: %s", cursor)
        for event in payload:
            try:
                event_id = int(event["id"])
                start = parse_time(event["start"])
                online = event.get("onsite") is False or str(event.get("location", "")).lower() in {"on-line", "online"}
                if now < start <= end and online and str(event.get("format", "")).lower() == "jeopardy":
                    found[event_id] = event
            except (KeyError, TypeError, ValueError):
                LOG.warning("Пропущено событие с неполными данными: %r", event)
        cursor = window_end
    return sorted(found.values(), key=lambda event: event["start"])


def team_results(payload: dict, team_ids: set[int], earliest: datetime, now: datetime):
    if not isinstance(payload, dict):
        raise ValueError("CTFtime вернул неожиданные результаты")
    for event_id, result in payload.items():
        if not isinstance(result, dict):
            continue
        try:
            finished = parse_time(result["time"])
            if not earliest <= finished <= now:
                continue
            for score in result.get("scores", []):
                if not isinstance(score, dict):
                    continue
                score_team_id = int(score.get("team_id", -1))
                if score_team_id in team_ids:
                    yield score_team_id, int(event_id), result, score
        except (KeyError, TypeError, ValueError):
            LOG.warning("Пропущен результат с неполными данными: %s", event_id)


def display_name(user: dict) -> str:
    username = user.get("username")
    if username:
        return "@" + username
    return " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or str(user["id"])


def buttons(event_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Участвую", "callback_data": f"vote:{event_id}:yes"},
        {"text": "❌ Не участвую", "callback_data": f"vote:{event_id}:no"},
    ]]}


def bee_mention_messages(members, rng=None) -> list[str]:
    """Build short HTML messages with a different bee nickname for each user."""
    rng = rng or random.SystemRandom()
    pool: list[str] = []
    lines: list[str] = []
    for member in members:
        if not pool:
            pool = list(BEE_PHRASES)
            rng.shuffle(pool)
        phrase = html.escape(pool.pop())
        username = member["username"]
        who = f"@{username}" if username else member["nick"]
        lines.append(f'🐝 <a href="tg://user?id={int(member["user_id"])}">{phrase}</a> · {html.escape(who)}')
    return ["🍯 <b>Збір у вулику!</b>\n\n" + "\n".join(lines[i:i + 40])
            for i in range(0, len(lines), 40)]


def normalized_event(event: dict) -> dict:
    location = str(event.get("location") or "")
    onsite = event.get("onsite") is True
    return {
        "id": int(event["id"]),
        "title": str(event["title"]),
        "start": parse_time(event["start"]).astimezone(timezone.utc).isoformat(),
        "finish": parse_time(event["finish"]).astimezone(timezone.utc).isoformat(),
        "format": str(event.get("format") or "Не указан"),
        "onsite": onsite,
        "location": location if onsite else "",
    }


def event_text(event: dict, yes_names: list[str], no_count: int, zone) -> str:
    event = normalized_event(event)
    start = parse_time(event["start"]).astimezone(zone).strftime("%d.%m.%Y %H:%M")
    finish = parse_time(event["finish"]).astimezone(zone).strftime("%d.%m.%Y %H:%M")
    title = html.escape(str(event["title"]))
    format_name = html.escape(event["format"])
    venue = html.escape(event["location"]) if event["onsite"] else "онлайн"
    url = f"https://ctftime.org/event/{int(event['id'])}/"
    lines = [f"• {html.escape(name)}" for name in yes_names]
    participants = "\n".join(lines)
    if len(participants) > 1800:
        participants = participants[:1800].rsplit("\n", 1)[0] + "\n• …"
    if not participants:
        participants = "Пока никто не записался"
    return (
        f"🏴‍☠️ <b>СКОРО CTF</b>\n\n"
        f"<b><a href=\"{url}\">{title}</a></b>\n\n"
        f"📅 Начало: <b>{start}</b>\n"
        f"🏁 Конец: <b>{finish}</b>\n"
        f"🕒 Время: Киев\n"
        f"🧩 Формат: <b>{format_name}</b> · {venue}\n\n"
        f"👥 <b>Участники ({len(yes_names)})</b>\n{participants}\n\n"
        f"✅ Участвуют: {len(yes_names)} · ❌ Не участвуют: {no_count}\n"
        f"🔗 <a href=\"{url}\">Открыть на CTFtime</a>"
    )


def achievement_text(team_id: int, event_id: int, result: dict, score: dict) -> str:
    title = html.escape(str(result.get("title") or f"CTF #{event_id}"))
    team_name = html.escape(TEAM_NAMES.get(team_id, f"Команда #{team_id}"))
    place = html.escape(str(score.get("place", "?")))
    points = score.get("points")
    points_line = f"\n🎯 Очки: <b>{html.escape(str(points))}</b>" if points is not None else ""
    return (
        f"🏆 <b>Результат команды</b>\n"
        f"👥 <a href=\"https://ctftime.org/team/{team_id}/\">{team_name}</a>\n"
        f"<a href=\"https://ctftime.org/event/{event_id}/\">{title}</a>\n"
        f"🥇 Место: <b>{place}</b>{points_line}"
    )


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                start TEXT NOT NULL,
                finish TEXT NOT NULL,
                url TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                chat_id INTEGER,
                details_json TEXT
            );
            CREATE TABLE IF NOT EXISTS votes (
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                choice TEXT NOT NULL CHECK(choice IN ('yes', 'no')),
                nick TEXT NOT NULL,
                PRIMARY KEY (event_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS achievements (
                team_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (team_id, event_id)
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seen_chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                kind TEXT NOT NULL,
                is_forum INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS seen_topics (
                chat_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                name TEXT,
                PRIMARY KEY (chat_id, topic_id)
            );
            CREATE TABLE IF NOT EXISTS seen_members (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                nick TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (chat_id, user_id)
            );
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(achievements)")}
        if "team_id" not in columns:
            with self.db:
                self.db.execute("CREATE TABLE achievements_v2 (team_id INTEGER NOT NULL, event_id INTEGER NOT NULL, "
                                "message_id INTEGER NOT NULL, PRIMARY KEY (team_id, event_id))")
                self.db.execute("INSERT INTO achievements_v2 SELECT 451320, event_id, message_id FROM achievements")
                self.db.execute("DROP TABLE achievements")
                self.db.execute("ALTER TABLE achievements_v2 RENAME TO achievements")
        event_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(events)")}
        if "details_json" not in event_columns:
            self.db.execute("ALTER TABLE events ADD COLUMN details_json TEXT")
        if "chat_id" not in event_columns:
            self.db.execute("ALTER TABLE events ADD COLUMN chat_id INTEGER")

    def event(self, event_id: int):
        return self.db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()

    def save_event(self, event: dict, message_id: int, chat_id: int):
        details = normalized_event(event)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO events (event_id,title,start,finish,url,message_id,chat_id,details_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (details["id"], details["title"], details["start"], details["finish"],
                 f"https://ctftime.org/event/{details['id']}/", message_id, chat_id,
                 json.dumps(details, ensure_ascii=False, sort_keys=True)),
            )

    def fill_event_chat(self, chat_id: int | None):
        if chat_id is not None:
            with self.db:
                self.db.execute("UPDATE events SET chat_id=? WHERE chat_id IS NULL", (chat_id,))

    def event_data(self, row) -> dict:
        if row["details_json"]:
            return json.loads(row["details_json"])
        return normalized_event({"id": row["event_id"], "title": row["title"],
                                 "start": row["start"], "finish": row["finish"],
                                 "format": "Jeopardy", "onsite": False})

    def update_event(self, event: dict):
        details = normalized_event(event)
        with self.db:
            self.db.execute("UPDATE events SET title=?,start=?,finish=?,details_json=? WHERE event_id=?",
                            (details["title"], details["start"], details["finish"],
                             json.dumps(details, ensure_ascii=False, sort_keys=True), details["id"]))

    def tracked_events(self, now: datetime):
        rows = self.db.execute("SELECT * FROM events").fetchall()
        return [row for row in rows if parse_time(row["finish"]) >= now - timedelta(days=7)]

    def vote(self, event_id: int, user_id: int, choice: str, nick: str):
        with self.db:
            self.db.execute(
                "INSERT INTO votes VALUES (?, ?, ?, ?) "
                "ON CONFLICT(event_id,user_id) DO UPDATE SET choice=excluded.choice,nick=excluded.nick",
                (event_id, user_id, choice, nick),
            )

    def votes(self, event_id: int) -> tuple[list[str], int]:
        rows = self.db.execute("SELECT choice,nick FROM votes WHERE event_id=? ORDER BY nick COLLATE NOCASE", (event_id,))
        yes, no = [], 0
        for row in rows:
            if row["choice"] == "yes":
                yes.append(row["nick"])
            else:
                no += 1
        return yes, no

    def achievement_exists(self, team_id: int, event_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM achievements WHERE team_id=? AND event_id=?",
                               (team_id, event_id)).fetchone() is not None

    def save_achievement(self, team_id: int, event_id: int, message_id: int):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO achievements VALUES (?, ?, ?)",
                            (team_id, event_id, message_id))

    def result_cutoff(self, now: datetime, backfill_days: int) -> datetime:
        row = self.db.execute("SELECT value FROM metadata WHERE key='result_cutoff'").fetchone()
        if row:
            return parse_time(row["value"])
        cutoff = now - timedelta(days=backfill_days)
        with self.db:
            self.db.execute("INSERT INTO metadata VALUES ('result_cutoff', ?)", (cutoff.isoformat(),))
        return cutoff

    def config(self, key: str) -> int | None:
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return int(row["value"]) if row else None

    def set_owner(self, user_id: int):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('owner_user_id', ?)", (str(user_id),))

    def set_destination(self, kind: str, chat_id: int, topic_id: int | None):
        if kind not in {"events", "achievements"}:
            raise ValueError("Неизвестный тип назначения")
        with self.db:
            self.db.execute("INSERT INTO metadata VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (f"{kind}_chat_id", str(chat_id)))
            self.db.execute("INSERT INTO metadata VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (f"{kind}_topic_id", str(topic_id or 0)))

    def record_chat(self, chat: dict, active: bool = True):
        if chat.get("type") not in {"group", "supergroup", "channel"}:
            return
        with self.db:
            self.db.execute(
                "INSERT INTO seen_chats VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,kind=excluded.kind,"
                "is_forum=MAX(seen_chats.is_forum,excluded.is_forum),active=excluded.active",
                (chat["id"], chat.get("title") or str(chat["id"]), chat["type"],
                 int(bool(chat.get("is_forum"))), int(active)),
            )

    def record_topic(self, chat_id: int, topic_id: int, name: str | None = None):
        with self.db:
            self.db.execute(
                "INSERT INTO seen_topics VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id,topic_id) DO UPDATE SET name=COALESCE(excluded.name,seen_topics.name)",
                (chat_id, topic_id, name or None),
            )

    def seen_chats(self):
        return self.db.execute("SELECT * FROM seen_chats WHERE active=1 ORDER BY title COLLATE NOCASE").fetchall()

    def chat_title(self, chat_id: int) -> str | None:
        row = self.db.execute("SELECT title FROM seen_chats WHERE chat_id=?", (chat_id,)).fetchone()
        return row["title"] if row else None

    def seen_topics(self, chat_id: int):
        return self.db.execute("SELECT * FROM seen_topics WHERE chat_id=? ORDER BY topic_id",
                               (chat_id,)).fetchall()

    def topic_name(self, chat_id: int, topic_id: int) -> str | None:
        row = self.db.execute("SELECT name FROM seen_topics WHERE chat_id=? AND topic_id=?",
                              (chat_id, topic_id)).fetchone()
        return row["name"] if row else None

    def record_member(self, chat_id: int, user: dict):
        if not user or user.get("is_bot"):
            return
        with self.db:
            self.db.execute(
                "INSERT INTO seen_members VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(chat_id,user_id) DO UPDATE SET username=excluded.username,"
                "nick=excluded.nick,active=1",
                (chat_id, user["id"], user.get("username"), display_name(user)),
            )

    def remove_member(self, chat_id: int, user_id: int):
        with self.db:
            self.db.execute("UPDATE seen_members SET active=0 WHERE chat_id=? AND user_id=?", (chat_id, user_id))

    def seen_members(self, chat_id: int):
        return self.db.execute("SELECT * FROM seen_members WHERE chat_id=? AND active=1 "
                               "ORDER BY nick COLLATE NOCASE", (chat_id,)).fetchall()

    def last_all_at(self, chat_id: int) -> float:
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (f"all_last_{chat_id}",)).fetchone()
        return float(row["value"]) if row else 0.0

    def record_all_at(self, chat_id: int, when: float):
        with self.db:
            self.db.execute("INSERT INTO metadata VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (f"all_last_{chat_id}", str(when)))


class Bot:
    def __init__(self):
        self.token = os.getenv("TG_BOT_TOKEN", "").strip()
        if not self.token:
            raise ValueError("Укажите TG_BOT_TOKEN в .env")
        self.store = Store(Path(os.getenv("DB_PATH", "data/bot.sqlite3")))
        self.owner_user_id = optional_int("BOT_OWNER_ID") or self.store.config("owner_user_id")
        self.load_destinations()
        self.store.fill_event_chat(self.events_chat_id)
        self.force_check = False
        configured_teams = os.getenv("CTFTIME_TEAM_IDS") or os.getenv("CTFTIME_TEAM_ID") or "451320,376454"
        self.team_ids = {int(value.strip()) for value in configured_teams.split(",") if value.strip()}
        if not self.team_ids:
            raise ValueError("Укажите хотя бы один ID в CTFTIME_TEAM_IDS")
        self.lookahead_days = int(os.getenv("EVENT_LOOKAHEAD_DAYS", "7"))
        self.result_backfill_days = int(os.getenv("RESULT_BACKFILL_DAYS", "14"))
        self.check_interval = int(os.getenv("CHECK_INTERVAL_MINUTES", "180")) * 60
        try:
            self.zone = ZoneInfo("Europe/Kyiv")
        except ZoneInfoNotFoundError as exc:
            raise RuntimeError("Установите зависимости: pip install -r requirements.txt") from exc

    def load_destinations(self):
        legacy_chat = optional_int("TG_CHAT_ID") or self.store.config("chat_id")
        events_chat = self.store.config("events_chat_id")
        achievements_chat = self.store.config("achievements_chat_id")
        events_topic = self.store.config("events_topic_id")
        achievements_topic = self.store.config("achievements_topic_id")
        events_chat = events_chat if events_chat is not None else optional_int("EVENTS_CHAT_ID")
        achievements_chat = achievements_chat if achievements_chat is not None else optional_int("ACHIEVEMENTS_CHAT_ID")
        events_topic = events_topic if events_topic is not None else optional_int("EVENTS_TOPIC_ID")
        achievements_topic = achievements_topic if achievements_topic is not None else optional_int("ACHIEVEMENTS_TOPIC_ID")
        events_topic = events_topic or None
        achievements_topic = achievements_topic or None
        self.events_chat_id = events_chat or legacy_chat or achievements_chat
        self.achievements_chat_id = achievements_chat or legacy_chat or events_chat
        self.events_topic_id = events_topic if events_topic is not None else (
            achievements_topic if events_chat is None else None)
        self.achievements_topic_id = achievements_topic if achievements_topic is not None else (
            events_topic if achievements_chat is None else None)
        self.configured = bool(self.events_chat_id and self.achievements_chat_id)

    def destination_label(self, chat_id: int | None, topic_id: int | None) -> str:
        if chat_id is None:
            return "не задано"
        chat = self.store.chat_title(chat_id) or str(chat_id)
        if topic_id is None:
            return f"{chat} · основной чат"
        topic = self.store.topic_name(chat_id, topic_id) or f"топик №{topic_id}"
        return f"{chat} · {topic}"

    def send(self, kind: str, text: str, reply_markup: dict | None = None):
        chat_id = self.events_chat_id if kind == "events" else self.achievements_chat_id
        topic_id = self.events_topic_id if kind == "events" else self.achievements_topic_id
        args = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                    link_preview_options={"is_disabled": True})
        if topic_id is not None:
            args["message_thread_id"] = topic_id
        if reply_markup:
            args["reply_markup"] = reply_markup
        return telegram(self.token, "sendMessage", **args)

    def check(self):
        now = datetime.now(timezone.utc)
        upcoming = upcoming_events(now, self.lookahead_days)
        upcoming_by_id = {int(event["id"]): event for event in upcoming}
        for row in self.store.tracked_events(now):
            event_id = row["event_id"]
            try:
                latest = upcoming_by_id.get(event_id) or request_json(f"{CTFTIME}/events/{event_id}/")
                if not isinstance(latest, dict):
                    raise ValueError("CTFtime вернул неожиданные данные события")
                self.sync_event(row, {**latest, "id": event_id})
            except Exception:
                LOG.exception("Не удалось проверить изменение события %s", event_id)

        for event in upcoming:
            event_id = int(event["id"])
            if self.store.event(event_id):
                continue
            message = self.send("events", event_text(event, [], 0, self.zone), buttons(event_id))
            self.store.save_event(event, message["message_id"], self.events_chat_id)
            LOG.info("Опубликован анонс %s", event_id)

        earliest = self.store.result_cutoff(now, self.result_backfill_days)
        years = range(earliest.year, now.year + 1)
        for year in years:
            payload = request_json(f"{CTFTIME}/results/{year}/")
            for team_id, event_id, result, score in team_results(payload, self.team_ids, earliest, now):
                if self.store.achievement_exists(team_id, event_id):
                    continue
                message = self.send("achievements", achievement_text(team_id, event_id, result, score))
                self.store.save_achievement(team_id, event_id, message["message_id"])
                LOG.info("Опубликован результат команды %s в событии %s", team_id, event_id)

    def sync_event(self, row, latest: dict):
        old = self.store.event_data(row)
        new = normalized_event(latest)
        yes, no = self.store.votes(row["event_id"])
        old_text = event_text(old, yes, no, self.zone)
        new_text = event_text(new, yes, no, self.zone)
        if old_text != new_text:
            try:
                telegram(self.token, "editMessageText", chat_id=row["chat_id"] or self.events_chat_id,
                         message_id=row["message_id"], text=new_text, parse_mode="HTML",
                         link_preview_options={"is_disabled": True}, reply_markup=buttons(row["event_id"]))
            except RuntimeError as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
            LOG.info("Обновлён анонс события %s", row["event_id"])
        if old != new:
            self.store.update_event(new)

    def on_callback(self, callback: dict):
        data = callback.get("data", "")
        if data.startswith("list:"):
            self.on_list_callback(callback)
            return
        if data.startswith("cfg:"):
            self.on_config_callback(callback)
            return
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "vote" or parts[2] not in {"yes", "no"}:
            return
        try:
            event_id = int(parts[1])
        except ValueError:
            return
        message = callback.get("message") or {}
        row = self.store.event(event_id)
        if (not row or message.get("chat", {}).get("id") != (row["chat_id"] or self.events_chat_id)
                or message.get("message_id") != row["message_id"]):
            telegram(self.token, "answerCallbackQuery", callback_query_id=callback["id"], text="Голосование недоступно")
            return
        self.store.vote(event_id, callback["from"]["id"], parts[2], display_name(callback["from"]))
        self.store.record_member(message["chat"]["id"], callback["from"])
        telegram(self.token, "answerCallbackQuery", callback_query_id=callback["id"],
                 text="Ответ сохранён: участвую" if parts[2] == "yes" else "Ответ сохранён: не участвую")
        self.refresh_event(row)

    def on_config_callback(self, callback: dict):
        parts = callback.get("data", "").split(":")
        message = callback.get("message") or {}
        if (len(parts) != 4 or parts[1] not in {"e", "a", "b"}
                or message.get("chat", {}).get("type") != "private"
                or callback.get("from", {}).get("id") != self.owner_user_id):
            telegram(self.token, "answerCallbackQuery", callback_query_id=callback["id"],
                     text="Настройка доступна только владельцу в личном чате")
            return
        try:
            chat_id, raw_topic = int(parts[2]), int(parts[3])
            if chat_id == 0 or raw_topic < 0:
                raise ValueError
        except ValueError:
            telegram(self.token, "answerCallbackQuery", callback_query_id=callback["id"], text="Неверный ID")
            return
        topic_id = raw_topic or None
        problem = self.target_problem(chat_id, topic_id)
        if problem:
            telegram(self.token, "answerCallbackQuery", callback_query_id=callback["id"], text=problem,
                     show_alert=True)
            return
        kinds = {"e": ("events",), "a": ("achievements",), "b": ("events", "achievements")}[parts[1]]
        for kind in kinds:
            self.store.set_destination(kind, chat_id, topic_id)
        self.load_destinations()
        self.force_check = self.configured
        telegram(self.token, "answerCallbackQuery", callback_query_id=callback["id"], text="Сохранено")
        telegram(self.token, "sendMessage", chat_id=message["chat"]["id"],
                 text=f"🐝 Назначение сохранено: {self.destination_label(chat_id, topic_id)}.")

    def target_problem(self, chat_id: int, topic_id: int | None) -> str | None:
        try:
            chat = telegram(self.token, "getChat", chat_id=chat_id)
            if chat.get("type") not in {"group", "supergroup", "channel"}:
                return "Нужна группа, супергруппа или канал. Выберите чат из /chats."
            me = telegram(self.token, "getMe")
            member = telegram(self.token, "getChatMember", chat_id=chat_id, user_id=me["id"])
            if member.get("status") in {"left", "kicked"}:
                return "Бот больше не состоит в выбранном чате."
            if chat.get("type") == "channel" and member.get("status") not in {"administrator", "creator"}:
                return "Для публикации в канале назначьте бота администратором."
            if topic_id is not None:
                if chat.get("type") != "supergroup" or not chat.get("is_forum"):
                    return "Топик можно указать только для супергруппы с топиками."
                telegram(self.token, "sendChatAction", chat_id=chat_id,
                         message_thread_id=topic_id, action="typing")
        except (RuntimeError, KeyError):
            return "Бот не видит этот чат или топик. Откройте /chats и выберите из списка."
        return None

    def topic_menu(self, chat_id: int) -> tuple[str, dict]:
        topics = self.store.seen_topics(chat_id)
        chat_title = self.store.chat_title(chat_id) or str(chat_id)
        if topics:
            names = [row["name"] or f"Топик без названия (№{row['topic_id']})" for row in topics[:20]]
            response = f"🐝 Топики чата «{chat_title}»:\n\n" + "\n".join(f"• {name}" for name in names)
        else:
            response = f"В чате «{chat_title}» бот пока не видел сообщений из топиков."
        response += ("\n\nНажмите кнопку назначения. Для старого топика, которого нет в списке, "
                     "отправьте /where внутри него. Чтобы указать название старого топика, "
                     "напишите в нём /nametopic Название. Переименование топика бот тоже заметит.")
        rows = [[
            {"text": "Анонсы → весь чат", "callback_data": f"cfg:e:{chat_id}:0"},
            {"text": "Результаты → весь чат", "callback_data": f"cfg:a:{chat_id}:0"},
        ], [{"text": "Оба → весь чат", "callback_data": f"cfg:b:{chat_id}:0"}]]
        for row in topics[:20]:
            label = (row["name"] or f"#{row['topic_id']}")[:18]
            rows.append([
                {"text": f"Анонсы → {label}", "callback_data": f"cfg:e:{chat_id}:{row['topic_id']}"},
                {"text": f"Результаты → {label}", "callback_data": f"cfg:a:{chat_id}:{row['topic_id']}"},
            ])
        return response, {"inline_keyboard": rows}

    def on_list_callback(self, callback: dict):
        message = callback.get("message") or {}
        if (message.get("chat", {}).get("type") != "private"
                or callback.get("from", {}).get("id") != self.owner_user_id):
            telegram(self.token, "answerCallbackQuery", callback_query_id=callback["id"], text="Только для владельца")
            return
        try:
            chat_id = int(callback["data"].split(":", 1)[1])
        except ValueError:
            telegram(self.token, "answerCallbackQuery", callback_query_id=callback["id"], text="Неверный ID")
            return
        response, keyboard = self.topic_menu(chat_id)
        telegram(self.token, "answerCallbackQuery", callback_query_id=callback["id"])
        telegram(self.token, "sendMessage", chat_id=message["chat"]["id"],
                 text=response, reply_markup=keyboard)

    def on_chat_member(self, update: dict):
        chat = update.get("chat") or {}
        status = update.get("new_chat_member", {}).get("status")
        active = status in {"member", "administrator", "creator", "restricted"}
        self.store.record_chat(chat, active=active)
        actor_id = update.get("from", {}).get("id")
        if active and actor_id and self.owner_user_id is None:
            try:
                member = telegram(self.token, "getChatMember", chat_id=chat["id"], user_id=actor_id)
                if member.get("status") in {"creator", "administrator"}:
                    self.store.set_owner(actor_id)
                    self.owner_user_id = actor_id
                    LOG.info("Владелец настройки определён при добавлении бота в чат %s", chat["id"])
            except Exception:
                LOG.exception("Не удалось проверить администратора при добавлении в чат")

    def observe_message(self, message: dict):
        chat = message.get("chat") or {}
        if chat.get("type") not in {"group", "supergroup", "channel"}:
            return
        self.store.record_chat(chat)
        self.store.record_member(chat["id"], message.get("from") or {})
        for user in message.get("new_chat_members") or []:
            self.store.record_member(chat["id"], user)
        departed = message.get("left_chat_member") or {}
        if departed.get("id"):
            self.store.remove_member(chat["id"], departed["id"])
        topic_id = message.get("message_thread_id")
        created = message.get("forum_topic_created") or {}
        edited = message.get("forum_topic_edited") or {}
        if created and topic_id is None:
            topic_id = message.get("message_id")
        if topic_id is not None:
            self.store.record_topic(chat["id"], topic_id, created.get("name") or edited.get("name"))

    def on_all(self, message: dict):
        chat_id = message["chat"]["id"]
        sender = message.get("from") or {}
        topic_id = message.get("message_thread_id")
        if not sender.get("id"):
            return
        member = telegram(self.token, "getChatMember", chat_id=chat_id, user_id=sender["id"])
        if member.get("status") not in {"creator", "administrator"}:
            return
        elapsed = time.time() - self.store.last_all_at(chat_id)
        if elapsed < 600:
            args = dict(chat_id=chat_id, text=f"🐝 Повторный сбор через {int((600 - elapsed) / 60) + 1} мин.")
            if topic_id is not None:
                args["message_thread_id"] = topic_id
            telegram(self.token, "sendMessage", **args)
            return
        try:
            for admin in telegram(self.token, "getChatAdministrators", chat_id=chat_id):
                self.store.record_member(chat_id, admin.get("user") or {})
        except Exception:
            LOG.exception("Не удалось дополнить список администраторов для /all")
        members = self.store.seen_members(chat_id)
        messages = bee_mention_messages(members)
        if not messages:
            args = dict(chat_id=chat_id, text="🐝 Пока нет известных участников. Напишите сообщение в чат, чтобы бот вас запомнил.")
            if topic_id is not None:
                args["message_thread_id"] = topic_id
            telegram(self.token, "sendMessage", **args)
            return
        for index, text in enumerate(messages):
            if index:
                time.sleep(3.2)
            args = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                        link_preview_options={"is_disabled": True})
            if topic_id is not None:
                args["message_thread_id"] = topic_id
            telegram(self.token, "sendMessage", **args)
        self.store.record_all_at(chat_id, time.time())

    def refresh_event(self, row):
        event = self.store.event_data(row)
        yes, no = self.store.votes(row["event_id"])
        try:
            telegram(self.token, "editMessageText", chat_id=row["chat_id"] or self.events_chat_id,
                     message_id=row["message_id"],
                     text=event_text(event, yes, no, self.zone), parse_mode="HTML",
                     link_preview_options={"is_disabled": True}, reply_markup=buttons(row["event_id"]))
        except RuntimeError as exc:
            if "message is not modified" not in str(exc).lower():
                LOG.exception("Не удалось обновить счётчик для %s", row["event_id"])

    def on_message(self, message: dict):
        self.observe_message(message)
        text = message.get("text", "")
        parts = text.split()
        if not parts:
            return
        chat_id = message["chat"]["id"]
        topic_id = message.get("message_thread_id")
        chat_type = message["chat"].get("type")
        user_id = message.get("from", {}).get("id")
        if chat_type == "private" and user_id == self.owner_user_id and len(parts) == 1 and text.startswith("https://"):
            try:
                target_chat, target_topic = parse_destination(["/setboth", text])
            except ValueError as exc:
                telegram(self.token, "sendMessage", chat_id=chat_id, text=str(exc))
                return
            problem = self.target_problem(target_chat, target_topic)
            if problem:
                telegram(self.token, "sendMessage", chat_id=chat_id, text=problem)
                return
            raw_topic = target_topic or 0
            keyboard = {"inline_keyboard": [[
                {"text": "📅 Анонсы", "callback_data": f"cfg:e:{target_chat}:{raw_topic}"},
                {"text": "🏆 Результаты", "callback_data": f"cfg:a:{target_chat}:{raw_topic}"},
            ], [{"text": "📌 Оба сюда", "callback_data": f"cfg:b:{target_chat}:{raw_topic}"}]]}
            telegram(self.token, "sendMessage", chat_id=chat_id,
                     text="🐝 Куда публиковать сообщения по этой ссылке?", reply_markup=keyboard)
            return
        command = parts[0].split("@", 1)[0]
        if command in {"/all", "/все"}:
            if chat_type in {"group", "supergroup"}:
                self.on_all(message)
            return
        if command == "/nametopic":
            if chat_type not in {"group", "supergroup"} or topic_id is None:
                response = "Напишите эту команду внутри топика: /nametopic Название"
            elif len(parts) < 2:
                response = "Укажите имя: /nametopic Анонсы CTF"
            elif user_id is None:
                response = "Отправьте команду от своего имени, без анонимного режима администратора."
            else:
                member = telegram(self.token, "getChatMember", chat_id=chat_id, user_id=user_id)
                if member.get("status") not in {"creator", "administrator"}:
                    response = "Название может сохранить только администратор группы."
                else:
                    name = text.split(None, 1)[1].strip()[:128]
                    self.store.record_topic(chat_id, topic_id, name)
                    response = f"🐝 Название топика сохранено: {name}. Теперь оно видно в /topics."
            args = {"chat_id": chat_id, "text": response}
            if topic_id is not None:
                args["message_thread_id"] = topic_id
            telegram(self.token, "sendMessage", **args)
            return
        if command not in {"/start", "/help", "/whoami", "/where", "/status", "/chats", "/topics",
                           "/setevents", "/setachievements", "/setboth"}:
            return
        keyboard = None
        formatted = False
        if command in {"/start", "/help"}:
            response = help_text()
            formatted = True
        elif command == "/whoami":
            response = f"Ваш Telegram ID: {user_id}"
        elif command == "/where":
            response = f"Chat ID: {chat_id}\nTopic ID: {topic_id if topic_id is not None else 'нет'}"
        elif command == "/status":
            if chat_type != "private" or user_id != self.owner_user_id:
                response = "Настройки доступны владельцу в личном чате с ботом."
            else:
                response = (f"📅 Анонсы: {self.destination_label(self.events_chat_id, self.events_topic_id)}\n"
                            f"🏆 Результаты: {self.destination_label(self.achievements_chat_id, self.achievements_topic_id)}\n"
                            f"Команды CTFtime: {', '.join(map(str, sorted(self.team_ids)))}")
        elif command in {"/chats", "/topics"}:
            if chat_type != "private" or user_id != self.owner_user_id:
                response = "Список доступен владельцу в личном чате с ботом."
            elif command == "/chats":
                chats = self.store.seen_chats()
                response = ("Чаты, в которых бот увидел сообщения:\n" +
                            "\n".join(f"• {row['title']} — {row['chat_id']}" for row in chats[:30])) if chats else (
                                "Пока нет известных чатов. Добавьте бота в группу или канал.")
                if chats:
                    response += "\n\nВыберите чат кнопкой или откройте топики: /topics CHAT_ID"
                    keyboard = {"inline_keyboard": [[
                        {"text": f"📂 {row['title'][:32]}", "callback_data": f"list:{row['chat_id']}"}
                    ] for row in chats[:30]]}
            else:
                try:
                    target_chat = parse_destination(["/topics", parts[1]])[0] if len(parts) > 1 else None
                except ValueError:
                    target_chat = None
                if target_chat is None:
                    response = "Укажите ID чата: /topics CHAT_ID. Список чатов: /chats."
                else:
                    response, keyboard = self.topic_menu(target_chat)
        elif chat_type == "private":
            if user_id != self.owner_user_id:
                response = ("Сначала выполните /setevents или /setboth от своего имени администратора "
                            "в целевой группе. Затем настройки будут доступны здесь.")
            elif len(parts) not in {2, 3}:
                response = (f"Формат: {command} https://t.me/c/CHAT/TOPIC\n"
                            f"Или: {command} CHAT_ID [TOPIC_ID]")
            else:
                try:
                    target_chat, target_topic = parse_destination(parts)
                except ValueError as exc:
                    response = str(exc)
                else:
                    problem = self.target_problem(target_chat, target_topic)
                    if problem:
                        response = problem
                    else:
                        kinds = ("events", "achievements") if command == "/setboth" else (
                            ("events",) if command == "/setevents" else ("achievements",))
                        for kind in kinds:
                            self.store.set_destination(kind, target_chat, target_topic)
                        self.load_destinations()
                        self.force_check = self.configured
                        response = "Назначение сохранено. Проверяю события и результаты."
        elif chat_type in {"group", "supergroup"}:
            if user_id is None:
                response = "Отправьте команду от своего имени, без анонимного режима администратора."
            else:
                member = telegram(self.token, "getChatMember", chat_id=chat_id, user_id=user_id)
                if member.get("status") not in {"creator", "administrator"}:
                    response = "Настраивать бота может только администратор группы."
                elif self.owner_user_id not in {None, user_id}:
                    response = "Этот бот уже настроен другим администратором."
                else:
                    if self.owner_user_id is None:
                        self.store.set_owner(user_id)
                        self.owner_user_id = user_id
                    kinds = ("events", "achievements") if command == "/setboth" else (
                        ("events",) if command == "/setevents" else ("achievements",))
                    for kind in kinds:
                        self.store.set_destination(kind, chat_id, topic_id)
                    self.load_destinations()
                    self.force_check = self.configured
                    response = "Назначение сохранено. Изменить его можно в личном чате с ботом через /setevents, /setachievements или /setboth."
        else:
            response = "Для канала укажите Chat ID в личном чате с ботом."
        args = dict(chat_id=chat_id, text=response)
        if formatted:
            args["parse_mode"] = "HTML"
        if topic_id is not None:
            args["message_thread_id"] = topic_id
        if keyboard:
            args["reply_markup"] = keyboard
        telegram(self.token, "sendMessage", **args)

    def run(self):
        webhook = telegram(self.token, "getWebhookInfo")
        if webhook.get("url"):
            raise RuntimeError("У этого бота уже настроен webhook; используйте отдельного бота или отключите webhook")
        try:
            telegram(self.token, "setMyCommands", commands=PRIVATE_COMMANDS,
                     scope={"type": "all_private_chats"})
            telegram(self.token, "setMyCommands", commands=GROUP_COMMANDS,
                     scope={"type": "all_group_chats"})
        except Exception:
            LOG.exception("Не удалось добавить подсказки команд Telegram")
        if not self.configured:
            LOG.info("Режим настройки: администратор может отправить /setevents и /setachievements в нужных топиках")
        offset = None
        next_check = 0.0
        while True:
            if self.configured and (self.force_check or time.monotonic() >= next_check):
                self.force_check = False
                try:
                    self.check()
                except Exception:
                    LOG.exception("Проверка CTFtime завершилась ошибкой; повторю позже")
                next_check = time.monotonic() + self.check_interval
            try:
                updates = telegram(self.token, "getUpdates", offset=offset, timeout=25,
                                   allowed_updates=["message", "callback_query", "my_chat_member", "channel_post"])
                for update in updates:
                    offset = update["update_id"] + 1
                    try:
                        if "callback_query" in update:
                            self.on_callback(update["callback_query"])
                        elif "message" in update:
                            self.on_message(update["message"])
                        elif "channel_post" in update:
                            self.observe_message(update["channel_post"])
                        elif "my_chat_member" in update:
                            self.on_chat_member(update["my_chat_member"])
                    except Exception:
                        LOG.exception("Не удалось обработать обновление Telegram")
            except Exception:
                LOG.exception("Ошибка Telegram; повтор через 5 секунд")
                time.sleep(5)


if __name__ == "__main__":
    load_dotenv()
    log_path = Path(os.getenv("DB_PATH", "data/bot.sqlite3")).parent / "bot.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
                  logging.StreamHandler()],
    )
    try:
        Bot().run()
    except KeyboardInterrupt:
        LOG.info("Бот остановлен")
