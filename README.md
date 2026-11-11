# Advanced Anti-Nuke Discord Bot

Protects a Discord server from raids, nukes, bot abuse and mass destruction.

**Support server:** https://discord.gg/GHyc7GyNvX
**Upvote:** https://discordbotlist.com/bots/nobara

[![Deploy on Bot-Hosting](https://bot-hosting.net/assets/deploy-badge.svg)](https://bot-hosting.net/deploy?source=template&template=advanced-antinuke-discord-bot)

## What it protects against

- **Nukes:** mass channel, role, emoji and sticker creation or deletion, mass bans and kicks, mass renames.
- **Slow attacks:** destructive actions spread over an hour (`/slowburn`).
- **Dangerous permission grants:** anyone who gives admin, manage server, manage roles, ban, kick or webhook rights is stripped at once.
- **Bots:** an added bot that nukes is banned, and the person who added it is punished too (unless trusted).
- **Webhooks and AutoMod:** webhook spam is deleted, and AutoMod rules that block almost everything are disabled.
- **Raids and spam:** join floods, account age checks, message bursts and bot spam.
- **Nuke bots:** known nuke bots are shared through the nuke-bot database and banned on sight.
- **Warnings:** the owner can set punishments at a number of warnings (`/warnpunish`).

## Requirements

| Item | Needed? | What it is for |
|---|---|---|
| `DISCORD_TOKEN` | **Required** | The bot's login token from the Discord Developer Portal. |
| Bot permissions | **Required** | Administrator is simplest. At minimum: View Audit Log, Manage Roles, Manage Channels, Ban Members, Kick Members, Moderate Members, Manage Webhooks, Manage Messages. |
| Bot role above staff roles | **Required** | Discord won't let the bot punish anyone whose role is higher than its own. |
| `OWNER_IDS` | Optional | Extra bot owner IDs, comma-separated. The Developer Portal owner is always an owner. |
| `DATABASE_URL` | Optional | PostgreSQL connection string. Without it, data is kept in local files. |
| `GITHUB_REPO` and `GITHUB_TOKEN` | Optional | Publishes the code, README and LICENSE to a GitHub repo after changes. |
| `DISCORDBOTLIST_TOKEN` | Optional | Posts the bot's commands to discordbotlist.com on each start. |
| `NUKEDB_MIN_ACTIONS` | Optional | How many destructive actions get a bot listed in the nuke-bot database (default 5). |

## How to host it on bot-hosting.net

1. Click the **Deploy on Bot-Hosting** button above, or create a Python bot there and upload the files.
2. Upload `main.py` (required). Upload `autofix.py` only if you want the AI features.
3. Set the startup file to `main.py`, and make sure the host runs Python 3.11 or newer.
4. Add the variables from the table in the bot's settings. Start with `DISCORD_TOKEN`.
5. Start the bot. The console should show `Logged in as ...`.
6. Invite the bot with Administrator, or with the permissions listed above, and put its role at the top of the role list.
7. In your server, run `/setup` and follow the four steps. Then run `/help`.

## Specialities

- **Works on its own.** The protection needs no external service: only the Discord token and the permissions above.
- **Feature suggestions and bug reports** go to the bot owners by DM (`/request` and `/bug`).
- **Premium features** such as forensics, reports, role backup and keys are listed in `/help` and tell you how to get premium.

## License

MIT. See the LICENSE file.

## Commands

- `/alerts`: Premium: owner DMs for serious incidents.
- `/antinuke`: Show anti-nuke status.
- `/antinuke limit`: Set allowed actions per window. Example: .antinuke limit ban 2
- `/antinuke logchannel`: Set log channel (no argument clears it).
- `/antinuke off`: Disable anti-nuke.
- `/antinuke on`: Enable anti-nuke.
- `/antinuke punishment`: Set punishment: ban, kick, strip (removes dangerous roles), or quarantine (hides every channel).
- `/antinuke status`: Show anti-nuke status.
- `/antinuke whitelist`: List whitelisted users.
- `/antinuke whitelist add`: Whitelist a user.
- `/antinuke whitelist list`: List whitelisted users.
- `/antinuke whitelist remove`: Remove a user from the whitelist.
- `/antinuke window`: Set the detection window in seconds (3-300).
- `/antispam`: Show anti-spam settings.
- `/antispam off`: Disable anti-spam.
- `/antispam on`: Enable anti-spam.
- `/antispam punishment`: Set the punishment. Minutes only applies to timeout.
- `/antispam set`: Set the limit: more than <messages> in <seconds> triggers the punishment.
- `/antispam status`: Show anti-spam settings.
- `/apiinfo`: Show API and bot info.
- `/appguard`: Show App Guard settings.
- `/appguard debug`: Post raw details about app messages in this server, to diagnose App Guard.
- `/appguard off`: Turn App Guard off.
- `/appguard on`: Turn App Guard on.
- `/appguard punishment`: Set the punishment for the user behind the app. Minutes only apply to timeout.
- `/appguard set`: Set the trigger: more than <messages> in <seconds> from one app user.
- `/appguard status`: Show App Guard settings.
- `/automod`: Show automod settings.
- `/automod action`: What to do on a violation (message is always deleted).
- `/automod badword`: List blocked words.
- `/automod badword add`: Add a blocked word or phrase.
- `/automod badword list`: List blocked words.
- `/automod badword remove`: Remove a blocked word or phrase.
- `/automod mentions`: Max user mentions allowed per message.
- `/automod off`: Disable automod.
- `/automod on`: Enable automod.
- `/automod spam`: Spam threshold: more than <count> messages in <seconds>.
- `/automod status`: Show automod settings.
- `/automod timeout`: Timeout length in minutes when action is timeout.
- `/automod toggle`: Toggle a filter on/off.
- `/automodcheck`: Scan AutoMod rules and disable any that would block almost every message.
- `/backup`: Premium: export or import bot settings.
- `/ban`: Ban a user (mention or ID).
- `/boost`: Show boost message and reward role settings.
- `/boost addrole`: Give this role to members when they boost.
- `/boost channel`: Set the boosters channel for boost messages.
- `/boost disable`: Stop sending boost messages.
- `/boost removerole`: Stop giving this role to boosters.
- `/boost status`: Show boost message and reward role settings.
- `/boost test`: Send a sample boost message to the boosters channel, as if this member boosted.
- `/botspam`: Show Bot Spam Guard settings.
- `/botspam off`: Turn Bot Spam Guard off.
- `/botspam on`: Turn Bot Spam Guard on.
- `/botspam set`: Set the trigger: more than <messages> in <seconds> from one bot.
- `/botspam status`: Show Bot Spam Guard settings.
- `/botstatus`: Change the bot's status and activity (bot owner only).
- `/bug`: Report a bug to the bot owners.
- `/cases`: Premium: per-user moderation history.
- `/clearwarns`: Clear all warnings for a member.
- `/discordautomod`: Show the Discord AutoMod rules this bot manages.
- `/discordautomod remove`: Delete every Discord AutoMod rule this bot manages.
- `/discordautomod status`: Show the Discord AutoMod rules this bot manages.
- `/discordautomod sync`: Create or update Discord AutoMod rules from this bot's word, mention and spam settings.
- `/errors`: Turn error alerts to bot owners on or off.
- `/forensics`: Premium: incident reports and replays.
- `/help`: Show the help menu, or details for one command.
- `/invite`: Get the bot invite link.
- `/kick`: Kick a member.
- `/lock`: Lock this channel for @everyone.
- `/lockdown`: Premium: emergency lockdown and release.
- `/logs`: Show where each type of log is sent.
- `/logs bots`: Choose whether actions by bots are ignored.
- `/logs disable`: Stop logging a category (or all of them).
- `/logs ignore`: Toggle ignoring a channel for message and reaction logs.
- `/logs set`: Send a log category (or all of them) to a channel.
- `/logs status`: Show where each type of log is sent.
- `/nukedb`: Show the malicious bot database summary.
- `/nukedb add`: Manually list a bot. It is banned in every server it is in.
- `/nukedb allow`: Protect a bot: it can never be auto-listed or banned by the database.
- `/nukedb info`: Show details about a listed bot.
- `/nukedb list`: List bots in the database, and the allowlisted bots with their names.
- `/nukedb release`: Remove a bot from the database AND unban it in every server the database banned it in.
- `/nukedb remove`: Remove a bot from the database (does not unban it).
- `/nukedb status`: Show the malicious bot database summary.
- `/nukedb sync`: Push this bot's whole database to the sync channel so the other bot gets it.
- `/nukedb unallow`: Remove a bot from the allowlist.
- `/onboarding`: Turn the setup DM to new server owners on or off.
- `/owners`: List the main owner and extra owners.
- `/owners add`: Give a user full bot-owner access.
- `/owners list`: List the main owner and extra owners.
- `/owners remove`: Remove an extra owner. Main owners can't be removed here.
- `/ping`: Check the bot's latency.
- `/prefix`: Show or change the bot prefix for this server.
- `/privacy`: Premium: export or delete this server's stored data.
- `/purge`: Delete recent messages (1-100).
- `/quarantine`: Show the Quarantine role and how many members are quarantined.
- `/quarantine add`: Quarantine a member manually.
- `/quarantine list`: List quarantined members.
- `/quarantine release`: Release a member and give back the roles they had before quarantine.
- `/quarantine setup`: Create the Quarantine role, hide every channel from it, and place it under my role.
- `/quarantine status`: Show the Quarantine role and how many members are quarantined.
- `/raid`: Show anti-raid settings.
- `/raid accountage`: Remove joiners whose account is younger than <days> (0 = off).
- `/raid action`: What to do with joiners during a raid.
- `/raid auto`: Turn automatic join-flood detection on or off.
- `/raid duration`: How long raid mode lasts.
- `/raid off`: Stop raid mode.
- `/raid on`: Manually start raid mode.
- `/raid status`: Show anti-raid settings.
- `/raid threshold`: Trigger raid mode on more than <joins> joins within <seconds>.
- `/redeem`: Premium: redeem a premium key.
- `/report`: Premium: weekly security report with charts.
- `/request`: Suggest a feature to the bot owners.
- `/roleadd`: Give a role to a member.
- `/rolebackup`: Premium: save and restore member roles.
- `/roleremove`: Remove a role from a member.
- `/screening`: Premium: new-member screening with a DM button.
- `/securityaudit`: Premium: exportable security audit report.
- `/securityscore`: Premium: server security score with fixes.
- `/setup`: Guided setup for anti-nuke, anti-spam and anti-raid.
- `/slowburn`: Owner: destructive actions per hour before a punishment.
- `/slowmode`: Set channel slowmode in seconds (0 to disable).
- `/suggestions`: Bot owners: open suggestions and bug reports.
- `/suggestions close`: Bot owners: close an item.
- `/suggestions open`: Bot owners: open suggestions and bug reports.
- `/support`: Get the support server link.
- `/tempban`: Premium: temporary bans that expire automatically.
- `/timeout`: Timeout a member (e.g. 10m, 2h, 1d).
- `/trust`: Premium: trust a bot for specific actions only.
- `/unban`: Unban a user by ID.
- `/unlock`: Unlock this channel for @everyone.
- `/untimeout`: Remove a member's timeout.
- `/vote`: Upvote the bot, and get 24 hours without tips.
- `/warn`: Warn a member.
- `/warnings`: Show a member's warnings.
- `/warnpunish`: Show the warning rules.
- `/warnpunish add`: Add a rule: at this many warnings, timeout (minutes), kick or ban the member.
- `/warnpunish remove`: Remove the rule for this number of warnings.
- `/warnpunish show`: Show the warning rules.
- `/webhookguard`: Show Webhook Guard settings.
- `/webhookguard off`: Turn Webhook Guard off.
- `/webhookguard on`: Turn Webhook Guard on.
- `/webhookguard punishment`: Set the punishment for whoever created the webhook. Minutes only apply to timeout.
- `/webhookguard set`: Set the trigger: more than <messages> in <seconds> from one webhook.
- `/webhookguard status`: Show Webhook Guard settings.

_This README is updated automatically whenever the bot code changes (2026-11-11)._
