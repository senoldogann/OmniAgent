"""
Zamanlanmış ve yinelenen görev planları.

Zaman hesabı saf fonksiyonlardır ve yerel duvar saatiyle yapılır: "her gün 09:00" yaz saati
geçişinden sonra da yerel 09:00'dır (sabit UTC ofsetiyle gün eklemek bir saat kaydırırdı).
Planlar atomik, yalnız kullanıcıya açık JSON dosyasında tutulur; onları zamanı gelince
çalıştıran süreç sürekli açık Telegram köprüsüdür.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
import logging
from pathlib import Path
import re
from typing import List, Optional, Tuple, TypedDict

from omniagent.integrations.runtime import read_json, save_json

WEEKDAY_NAMES: Tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_LABELS: Tuple[str, ...] = ("Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz")
REPEAT_KINDS: Tuple[str, ...] = ("once", "daily", "weekly", "interval")
MAX_SCHEDULES: int = 20
# Dakikalık döngü hem modeli hem ekranı meşgul eder; en sık tekrar 15 dakikadır
MIN_INTERVAL_MINUTES: int = 15
MAX_INTERVAL_MINUTES: int = 7 * 24 * 60
GOAL_LIMIT: int = 1000
# Bilgisayar kapalı/uykudayken kaçan çalışma bu süre içindeyse bir kez yetişilir; eskisi atlanır
MISSED_RUN_GRACE: timedelta = timedelta(hours=6)
_CLOCK: re.Pattern[str] = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")


class ScheduleError(ValueError):
    """Geçersiz plan tanımı; mesajı modele ve kullanıcıya gider."""


class ScheduleSpec(TypedDict):
    """Zamanlama kuralı: yalnız repeat türünün alanları doludur."""
    repeat: str
    time: Optional[str]
    weekdays: List[int]
    at: Optional[str]
    every_minutes: Optional[int]


class ScheduleRecord(TypedDict):
    """Saklanan plan; zamanlar yerel saat dilimli ISO 8601 metnidir."""
    id: str
    goal: str
    spec: ScheduleSpec
    created_at: str
    next_run: str
    last_run: Optional[str]
    runs: int


def _wall(moment: datetime) -> datetime:
    """Saat dilimli anı sistemin yerel duvar saatine (saat dilimsiz) çevirir."""
    return moment.astimezone().replace(tzinfo=None)


def _aware(wall: datetime) -> datetime:
    """Yerel duvar saatini sistemin saat dilimi kurallarıyla (yaz saati dahil) saat dilimli ana çevirir."""
    return wall.astimezone()


def _parse_clock(text: Optional[str]) -> time:
    match = _CLOCK.match(text or "")
    if match is None or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ScheduleError(f"time 'SS:DD' biçiminde geçerli bir saat olmalı; alınan: {text!r}")
    return time(int(match.group(1)), int(match.group(2)))


def _parse_weekdays(names: Optional[List[str]]) -> List[int]:
    days: List[int] = []
    for name in names or []:
        key = str(name).strip().casefold()[:3]
        if key not in WEEKDAY_NAMES:
            raise ScheduleError(f"Geçersiz gün: {name!r} (mon, tue, wed, thu, fri, sat, sun).")
        days.append(WEEKDAY_NAMES.index(key))
    return sorted(set(days))


def _parse_at(text: Optional[str]) -> datetime:
    try:
        moment = datetime.fromisoformat(str(text or "").strip())
    except ValueError as error:
        raise ScheduleError(f"at 'YYYY-AA-GGTSS:DD' biçiminde yerel tarih-saat olmalı; alınan: {text!r}") from error
    return _wall(moment) if moment.tzinfo is not None else moment


def build_spec(
    repeat: Optional[str], time_text: Optional[str], weekdays: Optional[List[str]],
    at: Optional[str], every_minutes: Optional[int],
) -> ScheduleSpec:
    """Model argümanlarını doğrulanmış zamanlama kuralına çevirir; türün gerektirmediği alanlar yok sayılır."""
    kind = str(repeat or "").strip().casefold()
    if kind not in REPEAT_KINDS:
        raise ScheduleError("repeat once, daily, weekly veya interval olmalı.")
    if kind in ("daily", "weekly"):
        clock = _parse_clock(time_text)
        days = _parse_weekdays(weekdays) if kind == "weekly" else []
        if kind == "weekly" and not days:
            raise ScheduleError("weekly için en az bir gün (weekdays) gerekli.")
        return {"repeat": kind, "time": clock.strftime("%H:%M"), "weekdays": days, "at": None, "every_minutes": None}
    if kind == "interval":
        if (isinstance(every_minutes, bool) or not isinstance(every_minutes, int)
                or not MIN_INTERVAL_MINUTES <= every_minutes <= MAX_INTERVAL_MINUTES):
            raise ScheduleError(
                f"every_minutes {MIN_INTERVAL_MINUTES}-{MAX_INTERVAL_MINUTES} arasında tamsayı olmalı; alınan: {every_minutes!r}",
            )
        return {"repeat": kind, "time": None, "weekdays": [], "at": None, "every_minutes": every_minutes}
    return {
        "repeat": kind, "time": None, "weekdays": [], "at": _parse_at(at).strftime("%Y-%m-%dT%H:%M"),
        "every_minutes": None,
    }


def next_occurrence(spec: ScheduleSpec, after: datetime) -> Optional[datetime]:
    """Kuralın `after` anından KESİNLİKLE sonraki çalışma anı; tek seferlik geçmişse None."""
    kind = spec["repeat"]
    if kind == "once":
        moment = _aware(datetime.fromisoformat(str(spec["at"])))
        return moment if moment > after else None
    if kind == "interval":
        return after + timedelta(minutes=int(spec["every_minutes"] or 0))
    clock = _parse_clock(spec["time"])
    start_day = _wall(after).date()
    for offset in range(8):
        day = start_day + timedelta(days=offset)
        if kind == "weekly" and day.weekday() not in spec["weekdays"]:
            continue
        candidate = _aware(datetime.combine(day, clock))
        if candidate > after:
            return candidate
    return None


def describe_spec(spec: ScheduleSpec) -> str:
    """Kuralın kısa Türkçe anlatımı. Saf."""
    kind = spec["repeat"]
    if kind == "daily":
        return f"her gün {spec['time']}"
    if kind == "weekly":
        return f"her {', '.join(_WEEKDAY_LABELS[day] for day in spec['weekdays'])} {spec['time']}"
    if kind == "interval":
        minutes = int(spec["every_minutes"] or 0)
        return f"her {minutes // 60} saatte bir" if minutes % 60 == 0 else f"her {minutes} dakikada bir"
    return f"bir kez {str(spec['at']).replace('T', ' ')}"


def format_moment(moment: datetime) -> str:
    """Yerel saatte kısa gösterim: '26.09 09:00'."""
    return _wall(moment).strftime("%d.%m %H:%M")


def describe_record(record: ScheduleRecord) -> str:
    """Plan satırı: kimlik, kural, sonraki çalışma ve görev. Saf."""
    goal = record["goal"] if len(record["goal"]) <= 90 else record["goal"][:89] + "…"
    upcoming = format_moment(datetime.fromisoformat(record["next_run"]))
    return f"[{record['id']}] {describe_spec(record['spec'])} · sonraki {upcoming} · {goal}"


def add_schedule(
    records: List[ScheduleRecord], goal: str, spec: ScheduleSpec, now: datetime, new_id: str,
) -> Tuple[List[ScheduleRecord], ScheduleRecord]:
    """Yeni planı ekler; geçmiş tek seferlik zaman, boş/uzun görev ve üst sınır reddedilir. Saf."""
    text = " ".join(str(goal or "").split())
    if not text:
        raise ScheduleError("Planlanacak görev (goal) boş olamaz.")
    if len(text) > GOAL_LIMIT:
        raise ScheduleError(f"Görev metni en çok {GOAL_LIMIT} karakter olabilir.")
    if len(records) >= MAX_SCHEDULES:
        raise ScheduleError(f"En çok {MAX_SCHEDULES} plan tutulur; önce eskilerden birini sil.")
    first = next_occurrence(spec, now)
    if first is None:
        raise ScheduleError(f"Tek seferlik zaman geçmişte: {spec['at']}.")
    record: ScheduleRecord = {
        "id": new_id, "goal": text, "spec": spec, "created_at": now.isoformat(timespec="seconds"),
        "next_run": first.isoformat(timespec="seconds"), "last_run": None, "runs": 0,
    }
    return records + [record], record


def remove_schedule(records: List[ScheduleRecord], schedule_id: str) -> Tuple[List[ScheduleRecord], Optional[ScheduleRecord]]:
    """Kimliği eşleşen planı çıkarır. Saf."""
    wanted = str(schedule_id or "").strip().strip("[]").casefold()
    removed = next((record for record in records if record["id"].casefold() == wanted), None)
    return [record for record in records if record is not removed], removed


def due_schedules(records: List[ScheduleRecord], now: datetime) -> List[ScheduleRecord]:
    """Zamanı gelmiş planlar, en eskisi önce. Saf."""
    ready = [record for record in records if datetime.fromisoformat(record["next_run"]) <= now]
    return sorted(ready, key=lambda record: datetime.fromisoformat(record["next_run"]))


def advance_schedule(records: List[ScheduleRecord], schedule_id: str, now: datetime) -> List[ScheduleRecord]:
    """Başlatılan planın sonraki zamanını ilerletir; biten tek seferlik plan silinir. Saf."""
    updated: List[ScheduleRecord] = []
    for record in records:
        if record["id"] != schedule_id:
            updated.append(record)
            continue
        upcoming = next_occurrence(record["spec"], now)
        if upcoming is not None:
            updated.append({
                **record, "next_run": upcoming.isoformat(timespec="seconds"),
                "last_run": now.isoformat(timespec="seconds"), "runs": record["runs"] + 1,
            })
    return updated


def skip_missed(records: List[ScheduleRecord], now: datetime) -> Tuple[List[ScheduleRecord], List[ScheduleRecord]]:
    """
    MISSED_RUN_GRACE'ten eski kaçmış çalışmaları atlar: yinelenen plan sonraki zamana geçer,
    tek seferlik plan silinir. İkinci değer atlanan planlardır (kullanıcıya bildirilir). Saf.
    """
    kept: List[ScheduleRecord] = []
    skipped: List[ScheduleRecord] = []
    for record in records:
        if datetime.fromisoformat(record["next_run"]) >= now - MISSED_RUN_GRACE:
            kept.append(record)
            continue
        skipped.append(record)
        upcoming = next_occurrence(record["spec"], now)
        if upcoming is not None:
            kept.append({**record, "next_run": upcoming.isoformat(timespec="seconds")})
    return kept, skipped


def _valid_record(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    spec = value.get("spec")
    try:
        datetime.fromisoformat(str(value.get("next_run")))
    except ValueError:
        return False
    return (
        isinstance(value.get("id"), str) and isinstance(value.get("goal"), str)
        and isinstance(spec, dict) and spec.get("repeat") in REPEAT_KINDS
        and isinstance(value.get("runs"), int)
    )


def load_schedules(path: Path) -> List[ScheduleRecord]:
    """Planları okur; bozuk kayıt sessizce çalıştırılmaz, günlüğe yazılıp atlanır."""
    value = read_json(path, [])
    if not isinstance(value, list):
        raise ValueError(f"Plan dosyası liste değil: {path}")
    records = [record for record in value if _valid_record(record)]
    if len(records) != len(value):
        logging.warning("Geçersiz plan kayıtları atlandı", extra={"skipped": len(value) - len(records)})
    return records


def save_schedules(path: Path, records: List[ScheduleRecord]) -> None:
    """Planları atomik ve yalnız kullanıcıya açık (0600) yazar."""
    save_json(path, records)
