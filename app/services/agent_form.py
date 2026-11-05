"""Форма «Настройки агента»: поля разделов agent.toml с подсказками вместо JSON.

Схема — список полей. Поле: раздел, путь внутри раздела, тип, подпись, подсказка. Типы:
num (число, min/max), bool (да/нет), str, choice, list (строки через запятую), json (список объектов — пример
в подсказке), flag (коллектор включён). Поля, которых нет в схеме, но есть на узле, сохраняются как есть
(скрытое поле extra_<раздел>), чтобы панель не стирала то, что не умеет показать.
Итог проверяет сам агент (check_sections + parse_config): здесь только типы и границы — для понятной ошибки.
"""
import json
import re
from dataclasses import dataclass, field

from app.services.agent_remote import RemoteError, SECTIONS

COLLECTORS = [("cpu", "CPU: загрузка"), ("memory", "Память"), ("disks", "Диски"), ("thermal", "Температура"),
              ("uptime", "Время работы"), ("processes", "Процессы"), ("systemd", "Службы systemd"),
              ("checks", "Проверки TCP/HTTP")]
SCREENS = [("network", "Сеть"), ("health", "Состояние"), ("panels", "Панели"), ("uptime", "Время работы")]


@dataclass
class Field:
    section: str
    path: tuple[str, ...]
    type: str
    label: str
    hint: str
    min: float | None = None
    max: float | None = None
    choices: list[tuple[str, str]] = field(default_factory=list)
    example: str = ""

    @property
    def name(self) -> str:
        return "f." + ".".join((self.section, *self.path))

    @property
    def collector(self) -> str:
        return self.path[0] if self.section == "collectors" else ""


def _collector_fields() -> list[Field]:
    out = []
    for c, label in COLLECTORS:
        out.append(Field("collectors", (c,), "flag", label, f"Собирать «{label.lower()}». Выкл — датчики пропадут."))
        out.append(Field("collectors", (c, "interval"), "num", f"{label}: интервал, с",
                         "Свой интервал опроса для этого сборщика; пусто — общий интервал.", 1, 86400))
    out += [
        Field("collectors", ("disks", "include_mounts"), "list", "Диски: только эти разделы",
              "Точки монтирования через запятую, например: /, /srv/data. Остальные не показываются."),
        Field("collectors", ("disks", "exclude_fs"), "list", "Диски: скрыть файловые системы",
              "Типы ФС через запятую, например: vfat, nfs — в дополнение к встроенным виртуальным."),
        Field("collectors", ("processes", "watch"), "json", "Процессы: за чем следить",
              "Список: имя для панели и подстрока командной строки процесса. Датчики: работает 1/0, CPU %, память.",
              example='[{"name": "nginx", "match": "nginx: master"}]'),
        Field("collectors", ("systemd", "units"), "list", "Службы: какие",
              "Имена служб через запятую, например: nginx.service, docker.service. Датчик — активна 1/0."),
        Field("collectors", ("systemd", "timeout"), "num", "Службы: таймаут, с",
              "Сколько ждать ответа systemctl.", 1, 120),
        Field("collectors", ("checks", "tcp"), "json", "Проверки TCP",
              "Список: имя, адрес, порт (timeout — по желанию). Датчики: доступен 1/0 и задержка, мс.",
              example='[{"name": "router", "host": "192.0.2.1", "port": 80}]'),
        Field("collectors", ("checks", "http"), "json", "Проверки HTTP",
              "Список: имя, адрес, ожидаемый код ответа (expect, по умолчанию 200).",
              example='[{"name": "site", "url": "http://127.0.0.1:8000/health", "expect": 200}]'),
    ]
    return out


FIELDS: list[Field] = [
    Field("interval", (), "num", "Интервал опроса, с",
          "Как часто агент собирает данные — общий для всех сборщиков. 1–3600.", 1, 3600),
    *_collector_fields(),
    Field("display", ("enabled",), "bool", "Дисплей включён", "OLED 128×64 по I2C на самом узле (Raspberry Pi)."),
    Field("display", ("driver",), "choice", "Драйвер", "SSD1315 совместим с SSD1306.",
          choices=[("ssd1306", "SSD1306 / SSD1315")]),
    Field("display", ("i2c_port",), "num", "Шина I2C", "Номер шины: на Raspberry Pi обычно 1.", 0, 10),
    Field("display", ("address",), "str", "Адрес I2C", "Шестнадцатеричный, обычно 0x3C. Узнать: sudo i2cdetect -y 1."),
    Field("display", ("rotate",), "choice", "Поворот", "Поворот изображения.",
          choices=[("0", "0°"), ("1", "90°"), ("2", "180°"), ("3", "270°")]),
    Field("display", ("screens",), "list", "Экраны", "По порядку через запятую: network, health, panels, uptime."),
    Field("display", ("switch_every",), "num", "Смена экрана, с", "Сколько секунд показывать каждый экран.", 1, 3600),
    Field("display", ("shift_every",), "num", "Сдвиг против выгорания, с",
          "Раз в столько секунд картинка сдвигается на 1–2 точки.", 0, 86400),
    Field("display", ("contrast",), "num", "Яркость", "0–255.", 0, 255),
    Field("display", ("night_contrast",), "num", "Яркость ночью", "0–255, действует с «ночь с» до «ночь до».", 0, 255),
    Field("display", ("night_start",), "str", "Ночь с", "Время ЧЧ:ММ, например 23:00."),
    Field("display", ("night_end",), "str", "Ночь до", "Время ЧЧ:ММ, например 07:00."),
    Field("display", ("font",), "str", "Шрифт", "Путь к .ttf на узле; пусто — DejaVu."),
    Field("wol", ("enabled",), "bool", "Wake-on-LAN",
          "Узел может будить другие машины своей сети по просьбе панели (только MAC, ничего не исполняет)."),
    Field("wol", ("port",), "choice", "Порт WoL", "UDP-порт magic packet.", choices=[("9", "9"), ("7", "7")]),
    Field("terminal", ("enabled",), "bool", "Консоль через агента",
          "Панель подключается к sshd узла через агента — порт 22 не нужно открывать для панели. Вход проверяет sshd. "
          "Адрес sshd (target) задаётся только в agent.toml, по умолчанию 127.0.0.1:22."),
    Field("terminal", ("max_sessions",), "num", "Сеансов одновременно", "Консоль и передача файлов вместе. 1–8.", 1, 8),
    Field("terminal", ("rate_kbps",), "num", "Ограничение скорости, кбит/с",
          "Общее на все сеансы в обе стороны: чтобы передача файла не забила канал узла.", 64, 1_000_000),
    Field("terminal", ("idle_timeout",), "num", "Закрывать без активности, с",
          "Сеанс без трафика закрывается через это время.", 10, 86400),
    Field("update", ("enabled",), "bool", "Обновление из панели",
          "Панель может ставить на узел новые версии агента (с самотестом и откатом)."),
    Field("update", ("confirm_seconds",), "num", "Ждать подтверждения, с",
          "Если новая версия не подключится к панели за это время — вернётся прежняя. 30–3600.", 30, 3600),
]
BY_NAME = {f.name: f for f in FIELDS}
SECTION_TITLES = {"interval": "Общее", "collectors": "Сбор данных", "display": "Дисплей", "wol": "Wake-on-LAN",
                  "terminal": "Консоль через агента", "update": "Обновление"}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _get(data, path):
    for p in path:
        if not isinstance(data, dict) or p not in data:
            return None
        data = data[p]
    return data


def _show(f: Field, value) -> str:
    """Значение для поля ввода."""
    if value is None:
        return ""
    if f.type == "list":
        return ", ".join(str(x) for x in value) if isinstance(value, list) else str(value)
    if f.type == "json":
        return json.dumps(value, ensure_ascii=False)
    if f.type == "bool":
        return "1" if value else "0"
    if f.path == ("address",):
        return hex(value) if isinstance(value, int) else str(value)
    if isinstance(value, float) and value == int(value):
        value = int(value)
    return str(value)


def view(managed: list[str], sections: dict) -> list[dict]:
    """Разделы для шаблона: поля с текущими значениями (present — задано на узле) и прочие ключи (extra)."""
    out = []
    for sec in [s for s in SECTIONS if s in managed]:
        cur = sections.get(sec)
        rows = []
        for f in (f for f in FIELDS if f.section == sec):
            if f.type == "flag":
                value = _get(cur, f.path)
                present = isinstance(value, dict) and value.get("enabled", True) is not False
                rows.append({"f": f, "value": "1", "present": present})
                continue
            value = cur if sec == "interval" else _get(cur, f.path)
            rows.append({"f": f, "value": _show(f, value), "present": value is not None})
        out.append({"name": sec, "title": SECTION_TITLES.get(sec, sec), "rows": rows,
                    "extra": json.dumps(_extra(sec, cur), ensure_ascii=False) if isinstance(cur, dict) else "{}"})
    return out


def _extra(sec: str, cur: dict) -> dict:
    """Ключи раздела, которых нет в схеме: панель их не показывает, но и не стирает."""
    known = {f.path for f in FIELDS if f.section == sec}
    if sec != "collectors":
        return {k: v for k, v in cur.items() if (k,) not in known}
    extra = {}
    for c, sub in cur.items():
        if (c,) not in known:
            extra[c] = sub
        elif isinstance(sub, dict):
            rest = {k: v for k, v in sub.items() if (c, k) not in known}
            if rest:
                extra[c] = rest
    return extra


def _value(f: Field, raw: str):
    raw = raw.strip()
    if f.type == "num":
        try:
            v = float(raw.replace(",", "."))
        except ValueError:
            raise RemoteError(f"«{f.label}»: нужно число") from None
        if (f.min is not None and v < f.min) or (f.max is not None and v > f.max):
            raise RemoteError(f"«{f.label}»: допустимо {f.min:g}–{f.max:g}")
        return int(v) if v == int(v) else v
    if f.type == "bool":
        return raw == "1"
    if f.type == "choice":
        if raw not in {c for c, _ in f.choices}:
            raise RemoteError(f"«{f.label}»: недопустимое значение")
        return int(raw) if raw.isdigit() else raw
    if f.type == "list":
        return [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]
    if f.type == "json":
        try:
            v = json.loads(raw or "[]")
        except ValueError:
            raise RemoteError(f"«{f.label}»: не получилось прочитать — образец: {f.example}") from None
        if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
            raise RemoteError(f"«{f.label}»: нужен список объектов, образец: {f.example}")
        return v
    if f.path == ("address",):
        try:
            return int(raw, 16) if raw.lower().startswith("0x") else int(raw)
        except ValueError:
            raise RemoteError("«Адрес I2C»: например 0x3C") from None
    if f.path in (("night_start",), ("night_end",)) and raw and not TIME_RE.match(raw):
        raise RemoteError(f"«{f.label}»: время ЧЧ:ММ")
    return raw


def parse(form, managed: list[str] | None = None) -> dict:
    """Поля формы f.<раздел>.<путь> (+ extra_<раздел>) → разделы для config_set. Раздел без полей не шлётся."""
    allowed = set(managed) if managed else set(SECTIONS)
    sections: dict = {}
    for name, raw in form.multi_items():
        if name.startswith("has_"):  # раздел есть в форме — шлём его, даже если все поля убраны (тогда — умолчания)
            sec = name[4:]
            if sec not in allowed:
                raise RemoteError(f"Раздел «{sec}» нельзя менять из панели")
            if sec != "interval":
                sections.setdefault(sec, {})
            continue
        if name.startswith("extra_"):
            sec = name[6:]
            if sec not in allowed:
                raise RemoteError(f"Раздел «{sec}» нельзя менять из панели")
            try:
                extra = json.loads(str(raw) or "{}")
            except ValueError:
                raise RemoteError("Повреждено скрытое поле формы — откройте настройки заново") from None
            if isinstance(extra, dict) and extra:
                base = sections.setdefault(sec, {})
                for k, v in extra.items():
                    if isinstance(v, dict) and isinstance(base.get(k), dict):
                        base[k] = {**v, **base[k]}
                    else:
                        base.setdefault(k, v)
            continue
        f = BY_NAME.get(name)
        if f is None:
            continue
        if f.section not in allowed:
            raise RemoteError(f"Раздел «{f.section}» нельзя менять из панели")
        if f.section == "interval":
            sections["interval"] = _value(f, str(raw))
            continue
        if f.type == "flag":
            sections.setdefault("collectors", {}).setdefault(f.path[0], {})
            continue
        if not str(raw).strip() and f.type != "bool":
            continue  # пустое поле — не задано (действует значение по умолчанию на узле)
        target = sections.setdefault(f.section, {})
        for p in f.path[:-1]:
            target = target.setdefault(p, {})
        target[f.path[-1]] = _value(f, str(raw))
    if "collectors" in sections:
        if "command" in sections["collectors"]:
            raise RemoteError("collectors.command меняется только на узле")
        # Поле сборщика без самого сборщика не включает его: включение — только явным «Сбор …»
        flags = {k for k, _ in form.multi_items() if k in BY_NAME and BY_NAME[k].type == "flag"}
        sections["collectors"] = {c: v for c, v in sections["collectors"].items()
                                  if f"f.collectors.{c}" in flags or (c,) not in {f.path for f in FIELDS
                                                                                 if f.type == "flag"}}
    if "target" in sections.get("terminal", {}):
        raise RemoteError("terminal.target меняется только на узле")
    if not sections:
        raise RemoteError("Нечего сохранять")
    return sections
