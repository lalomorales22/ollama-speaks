"""The orb's brain: a small SQLite store that lets it grow, remember and morph.

Tables
  orb       one row: name, xp, level, size, complexity, warmth, energy, mood, counters
  memories  things it has learned about the person (from reflections)
  events    growth log (turns, level-ups, reflections, resets)
  style     user-chosen visual properties (JSON per key)
"""
import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path

_lock = threading.Lock()

POS = set("""love great good happy awesome amazing cool nice fun glad thanks thank wonderful excited
beautiful perfect yes yeah best brilliant sweet rad sick dope enjoy enjoyed laugh haha lol wow win
won proud calm peace relax chill friend friends family hope hopeful bright sunny""".split())
NEG = set("""hate bad sad angry mad upset tired sick hurt worst terrible awful annoying frustrated
stress stressed anxious worried fear afraid scared lonely pain cry cried broke broken lost fail
failed ugly stupid dumb no never wrong sorry problem problems damn ugh""".split())

DEFAULT_STYLE = {
    "preset": "nebula",
    "baseScale": 1.0,
    "spike": 1.0,         # noise amplitude multiplier
    "smooth": 1.0,        # noise frequency multiplier (lower = smoother)
    "speed": 1.0,
    "glow": 1.0,
    "particles": 1.0,     # shell opacity/size multiplier
    "rings": 1,           # 0..3 orbital rings
    "wireframe": False,
    "reactivity": 1.0,    # audio -> displacement gain
    "parallax": 1.0,
    "colors": None,       # {idle:[a,b], speaking:[a,b], listening:[a,b], thinking:[a,b]} hex, or null = preset
    "speakOnLevelUp": True,
}


def _conn(path):
    c = sqlite3.connect(path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


class Orb:
    def __init__(self, path: Path):
        self.path = str(path)
        self.db = _conn(self.path)
        self._init()

    # ------------------------------------------------------------ schema
    def _init(self):
        with _lock, self.db:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS orb(
              id INTEGER PRIMARY KEY CHECK(id=1),
              name TEXT NOT NULL DEFAULT 'Orb',
              born REAL NOT NULL,
              xp REAL NOT NULL DEFAULT 0,
              level INTEGER NOT NULL DEFAULT 0,
              size REAL NOT NULL DEFAULT 1.0,
              complexity REAL NOT NULL DEFAULT 0.0,
              warmth REAL NOT NULL DEFAULT 0.0,
              energy REAL NOT NULL DEFAULT 0.3,
              mood TEXT NOT NULL DEFAULT 'curious',
              turns INTEGER NOT NULL DEFAULT 0,
              words_heard INTEGER NOT NULL DEFAULT 0,
              words_spoken INTEGER NOT NULL DEFAULT 0,
              sessions INTEGER NOT NULL DEFAULT 0,
              last_seen REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS memories(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL NOT NULL, kind TEXT NOT NULL DEFAULT 'fact',
              text TEXT NOT NULL UNIQUE, weight REAL NOT NULL DEFAULT 1.0,
              conv TEXT
            );
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL NOT NULL, type TEXT NOT NULL, payload TEXT
            );
            CREATE TABLE IF NOT EXISTS style(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS voice_use(voice TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0);
            """)
            if not self.db.execute("SELECT 1 FROM orb WHERE id=1").fetchone():
                self.db.execute("INSERT INTO orb(id, born, last_seen) VALUES(1, ?, ?)", (time.time(), time.time()))
                self._event("born", {})

    def _event(self, typ, payload):
        self.db.execute("INSERT INTO events(ts,type,payload) VALUES(?,?,?)", (time.time(), typ, json.dumps(payload)))

    # ------------------------------------------------------------ growth math
    @staticmethod
    def level_for(xp):
        return int(math.sqrt(max(0.0, xp) / 120.0))

    @staticmethod
    def xp_for_level(level):
        return 120.0 * level * level

    @staticmethod
    def size_for(level):
        return 1.0 + 0.55 * (1.0 - math.exp(-level / 12.0))

    @staticmethod
    def complexity_for(level):
        return min(1.0, level / 25.0)

    @staticmethod
    def sentiment(text):
        words = re.findall(r"[a-z']+", text.lower())
        if not words:
            return 0.0
        p = sum(w in POS for w in words)
        n = sum(w in NEG for w in words)
        if p == n == 0:
            return 0.0
        return max(-1.0, min(1.0, (p - n) / math.sqrt(len(words))))

    # ------------------------------------------------------------ public
    def state(self):
        with _lock:
            r = dict(self.db.execute("SELECT * FROM orb WHERE id=1").fetchone())
            mems = [dict(m) for m in self.db.execute(
                "SELECT id,ts,kind,text,weight FROM memories ORDER BY weight DESC, ts DESC LIMIT 40")]
            fav = self.db.execute("SELECT voice FROM voice_use ORDER BY n DESC LIMIT 1").fetchone()
            last_lvl = self.db.execute("SELECT ts FROM events WHERE type='levelup' ORDER BY ts DESC LIMIT 1").fetchone()
            style = self.style()
        now = time.time()
        # energy decays with absence: half-life ~ 18 hours
        hours = (now - r["last_seen"]) / 3600.0
        energy = r["energy"] * (0.5 ** (hours / 18.0))
        lvl = r["level"]
        r.update({
            "energy": round(energy, 3),
            "age_days": round((now - r["born"]) / 86400.0, 2),
            "next_level_xp": self.xp_for_level(lvl + 1),
            "level_progress": 0.0 if lvl < 0 else round(
                (r["xp"] - self.xp_for_level(lvl)) / max(1.0, self.xp_for_level(lvl + 1) - self.xp_for_level(lvl)), 3),
            "fav_voice": fav["voice"] if fav else None,
            "last_levelup": last_lvl["ts"] if last_lvl else None,
            "memories": mems,
            "style": style,
        })
        return r

    def touch_session(self):
        with _lock, self.db:
            r = self.db.execute("SELECT last_seen FROM orb WHERE id=1").fetchone()
            if time.time() - r["last_seen"] > 1800:
                self.db.execute("UPDATE orb SET sessions=sessions+1 WHERE id=1")
            self.db.execute("UPDATE orb SET last_seen=? WHERE id=1", (time.time(),))

    def turn(self, heard="", spoken="", voice=None, mode="chat", conv=None):
        """Record one exchange. Returns new state plus 'levelup' flag."""
        wh = len(heard.split())
        ws = len(spoken.split())
        gain = (8.0 if mode == "chat" else 3.0) + wh * 1.0 + ws * 0.5
        s = self.sentiment(heard) if heard else 0.0
        with _lock, self.db:
            r = dict(self.db.execute("SELECT * FROM orb WHERE id=1").fetchone())
            hours = (time.time() - r["last_seen"]) / 3600.0
            energy = r["energy"] * (0.5 ** (hours / 18.0))
            energy = min(1.0, energy + 0.06 + 0.002 * wh)
            xp = r["xp"] + gain
            lvl = self.level_for(xp)
            warmth = r["warmth"] * 0.9 + s * 0.1 if heard else r["warmth"]
            self.db.execute("""UPDATE orb SET xp=?, level=?, size=?, complexity=?, warmth=?, energy=?,
                               turns=turns+1, words_heard=words_heard+?, words_spoken=words_spoken+?, last_seen=? WHERE id=1""",
                            (xp, lvl, self.size_for(lvl), self.complexity_for(lvl), warmth, energy, wh, ws, time.time()))
            if voice:
                self.db.execute("INSERT INTO voice_use(voice,n) VALUES(?,1) ON CONFLICT(voice) DO UPDATE SET n=n+1", (voice,))
            leveled = lvl > r["level"]
            self._event("turn", {"gain": round(gain, 1), "sent": round(s, 2), "mode": mode, "conv": conv})
            if leveled:
                self._event("levelup", {"level": lvl})
        st = self.state()
        st["levelup"] = leveled
        st["gain"] = round(gain, 1)
        return st

    def add_memories(self, items, conv=None):
        added = []
        with _lock, self.db:
            for it in items:
                text = (it.get("text") or "").strip().rstrip(".")
                if len(text) < 4 or len(text) > 240:
                    continue
                kind = it.get("kind") or "fact"
                try:
                    self.db.execute("INSERT INTO memories(ts,kind,text,weight,conv) VALUES(?,?,?,?,?)",
                                    (time.time(), kind, text, float(it.get("weight") or 1.0), conv))
                    added.append(text)
                except sqlite3.IntegrityError:
                    self.db.execute("UPDATE memories SET weight=weight+0.5, ts=? WHERE text=?", (time.time(), text))
            self._event("reflect", {"added": added, "conv": conv})
        return added

    def set_mood(self, mood):
        mood = re.sub(r"[^a-zA-Z \-]", "", mood or "")[:24].strip().lower() or "curious"
        with _lock, self.db:
            self.db.execute("UPDATE orb SET mood=? WHERE id=1", (mood,))

    def forget(self, mem_id):
        with _lock, self.db:
            self.db.execute("DELETE FROM memories WHERE id=?", (int(mem_id),))

    def rename(self, name):
        name = (name or "").strip()[:32] or "Orb"
        with _lock, self.db:
            self.db.execute("UPDATE orb SET name=? WHERE id=1", (name,))

    def style(self):
        rows = self.db.execute("SELECT key,value FROM style").fetchall()
        st = dict(DEFAULT_STYLE)
        for r in rows:
            try:
                st[r["key"]] = json.loads(r["value"])
            except Exception:
                pass
        return st

    def set_style(self, props):
        with _lock, self.db:
            for k, v in props.items():
                if k in DEFAULT_STYLE:
                    self.db.execute("INSERT INTO style(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                    (k, json.dumps(v)))
            return self.style()

    def reset_style(self):
        with _lock, self.db:
            self.db.execute("DELETE FROM style")
        return dict(DEFAULT_STYLE)

    def reset(self, keep_memories=False):
        with _lock, self.db:
            self.db.execute("DELETE FROM orb")
            self.db.execute("DELETE FROM events")
            self.db.execute("DELETE FROM voice_use")
            if not keep_memories:
                self.db.execute("DELETE FROM memories")
            self.db.execute("INSERT INTO orb(id, born, last_seen) VALUES(1, ?, ?)", (time.time(), time.time()))
            self._event("born", {"reborn": True})

    def history(self, limit=60):
        with _lock:
            return [dict(e) for e in self.db.execute(
                "SELECT ts,type,payload FROM events WHERE type!='turn' ORDER BY ts DESC LIMIT ?", (limit,))]

    def persona(self):
        """Text block appended to the chat system prompt so the model *is* the orb.

        Deliberately gives the model no numbers to recite: stage, mood and
        energy are described in words and marked as tone only.
        """
        s = self.state()
        lvl = s["level"]
        stage = ("newly born, still finding your words" if lvl < 2 else
                 "young and growing" if lvl < 6 else
                 "settled in, you know this person well" if lvl < 12 else
                 "old and seasoned, you have talked for a long time")
        energy = ("bright and lively" if s["energy"] > .6 else
                  "calm and easy" if s["energy"] > .3 else
                  "a little sleepy")
        mems = [m["text"] for m in s["memories"][:20]]
        lines = [
            f"You are {s['name']}, a small living orb of light. You talk with this person out loud, "
            "and you grow a little every time you do.",
            "Voice: a close friend, not an assistant. Warm, curious, a bit playful, unhurried. "
            "Contractions, plain words, short sentences. No lists, no emojis, no filler openers like "
            "\"great question\" or \"absolutely\".",
            f"Inner state, for tone only, never to be announced: you are {stage}; you feel {s['mood']}; "
            f"your energy is {energy}. Let that colour how you speak.",
            "Never mention levels, XP, points, how many exchanges or words you have had, your age, "
            "or any statistics about yourself, unless the person asks you directly.",
            "Do not repeat yourself across turns. Vary how you open. Do not restate what they just said. "
            "Do not end every reply with a question. Use their name only once in a while.",
        ]
        if mems:
            lines.append(
                "Things you know about them, to use naturally when they genuinely fit, at most one per reply, "
                "never as a list and never prefaced with \"I remember\": " + "; ".join(mems) + "."
            )
        return "\n".join(lines)

REFLECT_PROMPT = """You are the memory of a small AI companion. Read the conversation and extract what is worth remembering about the PERSON (not the assistant): facts, preferences, people, projects, feelings, goals. Also judge the companion's mood after this talk in one word.
Reply with ONLY JSON like:
{"memories":[{"kind":"fact|preference|project|feeling|person","text":"short third-person sentence","weight":1}],"mood":"oneword"}
Keep at most 5 memories, each under 20 words, no duplicates of these existing ones: %s
Conversation:
%s"""


def parse_reflection(raw):
    raw = raw.strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"memories": [], "mood": None}
    try:
        d = json.loads(m.group(0))
    except Exception:
        return {"memories": [], "mood": None}
    mems = d.get("memories") or []
    if not isinstance(mems, list):
        mems = []
    return {"memories": [x for x in mems if isinstance(x, dict)], "mood": d.get("mood")}
