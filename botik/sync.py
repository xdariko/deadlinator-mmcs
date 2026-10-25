import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime

from botik import texts
from botik.config import Config
from botik.moodle import InvalidToken, Moodle, MoodleError
from botik.storage import Assignment, Storage, User

log = logging.getLogger(__name__)

DAY = 86400
STATUS_WINDOW = (-14 * DAY, 8 * DAY)
OVERDUE_LOOKBACK = 7 * DAY


class Syncer:
    def __init__(self, moodle: Moodle, store: Storage, cfg: Config):
        self.moodle = moodle
        self.store = store
        self.cfg = cfg
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def sync(self, chat_id: int) -> list[str]:
        async with self._locks[chat_id]:
            user = self.store.get_user(chat_id)
            if not user or not user.active:
                return []
            silent = not user.initialized
            msgs = await self._sync(user, silent)
            if silent:
                self.store.set_initialized(chat_id)
            return msgs

    def digest(self, chat_id: int, weekly: bool) -> str:
        now = int(time.time())
        items = self.store.assignments(chat_id)
        pending = [a for a in items if a.done is not True and a.opens <= now and a.due]
        week = sorted((a for a in pending if now < a.due <= now + 7 * DAY), key=lambda a: a.due)
        tails = sorted(
            (a for a in pending
             if now - 14 * DAY < a.due <= now and (not a.cutoff or a.cutoff > now)),
            key=lambda a: a.due,
        )
        return texts.digest(week, tails, now, self.cfg.tz, weekly)

    async def _try(self, token: str, function: str, **params):
        try:
            return await self.moodle.call(token, function, **params)
        except InvalidToken:
            raise
        except MoodleError as e:
            log.warning("%s(%s) failed: %s", function, params, e)
            return None

    async def _sync(self, user: User, silent: bool) -> list[str]:
        s, tz, chat, tok = self.store, self.cfg.tz, user.chat_id, user.token
        now = int(time.time())
        out: list[str] = []

        courses = await self.moodle.call(tok, "core_enrol_get_users_courses", userid=user.moodle_userid)
        courses = [c for c in courses if c.get("visible", 1)]
        names = {c["id"]: c.get("fullname") or c.get("shortname") or "?" for c in courses}

        seen_courses = s.get_state(chat, "course")
        fresh: set[int] = set()
        for cid in names:
            if str(cid) not in seen_courses:
                fresh.add(cid)
                s.set_state(chat, "course", str(cid))
                if not silent:
                    out.append(texts.new_course(names[cid]))

        def quiet(cid: int) -> bool:
            return silent or cid in fresh

        assigns: dict[int, tuple[int, dict]] = {}
        resp = None
        if courses:
            resp = await self._try(tok, "mod_assign_get_assignments", courseids=list(names))
        assigns_ok = resp is not None
        for c in (resp or {}).get("courses", []):
            for a in c.get("assignments", []):
                assigns[a["cmid"]] = (c["id"], a)

        seen_mods = s.get_state(chat, "module")
        materials: list[tuple[str, str, str, str]] = []
        visible: set[int] = set()
        contents_ok: set[int] = set()
        for cid in names:
            sections = await self._try(tok, "core_course_get_contents", courseid=cid)
            if sections is None:
                continue
            contents_ok.add(cid)
            for sec in sections:
                for mod in sec.get("modules", []):
                    modname = mod.get("modname")
                    if modname == "label" or not mod.get("uservisible", True):
                        continue
                    cmid = mod["id"]
                    visible.add(cmid)
                    if modname == "assign":
                        a = assigns.get(cmid)
                        if not assigns_ok or a is None or not a[1].get("nosubmissions"):
                            continue
                    if str(cmid) in seen_mods:
                        continue
                    s.set_state(chat, "module", str(cmid))
                    if not quiet(cid):
                        url = mod.get("url") or f"{self.moodle.base}/course/view.php?id={cid}"
                        materials.append((names[cid], mod.get("name", "?"), modname, url))
        if materials:
            out.append(texts.new_materials(materials))

        if assigns_ok:
            opened = s.get_state(chat, "assign_open")
            rows: list[Assignment] = []
            new_assigns: list[Assignment] = []
            for cmid, (cid, a) in assigns.items():
                if a.get("nosubmissions") or cid not in names:
                    continue
                if cid in contents_ok and cmid not in visible:
                    continue
                opens = a.get("allowsubmissionsfromdate") or 0
                due = a.get("duedate") or 0
                done = None
                if opens <= now and due and now + STATUS_WINDOW[0] <= due <= now + STATUS_WINDOW[1]:
                    done = await self._is_done(tok, a["id"])
                item = Assignment(
                    id=a["id"], course=names[cid], name=a.get("name", "?"),
                    url=f"{self.moodle.base}/mod/assign/view.php?id={cmid}",
                    opens=opens, due=due, cutoff=a.get("cutoffdate") or 0, done=done,
                )
                rows.append(item)
                if opens <= now and str(item.id) not in opened:
                    s.set_state(chat, "assign_open", str(item.id))
                    if not quiet(cid) and done is not True:
                        new_assigns.append(item)
            s.replace_assignments(chat, rows)
            if new_assigns:
                new_assigns.sort(key=lambda a: a.due or float("inf"))
                out.append(texts.new_assignments(new_assigns, now, tz))

        old_grades = {k: json.loads(v) for k, v in s.get_state(chat, "grade").items()}
        for cid in names:
            data = await self._try(
                tok, "gradereport_user_get_grade_items", courseid=cid, userid=user.moodle_userid
            )
            if data is None:
                continue
            current: dict[str, dict] = {}
            for ug in data.get("usergrades", []):
                for gi in ug.get("gradeitems", []):
                    if gi.get("itemtype") in ("course", "category"):
                        continue
                    current[str(gi["id"])] = {
                        "cid": cid,
                        "name": gi.get("itemname") or "Без названия",
                        "v": _grade_value(gi),
                        "raw": gi.get("graderaw"),
                        "max": gi.get("grademax"),
                    }
            prev = {k: g for k, g in old_grades.items() if g.get("cid") == cid}
            for key, cur in current.items():
                p = prev.get(key)
                pv = p["v"] if p else ""
                if cur["v"] != pv and not quiet(cid):
                    kind = "new" if not pv else ("changed" if cur["v"] else "deleted")
                    out.append(texts.grade(kind, names[cid], cur if cur["v"] else p, pv))
                if p != cur:
                    s.set_state(chat, "grade", key, json.dumps(cur, ensure_ascii=False))
            for key, p in prev.items():
                if key not in current:
                    s.del_state(chat, "grade", key)
                    if p["v"] and not quiet(cid):
                        out.append(texts.grade("deleted", names[cid], p, p["v"]))

        items = s.assignments(chat)
        overdue_seen = s.get_state(chat, "overdue")
        overdue = [
            a for a in items
            if a.done is not True and a.due and now - OVERDUE_LOOKBACK < a.due <= now
            and str(a.id) not in overdue_seen
        ]
        for a in overdue:
            s.set_state(chat, "overdue", str(a.id))
        if overdue and not silent:
            out.append(texts.overdue(sorted(overdue, key=lambda a: a.due), now, tz))

        local = datetime.now(tz)
        if local.hour >= self.cfg.remind_hour:
            today = local.date()
            today_seen = s.get_state(chat, "today")
            due_today = [
                a for a in items
                if a.done is not True and a.opens <= now and a.due > now
                and datetime.fromtimestamp(a.due, tz).date() == today
                and f"{a.id}:{today}" not in today_seen
            ]
            for a in due_today:
                s.set_state(chat, "today", f"{a.id}:{today}")
            if due_today:
                out.append(texts.due_today(sorted(due_today, key=lambda a: a.due), now, tz))

        return out

    async def _is_done(self, token: str, assign_id: int) -> bool | None:
        st = await self._try(token, "mod_assign_get_submission_status", assignid=assign_id)
        if st is None:
            return None
        attempt = st.get("lastattempt") or {}
        if attempt.get("gradingstatus") == "graded":
            return True
        sub = attempt.get("teamsubmission") or attempt.get("submission")
        if not sub:
            return False
        if sub.get("status") == "submitted":
            return True
        return sub.get("status") == "draft" and _has_content(sub)


def _has_content(sub: dict) -> bool:
    for plugin in sub.get("plugins", []):
        if any(fa.get("files") for fa in plugin.get("fileareas", [])):
            return True
        if any((ef.get("text") or "").strip() for ef in plugin.get("editorfields", [])):
            return True
    return False


def _grade_value(gi: dict) -> str:
    v = (gi.get("gradeformatted") or "").strip()
    return "" if v == "-" else v
