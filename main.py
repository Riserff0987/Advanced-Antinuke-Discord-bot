import asyncio
import copy
import json
import os
import platform
import re
import time
from datetime import timedelta
from typing import Literal
from collections import defaultdict, deque

import discord
from discord.ext import commands, tasks

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PREFIX = "."
START_TIME = time.time()
CONFIG_PATH = os.getenv("CONFIG_PATH", "config.json")
# Optional: comma-separated user IDs that can configure the bot in every server
EXTRA_OWNERS = {int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip().isdigit()}

# Audit log action -> config key
ACTIONS = {
    discord.AuditLogAction.channel_delete: "channel_delete",
    discord.AuditLogAction.channel_create: "channel_create",
    discord.AuditLogAction.role_delete: "role_delete",
    discord.AuditLogAction.role_create: "role_create",
    discord.AuditLogAction.ban: "ban",
    discord.AuditLogAction.kick: "kick",
    discord.AuditLogAction.webhook_create: "webhook_create",
    discord.AuditLogAction.bot_add: "bot_add",
    discord.AuditLogAction.channel_update: "channel_update",
    discord.AuditLogAction.role_update: "role_update",
    discord.AuditLogAction.emoji_delete: "emoji_delete",
    discord.AuditLogAction.sticker_delete: "sticker_delete",
    discord.AuditLogAction.emoji_create: "emoji_create",
    discord.AuditLogAction.sticker_create: "sticker_create",
    discord.AuditLogAction.member_update: "nick_change",
    discord.AuditLogAction.member_role_update: "member_role_update",
}

# A user is punished when they exceed `limit` actions inside `window` seconds.
DEFAULTS = {
    "prefix": PREFIX,
    "logs": {"channels": {}, "ignore_bots": True, "ignore_channels": []},
    "boost": {"channel": None, "roles": []},
    "enabled": True,
    "punishment": "ban",  # ban | kick | strip
    "log_channel": None,
    "whitelist": [],
    "window": 10,
    "limits": {
        "channel_delete": 1,
        "channel_create": 3,
        "role_delete": 1,
        "role_create": 3,
        "ban": 2,
        "kick": 2,
        "webhook_create": 1,
        "bot_add": 0,
        "channel_update": 5,  # renames only, see the handler; higher because admins rename things in bulk
        "emoji_delete": 3,
        "sticker_delete": 3,
        "nick_change": 5,  # nickname changes only
        "emoji_create": 3,
        "sticker_create": 3,
        "role_update": 5,
    },
    "warnings": {},
    "raid": {
        "auto": True,
        "join_count": 5,
        "join_seconds": 10,
        "action": "kick",  # kick | ban | timeout
        "duration_minutes": 10,
        "min_account_age_days": 0,
        "active_until": 0,
    },
    "automod": {
        "enabled": True,
        "antiinvite": True,
        "antilink": False,
        "antispam": True,
        "antimention": True,
        "badwords": [],
        "spam_count": 5,
        "spam_seconds": 5,
        "spam_action": "timeout",  # delete | warn | timeout | kick | ban
        "spam_timeout_minutes": 10,
        "mention_limit": 5,
        "action": "timeout",  # delete | warn | timeout
        "timeout_minutes": 10,
    },
}


class Store:
    def __init__(self, path):
        self.path = path
        self.data = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}

    def get(self, guild_id):
        cfg = self.data.setdefault(str(guild_id), copy.deepcopy(DEFAULTS))
        for k, v in DEFAULTS.items():
            cfg.setdefault(k, copy.deepcopy(v))
        for k, v in DEFAULTS["limits"].items():
            cfg["limits"].setdefault(k, v)
        for k, v in DEFAULTS["automod"].items():
            cfg["automod"].setdefault(k, copy.deepcopy(v))
        return cfg

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)


STORE = Store(CONFIG_PATH)  # shared by all cogs

OWNERS_PATH = os.getenv("OWNERS_PATH", "owners.json")


class OwnerStore:
    """Extra bot owners added with /owners add. The main owner (panel OWNER_IDS or the app owner) is not stored here."""

    def __init__(self, path):
        self.path = path
        self.ids = set()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.ids = {int(x) for x in json.load(f).get("owners", [])}
            except Exception:
                os.replace(path, path + ".corrupt")

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"owners": sorted(self.ids)}, f, indent=2)
        os.replace(tmp, self.path)


OWNERS = OwnerStore(OWNERS_PATH)
BOT_OWNER_IDS = set(EXTRA_OWNERS)  # filled in setup_hook from the Developer Portal owner or team



# ---------------- nuke-bot database ----------------

NUKEDB_PATH = os.getenv("NUKEDB_PATH", "nukedb.json")
NUKEDB_MIN_ACTIONS = int(os.getenv("NUKEDB_MIN_ACTIONS", "5"))  # destructive actions needed before a bot is listed
NUKEDB_VERIFIED_MIN_ACTIONS = int(os.getenv("NUKEDB_VERIFIED_MIN_ACTIONS", "10"))  # verified bots need more evidence
NUKEDB_NEW_JOIN_DAYS = 7      # a bot that joined the server longer ago than this is never auto-listed
NUKEDB_NEW_ACCOUNT_DAYS = 30  # used only when the join date is unknown
NUKE_DESTRUCTIVE = {"channel_delete", "role_delete", "ban", "kick", "emoji_delete", "sticker_delete"}


def _int_env(name):
    v = os.getenv(name, "").strip()
    return int(v) if v.isdigit() else 0


# Sync between the free and paid bot: a private channel both bots can see, plus the other bot's user ID.
NUKEDB_SYNC_CHANNEL = _int_env("NUKEDB_SYNC_CHANNEL")
NUKEDB_PEER_ID = _int_env("NUKEDB_PEER_ID")

# Shared PostgreSQL storage (preferred over the sync channel). Example:
# DATABASE_URL=postgresql://user:password@host:5432/dbname
NUKEDB_DSN = os.getenv("DATABASE_URL", "").strip()
try:
    import asyncpg
except ImportError:
    asyncpg = None


class NukeDB:
    """Malicious bots (banned on sight) and an allowlist of bots that can never be listed.

    This is the in-memory cache every check reads. It is saved to a local JSON file, and when
    DATABASE_URL is set every change is also written to PostgreSQL through `hook`.
    """

    def __init__(self, path):
        self.path = path
        self.bots = {}
        self.allow = []
        self.hook = None     # set by NukePG once the database is connected
        self.backlog = []    # changes made while the database was unreachable
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                self.bots = data.get("bots", {})
                self.allow = data.get("allow", [])
            except Exception:
                os.replace(path, path + ".corrupt")

    def _emit(self, kind, bot_id):
        entry = dict(self.bots.get(str(bot_id)) or {})
        if self.hook:
            self.hook(kind, int(bot_id), entry)
        elif NUKEDB_DSN and len(self.backlog) < 500:
            self.backlog.append((kind, int(bot_id), entry))

    def blocked(self, bot_id):
        return str(bot_id) in self.bots

    def allowed(self, bot_id):
        return int(bot_id) in self.allow

    def add(self, bot_id, **info):
        entry = self.bots.get(str(bot_id))
        if entry:
            entry["reports"] = entry.get("reports", 1) + 1
        else:
            self.bots[str(bot_id)] = {"reports": 1, "flagged_at": int(time.time()), **info}
        was_allowed = int(bot_id) in self.allow
        if was_allowed:
            self.allow.remove(int(bot_id))
        self.save()
        self._emit("upsert", bot_id)
        if was_allowed:
            self._emit("unallow", bot_id)

    def merge(self, bot_id, entry):
        """Add an entry received through the sync channel (no report bump, keeps it idempotent)."""
        if str(bot_id) in self.bots:
            return False
        self.bots[str(bot_id)] = {**entry, "source": entry.get("source", "synced"), "synced": True}
        if int(bot_id) in self.allow:
            self.allow.remove(int(bot_id))
        self.save()
        return True

    def remove(self, bot_id, kind="delete"):
        if self.bots.pop(str(bot_id), None) is None:
            return False
        self.save()
        self._emit(kind, bot_id)  # kind is "delete" or "release"
        return True

    def set_allowed(self, bot_id, value):
        bot_id = int(bot_id)
        if value and bot_id not in self.allow:
            self.allow.append(bot_id)
            removed = self.bots.pop(str(bot_id), None) is not None
            self.save()
            if removed:
                self._emit("delete", bot_id)
            self._emit("allow", bot_id)
            return True
        if not value and bot_id in self.allow:
            self.allow.remove(bot_id)
            self.save()
            self._emit("unallow", bot_id)
            return True
        return False

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"bots": self.bots, "allow": self.allow}, f, indent=2)
        os.replace(tmp, self.path)


NUKEDB = NukeDB(NUKEDB_PATH)


NUKEPG_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS nukedb;
CREATE TABLE IF NOT EXISTS nukedb.bots (
    bot_id     bigint PRIMARY KEY,
    status     text NOT NULL DEFAULT 'listed',
    data       jsonb NOT NULL DEFAULT '{}',
    banned_in  bigint[] NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS nukedb.allow (bot_id bigint PRIMARY KEY);
"""
_MERGE_BANNED = "(SELECT ARRAY(SELECT DISTINCT x FROM unnest({a} || {b}) AS x))"


class NukePG:
    """Shared PostgreSQL storage for the nuke-bot list (all tables live in the `nukedb` schema)."""

    def __init__(self, dsn):
        self.dsn = dsn
        self.pool = None
        self.active = False
        self.last_error = ""  # shown in /nukedb so problems are visible without the console
        self.lock = asyncio.Lock()
        self.inflight = {}  # bot id -> number of local writes not yet saved to the database
        self.fails = 0
        self.next_try = 0.0  # monotonic time before which we don't try to reconnect

    async def start(self):
        if self.active or not self.dsn or time.monotonic() < self.next_try:
            return
        if asyncpg is None:
            self.last_error = "asyncpg isn't installed (add it to requirements.txt)"
            print(f"[nukedb] {self.last_error}")
            return
        try:
            self.pool = await asyncio.wait_for(
                asyncpg.create_pool(self.dsn, min_size=1, max_size=2, command_timeout=15, statement_cache_size=0),
                timeout=8,
            )
            async with self.pool.acquire() as con:
                await con.execute(NUKEPG_SCHEMA)
                remote_bot_ids = {int(r["bot_id"]) for r in await con.fetch("SELECT bot_id FROM nukedb.bots")}
                remote_allow_ids = {int(r["bot_id"]) for r in await con.fetch("SELECT bot_id FROM nukedb.allow")}
        except Exception as exc:
            self.fails += 1
            self.next_try = time.monotonic() + min(600, 15 * 2 ** min(self.fails, 6))
            self.last_error = f"{type(exc).__name__}: {exc or 'no response'}"[:300]
            print(f"[nukedb] database unavailable, using the local file for now: {exc!r}")
            if self.pool is not None:
                await self.pool.close()
                self.pool = None
            return
        self.active = True
        self.fails = 0
        NUKEDB.hook = self.on_change
        backlog, NUKEDB.backlog = NUKEDB.backlog, []
        # Push every local entry the database doesn't have yet. Entries already in the database are left
        # alone, and the pull below then brings in what the other bot added.
        backlog += [("upsert", int(k), dict(v)) for k, v in NUKEDB.bots.items() if int(k) not in remote_bot_ids]
        backlog += [("allow", a, {}) for a in NUKEDB.allow if a not in remote_allow_ids]
        for kind, bid, entry in backlog:
            self.on_change(kind, bid, entry)
        await self.pull()
        print("[nukedb] connected to PostgreSQL")

    def on_change(self, kind, bot_id, entry):
        if not self.active or self.pool is None:
            NUKEDB.backlog.append((kind, bot_id, entry))  # sent when the database reconnects
            return
        self.inflight[bot_id] = self.inflight.get(bot_id, 0) + 1
        try:
            asyncio.get_running_loop().create_task(self.write(kind, bot_id, entry))
        except RuntimeError:
            self.inflight[bot_id] -= 1

    async def write(self, kind, bid, entry):
        try:
            async with self.lock, self.pool.acquire() as con:
                banned = [int(g) for g in entry.get("banned_in", [])]
                if kind == "upsert":
                    data = {k: v for k, v in entry.items() if k != "banned_in"}
                    await con.execute(
                        "INSERT INTO nukedb.bots AS b (bot_id, status, data, banned_in) VALUES ($1, 'listed', $2::jsonb, $3) "
                        "ON CONFLICT (bot_id) DO UPDATE SET status = 'listed', data = EXCLUDED.data, updated_at = now(), "
                        "banned_in = " + _MERGE_BANNED.format(a="b.banned_in", b="EXCLUDED.banned_in"),
                        bid, json.dumps(data), banned,
                    )
                elif kind == "banned":
                    await con.execute(
                        "UPDATE nukedb.bots SET updated_at = now(), banned_in = "
                        + _MERGE_BANNED.format(a="banned_in", b="$2::bigint[]") + " WHERE bot_id = $1",
                        bid, banned,
                    )
                elif kind == "delete":
                    await con.execute("DELETE FROM nukedb.bots WHERE bot_id = $1", bid)
                elif kind == "release":
                    await con.execute("UPDATE nukedb.bots SET status = 'released', updated_at = now() WHERE bot_id = $1", bid)
                elif kind == "allow":
                    await con.execute("INSERT INTO nukedb.allow (bot_id) VALUES ($1) ON CONFLICT DO NOTHING", bid)
                elif kind == "unallow":
                    await con.execute("DELETE FROM nukedb.allow WHERE bot_id = $1", bid)
        except Exception as exc:
            print(f"[nukedb] database write failed ({kind} {bid}): {exc!r}")
        finally:
            self.inflight[bid] = self.inflight.get(bid, 1) - 1

    async def pull(self):
        """Make the local cache match the database. Returns (newly listed ids, released [(id, guild ids)])."""
        async with self.lock, self.pool.acquire() as con:
            rows = await con.fetch("SELECT bot_id, status, data, banned_in FROM nukedb.bots")
            allow_rows = await con.fetch("SELECT bot_id FROM nukedb.allow")
        busy = {b for b, n in self.inflight.items() if n > 0}
        new, released, remote_listed = [], [], set()
        for r in rows:
            bid = int(r["bot_id"])
            key = str(bid)
            if bid in busy:
                remote_listed.add(bid)
                continue  # a local change to this bot is still being saved, keep the local version
            if r["status"] == "listed":
                remote_listed.add(bid)
                if key not in NUKEDB.bots:
                    new.append(bid)
                data = json.loads(r["data"]) if isinstance(r["data"], str) else dict(r["data"])
                NUKEDB.bots[key] = {**data, "banned_in": list(r["banned_in"])}
            elif key in NUKEDB.bots:
                released.append((bid, list(r["banned_in"])))
                NUKEDB.bots.pop(key)
        for key in list(NUKEDB.bots):
            if int(key) not in remote_listed and int(key) not in busy:
                NUKEDB.bots.pop(key)  # removed on the other bot
        remote_allow = {int(r["bot_id"]) for r in allow_rows}
        NUKEDB.allow = sorted((remote_allow - busy) | (set(NUKEDB.allow) & busy))
        NUKEDB.save()
        self.last_error = ""
        return new, released


PG = NukePG(NUKEDB_DSN)


def is_dangerous(role: discord.Role) -> bool:
    p = role.permissions
    return (
        p.administrator or p.manage_guild or p.manage_roles or p.manage_channels
        or p.ban_members or p.kick_members or p.manage_webhooks
    )


DANGEROUS_PERMS = ("administrator", "manage_guild", "manage_roles", "manage_channels",
                    "ban_members", "kick_members", "manage_webhooks")


def new_dangerous(entry):
    """Dangerous permissions this audit entry handed out: a role gaining them, or a member being given such a role."""
    found = []
    if entry.action == discord.AuditLogAction.role_update:
        before = getattr(entry.before, "permissions", None)
        after = getattr(entry.after, "permissions", None)
        if before is not None and after is not None:
            found += [p for p in DANGEROUS_PERMS if getattr(after, p, False) and not getattr(before, p, False)]
    elif entry.action == discord.AuditLogAction.member_role_update:
        for r in (getattr(entry.after, "roles", None) or []):
            perms = getattr(r, "permissions", None)
            if perms is not None:
                found += [p for p in DANGEROUS_PERMS if getattr(perms, p, False)]
    return sorted(set(found))


class AntiNuke(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = STORE
        self.hits = defaultdict(deque)
        self.recent = defaultdict(deque)  # (guild, user) -> (time, action) for evidence
        self.punished = {}

    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False
        return ctx.author.id == ctx.guild.owner_id or ctx.author.id in EXTRA_OWNERS

    # ---------- detection ----------

    def trusted(self, guild, user_id, cfg):
        return (
            user_id == guild.owner_id
            or user_id == self.bot.user.id
            or user_id in BOT_OWNER_IDS
            or user_id in OWNERS.ids
            or user_id in cfg["whitelist"]
        )

    async def log(self, guild, cfg, title, desc):
        cid = cfg["log_channel"]
        ch = guild.get_channel(cid) if cid else None
        if ch:
            try:
                await ch.send(embed=discord.Embed(title=title, description=desc, color=0xE74C3C))
            except discord.HTTPException:
                pass

    async def punish(self, guild, user_id, cfg, reason, force_ban=False):
        if user_id == self.bot.user.id:
            return False  # never punish the bot itself
        mode = "ban" if force_ban else cfg["punishment"]  # bots are always banned: kick/strip can't stop them
        member = guild.get_member(user_id)
        try:
            if mode == "ban":
                await guild.ban(discord.Object(id=user_id), reason=reason, delete_message_seconds=0)
            elif mode == "kick" and member:
                await member.kick(reason=reason)
            elif mode == "quarantine" and member:
                await quarantine_member(guild, member, reason)
            elif mode == "strip" and member:
                roles = [
                    r for r in member.roles
                    if r != guild.default_role and not r.managed
                    and r < guild.me.top_role and is_dangerous(r)
                ]
                if roles:
                    await member.remove_roles(*roles, reason=reason)
            return True
        except discord.HTTPException:
            return False

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        key = ACTIONS.get(entry.action)
        if not key or entry.user is None:
            return
        if key in ("channel_update", "role_update") and getattr(entry.before, "name", None) == getattr(entry.after, "name", None):
            return  # not a rename (permission or position edit), so don't count it
        if key == "nick_change" and getattr(entry.before, "nick", None) == getattr(entry.after, "nick", None):
            return  # not a nickname change (timeout or other member edit)
        guild = entry.guild
        cfg = self.store.get(guild.id)
        if not cfg["enabled"] or self.trusted(guild, entry.user.id, cfg):
            return
        if entry.action in (discord.AuditLogAction.automod_rule_create, discord.AuditLogAction.automod_rule_update):
            if entry.user.id != self.bot.user.id:  # our own sync is allowed
                await self.automod_guard(guild, entry, cfg)
            return
        perms = new_dangerous(entry)
        if perms:
            await self.dangerous_punish(guild, entry, perms, cfg)
            return

        now = time.monotonic()
        rq = self.recent[(guild.id, entry.user.id)]
        rq.append((now, key))
        while rq and now - rq[0][0] > 60:
            rq.popleft()
        q = self.hits[(guild.id, entry.user.id, key)]
        q.append(now)
        while q and now - q[0] > cfg["window"]:
            q.popleft()

        if len(q) <= cfg["limits"][key]:
            return
        q.clear()

        # Claim the offender before any await so concurrent events don't double-fire.
        pk = (guild.id, entry.user.id)
        if now - self.punished.get(pk, -999) < 30:
            return
        self.punished[pk] = now

        reason = f"Anti-nuke: too many {key} ({cfg['limits'][key]} per {cfg['window']}s)"
        is_bot = bool(getattr(entry.user, "bot", False))
        mode_used = "ban" if is_bot else cfg["punishment"]
        member = guild.get_member(entry.user.id)
        joined_at = member.joined_at if member else None
        jobs = [self.punish(guild, entry.user.id, cfg, reason, force_ban=is_bot)]
        if key == "bot_add" and entry.target is not None:
            jobs.append(self.kick_bot(guild, entry.target.id, reason))
        results = await asyncio.gather(*jobs)  # punish + bot kick in parallel
        if not results[0]:
            self.punished.pop(pk, None)  # failed: allow a retry on the next event
        if is_bot:
            asyncio.create_task(self.review_bot(guild, entry.user, joined_at, key))

        extra = ""
        if len(results) > 1:
            extra = f"\nKicked added bot `{entry.target.id}`." if results[1] else "\nFailed to kick the added bot."
        # log in the background so it never delays enforcement
        asyncio.create_task(self.log(
            guild, cfg, "Anti-nuke triggered",
            f"User: {entry.user.mention} (`{entry.user.id}`)\nAction: `{key}`\n"
            f"Punishment: `{mode_used}` ({'done' if results[0] else 'FAILED'}){extra}",
        ))

    async def review_bot(self, guild, bot_user, joined_at, key=None):
        """After a bot was banned, decide if the evidence is strong enough to list it in the database."""
        try:
            await asyncio.sleep(2)  # let the rest of the burst show up in the audit log
            bid = bot_user.id

            async def skip(why):
                await self.log(
                    guild, self.store.get(guild.id), "Bot not added to nuke database",
                    f"Bot: {bot_user.mention} (`{bid}`)\nReason: {why}",
                )

            if NUKEDB.allowed(bid):
                return await skip("it is on the allowlist")
            if NUKEDB.blocked(bid):
                return await skip("it is already in the database")
            flags = getattr(bot_user, "public_flags", None)
            verified = bool(flags is not None and flags.verified_bot)
            cfg = self.store.get(guild.id)
            now = time.monotonic()
            horizon = cfg["window"] + 5
            seen = [k for t, k in self.recent[(guild.id, bid)] if now - t <= horizon and k in NUKE_DESTRUCTIVE]
            # Verified bots are trusted by default, so they need much stronger evidence
            # (this is how a hijacked popular bot gets caught).
            need = NUKEDB_VERIFIED_MIN_ACTIONS if verified else NUKEDB_MIN_ACTIONS
            if not verified and key in cfg["limits"]:
                # It already broke its limit for this action, so one more than the limit is enough evidence
                need = min(need, cfg["limits"][key] + 1)
            if len(seen) < need:
                return await skip(
                    f"only {len(seen)} destructive action(s) seen, it needs {need}"
                    + (" because it is a Discord-verified bot" if verified else "")
                    + " (see the NUKEDB_MIN_ACTIONS / NUKEDB_VERIFIED_MIN_ACTIONS variables)"
                )
            if not verified:
                utc = discord.utils.utcnow()
                if joined_at is not None:
                    newcomer = (utc - joined_at).days < NUKEDB_NEW_JOIN_DAYS
                else:
                    newcomer = (utc - bot_user.created_at).days < NUKEDB_NEW_ACCOUNT_DAYS
                if not newcomer:
                    return await skip(f"it joined this server more than {NUKEDB_NEW_JOIN_DAYS} days ago")
            counts = {}
            for k in seen:
                counts[k] = counts.get(k, 0) + 1
            NUKEDB.add(
                bid, name=str(bot_user), source="auto", verified=verified, guild_id=guild.id, guild_name=guild.name,
                banned_in=[guild.id], actions=counts,
                reason=f"{len(seen)} destructive actions within {cfg['window']}s",
            )
            banned = await enforce_db_everywhere(bid)
            await sync_publish("add", bid, NUKEDB.bots.get(str(bid)))
            summary = ", ".join(f"{k}: {v}" for k, v in counts.items())
            kind = "Verified bot quarantined (possible hijack)" if verified else "Bot added to nuke database"
            await self.log(
                guild, cfg, kind,
                f"Bot: {bot_user.mention} (`{bid}`)\nActions: {summary}\nAlso banned in {banned} other server(s).",
            )
            await notify_owners(
                f"{kind}: **{bot_user}** (`{bid}`) in **{guild.name}** ({summary}). "
                f"Wrong or recovered? Use `/nukedb release {bid}` (unbans it) or `/nukedb allow {bid}`."
            )
        except Exception as exc:
            print(f"[nukedb] review error: {exc!r}")

    async def automod_guard(self, guild, entry, cfg):
        """An untrusted user created or changed an AutoMod rule (the way attackers block all messages).
        Delete the rule, then punish the user the same way as a dangerous permission grant."""
        rule_id = getattr(entry.target, "id", None)
        name = "unknown"
        if rule_id:
            try:
                rule = await guild.fetch_automod_rule(rule_id)
                name = rule.name
                await rule.delete(reason="Anti-nuke: AutoMod rule created or changed by an untrusted user")
            except discord.HTTPException:
                pass
        await self.dangerous_punish(guild, entry, [f"AutoMod rule '{clip(name, 40)}' created or changed"], cfg)

    async def dangerous_punish(self, guild, entry, perms, cfg):
        """Someone who isn't trusted handed out dangerous permissions: strip their roles (or ban a bot)."""
        user = entry.user
        if user.id == self.bot.user.id:
            return  # never punish the bot itself
        reason = f"Anti-nuke: granted dangerous permissions ({', '.join(perms)})"
        if user.bot:
            try:
                await guild.ban(discord.Object(id=user.id), reason=reason, delete_message_seconds=0)
                note = "bot banned"
            except discord.HTTPException:
                note = "bot ban failed"
        else:
            member = guild.get_member(user.id)
            if member is None:
                note = "not in server"
            else:
                strip = [r for r in member.roles if not r.is_default() and not r.managed and r < guild.me.top_role]
                try:
                    if strip:
                        await member.remove_roles(*strip, reason=reason)
                    note = f"stripped {len(strip)} role(s)"
                except discord.HTTPException:
                    note = "role removal failed (check my role position)"
                await dm_action(member, guild, "Your roles were removed", reason, f"Action: {note}")
        self.log(guild, cfg, "Dangerous permissions granted",
                 f"{user.mention} (`{user.id}`) gave dangerous permissions: {', '.join(perms)}\nPunishment: {note}")

    async def kick_bot(self, guild, bot_id, reason):
        try:
            await guild.kick(discord.Object(id=bot_id), reason=reason)
            return True
        except discord.HTTPException:
            return False

    # ---------- commands ----------

    @commands.hybrid_group(name="antinuke", aliases=["an"], fallback="status", invoke_without_command=True)
    async def antinuke(self, ctx):
        """Show anti-nuke status."""
        cfg = self.store.get(ctx.guild.id)
        limits = "\n".join(f"`{k}`: {v}" for k, v in cfg["limits"].items())
        wl = ", ".join(f"<@{u}>" for u in cfg["whitelist"]) or "none"
        log = f"<#{cfg['log_channel']}>" if cfg["log_channel"] else "not set"
        e = discord.Embed(title="Anti-nuke", color=0x3498DB)
        e.add_field(name="Status", value="ON" if cfg["enabled"] else "OFF")
        e.add_field(name="Punishment", value=cfg["punishment"])
        e.add_field(name="Window", value=f"{cfg['window']}s")
        e.add_field(name="Log channel", value=log, inline=False)
        e.add_field(name="Limits (allowed per window)", value=limits, inline=False)
        e.add_field(name="Whitelist", value=wl, inline=False)
        e.set_footer(text=f"{ctx.clean_prefix}antinuke on|off|punishment|limit|window|logchannel|whitelist")
        await ctx.send(embed=e)

    @antinuke.command(name="on")
    async def an_on(self, ctx):
        """Enable anti-nuke."""
        self.store.get(ctx.guild.id)["enabled"] = True
        self.store.save()
        await ctx.send("Anti-nuke enabled.")

    @antinuke.command(name="off")
    async def an_off(self, ctx):
        """Disable anti-nuke."""
        self.store.get(ctx.guild.id)["enabled"] = False
        self.store.save()
        await ctx.send("Anti-nuke disabled.")

    @antinuke.command(name="punishment", aliases=["punish"])
    async def an_punishment(self, ctx, mode: Literal["ban", "kick", "strip", "quarantine"]):
        """Set punishment: ban, kick, strip (removes dangerous roles), or quarantine (hides every channel)."""
        mode = mode.lower()
        if mode not in ("ban", "kick", "strip", "quarantine"):
            return await ctx.send("Choose: `ban`, `kick`, `strip`, or `quarantine`.")
        self.store.get(ctx.guild.id)["punishment"] = mode
        self.store.save()
        await ctx.send(f"Punishment set to `{mode}`.")

    @antinuke.command(name="limit")
    async def an_limit(
        self, ctx,
        action: Literal["channel_delete", "channel_create", "channel_update", "role_delete", "role_create", "role_update", "ban", "kick", "webhook_create", "bot_add", "emoji_delete", "sticker_delete", "emoji_create", "sticker_create", "nick_change"],
        count: int,
    ):
        """Set allowed actions per window. Example: .antinuke limit ban 2"""
        action = action.lower()
        cfg = self.store.get(ctx.guild.id)
        if action not in cfg["limits"]:
            return await ctx.send("Actions: " + ", ".join(f"`{k}`" for k in cfg["limits"]))
        if not 0 <= count <= 50:
            return await ctx.send("Count must be 0-50.")
        cfg["limits"][action] = count
        self.store.save()
        await ctx.send(f"`{action}` limit set to {count}.")

    @antinuke.command(name="window")
    async def an_window(self, ctx, seconds: int):
        """Set the detection window in seconds (3-300)."""
        if not 3 <= seconds <= 300:
            return await ctx.send("Window must be 3-300 seconds.")
        self.store.get(ctx.guild.id)["window"] = seconds
        self.store.save()
        await ctx.send(f"Window set to {seconds}s.")

    @antinuke.command(name="logchannel", aliases=["log"])
    async def an_log(self, ctx, channel: discord.TextChannel = None):
        """Set log channel (no argument clears it)."""
        self.store.get(ctx.guild.id)["log_channel"] = channel.id if channel else None
        self.store.save()
        await ctx.send(f"Log channel: {channel.mention}" if channel else "Log channel cleared.")

    @antinuke.group(name="whitelist", aliases=["wl"], fallback="list", invoke_without_command=True)
    async def an_wl(self, ctx):
        """List whitelisted users."""
        wl = self.store.get(ctx.guild.id)["whitelist"]
        await ctx.send("Whitelist: " + (", ".join(f"<@{u}>" for u in wl) or "empty"))

    @an_wl.command(name="add")
    async def an_wl_add(self, ctx, user: discord.User):
        """Whitelist a user."""
        cfg = self.store.get(ctx.guild.id)
        if user.id not in cfg["whitelist"]:
            cfg["whitelist"].append(user.id)
            self.store.save()
        await ctx.send(f"Whitelisted {user.mention}.")

    @an_wl.command(name="remove", aliases=["rm"])
    async def an_wl_remove(self, ctx, user: discord.User):
        """Remove a user from the whitelist."""
        cfg = self.store.get(ctx.guild.id)
        if user.id in cfg["whitelist"]:
            cfg["whitelist"].remove(user.id)
            self.store.save()
        await ctx.send(f"Removed {user.mention} from whitelist.")


# ---------- moderation helpers ----------

DURATION_RE = re.compile(r"^(\d+)([smhd])$")
INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/[\w-]+", re.I)
LINK_RE = re.compile(r"https?://\S+|www\.\S+", re.I)


def parse_duration(text: str):
    m = DURATION_RE.match(text.strip().lower())
    if not m:
        return None
    secs = int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]
    if not 1 <= secs <= 28 * 86400:
        return None
    return timedelta(seconds=secs)


def check_target(ctx, user):
    """Return an error string if the invoker/bot can't act on this user, else None."""
    g = ctx.guild
    if user.id in (ctx.author.id, g.owner_id, ctx.bot.user.id):
        return "You can't target that user."
    member = g.get_member(user.id)
    if member:
        if ctx.author.id != g.owner_id and member.top_role >= ctx.author.top_role:
            return "Their top role is equal to or higher than yours."
        if member.top_role >= g.me.top_role:
            return "Their top role is higher than mine."
    return None


async def dm_action(user, guild, title, reason=None, details=None, color=0xE74C3C):
    """DM a person an embed about a moderation action taken on them. Never raises."""
    e = discord.Embed(title=title, description=f"Server: **{guild.name}**", color=color, timestamp=discord.utils.utcnow())
    if details:
        e.add_field(name="Details", value=clip(details, 500), inline=False)
    if reason:
        e.add_field(name="Reason", value=clip(reason, 1000), inline=False)
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    try:
        await user.send(embed=e)
    except discord.HTTPException:
        pass


async def try_dm(user, text):
    try:
        await user.send(text)
    except discord.HTTPException:
        pass


async def mod_log(guild, title, desc):
    cid = STORE.get(guild.id)["log_channel"]
    ch = guild.get_channel(cid) if cid else None
    if ch:
        try:
            await ch.send(embed=discord.Embed(title=title, description=desc, color=0xF1C40F))
        except discord.HTTPException:
            pass


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        return ctx.guild is not None

    @commands.hybrid_command(description="Ban a user (mention or ID).")
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def ban(self, ctx, user: discord.User, *, reason: str = "No reason provided"):
        if err := check_target(ctx, user):
            return await ctx.send(err)
        await dm_action(user, ctx.guild, "You were banned", reason)
        await ctx.guild.ban(user, reason=f"{ctx.author} ({ctx.author.id}): {reason}", delete_message_seconds=0)
        await ctx.send(f"Banned **{user}**: {reason}")
        await mod_log(ctx.guild, "Ban", f"{user.mention} (`{user.id}`) by {ctx.author.mention}\n{reason}")

    @commands.hybrid_command(description="Unban a user by ID.")
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx, user: discord.User, *, reason: str = "No reason provided"):
        try:
            await ctx.guild.unban(user, reason=f"{ctx.author} ({ctx.author.id}): {reason}")
        except discord.NotFound:
            return await ctx.send("That user isn't banned.")
        await dm_action(user, ctx.guild, "You were unbanned", reason, color=0x2ECC71)
        await ctx.send(f"Unbanned **{user}**.")
        await mod_log(ctx.guild, "Unban", f"{user.mention} (`{user.id}`) by {ctx.author.mention}\n{reason}")

    @commands.hybrid_command(description="Kick a member.")
    @commands.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if err := check_target(ctx, member):
            return await ctx.send(err)
        await dm_action(member, ctx.guild, "You were kicked", reason)
        await member.kick(reason=f"{ctx.author} ({ctx.author.id}): {reason}")
        await ctx.send(f"Kicked **{member}**: {reason}")
        await mod_log(ctx.guild, "Kick", f"{member.mention} (`{member.id}`) by {ctx.author.mention}\n{reason}")

    @commands.hybrid_command(name="timeout", aliases=["mute"], description="Timeout a member (e.g. 10m, 2h, 1d).")
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def timeout(self, ctx, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
        if err := check_target(ctx, member):
            return await ctx.send(err)
        td = parse_duration(duration)
        if td is None:
            return await ctx.send("Duration like `30s`, `10m`, `2h`, `1d` (max 28d).")
        await member.timeout(td, reason=f"{ctx.author} ({ctx.author.id}): {reason}")
        await dm_action(member, ctx.guild, "You were timed out", reason, f"Duration: {duration}", 0xE67E22)
        await ctx.send(f"Timed out **{member}** for {duration}: {reason}")
        await mod_log(ctx.guild, "Timeout", f"{member.mention} for {duration} by {ctx.author.mention}\n{reason}")

    @commands.hybrid_command(name="untimeout", aliases=["unmute"], description="Remove a member's timeout.")
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def untimeout(self, ctx, member: discord.Member):
        await member.timeout(None, reason=f"{ctx.author} ({ctx.author.id})")
        await dm_action(member, ctx.guild, "Your timeout was removed", color=0x2ECC71)
        await ctx.send(f"Removed timeout from **{member}**.")
        await mod_log(ctx.guild, "Untimeout", f"{member.mention} by {ctx.author.mention}")

    @commands.hybrid_command(description="Warn a member.")
    @commands.has_permissions(moderate_members=True)
    async def warn(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if err := check_target(ctx, member):
            return await ctx.send(err)
        cfg = STORE.get(ctx.guild.id)
        w = cfg["warnings"].setdefault(str(member.id), [])
        w.append({"reason": reason, "mod": ctx.author.id, "ts": int(time.time())})
        STORE.save()
        await dm_action(member, ctx.guild, "You were warned", reason, f"Warning #{len(w)}", 0xF1C40F)
        await ctx.send(f"Warned {member.mention} (warning #{len(w)}): {reason}")
        await mod_log(ctx.guild, "Warn", f"{member.mention} (#{len(w)}) by {ctx.author.mention}\n{reason}")

    @commands.hybrid_command(description="Show a member's warnings.")
    @commands.has_permissions(moderate_members=True)
    async def warnings(self, ctx, member: discord.Member):
        w = STORE.get(ctx.guild.id)["warnings"].get(str(member.id), [])
        if not w:
            return await ctx.send(f"{member.mention} has no warnings.")
        lines = [
            f"**{i}.** {x['reason']} — <@{x['mod']}> <t:{x['ts']}:R>"
            for i, x in enumerate(w[-10:], start=max(1, len(w) - 9))
        ]
        e = discord.Embed(title=f"Warnings for {member}", description="\n".join(lines), color=0xF1C40F)
        e.set_footer(text=f"{len(w)} total")
        await ctx.send(embed=e)

    @commands.hybrid_command(description="Clear all warnings for a member.")
    @commands.has_permissions(moderate_members=True)
    async def clearwarns(self, ctx, member: discord.Member):
        cfg = STORE.get(ctx.guild.id)
        cfg["warnings"].pop(str(member.id), None)
        STORE.save()
        await ctx.send(f"Cleared warnings for {member.mention}.")

    @commands.hybrid_command(description="Delete recent messages (1-100).")
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge(self, ctx, amount: commands.Range[int, 1, 100]):
        if ctx.interaction:
            await ctx.defer(ephemeral=True)
            await ctx.channel.purge(limit=amount)
        else:
            await ctx.channel.purge(limit=amount + 1)  # +1 for the command message
        await ctx.send(f"Deleted {amount} messages.", delete_after=5)

    @commands.hybrid_command(description="Set channel slowmode in seconds (0 to disable).")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def slowmode(self, ctx, seconds: commands.Range[int, 0, 21600]):
        await ctx.channel.edit(slowmode_delay=seconds, reason=f"{ctx.author} ({ctx.author.id})")
        await ctx.send(f"Slowmode set to {seconds}s." if seconds else "Slowmode disabled.")

    @commands.hybrid_command(description="Lock this channel for @everyone.")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def lock(self, ctx):
        ow = ctx.channel.overwrites_for(ctx.guild.default_role)
        ow.send_messages = False
        await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=ow, reason=f"{ctx.author} ({ctx.author.id})")
        await ctx.send("Channel locked.")

    @commands.hybrid_command(description="Unlock this channel for @everyone.")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def unlock(self, ctx):
        ow = ctx.channel.overwrites_for(ctx.guild.default_role)
        ow.send_messages = None
        await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=ow, reason=f"{ctx.author} ({ctx.author.id})")
        await ctx.send("Channel unlocked.")


# ---------- automod ----------
    # ---------- roles ----------

    @staticmethod
    def role_problem(ctx, role):
        if role.is_default():
            return "You can't use @everyone."
        if role.managed:
            return "That role is managed by an integration and can't be assigned."
        if role >= ctx.guild.me.top_role:
            return "That role is higher than or equal to my top role."
        if ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
            return "That role is higher than or equal to your top role."
        return None

    @commands.hybrid_command(name="roleadd", aliases=["addrole"], description="Give a role to a member.")
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def roleadd(self, ctx, member: discord.Member, role: discord.Role):
        problem = self.role_problem(ctx, role)
        if problem:
            return await ctx.send(problem)
        quiet = discord.AllowedMentions.none()
        if role in member.roles:
            return await ctx.send(f"{member.mention} already has {role.mention}.", allowed_mentions=quiet)
        await member.add_roles(role, reason=f"{ctx.author} ({ctx.author.id})")
        await ctx.send(f"Added {role.mention} to {member.mention}.", allowed_mentions=quiet)
        await mod_log(ctx.guild, "Role added", f"{ctx.author.mention} gave {role.mention} to {member.mention}")

    @commands.hybrid_command(name="roleremove", aliases=["removerole"], description="Remove a role from a member.")
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def roleremove(self, ctx, member: discord.Member, role: discord.Role):
        problem = self.role_problem(ctx, role)
        if problem:
            return await ctx.send(problem)
        quiet = discord.AllowedMentions.none()
        if role not in member.roles:
            return await ctx.send(f"{member.mention} doesn't have {role.mention}.", allowed_mentions=quiet)
        await member.remove_roles(role, reason=f"{ctx.author} ({ctx.author.id})")
        await ctx.send(f"Removed {role.mention} from {member.mention}.", allowed_mentions=quiet)
        await mod_log(ctx.guild, "Role removed", f"{ctx.author.mention} removed {role.mention} from {member.mention}")


class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.msgs = defaultdict(deque)

    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False
        p = ctx.author.guild_permissions
        if p.manage_guild or p.administrator or ctx.author.id == ctx.guild.owner_id:
            return True
        raise commands.MissingPermissions(["manage_guild"])

    # ---------- detection ----------

    def check(self, message, am):
        text = message.content
        if am["antiinvite"] and INVITE_RE.search(text):
            return "invite link"
        if am["antilink"] and LINK_RE.search(text):
            return "link"
        low = text.lower()
        for w in am["badwords"]:
            if re.search(rf"\b{re.escape(w)}\b", low):
                return "blocked word"
        if am["antimention"] and len(message.mentions) >= am["mention_limit"]:
            return "mass mention"
        if am["antispam"]:
            now = time.monotonic()
            q = self.msgs[(message.guild.id, message.author.id)]
            q.append(now)
            while q and now - q[0] > am["spam_seconds"]:
                q.popleft()
            if len(q) > am["spam_count"]:
                return "spam"
        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        cfg = STORE.get(message.guild.id)
        am = cfg["automod"]
        if not am["enabled"]:
            return
        p = member.guild_permissions
        if (
            p.administrator or p.manage_messages
            or member.id == message.guild.owner_id or member.id in cfg["whitelist"]
        ):
            return
        reason = self.check(message, am)
        if not reason:
            return

        try:
            await message.delete()
        except discord.HTTPException:
            pass

        spam = reason == "spam"
        action = am["spam_action"] if spam else am["action"]
        minutes = am["spam_timeout_minutes"] if spam else am["timeout_minutes"]
        note = "message deleted"
        if spam and action != "delete":
            self.msgs[(message.guild.id, member.id)].clear()
        if action == "warn":
            w = cfg["warnings"].setdefault(str(member.id), [])
            w.append({"reason": f"AutoMod: {reason}", "mod": self.bot.user.id, "ts": int(time.time())})
            STORE.save()
            note = f"warned (#{len(w)})"
            await dm_action(member, message.guild, "You were warned by AutoMod", f"AutoMod: {reason}", f"Warning #{len(w)}", 0xF1C40F)
        elif action == "timeout":
            try:
                await member.timeout(timedelta(minutes=minutes), reason=f"AutoMod: {reason}")
                note = f"timed out {minutes}m"
                await dm_action(member, message.guild, "You were timed out by AutoMod", f"AutoMod: {reason}", f"Duration: {minutes} minutes", 0xE67E22)
            except discord.HTTPException:
                note = "timeout failed"
        elif action == "kick":
            try:
                await dm_action(member, message.guild, "You were kicked by AutoMod", f"AutoMod: {reason}")
                await member.kick(reason=f"AutoMod: {reason}")
                note = "kicked"
            except discord.HTTPException:
                note = "kick failed"
        elif action == "ban":
            try:
                await dm_action(member, message.guild, "You were banned by AutoMod", f"AutoMod: {reason}")
                await member.ban(reason=f"AutoMod: {reason}", delete_message_seconds=0)
                note = "banned"
            except discord.HTTPException:
                note = "ban failed"

        try:
            await message.channel.send(f"{member.mention} your message was removed ({reason}).", delete_after=5)
        except discord.HTTPException:
            pass
        await mod_log(
            message.guild, "AutoMod",
            f"{member.mention} (`{member.id}`) in {message.channel.mention}\nReason: {reason}\nAction: {note}",
        )

    # ---------- commands ----------

    @commands.hybrid_group(name="automod", fallback="status", invoke_without_command=True)
    async def automod(self, ctx):
        """Show automod settings."""
        am = STORE.get(ctx.guild.id)["automod"]
        flag = lambda v: "on" if v else "off"
        action = am["action"] + (f" ({am['timeout_minutes']}m)" if am["action"] == "timeout" else "")
        e = discord.Embed(title="AutoMod", color=0x3498DB)
        e.add_field(name="Status", value=flag(am["enabled"]))
        e.add_field(name="Action", value=action)
        e.add_field(name="Blocked words", value=str(len(am["badwords"])))
        e.add_field(
            name="Filters",
            value=(
                f"`antiinvite`: {flag(am['antiinvite'])}\n"
                f"`antilink`: {flag(am['antilink'])}\n"
                f"`antispam`: {flag(am['antispam'])} ({am['spam_count']} msgs / {am['spam_seconds']}s)\n"
                f"`antimention`: {flag(am['antimention'])} ({am['mention_limit']}+ mentions)"
            ),
            inline=False,
        )
        await ctx.send(embed=e)

    @automod.command(name="on")
    async def am_on(self, ctx):
        """Enable automod."""
        STORE.get(ctx.guild.id)["automod"]["enabled"] = True
        STORE.save()
        await ctx.send("AutoMod enabled.")

    @automod.command(name="off")
    async def am_off(self, ctx):
        """Disable automod."""
        STORE.get(ctx.guild.id)["automod"]["enabled"] = False
        STORE.save()
        await ctx.send("AutoMod disabled.")

    @automod.command(name="toggle")
    async def am_toggle(self, ctx, feature: Literal["antiinvite", "antilink", "antispam", "antimention"]):
        """Toggle a filter on/off."""
        am = STORE.get(ctx.guild.id)["automod"]
        am[feature] = not am[feature]
        STORE.save()
        await ctx.send(f"`{feature}` is now {'on' if am[feature] else 'off'}.")

    @automod.command(name="action")
    async def am_action(self, ctx, action: Literal["delete", "warn", "timeout"]):
        """What to do on a violation (message is always deleted)."""
        STORE.get(ctx.guild.id)["automod"]["action"] = action
        STORE.save()
        await ctx.send(f"AutoMod action set to `{action}`.")

    @automod.command(name="timeout")
    async def am_timeout(self, ctx, minutes: commands.Range[int, 1, 10080]):
        """Timeout length in minutes when action is timeout."""
        STORE.get(ctx.guild.id)["automod"]["timeout_minutes"] = minutes
        STORE.save()
        await ctx.send(f"AutoMod timeout set to {minutes}m.")

    @automod.command(name="spam")
    async def am_spam(self, ctx, count: commands.Range[int, 2, 30], seconds: commands.Range[int, 2, 60]):
        """Spam threshold: more than <count> messages in <seconds>."""
        am = STORE.get(ctx.guild.id)["automod"]
        am["spam_count"], am["spam_seconds"] = count, seconds
        STORE.save()
        await ctx.send(f"Spam: more than {count} messages in {seconds}s.")

    @automod.command(name="mentions")
    async def am_mentions(self, ctx, limit: commands.Range[int, 2, 30]):
        """Max user mentions allowed per message."""
        STORE.get(ctx.guild.id)["automod"]["mention_limit"] = limit
        STORE.save()
        await ctx.send(f"Messages with {limit}+ mentions will be removed.")

    # ---------- antispam ----------

    @commands.hybrid_group(name="antispam", fallback="status", invoke_without_command=True)
    async def antispam(self, ctx):
        """Show anti-spam settings."""
        am = STORE.get(ctx.guild.id)["automod"]
        act = am["spam_action"] + (f" ({am['spam_timeout_minutes']}m)" if am["spam_action"] == "timeout" else "")
        e = discord.Embed(title="Anti-spam", color=0x3498DB)
        e.add_field(name="Status", value="ON" if am["antispam"] else "OFF")
        e.add_field(name="Trigger", value=f"more than {am['spam_count']} msgs in {am['spam_seconds']}s")
        e.add_field(name="Punishment", value=act)
        if not am["enabled"]:
            e.add_field(name="Note", value=f"AutoMod is off, so anti-spam won't run. Use `{ctx.clean_prefix}automod on`.", inline=False)
        e.set_footer(text=f"{ctx.clean_prefix}antispam on|off|set|punishment")
        await ctx.send(embed=e)

    @antispam.command(name="on")
    async def as_on(self, ctx):
        """Enable anti-spam."""
        am = STORE.get(ctx.guild.id)["automod"]
        am["antispam"] = True
        STORE.save()
        extra = "" if am["enabled"] else f" AutoMod is off though, run `{ctx.clean_prefix}automod on`."
        await ctx.send("Anti-spam enabled." + extra)

    @antispam.command(name="off")
    async def as_off(self, ctx):
        """Disable anti-spam."""
        STORE.get(ctx.guild.id)["automod"]["antispam"] = False
        STORE.save()
        await ctx.send("Anti-spam disabled.")

    @antispam.command(name="set")
    async def as_set(self, ctx, messages: commands.Range[int, 2, 50], seconds: commands.Range[int, 1, 120]):
        """Set the limit: more than <messages> in <seconds> triggers the punishment."""
        am = STORE.get(ctx.guild.id)["automod"]
        am["spam_count"], am["spam_seconds"] = messages, seconds
        STORE.save()
        await ctx.send(f"Anti-spam: more than {messages} messages in {seconds}s will be punished.")

    @antispam.command(name="punishment")
    async def as_punishment(
        self, ctx,
        action: Literal["delete", "warn", "timeout", "kick", "ban"],
        minutes: commands.Range[int, 1, 10080] = None,
    ):
        """Set the punishment. Minutes only applies to timeout."""
        am = STORE.get(ctx.guild.id)["automod"]
        am["spam_action"] = action
        if minutes is not None:
            am["spam_timeout_minutes"] = minutes
        STORE.save()
        extra = f" for {am['spam_timeout_minutes']}m" if action == "timeout" else ""
        await ctx.send(f"Spam punishment set to `{action}`{extra}.")

    @automod.group(name="badword", aliases=["bw"], fallback="list", invoke_without_command=True)
    async def am_badword(self, ctx):
        """List blocked words."""
        words = STORE.get(ctx.guild.id)["automod"]["badwords"]
        await ctx.send(", ".join(f"`{w}`" for w in words) or "No blocked words.", ephemeral=True)

    @am_badword.command(name="add")
    async def am_bw_add(self, ctx, *, word: str):
        """Add a blocked word or phrase."""
        am = STORE.get(ctx.guild.id)["automod"]
        word = word.lower().strip()
        if word and word not in am["badwords"]:
            am["badwords"].append(word)
            STORE.save()
        if ctx.interaction is None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
        await ctx.send("Word added.", ephemeral=True, delete_after=5)

    @am_badword.command(name="remove", aliases=["rm"])
    async def am_bw_remove(self, ctx, *, word: str):
        """Remove a blocked word or phrase."""
        am = STORE.get(ctx.guild.id)["automod"]
        word = word.lower().strip()
        if word in am["badwords"]:
            am["badwords"].remove(word)
            STORE.save()
        if ctx.interaction is None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
        await ctx.send("Word removed.", ephemeral=True, delete_after=5)


# ---------- anti-raid ----------

class AntiRaid(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.joins = defaultdict(deque)  # guild_id -> deque[(monotonic, member)]

    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False
        p = ctx.author.guild_permissions
        if p.manage_guild or p.administrator or ctx.author.id == ctx.guild.owner_id:
            return True
        raise commands.MissingPermissions(["manage_guild"])

    @staticmethod
    def active(raid):
        return time.time() < raid["active_until"]

    async def punish(self, member, raid, reason):
        try:
            if raid["action"] == "ban":
                await member.guild.ban(member, reason=reason, delete_message_seconds=0)
            elif raid["action"] == "kick":
                await member.kick(reason=reason)
            else:
                await member.timeout(timedelta(minutes=raid["duration_minutes"]), reason=reason)
            return True
        except discord.HTTPException:
            return False

    def activate(self, guild, raid, minutes, why):
        """Turn raid mode on instantly (no awaits), then log/schedule the end in the background."""
        until = time.time() + minutes * 60
        raid["active_until"] = until
        STORE.save()
        asyncio.create_task(mod_log(guild, "Raid mode ON", f"{why}\nNew joins will be `{raid['action']}`ed for {minutes}m."))
        asyncio.create_task(self.end_later(guild, until))

    async def end_later(self, guild, until):
        await asyncio.sleep(max(0, until - time.time()) + 1)
        raid = STORE.get(guild.id)["raid"]
        if raid["active_until"] == until:
            raid["active_until"] = 0
            STORE.save()
            await mod_log(guild, "Raid mode OFF", "Raid mode ended.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        guild = member.guild
        cfg = STORE.get(guild.id)
        raid = cfg["raid"]
        if member.id in cfg["whitelist"]:
            return

        reason = None
        if self.active(raid):
            reason = "raid mode active"
        elif raid["min_account_age_days"] and (discord.utils.utcnow() - member.created_at).days < raid["min_account_age_days"]:
            reason = "account too new"
        elif raid["auto"]:
            now = time.monotonic()
            q = self.joins[guild.id]
            q.append((now, member))
            while q and now - q[0][0] > raid["join_seconds"]:
                q.popleft()
            if len(q) > raid["join_count"]:
                members = [m for _, m in q]
                q.clear()
                self.activate(
                    guild, raid, raid["duration_minutes"],
                    f"Join flood: {len(members)} joins in {raid['join_seconds']}s.",
                )
                await asyncio.gather(*(self.punish(m, raid, "Anti-raid: join flood") for m in members))
                return

        if reason:
            await self.punish(member, raid, f"Anti-raid: {reason}")

    # ---------- commands ----------

    @commands.hybrid_group(name="raid", fallback="status", invoke_without_command=True)
    async def raid(self, ctx):
        """Show anti-raid settings."""
        r = STORE.get(ctx.guild.id)["raid"]
        active = self.active(r)
        e = discord.Embed(title="Anti-raid", color=0xE67E22)
        e.add_field(name="Raid mode", value=f"ACTIVE until <t:{int(r['active_until'])}:R>" if active else "off")
        e.add_field(name="Auto-detect", value="on" if r["auto"] else "off")
        e.add_field(name="Action", value=r["action"])
        e.add_field(name="Trigger", value=f"more than {r['join_count']} joins in {r['join_seconds']}s")
        e.add_field(name="Duration", value=f"{r['duration_minutes']}m")
        e.add_field(name="Min account age", value=f"{r['min_account_age_days']}d" if r["min_account_age_days"] else "off")
        await ctx.send(embed=e)

    @raid.command(name="on")
    async def raid_on(self, ctx):
        """Manually start raid mode."""
        r = STORE.get(ctx.guild.id)["raid"]
        self.activate(ctx.guild, r, r["duration_minutes"], f"Started by {ctx.author.mention}.")
        await ctx.send(f"Raid mode ON for {r['duration_minutes']}m. New joins will be {r['action']}ed.")

    @raid.command(name="off")
    async def raid_off(self, ctx):
        """Stop raid mode."""
        r = STORE.get(ctx.guild.id)["raid"]
        r["active_until"] = 0
        STORE.save()
        await ctx.send("Raid mode OFF.")

    @raid.command(name="auto")
    async def raid_auto(self, ctx, state: Literal["on", "off"]):
        """Turn automatic join-flood detection on or off."""
        STORE.get(ctx.guild.id)["raid"]["auto"] = state == "on"
        STORE.save()
        await ctx.send(f"Auto-detect {state}.")

    @raid.command(name="action")
    async def raid_action(self, ctx, action: Literal["kick", "ban", "timeout"]):
        """What to do with joiners during a raid."""
        STORE.get(ctx.guild.id)["raid"]["action"] = action
        STORE.save()
        await ctx.send(f"Raid action set to `{action}`.")

    @raid.command(name="threshold")
    async def raid_threshold(self, ctx, joins: commands.Range[int, 2, 50], seconds: commands.Range[int, 2, 120]):
        """Trigger raid mode on more than <joins> joins within <seconds>."""
        r = STORE.get(ctx.guild.id)["raid"]
        r["join_count"], r["join_seconds"] = joins, seconds
        STORE.save()
        await ctx.send(f"Raid triggers on more than {joins} joins in {seconds}s.")

    @raid.command(name="duration")
    async def raid_duration(self, ctx, minutes: commands.Range[int, 1, 1440]):
        """How long raid mode lasts."""
        STORE.get(ctx.guild.id)["raid"]["duration_minutes"] = minutes
        STORE.save()
        await ctx.send(f"Raid mode lasts {minutes}m.")

    @raid.command(name="accountage")
    async def raid_accountage(self, ctx, days: commands.Range[int, 0, 365]):
        """Remove joiners whose account is younger than <days> (0 = off)."""
        STORE.get(ctx.guild.id)["raid"]["min_account_age_days"] = days
        STORE.save()
        await ctx.send(f"Min account age: {days}d." if days else "Account age filter off.")


intents = discord.Intents.default()  # includes `moderation`, needed for audit log events
intents.members = True
intents.message_content = True


# ---------------- activity logging ----------------

LOG_CATEGORIES = ("messages", "members", "voice", "server", "moderation", "commands", "reactions")
GREEN, RED, BLUE, ORANGE, GRAY = 0x2ECC71, 0xE74C3C, 0x3498DB, 0xE67E22, 0x95A5A6

_A = discord.AuditLogAction
AUDIT_CATEGORY = {
    getattr(_A, n): c
    for n, c in [
        ("ban", "moderation"), ("unban", "moderation"), ("kick", "moderation"), ("member_prune", "moderation"),
        ("member_update", "members"), ("member_role_update", "members"),
        ("member_move", "members"), ("member_disconnect", "members"),
        ("message_delete", "messages"), ("message_bulk_delete", "messages"),
        ("message_pin", "messages"), ("message_unpin", "messages"),
    ]
    if hasattr(_A, n)
}  # every other audit action goes to "server"


def clip(text, n):
    text = str(text)
    return text if len(text) <= n else text[: n - 3] + "..."


def emb(title, desc=None, color=GRAY, user=None):
    e = discord.Embed(title=title, description=desc, color=color, timestamp=discord.utils.utcnow())
    if user is not None:
        e.set_author(name=f"{user} ({user.id})", icon_url=user.display_avatar.url)
    return e


def fmt_value(v):
    if v is None:
        return "none"
    if isinstance(v, (list, tuple, set)):
        return clip(", ".join(fmt_value(x) for x in list(v)[:6]), 200) or "none"
    if isinstance(v, discord.Object):
        return f"<@&{v.id}>" if v.type is discord.Role else f"`{v.id}`"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, discord.Permissions):
        return f"`{v.value}`"
    mention = getattr(v, "mention", None)
    return mention if mention else clip(v, 80)


def describe_target(t):
    if t is None:
        return "unknown"
    mention = getattr(t, "mention", None)
    ident = getattr(t, "id", None) or getattr(t, "code", None) or ""
    name = getattr(t, "name", None)
    return clip(f"{mention or name or t} (`{ident}`)" if ident else (mention or name or t), 300)


class ActivityLog(commands.Cog):
    """Logs user and server activity into per-category channels. Bot owner only to configure."""

    def __init__(self, bot):
        self.bot = bot
        self.pending = defaultdict(list)  # channel id -> embeds waiting to be sent
        self.seen_cmds = deque(maxlen=300)
        self.flush.start()

    def cog_unload(self):
        self.flush.cancel()

    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False
        if not await self.bot.is_owner(ctx.author):
            raise commands.NotOwner()
        return True

    # ---------- sending (batched to respect rate limits) ----------

    def lcfg(self, guild):
        return STORE.get(guild.id)["logs"]

    def chan(self, guild, category):
        return self.lcfg(guild)["channels"].get(category)

    def skip_channel(self, guild, channel_id):
        lc = self.lcfg(guild)
        return channel_id in lc["ignore_channels"] or channel_id in lc["channels"].values()

    def skip_bot(self, guild, user):
        return user.id == self.bot.user.id or (user.bot and self.lcfg(guild)["ignore_bots"])

    def push(self, guild, category, embed):
        cid = self.chan(guild, category)
        if not cid:
            return
        q = self.pending[cid]
        if len(q) >= 200:
            q.pop(0)  # drop the oldest if a channel is flooded
        q.append(embed)

    @tasks.loop(seconds=2)
    async def flush(self):
        try:
            for cid in list(self.pending):
                q = self.pending[cid]
                ch = self.bot.get_channel(cid)
                if not q or ch is None:
                    self.pending.pop(cid, None)
                    continue
                batch, size = [], 0
                while q and len(batch) < 10 and size + len(q[0]) <= 5500:
                    e = q.pop(0)
                    batch.append(e)
                    size += len(e)
                if not batch:
                    q.pop(0)  # single embed too large, drop it
                    continue
                try:
                    await ch.send(embeds=batch)
                except (discord.Forbidden, discord.NotFound):
                    self.pending.pop(cid, None)
                except discord.HTTPException:
                    pass
        except Exception as exc:
            print(f"[logs] flush error: {exc!r}")

    @flush.before_loop
    async def before_flush(self):
        await self.bot.wait_until_ready()

    # ---------- messages ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        g = message.guild
        if g is None or not self.chan(g, "messages"):
            return
        if self.skip_bot(g, message.author) or self.skip_channel(g, message.channel.id):
            return
        text = clip(message.content, 1500) or "*no text*"
        if message.attachments:
            text += "\n" + "\n".join(a.url for a in message.attachments[:5])
        self.push(g, "messages", emb(
            "Message sent",
            f"{message.author.mention} in {message.channel.mention} [jump]({message.jump_url})\n{text}",
            GRAY, message.author,
        ))

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        g = after.guild
        if g is None or not self.chan(g, "messages") or before.content == after.content:
            return
        if self.skip_bot(g, after.author) or self.skip_channel(g, after.channel.id):
            return
        e = emb(
            "Message edited",
            f"{after.author.mention} in {after.channel.mention} [jump]({after.jump_url})",
            BLUE, after.author,
        )
        e.add_field(name="Before", value=clip(before.content, 900) or "*empty*", inline=False)
        e.add_field(name="After", value=clip(after.content, 900) or "*empty*", inline=False)
        self.push(g, "messages", e)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        g = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if g is None or not self.chan(g, "messages") or self.skip_channel(g, payload.channel_id):
            return
        msg = payload.cached_message
        if msg is None:
            self.push(g, "messages", emb(
                "Message deleted", f"Message `{payload.message_id}` in <#{payload.channel_id}> (not cached, content unknown)", RED))
            return
        if self.skip_bot(g, msg.author):
            return
        text = clip(msg.content, 1500) or "*no text*"
        if msg.attachments:
            text += "\n" + "\n".join(a.url for a in msg.attachments[:5])
        self.push(g, "messages", emb(
            "Message deleted", f"{msg.author.mention} in {msg.channel.mention}\n{text}", RED, msg.author))

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        g = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if g is None or not self.chan(g, "messages") or self.skip_channel(g, payload.channel_id):
            return
        self.push(g, "messages", emb(
            "Bulk delete", f"{len(payload.message_ids)} messages deleted in <#{payload.channel_id}>", RED))

    # ---------- members ----------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        g = member.guild
        if not self.chan(g, "members") or self.skip_bot(g, member):
            return
        self.push(g, "members", emb(
            "Member joined",
            f"{member.mention}\nAccount created {discord.utils.format_dt(member.created_at, 'R')}",
            GREEN, member,
        ))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        g = member.guild
        if not self.chan(g, "members") or self.skip_bot(g, member):
            return
        roles = ", ".join(r.mention for r in member.roles[1:]) or "none"
        joined = discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "unknown"
        self.push(g, "members", emb(
            "Member left", f"{member.mention}\nJoined {joined}\nRoles: {clip(roles, 900)}", RED, member))

    # ---------- voice ----------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        g = member.guild
        if not self.chan(g, "voice") or self.skip_bot(g, member):
            return
        if before.channel != after.channel:
            if before.channel is None:
                text, color = f"joined {after.channel.mention}", GREEN
            elif after.channel is None:
                text, color = f"left {before.channel.mention}", RED
            else:
                text, color = f"moved {before.channel.mention} -> {after.channel.mention}", BLUE
        else:
            changes = []
            for attr, label in (
                ("mute", "server muted"), ("deaf", "server deafened"),
                ("self_mute", "self muted"), ("self_deaf", "self deafened"),
                ("self_stream", "streaming"), ("self_video", "camera"),
            ):
                b, a = getattr(before, attr), getattr(after, attr)
                if b != a:
                    changes.append(f"{label}: {'on' if a else 'off'}")
            if not changes:
                return
            text, color = ", ".join(changes) + f" in {after.channel.mention}", GRAY
        self.push(g, "voice", emb("Voice", f"{member.mention} {text}", color, member))

    # ---------- reactions ----------

    def reaction(self, payload, added):
        g = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if g is None or not self.chan(g, "reactions") or payload.user_id == self.bot.user.id:
            return
        if self.skip_channel(g, payload.channel_id):
            return
        member = payload.member if added else g.get_member(payload.user_id)
        if member is not None and member.bot and self.lcfg(g)["ignore_bots"]:
            return
        link = f"https://discord.com/channels/{g.id}/{payload.channel_id}/{payload.message_id}"
        who = member.mention if member else f"<@{payload.user_id}>"
        self.push(g, "reactions", emb(
            "Reaction added" if added else "Reaction removed",
            f"{who} {payload.emoji} in <#{payload.channel_id}> [jump]({link})",
            GREEN if added else RED, member,
        ))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        self.reaction(payload, True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        self.reaction(payload, False)

    # ---------- commands used ----------

    def log_command(self, guild, user, channel, name, args, key):
        if guild is None or not self.chan(guild, "commands") or key in self.seen_cmds:
            return
        self.seen_cmds.append(key)
        self.push(guild, "commands", emb(
            "Command used",
            f"{user.mention} in {channel.mention}\n`{clip(name, 60)}` {clip(args, 400)}".strip(),
            BLUE, user,
        ))

    @staticmethod
    def slash_args(interaction):
        try:
            return " ".join(f"{k}:{v}" for k, v in interaction.namespace)
        except Exception:
            return ""

    @commands.Cog.listener()
    async def on_command_completion(self, ctx):
        inter = ctx.interaction
        key = inter.id if inter else ctx.message.id
        if inter:
            name, args = f"/{ctx.command.qualified_name}", self.slash_args(inter)
        else:
            name = ctx.clean_prefix + ctx.command.qualified_name
            args = ctx.message.content[len(ctx.prefix or "") + len(ctx.invoked_with or ""):].strip()
        self.log_command(ctx.guild, ctx.author, ctx.channel, name, args, key)

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction, command):
        self.log_command(
            interaction.guild, interaction.user, interaction.channel,
            f"/{command.qualified_name}", self.slash_args(interaction), interaction.id,
        )

    # ---------- audit log (who did it) ----------

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        try:
            g = entry.guild
            category = AUDIT_CATEGORY.get(entry.action, "server")
            if not self.chan(g, category):
                return
            user = entry.user
            title = entry.action.name.replace("_", " ").title()
            e = emb(title, color=ORANGE)
            e.add_field(name="By", value=f"{user.mention} (`{user.id}`)" if user else "unknown", inline=False)
            e.add_field(name="Target", value=describe_target(entry.target), inline=False)
            if entry.reason:
                e.add_field(name="Reason", value=clip(entry.reason, 500), inline=False)

            extra = entry.extra
            if hasattr(extra, "channel") and hasattr(extra, "count"):
                e.add_field(name="Details", value=f"{extra.count} message(s) in {getattr(extra.channel, 'mention', extra.channel)}", inline=False)

            if entry.action == discord.AuditLogAction.member_role_update:
                added = fmt_value(getattr(entry.after, "roles", None) or [])
                removed = fmt_value(getattr(entry.before, "roles", None) or [])
                e.add_field(name="Roles", value=f"Added: {added}\nRemoved: {removed}", inline=False)
            else:
                before, after = dict(iter(entry.before)), dict(iter(entry.after))
                lines = [
                    f"`{k}`: {fmt_value(before.get(k))} -> {fmt_value(after.get(k))}"
                    for k in list(dict.fromkeys([*before, *after]))[:8]
                ]
                if lines:
                    e.add_field(name="Changes", value=clip("\n".join(lines), 1000), inline=False)
            self.push(g, category, e)
        except Exception as exc:
            print(f"[logs] audit entry error: {exc!r}")

    # ---------- setup commands (bot owner only) ----------

    @commands.hybrid_group(name="logs", fallback="status", invoke_without_command=True)
    async def logs(self, ctx):
        """Show where each type of log is sent."""
        lc = self.lcfg(ctx.guild)
        lines = [f"`{c}`: " + (f"<#{lc['channels'][c]}>" if c in lc["channels"] else "off") for c in LOG_CATEGORIES]
        e = discord.Embed(title="Activity logs", description="\n".join(lines), color=BLUE)
        e.add_field(name="Ignore bots", value="yes" if lc["ignore_bots"] else "no")
        ignored = ", ".join(f"<#{c}>" for c in lc["ignore_channels"]) or "none"
        e.add_field(name="Ignored channels", value=ignored)
        if not ctx.guild.me.guild_permissions.view_audit_log:
            e.add_field(
                name="Warning",
                value="I need View Audit Log to show who did moderation, role and server changes.",
                inline=False,
            )
        e.set_footer(text=f"{ctx.clean_prefix}logs set|disable|bots|ignore")
        await ctx.send(embed=e)

    @logs.command(name="set")
    async def logs_set(
        self, ctx,
        category: Literal["all", "messages", "members", "voice", "server", "moderation", "commands", "reactions"],
        channel: discord.TextChannel,
    ):
        """Send a log category (or all of them) to a channel."""
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await ctx.send(f"I need View Channel, Send Messages and Embed Links in {channel.mention}.")
        lc = self.lcfg(ctx.guild)
        for c in (LOG_CATEGORIES if category == "all" else (category,)):
            lc["channels"][c] = channel.id
        STORE.save()
        await ctx.send(f"`{category}` logs will go to {channel.mention}.")

    @logs.command(name="disable")
    async def logs_disable(
        self, ctx,
        category: Literal["all", "messages", "members", "voice", "server", "moderation", "commands", "reactions"],
    ):
        """Stop logging a category (or all of them)."""
        lc = self.lcfg(ctx.guild)
        for c in (LOG_CATEGORIES if category == "all" else (category,)):
            lc["channels"].pop(c, None)
        STORE.save()
        await ctx.send(f"`{category}` logs disabled.")

    @logs.command(name="bots")
    async def logs_bots(self, ctx, ignore: Literal["on", "off"]):
        """Choose whether actions by bots are ignored."""
        self.lcfg(ctx.guild)["ignore_bots"] = ignore == "on"
        STORE.save()
        await ctx.send(f"Ignoring bots: {ignore}.")

    @logs.command(name="ignore")
    async def logs_ignore(self, ctx, channel: discord.TextChannel):
        """Toggle ignoring a channel for message and reaction logs."""
        lst = self.lcfg(ctx.guild)["ignore_channels"]
        if channel.id in lst:
            lst.remove(channel.id)
            msg = f"No longer ignoring {channel.mention}."
        else:
            lst.append(channel.id)
            msg = f"Now ignoring {channel.mention}."
        STORE.save()
        await ctx.send(msg)


# ---------------- boost rewards ----------------

BOOST_PINK, BOOST_GRAY = 0xF47FFF, 0x95A5A6


def boost_embed(member, started, roles=(), failed=False, test=False):
    g = member.guild
    if started:
        e = discord.Embed(
            title="💎 New Server Boost",
            description=f"{member.mention} just boosted **{g.name}**! Thank you for the support!",
            color=BOOST_PINK, timestamp=discord.utils.utcnow(),
        )
    else:
        e = discord.Embed(
            title="💔 Boost Ended",
            description=f"{member.mention} stopped boosting **{g.name}**.",
            color=BOOST_GRAY, timestamp=discord.utils.utcnow(),
        )
    e.set_author(name=str(member), icon_url=member.display_avatar.url)
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="Server boosts", value=str(g.premium_subscription_count))
    e.add_field(name="Boost level", value=f"Level {g.premium_tier}")
    if roles:
        e.add_field(name="Rewards" if started else "Roles removed", value=", ".join(r.mention for r in roles), inline=False)
    if failed:
        e.add_field(
            name="Note",
            value="I couldn't update the reward roles. Check my permissions and that my role is above them.",
            inline=False,
        )
    if test:
        e.set_footer(text="Test message")
    return e


class BoostRewards(commands.Cog):
    """Boost announcements and reward roles."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False
        if await self.bot.is_owner(ctx.author) or ctx.author.guild_permissions.manage_guild:
            return True
        raise commands.MissingPermissions(["manage_guild"])

    def cfg(self, guild):
        return STORE.get(guild.id)["boost"]

    async def announce(self, guild, embed):
        channel = guild.get_channel(self.cfg(guild)["channel"] or 0)
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    async def handle(self, member, started, left=False):
        g = member.guild
        cfg = self.cfg(g)
        reward_roles = [r for rid in cfg["roles"] if (r := g.get_role(rid))]
        changed, failed = [], False
        if reward_roles and not left:
            try:
                if started:
                    changed = [r for r in reward_roles if r not in member.roles]
                    if changed:
                        await member.add_roles(*changed, reason="Server boost reward")
                else:
                    changed = [r for r in reward_roles if r in member.roles]
                    if changed:
                        await member.remove_roles(*changed, reason="Server boost ended")
            except discord.HTTPException:
                failed = True
        await self.announce(g, boost_embed(member, started, changed, failed))

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.premium_since is None and after.premium_since is not None:
            await self.handle(after, True)
        elif before.premium_since is not None and after.premium_since is None:
            await self.handle(after, False)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        if member.premium_since is not None:
            await self.handle(member, False, left=True)

    # ---------- setup commands (needs Manage Server) ----------

    @commands.hybrid_group(name="boost", fallback="status", invoke_without_command=True)
    async def boost(self, ctx):
        """Show boost message and reward role settings."""
        cfg = self.cfg(ctx.guild)
        ch = f"<#{cfg['channel']}>" if cfg["channel"] else "not set"
        roles = ", ".join(f"<@&{r}>" for r in cfg["roles"]) or "none"
        e = discord.Embed(title="Boost settings", color=BOOST_PINK)
        e.add_field(name="Boosters channel", value=ch)
        e.add_field(name="Reward roles", value=roles, inline=False)
        e.add_field(name="Server boosts", value=str(ctx.guild.premium_subscription_count))
        e.set_footer(text=f"{ctx.clean_prefix}boost channel|disable|addrole|removerole|test")
        await ctx.send(embed=e)

    @boost.command(name="channel")
    async def boost_channel(self, ctx, channel: discord.TextChannel):
        """Set the boosters channel for boost messages."""
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await ctx.send(f"I need View Channel, Send Messages and Embed Links in {channel.mention}.")
        self.cfg(ctx.guild)["channel"] = channel.id
        STORE.save()
        await ctx.send(f"Boost messages will be sent in {channel.mention}.")

    @boost.command(name="disable")
    async def boost_disable(self, ctx):
        """Stop sending boost messages."""
        self.cfg(ctx.guild)["channel"] = None
        STORE.save()
        await ctx.send("Boost messages disabled. Reward roles still work.")

    @boost.command(name="addrole")
    async def boost_addrole(self, ctx, role: discord.Role):
        """Give this role to members when they boost."""
        quiet = discord.AllowedMentions.none()
        if role.is_default() or role.managed:
            return await ctx.send("That role can't be given manually.")
        if role >= ctx.guild.me.top_role:
            return await ctx.send("That role is above my highest role, so I couldn't assign it.")
        if ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
            return await ctx.send("That role is higher than or equal to your top role.")
        lst = self.cfg(ctx.guild)["roles"]
        if role.id in lst:
            return await ctx.send(f"{role.mention} is already a reward role.", allowed_mentions=quiet)
        if len(lst) >= 10:
            return await ctx.send("You can set at most 10 reward roles.")
        lst.append(role.id)
        STORE.save()
        await ctx.send(f"Boosters will now get {role.mention} and lose it when they stop boosting.", allowed_mentions=quiet)

    @boost.command(name="removerole")
    async def boost_removerole(self, ctx, role: discord.Role):
        """Stop giving this role to boosters."""
        lst = self.cfg(ctx.guild)["roles"]
        if role.id not in lst:
            return await ctx.send(f"{role.mention} isn't a reward role.", allowed_mentions=discord.AllowedMentions.none())
        lst.remove(role.id)
        STORE.save()
        await ctx.send(f"{role.mention} removed from boost rewards.", allowed_mentions=discord.AllowedMentions.none())

    @boost.command(name="test")
    async def boost_test(self, ctx, kind: Literal["start", "stop"] = "start"):
        """Send a sample boost message to the boosters channel."""
        cfg = self.cfg(ctx.guild)
        channel = ctx.guild.get_channel(cfg["channel"] or 0)
        if channel is None:
            return await ctx.send("Set a boosters channel first with the `boost channel` command.")
        roles = [r for rid in cfg["roles"] if (r := ctx.guild.get_role(rid))]
        await self.announce(ctx.guild, boost_embed(ctx.author, kind == "start", roles, test=True))
        await ctx.send(f"Sent a test message to {channel.mention}.")


# ---------------- nuke-bot database: enforcement + commands ----------------

async def owner_user_ids():
    ids = set(EXTRA_OWNERS) | OWNERS.ids
    if ids:
        return ids
    info = await bot.application_info()
    return {m.id for m in info.team.members} if info.team else {info.owner.id}


async def notify_owners(text):
    try:
        for uid in await owner_user_ids():
            user = bot.get_user(uid) or await bot.fetch_user(uid)
            await try_dm(user, text)
    except discord.HTTPException:
        pass


async def ban_db_bot(guild, target, why="known malicious bot"):
    """Ban a listed bot. A server's own antinuke whitelist and the global allowlist always win."""
    bid = getattr(target, "id", target)
    cfg = STORE.get(guild.id)
    if not cfg["enabled"] or bid in cfg["whitelist"] or bid == bot.user.id or NUKEDB.allowed(bid):
        return False
    try:
        await guild.ban(discord.Object(id=bid), reason=f"Anti-nuke DB: {why}", delete_message_seconds=0)
    except discord.HTTPException:
        return False
    entry = NUKEDB.bots.get(str(bid))
    if entry is not None and guild.id not in entry.setdefault("banned_in", []):
        entry["banned_in"].append(guild.id)
        NUKEDB.save()
        NUKEDB._emit("banned", bid)
    ch = guild.get_channel(cfg["log_channel"] or 0)
    if ch:
        try:
            await ch.send(embed=discord.Embed(
                title="Known nuke bot banned",
                description=f"Bot `{bid}` is in the malicious bot database and was banned.",
                color=0xE74C3C,
            ))
        except discord.HTTPException:
            pass
    return True


async def enforce_db_everywhere(bot_id):
    n = 0
    for g in bot.guilds:
        if g.get_member(bot_id) and await ban_db_bot(g, bot_id):
            n += 1
    return n


def sync_on():
    return bool(NUKEDB_SYNC_CHANNEL and NUKEDB_PEER_ID)


async def sync_publish(op, bot_id, entry=None):
    """Tell the other bot about a database change by posting to the sync channel."""
    if not sync_on():
        return
    ch = bot.get_channel(NUKEDB_SYNC_CHANNEL)
    if ch is None:
        return
    payload = {"op": op, "id": int(bot_id)}
    if entry:
        payload["e"] = {k: v for k, v in entry.items() if k not in ("banned_in", "synced")}  # banned_in is per-bot
    text = "NUKEDB1 " + json.dumps(payload, separators=(",", ":"))
    if len(text) > 1900 and "e" in payload:
        payload["e"].pop("actions", None)
        payload["e"]["reason"] = clip(payload["e"].get("reason", ""), 100)
        text = "NUKEDB1 " + json.dumps(payload, separators=(",", ":"))
    try:
        await ch.send(text[:1990], allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass


def clean_entry(e):
    actions = e.get("actions") if isinstance(e.get("actions"), dict) else {}
    return {
        "name": clip(e.get("name", "unknown"), 60),
        "source": clip(e.get("source", "synced"), 20),
        "verified": bool(e.get("verified", False)),
        "guild_id": e.get("guild_id") if isinstance(e.get("guild_id"), int) else None,
        "guild_name": clip(e.get("guild_name", ""), 60),
        "actions": {clip(k, 20): v for k, v in actions.items() if isinstance(v, int)},
        "reason": clip(e.get("reason", ""), 200),
        "flagged_at": e.get("flagged_at") if isinstance(e.get("flagged_at"), int) else int(time.time()),
        "reports": e.get("reports") if isinstance(e.get("reports"), int) else 1,
    }


async def apply_sync(text, live):
    """Apply one sync message. live=False is used while replaying history (no bans or unbans)."""
    if not text.startswith("NUKEDB1 "):
        return
    try:
        p = json.loads(text[8:])
        op, bid = p["op"], int(p["id"])
    except (ValueError, KeyError, TypeError):
        return
    if bid <= 0 or bid in (bot.user.id, NUKEDB_PEER_ID):
        return
    if op == "add":
        NUKEDB.merge(bid, clean_entry(p.get("e") if isinstance(p.get("e"), dict) else {}))
        if live:
            await enforce_db_everywhere(bid)
    elif op in ("remove", "release"):
        info = NUKEDB.bots.get(str(bid)) or {}
        NUKEDB.remove(bid)
        if op == "release" and live:
            for gid in info.get("banned_in", []):
                g = bot.get_guild(gid)
                if g is not None:
                    try:
                        await g.unban(discord.Object(id=bid), reason="Released from nuke database")
                    except discord.HTTPException:
                        pass
    elif op == "allow":
        NUKEDB.set_allowed(bid, True)
    elif op == "unallow":
        NUKEDB.set_allowed(bid, False)


class NukeBotDB(commands.Cog):
    """Database of malicious bots. Bot owner only."""

    def __init__(self, bot):
        self.bot = bot
        self.swept = False
        self.poll.start()

    async def cog_load(self):
        await PG.start()

    def cog_unload(self):
        self.poll.cancel()

    @tasks.loop(seconds=15)
    async def poll(self):
        """Pick up changes made by the other bot through the shared database."""
        try:
            if not NUKEDB_DSN:
                return
            if not PG.active:
                await PG.start()
                if not PG.active:
                    return
                for key in list(NUKEDB.bots):
                    await enforce_db_everywhere(int(key))
                return
            new, released = await asyncio.wait_for(PG.pull(), timeout=10)
            PG.fails = 0
            for bid in new:
                await enforce_db_everywhere(bid)
            for bid, guild_ids in released:
                for gid in guild_ids:
                    g = self.bot.get_guild(gid)
                    if g is not None:
                        try:
                            await g.unban(discord.Object(id=bid), reason="Released from nuke database")
                        except discord.HTTPException:
                            pass
        except Exception as exc:
            PG.last_error = f"{type(exc).__name__}: {exc or 'no response'}"[:300]
            PG.fails += 1
            if PG.fails >= 3 and PG.active:
                PG.active = False
                PG.next_try = time.monotonic() + 30
                if PG.pool is not None:
                    asyncio.create_task(PG.pool.close())
                    PG.pool = None
                print("[nukedb] database stopped responding; using the local list and reconnecting later")

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    async def cog_check(self, ctx):
        if not await self.bot.is_owner(ctx.author):
            raise commands.NotOwner()
        return True

    async def sweep(self, guild):
        for m in list(guild.members):
            if m.bot and NUKEDB.blocked(m.id):
                await ban_db_bot(guild, m)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if member.bot and NUKEDB.blocked(member.id):
            await ban_db_bot(member.guild, member)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        await self.sweep(guild)

    @commands.Cog.listener()
    async def on_ready(self):
        if self.swept:
            return
        self.swept = True
        await self.catch_up()
        for g in list(self.bot.guilds):
            await self.sweep(g)

    async def catch_up(self):
        """Replay the sync channel so a bot that was offline catches up, oldest message first."""
        if not sync_on():
            return
        ch = self.bot.get_channel(NUKEDB_SYNC_CHANNEL)
        if ch is None:
            print("[nukedb] sync channel not found; is the bot in that server?")
            return
        msgs = []
        try:
            async for m in ch.history(limit=1000):
                if m.author.id in (self.bot.user.id, NUKEDB_PEER_ID) and m.content.startswith("NUKEDB1 "):
                    msgs.append(m)
        except discord.HTTPException:
            print("[nukedb] couldn't read sync channel history (needs Read Message History)")
            return
        for m in reversed(msgs):
            await apply_sync(m.content, live=False)

    @commands.Cog.listener()
    async def on_message(self, message):
        # Only the other bot's messages in the sync channel count; this is what keeps the sync trusted.
        if sync_on() and message.channel.id == NUKEDB_SYNC_CHANNEL and message.author.id == NUKEDB_PEER_ID:
            await apply_sync(message.content, live=True)

    @staticmethod
    def parse_id(text):
        try:
            return int(text.strip().strip("<@>"))
        except ValueError:
            return None

    @commands.hybrid_group(name="nukedb", fallback="status", invoke_without_command=True)
    async def nukedb(self, ctx):
        """Show the malicious bot database summary."""
        e = discord.Embed(title="Nuke bot database", color=0xE74C3C)
        e.add_field(name="Listed bots", value=str(len(NUKEDB.bots)))
        e.add_field(name="Allowlisted bots", value=str(len(NUKEDB.allow)))
        if PG.active:
            storage = "PostgreSQL (connected)" + (f"\nLast error: {PG.last_error}" if PG.last_error else "")
        elif not NUKEDB_DSN:
            storage = f"sync channel <#{NUKEDB_SYNC_CHANNEL}>" if sync_on() else "local file only (DATABASE_URL not set)"
        else:
            storage = "local file only (not connected)" + (f"\nError: {PG.last_error}" if PG.last_error else "")
        e.add_field(name="Shared storage", value=clip(storage, 1000), inline=False)
        e.add_field(name="Auto-list needs", value=f"Unverified: {NUKEDB_MIN_ACTIONS}+ destructive actions and joined under {NUKEDB_NEW_JOIN_DAYS} days ago\nVerified: {NUKEDB_VERIFIED_MIN_ACTIONS}+ destructive actions", inline=False)
        e.set_footer(text=f"{ctx.clean_prefix}nukedb list|info|add|remove|release|allow|unallow|sync")
        await ctx.send(embed=e)

    @nukedb.command(name="list")
    async def nukedb_list(self, ctx, page: int = 1):
        """List bots in the database, and the allowlisted bots with their names."""
        await ctx.defer()
        items = sorted(NUKEDB.bots.items(), key=lambda kv: kv[1].get("flagged_at", 0), reverse=True)
        per = 15
        pages = max(1, -(-len(items) // per))
        page = min(max(page, 1), pages)
        chunk = items[(page - 1) * per: page * per]
        lines = [
            f"`{bid}` {clip(info.get('name', 'unknown'), 30)} - <t:{info.get('flagged_at', 0)}:R> ({info.get('source', 'auto')})"
            for bid, info in chunk
        ] or ["Empty."]
        e = discord.Embed(title="Listed nuke bots", description="\n".join(lines), color=0xE74C3C)
        allow_lines = []
        for bid in NUKEDB.allow[:25]:
            try:
                user = self.bot.get_user(bid) or await self.bot.fetch_user(bid)
                name = str(user)
            except discord.HTTPException:
                name = "name unavailable"
            allow_lines.append(f"`{bid}` {clip(name, 40)}")
        e.add_field(
            name=f"Allowlisted bots ({len(NUKEDB.allow)})",
            value=clip("\n".join(allow_lines), 1000) or "none",
            inline=False,
        )
        footer = f"Page {page}/{pages}"
        if len(NUKEDB.allow) > 25:
            footer += f" | showing 25 of {len(NUKEDB.allow)} allowlisted bots"
        e.set_footer(text=footer)
        await ctx.send(embed=e)

    @nukedb.command(name="info")
    async def nukedb_info(self, ctx, bot_id: str):
        """Show details about a listed bot."""
        bid = self.parse_id(bot_id)
        info = NUKEDB.bots.get(str(bid)) if bid else None
        if not info:
            extra = " It is on the allowlist." if bid and NUKEDB.allowed(bid) else ""
            return await ctx.send("That bot isn't in the database." + extra)
        e = discord.Embed(title=f"{info.get('name', 'unknown')} ({bid})", color=0xE74C3C)
        e.add_field(name="Listed", value=f"<t:{info.get('flagged_at', 0)}:R>")
        e.add_field(name="Source", value=info.get("source", "auto"))
        e.add_field(name="Reports", value=str(info.get("reports", 1)))
        if info.get("verified"):
            e.add_field(name="Verified bot", value="yes")
        if info.get("guild_name"):
            e.add_field(name="First seen in", value=f"{info['guild_name']} (`{info.get('guild_id')}`)", inline=False)
        if info.get("actions"):
            e.add_field(name="Actions seen", value=", ".join(f"{k}: {v}" for k, v in info["actions"].items()), inline=False)
        if info.get("reason"):
            e.add_field(name="Reason", value=clip(info["reason"], 500), inline=False)
        await ctx.send(embed=e)

    @nukedb.command(name="add")
    async def nukedb_add(self, ctx, bot_id: str, *, reason: str = "Added manually"):
        """Manually list a bot. It is banned in every server it is in."""
        bid = self.parse_id(bot_id)
        if bid is None:
            return await ctx.send("Give me a bot ID.")
        try:
            user = await self.bot.fetch_user(bid)
        except discord.HTTPException:
            return await ctx.send("I couldn't find that account.")
        if not user.bot:
            return await ctx.send("That's not a bot account, so I won't list it.")
        if user.id == self.bot.user.id:
            return await ctx.send("I can't list myself.")
        flags = getattr(user, "public_flags", None)
        NUKEDB.add(bid, name=str(user), source="manual", verified=bool(flags and flags.verified_bot), reason=reason)
        n = await enforce_db_everywhere(bid)
        await sync_publish("add", bid, NUKEDB.bots.get(str(bid)))
        await ctx.send(f"Listed **{user}** (`{bid}`). Banned in {n} server(s) it was in.")

    @nukedb.command(name="remove")
    async def nukedb_remove(self, ctx, bot_id: str):
        """Remove a bot from the database (does not unban it)."""
        bid = self.parse_id(bot_id)
        if bid is None or not NUKEDB.remove(bid):
            return await ctx.send("That bot isn't in the database.")
        await sync_publish("remove", bid)
        await ctx.send(f"Removed `{bid}`. It stays banned in servers that already banned it; unban it there if needed.")

    @nukedb.command(name="release")
    async def nukedb_release(self, ctx, bot_id: str):
        """Remove a bot from the database AND unban it in every server the database banned it in."""
        bid = self.parse_id(bot_id)
        info = NUKEDB.bots.get(str(bid)) if bid else None
        if not info:
            return await ctx.send("That bot isn't in the database.")
        n = 0
        for gid in info.get("banned_in", []):
            g = self.bot.get_guild(gid)
            if g is None:
                continue
            try:
                await g.unban(discord.Object(id=bid), reason="Released from nuke database")
                n += 1
            except discord.HTTPException:
                pass
        NUKEDB.remove(bid, "release")
        await sync_publish("release", bid)
        await ctx.send(f"Released `{bid}`. Unbanned in {n} server(s).")

    @nukedb.command(name="sync")
    async def nukedb_sync(self, ctx):
        """Push this bot's whole database to the sync channel so the other bot gets it."""
        if PG.active:
            for key in list(NUKEDB.bots):
                NUKEDB._emit("upsert", int(key))
            for bid in list(NUKEDB.allow):
                NUKEDB._emit("allow", bid)
            return await ctx.send(f"Pushed {len(NUKEDB.bots)} listed bot(s) and {len(NUKEDB.allow)} allowlisted bot(s) to the shared database.")
        if not sync_on():
            return await ctx.send("Shared storage isn't set up. Set DATABASE_URL (or NUKEDB_SYNC_CHANNEL and NUKEDB_PEER_ID).")
        await ctx.defer()
        for bid, entry in list(NUKEDB.bots.items()):
            await sync_publish("add", int(bid), entry)
            await asyncio.sleep(1)
        for bid in list(NUKEDB.allow):
            await sync_publish("allow", bid)
            await asyncio.sleep(1)
        await ctx.send(f"Pushed {len(NUKEDB.bots)} listed bot(s) and {len(NUKEDB.allow)} allowlisted bot(s) to the sync channel.")

    @nukedb.command(name="allow")
    async def nukedb_allow(self, ctx, bot_id: str):
        """Protect a bot: it can never be auto-listed or banned by the database."""
        bid = self.parse_id(bot_id)
        if bid is None:
            return await ctx.send("Give me a bot ID.")
        NUKEDB.set_allowed(bid, True)
        await sync_publish("allow", bid)
        await ctx.send(f"`{bid}` is allowlisted (and removed from the database if it was listed).")

    @nukedb.command(name="unallow")
    async def nukedb_unallow(self, ctx, bot_id: str):
        """Remove a bot from the allowlist."""
        bid = self.parse_id(bot_id)
        if bid is None or not NUKEDB.set_allowed(bid, False):
            return await ctx.send("That bot isn't allowlisted.")
        await sync_publish("unallow", bid)
        await ctx.send(f"`{bid}` removed from the allowlist.")


# ---------------- extra bot owners ----------------

class BotOwners(commands.Cog):
    """Manage extra bot owners. Only the main owner can use this, extra owners can't add more."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        # Use the base check so extra owners don't pass this one
        if not await commands.Bot.is_owner(self.bot, ctx.author):
            raise commands.NotOwner()
        return True

    @commands.hybrid_group(name="owners", fallback="list", invoke_without_command=True)
    async def owners(self, ctx):
        """List the main owner and extra owners."""
        main = []
        if EXTRA_OWNERS:
            main = sorted(EXTRA_OWNERS)
        extra = sorted(OWNERS.ids)
        e = discord.Embed(title="Bot owners", color=0x5865F2)
        e.add_field(name="Main owner(s)", value="\n".join(f"<@{i}>" for i in main) or "Bot application owner/team", inline=False)
        e.add_field(name="Extra owners", value="\n".join(f"<@{i}> (`{i}`)" for i in extra) or "none", inline=False)
        e.set_footer(text="Extra owners have full bot-owner commands but can't manage owners.")
        await ctx.send(embed=e, allowed_mentions=discord.AllowedMentions.none())

    @owners.command(name="add")
    async def owners_add(self, ctx, user: discord.User):
        """Give a user full bot-owner access."""
        quiet = discord.AllowedMentions.none()
        if user.bot:
            return await ctx.send("Bots can't be bot owners.", allowed_mentions=quiet)
        if await commands.Bot.is_owner(self.bot, user):
            return await ctx.send(f"{user.mention} is already a main owner.", allowed_mentions=quiet)
        if user.id in OWNERS.ids:
            return await ctx.send(f"{user.mention} is already an extra owner.", allowed_mentions=quiet)
        OWNERS.ids.add(user.id)
        OWNERS.save()
        await ctx.send(f"{user.mention} is now an extra bot owner.", allowed_mentions=quiet)

    @owners.command(name="remove")
    async def owners_remove(self, ctx, user: discord.User):
        """Remove an extra owner. Main owners can't be removed here."""
        quiet = discord.AllowedMentions.none()
        if user.id not in OWNERS.ids:
            return await ctx.send(f"{user.mention} isn't an extra owner.", allowed_mentions=quiet)
        OWNERS.ids.discard(user.id)
        OWNERS.save()
        await ctx.send(f"{user.mention} is no longer an extra bot owner.", allowed_mentions=quiet)


# ---------------- app guard (user-installed app bursts) ----------------

APPGUARD_DEFAULT = {"enabled": True, "count": 3, "seconds": 10, "action": "timeout", "minutes": 60}


def ag_cfg(guild):
    return STORE.get(guild.id).setdefault("appguard", dict(APPGUARD_DEFAULT))


def interaction_meta(message):
    """The interaction metadata of a message, under whichever name this discord.py version uses."""
    return getattr(message, "interaction_metadata", None)


def user_install_info(meta):
    """(is_user_installed, installer_id). Discord marks user installs with key 1 in authorizing_integration_owners."""
    owners = getattr(meta, "authorizing_integration_owners", None)
    if not owners:
        return False, None
    for k, v in owners.items():
        if str(getattr(k, "value", k)) == "1":
            return True, getattr(v, "id", v)
    return False, None


class AppGuard(commands.Cog):
    """Punishes the user behind a burst of messages sent through a user-installed app."""

    def __init__(self, bot):
        self.bot = bot
        self.recent = defaultdict(deque)  # (guild id, user id) -> timestamps
        self.punished = {}

    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False
        p = ctx.author.guild_permissions
        if p.manage_guild or p.administrator or ctx.author.id == ctx.guild.owner_id or await self.bot.is_owner(ctx.author):
            return True
        raise commands.MissingPermissions(["manage_guild"])

    async def debug_report(self, message):
        """Post what Discord sent for an app-related message, so we can see why App Guard did or didn't fire."""
        meta = interaction_meta(message)
        if not (getattr(message, "application_id", None) or getattr(message, "webhook_id", None) or meta or message.author.bot):
            return
        is_user, installer = user_install_info(meta) if meta else (False, None)
        owners = (getattr(meta, "authorizing_integration_owners", None) or {}) if meta else {}
        owners_txt = ", ".join(f"{getattr(k, 'value', k)}={getattr(v, 'id', v)}" for k, v in owners.items()) or "none"
        mu = getattr(meta, "user", None) if meta else None
        in_server = message.guild.get_member(message.author.id) is not None
        counted = is_user or (meta is not None and not owners and message.author.bot and not in_server)
        lines = [
            f"author: {message.author} bot={message.author.bot} id={message.author.id}",
            f"application_id: {getattr(message, 'application_id', None)}",
            f"webhook_id: {getattr(message, 'webhook_id', None)}",
            f"interaction metadata: {type(meta).__name__ if meta else 'none'}",
            f"metadata.user: {mu}",
            f"authorizing owners: {owners_txt}",
            f"detected as user install: {is_user} (installer {installer})",
            f"author in this server: {in_server}",
            f"counted as user install: {counted}",
        ]
        text = "\n".join(lines)
        print(f"[appguard debug] {text}")
        await mod_log(message.guild, "App guard debug", text[:1900])

    @commands.Cog.listener()
    async def on_message(self, message):
        g = message.guild
        if g is None:
            return
        if message.author.id == self.bot.user.id:
            return
        if ag_cfg(g).get("debug"):
            await self.debug_report(message)
        meta = interaction_meta(message)
        if meta is None:
            return
        is_user, installer = user_install_info(meta)
        # Discord sent no install type. A bot that isn't a member of this server can only be
        # user-installed, so count it. Server-installed bots are members and are still ignored.
        if not is_user and not getattr(meta, "authorizing_integration_owners", None) \
                and message.author.bot and g.get_member(message.author.id) is None:
            is_user = True
        if not is_user:
            return  # a server-installed bot's command, not a user-installed app
        user = getattr(meta, "user", None)
        if user is None or user.bot or user.id == self.bot.user.id:
            return
        cfg = ag_cfg(g)
        if not cfg["enabled"]:
            return
        now = time.monotonic()
        pk = (g.id, user.id)
        if pk in self.punished and now - self.punished[pk] < 30:
            # Already punished: remove every further message from this user's app right away
            await self.delete_messages([message])
            return
        q = self.recent[pk]
        q.append((now, message))
        while q and now - q[0][0] > cfg["seconds"]:
            q.popleft()
        if len(q) <= cfg["count"]:
            return
        if user.id == g.owner_id or user.id in STORE.get(g.id)["whitelist"] or await self.bot.is_owner(user):
            return
        self.punished[pk] = now
        msgs = [m for _, m in q]
        q.clear()
        await self.punish(g, user, cfg, installer)
        await self.delete_messages(msgs)

    async def delete_messages(self, msgs):
        for m in msgs:
            try:
                await m.delete()
            except discord.HTTPException:
                pass  # already gone, or I lack Manage Messages

    async def punish(self, g, user, cfg, installer=None):
        member = g.get_member(user.id)
        action = cfg["action"]
        reason = f"App guard: {cfg['count']}+ messages via a user-installed app in {cfg['seconds']}s"
        note = ""
        try:
            if action == "ban":
                await g.ban(discord.Object(id=user.id), reason=reason, delete_message_seconds=0)
                note = "banned"
            elif member is None:
                note = "not in server"
            elif action == "kick":
                await member.kick(reason=reason)
                note = "kicked"
            else:
                await member.timeout(timedelta(minutes=cfg["minutes"]), reason=reason)
                note = f"timed out {cfg['minutes']}m"
        except discord.HTTPException:
            note = "punishment failed (check my permissions and role position)"
        if member is not None and action != "ban":
            await dm_action(member, g, "You were punished by app guard", reason, f"Action: {note}")
        await mod_log(
            g, "App guard",
            f"{user.mention} (`{user.id}`) sent {cfg['count']}+ messages through a user-installed app within {cfg['seconds']}s.\n"
            f"Installed by: {f'<@{installer}>' if installer else 'unknown'}\nPunishment: {note}",
        )

    @commands.hybrid_group(name="appguard", fallback="status", invoke_without_command=True)
    async def appguard(self, ctx):
        """Show App Guard settings."""
        cfg = ag_cfg(ctx.guild)
        act = cfg["action"] + (f" ({cfg['minutes']}m)" if cfg["action"] == "timeout" else "")
        e = discord.Embed(title="App guard", color=0x9B59B6)
        e.add_field(name="Status", value="ON" if cfg["enabled"] else "OFF")
        e.add_field(name="Trigger", value=f"more than {cfg['count']} messages in {cfg['seconds']}s from one user-installed app")
        e.add_field(name="Punishment", value=act)
        e.set_footer(text=f"{ctx.clean_prefix}appguard on|off|set|punishment")
        await ctx.send(embed=e)

    @appguard.command(name="on")
    async def ag_on(self, ctx):
        """Turn App Guard on."""
        ag_cfg(ctx.guild)["enabled"] = True
        STORE.save()
        await ctx.send("App guard enabled.")

    @appguard.command(name="debug")
    async def ag_debug(self, ctx, state: Literal["on", "off"]):
        """Post raw details about app messages in this server, to diagnose App Guard."""
        ag_cfg(ctx.guild)["debug"] = state == "on"
        STORE.save()
        await ctx.send(f"App guard debug {state}. Details go to the anti-nuke log channel." if state == "on" else "App guard debug off.")

    @appguard.command(name="off")
    async def ag_off(self, ctx):
        """Turn App Guard off."""
        ag_cfg(ctx.guild)["enabled"] = False
        STORE.save()
        await ctx.send("App guard disabled.")

    @appguard.command(name="set")
    async def ag_set(self, ctx, messages: commands.Range[int, 2, 50], seconds: commands.Range[int, 1, 120]):
        """Set the trigger: more than <messages> in <seconds> from one app user."""
        cfg = ag_cfg(ctx.guild)
        cfg["count"], cfg["seconds"] = messages, seconds
        STORE.save()
        await ctx.send(f"App guard: more than {messages} messages in {seconds}s will be punished.")

    @appguard.command(name="punishment")
    async def ag_punishment(
        self, ctx,
        action: Literal["timeout", "kick", "ban"],
        minutes: commands.Range[int, 1, 10080] = None,
    ):
        """Set the punishment for the user behind the app. Minutes only apply to timeout."""
        cfg = ag_cfg(ctx.guild)
        cfg["action"] = action
        if minutes is not None:
            cfg["minutes"] = minutes
        STORE.save()
        extra = f" for {cfg['minutes']}m" if action == "timeout" else ""
        await ctx.send(f"App guard punishment set to `{action}`{extra}.")


# ---------------- webhook guard (webhook message bursts) ----------------

WEBHOOKGUARD_DEFAULT = {"enabled": True, "count": 10, "seconds": 10, "action": "timeout", "minutes": 60}


def wg_cfg(guild):
    return STORE.get(guild.id).setdefault("webhookguard", dict(WEBHOOKGUARD_DEFAULT))


class WebhookGuard(commands.Cog):
    """Deletes a webhook that bursts messages, deletes its spam, and punishes whoever created it."""

    def __init__(self, bot):
        self.bot = bot
        self.recent = defaultdict(deque)  # webhook id -> (time, message)
        self.handled = {}                 # webhook id -> time it was deleted

    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False
        p = ctx.author.guild_permissions
        if p.manage_guild or p.administrator or ctx.author.id == ctx.guild.owner_id or await self.bot.is_owner(ctx.author):
            return True
        raise commands.MissingPermissions(["manage_guild"])

    @commands.Cog.listener()
    async def on_message(self, message):
        g = message.guild
        wid = getattr(message, "webhook_id", None)
        if g is None or wid is None:
            return
        cfg = wg_cfg(g)
        if not cfg["enabled"]:
            return
        now = time.monotonic()
        if wid in self.handled and now - self.handled[wid] < 60:
            await self.delete_messages([message])  # leftover message from a webhook we already removed
            return
        q = self.recent[wid]
        q.append((now, message))
        while q and now - q[0][0] > cfg["seconds"]:
            q.popleft()
        if len(q) <= cfg["count"]:
            return
        self.handled[wid] = now
        msgs = [m for _, m in q]
        q.clear()
        await self.punish(g, wid, cfg)
        await self.delete_messages(msgs)

    async def find_creator(self, g, wid):
        try:
            async for entry in g.audit_logs(limit=100, action=discord.AuditLogAction.webhook_create):
                if getattr(entry.target, "id", None) == wid:
                    return entry.user
        except discord.HTTPException:
            pass  # missing View Audit Log, or the entry is too old
        return None

    async def punish(self, g, wid, cfg):
        creator = await self.find_creator(g, wid)
        note = "webhook deleted"
        channel_id = None
        try:
            hook = await self.bot.fetch_webhook(wid)
            channel_id = hook.channel_id
            await hook.delete(reason="Webhook guard: message burst")
        except discord.HTTPException:
            note = "couldn't delete webhook (check Manage Webhooks)"
        if channel_id:
            purged, perr = await self.purge_webhook_messages(g, channel_id, wid)
            note += f", {purged} message(s) removed"
            if perr:
                note += f" ({perr})"
        if creator is None:
            creator_note = "creator unknown (check View Audit Log)"
        elif creator.id == g.owner_id or creator.id in STORE.get(g.id)["whitelist"] or await self.bot.is_owner(creator):
            creator_note = "creator is exempt"
        else:
            creator_note = await self.punish_user(g, creator, cfg)
        await mod_log(
            g, "Webhook guard",
            f"Webhook `{wid}` sent {cfg['count']}+ messages in {cfg['seconds']}s.\n"
            f"Created by: {creator.mention if creator else 'unknown'}\n"
            f"Webhook: {note}\nCreator punishment: {creator_note}",
        )

    async def purge_webhook_messages(self, g, channel_id, wid):
        """Delete every message this webhook sent in its channel (deleting a webhook leaves its messages behind)."""
        ch = g.get_channel(channel_id)
        if ch is None:
            return 0, "channel not found"
        found = []
        try:
            async for m in ch.history(limit=300):
                if m.webhook_id == wid:
                    found.append(m)
        except discord.HTTPException:
            return 0, "couldn't read history (check Read Message History)"
        cutoff = discord.utils.utcnow() - timedelta(days=14)
        recent = [m for m in found if m.created_at > cutoff]
        old = [m for m in found if m.created_at <= cutoff]
        deleted, err = 0, None
        try:
            for i in range(0, len(recent), 100):
                chunk = recent[i:i + 100]
                if len(chunk) == 1:
                    await chunk[0].delete()
                else:
                    await ch.delete_messages(chunk)
                deleted += len(chunk)
        except discord.HTTPException:
            err = "some messages failed (check Manage Messages)"
        for m in old:  # bulk delete only works on messages under 14 days old
            try:
                await m.delete()
                deleted += 1
            except discord.HTTPException:
                err = "some messages failed (check Manage Messages)"
        return deleted, err

    async def punish_user(self, g, user, cfg):
        member = g.get_member(user.id)
        action = "ban" if user.bot else cfg["action"]
        reason = "Webhook guard: webhook message burst"
        try:
            if action == "ban":
                await g.ban(discord.Object(id=user.id), reason=reason, delete_message_seconds=0)
                return "banned"
            if member is None:
                return "not in server"
            if action == "kick":
                await member.kick(reason=reason)
                return "kicked"
            await member.timeout(timedelta(minutes=cfg["minutes"]), reason=reason)
            return f"timed out {cfg['minutes']}m"
        except discord.HTTPException:
            return "punishment failed (check my permissions and role position)"

    async def delete_messages(self, msgs):
        for m in msgs:
            try:
                await m.delete()
            except discord.HTTPException:
                pass

    @commands.hybrid_group(name="webhookguard", fallback="status", invoke_without_command=True)
    async def webhookguard(self, ctx):
        """Show Webhook Guard settings."""
        cfg = wg_cfg(ctx.guild)
        act = cfg["action"] + (f" ({cfg['minutes']}m)" if cfg["action"] == "timeout" else "")
        e = discord.Embed(title="Webhook guard", color=0x95A5A6)
        e.add_field(name="Status", value="ON" if cfg["enabled"] else "OFF")
        e.add_field(name="Trigger", value=f"more than {cfg['count']} messages in {cfg['seconds']}s from one webhook")
        e.add_field(name="Creator punishment", value=act)
        e.set_footer(text=f"{ctx.clean_prefix}webhookguard on|off|set|punishment")
        await ctx.send(embed=e)

    @webhookguard.command(name="on")
    async def wg_on(self, ctx):
        """Turn Webhook Guard on."""
        wg_cfg(ctx.guild)["enabled"] = True
        STORE.save()
        await ctx.send("Webhook guard enabled.")

    @webhookguard.command(name="off")
    async def wg_off(self, ctx):
        """Turn Webhook Guard off."""
        wg_cfg(ctx.guild)["enabled"] = False
        STORE.save()
        await ctx.send("Webhook guard disabled.")

    @webhookguard.command(name="set")
    async def wg_set(self, ctx, messages: commands.Range[int, 2, 100], seconds: commands.Range[int, 1, 120]):
        """Set the trigger: more than <messages> in <seconds> from one webhook."""
        cfg = wg_cfg(ctx.guild)
        cfg["count"], cfg["seconds"] = messages, seconds
        STORE.save()
        await ctx.send(f"Webhook guard: more than {messages} messages in {seconds}s will be punished.")

    @webhookguard.command(name="punishment")
    async def wg_punishment(
        self, ctx,
        action: Literal["timeout", "kick", "ban"],
        minutes: commands.Range[int, 1, 10080] = None,
    ):
        """Set the punishment for whoever created the webhook. Minutes only apply to timeout."""
        cfg = wg_cfg(ctx.guild)
        cfg["action"] = action
        if minutes is not None:
            cfg["minutes"] = minutes
        STORE.save()
        extra = f" for {cfg['minutes']}m" if action == "timeout" else ""
        await ctx.send(f"Webhook guard punishment set to `{action}`{extra}.")


# ---------------- bot spam guard (bot accounts flooding messages) ----------------

BOTSPAM_DEFAULT = {"enabled": True, "count": 15, "seconds": 10}


def bs_cfg(guild):
    return STORE.get(guild.id).setdefault("botspam", dict(BOTSPAM_DEFAULT))


class BotSpam(commands.Cog):
    """Bans a bot account that floods messages and deletes its spam.
    Webhooks and user-installed apps are handled by their own guards."""

    def __init__(self, bot):
        self.bot = bot
        self.recent = defaultdict(deque)  # (guild id, bot id) -> (time, message)
        self.punished = {}

    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False
        p = ctx.author.guild_permissions
        if p.manage_guild or p.administrator or ctx.author.id == ctx.guild.owner_id or await self.bot.is_owner(ctx.author):
            return True
        raise commands.MissingPermissions(["manage_guild"])

    @commands.Cog.listener()
    async def on_message(self, message):
        g = message.guild
        if g is None or not message.author.bot or message.webhook_id is not None:
            return
        if message.author.id == self.bot.user.id:
            return
        cfg = bs_cfg(g)
        if not cfg["enabled"]:
            return
        bid = message.author.id
        if bid in STORE.get(g.id)["whitelist"]:
            return
        pk = (g.id, bid)
        now = time.monotonic()
        if pk in self.punished and now - self.punished[pk] < 60:
            await self.delete_messages([message])  # the bot is already banned, remove what it still sends
            return
        q = self.recent[pk]
        q.append((now, message))
        while q and now - q[0][0] > cfg["seconds"]:
            q.popleft()
        if len(q) <= cfg["count"]:
            return
        self.punished[pk] = now
        msgs = [m for _, m in q]
        q.clear()
        note = "banned"
        try:
            await g.ban(
                discord.Object(id=bid),
                reason=f"Bot spam: more than {cfg['count']} messages in {cfg['seconds']}s",
                delete_message_seconds=0,
            )
        except discord.HTTPException:
            note = "ban failed (check my permissions and role position)"
        await self.delete_messages(msgs)
        await mod_log(
            g, "Bot spam",
            f"{message.author.mention} (`{bid}`) sent more than {cfg['count']} messages in {cfg['seconds']}s.\n"
            f"Punishment: {note}, {len(msgs)} message(s) removed",
        )

    async def delete_messages(self, msgs):
        for m in msgs:
            try:
                await m.delete()
            except discord.HTTPException:
                pass

    @commands.hybrid_group(name="botspam", fallback="status", invoke_without_command=True)
    async def botspam(self, ctx):
        """Show Bot Spam Guard settings."""
        cfg = bs_cfg(ctx.guild)
        e = discord.Embed(title="Bot spam guard", color=0xE67E22)
        e.add_field(name="Status", value="ON" if cfg["enabled"] else "OFF")
        e.add_field(name="Trigger", value=f"more than {cfg['count']} messages in {cfg['seconds']}s from one bot")
        e.add_field(name="Punishment", value="ban the bot and delete its messages", inline=False)
        e.set_footer(text=f"{ctx.clean_prefix}botspam on|off|set  (whitelisted bots are skipped)")
        await ctx.send(embed=e)

    @botspam.command(name="on")
    async def bsp_on(self, ctx):
        """Turn Bot Spam Guard on."""
        bs_cfg(ctx.guild)["enabled"] = True
        STORE.save()
        await ctx.send("Bot spam guard enabled.")

    @botspam.command(name="off")
    async def bsp_off(self, ctx):
        """Turn Bot Spam Guard off."""
        bs_cfg(ctx.guild)["enabled"] = False
        STORE.save()
        await ctx.send("Bot spam guard disabled.")

    @botspam.command(name="set")
    async def bsp_set(self, ctx, messages: commands.Range[int, 3, 100], seconds: commands.Range[int, 1, 120]):
        """Set the trigger: more than <messages> in <seconds> from one bot."""
        cfg = bs_cfg(ctx.guild)
        cfg["count"], cfg["seconds"] = messages, seconds
        STORE.save()
        await ctx.send(f"Bot spam guard: more than {messages} messages in {seconds}s will be punished.")


# ---------------- Discord AutoMod sync  ----------------

NATIVE_PREFIX = "[Bot] "


async def discord_call(coro):
    """Wait at most 15 seconds for one Discord request, so a stuck request reports an error instead of hanging."""
    return await asyncio.wait_for(coro, timeout=15)


class NativeAutoMod(commands.Cog):
    """Pushes this bot's word, mention and spam settings to Discord's own AutoMod, and logs Discord's actions.
    One-way: changes made in Discord's AutoMod screen are not read back."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False
        p = ctx.author.guild_permissions
        if p.manage_guild or p.administrator or ctx.author.id == ctx.guild.owner_id or await self.bot.is_owner(ctx.author):
            return True
        raise commands.MissingPermissions(["manage_guild"])

    def wanted_rules(self, guild):
        """The Discord rules this server should have, built from this bot's settings."""
        am = STORE.get(guild.id)["automod"]
        log = STORE.get(guild.id).get("log_channel")
        alert = [discord.AutoModRuleAction(channel_id=log)] if log else []
        rules = []
        if am["badwords"]:
            rules.append((
                "Words",
                discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.keyword, keyword_filter=am["badwords"][:1000]),
                [discord.AutoModRuleAction()] + alert,
            ))
        if am["antimention"]:
            rules.append((
                "Mentions",
                discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.mention_spam, mention_limit=am["mention_limit"]),
                [discord.AutoModRuleAction()] + alert,
            ))
        if am["antispam"]:
            rules.append((
                "Spam",
                discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.spam),
                [discord.AutoModRuleAction()] + alert,
            ))
        return rules

    @commands.hybrid_group(name="discordautomod", fallback="status", invoke_without_command=True)
    async def discordautomod(self, ctx):
        """Show the Discord AutoMod rules this bot manages."""
        rules = [r for r in await ctx.guild.fetch_automod_rules() if r.name.startswith(NATIVE_PREFIX)]
        names = ", ".join(r.name[len(NATIVE_PREFIX):] for r in rules) or "none"
        await ctx.send(f"Rules managed by me in Discord AutoMod: {names}. Use `{ctx.clean_prefix}discordautomod sync` to update them.")

    @discordautomod.command(name="sync")
    async def dam_sync(self, ctx):
        """Create or update Discord AutoMod rules from this bot's word, mention and spam settings."""
        await ctx.defer()
        try:
            existing = {r.name: r for r in await discord_call(ctx.guild.fetch_automod_rules())}
            made, updated = 0, 0
            wanted = self.wanted_rules(ctx.guild)
            for label, trigger, actions in wanted:
                name = NATIVE_PREFIX + label
                if name in existing:
                    await discord_call(existing[name].edit(trigger=trigger, actions=actions, enabled=True, reason="Synced from bot settings"))
                    updated += 1
                else:
                    await discord_call(ctx.guild.create_automod_rule(
                        name=name,
                        event_type=discord.AutoModRuleEventType.message_send,
                        trigger=trigger, actions=actions, enabled=True,
                        reason="Created from bot settings",
                    ))
                    made += 1
            keep = {NATIVE_PREFIX + label for label, _, _ in wanted}
            removed = 0
            for name, rule in existing.items():
                if name.startswith(NATIVE_PREFIX) and name not in keep:
                    await discord_call(rule.delete(reason="No longer enabled in bot settings"))
                    removed += 1
        except asyncio.TimeoutError:
            return await ctx.send(
                "Discord didn't respond within 15 seconds, probably rate limiting on AutoMod changes. "
                "Wait a minute and run it again. Rules already updated are kept."
            )
        except discord.Forbidden:
            return await ctx.send("I need Manage Server to manage Discord AutoMod rules.")
        except discord.HTTPException as exc:
            return await ctx.send(f"Discord refused the change: {clip(exc.text or str(exc), 300)}")
        await ctx.send(f"Discord AutoMod synced: {made} created, {updated} updated, {removed} removed.")

    @discordautomod.command(name="remove")
    async def dam_remove(self, ctx):
        """Delete every Discord AutoMod rule this bot manages."""
        await ctx.defer()
        try:
            removed = 0
            for rule in await discord_call(ctx.guild.fetch_automod_rules()):
                if rule.name.startswith(NATIVE_PREFIX):
                    await discord_call(rule.delete(reason="Removed by bot command"))
                    removed += 1
        except asyncio.TimeoutError:
            return await ctx.send("Discord didn't respond within 15 seconds. Try again in a minute.")
        await ctx.send(f"Removed {removed} Discord AutoMod rule(s) managed by me.")

    @commands.Cog.listener()
    async def on_automod_action(self, execution):
        """Log every message Discord's AutoMod blocks or acts on, in the mod log and forensics."""
        guild = execution.guild
        if guild is None:
            return
        user = f"<@{execution.user_id}>" if execution.user_id else "unknown"
        channel = f"<#{execution.channel_id}>" if execution.channel_id else "unknown"
        keyword = f"\nKeyword: `{clip(execution.matched_keyword, 60)}`" if getattr(execution, "matched_keyword", None) else ""
        await mod_log(
            guild, "Discord AutoMod",
            f"{user} in {channel} triggered Discord AutoMod ({execution.rule_trigger_type.name}).{keyword}\n"
            f"Action: {execution.action.type.name}",
        )


# ---------------- quarantine (Wick-style) ----------------

QUARANTINE_PATH = os.getenv("QUARANTINE_PATH", "quarantine.json")


class QuarantineStore:
    def __init__(self, path):
        self.path = path
        self.data = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.data = json.load(f)
            except Exception:
                os.replace(path, path + ".corrupt")

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)


QUAR = QuarantineStore(QUARANTINE_PATH)


async def quarantine_role(guild, create=False):
    """The Quarantine role for a server. Created if create=True and it doesn't exist."""
    rid = QUAR.data.get(str(guild.id), {}).get("role")
    role = guild.get_role(rid) if rid else None
    if role is None:
        role = next((r for r in guild.roles if r.name == "Quarantine"), None)
    if role is None and create:
        role = await guild.create_role(name="Quarantine", permissions=discord.Permissions.none(),
                                       reason="Quarantine role created by the bot")
    if role is not None:
        QUAR.data.setdefault(str(guild.id), {})["role"] = role.id
        QUAR.save()
    return role


async def quarantine_channel(guild, role):
    """The #quarantine appeal channel: visible only to quarantined members. Created if missing."""
    cid = QUAR.data.get(str(guild.id), {}).get("channel")
    ch = guild.get_channel(cid) if cid else None
    if ch is None:
        ch = next((c for c in guild.text_channels if c.name == "quarantine"), None)
    if ch is None:
        ch = await guild.create_text_channel(
            "quarantine",
            overwrites={
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            },
            reason="Quarantine appeal channel",
        )
    else:
        await ch.set_permissions(guild.default_role, view_channel=False)
        await ch.set_permissions(role, view_channel=True, send_messages=True, read_message_history=True,
                                 reason="Quarantine appeal channel")
    QUAR.data.setdefault(str(guild.id), {})["channel"] = ch.id
    QUAR.save()
    return ch


async def quarantine_setup(guild):
    """Create the Quarantine role if needed, put it just below the bot's role, and hide every channel from it.
    Returns (role, channels locked, failures)."""
    role = await quarantine_role(guild, create=True)
    top = guild.me.top_role
    if role.position >= top.position and top.position > 1:
        try:
            await role.edit(position=top.position - 1, reason="Quarantine role placed below my role")
        except discord.HTTPException:
            pass
    qch = await quarantine_channel(guild, role)
    locked, failed = 0, 0
    for ch in guild.channels:
        if qch is not None and ch.id == qch.id:
            continue  # the appeal channel stays visible
        try:
            await ch.set_permissions(role, view_channel=False, reason="Quarantine lockout")
            locked += 1
        except discord.HTTPException:
            failed += 1
        await asyncio.sleep(0.5)
    return role, locked, failed


async def quarantine_member(guild, member, reason):
    """Save the member's removable roles, then leave them with only the Quarantine role. Returns a note."""
    role = await quarantine_role(guild)
    if role is None:
        return "no Quarantine role yet, run /quarantine setup"
    keep = [r for r in member.roles if not r.is_default() and not r.managed and r < guild.me.top_role and r != role]
    QUAR.data.setdefault(str(guild.id), {}).setdefault("users", {})[str(member.id)] = [r.id for r in keep]
    QUAR.save()
    if keep:
        await member.remove_roles(*keep, reason=reason)
    if role not in member.roles:
        await member.add_roles(role, reason=reason)
    return f"quarantined ({len(keep)} role(s) saved)"


async def quarantine_release(guild, member, reason):
    users = QUAR.data.get(str(guild.id), {}).get("users", {})
    saved = users.pop(str(member.id), [])
    QUAR.save()
    role = await quarantine_role(guild)
    if role is not None and role in member.roles:
        await member.remove_roles(role, reason=reason)
    back = [r for r in guild.roles if r.id in saved and r < guild.me.top_role and not r.managed]
    if back:
        await member.add_roles(*back, reason=reason)
    return len(back)


class Quarantine(commands.Cog):
    """Wick-style quarantine: a role that hides every channel, with roles saved and restored on release."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False
        p = ctx.author.guild_permissions
        if p.manage_guild or p.administrator or ctx.author.id == ctx.guild.owner_id or await self.bot.is_owner(ctx.author):
            return True
        raise commands.MissingPermissions(["manage_guild"])

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        if channel.id == QUAR.data.get(str(channel.guild.id), {}).get("channel"):
            return
        role = await quarantine_role(channel.guild)
        if role is not None:
            try:
                await channel.set_permissions(role, view_channel=False, reason="Quarantine lockout for new channel")
            except discord.HTTPException:
                pass

    @commands.hybrid_group(name="quarantine", fallback="status", invoke_without_command=True)
    async def quarantine(self, ctx):
        """Show the Quarantine role and how many members are quarantined."""
        role = await quarantine_role(ctx.guild)
        count = len(QUAR.data.get(str(ctx.guild.id), {}).get("users", {}))
        if role is None:
            return await ctx.send("No Quarantine role yet. Run `/quarantine setup`.")
        await ctx.send(f"Quarantine role: {role.mention}. Members quarantined: {count}.")

    @quarantine.command(name="setup")
    async def q_setup(self, ctx):
        """Create the Quarantine role, hide every channel from it, and place it under my role."""
        await ctx.defer()
        try:
            role, locked, failed = await quarantine_setup(ctx.guild)
        except discord.HTTPException as exc:
            return await ctx.send(f"Could not create the role: {clip(exc.text or str(exc), 200)}. Check Manage Roles.")
        e = discord.Embed(title="Quarantine ready", color=0x2ECC71)
        e.add_field(name="Role", value=role.mention)
        e.add_field(name="Channels hidden", value=str(locked))
        if failed:
            e.add_field(name="Failed", value=f"{failed} channel(s). Check my permissions on them.", inline=False)
        if role.position >= ctx.guild.me.top_role.position:
            e.add_field(
                name="Action needed",
                value="Drag my role to the top of the role list, then drag the Quarantine role just below it. "
                      "Otherwise I can't assign it.",
                inline=False,
            )
        e.add_field(name="Appeal channel", value="#quarantine is visible only to quarantined members. "
                    "Give your staff roles access to it, or they need Administrator to see it.", inline=False)
        e.set_footer(text="New channels are hidden from the Quarantine role automatically.")
        await ctx.send(embed=e)

    @quarantine.command(name="add")
    async def q_add(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        """Quarantine a member manually."""
        if err := check_target(ctx, member):
            return await ctx.send(err)
        note = await quarantine_member(ctx.guild, member, f"{ctx.author} ({ctx.author.id}): {reason}")
        await mod_log(ctx.guild, "Quarantine", f"{member.mention} by {ctx.author.mention}: {note}\n{reason}")
        await ctx.send(f"{member.mention} {note}.")

    @quarantine.command(name="release")
    async def q_release(self, ctx, member: discord.Member):
        """Release a member and give back the roles they had before quarantine."""
        n = await quarantine_release(ctx.guild, member, f"Released by {ctx.author} ({ctx.author.id})")
        await mod_log(ctx.guild, "Quarantine released", f"{member.mention} by {ctx.author.mention}, {n} role(s) returned")
        await ctx.send(f"Released {member.mention}. {n} role(s) returned.")

    @quarantine.command(name="list")
    async def q_list(self, ctx):
        """List quarantined members."""
        users = QUAR.data.get(str(ctx.guild.id), {}).get("users", {})
        lines = [f"<@{uid}>" for uid in users][:50] or ["none"]
        e = discord.Embed(title=f"Quarantined ({len(users)})", description="\n".join(lines), color=0xE67E22)
        await ctx.send(embed=e)


# ---------------- overly broad AutoMod rules ----------------

BROAD_SAMPLES = ["hello world", "12345", "!!!???", "a", "The quick brown fox 99"]


def rule_is_broad(rule):
    """True if an AutoMod rule would block almost any message (wildcard-only words, or a regex matching every sample)."""
    trig = getattr(rule, "trigger", None)
    if trig is None:
        return False
    for kw in (getattr(trig, "keyword_filter", None) or []):
        if kw.replace("*", "").strip() == "":
            return True
    for pat in (getattr(trig, "regex_patterns", None) or []):
        try:
            rx = re.compile(pat)
        except re.error:
            continue
        if all(rx.search(t) for t in BROAD_SAMPLES):
            return True
    return False


class AutoModWatch(commands.Cog):
    """Disables AutoMod rules that would block almost every message, whoever created them."""

    def __init__(self, bot):
        self.bot = bot
        self.scan.start()

    def cog_unload(self):
        self.scan.cancel()

    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False
        p = ctx.author.guild_permissions
        if p.manage_guild or p.administrator or ctx.author.id == ctx.guild.owner_id or await self.bot.is_owner(ctx.author):
            return True
        raise commands.MissingPermissions(["manage_guild"])

    async def check_rule(self, guild, rule, who):
        """Disable the rule if it's broad. Returns True if it was disabled now."""
        if not rule.enabled or rule.name.startswith(NATIVE_PREFIX) or not rule_is_broad(rule):
            return False
        try:
            await rule.edit(enabled=False, reason="Blocks almost every message: disabled by the bot")
        except discord.HTTPException:
            return False
        await mod_log(
            guild, "Overly broad AutoMod rule disabled",
            f"Rule '{clip(rule.name, 60)}' would block almost every message. Changed by {who}.\n"
            "It is disabled until you review it in Server Settings → AutoMod.",
        )
        if guild.owner:
            try:
                await guild.owner.send(
                    f"**{guild.name}**: an AutoMod rule '{clip(rule.name, 60)}' would block almost every message, "
                    "so I disabled it. Review it in Server Settings → AutoMod."
                )
            except discord.HTTPException:
                pass
        return True

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry):
        if entry.action not in (discord.AuditLogAction.automod_rule_create, discord.AuditLogAction.automod_rule_update):
            return
        if entry.user is None or entry.user.id == self.bot.user.id or entry.guild is None:
            return
        try:
            rule = await entry.guild.fetch_automod_rule(entry.target.id)
        except (discord.HTTPException, AttributeError):
            return
        await self.check_rule(entry.guild, rule, entry.user.mention)

    @commands.hybrid_command(name="automodcheck", description="Scan AutoMod rules and disable any that would block almost every message.")
    async def automodcheck(self, ctx):
        """Scan every AutoMod rule now, disable any that would block almost every message, and list them."""
        await ctx.defer()
        try:
            rules = await ctx.guild.fetch_automod_rules()
        except discord.HTTPException:
            return await ctx.send("I couldn't read the AutoMod rules. Check that I have Manage Server.")
        checked = [r for r in rules if not r.name.startswith(NATIVE_PREFIX)]
        broad = [r for r in checked if rule_is_broad(r)]
        if not broad:
            return await ctx.send(f"No overly broad AutoMod rules. Checked {len(checked)} rule(s).")
        lines = []
        for r in broad:
            if r.enabled:
                ok = await self.check_rule(ctx.guild, r, f"{ctx.author.mention} (scan)")
                status = "disabled now" if ok else "could not disable (check Manage Server)"
            else:
                status = "already disabled"
            lines.append(f"`{clip(r.name, 50)}`: {status}")
        e = discord.Embed(
            title=f"Overly broad AutoMod rules ({len(broad)})",
            description=clip("\n".join(lines), 4000),
            color=0xE74C3C,
        )
        e.set_footer(text=f"Checked {len(checked)} rule(s). Re-enable a rule in Server Settings → AutoMod only if it's safe.")
        await ctx.send(embed=e)

    @tasks.loop(minutes=10)
    async def scan(self):
        for g in list(self.bot.guilds):
            try:
                for rule in await g.fetch_automod_rules():
                    await self.check_rule(g, rule, "an earlier change")
            except discord.HTTPException:
                continue
            await asyncio.sleep(1)

    @scan.before_loop
    async def before_scan(self):
        await self.bot.wait_until_ready()


class Bot(commands.Bot):
    async def is_owner(self, user):
        # Extra owners pass every bot-owner check (commands.is_owner, cog checks, etc.)
        if user.id in OWNERS.ids:
            return True
        return await super().is_owner(user)

    async def setup_hook(self):
        global BOT_OWNER_IDS
        try:
            info = await self.application_info()
            if info.team:
                BOT_OWNER_IDS |= {m.id for m in info.team.members}
            else:
                BOT_OWNER_IDS.add(info.owner.id)
        except Exception as exc:
            print(f"[owners] could not read the Developer Portal owner: {exc!r}")
        await self.add_cog(ActivityLog(self))
        await self.add_cog(BoostRewards(self))
        await self.add_cog(NukeBotDB(self))
        await self.add_cog(BotOwners(self))
        await self.add_cog(AppGuard(self))
        await self.add_cog(WebhookGuard(self))
        await self.add_cog(BotSpam(self))
        await self.add_cog(NativeAutoMod(self))
        await self.add_cog(AntiNuke(self))
        await self.add_cog(Moderation(self))
        await self.add_cog(AutoMod(self))
        await self.add_cog(Quarantine(self))
        await self.add_cog(AutoModWatch(self))
        await self.add_cog(AntiRaid(self))
        await self.tree.sync()  # register slash commands globally


def get_prefix(bot, message):
    prefix = PREFIX
    if message.guild:
        prefix = STORE.get(message.guild.id).get("prefix", PREFIX)
    return commands.when_mentioned_or(prefix)(bot, message)


bot = Bot(command_prefix=get_prefix, intents=intents, owner_ids=EXTRA_OWNERS, help_command=None)

PRESENCE_PATH = os.getenv("PRESENCE_PATH", "presence.json")
STATES = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
    "invisible": discord.Status.invisible,
}
ACTIVITY_TYPES = {
    "watching": discord.ActivityType.watching,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
}


async def apply_presence(data):
    kind, text = data.get("kind", "playing"), data.get("text", "")
    if kind == "none" or not text:
        activity = None
    elif kind == "playing":
        activity = discord.Game(name=text)
    else:
        activity = discord.Activity(type=ACTIVITY_TYPES[kind], name=text)
    await bot.change_presence(status=STATES[data.get("state", "online")], activity=activity)


SUPPORT_URL = "https://discord.gg/jRQWFHRGn4"


@bot.hybrid_command(name="support", description="Get the support server link.")
async def support(ctx):
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Join Support Server", url=SUPPORT_URL))
    await ctx.send(f"Need help? Join the support server:\n{SUPPORT_URL}", view=view)


@bot.hybrid_command(name="invite", description="Get the bot invite link.")
async def invite(ctx):
    perms = discord.Permissions(
        view_channel=True, send_messages=True, embed_links=True,
        read_message_history=True, view_audit_log=True, ban_members=True,
        kick_members=True, moderate_members=True, manage_messages=True,
        manage_channels=True, manage_roles=True,
    )
    url = discord.utils.oauth_url(
        bot.user.id, permissions=perms, scopes=("bot", "applications.commands")
    )
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Invite Bot", url=url))
    await ctx.send("Add me to your server:", view=view)


def fmt_uptime(seconds: float) -> str:
    d, r = divmod(int(seconds), 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = [f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else "", f"{s}s"]
    return " ".join(p for p in parts if p)


@bot.hybrid_command(name="ping", description="Check the bot's latency.")
async def ping(ctx):
    gateway = round(bot.latency * 1000)
    start = time.perf_counter()
    msg = await ctx.send("Pinging...")
    api = round((time.perf_counter() - start) * 1000)
    e = discord.Embed(title="Pong!", color=0x2ECC71)
    e.add_field(name="Gateway", value=f"`{gateway} ms`")
    e.add_field(name="API", value=f"`{api} ms`")
    await msg.edit(content=None, embed=e)


@bot.hybrid_command(name="apiinfo", aliases=["api"], description="Show API and bot info.")
async def apiinfo(ctx):
    api_version = discord.http.Route.BASE.rsplit("/", 1)[-1]
    users = sum(g.member_count or 0 for g in bot.guilds)
    e = discord.Embed(title="API Info", color=0x3498DB)
    e.add_field(name="Library", value=f"discord.py `{discord.__version__}`")
    e.add_field(name="Python", value=f"`{platform.python_version()}`")
    e.add_field(name="Discord API", value=f"`{api_version}`")
    e.add_field(name="Gateway latency", value=f"`{round(bot.latency * 1000)} ms`")
    e.add_field(name="Uptime", value=f"`{fmt_uptime(time.time() - START_TIME)}`")
    e.add_field(name="Shards", value=f"`{bot.shard_count or 1}`")
    e.add_field(name="Servers", value=f"`{len(bot.guilds)}`")
    e.add_field(name="Users", value=f"`{users}`")
    e.add_field(name="Commands", value=f"`{len(bot.commands)}`")
    e.set_footer(text=f"Prefix: {ctx.clean_prefix}")
    await ctx.send(embed=e)


@bot.hybrid_command(name="prefix", description="Show or change the bot prefix for this server.")
@commands.guild_only()
async def prefix(ctx, new_prefix: str = None):
    cfg = STORE.get(ctx.guild.id)
    if new_prefix is None:
        return await ctx.send(f"Prefix here is `{cfg['prefix']}` (you can also mention me).")
    p = ctx.author.guild_permissions
    if not (p.manage_guild or p.administrator or ctx.author.id == ctx.guild.owner_id or ctx.author.id in EXTRA_OWNERS):
        raise commands.MissingPermissions(["manage_guild"])
    new_prefix = new_prefix.strip()
    if new_prefix.lower() == "reset":
        new_prefix = PREFIX
    if not new_prefix or len(new_prefix) > 5 or any(c.isspace() for c in new_prefix):
        return await ctx.send("Prefix must be 1-5 characters with no spaces.")
    cfg["prefix"] = new_prefix
    STORE.save()
    await ctx.send(f"Prefix changed to `{new_prefix}`. Use `{new_prefix}prefix reset` to go back.")


# ---------------- help menu ----------------

HELP_COLOR = 0x5865F2
HELP_OWNER_COGS = {"ActivityLog", "Premium", "NukeBotDB", "BotOwners"}  # hidden from everyone except the bot owner
HELP_OWNER_COMMANDS = {"botstatus"}
HELP_SECTIONS = [  # (cog name, label, emoji, short description)
    ("AntiNuke", "Anti-nuke", "🛡️", "Stops mass bans, deletions and other nukes. Server owner or bot owner."),
    ("Moderation", "Moderation", "🔨", "Moderation and role commands. Each needs its own permission."),
    ("AutoMod", "AutoMod & Anti-spam", "🤖", "Message filters and spam control. Needs Manage Server."),
    ("AntiRaid", "Anti-raid", "🚨", "Join-raid protection. Needs Manage Server."),
    ("BoostRewards", "Boosts", "💎", "Boost messages and reward roles. Needs Manage Server."),
    ("NukeBotDB", "Nuke bot DB", "☠️", "Database of malicious bots. Bot owner only."),
    ("BotOwners", "Owners", "👑", "Add or remove extra bot owners. Main owner only."),
    ("AppGuard", "App guard", "🧩", "Punishes users who spam through user-installed apps. Needs Manage Server."),
    ("WebhookGuard", "Webhook guard", "🪝", "Deletes webhooks that spam and punishes whoever created them. Needs Manage Server."),
    ("BotSpam", "Bot spam", "📢", "Bans bot accounts that flood messages. Needs Manage Server."),
    ("NativeAutoMod", "Discord AutoMod", "🧩", "Syncs settings to Discord AutoMod and logs its actions. Needs Manage Server."),
    ("Quarantine", "Quarantine", "🚧", "Wick-style quarantine: hide channels from a member and restore their roles later. Needs Manage Server."),
    ("AutoModWatch", "AutoMod checks", "🛑", "Finds and disables AutoMod rules that block almost every message. Needs Manage Server."),
    ("ActivityLog", "Logs", "📜", "Activity logging. Bot owner only."),
    ("Premium", "Premium", "⭐", "Premium access control. Bot owner only."),
    (None, "General", "ℹ️", "Info and utility commands."),
]


def guild_prefix(ctx):
    return STORE.get(ctx.guild.id)["prefix"] if ctx.guild else PREFIX


def cmd_desc(c):
    return (c.description or c.short_doc or "").strip()


def help_visible(cmd, is_owner):
    top = cmd.root_parent or cmd
    if top.hidden:
        return False
    if is_owner:
        return True
    if top.qualified_name in HELP_OWNER_COMMANDS:
        return False
    return not (top.cog and top.cog.qualified_name in HELP_OWNER_COGS)


def help_lines(cmd):
    sig = f" {cmd.signature}" if cmd.signature else ""
    lines = [f"`/{cmd.qualified_name}{sig}`\n{clip(cmd_desc(cmd), 110) or 'No description.'}"]
    if isinstance(cmd, commands.Group):
        for sub in dict.fromkeys(cmd.all_commands.values()):
            lines.extend(help_lines(sub))
    return lines


def section_commands(cog_name):
    if cog_name is None:
        return [c for c in dict.fromkeys(bot.all_commands.values()) if c.cog is None]
    cog = bot.get_cog(cog_name)
    return cog.get_commands() if cog else []


def help_detail(cmd, prefix):
    sig = f" {cmd.signature}" if cmd.signature else ""
    e = discord.Embed(title=f"/{cmd.qualified_name}", description=cmd_desc(cmd) or "No description.", color=HELP_COLOR)
    e.add_field(name="Usage", value=f"`/{cmd.qualified_name}{sig}`\n`{prefix}{cmd.qualified_name}{sig}`", inline=False)
    if cmd.aliases:
        e.add_field(name="Aliases", value=", ".join(f"`{a}`" for a in cmd.aliases))
    if isinstance(cmd, commands.Group):
        subs = "\n".join(f"`{s.name}` {clip(cmd_desc(s), 80)}" for s in dict.fromkeys(cmd.all_commands.values()))
        e.add_field(name="Subcommands", value=clip(subs, 1000) or "none", inline=False)
    return e


async def build_help(ctx):
    is_owner = await ctx.bot.is_owner(ctx.author)
    prefix = guild_prefix(ctx)
    pages = {}
    options = [discord.SelectOption(label="Home", value="home", emoji="🏠", description="Overview of all categories")]
    fields = []
    for cog_name, label, emoji, blurb in HELP_SECTIONS:
        if cog_name in HELP_OWNER_COGS and not is_owner:
            continue
        cmds = [c for c in section_commands(cog_name) if help_visible(c, is_owner)]
        if not cmds:
            continue
        lines = [line for c in cmds for line in help_lines(c)]
        e = discord.Embed(
            title=f"{emoji} {label}",
            description=clip(f"{blurb}\n\n" + "\n\n".join(lines), 4000),
            color=HELP_COLOR,
        )
        e.set_footer(text=f"{prefix}help <command> for details")
        key = cog_name or "general"
        pages[key] = e
        options.append(discord.SelectOption(label=label, value=key, emoji=emoji, description=clip(blurb, 100)))
        fields.append((f"{emoji} {label}", f"{len(cmds)} command{'s' if len(cmds) != 1 else ''}"))

    home = discord.Embed(
        title=f"{ctx.bot.user.name} help",
        description=(
            "Pick a category from the menu below, or use `/help <command>` for details on one command.\n"
            f"Slash: `/`  |  Prefix: `{prefix}`"
        ),
        color=HELP_COLOR,
    )
    home.set_thumbnail(url=ctx.bot.user.display_avatar.url)
    for name, value in fields:
        home.add_field(name=name, value=value)
    pages["home"] = home
    return pages, options


class HelpView(discord.ui.View):
    def __init__(self, author_id, pages, options):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.pages = pages
        self.message = None
        self.select = discord.ui.Select(placeholder="Choose a category", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This menu belongs to someone else. Run /help yourself.", ephemeral=True)
            return False
        return True

    async def on_select(self, interaction):
        await interaction.response.edit_message(embed=self.pages[self.select.values[0]], view=self)

    async def on_timeout(self):
        self.select.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


@bot.hybrid_command(name="help", description="Show the help menu, or details for one command.")
async def help_cmd(ctx, command: str = None):
    if command:
        name = command.strip()
        for p in ("/", ctx.clean_prefix):
            if p and name.startswith(p):
                name = name[len(p):]
        cmd = ctx.bot.get_command(name.strip().lower())
        if cmd is None or not help_visible(cmd, await ctx.bot.is_owner(ctx.author)):
            return await ctx.send("I don't know that command. Use `/help` to see the list.")
        return await ctx.send(embed=help_detail(cmd, guild_prefix(ctx)))
    pages, options = await build_help(ctx)
    view = HelpView(ctx.author.id, pages, options)
    view.message = await ctx.send(embed=pages["home"], view=view)


# ---------------- setup wizard ----------------

SETUP_PRESETS = {
    "Relaxed": {
        "window": 10,
        "limits": {"channel_delete": 2, "channel_create": 5, "role_delete": 2, "role_create": 5,
                   "ban": 3, "kick": 3, "webhook_create": 2, "bot_add": 0, "channel_update": 8, "role_update": 8},
        "punishment": "kick", "spam": (8, 5), "spam_action": "timeout", "spam_minutes": 10,
        "raid": (8, 10), "raid_action": "kick", "age": 0,
        "botspam": (20, 10), "webhook": (15, 10), "app": (4, 10),
    },
    "Recommended": {
        "window": 10,
        "limits": {"channel_delete": 1, "channel_create": 3, "role_delete": 1, "role_create": 3,
                   "ban": 2, "kick": 2, "webhook_create": 1, "bot_add": 0, "channel_update": 5, "role_update": 5},
        "punishment": "ban", "spam": (5, 5), "spam_action": "timeout", "spam_minutes": 10,
        "raid": (5, 10), "raid_action": "kick", "age": 3,
        "botspam": (15, 10), "webhook": (10, 10), "app": (3, 10),
    },
    "Strict": {
        "window": 10,
        "limits": {"channel_delete": 1, "channel_create": 2, "role_delete": 1, "role_create": 2,
                   "ban": 1, "kick": 1, "webhook_create": 1, "bot_add": 0, "channel_update": 2, "role_update": 2},
        "punishment": "ban", "spam": (4, 5), "spam_action": "timeout", "spam_minutes": 60,
        "raid": (3, 10), "raid_action": "ban", "age": 7,
        "botspam": (8, 10), "webhook": (5, 10), "app": (2, 10),
    },
}


def apply_setup_preset(guild, name):
    p = SETUP_PRESETS[name]
    cfg = STORE.get(guild.id)
    cfg["enabled"] = True
    cfg["punishment"] = p["punishment"]
    cfg["window"] = p["window"]
    cfg["limits"].update(p["limits"])
    am = cfg["automod"]
    am["enabled"] = True
    am["antispam"] = True
    am["spam_count"], am["spam_seconds"] = p["spam"]
    am["spam_action"], am["spam_timeout_minutes"] = p["spam_action"], p["spam_minutes"]
    rd = cfg["raid"]
    rd["auto"] = True
    rd["join_count"], rd["join_seconds"] = p["raid"]
    rd["action"], rd["min_account_age_days"] = p["raid_action"], p["age"]
    for key, (count, seconds) in (("botspam", p["botspam"]), ("webhookguard", p["webhook"]), ("appguard", p["app"])):
        section = cfg.setdefault(key, {"action": "timeout", "minutes": 60})
        section.update({"enabled": True, "count": count, "seconds": seconds})
    STORE.save()


def setup_summary(guild, name):
    p = SETUP_PRESETS[name]
    cfg = STORE.get(guild.id)
    lg = f"<#{cfg['log_channel']}>" if cfg.get("log_channel") else "not set (pick one below)"
    e = discord.Embed(title=f"Setup applied: {name}", color=0x2ECC71)
    e.add_field(name="Anti-nuke", value=(
        f"Punishment: `{p['punishment']}`\n"
        f"Channel delete: {p['limits']['channel_delete']} / Role delete: {p['limits']['role_delete']}\n"
        f"Bans: {p['limits']['ban']} / Kicks: {p['limits']['kick']}\n"
        f"Renames: {p['limits']['channel_update']} channel, {p['limits']['role_update']} role\n"
        f"Window: {p['window']}s"), inline=False)
    e.add_field(name="Anti-spam", value=f"More than {p['spam'][0]} msgs in {p['spam'][1]}s → `{p['spam_action']}`", inline=False)
    e.add_field(name="Anti-raid", value=f"More than {p['raid'][0]} joins in {p['raid'][1]}s → `{p['raid_action']}`\nAccounts under {p['age']} days blocked during a raid" if p['age'] else f"More than {p['raid'][0]} joins in {p['raid'][1]}s → `{p['raid_action']}`", inline=False)
    e.add_field(name="Bot, webhook and app guards", value=(
        f"Bot spam: {p['botspam'][0]} msgs / {p['botspam'][1]}s\n"
        f"Webhook: {p['webhook'][0]} msgs / {p['webhook'][1]}s\n"
        f"App: {p['app'][0]} msgs / {p['app'][1]}s"), inline=False)
    e.add_field(name="AutoMod rule check", value="On: disables rules that would block almost every message. Scan anytime with /automodcheck.", inline=False)
    e.add_field(name="Log channel", value=lg, inline=False)
    e.set_footer(text="Use /antinuke, /automod, /raid and the other commands to fine-tune any value.")
    return e


class SetupWizard(discord.ui.View):
    """Step-by-step setup in one message: level, log channel, trusted bots/people, review, apply."""

    def __init__(self, author_id):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.step = 0
        self.level = None
        self.log_channel = None
        self.trusted = []
        self.quarantine = False
        self.message = None
        self.build()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup belongs to someone else. Run /setup yourself.", ephemeral=True)
            return False
        return True

    def embed(self, guild=None):
        if self.step == 0:
            e = discord.Embed(
                title="Setup (1/4): protection level",
                description=(
                    "Pick how strict the protection should be. It sets anti-nuke, anti-spam, anti-raid, "
                    "and the bot, webhook and app guards together.\n\n"
                    "**Relaxed:** fewer false alarms, for busy servers with many bots.\n"
                    "**Recommended:** the right choice for most servers.\n"
                    "**Strict:** fastest response, for servers with few bots."
                ),
                color=0x5865F2,
            )
        elif self.step == 1:
            e = discord.Embed(
                title="Setup (2/4): log channel",
                description="Pick the channel where moderation and protection logs go. Optional, but strongly recommended.",
                color=0x5865F2,
            )
            e.add_field(name="Selected", value=f"<#{self.log_channel}>" if self.log_channel else "none", inline=False)
        elif self.step == 2:
            e = discord.Embed(
                title="Setup (3/4): trusted bots and people",
                description=(
                    "Pick bots or people the protection should ignore, such as your ticket bot or a co-owner. "
                    "Roles are ignored. Optional."
                ),
                color=0x5865F2,
            )
            e.add_field(name="Selected", value=", ".join(f"<@{i}>" for i in self.trusted) or "none", inline=False)
        else:
            e = discord.Embed(
                title="Setup (4/4): review",
                description="Check the choices below, then press Apply. Nothing changes until you press it. "
                            "Quarantine needs my role at the top of the role list, with the Quarantine role just below it.",
                color=0x5865F2,
            )
            e.add_field(name="Protection level", value=self.level or "not chosen", inline=True)
            e.add_field(name="Log channel", value=f"<#{self.log_channel}>" if self.log_channel else "none", inline=True)
            e.add_field(name="Trusted", value=", ".join(f"<@{i}>" for i in self.trusted) or "none", inline=False)
            e.add_field(name="Quarantine", value="on: role and channel lockout will be set up" if self.quarantine else "off", inline=False)
        return e

    def button(self, label, style, callback):
        b = discord.ui.Button(label=label, style=style)
        b.callback = callback
        self.add_item(b)

    def build(self):
        self.clear_items()
        if self.step == 0:
            self.button("Relaxed", discord.ButtonStyle.secondary, self.pick("Relaxed"))
            self.button("Recommended", discord.ButtonStyle.success, self.pick("Recommended"))
            self.button("Strict", discord.ButtonStyle.danger, self.pick("Strict"))
        elif self.step == 1:
            sel = discord.ui.ChannelSelect(
                placeholder="Pick a log channel", channel_types=[discord.ChannelType.text],
                min_values=0, max_values=1, row=0,
            )
            sel.callback = self.on_channel
            self.add_item(sel)
            self.button("Continue", discord.ButtonStyle.primary, self.go(2))
            self.button("Back", discord.ButtonStyle.secondary, self.go(0))
        elif self.step == 2:
            sel = discord.ui.MentionableSelect(
                placeholder="Pick trusted bots or people", min_values=0, max_values=10, row=0,
            )
            sel.callback = self.on_trusted
            self.add_item(sel)
            self.button("Continue", discord.ButtonStyle.primary, self.go(3))
            self.button("Back", discord.ButtonStyle.secondary, self.go(1))
        else:
            self.button("Quarantine: " + ("on" if self.quarantine else "off"),
                        discord.ButtonStyle.primary if self.quarantine else discord.ButtonStyle.secondary,
                        self.toggle_quarantine)
            self.button("Apply", discord.ButtonStyle.success, self.apply)
            self.button("Back", discord.ButtonStyle.secondary, self.go(2))
            self.button("Cancel", discord.ButtonStyle.danger, self.cancel)

    def pick(self, level):
        async def callback(interaction):
            self.level = level
            self.step = 1
            self.build()
            await interaction.response.edit_message(embed=self.embed(), view=self)
        return callback

    def go(self, step):
        async def callback(interaction):
            self.step = step
            self.build()
            await interaction.response.edit_message(embed=self.embed(), view=self)
        return callback

    async def on_channel(self, interaction):
        vals = interaction.data.get("values", [])
        self.log_channel = int(vals[0]) if vals else None
        await interaction.response.defer()

    async def on_trusted(self, interaction):
        self.trusted = [int(v) for v in interaction.data.get("values", [])]
        await interaction.response.defer()

    async def toggle_quarantine(self, interaction):
        self.quarantine = not self.quarantine
        self.build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def apply(self, interaction):
        try:
            await self._apply(interaction)
        except Exception as exc:
            print(f"[setup] apply failed: {exc!r}")
            await interaction.followup.send(
                f"Setup failed: {clip(str(exc) or type(exc).__name__, 300)}. Run /setup again after fixing it.",
                ephemeral=True,
            )

    async def _apply(self, interaction):
        await interaction.response.defer()
        g = interaction.guild
        apply_setup_preset(g, self.level)
        cfg = STORE.get(g.id)
        if self.log_channel:
            cfg["log_channel"] = self.log_channel
        added = 0
        for uid in self.trusted:
            if g.get_role(uid) is None and uid not in cfg["whitelist"]:
                cfg["whitelist"].append(uid)
                added += 1
        STORE.save()
        e = setup_summary(g, self.level)
        e.add_field(name="Trusted", value=f"{added} added to the whitelist" if added else "none", inline=False)
        if self.quarantine:
            try:
                role, locked, failed = await quarantine_setup(g)
                e.add_field(name="Quarantine", value=f"Role {role.mention} ready, {locked} channel(s) hidden"
                            + (f", {failed} failed" if failed else ""), inline=False)
            except Exception as exc:
                e.add_field(name="Quarantine", value=f"Could not set up quarantine: {clip(str(exc) or type(exc).__name__, 200)}. Check Manage Roles and Manage Channels.", inline=False)
        self.disable()
        await interaction.edit_original_response(embed=e, view=self)

    async def cancel(self, interaction):
        self.disable()
        e = discord.Embed(title="Setup cancelled", description="Nothing was changed.", color=0x95A5A6)
        await interaction.response.edit_message(embed=e, view=self)

    def disable(self):
        for item in self.children:
            item.disabled = True
        self.stop()

    async def on_timeout(self):
        self.disable()
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


@bot.hybrid_command(name="setup", description="Guided setup for anti-nuke, anti-spam and anti-raid.")
@commands.guild_only()
async def setup_cmd(ctx):
    p = ctx.author.guild_permissions
    if not (p.administrator or p.manage_guild or ctx.author.id == ctx.guild.owner_id or await ctx.bot.is_owner(ctx.author)):
        raise commands.MissingPermissions(["manage_guild"])
    view = SetupWizard(ctx.author.id)
    view.message = await ctx.send(embed=view.embed(), view=view)


@bot.hybrid_command(name="botstatus")
@commands.is_owner()
async def botstatus(
    ctx,
    state: Literal["online", "idle", "dnd", "invisible"],
    kind: Literal["playing", "watching", "listening", "competing", "none"] = "playing",
    *,
    text: str = "",
):
    """Change the bot's status and activity (bot owner only)."""
    data = {"state": state, "kind": kind, "text": text}
    await apply_presence(data)
    with open(PRESENCE_PATH, "w") as f:
        json.dump(data, f)
    await ctx.send(f"Status set: `{state}` / `{kind}` {text}".strip())


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    if os.path.exists(PRESENCE_PATH):
        try:
            with open(PRESENCE_PATH) as f:
                await apply_presence(json.load(f))
        except Exception:
            pass


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.NotOwner):
        return await ctx.send("Only the bot owner can use this.")
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send("You're missing: " + ", ".join(error.missing_permissions))
    if isinstance(error, commands.BotMissingPermissions):
        return await ctx.send("I'm missing: " + ", ".join(error.missing_permissions))
    if isinstance(error, commands.CheckFailure):
        return await ctx.send("You don't have permission to use this command.")
    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        return await ctx.send(f"Invalid usage. Try `{ctx.clean_prefix}help {ctx.command}`.")
    if isinstance(error, commands.CommandNotFound):
        return
    orig = error
    while hasattr(orig, "original"):
        orig = orig.original
    if isinstance(orig, discord.Forbidden):
        return await ctx.send("I don't have permission to do that.")
    raise error


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set. Add it to the panel variables or to .env")
    bot.run(token)
