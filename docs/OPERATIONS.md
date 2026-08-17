# Operations: keeping it running

For the owner, once a month, and whenever something looks wrong.

Everything here assumes you are connected to the server. From your Mac's Terminal,
replacing the address with yours:

```
ssh mason@100.92.147.61
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
   dashboard   college-dashboard:local    Up 12 days (healthy)      100.92.147.61:8000->8000/tcp
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
ping -c 3 100.92.147.61
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
LISTEN 0  4096  100.92.147.61:8000  0.0.0.0:*  users:(("docker-proxy",pid=4412,fd=7))
```

If the address shown is not the one you are typing into your browser, use the one
shown here. If you see `0.0.0.0:8000`, the dashboard is exposed to the public
internet — stop it immediately with `sudo docker compose down` and fix
`TAILNET_BIND_IP` in `.env` before starting it again.

### Step 6 — Ask the dashboard directly

```
curl -s http://localhost:8000/healthz
```

- **`"ok":true`** — the dashboard is fine and the problem is between your device
  and the server. Go back to step 1.
- **`"ok":false`** — one of the checks failed. The `checks` section names which
  one, and each has a section below.
- **`Connection refused`** — it is not actually listening. Go to step 3.

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
   curl -s http://localhost:8000/healthz
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
   curl -s http://localhost:8000/healthz
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
