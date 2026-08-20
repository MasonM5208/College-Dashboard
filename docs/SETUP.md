# Setting up the Semester Dashboard

This guide takes you from nothing to a running dashboard you can open on your
laptop and your phone.

It assumes you have never used a terminal, never heard of SSH or Docker, and have
never rented a server. Every step says what it does and why, every command shows
what you should see afterwards, and every section ends with a way to check that it
worked before you move on.

**You do not have to do this in one sitting.** Each section says what must already
be true before you start it. Stop whenever you like and pick it up later.

---

## What you are building

A small computer, rented for about $5 a month, that runs day and night in a data
centre. It keeps track of your assignments, and later will send reminders to your
phone. Because it is always on, it can remind you at 7am on a Saturday while your
laptop is shut in a bag.

That computer is not reachable from the public internet. It joins a private
network that contains only your own devices. Nothing else can see it or connect to
it.

### How long this takes

| Section | First time |
|---|---|
| 1. Rent the server | 15 minutes |
| 2. Connect to it | 15 minutes |
| 3. Make your own account | 10 minutes |
| 4. Automatic security updates | 5 minutes |
| 5. Install Docker | 10 minutes |
| 6. Join the private network | 25 minutes |
| 7. Close the public door | 10 minutes |
| 8. Get the code onto the server | 10 minutes |
| 9. Backup keys and off-site storage | 30 minutes |
| 10. Start the dashboard | 15 minutes |
| 11. Put it on your phone | 10 minutes |
| 12. Schedule the nightly backup | 10 minutes |
| 13. Prove a backup can be restored | 20 minutes |
| 14. Connect your Canvas calendar | 15 minutes |
| 15. Turn on the chat | 15 minutes |
| 16. Reminders on your phone | 20 minutes |

About three hours in total, spread over as many sittings as you want.

### What it costs

About $5 a month for the server, and under $1 a month for backup storage. The
chat adds a few dollars a month at ordinary use — Section 15 covers setting a
spending limit and shows a running total so it never surprises you.

### Things you will create and must write down

As you go, this guide will tell you to record several passwords and keys. Some of
them are shown **once and never again**. Have somewhere ready to put them — a
password manager, or the Notes app with a password on the note.

Here is the full list, so you know what is coming. Do not fill it in yet.

| # | What | Created in | Can you get it back later? |
|---|---|---|---|
| 1 | Vultr account password | Section 1 | Yes, by email reset |
| 2 | Server root password | Section 1 | Yes, from the Vultr website |
| 3 | Your `mason` account password | Section 3 | **No.** Write it down. |
| 4 | Backup encryption private key | Section 9 | **No. If you lose this, every backup is permanently unreadable.** |
| 5 | Backblaze account password | Section 9 | Yes, by email reset |
| 6 | Backblaze application key | Section 9 | **No.** Shown once. |
| 7 | Canvas calendar feed address | Section 14 | Yes, from Canvas |
| 8 | Claude API key | Section 15 | **No.** Shown once. |
| 9 | Apple app-specific password | Section 16 | **No.** Shown once. |

---

## Section 1 — Rent the server

**What this does:** creates the always-on computer, called a VPS (Virtual Private
Server — a slice of a larger machine in a data centre, rented by the month).

**Why:** reminders have to fire when your laptop is closed, and a laptop cannot do
that.

**Before you start:** a credit card and an email address.

**Time:** about 15 minutes.

### Steps

1. Go to <https://www.vultr.com> and create an account. Record the password you
   choose as **item 1** on your list.

2. Once you are signed in you will be on a page showing your products, which is
   empty. Look for a blue **Deploy +** button, usually near the top right. Click
   it, then choose **Deploy New Server**.

   If you do not see that button, you may be on a billing or account page. The web
   address should start with `https://my.vultr.com`.

3. You will be asked a series of choices. Set them as follows, and leave anything
   not mentioned at its default:

   | Choice | Pick |
   |---|---|
   | Type | **Shared CPU**, then **Regular Performance** |
   | Location | Whichever city is closest to you |
   | Image / operating system | **Debian**, version **12 x64** |
   | Plan / size | The cheapest with at least **1 GB** of memory (about $5–6 a month) |
   | Auto backups | Off — this project makes its own, and Vultr's cost extra |
   | IPv6 | On is fine |
   | SSH keys | **Add one — see the box below.** Do not skip this. |
   | Hostname | `dashboard` |

   Debian 12 matters: the instructions below are written for it. A different
   operating system will not match.

   #### Adding your SSH key — do not skip this

   An SSH key is a pair of files on your Mac that proves who you are without a
   password. Setting one up here takes two minutes and removes password typing
   from the rest of this guide.

   It is worth the two minutes for a specific reason. The password Vultr generates
   for a new server contains symbols, and both the browser console and some
   copy-paste paths deliver those symbols incorrectly — so you can type the
   correct password repeatedly and be refused every time, with no way to tell why.
   A key sidesteps that completely.

   First, check whether you already have a key. On your **MacBook**:

   ```
   ls ~/.ssh/id_ed25519.pub
   ```

   If it prints the filename, you have one. If it prints
   `No such file or directory`, create one — press `Return` at each question to
   accept the defaults, and leave the passphrase empty:

   ```
   ssh-keygen -t ed25519 -C "macbook"
   ```

   Expected, at the end:

   ```
   Your public key has been saved in /Users/you/.ssh/id_ed25519.pub
   The key fingerprint is:
   SHA256:9Xk2pQ7rL4mN8vB1cD6wF3jH5tY0aS2eR7uI4oP9gK8 macbook
   ```

   Copy it to your clipboard:

   ```
   pbcopy < ~/.ssh/id_ed25519.pub
   ```

   This prints nothing. Now, in the Vultr deployment page's **SSH Keys** section,
   click **Add New**, paste with `Command` and `V` together, name it `macbook`,
   and save. **Make sure its checkbox is ticked** before you deploy — an added but
   unticked key is not installed on the server.

   You will still record the root password in step 5, as a fallback for the
   browser console.

4. Click **Deploy Now**. The server takes two or three minutes to build. Its
   status will read *Installing*, then *Running*.

5. Click the server's name to open its page. You need two things from it:

   - **IP address** — four numbers separated by dots, for example `149.28.44.19`.
     This is the server's address on the public internet.
   - **Password** — next to it is an eye icon that reveals it, and a copy icon.

   Record the password as **item 2** on your list.

### Check before continuing

On the server's page, the status reads **Running**, and you have written down its
IP address and password.

If it says *Installing* for more than five minutes, reload the page. If it fails,
delete the server and deploy it again — nothing is on it yet, so there is nothing
to lose.

---

## Section 2 — Connect to the server

**What this does:** opens a window on your Mac where what you type is run by the
server instead of by your Mac.

**Why:** the server has no screen or keyboard. Typing commands to it remotely is
the only way to set it up.

**Before you start:** Section 1 finished; the IP address and root password to hand.

**Time:** about 15 minutes.

### Steps

1. On your Mac, open the **Terminal** application. Press `Command` and `Space`
   together, type `Terminal`, and press `Return`.

   A window opens with some text and a blinking cursor. This is a place to type
   commands. It will not do anything you do not tell it to.

2. Type the command below, replacing `YOUR_SERVER_IP` with the IP address from
   Section 1, then press `Return`.

   SSH (Secure Shell) is the standard way to type commands into a computer that is
   not in front of you. `root` is the name of the server's administrator account.

   ```
   ssh root@YOUR_SERVER_IP
   ```

   The first time, you will see something like this:

   ```
   The authenticity of host '149.28.44.19 (149.28.44.19)' can't be established.
   ED25519 key fingerprint is SHA256:kPr5s0Xr3Aa9yvHt7cQ2mzZ8dJ1oN4bV6uW0eR2tY8s.
   This key is not known by any other names.
   Are you sure you want to continue connecting (yes/no/[fingerprint])?
   ```

   This is your Mac saying it has not seen this server before. Type `yes` and
   press `Return`.

3. Because you added an SSH key in Section 1, you should **not** be asked for a
   password at all. Go straight to step 4.

   If you are asked for one:

   ```
   root@149.28.44.19's password:
   ```

   that means the key was not installed — most often because it was added on the
   Vultr page but its checkbox was left unticked at deploy time. Paste the root
   password from Section 1 to get in for now, and see the troubleshooting note
   below about installing the key afterwards.

   **Nothing appears as you type or paste a password.** No dots, no stars, no
   movement. That is deliberate, not a fault. Paste it once and press `Return`.

4. When it works you will see a welcome message ending with a line like:

   ```
   root@dashboard:~#
   ```

   That is the server waiting for a command. Everything you type now runs on the
   server, not on your Mac.

### Troubleshooting

- **`Permission denied, please try again.`** — the password was wrong, or an extra
  space came with it when pasted. Copy it again from the Vultr page. Three wrong
  attempts closes the connection; run the `ssh` command again to retry.

  **If it keeps refusing a password you are certain is right**, do not keep
  retrying, and do not assume the browser console will do better — the console is
  the *more* likely of the two to mangle it. Vultr's generated passwords contain
  symbols, and both paths can deliver those symbols incorrectly, which looks
  identical to typing the wrong password.

  The reliable fix is to stop using that password. On the server's page at
  `my.vultr.com`, find the **Settings** tab and the section for changing the root
  password, and set one made of **letters and digits only**, at least 16
  characters. Vultr reboots the server to apply it, which takes two or three
  minutes. Then install your SSH key so this cannot recur — from your **MacBook**:

  ```
  ssh-copy-id root@149.28.44.19
  ```

  It asks for that new password once, then never again.

- **You were asked for a password even though you added an SSH key** — the key was
  added to your Vultr account but its checkbox was not ticked on the deployment
  page, so it was never installed. Fix it with the `ssh-copy-id` command above.

- **`ssh: connect to host ... port 22: Operation timed out`** — the server is not
  finished starting, or the IP address has a typo. Wait two minutes and try again.

- **`WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!`** — expected only if you
  destroyed and rebuilt the server at the same address. The message includes a
  command beginning `ssh-keygen -R`; run that on your Mac, then connect again.

### Check before continuing

Your Terminal prompt ends in `root@dashboard:~#`.

To leave at any time, type `exit` and press `Return`. To come back, run the `ssh`
command from step 2 again.

---

## Section 3 — Make your own account

**What this does:** creates an account named `mason` for everyday use, instead of
working as `root`.

**Why:** `root` can delete anything on the server without asking for confirmation.
A normal account has to ask for permission first, which turns a typo into a
question rather than into damage.

**Before you start:** connected as `root` from Section 2.

**Time:** about 10 minutes.

### Steps

1. Create the account:

   ```
   adduser mason
   ```

   It asks for a new password twice, then for a full name and some other details.
   Leave the extra details blank by pressing `Return` at each one. The last
   question is answered with `Y`:

   ```
   Adding user `mason' ...
   Adding new group `mason' (1000) ...
   Adding new user `mason' (1000) with group `mason' ...
   Creating home directory `/home/mason' ...
   New password:
   Retype new password:
   passwd: password updated successfully
   Changing the user information for mason
   Enter the new value, or press ENTER for the default
       Full Name []:
   Is the information correct? [Y/n] Y
   ```

   **Record this password as item 3 on your list.** It cannot be recovered. You
   will need it every time the server asks you to confirm something.

   Note the `(1000)` in the output. That number is how the dashboard's files end
   up belonging to you rather than to nobody.

2. Give the account permission to do administrative tasks when it asks:

   ```
   usermod -aG sudo mason
   ```

   This command prints nothing at all when it works. Silence means success.

3. Give the new account the same SSH key, so you can connect as `mason` without a
   password the way you can as `root`. Run both of these while still logged in as
   `root`:

   ```
   mkdir -p /home/mason/.ssh && cp /root/.ssh/authorized_keys /home/mason/.ssh/
   ```

   ```
   chown -R mason:mason /home/mason/.ssh && chmod 700 /home/mason/.ssh && chmod 600 /home/mason/.ssh/authorized_keys
   ```

   Neither prints anything when it works.

   You will still be asked for the `mason` password by `sudo`, which is a separate
   thing: the key proves who you are when connecting, and `sudo` re-checks that
   you are still at the keyboard before doing something administrative.

4. Switch to the new account:

   ```
   su - mason
   ```

   The prompt changes to:

   ```
   mason@dashboard:~$
   ```

   The `$` on the end instead of `#` means you are no longer root.

5. Check that administrative permission works. `sudo` means "do this one thing as
   the administrator". It will ask for the password you created a moment ago.

   ```
   sudo echo "permission works"
   ```

   Expected:

   ```
   [sudo] password for mason:
   permission works
   ```

   Again, nothing appears while you type the password.

### Troubleshooting

- **`mason is not in the sudoers file. This incident will be reported.`** — step 2
  did not take effect. Type `exit` to go back to `root`, run the `usermod` command
  again, then `su - mason`.

### Check before continuing

Your prompt ends in `mason@dashboard:~$`, and `sudo echo "permission works"`
prints `permission works`.

---

## Section 4 — Turn on automatic security updates

**What this does:** makes the server install its own security patches.

**Why:** software flaws are found constantly, and a server nobody patches becomes
a liability. This is the one piece of ongoing maintenance worth automating
completely.

**Before you start:** logged in as `mason` from Section 3.

**Time:** about 5 minutes.

### Steps

1. Refresh the list of available software:

   ```
   sudo apt update
   ```

   Expected — several lines beginning `Get:` or `Hit:`, ending with something like:

   ```
   Reading package lists... Done
   Building dependency tree... Done
   All packages are up to date.
   ```

2. Install the updater, along with three tools the backup will need later:

   ```
   sudo apt install -y unattended-upgrades sqlite3 age rclone
   ```

   This prints many lines. The last should be similar to:

   ```
   Setting up unattended-upgrades (2.9.1+nmu3) ...
   Processing triggers for man-db (2.11.2-2) ...
   ```

3. Switch it on:

   ```
   sudo dpkg-reconfigure -plow unattended-upgrades
   ```

   A blue screen appears asking whether to automatically download and install
   stable updates. Press the left arrow to highlight **`<Yes>`**, then press
   `Return`.

### Check before continuing

Run:

```
systemctl is-enabled unattended-upgrades
```

Expected:

```
enabled
```

If it prints `disabled`, run the `dpkg-reconfigure` command again and be sure to
answer `<Yes>`.

---

## Section 5 — Install Docker

**What this does:** installs Docker, which runs the dashboard inside a sealed
container.

**Why:** a container bundles the dashboard with the exact versions of everything it
needs. It cannot be broken by an unrelated change elsewhere on the server, and
updating it is one command.

**Before you start:** Section 4 finished.

**Time:** about 10 minutes.

### Steps

1. Check whether Docker is already present:

   ```
   docker --version
   ```

   On a new server, expected:

   ```
   -bash: docker: command not found
   ```

   If it instead prints something like `Docker version 27.3.1, build ce1223035a`,
   Docker is already installed and you can skip to the check at the end of this
   section.

2. Download and run Docker's official installer. This fetches a script from
   `get.docker.com`, which is Docker's own website, and runs it.

   ```
   curl -fsSL https://get.docker.com -o install-docker.sh
   ```

   This prints nothing. It has downloaded the script without running it, so you
   can look at it first if you want:

   ```
   less install-docker.sh
   ```

   Press `q` to stop viewing.

3. Run it:

   ```
   sudo sh install-docker.sh
   ```

   This takes two or three minutes and prints a great deal. Near the end you
   should see version information for Docker.

4. Tidy up the installer:

   ```
   rm install-docker.sh
   ```

### Check before continuing

```
sudo docker run --rm hello-world
```

Expected — a paragraph that includes:

```
Hello from Docker!
This message shows that your installation appears to be working correctly.
```

If that does not appear, run `sudo systemctl start docker` and try again.

---

## Section 6 — Join the private network

**What this does:** puts the server, your MacBook and your iPhone onto a private
network called a tailnet, using a service called Tailscale.

**Why this instead of a normal website address:** a server on the public internet
is found by automated scanners within minutes of existing, and defending one is a
skill in its own right — certificates, firewalls, watching for break-in attempts.
It is a skill you should not have to learn to keep track of your homework.
Tailscale takes the other path. The server gets an address that only your own
devices can reach. There is nothing public to attack, so there is nothing public
to defend.

**Before you start:** Section 5 finished. You will need your phone as well as your
laptop.

**Time:** about 25 minutes.

### Steps

1. **On the server**, install Tailscale:

   ```
   curl -fsSL https://tailscale.com/install.sh | sh
   ```

   Expected, at the end:

   ```
   Installation complete! Log in to start using Tailscale by running:
   sudo tailscale up
   ```

2. Connect it:

   ```
   sudo tailscale up
   ```

   Expected — a web address to open:

   ```
   To authenticate, visit:

       https://login.tailscale.com/a/2f9c1b4e7a83
   ```

3. Copy that address into a browser on your Mac. Sign in with Google, Microsoft,
   GitHub or Apple — whichever you prefer. This becomes your Tailscale account.
   Approve the request to add the machine.

   Back in the Terminal, the command finishes and returns you to the prompt.

4. Find the server's private address:

   ```
   tailscale ip -4
   ```

   Expected — one address beginning with `100`:

   ```
   100.x.y.z
   ```

   **Write this down.** It is the address you will use for everything from here
   on. It is not secret in the way a password is, but there is no reason to post
   it anywhere either, which is why it is not written down in this repository.

   > **Every `100.x.y.z` later in these documents means the address you just
   > wrote down.** Yours will be different, and it never appears here — type your
   > own in its place each time.

   If you have destroyed and rebuilt a server at any point, its old entry stays
   registered and the new one is given a name with a number added, such as
   `dashboard-1`. Remove the stale one at
   <https://login.tailscale.com/admin/machines> so the name is freed and the list
   keeps matching reality.

5. **On your MacBook**, install Tailscale from <https://tailscale.com/download>.
   Open the downloaded file, drag Tailscale to Applications, open it, and sign in
   with the same account you used in step 3.

   A small Tailscale icon appears in the menu bar at the top of the screen. Click
   it — `dashboard` should be listed as connected.

6. **On your iPhone**, install Tailscale from the App Store. Open it, sign in with
   the same account, and allow it to add a VPN configuration when iOS asks. Turn
   the switch on.

   Leave Tailscale switched on permanently. It uses very little battery, and the
   dashboard is unreachable without it.

### Troubleshooting

- **`tailscale: command not found` after step 1** — the install script failed,
  often from a temporary network problem. Run it again.

- **The browser page says the machine is already connected** — that is fine.
  Return to the Terminal and continue.

- **`tailscale ip -4` prints nothing** — run `sudo tailscale up` again and finish
  the sign-in.

### Check before continuing

From your Mac's Terminal — open a second Terminal window, or type `exit` first —
run this, replacing the address with yours:

```
ping -c 3 100.x.y.z
```

Expected:

```
PING 100.x.y.z (100.x.y.z): 56 data bytes
64 bytes from 100.x.y.z: icmp_seq=0 ttl=64 time=24.113 ms
64 bytes from 100.x.y.z: icmp_seq=1 ttl=64 time=23.887 ms
64 bytes from 100.x.y.z: icmp_seq=2 ttl=64 time=24.402 ms

--- 100.x.y.z ping statistics ---
3 packets transmitted, 3 packets received, 0.0% packet loss
```

`0.0% packet loss` means your Mac can reach the server privately.

From now on you can connect with the private address instead of the public one:

```
ssh mason@100.x.y.z
```

---

## Section 7 — Close the public door

**What this does:** stops the server accepting connections from the public
internet, so it is reachable only through your private network.

**Why:** this is the point of the whole arrangement. Until now the server has been
answering anyone on the internet who knocks on its SSH door, and automated
programs knock constantly.

> **Read this before you start.** If Tailscale stops working on the server after
> this change, you cannot reach it by SSH at all. The way back in is Vultr's
> browser console: on the server's page at `my.vultr.com`, look for a **View
> Console** button (an icon of a screen, near the top right). That opens a
> keyboard-and-screen view of the server that does not use the network, and you
> can log in there as `mason` and undo this. Confirm you can open that console
> **now**, before making the change.

**Before you start:** Section 6 finished, and you have opened the Vultr console
once to be sure it works.

**Time:** about 10 minutes.

### Steps

1. Connect over the private address, to be sure that route works before you remove
   the other one:

   ```
   ssh mason@100.x.y.z
   ```

2. Get the address from the server itself, rather than from your notes. It must be
   **this server's** tailnet address — your MacBook and iPhone have their own
   `100.` addresses too, and using one of those stops SSH from starting at all.

   ```
   ip -4 addr show tailscale0
   ```

   Expected — the address you want is the one after `inet`, before the `/`:

   ```
   4: tailscale0: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> mtu 1280 qdisc fq_codel state UNKNOWN group default qlen 500
       inet 100.x.y.z/32 scope global tailscale0
          valid_lft forever preferred_lft forever
   ```

   **If this prints `Device "tailscale0" does not exist`**, Tailscale is not
   running on the server, and the address does not exist for SSH to listen on.
   Stop here and run `sudo tailscale up`, finishing the browser sign-in, before
   going any further with this section.

3. Tell SSH to listen only on that private address. This writes a small
   configuration file, using the address you saw above:

   ```
   echo "ListenAddress 100.x.y.z" | sudo tee /etc/ssh/sshd_config.d/tailscale-only.conf
   ```

   Expected — it echoes the line back:

   ```
   ListenAddress 100.x.y.z
   ```

4. Check the configuration is valid before applying it:

   ```
   sudo sshd -t
   ```

   This prints nothing when the configuration is good. If it prints an error, do
   not continue — run `sudo rm /etc/ssh/sshd_config.d/tailscale-only.conf` to undo
   the change, and check the address for typos.

   Be aware of what this does **not** check. It reads the file for spelling
   mistakes, but it does not test whether the address can actually be used. An
   address that is correctly written but belongs to a different machine passes
   this check and then fails in the next step. Step 2 is what guards against that.

5. Apply it. **Keep this window open afterwards, whatever happens.**

   ```
   sudo systemctl restart ssh
   ```

   Nothing is printed when it works. Your existing connection stays open either
   way, and new connections now have to come over Tailscale.

   **If it prints `Job for ssh.service failed`**, the new setting stopped SSH from
   starting, and no new connection can be made until it is fixed. Your current
   window still works, so use it — undo the change and put SSH back:

   ```
   sudo rm /etc/ssh/sshd_config.d/tailscale-only.conf
   ```

   ```
   sudo systemctl restart ssh
   ```

   ```
   sudo systemctl status ssh --no-pager
   ```

   You want `Active: active (running)`. Confirm you can open a *second* Terminal
   window and connect before doing anything else. Then find the cause with:

   ```
   sudo journalctl -xeu ssh.service --no-pager | tail -30
   ```

   The `sudo` matters. Without it this prints `-- No entries --` and a note about
   groups called `adm` and `systemd-journal`, which reads like "there is nothing
   wrong" but means "you are not allowed to see the system's messages".

   `Cannot bind any address` means the address in the file is not one this server
   holds — go back to step 2 and read it from `tailscale0` rather than from your
   notes.

### Check before continuing

**Do not close your current Terminal window.** Open a *second* window and try to
connect over the private address:

```
ssh mason@100.x.y.z
```

You should get a password prompt. That proves the private route still works.

Now confirm the public route is closed. From the second window, using the *public*
IP address from Section 1:

```
ssh -o ConnectTimeout=10 root@149.28.44.19
```

Expected:

```
ssh: connect to host 149.28.44.19 port 22: Operation timed out
```

That timeout is the goal. The server no longer answers the public internet.

**To undo this** at any time, from the Vultr console or a working session:

```
sudo rm /etc/ssh/sshd_config.d/tailscale-only.conf
sudo systemctl restart ssh
```

---

## Section 8 — Get the code onto the server

**What this does:** copies the dashboard's code onto the server and creates the
folder its database will live in.

**Before you start:** Section 7 finished, connected as `mason`.

**Time:** about 10 minutes.

### Steps

1. Install `git`, the tool that copies code:

   ```
   sudo apt install -y git
   ```

2. Download the code into your home folder:

   ```
   git clone https://github.com/MasonM5208/College-Dashboard.git /home/mason/College-Dashboard
   ```

   Expected:

   ```
   Cloning into '/home/mason/College-Dashboard'...
   remote: Enumerating objects: 84, done.
   Receiving objects: 100% (84/84), 41.23 KiB | 4.12 MiB/s, done.
   Resolving deltas: 100% (12/12), done.
   ```

   If the repository is private, git will ask for a username and password. Use
   your GitHub username and a personal access token rather than your password —
   GitHub stopped accepting passwords here. Create one at **GitHub → Settings →
   Developer settings → Personal access tokens**.

3. Create the folder for the database. This lives outside the code folder on
   purpose: the code can be deleted and downloaded again at any time, and the
   database cannot.

   ```
   sudo mkdir -p /srv/dashboard/data /srv/dashboard/backups
   ```

4. Give yourself ownership of the data folder, so the dashboard can write to it:

   ```
   sudo chown mason:mason /srv/dashboard/data
   ```

   Neither command prints anything when it works.

### Check before continuing

```
ls /home/mason/College-Dashboard
```

Expected:

```
CLAUDE.md  Dockerfile  README.md  SPEC.md  app  docker-compose.yml
docs  migrations  ops  pytest.ini  requirements-dev.txt  requirements.txt
```

---

## Section 9 — Backup keys and off-site storage

**What this does:** creates the key that encrypts your backups, and an account at
Backblaze where the encrypted copies are stored.

**Why off the server:** the server is rented. It can be lost to a billing mistake,
a hardware failure, or a mistake of your own. A backup that only exists on the
machine it is backing up is not a backup.

**Why encrypted:** the backup contains your entire academic life. Storing it
somewhere else means somebody else's computer is holding it, so it should be
unreadable to them.

**Before you start:** Section 8 finished. Part of this happens on your **MacBook**,
not the server — read each step's heading carefully.

**Time:** about 30 minutes.

### Steps

1. **On your MacBook**, in a Terminal window that is *not* connected to the server
   (type `exit` if it is), check for Homebrew, the tool that installs software on
   a Mac:

   ```
   brew --version
   ```

   If it prints something like `Homebrew 4.3.20`, continue to the next step. If it
   prints `command not found`, install it by following the single command at
   <https://brew.sh>, then run `brew --version` again.

2. **On your MacBook**, install `age`, the encryption tool:

   ```
   brew install age
   ```

   Check it:

   ```
   age --version
   ```

   Expected — a version number such as `v1.2.1`.

3. **On your MacBook**, create the key pair. A key pair is two matching keys: a
   *public* one that can only lock files, and a *private* one that unlocks them.
   The public key goes on the server. The private key never does — so if somebody
   breaks into the server, the backups are useless to them.

   ```
   mkdir -p ~/.age
   ```

   ```
   age-keygen -o ~/.age/dashboard-backup.key
   ```

   Expected:

   ```
   Public key: age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p
   ```

   > **This is the most important thing on the list.** The file
   > `~/.age/dashboard-backup.key` is the only thing that can ever decrypt your
   > backups. If your Mac dies and this file is not somewhere else, every backup
   > you have is permanently unreadable. Nobody can recover it — not Backblaze,
   > not Vultr, not anyone.

4. **On your MacBook**, look at the private key and store a second copy safely:

   ```
   cat ~/.age/dashboard-backup.key
   ```

   Expected — three lines, the last beginning `AGE-SECRET-KEY-`:

   ```
   # created: 2026-08-17T15:04:22-04:00
   # public key: age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p
   AGE-SECRET-KEY-1QYQSZQGPQYQSZQGPQYQSZQGPQYQSZQGPQYQSZQGPQYQSZQGPQYQSZQ4EXAMPLE
   ```

   Record the whole thing as **item 4** on your list, in a password manager. Put a
   printed copy somewhere physical as well. This is the one secret worth that
   trouble.

   Also record the **public key** — the `age1...` line. You need it in step 7.

5. **In a web browser**, create a Backblaze account at
   <https://www.backblaze.com/sign-up/cloud-storage>. Record the password as
   **item 5**.

6. **In the Backblaze web interface**, create somewhere to put the backups. In the
   left-hand menu, under **B2 Cloud Storage**, click **Buckets**, then the
   **Create a Bucket** button.

   | Setting | Value |
   |---|---|
   | Bucket Unique Name | `mason-dashboard-backups` — if taken, add some digits |
   | Files in Bucket are | **Private** |
   | Default Encryption | Disabled — the files are already encrypted before they leave the server |
   | Object Lock | Disabled |

   Write down the exact bucket name you used.

7. **In the Backblaze web interface**, create a key so the server can upload. In
   the left-hand menu click **Application Keys**, then **Add a New Application
   Key**.

   | Setting | Value |
   |---|---|
   | Name of Key | `dashboard-server` |
   | Allow access to Bucket | choose the bucket from step 6, not "All" |
   | Type of Access | **Read and Write** |

   Click **Create New Key**. The next page shows `keyID` and `applicationKey`.

   > **The `applicationKey` is shown once.** Leaving the page loses it, and you
   > would have to create another key. Copy both values now and record them as
   > **item 6**.

8. **Back on the server.** Steps 1 to 4 happened on your MacBook and steps 5 to 7
   happened in a web browser, so the remaining steps need you to connect to the
   server again:

   ```
   ssh mason@100.x.y.z
   ```

   **Check you are in the right place before typing anything secret:**

   ```
   hostname
   ```

   Expected:

   ```
   dashboard
   ```

   If it prints your Mac's name instead — something ending in `.local` — you are
   still on the laptop. Run the `ssh` command above and check again.

   This matters more than it looks. Every command below works on a Mac too, so
   there is no error to warn you; the settings would be written to the laptop and
   the server would have nothing. The giveaway comes later, at the end of step 9:
   on a Mac, `chown root:root` fails with `chown: root: illegal group name`,
   because macOS calls that group `wheel`.

   Now create the file that holds the secrets. It is placed outside the code
   folder, and only the administrator account can read it, so it can never be
   uploaded to GitHub by accident.

   This is the first command in a while that begins with `sudo`, so it will ask:

   ```
   [sudo] password for mason:
   ```

   That is asking for **your `mason` account password from Section 3** — item 3 on
   your list. It is not the server's root password from Vultr, and not your Mac's
   password. `sudo` means "do this one thing as the administrator", so it is
   checking that you are the person at the keyboard, using your own password.

   Nothing appears as you type it. It will not ask again for about fifteen minutes,
   so the remaining commands in this section should not prompt you.

   If you have forgotten that password, see "The `mason` account password" in
   `docs/SECRETS.md` for how to set a new one using Vultr's browser console.

   ```
   sudo mkdir -p /etc/college-dashboard
   ```

   ```
   sudo nano /etc/college-dashboard/env
   ```

   `nano` is a simple text editor inside the terminal. Type the five lines below.

   **Every value shown here is a made-up example.** Four of the five have to be
   replaced with your own. There must be no spaces around the `=` signs.

   ```
   BACKUP_AGE_RECIPIENT=age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p
   BACKUP_B2_BUCKET=mason-dashboard-backups
   RCLONE_CONFIG_B2_TYPE=b2
   RCLONE_CONFIG_B2_ACCOUNT=0035f8e2a91c4d70000000001
   RCLONE_CONFIG_B2_KEY=K003xY9pQr2sT4uV6wX8yZ0aB2cD4eF
   ```

   Where each value comes from:

   | Setting | What to put there |
   |---|---|
   | `BACKUP_AGE_RECIPIENT` | The **public** key printed in step 3, beginning `age1`. Never the one beginning `AGE-SECRET-KEY-`. |
   | `BACKUP_B2_BUCKET` | The bucket name you chose in step 6. |
   | `RCLONE_CONFIG_B2_TYPE` | **Type `b2` exactly as shown.** This one is not yours to change — it names the storage service. |
   | `RCLONE_CONFIG_B2_ACCOUNT` | The **keyID** from step 7. |
   | `RCLONE_CONFIG_B2_KEY` | The **applicationKey** from step 7. |

   The last two names are misleading, and it is worth knowing why:
   `RCLONE_CONFIG_B2_ACCOUNT` does **not** hold your Backblaze email address or
   account name. `rclone`, the program that uploads the backups, refers to the
   keyID as the "account". Both values appeared together on the page immediately
   after you clicked **Create New Key**.

   If you have already navigated away from that page, the `applicationKey` cannot
   be shown again. Go back to step 7 and create a new key, then delete the unused
   one from the **Application Keys** page.

   To save and leave nano: press `Control` and `O` together, press `Return` to
   confirm the filename, then press `Control` and `X` together.

9. Lock the file down so only the administrator can read it:

   ```
   sudo chmod 600 /etc/college-dashboard/env
   ```

   ```
   sudo chown root:root /etc/college-dashboard/env
   ```

### Check before continuing

```
sudo ls -l /etc/college-dashboard/env
```

Expected — note `-rw-------`, meaning only the owner can read it:

```
-rw------- 1 root root 312 Aug 17 15:22 /etc/college-dashboard/env
```

And check the file has all five settings, without showing their values:

```
sudo grep -c '=' /etc/college-dashboard/env
```

Expected:

```
5
```

---

## Section 10 — Start the dashboard

**What this does:** builds and starts the dashboard.

**Before you start:** Sections 8 and 9 finished.

**Time:** about 15 minutes.

### Steps

1. Move into the code folder. Every command in this section runs from there.

   ```
   cd /home/mason/College-Dashboard
   ```

2. Create the small settings file that tells the dashboard which address to listen
   on. Replace the address with your own from Section 6.

   ```
   cp .env.example .env
   ```

   ```
   nano .env
   ```

   Change the line reading `TAILNET_BIND_IP=100.x.y.z` to your real address, for
   example `TAILNET_BIND_IP=100.x.y.z`. Save and exit as before: `Control`
   and `O`, `Return`, `Control` and `X`.

   This address is what keeps the dashboard private. It tells the server to offer
   the dashboard on the private network only.

3. Build and start it. The first build downloads several hundred megabytes and
   takes three to six minutes on this size of server. Later builds take seconds.

   ```
   sudo docker compose up -d --build
   ```

   Expected, at the end:

   ```
   [+] Running 2/2
    ✔ Network college-dashboard_default  Created
    ✔ Container dashboard                Started
   ```

4. Watch it start up:

   ```
   sudo docker compose logs
   ```

   Expected:

   ```
   dashboard  | [entrypoint] Applying any pending database migrations ...
   dashboard  | 2026-08-17T15:31:02-0400 INFO [migrate] Applying 0001_core.sql ...
   dashboard  | 2026-08-17T15:31:02-0400 INFO [migrate] Applied 0001_core.sql.
   dashboard  | 2026-08-17T15:31:02-0400 INFO [migrate] Applied 1 migration(s). Schema version is now 0001.
   dashboard  | [entrypoint] Starting the web server on port 8000 ...
   dashboard  | INFO:     Started server process [1]
   dashboard  | INFO:     Application startup complete.
   dashboard  | INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
   ```

   The `0.0.0.0` in that last line refers to the inside of the container, which is
   sealed off. It does **not** mean the dashboard is public. Step 6 proves that.

5. Confirm the dashboard considers itself healthy:

   ```
   curl -s "http://$(tailscale ip -4):8000/healthz"
   ```

   Copy that line exactly, including the `$(` and `)`. The `$(tailscale ip -4)`
   part runs that command first and drops its answer into the address, so you do
   not have to type your server's private address by hand.

   It has to be the private address rather than `localhost`. Step 2 told the
   dashboard to offer itself on the tailnet address only, and that instruction
   applies to the server talking to itself as much as to anything else — asking
   `localhost` for the dashboard gets no answer, because nothing is listening
   there. That is the arrangement working as intended.

   Expected — one long line. The three `true` values are the important part:

   ```
   {"ok":true,"checks":{"journal_mode_wal":true,"fts5":true,"migrations_up_to_date":true},"schema_version":1,"pending_migrations":[],"database_path":"/data/dashboard.db","database_bytes":110592,"timezone":"America/Indiana/Indianapolis","now":"2026-08-17T19:31:14Z","sync_sources":[],"attention":[]}
   ```

6. Confirm it is **not** listening to the public internet:

   ```
   sudo ss -ltnp | grep 8000
   ```

   Expected — your private `100.` address before the `:8000`:

   ```
   LISTEN 0  4096  100.x.y.z:8000  0.0.0.0:*  users:(("docker-proxy",pid=4412,fd=7))
   ```

   If you see `0.0.0.0:8000` or `*:8000` instead, the dashboard **is** exposed to
   the internet. Stop it at once with `sudo docker compose down`, check the
   `TAILNET_BIND_IP` line in `.env`, and start again.

### Troubleshooting

- **`DATABASE MIGRATION FAILED` and `Could not open the database`** — the folder
  holding the database belongs to the wrong user. The dashboard runs as user id
  1000 inside the container and cannot write to a folder owned by `root`, which is
  what Section 8 step 4 sets right. Check it:

  ```
  ls -ld /srv/dashboard/data
  ```

  If the owner is `root` rather than `mason`, fix it and start again:

  ```
  sudo chown -R 1000:1000 /srv/dashboard/data
  ```

  ```
  sudo docker compose up -d
  ```

- **`env file /etc/college-dashboard/env not found`** — Section 9 step 8 was not
  completed, or the filename is misspelled. Check with
  `sudo ls -l /etc/college-dashboard/`.

- **`required variable TAILNET_BIND_IP is missing a value: set TAILNET_BIND_IP in
  .env — run 'tailscale ip -4' on this server to find it`** — step 2 was skipped,
  or you are running the command from a different folder. Run `pwd` to confirm you
  are in `/home/mason/College-Dashboard`.

- **`permission denied while trying to connect to the Docker daemon socket`** —
  the `sudo` was left off. Every `docker compose` command in this project needs
  it, because the secrets file is readable only by the administrator.

- **`Error response from daemon: driver failed programming external connectivity`
  ... `cannot assign requested address`** — the address in `.env` is not one this
  server actually has. Run `tailscale ip -4` and compare.

### Check before continuing

`curl -s "http://$(tailscale ip -4):8000/healthz"` prints `"ok":true`, and
`sudo ss -ltnp | grep 8000` shows your `100.` address rather than `0.0.0.0`.

---

## Section 11 — Open it on your laptop and your phone

**What this does:** confirms the whole point of the exercise — that you can reach
the dashboard from both of your devices.

**Before you start:** Section 10 finished. Tailscale switched on for both devices.

**Time:** about 10 minutes.

### Steps

1. **On your MacBook**, open Safari and go to this address, with your own private
   address and `:8000` on the end:

   ```
   http://100.x.y.z:8000
   ```

   You should see a page headed **Semester Dashboard**, with a green banner
   reading *Everything checks out*, three checks marked **pass**, and a note that
   nothing runs on a schedule yet.

   It is `http`, not `https`, and Safari may note that the connection is not
   private. That is expected here: the traffic is already encrypted by Tailscale
   between your devices, and the page never leaves that private network.

2. **On your iPhone**, make sure Tailscale is switched on, then open **Safari** and
   go to the same address.

   It must be Safari. Chrome and other browsers on iOS cannot add a page to the
   home screen.

3. Add it to your home screen. Tap the **Share** button — the square with an arrow
   pointing up, at the bottom of the screen. Scroll down the list of options and
   tap **Add to Home Screen**, then tap **Add** in the top right.

4. Close Safari and open the new **Semester** icon from your home screen. It should
   open without Safari's address bar, like an app.

### Troubleshooting

- **The page loads on the MacBook but not on the iPhone.** The Mac working proves
  the dashboard and the private network are both fine, so the cause is on the
  phone. Work through these in order.

  **First, type the address in full, with `http://` on the front:**

  ```
  http://100.x.y.z:8000
  ```

  Without the prefix, Safari often treats what you typed as something to search
  for and hands it to Google, or tries `https://`, which this server does not
  speak. If you landed on a search results page or saw a message about a secure
  connection, this was it.

  **Second, turn off iCloud Private Relay for this network.** Private Relay sends
  Safari's traffic through Apple's servers, which cannot reach an address that
  exists only on your private network. This is the most common cause on an iPhone
  where everything else looks right.

  Open **Settings**, tap **Wi-Fi**, tap the **ⓘ** beside the network you are on,
  and turn **Limit IP Address Tracking** off. If you pay for iCloud+, also check
  **Settings → your name → iCloud → Private Relay**.

  **Third, confirm the VPN is genuinely running.** The switch in the Tailscale app
  and iOS actually having the connection up are two different things. Look at the
  top of the iPhone screen for a **VPN** badge in the status bar. If it is absent,
  the connection is not up regardless of what the app shows. Check
  **Settings → General → VPN & Device Management → VPN**, which should read
  **Connected**.

  **Fourth, confirm the phone is on the tailnet at all.** On your MacBook:

  ```
  /Applications/Tailscale.app/Contents/MacOS/Tailscale status
  ```

  Your phone should appear in that list with a `100.` address of its own. If it
  does not, sign in to the Tailscale app on the phone again, using the same
  account as the other devices.

- **`Safari cannot open the page because the server stopped responding`** — check
  the dashboard is running. On the server: `sudo docker compose ps`. The `STATUS`
  column should read `Up ... (healthy)`.

- **No "Add to Home Screen" option** — you are not in Safari, or you are in a
  Private Browsing tab. Open a normal tab in Safari.

### Check before continuing

The dashboard loads on the MacBook, loads on the iPhone, and opens from the iPhone
home screen icon without an address bar.

**This is the main goal of this milestone.** Everything after this is about not
losing the data.

---

## Section 12 — Schedule the nightly backup

**What this does:** arranges for a backup to be taken every night at 3:15am,
encrypted, and copied to Backblaze.

**Before you start:** Sections 9 and 10 finished.

**Time:** about 10 minutes.

### Steps

1. Run a backup by hand first, to prove it works before trusting it to a schedule.
   It runs as the administrator because the encryption key and Backblaze
   credentials are in the administrator-only file.

   ```
   sudo /home/mason/College-Dashboard/ops/backup.sh
   ```

   Expected — five lines:

   ```
   2026-08-17T19:44:03Z [backup] copying /srv/dashboard/data/dashboard.db
   2026-08-17T19:44:03Z [backup] copy verified: intact, 8 tables
   2026-08-17T19:44:03Z [backup] encrypted to dashboard-20260817T194403Z.db.age (4128 bytes)
   2026-08-17T19:44:05Z [backup] copied off-site to b2:mason-dashboard-backups/dashboard/dashboard-20260817T194403Z.db.age
   2026-08-17T19:44:05Z [backup] done: dashboard-20260817T194403Z.db.age
   ```

2. Check it appears at Backblaze. In the Backblaze web interface, click
   **Buckets**, then **Browse Files** on your bucket. There should be a `dashboard`
   folder containing a file ending in `.db.age`.

3. Install the schedule:

   ```
   sudo crontab -e
   ```

   If it asks which editor to use, choose the number next to `nano`.

   A file opens. Move to the bottom and add these two lines exactly:

   ```
   15 3 * * * /home/mason/College-Dashboard/ops/backup.sh >> /var/log/dashboard-backup.log 2>&1
   0 4 1 * * tail -n 2000 /var/log/dashboard-backup.log > /var/log/dashboard-backup.log.tmp && mv /var/log/dashboard-backup.log.tmp /var/log/dashboard-backup.log
   ```

   The first runs the backup at 3:15am daily. The second trims the log once a
   month so it cannot grow without limit.

   Save and exit: `Control` and `O`, `Return`, `Control` and `X`. You should see:

   ```
   crontab: installing new crontab
   ```

### Troubleshooting

- **`BACKUP_AGE_RECIPIENT is not set`** — the secrets file is missing that line,
  or has a space around the `=`. Reopen it with
  `sudo nano /etc/college-dashboard/env`.

- **`Failed to copy: failed to authorize account`** — the Backblaze keyID or
  applicationKey is wrong. Application keys cannot be viewed again after creation,
  so create a new one following Section 9 step 7 and update the file.

- **`age is not installed`** — run `sudo apt install -y age`.

### Check before continuing

```
sudo crontab -l | grep backup.sh
```

Expected:

```
15 3 * * * /home/mason/College-Dashboard/ops/backup.sh >> /var/log/dashboard-backup.log 2>&1
```

And reload the dashboard in your browser. Under **Automatic jobs** there should
now be a **Nightly backup** entry marked `ok`.

---

## Section 13 — Prove a backup can be restored

**What this does:** takes a real backup out of Backblaze, decrypts it on your
MacBook, and checks it is a working database.

**Why:** a backup nobody has ever restored is a guess. This is the only step that
turns it into a fact. It also rehearses the situation that actually matters — the
server is gone and you are recovering from your laptop.

**Before you start:** Section 12 finished, with at least one backup at Backblaze.
This all happens on the **MacBook**.

**Time:** about 20 minutes.

### Steps

1. **In the Backblaze web interface**, click **Buckets**, **Browse Files**, open
   the `dashboard` folder, click the most recent `.db.age` file, and choose
   **Download**. It lands in your `Downloads` folder.

2. **On your MacBook**, open Terminal and go to your copy of the code:

   ```
   cd ~/College-Dashboard
   ```

   If you do not have one on the Mac, get it first:

   ```
   git clone https://github.com/MasonM5208/College-Dashboard.git ~/College-Dashboard
   ```

3. Restore it to a temporary file. Replace the filename with the one you
   downloaded:

   ```
   ./ops/restore.sh ~/Downloads/dashboard-20260817T194403Z.db.age /tmp/restore-test.db
   ```

   Expected:

   ```
   [restore] decrypting /Users/masonmiller/Downloads/dashboard-20260817T194403Z.db.age
   [restore] checking the restored file

   [restore] Restored successfully to /tmp/restore-test.db
   [restore]   integrity check: ok
   [restore]   schema version:  0001

   What it contains:
   courses  0
   assignments  0
   reminder_instances  0
   audit_log entries  0

   This copy is a plain, unencrypted database file. Delete it when you are done:
     rm /tmp/restore-test.db
   ```

   The counts are zero because nothing has been entered yet. `integrity check: ok`
   is the line that matters — it means the file is a complete, undamaged database.

4. Write down what you saw. `docs/OPERATIONS.md` has a section for recording the
   date of your last successful restore test. Do this one every few months.

5. Delete the unencrypted copy:

   ```
   rm /tmp/restore-test.db
   ```

### Troubleshooting

- **`no age private key at /Users/masonmiller/.age/dashboard-backup.key`** — the key
  from Section 9 step 3 is missing or on a different Mac. Restore it from your
  password manager into that path, or pass its location with
  `--identity /path/to/key`.

- **`age: error: no identity matched any of the recipients`** — the key on the
  server does not match the private key on your Mac. Check that
  `BACKUP_AGE_RECIPIENT` in the server's secrets file is the `age1...` public key
  printed alongside this private key.

- **`the restored database is damaged`** — do not ignore this. Try an older backup
  from Backblaze; if that also fails, see `docs/OPERATIONS.md`.

### Check before continuing

`ops/restore.sh` printed `integrity check: ok`, and you have deleted the temporary
file.

---

## Section 14 — Connect your Canvas calendar

**What this does:** gives the dashboard the address of your Canvas calendar, so
assignments arrive on their own every 30 minutes.

**Why:** your institution has switched off Canvas API access, so this calendar feed
is the only automatic source of anything. Everything the dashboard knows without
you typing it comes through here.

**Before you start:** Section 10 finished, with the dashboard running.

**Time:** about 15 minutes.

### Steps

1. **In a web browser**, sign in to Canvas and open **Calendar** from the left-hand
   menu.

2. Scroll to the bottom of the right-hand sidebar, below the list of courses, and
   look for a button labelled **Calendar Feed**. Clicking it opens a small box
   containing a long web address ending in `.ics`.

   If you cannot find the button, the sidebar may be collapsed on a narrow window.
   Widen the browser window, or look for a link with the same name at the bottom of
   the page.

3. Copy that address.

   > **Treat this address like a password.** It contains a token, and anybody who
   > has it can read your entire academic schedule — every course, assignment and
   > due date — without signing in, and without you being told. Do not paste it
   > into a chat, an email, or any file inside the project folder. If it does get
   > out, Section "Canvas feed address" of `docs/SECRETS.md` explains how to reset
   > it.

4. **On the server**, open the secrets file:

   ```
   ssh mason@100.x.y.z
   ```

   ```
   hostname
   ```

   Confirm this prints your server's name, not your Mac's, before continuing.

   ```
   sudo nano /etc/college-dashboard/env
   ```

5. Add one line at the end, with your address after the `=` and no spaces around
   it:

   ```
   CANVAS_ICS_URL=https://iu.instructure.com/feeds/calendars/user_EXAMPLETOKEN.ics
   ```

   Save and exit: `Control` and `O`, `Return`, `Control` and `X`.

6. Restart so the dashboard picks up the new setting:

   ```
   cd /home/mason/College-Dashboard
   ```

   ```
   sudo docker compose up -d
   ```

7. Watch the first check run. It happens a few seconds after startup:

   ```
   sudo docker compose logs --tail 20
   ```

   Expected — a line reporting what arrived:

   ```
   dashboard  | 2026-08-17T20:02:07-0400 INFO [scheduler] Canvas polling every 30 minutes.
   dashboard  | 2026-08-17T20:02:12-0400 INFO [canvas] Canvas sync: 12 events: 12 new, 0 moved, 0 retitled, 0 vanished, 0 returned, 0 awaiting a course
   ```

### Troubleshooting

- **`Canvas refused the calendar feed address (HTTP 403)`** — the address is wrong,
  or it has been reset in Canvas since you copied it. Get it again from
  **Calendar → Calendar Feed**.

- **`Canvas has no calendar at that address (HTTP 404)`** — usually a partial copy.
  The address is long; make sure you have all of it, ending in `.ics`.

- **`This does not look like a calendar feed`** — Canvas returned a web page rather
  than a calendar, which happens when the address has expired. Copy it again.

- **`CANVAS_ICS_URL is not set`** — the line did not save, or has a space around the
  `=`. Reopen the file and check.

- **The log says nothing about Canvas at all** — the setting is missing entirely.
  Confirm with `sudo grep -c CANVAS /etc/college-dashboard/env`, which should print
  `1`.

### Check before continuing

Open the dashboard on your phone or laptop and tap **See them all**. Your Canvas
assignments should be listed, grouped by course.

Two things will look odd, and both are expected:

- **Courses are named after enrolment codes** like `FA26-BL-MATH-M211-2050`.
  Canvas puts no readable course name in the feed, so the code stands in until you
  can rename it. That arrives with the next milestone.
- **Most of your courses are missing.** Only work with a due date set in Canvas
  appears. Courses that do not use Canvas that way — your music courses, most
  likely — produce nothing at all. This list is a floor, not the full picture, and
  entering the rest by hand is the next milestone's job.

---

## Section 15 — Turn on the chat

**What this does:** lets you ask the dashboard questions in plain English, about
your own deadlines or about anything else.

**Why:** it is the difference between a list you read and an assistant you ask.
It knows your courses, what is due, and how much spare time you have before each
deadline, so "can I finish all this by Thursday" is a question it can actually
answer.

**Before you start:** Section 14 finished, with assignments arriving.

**Time:** about 15 minutes.

**What it costs:** a few dollars a month at ordinary use. The dashboard shows a
running total for the current month so it never surprises you, and the last step
of this section explains how to halve it if it climbs.

### Steps

1. **In a web browser**, go to <https://console.anthropic.com> and create an
   account if you do not have one.

2. Add a payment method. Look for **Billing** in the left-hand menu, or under your
   account name in the top right.

   > **Set a spending limit while you are there.** Look for a monthly limit or
   > budget field on the same billing page and set it to something you would not
   > mind losing — $25 is generous for this. It converts the worst case from a
   > surprising bill into a chat that stops working.

3. In the left-hand menu, click **API Keys**, then **Create Key**. Name it
   `dashboard`.

   > **The key is shown once.** Copy it now and record it as **item 7** on your
   > list. If you leave the page without copying it, delete it and make another.

4. **On the server**, open the secrets file:

   ```
   ssh mason@100.x.y.z
   ```

   ```
   hostname
   ```

   Confirm this prints your server's name before continuing.

   ```
   sudo nano /etc/college-dashboard/env
   ```

5. Add one line, with your key after the `=` and no spaces around it:

   ```
   CLAUDE_API_KEY=sk-ant-api03-EXAMPLEEXAMPLEEXAMPLE
   ```

   Save and exit: `Control` and `O`, `Return`, `Control` and `X`.

6. Restart:

   ```
   cd /home/mason/College-Dashboard
   ```

   ```
   sudo docker compose up -d
   ```

### Check before continuing

Open the dashboard and tap **Ask**. The warning about chat not being switched on
should be gone. Ask it:

```
what's due this week?
```

The reply should name your actual assignments, with real due dates. Watch for the
word **Reasoning** appearing above the answer — you can tap it to see how it
worked the answer out.

Then ask it two more things, because they check different things:

- **Something with no schedule in it at all**, such as `explain what a limit is in
  calculus`. It should answer properly. It is a general assistant, not only a
  deadline reader.
- **Something about a message**, such as `what did my professor email me about the
  recital?`. It should tell you plainly that it has no archive of your messages
  yet. If it invents an email, tell me — that is the one failure that matters here,
  and it is the reason the archive milestone exists.

### Troubleshooting

- **The page still says chat is not switched on** — the line did not save, or has
  a space around the `=`. Check with
  `sudo grep -c CLAUDE_API_KEY /etc/college-dashboard/env`, which should print `1`.

- **`Something went wrong talking to Claude`** — the detail is in the log, kept out
  of the browser because an API error can quote the request and the request carries
  your key. Run `sudo docker compose logs --tail 30`.

- **`authentication_error`** in the log — the key is wrong or was deleted. Make a
  new one at the console and update the secrets file.

- **`credit balance is too low`** — add credit at the console's billing page.

- **The answer starts, then stops partway** — usually a network drop. Reload the
  page; if the answer was saved it will be there.

### If it costs more than you want

The dashboard shows the running monthly total at the bottom of the **Ask** page
and on the status page. If it climbs higher than you like, one line halves it —
add this to the secrets file and restart:

```
CHAT_MODEL=claude-sonnet-5
```

That switches to a cheaper model that is still very capable. Nothing else changes,
and you can switch back the same way.

---

## Section 16 — Reminders on your phone

**What this does:** puts your deadlines into Apple Reminders, so your phone nudges
you at the right times without you opening anything.

**Why this and not a notification from the dashboard itself:** a web notification
on an iPhone needs the app installed to the home screen, a permission prompt, and
a registration that **iOS quietly drops if you have not opened the app for a few
weeks** — which is exactly what happens in a busy stretch, when the reminders
matter most. Apple Reminders has none of that. Once an alert is on your phone it
fires whether or not the server is running.

**Before you start:** Section 14 finished, with assignments arriving.

**Time:** about 20 minutes.

### Steps

1. **In a web browser**, sign in at <https://account.apple.com> with your Apple ID.

2. Find the sign-in and security section, then **App-Specific Passwords**. Create
   one and name it `dashboard`.

   > **This is not your Apple ID password, and your Apple ID password will not
   > work here.** An app-specific password is a separate one you can revoke on its
   > own. It looks like `abcd-efgh-ijkl-mnop`, and it is **shown once** — copy it
   > now and record it as **item 9** on your list.

3. **On the server**, open the secrets file:

   ```
   ssh mason@100.x.y.z
   ```

   ```
   hostname
   ```

   Confirm this prints your server's name before continuing.

   ```
   sudo nano /etc/college-dashboard/env
   ```

4. Add two lines — your Apple ID email, and the app-specific password with its
   dashes exactly as shown to you:

   ```
   CALDAV_USERNAME=you@example.com
   CALDAV_PASSWORD=abcd-efgh-ijkl-mnop
   ```

   Save and exit: `Control` and `O`, `Return`, `Control` and `X`.

5. **Check the connection before anything is written.** This looks for the list it
   would use and stops — it creates nothing:

   ```
   cd /home/mason/College-Dashboard
   ```

   ```
   sudo docker compose run --rm app python -m app.caldav_push --probe
   ```

   Expected — five steps, ending with the list it will use:

   ```
   Reminders server: https://caldav.icloud.com
   Signing in as:    you@example.com

     step 1  asking https://caldav.icloud.com who we are signed in as
     step 2  account is https://p01-caldav.icloud.com/1234567/principal/
     step 3  calendar home is https://p01-caldav.icloud.com/1234567/calendars/
     step 4  found 3 calendar(s):
             Home — calendar events only
             Work — calendar events only
             Reminders — accepts reminders
     step 5  will write to: Reminders

     Reminders will be written to:
       https://p01-caldav.icloud.com/1234567/calendars/reminders/
   ```

   **Do not continue until this succeeds.** Whatever step it stops at is the thing
   to fix, and the troubleshooting below is arranged by step.

6. Restart so the sync starts running:

   ```
   sudo docker compose up -d
   ```

   It syncs a few seconds after startup, then every 15 minutes.

### Troubleshooting

- **`Apple rejected the sign-in (HTTP 401)`** — almost always the password. A
  normal Apple ID password does not work; it must be an app-specific one. Make a
  new one and update the secrets file.

- **It stops after step 1** — the address is wrong. Remove any `CALDAV_URL` line
  and let it use Apple's default.

- **`The account has calendars but none of them accept reminders`** — your
  reminder lists are stored on the phone rather than in iCloud. On the iPhone, open
  **Settings → your name → iCloud** and turn **Reminders** on, then run the probe
  again.

- **`Could not reach the reminders server`** — the server has no internet. Check
  with `curl -s -o /dev/null -w '%{http_code}' https://www.apple.com`.

- **Nothing appears on the phone after 20 minutes** — check the log:
  `sudo docker compose logs --tail 30 | grep -i caldav`.

### Check before continuing — this is the milestone

1. Open **Reminders** on your iPhone. Your assignments should be there, one item
   each, with due dates.

2. Now prove the part that matters. In the dashboard, add a piece of work due in
   about **three hours and ten minutes** — its 3-hour nudge then falls a few
   minutes from now.

3. Force a sync rather than waiting:

   ```
   sudo docker compose restart
   ```

4. **Close the laptop. Put it away.** Wait for the alert.

When your phone buzzes with the laptop shut, M3 is done — and so is everything
SPEC calls the useful core.

---

## You are finished

The foundation is running:

- A private server that only your devices can reach.
- A database with a proper structure, kept in step by migrations.
- A dashboard on your laptop and your phone's home screen.
- Encrypted backups every night, stored somewhere other than the server, and
  proven to restore.

### What is not built yet

The dashboard currently shows its own status and nothing else. Coming next, in
order:

1. **Assignments from Canvas** — pulled in automatically every half hour.
2. **The daily view** — what to work on, ordered by how much time you actually
   have rather than by which deadline is nearest.
3. **Reminders** — arriving on your phone through Apple Reminders.

### Day-to-day from here

- To check on it: `docs/OPERATIONS.md`.
- If something breaks: `docs/OPERATIONS.md` has a "the site won't load" walkthrough.
- Your passwords and keys: `docs/SECRETS.md` lists what each one is for and how to
  replace it.

### Keep this

Somewhere you will find it again:

- The server's private address, beginning `100.`
- The `mason` account password
- The age private key file, `~/.age/dashboard-backup.key`
- Your Backblaze bucket name
