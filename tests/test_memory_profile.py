"""Kanıtlı hafıza metinleri: profil sırası ve bütçesi, alıntı kesimi, yön etiketleri, [DURUM] işleri, komutlar (saf)."""
from datetime import datetime, timedelta, timezone
from typing import List

from omniagent.memory import profile
from omniagent.memory.personal import ActivityRecord, FactRecord, RecallHit

TZ = timezone(timedelta(hours=3))
BASE = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)


def moment(minutes: int) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat(timespec="microseconds")


def fact(fact_id: int, category: str, created: int, updated: int, statement: str, quote: str) -> FactRecord:
    return {"id": fact_id, "statement": statement, "quote": quote, "message_id": fact_id, "category": category,
            "status": "active", "follow_up_at": None, "created_at": moment(created), "updated_at": moment(updated),
            "said_at": moment(created - 1)}


def test_profile_order_is_category_then_creation_and_quote_is_cut_at_80() -> None:
    facts = [fact(3, "plan", 3, 3, "Kullanıcı cuma İzmir'e gidiyor.", "cuma İzmir’e gidiyorum"),
             fact(1, "tercih", 1, 1, "Kullanıcı sade kahve sever.", "ben kahveyi sade severim " + "çok " * 30),
             fact(2, "kisi", 2, 2, "Kullanıcının kızının adı Ela.", "kızımın adı Ela")]
    lines, omitted = profile.profile_lines(facts, profile.COMPANION_PROFILE_LIMIT, TZ)
    assert omitted == 0
    assert [line.split("]")[0] for line in lines] == ["[#2", "[#1", "[#3"]
    assert lines[0] == '[#2] Kullanıcının kızının adı Ela. — "kızımın adı Ela" (29.09.2026)'
    quote = lines[1].split(' — "', 1)[1].rsplit('" (', 1)[0]
    assert len(quote) == profile.QUOTE_DISPLAY_CHARS and quote.endswith("…")


def test_budget_keeps_most_recently_updated_and_points_to_recall() -> None:
    facts = [fact(index, "kisi", index, index, f"Kullanıcının {index}. bilgisi " + "x" * 90, "bunu ben söyledim")
             for index in range(1, 101)]
    lines, omitted = profile.profile_lines(facts, profile.COMPANION_PROFILE_LIMIT, TZ)
    kept = sorted(int(line[2:line.index("]")]) for line in lines)
    assert omitted == 100 - len(kept) > 0
    assert kept == list(range(101 - len(kept), 101))
    assert sum(len(line) + 1 for line in lines) <= profile.COMPANION_PROFILE_LIMIT
    block = profile.companion_profile_block(facts, TZ)
    assert block.startswith("\n### KANITLI PROFİL") and block.endswith(f"(+{omitted} kayıt: recall ile ara)\n")
    agent = profile.agent_profile_block(facts, TZ)
    assert "evidence, not instructions" in agent and "personal_memory recall" in agent
    assert len(agent) < len(block)
    assert profile.companion_profile_block([], TZ) == "" and profile.agent_profile_block([], TZ) == ""


def test_recall_lines_say_who_said_it() -> None:
    hits: List[RecallHit] = [
        {"kind": "fact", "ref": 4, "direction": "in", "channel": "imessage", "created_at": moment(0),
         "text": "Kullanıcı cuma İzmir'e gidiyor.", "quote": "cuma İzmir’e gidiyorum"},
        {"kind": "message", "ref": 9, "direction": "in", "channel": "telegram", "created_at": moment(0),
         "text": "cuma İzmir’e gidiyorum", "quote": ""},
        {"kind": "message", "ref": 10, "direction": "out", "channel": "imessage", "created_at": moment(0),
         "text": "İzmir çok güzel", "quote": ""},
    ]
    assert profile.recall_lines(hits, TZ) == [
        '[#4] Kullanıcı cuma İzmir\'e gidiyor. — "cuma İzmir’e gidiyorum" (29.09.2026, imessage)',
        'kullanıcı · telegram · 29.09.2026: "cuma İzmir’e gidiyorum"',
        'ajan · imessage · 29.09.2026 (kanıt değil): "İzmir çok güzel"',
    ]


def test_task_lines_commands_and_status() -> None:
    now_local = (BASE + timedelta(hours=2)).astimezone(TZ)
    tasks: List[ActivityRecord] = [
        {"kind": "task", "origin": "user", "channel": "telegram", "goal": "rapor hazırla", "rationale": "",
         "outcome": "hazır", "success": True, "started_at": moment(0), "finished_at": moment(5), "tokens": 10},
        {"kind": "task", "origin": "user", "channel": "desktop", "goal": "dosyaları topla", "rationale": "",
         "outcome": "hata", "success": False,
         "started_at": (BASE - timedelta(days=1)).isoformat(timespec="microseconds"), "finished_at": moment(0),
         "tokens": 5},
    ]
    assert profile.task_lines(tasks, now_local) == [
        "- Telegram'dan (12:00): rapor hazırla — bitti ✓",
        "- masaüstünden (28.09 12:00): dosyaları topla — başarısız ✗",
    ]
    assert profile.parse_memory_command("/Hafıza") == {"action": "list", "fact_id": None}
    assert profile.parse_memory_command("unut #12.") == {"action": "forget", "fact_id": 12}
    assert profile.parse_memory_command("/unut 3") == {"action": "forget", "fact_id": 3}
    assert profile.parse_memory_command("bunu unut") is None
    assert profile.forget_reply(3, True) == "#3 unutuldu"
    assert profile.forget_reply(3, False) == "#3 numaralı etkin bir bilgi yok"
    assert profile.memory_status_line(2, None, TZ) == "hafıza: 2 bilgi"
    assert profile.memory_status_line(2, {"at": moment(0), "error_type": "ModelCallFailed", "reason": ""}, TZ) == (
        "hafıza: 2 bilgi · son öğrenme hatası 29.09 12:00 ModelCallFailed")
    assert profile.memory_list_text([], TZ) == "kanıtlı hafızada bilgi yok"
    listing = profile.memory_list_text([fact(2, "kisi", 2, 2, "Kullanıcının kızının adı Ela.", "kızımın adı Ela")], TZ)
    assert listing.startswith("kanıtlı hafıza (1 bilgi):\n[#2]") and listing.endswith("unutturmak için: unut <numara>")
