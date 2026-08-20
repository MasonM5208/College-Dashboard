# Operations: keeping it running

For the owner, once a month, and whenever something looks wrong.

Everything here assumes you are connected to the server. From your Mac's Terminal,
replacing the address with yours:

```
ssh mason@100.x.y.z
```

Then move into the code folder, which is where every `docker compose` command has
to be run from:

```
cd /home/mason/College-Dashboard
```

---

## The five-minute monthly check

**Why:** the failure that hurts is not a crash, it is a sync that stopped quietly
three weeks ago while the dashboard kept showing what it knew in September.

1. **Open the dashboard** on your phone. Under **Automatic jobs**, every entry
   should say `ok`. Anything marked `stale` or `failing` is the whole point of the
   check — go to the matching section below.

2. **Confirm the container is healthy:**

   ```
   sudo docker compose ps
   ```

   Expected:

   ```
   NAME        IMAGE                      STATUS                    PORTS
   dashboard   college-dashboard:local    Up 12 days (healthy)      100.x.y.z:8000->8000/tcp
   ```

   `(healthy)` is what you want. `(unhealthy)` or a `STATUS` that keeps resetting
   to `Up 10 seconds` means it is crashing and restarting — see **The dashboard
   keeps restarting** below.

3. **Confirm backups are landing off-site.** In the Backblaze web interface, click
   **Buckets**, then **Browse Files**, then the `dashboard` folder. The newest file
   should be from last night.

4. **Check free disk space:**

   ```
   df -h /
   ```

   Expected — the `Use%` column well under 80%:

   ```
   Filesystem      Size  Used Avail Use% Mounted on
   /dev/vda1        40G  4.2G   34G  12% /
   ```

5. **Note the date.** Every few months, do the restore test in `SETUP.md` Section
   13 again and record it in the log at the bottom of this file.

---

## The site will not load

Work down this list in order. Each step tells you where to go next.

### Step 1 — Is Tailscale on?

The dashboard is unreachable without it, and iOS sometimes turns VPN
configurations off after an update.

- **On the iPhone:** open the Tailscale app. The switch at the top should be green.
- **On the Mac:** the Tailscale icon in the menu bar at the top of the screen
  should not be greyed out.

Then try the dashboard again. If it loads, that was the problem.

### Step 2 — Can you reach the server at all?

From your Mac's Terminal:

```
ping -c 3 100.x.y.z
```

- **`0.0% packet loss`** — the server is up and reachable. Go to step 3.
- **`100.0% packet loss` or `Request timeout`** — the server is off, or Tailscale
  is not running on it. Sign in to `my.vultr.com`, open the server's page, and
  check its status says **Running**. If it does, use the **View Console** button to
  log in and run `sudo tailscale up`. If it says stopped, start it.

### Step 3 — Is the dashboard running?

Connect to the server, then:

```
sudo docker compose ps
```

- **No rows at all** — it is not running. Start it:

  ```
  sudo docker compose up -d
  ```

- **`STATUS` says `Exited`** — it tried to start and failed. Go to step 4.
- **`STATUS` says `Up ... (healthy)`** — it is running and believes it is fine. Go
  to step 5.

### Step 4 — What did it say when it failed?

```
sudo docker compose logs --tail 50
```

Look for the last few lines before it stopped.

- **A block of `=` characters around `DATABASE MIGRATION FAILED`** — see **A
  migration failed** below.
- **`env file /etc/college-dashboard/env not found`** — the secrets file is gone or
  renamed. See `docs/SECRETS.md`.
- **`required variable TAILNET_BIND_IP is missing a value`** — the `.env` file in
  the code folder is missing. `SETUP.md` Section 10 step 2 recreates it.
- **`cannot assign requested address`** — the server's Tailscale address changed.
  Run `tailscale ip -4`, put the new address in `.env`, and run
  `sudo docker compose up -d`.

### Step 5 — Is it listening where you think?

```
sudo ss -ltnp | grep 8000
```

Expected — your `100.` address:

```
LISTEN 0  4096  100.x.y.z:8000  0.0.0.0:*  users:(("docker-proxy",pid=4412,fd=7))
```

If the address shown is not the one you are typing into your browser, use the one
shown here. If you see `0.0.0.0:8000`, the dashboard is exposed to the public
internet — stop it immediately with `sudo docker compose down` and fix
`TAILNET_BIND_IP` in `.env` before starting it again.

### Step 6 — Ask the dashboard directly

```
curl -s "http://$(tailscale ip -4):8000/healthz"
```

The `$(tailscale ip -4)` part fills in this server's private address. That address
is required — the dashboard listens on the tailnet address only, so `localhost`
gets no answer even when everything is working.

- **`"ok":true`** — the dashboard is fine and the problem is between your device
  and the server. Go back to step 1.
- **`"ok":false`** — one of the checks failed. The `checks` section names which
  one, and each has a section below.
- **`Connection refused`** — it is not actually listening. Go to step 3.
- **No output at all** — `curl -s` stays quiet about connection errors. Run it
  again without the `-s` to see the reason.

---

## Reading the logs

The dashboard's own output, most recent last:

```
sudo docker compose logs --tail 100
```

To watch it live, which is useful while reproducing a problem — press `Control` and
`C` together to stop watching:

```
sudo docker compose logs -f
```

The nightly backup keeps a separate log, because it runs outside the container:

```
sudo tail -n 40 /var/log/dashboard-backup.log
```

---

## Common situations

### The dashboard keeps restarting

`STATUS` resets to `Up 10 seconds` each time you look. Something fails at startup
and Docker keeps retrying.

```
sudo docker compose logs --tail 50
```

The cause is almost always in the last twenty lines. The most common is a failed
migration, below.

To stop the restarting while you investigate:

```
sudo docker compose stop
```

### A migration failed

The dashboard refuses to start, and the log has a banner reading `DATABASE
MIGRATION FAILED — the dashboard did not start.`

This is deliberate. A schema the code does not recognise is a situation where
continuing would do damage, so it stops instead. **The database has not been
changed** — each migration runs as a single unit and is undone completely if any
part of it fails.

The message below the banner names the problem:

- **`Migration 0002_documents.sql was modified after it was applied`** — an already
  applied migration file was edited. The fix is to put the file back the way it
  was, then make the new change in a new numbered file. If the edit came from a
  `git pull`, run `git log -- migrations/` to see what changed.

- **`Migration 0002 (...) is recorded as applied ... but its file is missing`** — a
  migration file was deleted. Restore it: `git checkout migrations/`.

- **`Migration 0003_x.sql has not been applied, but the higher version 0004 already
  has`** — two pieces of work numbered their migrations independently. Renumber the
  unapplied file above the highest applied one.

- **`Migration ... failed and was rolled back`** — the SQL itself is wrong. The
  message includes what SQLite said. This one needs the code fixed.

To apply migrations by hand, without starting the web server:

```
sudo docker compose run --rm app python -m app.migrate
```

### `Could not open the database` at startup

The dashboard restarts over and over, and the log says it cannot open
`/data/dashboard.db`.

`/data` inside the container is the folder `/srv/dashboard/data` on the server.
The dashboard runs as user id 1000, and that folder has to belong to the same id.
It usually does not when the folder was created by `sudo` without the ownership
step afterwards, or when Docker created it for you, because Docker makes it belong
to `root`.

Check who owns it:

```
ls -ld /srv/dashboard/data
```

Expected — `mason mason` in the middle:

```
drwxr-xr-x 2 mason mason 4096 Aug 17 15:28 /srv/dashboard/data
```

If it says `root root`, hand it over and start again:

```
sudo chown -R 1000:1000 /srv/dashboard/data
```

```
cd /home/mason/College-Dashboard
```

```
sudo docker compose up -d
```

The number 1000 is used rather than the name `mason` because it is the id the
dashboard runs as inside the container, and that is what has to match. On a server
set up by following `SETUP.md`, `mason` is id 1000 — confirm with `id mason`.

### Canvas assignments have stopped arriving

The status page shows **Canvas calendar feed** as `stale` or `failing`, or the
assignment list has not changed in days.

Check what the dashboard says happened:

```
sudo docker compose logs --tail 30 | grep -i canvas
```

Then force a check rather than waiting for the next one — open the dashboard and
press **Check Canvas now** on the assignments page.

The error shown on the status page names the cause:

- **`Canvas refused the calendar feed address (HTTP 403)`** — the feed address was
  reset in Canvas, which happens if you reset it deliberately or occasionally after
  an institutional change. Get a new one from **Calendar → Calendar Feed** and
  update `CANVAS_ICS_URL`. See `SETUP.md` Section 14.
- **`Could not reach Canvas`** — the server has no network, or Canvas is down.
  Check the server can reach the internet: `curl -s -o /dev/null -w '%{http_code}'
  https://www.canvas.com`.
- **`Canvas had a server problem`** — Canvas's own fault, and it usually clears
  without help. The next check runs within 30 minutes.
- **`This does not look like a calendar feed`** — Canvas served a web page instead
  of a calendar, which means the address is no longer valid.

**Assignments are not deleted while the feed is failing.** Everything already
collected stays, and the status page reports how long it has been since the last
success so stale data cannot be mistaken for current data.

### An assignment says it disappeared from the feed

The assignments page shows *Gone from the Canvas feed since ...* on an item.

This means the assignment stopped appearing in Canvas's calendar. Usually a
professor deleted or unpublished it, in which case nothing needs doing. But a
temporary Canvas fault looks identical from here, so the dashboard marks the item
and keeps it rather than deleting it — SPEC §6.6 is explicit that a transient feed
error must never destroy data.

Check the assignment in Canvas. If it really is gone, it can be dismissed from the
dashboard once M2 adds that control; until then it stays visible and harmless.

### Courses are named after enrolment codes

The feed identifies courses only by an SIS code like `FA26-BL-MATH-M211-2050`, with
no readable name anywhere in it. The first time a code appears, the dashboard
creates the course using the code as a placeholder name and marks it as needing a
real one. Renaming arrives with M2.

### The chat has stopped answering

The error shown on the page is deliberately vague, because an API error can quote
the request back and the request carries your key. The detail is in the log:

```
sudo docker compose logs --tail 30 | grep -i chat
```

- **`authentication_error`** — the key is wrong, was deleted, or never saved.
  Check with `sudo grep -c CLAUDE_API_KEY /etc/college-dashboard/env`, which
  should print `1`, then make a fresh key at `console.anthropic.com`.
- **`credit balance is too low`** — add credit on the console's billing page.
- **`rate_limit_error`** — asking faster than the account allows. It clears on its
  own; wait a minute.
- **Nothing about chat in the log at all** — the key is not set, and the page says
  so at the top.

### The chat is costing more than expected

The running total for the month is at the bottom of the **Ask** page and on the
status page. To halve the cost, switch models — add this line to
`/etc/college-dashboard/env` and restart:

```
CHAT_MODEL=claude-sonnet-5
```

Sonnet is roughly half the price per token and still very capable for this. To go
further, `CHAT_EFFORT=medium` makes it think less before answering. Both are
reversible: remove the line and restart.

Historical messages keep the price of whichever model produced them — the model is
recorded per message, so switching does not silently rewrite what the past cost.

### The chat claims to remember an email

**Report this rather than working around it.** There is no message archive until
M4, and the assistant is instructed to say so plainly instead of guessing. An
invented email is the one failure mode this milestone was designed to prevent, and
it means the instruction is not holding.

### Reminders have stopped reaching the phone

The status page shows **Reminders sent to your iPhone** as `stale` or `failing`.
Check what it says, then:

```
sudo docker compose run --rm app python -m app.caldav_push --probe
```

That walks the connection step by step and writes nothing. Whichever step it stops
at is the problem — `SETUP.md` Section 16's troubleshooting is arranged the same
way.

- **HTTP 401** — the app-specific password was revoked, or the Apple ID password
  was changed, which revokes them all. Make a new one at `account.apple.com`.
- **No list accepts reminders** — Reminders was turned off for iCloud on the phone.
- **It worked yesterday and not today** — Apple occasionally moves accounts between
  servers, which invalidates the cached list address. Clear it and let it rediscover:

  ```
  sudo docker compose run --rm app python -c "from app import db; c=db.connect(); c.execute(\"UPDATE sync_state SET cursor=NULL WHERE source='caldav_push'\"); c.close()"
  ```

### An alert fired at the wrong time, or one never came

Check what the dashboard thinks it scheduled:

```
sudo docker compose run --rm app python -m app.caldav_push --dry-run
```

That prints the exact reminder it would send for the next few assignments,
including every alert time. If those look right and the phone disagrees, the
problem is between Apple and the phone rather than here — check that the Reminders
list is the one syncing to iCloud.

Reminders are never sent between 10:30pm and 7:30am. An alert that looks missing
around those hours has most likely moved to the edge of the window by design.

### A reminder is on the phone for work already done

Ticking something off in Reminders does not tell the dashboard. Mark it **Done** in
the dashboard instead, and the next sync removes it from the phone. Completion does
not flow back from Apple — that needs a read loop, which SPEC §8 defers.

### A job says `stale` or `failing` on the dashboard

`stale` means it has not succeeded recently enough. `failing` means it has failed
three or more times in a row. The dashboard shows the actual error text underneath.

For the nightly backup, check its log:

```
sudo tail -n 40 /var/log/dashboard-backup.log
```

Then run it by hand to see the failure as it happens:

```
sudo /home/mason/College-Dashboard/ops/backup.sh
```

Common causes: Backblaze credentials changed or expired (see `docs/SECRETS.md`), or
the disk is full (`df -h /`).

### The server was rebooted

Nothing to do. The dashboard is set to start again by itself, and the backup
schedule survives reboots. Confirm with:

```
sudo docker compose ps
```

If it did not come back:

```
cd /home/mason/College-Dashboard
```

```
sudo docker compose up -d
```

---

## Applying updates

**Why:** security patches for the operating system install themselves, but the
dashboard's own code does not.

**Before you start:** do this when you have ten unhurried minutes, not the night
before something is due.

1. Take a backup first, so there is a known good point to return to:

   ```
   sudo /home/mason/College-Dashboard/ops/backup.sh
   ```

2. Fetch the new code:

   ```
   cd /home/mason/College-Dashboard
   ```

   ```
   git pull
   ```

3. Rebuild and restart. Any new migrations are applied automatically as it starts:

   ```
   sudo docker compose up -d --build
   ```

4. Confirm it came back:

   ```
   sudo docker compose ps
   ```

   ```
   curl -s "http://$(tailscale ip -4):8000/healthz"
   ```

   You want `(healthy)` and `"ok":true`.

**If the update broke something**, go back to the previous version:

```
git log --oneline -5
```

```
git checkout <the commit before the one you want to undo>
```

```
sudo docker compose up -d --build
```

Note that this puts the *code* back, not the database. A migration that has already
run stays applied, and migrations are forward-only by design. If old code cannot
work with the new schema, restore the backup from step 1 as described below.

---

## Putting a restored backup back into service

> **This replaces the live database.** Anything entered since the backup was taken
> is lost. Take the export in step 2 before you start, and it is undoable.

**Before you start:** you have restored a backup to a scratch file and seen
`integrity check: ok`, following `SETUP.md` Section 13. Do not skip that. Restoring
a damaged backup over a working database turns one problem into two.

1. Stop the dashboard, so nothing is writing while you swap files:

   ```
   cd /home/mason/College-Dashboard
   ```

   ```
   sudo docker compose stop
   ```

2. Move the current database aside rather than deleting it. This is what makes the
   whole operation reversible:

   ```
   sudo mv /srv/dashboard/data/dashboard.db /srv/dashboard/data/dashboard.db.replaced
   ```

   ```
   sudo rm -f /srv/dashboard/data/dashboard.db-wal /srv/dashboard/data/dashboard.db-shm
   ```

3. Put the restored copy in place. This assumes it is at
   `/tmp/restore-test.db` on the server — copy it there from your Mac with `scp`
   if you restored it on the laptop:

   ```
   sudo cp /tmp/restore-test.db /srv/dashboard/data/dashboard.db
   ```

4. Give it to the right owner, or the dashboard cannot write to it:

   ```
   sudo chown mason:mason /srv/dashboard/data/dashboard.db
   ```

5. Start up again:

   ```
   sudo docker compose up -d
   ```

6. Confirm:

   ```
   curl -s "http://$(tailscale ip -4):8000/healthz"
   ```

   You want `"ok":true`, with a `schema_version` matching what the restore printed.

**To undo all of this**, if the restored copy turns out to be wrong:

```
sudo docker compose stop
```

```
sudo mv /srv/dashboard/data/dashboard.db.replaced /srv/dashboard/data/dashboard.db
```

```
sudo docker compose up -d
```

Once you are confident, remove the old file:

```
sudo rm /srv/dashboard/data/dashboard.db.replaced
```

---

## Reading a deleted conversation out of a backup

Deleting a conversation on the Ask page is final — the dashboard has no undo for
it. Last night's backup still has it, though, and you do **not** have to replace
the live database to get it back. Read it out of a scratch copy instead.

**This changes nothing.** Every command below touches a temporary file.

1. Restore a backup to a scratch file, exactly as in `SETUP.md` Section 13. That
   leaves you with `/tmp/restore-test.db`.

2. List what conversations that copy has, newest last:

   ```
   sqlite3 /tmp/restore-test.db "SELECT id, updated_at, title FROM chat_threads ORDER BY updated_at;"
   ```

   Expect one line per conversation, like:

   ```
   4|2026-09-14T18:22:05Z|what's left before the Bio exam
   ```

   The first number is the id you need.

3. Print the conversation itself, putting the id from step 2 where the `4` is:

   ```
   sqlite3 -noheader /tmp/restore-test.db "SELECT role || ': ' || content || char(10) FROM chat_messages WHERE thread_id = 4 ORDER BY id;"
   ```

   It prints the whole exchange as plain text, alternating `user:` and
   `assistant:`.

4. Delete the scratch copy when you have what you needed:

   ```
   rm /tmp/restore-test.db
   ```

If step 2 prints nothing, the backup predates the conversation or postdates the
deletion. Backups older than last night are still in the bucket — `SETUP.md`
Section 13 shows how to list them and restore a specific one.

---

## Restore test log

SPEC §11: an untested backup is not a backup. Do the drill in `SETUP.md` Section 13
every few months and add a line here.

| Date | Backup tested | Result | Notes |
|---|---|---|---|
| _(fill in during setup)_ | | | |

---

## Facts worth having to hand

| Thing | Where |
|---|---|
| Code | `/home/mason/College-Dashboard` |
| Database | `/srv/dashboard/data/dashboard.db` |
| Local backups | `/srv/dashboard/backups` |
| Secrets | `/etc/college-dashboard/env` (administrator only) |
| Server address setting | `/home/mason/College-Dashboard/.env` |
| Backup log | `/var/log/dashboard-backup.log` |
| Backup schedule | `sudo crontab -l` |
| Backups kept | 30 nightly, plus one a month for 6 months, on the server and at Backblaze |
