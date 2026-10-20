# project-Antinuke
an discord antinuke and antiraid project.
you can freely fork it and edit features and code using ai. 
you can also use ai and update syntax if discord.py drops new update and this one doesn't work.
# Nobara Antinuke (Free)

A Discord bot that protects servers from nukes, raids, spam, webhook floods and bot floods. It bans or quarantines attackers, shares a database of known nuke bots, and keeps your Discord AutoMod rules in sync with the bot's settings.

## Features

- **Anti-nuke:** limits mass channel, role, emoji and sticker creates and deletes, bans, kicks, renames and nickname changes. Punishes the user or bot responsible: ban, kick, strip dangerous roles, or quarantine.
- **Dangerous permissions guard:** if someone who isn't trusted gives out admin, manage server, manage roles, manage channels, ban, kick or manage webhooks, their roles are stripped (bots are banned).
- **Quarantine (Wick-style):** a Quarantine role that hides every channel. Roles are saved and restored on release.
- **Anti-raid:** detects join floods and responds with kick or ban, with optional account-age checks.
- **Anti-spam, AutoMod and bad words:** the bot's own message filters (invites, links, bad words, mentions, spam).
- **Discord AutoMod sync:** creates Discord's built-in AutoMod rules from your settings and logs what Discord blocks.
- **App guard:** punishes the user behind a user-installed app that sends a burst of messages, and deletes their spam.
- **Webhook guard:** deletes a webhook that bursts messages, removes its messages, and punishes its creator.
- **Bot spam guard:** bans a bot account that floods messages.
- **Nuke bot database:** lists bots caught nuking, bans them in every server, and shares the list across your bots through PostgreSQL.
- **Logs:** sends message, member, voice, reaction, command, moderation and server events to channels you choose.
- **Boosts:** announces boosts and gives reward roles.
- **Setup wizard:** `/setup` configures protection in four steps.

## Commands

All commands work as slash commands (`/command`) and as prefix commands (default `.`). Prefix can be changed per server with `/prefix`.

### General
- `/help [command]`: help menu, or details for one command.
- `/setup`: step-by-step setup wizard: protection level, log channel, trusted bots and people, and optional quarantine.
- `/prefix [new_prefix]`: show or change the prefix. Needs Manage Server. `reset` restores `.`.
- `/ping`: gateway and API latency.
- `/apiinfo`: library and Python versions, uptime, server and user counts.
- `/support`, `/invite`: support server and invite links.

### Anti-nuke (`/antinuke`)
Server owner or bot owner.
- `/antinuke`: show settings.
- `/antinuke on` / `off`
- `/antinuke punishment <ban|kick|strip|quarantine>`: what happens to a user who breaks a limit.
- `/antinuke limit <action> <count>`: allowed actions per window. Actions: `channel_create`, `channel_delete`, `channel_update`, `role_create`, `role_delete`, `role_update`, `ban`, `kick`, `webhook_create`, `bot_add`, `emoji_create`, `emoji_delete`, `sticker_create`, `sticker_delete`, `nick_change`.
- `/antinuke window <seconds>`: the counting window.
- `/antinuke logchannel <#channel>`
- `/antinuke whitelist add <user>` / `remove <user>`: exempts a user or bot.

### Quarantine (`/quarantine`)
Needs Manage Server.
- `/quarantine`: show the role and how many members are quarantined.
- `/quarantine setup`: creates the Quarantine role if missing, hides every channel from it, and places it below the bot's role.
- `/quarantine add <member> [reason]`
- `/quarantine release <member>`: restores the member's saved roles.
- `/quarantine list`

### Moderation
- `/ban <user> [reason]`, `/unban <user>`, `/kick <member> [reason]`
- `/timeout <member> <duration> [reason]`, `/untimeout <member>`: durations like `10m`, `2h`, `1d`.
- `/warn <member> [reason]`, `/warnings <member>`, `/clearwarns <member>`
- `/purge <amount>`, `/slowmode <seconds>`, `/lock`, `/unlock`
- `/roleadd <member> <role>`, `/roleremove <member> <role>`

### AutoMod (`/automod`)
The bot's own message filters. Needs Manage Server.
- `/automod`: show settings. `on`, `off`, `toggle`
- `/automod action <delete|warn|timeout>`, `/automod timeout <minutes>`
- `/automod spam`, `/automod mentions`
- `/automod badword add <word>` / `remove <word>`

### Anti-spam (`/antispam`)
Needs Manage Server.
- `/antispam`, `on`, `off`, `set <messages> <seconds>`, `punishment <action> [minutes]`

### Discord AutoMod (`/discordautomod`)
Needs Manage Server. Creates rules named `[Bot] Words`, `[Bot] Mentions` and `[Bot] Spam` from your settings.
- `/discordautomod`: list the rules the bot manages.
- `/discordautomod sync`: create or update them. Run again after changing settings.
- `/discordautomod remove`: delete them.

### Anti-raid (`/raid`)
Needs Manage Server.
- `/raid`, `on`, `off`, `auto`, `action`, `threshold`, `duration`, `accountage`

### App guard (`/appguard`)
Needs Manage Server.
- `/appguard`, `on`, `off`, `set <messages> <seconds>`, `punishment <timeout|kick|ban> [minutes]`, `debug <on|off>`

### Webhook guard (`/webhookguard`)
Needs Manage Server.
- `/webhookguard`, `on`, `off`, `set <messages> <seconds>`, `punishment <timeout|kick|ban> [minutes]`

### Bot spam guard (`/botspam`)
Needs Manage Server.
- `/botspam`, `on`, `off`, `set <messages> <seconds>`

### Logs (`/logs`)
Bot owner only.
- `/logs`: show where each log type goes.
- `/logs set <category|all> <#channel>`: categories are `messages`, `members`, `voice`, `server`, `moderation`, `commands`, `reactions`.
- `/logs disable <category|all>`
- `/logs bots <on|off>`: whether bot actions are logged.
- `/logs ignore <#channel>`: stop logging a channel. Run again to undo.

### Boosts (`/boost`)
Needs Manage Server.
- `/boost`, `channel <#channel>`, `disable`, `addrole <role>`, `removerole <role>`, `test <start|stop>`

### Nuke bot database (`/nukedb`)
Bot owner only.
- `/nukedb`: summary. `list [page]`: listed bots and allowlisted bots with names.
- `/nukedb info <bot id>`, `add <bot id> [reason]`, `remove <bot id>`
- `/nukedb release <bot id>`: removes a listing and unbans the bot where it was banned.
- `/nukedb allow <bot id>` / `unallow <bot id>`: protects a bot from being listed.
- `/nukedb sync`: pushes the local list to the shared database.

### Owners (`/owners`)
Main owner only. Extra owners get bot-owner commands but can't manage owners.
- `/owners`: list owners. `add <user>`, `remove <user>`

### Bot status
- `/botstatus`: bot owner only.

## Setup

### 1. Create the bot
1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Under **Bot**, turn on **Server Members Intent** and **Message Content Intent**.
3. Copy the bot token. Keep it secret.
4. Invite the bot with the `bot` and `applications.commands` scopes, and Administrator or the permissions listed below.

### 2. Permissions the bot needs
- Manage Roles, Manage Channels, Manage Webhooks, Manage Messages
- Ban Members, Kick Members, Moderate Members (timeouts)
- View Audit Log, Read Message History
- Manage Server (for Discord AutoMod rules and the AutoMod sync)
- Send Messages, Embed Links

Keep the bot's role **at the top** of the role list. Quarantine and punishments only work on roles below it. Put the Quarantine role just below the bot's role.

### 3. Environment variables
Set these in your host's variable settings, or in a `.env` file next to `main.py`.

| Variable | Required | What it does |
|---|---|---|
| `DISCORD_TOKEN` | yes | The bot token |
| `OWNER_IDS` | no | Comma-separated user IDs with bot-owner commands. If empty, the Developer Portal owner or team is used. |
| `DATABASE_URL` | no | PostgreSQL connection string for sharing the nuke list between bots. See below. |
| `NUKEDB_MIN_ACTIONS` | no | Destructive actions needed before an unverified bot is listed. Default 5. |
| `NUKEDB_VERIFIED_MIN_ACTIONS` | no | Same for verified bots. Default 10. |

Data files are created automatically next to `main.py`. Keep them when you update the bot: `config.json`, `nukedb.json`, `owners.json`, `quarantine.json`.

### 4. Install and run
```bash
pip install -r requirements.txt
python main.py
```
Set the startup file to `main.py`. The bot needs `discord.py` 2.7.1 or newer.

## Shared nuke database (PostgreSQL)

Each person running the bot should use **their own** database. Don't share one, since it holds your listed bots and anyone with the connection string can change it.

1. Create a free PostgreSQL database. [Supabase](https://supabase.com) and [Neon](https://neon.tech) both offer free tiers.
2. Copy the connection string: `postgresql://user:password@host:5432/dbname`.
3. Set it as `DATABASE_URL` on your host.
4. Restart. The `nukedb` schema is created automatically.
5. Run `/nukedb`. It should say **PostgreSQL (connected)**.

If you don't set `DATABASE_URL`, the nuke list stays in a local file. That works for one bot on one host. You only lose sharing it with other bots.

## Security

- **Never commit your token or `DATABASE_URL`.** Keep them in host variables or a `.env` file.
- Add a `.gitignore` that excludes `.env` and the data files (`config.json`, `nukedb.json`, `owners.json`, `quarantine.json`).
- Use `.env.example` with placeholder values, so others know which variables to set.
- Only give owner-level access (`OWNER_IDS`, `/owners add`) to people you fully trust. Owners can list or release nuke bots, which bans or unbans bots in every server the bot is in.

## Troubleshooting

- **Commands missing:** slash commands can take up to an hour to appear after a restart. Prefix commands work right away.
- **`DISCORD_TOKEN is not set`:** add the token variable and restart.
- **Database says "local file only":** check `DATABASE_URL`, and that the database accepts connections from your host.
- **Punishments or quarantine fail:** check the bot's role position and permissions.
- **`429 Too Many Requests` at startup:** the host's IP is temporarily blocked by Discord after repeated restarts. Stop the bot for 30 minutes or more, then start it once.
