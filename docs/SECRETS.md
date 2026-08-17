# Secrets: what they are, where they live, how to replace them

Every password, key and token the dashboard uses, in one list.

**No real values appear in this file, or anywhere else in this repository.** If you
ever find one here, treat it as leaked: replace it using the instructions below,
then remove it from the file and from the project's history.

---

## Where secrets live

There is exactly one place on the server for them:

```
/etc/college-dashboard/env
```

Owned by `root`, permissions `600` — meaning only the administrator account can
read it. It sits outside the code folder, so no mistake with `git` can ever upload
it.

`ops/secrets.env.example` shows the shape of that file with placeholder values.

Two files are easy to confuse:

| File | Holds | Read by |
|---|---|---|
| `/etc/college-dashboard/env` | real secrets | passed into the dashboard container |
| `/home/mason/College-Dashboard/.env` | the server's private address and timezone | Docker Compose, on the server itself |

The second is not secret in the way a password is, but it is excluded from `git`
anyway, because there is no reason to publish which address your server answers on.

### Seeing what is in the file, without printing it

To list the setting names only:

```
sudo grep -o '^[A-Z_]*' /etc/college-dashboard/env
```

To read the whole file, including values:

```
sudo cat /etc/college-dashboard/env
```

Be aware that the second command prints secrets to your screen, and your terminal
keeps scrollback. Close the window afterwards.

### Applying a change

After editing the file, the dashboard has to be restarted to see it:

```
cd /home/mason/College-Dashboard
```

```
sudo docker compose up -d
```

The backup script reads the file fresh on every run, so backup-related changes
take effect on the next nightly run with no restart.

---

## The list

### 1. `BACKUP_AGE_RECIPIENT` — backup encryption public key

**What it is:** the public half of an `age` key pair. It can only lock backups, not
unlock them, which is why it is safe to keep on the server.

**Needed by:** `ops/backup.sh`, every night.

**Its private half** lives at `~/.age/dashboard-backup.key` on the MacBook, plus
wherever you stored the second copy. **It must never be placed on the server.** The
whole point is that somebody who breaks into the server finds backups they cannot
read.

**If you lose the private key, every existing backup is permanently unreadable.**
There is no recovery path. Not from Backblaze, not from anyone.

**To replace it** — note that old backups stay locked to the old key, so keep that
key for as long as you want to be able to read them:

On the MacBook:

```
age-keygen -o ~/.age/dashboard-backup-2.key
```

Record the new private key, then put the printed `age1...` public key into the
server's secrets file as `BACKUP_AGE_RECIPIENT`. Backups taken from then on use the
new key.

---

### 2. `RCLONE_CONFIG_B2_ACCOUNT` and `RCLONE_CONFIG_B2_KEY` — Backblaze upload credentials

**What they are:** the `keyID` and `applicationKey` of a Backblaze application key,
allowed to read and write one bucket and nothing else.

**Needed by:** `ops/backup.sh`, to copy backups off the server.

**Exposure if leaked:** somebody could read, add to, or delete the contents of that
one bucket. The backups themselves stay encrypted and unreadable without item 1.

**To replace them:** application keys cannot be viewed again after creation, so
replacing means creating a new one.

1. In the Backblaze web interface, click **Application Keys** in the left menu.
2. Click **Add a New Application Key**. Name it, restrict it to your backup bucket,
   and give it **Read and Write** access.
3. Copy both values from the page that appears — they are shown once.
4. Put them in the server's secrets file.
5. Run a backup by hand to confirm:

   ```
   sudo /home/mason/College-Dashboard/ops/backup.sh
   ```

6. Once that succeeds, delete the old key from the Backblaze **Application Keys**
   page.

---

### 3. `BACKUP_B2_BUCKET` — Backblaze bucket name

Not a secret, but it lives with the others because the backup script needs it.
Changing it points future backups at a different bucket; it does not move existing
ones.

---

### 4. `CANVAS_ICS_URL` — Canvas calendar feed address *(needed from M1)*

**What it is:** a web address with a long token in it, which Canvas gives you so
that calendar applications can read your assignment due dates.

**Treat it exactly like a password.** Anyone holding this address can read your
entire academic schedule, without logging in and without you being told.

**Where to get it:** in Canvas, open **Calendar**, then look at the bottom of the
right-hand sidebar for a **Calendar Feed** button. It shows an address beginning
`https://` and containing `.ics`.

**To replace it** — do this if it is ever posted, emailed, or pasted somewhere
public:

1. In Canvas, open **Account → Settings**.
2. Find **Reset** next to the calendar feed, and confirm. Every copy of the old
   address stops working at once.
3. Get the new address from **Calendar → Calendar Feed**.
4. Put it in the server's secrets file and restart the dashboard.

---

### 5. `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD` — Apple Reminders access *(needed from M3)*

**What they are:** credentials that let the server add items to Apple Reminders and
Calendar.

**Use an app-specific password, never your real Apple ID password.** An
app-specific password can be revoked on its own without affecting anything else.

**To create one:** sign in at <https://account.apple.com>, find **App-Specific
Passwords** in the sign-in and security section, and generate one named
`dashboard`. It is shown once.

**To replace it:** revoke the old one on that same page, generate a new one, and
update the server's secrets file.

---

### 6. `INGEST_BEARER_TOKEN` — saved-messages endpoint token *(needed from M4)*

**What it is:** a long random string the iPhone Shortcut sends with each saved
message, so the server knows the request is from you.

**To create one** — run this on the server and use the output:

```
openssl rand -hex 32
```

**To replace it:** generate a new one, update the secrets file, restart the
dashboard, and update the Shortcut on the iPhone to match. The old token stops
working immediately, so the Shortcut fails until it is updated.

---

### 7. `CLAUDE_API_KEY` — chat access *(needed from M5)*

**What it is:** the key that lets the dashboard ask Claude questions. It is billed
to you, so a leaked key costs money.

**Where to get it:** <https://console.anthropic.com>, under **API Keys**.

**To replace it:** create a new key in the console, put it in the secrets file,
restart the dashboard, then delete the old key in the console. Set a monthly
spending limit in the console as well — expected use is a few dollars a month, so a
$25 cap turns a leak into an annoyance rather than a bill.

---

### 8. VAPID keys — web push notifications *(needed from M4)*

**What they are:** a key pair identifying this dashboard to Apple's push service.
Losing them means every device has to re-subscribe to notifications; they are not
otherwise sensitive.

Generation instructions arrive with M4.

---

## Secrets that are not in that file

### The `mason` account password

Set in `SETUP.md` Section 3. It is what `sudo` asks for.

**To change it**, connected to the server as `mason`:

```
passwd
```

It asks for the current password once and the new one twice.

**If you have forgotten it:** use Vultr's browser console — on the server's page at
`my.vultr.com`, the **View Console** button — log in as `root` with the root
password from the Vultr page, and run `passwd mason`.

### The server root password

Shown on the server's page at `my.vultr.com` and resettable from there.

### Your Vultr and Backblaze account passwords

Managed on their own websites, both resettable by email.

### The Tailscale account

Signed in through Google, Microsoft, GitHub or Apple, so there is no separate
password. Protect that underlying account — it is what grants access to the private
network. Turn on two-factor authentication for it.

---

## If something leaks

Work in this order:

1. **Canvas feed address** — reset it in Canvas first. It is the one that exposes
   the most with the least effort on an attacker's part.
2. **Claude API key** — delete it in the console. It is the one that costs money.
3. **Backblaze application key** — delete it in the Backblaze interface.
4. **CalDAV app-specific password** — revoke it at `account.apple.com`.
5. **Anything on the server** — if you believe the server itself was accessed,
   assume every secret on it is compromised, replace all of them, and consider
   rebuilding the server from `SETUP.md`. Your data is recoverable from a backup,
   which is exactly what the backups are for.

The `age` private key is the one thing on this list that cannot be replaced after
the fact: it is not on the server, so a server compromise does not reach it, but
losing your own copy is unrecoverable.
