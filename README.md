# project-Antinuke
an discord antinuke and antiraid project.
you can freely fork it and edit features and code using ai. 
you can also use ai and update syntax if discord.py drops new update and this one doesn't work.
Here are the slash commands for the free bot's README. Paid-only commands are left out.

## Commands

### General
- `/help [command]`: shows the help menu, or details for one command.
- `/setup`: step-by-step setup wizard for protection levels, log channel and trusted users.
- `/prefix [new_prefix]`: shows or changes the server's prefix. Needs Manage Server. Use `reset` to restore the default.
- `/ping`: shows the bot's gateway and API latency.
- `/apiinfo`: shows library, Python and Discord API versions, uptime, and server and user counts.
- `/support`: link to the support server.
- `/invite`: link to add the bot to another server.

### Anti-nuke (`/antinuke`)
Protects against mass channel, role, ban, kick, webhook and emoji actions. Server owner or bot owner.
- `/antinuke`: shows the current settings.
- `/antinuke on` / `off`: turns anti-nuke on or off.
- `/antinuke punishment <ban|kick|strip>`: what happens to a user who breaks a limit. `strip` removes their dangerous roles.
- `/antinuke limit <action> <count>`: how many times an action is allowed in the window.
- `/antinuke window <seconds>`: the time window for counting actions.
- `/antinuke logchannel <#channel>`: where anti-nuke events are logged.
- `/antinuke whitelist add <user>` / `remove <user>`: exempts a user or bot from anti-nuke.

### Moderation
- `/ban <user> [reason]`, `/unban <user>`, `/kick <member> [reason]`
- `/timeout <member> <duration> [reason]`, `/untimeout <member>`
- `/warn <member> [reason]`, `/warnings <member>`, `/clearwarns <member>`
- `/purge <amount>`: deletes recent messages.
- `/slowmode <seconds>`: sets slowmode in the current channel.
- `/lock` / `/unlock`: locks or unlocks the current channel for @everyone.
- `/roleadd <member> <role>` / `/roleremove <member> <role>`: gives or removes a role.

### AutoMod (`/automod`)
The bot's own message filters. Needs Manage Server.
- `/automod`: shows the current settings.
- `/automod on` / `off` / `toggle`: turns AutoMod on, off, or flips it.
- `/automod action <delete|warn|timeout>`: what happens on a violation.
- `/automod timeout <minutes>`: timeout length.
- `/automod spam`, `/automod mentions`: configure spam and mention filters.
- `/automod badword add <word>` / `remove <word>`: manages the blocked word list.

### Anti-spam (`/antispam`)
Needs Manage Server.
- `/antispam`: shows the current settings.
- `/antispam on` / `off`: turns it on or off.
- `/antispam set <messages> <seconds>`: how many messages in how many seconds count as spam.
- `/antispam punishment <action> [minutes]`: what happens to spammers.

### Anti-raid (`/raid`)
Needs Manage Server.
- `/raid`: shows the current settings.
- `/raid on` / `off` / `auto`: turns raid protection on, off, or automatic.
- `/raid action <action>`: what happens to raiders.
- `/raid threshold <joins>`, `/raid duration <seconds>`: how many joins in how long count as a raid.
- `/raid accountage <days>`: blocks accounts younger than this during a raid.

### App guard (`/appguard`)
Punishes the user behind a user-installed app that sends a burst of messages. Needs Manage Server.

- `/appguard`: shows the current settings.
- `/appguard on` / `off`.
- `/appguard set <messages> <seconds>`.
- `/appguard punishment <timeout|kick|ban> [minutes]`.
- `/appguard debug <on|off>`: posts raw details about app messages, for troubleshooting.

### Webhook guard (`/webhookguard`)
Deletes a webhook that sends a burst of messages, removes its messages, and punishes its creator. Needs Manage Server.
- `/webhookguard`: shows the current settings.
- `/webhookguard on` / `off`.
- `/webhookguard set <messages> <seconds>`.
- `/webhookguard punishment <timeout|kick|ban> [minutes]`.

### Bot spam guard (`/botspam`)
Bans a bot account that floods messages and deletes its messages. Needs Manage Server.
- `/botspam`: shows the current settings.
- `/botspam on` / `off`.
- `/botspam set <messages> <seconds>`.

### Discord AutoMod (`/discordautomod`)
Syncs word, mention and spam settings to Discord's built-in AutoMod. Needs Manage Server.
- `/discordautomod`: lists the rules the bot manages.
- `/discordautomod sync`: creates or updates those rules.
- `/discordautomod remove`: deletes them.

### Logs (`/logs`) (bot owner only)
- `/logs`: shows where each log type goes.
- `/logs set <category|all> <#channel>`: sends a log type to a channel. Categories: messages, members, voice, server, moderation, commands, reactions.
- `/logs disable <category|all>`.
- `/logs bots <on|off>`: whether bot actions are logged.
- `/logs ignore <#channel>`: stops logging a channel. Run again to undo.

### Boosts (`/boost`) (Manage Server)
- `/boost`: shows the boost channel and reward roles.
- `/boost channel <#channel>`, `/boost disable`.
- `/boost addrole <role>`, `/boost removerole <role>`: roles given to boosters.
- `/boost test <start|stop>`: sends a sample message.

### Nuke bot database (`/nukedb`) (bot owner only)
- `/nukedb`: summary of listed and allowlisted bots.
- `/nukedb list [page]`: lists listed bots and allowlisted bots with names.
- `/nukedb info <bot id>`.
- `/nukedb add <bot id> [reason]`: lists a bot manually.
- `/nukedb remove <bot id>`.
- `/nukedb release <bot id>`: removes a listing and unbans the bot where the database banned it.
- `/nukedb allow <bot id>` / `unallow <bot id>`: protects a bot from being listed.
- `/nukedb sync`: pushes the local list to the shared database.

### Owners (`/owners`) (main owner only)
- `/owners`: lists main and extra owners.
- `/owners add <user>` / `remove <user>`: extra owners get bot-owner commands but can't manage owners.

### Bot status (`/botstatus`) (bot owner only)
- `/botstatus`: shows the bot's status.

---

Most commands need Manage Server, or the server owner. Commands marked bot owner or main owner are limited to those users.
