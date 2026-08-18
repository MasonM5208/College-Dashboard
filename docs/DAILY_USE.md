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

### Add work Canvas does not know about

**Add work** on the Today screen. Title, course, kind of work, when it is due, how
long it will take. The estimate fills itself in from the kind of work you pick —
change it if you know better, but it is never saved without you seeing it.

Dates can be typed however you think of them: `2026-09-08`, `9/8`, `Sep 8`, or
`Sep 8 2pm`. With no time, it means the end of that day. With no year, it means the
next one to come, so typing a syllabus in August files December correctly.

### Type up a whole syllabus at once

**Paste a syllabus**. Pick the course, then one assignment per line:

```
Species counterpoint 1 | 2026-09-08 | 2h
Species counterpoint 2 | 9/15 | 2h
Listening journal wk3 | Sep 17
Midterm exam | 10/6 | 6h | exam
```

Title first, then the due date, then how long it takes, separated by `|`. Only the
title is required. A fourth part sets the kind of work if the title does not make
it obvious.

Press **Check it** and you get a preview of exactly what would be created.
**Nothing is saved until you press Save**, and any line that could not be read is
shown with the reason, left out, and waiting for you to fix it. A whole 14-week
syllabus takes about two minutes this way.

### Fill in a captured note

Anything in **Needs a date** has an **Add details** button. That opens the same
form, already holding what you captured, so you can give it a course, a date and a
length and let it join the ranking.

### Ask it something

**Ask**, at the top of Today. It knows today's date, your courses, everything due
in the next two weeks, and how much spare time you have before each deadline —
that context goes with every question, so you never have to set the scene.

Worth asking:

```
what should I do first today?
am I going to be able to finish all this by Thursday?
what's the least damaging thing to skip this week?
explain species counterpoint to me like I've forgotten the lecture
```

The last one is not a mistake — it answers general questions as readily as
schedule ones. There is no mode to switch.

It will say **Reasoning** above longer answers; tap it to see how it worked
something out. Below each answer is what that answer cost, and the bottom of the
page shows the running total for the month.

**It cannot see your messages or emails.** That archive is not built yet, and it
will tell you so rather than guessing. If it ever appears to remember an email,
that is a bug worth reporting.

**It cannot change anything.** Marking work done, adding assignments and editing
courses are all yours — it reads, it does not write.

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

So for anything not in Canvas, **Add work** and **Paste a syllabus** are how it
gets in, and quick capture is for the rest. The system is only as complete as what
you put into it.

---

## Other screens

| Screen | What it is for |
|---|---|
| **Today** (the front page) | The daily view described above |
| **Ask** | Questions about your schedule, or about anything else |
| **Add work** | One assignment Canvas does not know about |
| **Paste a syllabus** | A whole term of assignments in one go |
| **Courses** | Adding and editing courses, instructors, meeting times, late policies |
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
