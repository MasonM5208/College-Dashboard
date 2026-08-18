# Using the dashboard

For the owner, every day. Short on purpose — if this took long to read, the thing
it describes would be too fussy to keep using.

Open it from the **Semester** icon on your phone's home screen, or in a browser at
your server's address. Tailscale has to be on.

---

## The morning look

The first screen is **Today**. Read it top to bottom.

### 1. Anything asking for an estimate

Items that arrived from Canvas have no idea how long they will take, and nothing
can be ranked without that. They sit in a block at the top:

```
3 need an estimate before they can be ranked

  Chapter 2 Homework      BIOL-L112 · due Wed 26 Aug
  [15m] [30m] [1h] [2h] [4h] [8h]
```

Tap the closest size. Do not agonise — a rough number that exists beats an accurate
one that does not, and being wrong is recoverable. Expect to underestimate papers
by roughly double at first; almost everyone does, and M6 will start correcting for
your particular bias automatically.

Once tapped, the item drops into the ranking below.

### 2. Start with this

The single most pressing thing, in a box of its own:

```
START WITH THIS
Readiness Assurance Test (RAT) 1
Calculus I
due Thu 27 Aug · 3h of work left · 12.1h free before then
9.1h to spare
                          [Start]  [Done]
```

That is the answer to "what do I do next". If you do nothing else with this app,
do that one thing.

### 3. Everything else, in order

Below it, the same information for everything coming up, in the same order of
pressure.

---

## Why the order looks wrong sometimes

**It is not sorted by due date.** That is deliberate, and it is the most useful
thing here.

Sorting by deadline puts a 20-minute worksheet due tomorrow above a 14-hour paper
due Thursday. That is backwards: the worksheet fits in a gap between classes, the
paper does not, and by the time the paper is nearest it is too late to write.

So each item is ranked by how much **spare time** is left:

```
spare time  =  free hours before it is due  −  hours of work left
```

Every item shows both numbers, so you can always check the arithmetic yourself.
"Free hours" assumes four productive hours a day, spread between 8am and 10pm.
That is a rough constant for now; later it will be replaced by your real schedule,
with classes, rehearsals and practice already subtracted.

**Negative spare time means you are already behind on that item** — there is less
time before it is due than the work needs. It says so plainly:

```
4.5h short of the time needed
```

That warning appears days before a deadline-sorted list would show anything wrong
at all. It is the single most valuable thing on the screen.

### Forcing something to the top

If the arithmetic disagrees with you, you win. Pinning an item keeps it at the top
regardless. Use it when something matters for a reason the app cannot know.

---

## The things you do most

### Mark something done

Tap **Done**. It leaves the list immediately and stops taking up time in the
calculation.

### Say you have started something

Tap **Start**. It stays in the list, marked *in progress*, and gets a small nudge
upward so you are not pulled off it by something marginally more urgent. The nudge
is deliberately small and cannot outweigh a genuine emergency.

### Capture a thought

The box at the very top of Today takes anything:

```
Capture anything — sort it out later          [Add]
```

Type it, tap **Add**, carry on. It lands under **Needs a date** at the bottom until
you give it one. Use it constantly — for something a professor said in class, a
book to find, a question to ask. Anything you have to stop and categorise is
something you will not bother writing down at all.

### Name a course properly

Canvas gives no readable course name in its calendar, only an enrolment code like
`FA26-BL-MATH-M211-2050`. Courses arrive named after that code and appear at the
bottom of Today under **Courses still named after a code**. Type a real name, tap
**Save**, and it is done once and for all.

---

## What this does not know about

**Only work with a due date set in Canvas appears automatically.** If a professor
assigns something in class, on a paper syllabus, or without setting a Canvas due
date, it is invisible here.

For you specifically, that means **most of your courses are not represented**. Your
music courses produce nothing at all. Treat the list as a floor, never as the full
picture, and keep using quick capture for everything else.

Entering assignments by hand, and typing up a syllabus in one go, are the next
piece of work.

---

## Other screens

| Screen | What it is for |
|---|---|
| **Today** (the front page) | The daily view described above |
| **Everything** | Every assignment grouped by course, including finished ones |
| **Status** | Whether the server, the database and the Canvas sync are healthy |

Worth glancing at **Status** about once a month, or any time the assignment list
looks suspiciously quiet. `docs/OPERATIONS.md` explains what to do about anything
it reports.

---

## Things that will surprise you

- **A change in Canvas takes up to an hour to appear.** Canvas caches its calendar
  on its own servers. Anything due within the next two hours should be checked in
  Canvas directly rather than trusted here.
- **Marking something done here does not change Canvas.** This dashboard is the
  record of what you have done; Canvas is the record of what was assigned. They do
  not talk back to each other.
- **Nothing is ever deleted because Canvas stopped mentioning it.** If an
  assignment disappears from the feed, it is flagged rather than removed, because a
  temporary Canvas fault looks exactly like a deleted assignment.
