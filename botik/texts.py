import random
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from botik.storage import Assignment

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

MOD_EMOJI = {
    "resource": "📄", "url": "🔗", "page": "📃", "folder": "📁", "book": "📘",
    "quiz": "🧠", "forum": "💬", "assign": "📎", "lesson": "🎓", "workshop": "🛠",
}

START = (
    "🤖 Я — Дедлайнатор. Модель Т-800.\n"
    "Меня отправили из будущего, в котором ты не сдал лабу. Моя миссия — не допустить этого.\n\n"
    "Протокол наблюдения за Moodle мехмата:\n"
    "• оценку поставили / изменили / стёрли — доложу\n"
    "• открыли лабу, презентацию или материал — доложу\n"
    "• выдали задание, куда надо что-то прикрепить — доложу\n"
    "• Судный день (дедлайн) сегодня или уже прошёл — найду тебя\n"
    "• по понедельникам — сводка целей на неделю\n\n"
    "Для работы мне нужен вход в edu.mmcs.sfedu.ru: /login\n"
    "Логин и пароль хранятся только в зашифрованном виде, сообщения с ними я сразу удаляю.\n\n"
    "/week — цели на неделю, /check — сканировать сейчас, /logout — самоуничтожение."
)

ASK_USERNAME = (
    "🤖 Мне нужна твоя одежда, сапоги и… логин от Moodle.\n\n"
    "Пришли логин, с которым входишь на edu.mmcs.sfedu.ru.\n"
    "(отмена — /cancel)"
)
ASK_PASSWORD = "🤖 Принято. Теперь пароль.\n(отмена — /cancel)"
PASSWORD_NOT_DELETED = "⚠️ Не смог удалить сообщение с паролем — удали его сам."
CANCELLED = "🤖 Миссия отменена. I'll be back: /login"
LOGIN_CHECKING = "🤖 Проверяю доступ…"
LOGIN_BAD = "🤖 Доступ запрещён: неверный логин или пароль. Попробуй снова: /login"
LOGIN_REFUSED = "🤖 Moodle не пустил: <code>{err}</code>. Попробуй войти на сайте вручную, потом снова /login"
LOGIN_FAIL = "🤖 Сбой системы: {err}. Moodle сопротивляется. Попробуй позже: /login"
SCANNING = "🤖 Цель идентифицирована: {name}. Сканирую курсы… ⏳"
LOGGED_OUT = "🤖 Hasta la vista, baby. Логин, пароль и все данные о тебе уничтожены.\nВернуться: /login"
NEED_LOGIN = "🤖 Цель не опознана. Сначала войди: /login"
LOGIN_EXPIRED = "🤖 Связь с Moodle потеряна: логин или пароль больше не подходят. Войди заново: /login"
UNKNOWN = "🤖 Команда не распознана. Что я умею: /help"
UNKNOWN_DELETED = "🤖 Не понял и на всякий случай удалил сообщение. Войти — /login, что я умею — /help"
NOTHING_NEW = [
    "🤖 Сканирование завершено. Угроз не обнаружено.",
    "🤖 Новых целей нет. Режим ожидания.",
    "🤖 Всё тихо. Слишком тихо. Скайнет что-то замышляет…",
]
CHECKING = ["🤖 Запускаю сканирование…", "🤖 Анализирую Moodle. Не двигайся."]


def check_cooldown(last: int, next_at: int, tz: ZoneInfo) -> str:
    return (
        f"🤖 Я уже сканировал Moodle в {fmt_time(last, tz)}. Перегреваться нельзя.\n"
        f"Ручная проверка снова доступна в {fmt_time(next_at, tz)}, "
        "а о новом я и сам доложу, когда найду.\n"
        "Цели на неделю: /week"
    )


def as_of(ts: int, tz: ZoneInfo) -> str:
    return f"\n\n<i>🤖 Данные на {fmt_time(ts, tz)}</i>"


def ready(courses: int) -> str:
    return (
        f"🤖 Сканирование завершено. Обнаружено {courses} "
        f"{plural(courses, 'курс', 'курса', 'курсов')}.\n"
        "Всё, что уже есть, — старые цели. Докладываю только о новом. "
        "Сканирую несколько раз в день, про сегодняшние дедлайны напомню утром. I'll be back.\n"
        "Как удалить мои данные — /logout\n"
        "Цели на неделю: /week"
    )


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def fmt_dt(ts: int, tz: ZoneInfo) -> str:
    d = datetime.fromtimestamp(ts, tz)
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m %H:%M}"


def fmt_time(ts: int, tz: ZoneInfo) -> str:
    d = datetime.fromtimestamp(ts, tz)
    return f"{d:%H:%M}" if d.date() == datetime.now(tz).date() else fmt_dt(ts, tz)


def fmt_left(sec: int) -> str:
    if sec < 3600:
        return "меньше часа"
    h = sec // 3600
    if h < 24:
        return f"{h} {plural(h, 'час', 'часа', 'часов')}"
    d, h = divmod(h, 24)
    return f"{d} {plural(d, 'день', 'дня', 'дней')}" + (f" {h} ч" if h else "")


def link(name: str, url: str) -> str:
    return f'<a href="{escape(url, quote=True)}">{escape(name)}</a>'


def _assign_line(a: Assignment, now: int, tz: ZoneInfo) -> str:
    line = f"🎯 {link(a.name, a.url)} — <i>{escape(a.course)}</i>"
    if a.due:
        if a.due > now:
            line += f"\n   до {fmt_dt(a.due, tz)} (осталось {fmt_left(a.due - now)})"
        else:
            line += f"\n   дедлайн был {fmt_dt(a.due, tz)}"
    else:
        line += "\n   без дедлайна"
    return line


def new_course(name: str) -> str:
    return random.choice([
        f"🤖 Обнаружена новая территория: <b>{escape(name)}</b>. Взял под наблюдение.",
        f"🤖 Тебя добавили на курс <b>{escape(name)}</b>. Начинаю патрулирование.",
    ])


def new_materials(items: list[tuple[str, str, str, str]]) -> str:
    head = random.choice([
        "🤖 Обнаружены новые материалы. Рекомендую изучить:",
        "🤖 Разведданные получены. Новые материалы в курсах:",
        "🤖 Пополнение арсенала знаний:",
    ])
    lines = [head]
    current = None
    for course, name, modname, url in items:
        if course != current:
            lines.append(f"\n<b>{escape(course)}</b>")
            current = course
        lines.append(f"{MOD_EMOJI.get(modname, '📦')} {link(name, url)}")
    return "\n".join(lines)


def new_assignments(items: list[Assignment], now: int, tz: ZoneInfo) -> str:
    head = random.choice([
        "🤖 Новая цель обнаружена. Задание:",
        "🤖 Миссия получена. Провал не вариант:",
        "🤖 Внимание. Препод выдал задание. Сопротивление бесполезно:",
    ])
    return "\n".join([head, *(_assign_line(a, now, tz) for a in items)])


def due_today(items: list[Assignment], now: int, tz: ZoneInfo) -> str:
    head = random.choice([
        "🔴 Судный день наступает СЕГОДНЯ. Поторапливайся:",
        "🔴 Дедлайн сегодня. Нет судьбы, кроме той, что мы творим сами. Сдавай:",
        "🔴 Иди со мной, если хочешь сдать. Дедлайн сегодня:",
    ])
    return "\n".join([head, *(_assign_line(a, now, tz) for a in items)])


def overdue(items: list[Assignment], now: int, tz: ZoneInfo) -> str:
    head = random.choice([
        "💀 Миссия провалена. Дедлайн прошёл, задание не сдано:",
        "💀 Судный день наступил. А лаба — нет:",
        "💀 Я не смог тебя спасти. Просрочка:",
    ])
    lines = [head]
    for a in items:
        line = _assign_line(a, now, tz)
        if a.cutoff and a.cutoff > now:
            line += f"\n   ⚡ но временная петля открыта до {fmt_dt(a.cutoff, tz)} — ещё можно сдать!"
        lines.append(line)
    return "\n".join(lines)


def grade(kind: str, course: str, item: dict, old: str) -> str:
    name = f"<b>{escape(item['name'])}</b> — <i>{escape(course)}</i>"
    if kind == "deleted":
        head = random.choice([
            "🤖 Оценка терминирована. Была — и нет:",
            "🤖 Аномалия: кто-то стёр оценку. Скайнет?",
        ])
        return f"{head}\n{name}\nБыло: {escape(old)}"
    if kind == "changed":
        head = random.choice([
            "🤖 Оценка изменена. Временная линия переписана:",
            "🤖 Обнаружено вмешательство в прошлое. Оценка изменилась:",
        ])
        body = f"{name}\n{escape(old)} → <b>{escape(item['v'])}</b>"
    else:
        head = random.choice([
            "🤖 Новая оценка зафиксирована:",
            "🤖 Препод нанёс удар. Оценка:",
            "🤖 Входящие разведданные. Оценка:",
        ])
        body = f"{name}\n<b>{escape(item['v'])}</b>"
    reaction = _reaction(item.get("raw"), item.get("max"))
    return f"{head}\n{body}" + (f"\n{reaction}" if reaction else "")


def _reaction(raw, maximum) -> str:
    try:
        ratio = float(raw) / float(maximum)
    except (TypeError, ValueError, ZeroDivisionError):
        return ""
    if ratio >= 0.85:
        return random.choice([
            "Отличная работа. Скайнет нервничает 😎",
            "Эффективность максимальная. Ты почти машина 🦾",
            "👍 *показывает большой палец, погружаясь в расплавленную сталь*",
        ])
    if ratio >= 0.6:
        return random.choice([
            "Приемлемо. Системы в норме 👍",
            "Результат удовлетворительный. Продолжай миссию.",
        ])
    return random.choice([
        "Повреждения незначительные. I'll be back — и ты тоже 💪",
        "Это не конец. Нет судьбы, кроме той, что мы творим сами.",
        "Критическое попадание. Перезагрузка рекомендована 🔧",
    ])


def digest(week: list[Assignment], tails: list[Assignment], now: int, tz: ZoneInfo, weekly: bool) -> str:
    if weekly:
        head = random.choice([
            "🤖 Доброе утро. Понедельник. Цели на неделю:",
            "🤖 Новая неделя — новые угрозы. Список целей:",
        ])
    else:
        head = "🤖 Цели на ближайшие 7 дней:"
    lines = [head]
    if week:
        lines += [_assign_line(a, now, tz) for a in week]
    else:
        lines.append(random.choice([
            "Целей не обнаружено. Можно отдохнуть, человек 🏖",
            "Угроз нет. Подозрительно. Но отдыхай 🧘",
        ]))
    if tails:
        lines.append("\n🧟 Недобитые цели (ещё можно сдать):")
        lines += [_assign_line(a, now, tz) for a in tails]
    return "\n".join(lines)
