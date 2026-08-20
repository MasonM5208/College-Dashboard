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
"Free hours" comes from **Your week** — your per-day budget, minus your classes
and rehearsals, minus your practice time. Until you set that up it assumes four
productive hours a day between 8am and 10pm, which is a rough constant rather
than anything about you.

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

Answers come back formatted — bold, lists, headings, and indented blocks for
anything that needs to keep its alignment. **Maths is written in plain characters**
(`lim(x→1)`, `x²`, `f'(x)`) rather than typeset, because there is no maths renderer
on the page and on a phone the plain version is easier to read anyway. If you ever
see stray dollar signs or backslashes like `\frac`, that is the model slipping back
into LaTeX — worth telling me, since the instruction is meant to prevent it.

It will say **Reasoning** above longer answers; tap it to see how it worked
something out. Below each answer is what that answer cost, and the bottom of the
page shows the running total for the month.

**It can read what you have saved to the archive**, and any claim it makes from a
saved message comes with a tappable link to the original — tap it and you see the
message itself, word for word. An answer that leans on your messages *without* a
link is flagged with a red **No citation** warning; treat that answer as unchecked
and go and look. It cannot see anything you have not saved, and it will say so
rather than filling the gap.

**It cannot change anything.** Marking work done, adding assignments and editing
courses are all yours — it reads, it does not write.

### Keeping conversations apart

Each question you ask is part of a **conversation**, and a conversation is the
unit that gets remembered. Ask a follow-up and it knows what you were just
talking about; the whole conversation goes back to Claude every time you add to
it.

The name of the one you are in sits directly above the question box.

- **Opening the Ask page starts a new conversation.** A question about Tuesday's
  lab has nothing to do with the paper you asked about last week, and mixing them
  makes both harder to read later.
- **To carry on with an earlier one**, tap its name under **Earlier
  conversations** and ask there. Everything said before comes back with it.
- **To leave one**, tap **New conversation** next to the name.

Under the transcript, a conversation you have open can be:

- **Renamed.** It is named after your first question until you say otherwise, and
  first questions all start to look alike by October. `Bio 105 lab writeups` is
  easier to find again than `hey what do I need for`.
- **Kept.** Kept conversations sort to the top of the list however old they get,
  which is what you want for the two or three you actually return to.
- **Deleted.** It asks first, and then it is gone — the messages with it. Nothing
  in the dashboard undoes that. Last night's backup still has it, and
  `docs/OPERATIONS.md` covers reading a deleted conversation out of a restored
  copy, but that is a chore, so read the confirmation before tapping through it.

**All conversations**, linked at the top of the Ask page, lists every one you have
had with the same three controls on each.

One practical note about cost: because a whole conversation is re-sent on every
question, a long one costs more per question than a fresh one. Starting a new
conversation when the subject changes keeps answers sharper *and* the bill lower.

### Save an email

**On the phone:** open it, tap the share button, tap **Save to Semester**. A
notification says **Saved to the archive.** That is the whole thing.

**On the laptop:** **Archive**, then **Paste something in**. Only the message
itself is required — subject, sender and date are filled in if you have them and
guessed at if you do not.

What gets kept is the message **exactly as it arrived**. Nothing edits it
afterwards — not you, not the chat, not a later version of this dashboard. That
is enforced by the database, not just by good intentions, and it is the reason the
archive is worth anything: when your memory of a deadline disagrees with a
professor's, this is the copy that settles it.

**Save the same thing twice and it is kept once.** Before saving, the quoted reply
chain, the signature and the "Sent from my iPhone" are stripped off and what
remains is fingerprinted. So an email shared from Mail and the same message pasted
out of Canvas are recognised as one, kept once, with a note of both routes. Open a
message and look under **How it got here** to see them.

There is no penalty for saving something you are not sure about. Storage is not
the constraint; remembering to save is. Save it.

### Deal with the review queue

IU's Outlook forwards your mail to a mailbox the dashboard reads. What it finds
does **not** go into the archive — it waits for you to say whether it belongs
there. The **Archive** link on Today shows a number when something is waiting.

**Keep** puts the message in, word for word, and makes it searchable. **Discard**
means the dashboard never offers it again. Neither touches your actual mailbox.

Expect to discard most of it. You are forwarding an entire university account, so
most of what arrives is a parking notice. That is the arrangement working, not
failing: the value is in the handful you keep, because searching thirty things you
chose beats searching three thousand you did not.

If it has piled up, **Discard all** at the bottom of the review page clears it.
Nothing is lost — the mail is still in your mailbox, and anything you later want
can be saved by hand from the phone.

**Something you already saved appears anyway?** Worth reporting. The dashboard is
meant to recognise a forwarded copy of something you saved from the share sheet
and record it as another route rather than asking again.

### Find something you saved

**Archive**, then type. Words match from the start, so `lab` finds *labs* and
*laboratory*. Matches are highlighted in the results.

Two things worth knowing:

- **The subject counts for much more than the body.** A word in a subject line is
  a far better signal than the same word buried three paragraphs down, and the
  ranking reflects that.
- **This is keyword search, not a question.** Search for words that would appear
  in the message — `makeup exam`, `rescheduled`, `rubric` — not for what you want
  to know. To ask a question, use **Ask**: it searches the archive for you and
  cites what it finds.

Tap a course chip on a result, or on the row of course buttons, to narrow a search
to one course.

**Attaching a message to a course is manual and always will be.** Nothing guesses.
A wrong link in this archive would be worse than no link, because the whole point
of it is that everything here is something you know to be true.

### Name a course properly

Canvas gives no readable course name in its calendar, only an enrolment code like
`FA26-BL-MATH-M211-2050`. Courses arrive named after that code and appear at the
bottom of Today under **Courses still named after a code**. Type a real name, tap
**Save**, and it is done once and for all.

---

## Your week, and what it will actually hold

Everything on Today is ranked against how many hours you really have, not how
many hours are in the day. **Your week** is where you tell it.

### Set it up once, in about ten minutes

**Hours per day.** For each day, two numbers:

- **Budget** — hours of coursework you can genuinely do that day. Not hours
  awake. Four is a normal figure; a Tuesday with a full teaching day and a
  rehearsal might honestly be one.
- **Practice** — hours at the instrument. This is protected. It comes out before
  any assignment is ranked, so a deadline can never quietly eat it.

That second one is deliberate and worth understanding, because it looks like a
stray setting until you see why. Practice has no due date. In any ranking that
sorts by urgency it loses every single comparison, every day, quietly — which is
exactly how a performance major reaches October having not practised properly
since August. Taking it off the top means the maths protects it without practice
ever having to win an argument it cannot win.

**Fixed commitments.** Classes, rehearsals, lessons, shifts — anything at the same
time every week. These come out of the day before coursework is counted. Add them
once and they apply every week.

If a rehearsal stops after the concert, **switch it off** rather than deleting it.
It probably comes back next term, and re-typing it is exactly the friction that
ends with the model being wrong and nobody noticing.

### What it does with them

Each day is worth the **smaller** of your budget and what is actually unbooked,
minus practice. A day with eight free hours and a four-hour budget gives four. A
day with a twelve-hour rehearsal gives two, whatever the budget says. The
**Your week** page shows all seven days with the numbers that produced each.

Nothing changes until you edit it. Out of the box every day is four hours with no
practice, which is exactly what the dashboard already assumed, so setting this up
is the moment the rankings start being about your actual life.

---

## When the week does not fit

If the work due in the next seven days needs more hours than exist, Today says so
at the top, in hours:

> **31 hours of work, 22 hours available.** You are 9 hours short.

Then it names what is cheapest to let slide, in order, with why each one is cheap
— a course that takes no late penalty, or an assignment worth few points.

**This is the most useful thing here.** It is the calculation you would otherwise
attempt at 1am on a Wednesday with worse information and less sleep. Nothing is
hidden behind a toggle and nothing is softened, because a system that tells you
everything is fine when it is not is worse than no system.

Two honest limits:

- **Work with no estimate is not counted.** So a shortfall is a floor — the real
  gap can only be larger, never smaller.
- **If nothing has points or a late policy recorded**, it says so rather than
  guessing at which is cheapest. Filling those in on **Courses** is what turns the
  ordering from "frees the most hours" into a real recommendation.

---

## The timer

**Start** on Today starts a clock as well as marking the work started. **Stop**
books the time and takes it off what is left — 90 minutes against a three-hour
task leaves an hour and a half.

The running clock stays pinned to the top of every screen. That is on purpose: a
timer you cannot see is a timer left running overnight, and one twelve-hour
entry distorts everything measured after it.

Starting a timer on something else stops the first one automatically. You do not
have to go and find it.

### What the timer is for

After three finished pieces of work of a kind, **Your week** tells you how far off
your estimates for that kind actually are — *"papers take about 2.1× your
estimate, over 4 timed"*. Most people underestimate papers by roughly double.

**It never changes a number you typed.** It tells you the multiplier and leaves
the applying to you. An estimate that silently doubled itself would make every
number on the screen untrustworthy, including the ones that are right.

---

## Study sessions that appear on their own

Give an exam a time estimate and, if it is more than a week away, the dashboard
breaks that estimate into four study sessions — eight, five, three and one day
before — and puts them in the list like any other work. They are ranked, they can
be timed, and they count against your capacity, which is the point: eight hours of
revision that only exists in your head competes with nothing.

Each one says **Made automatically from an exam** so you can never mistake it for
something you entered and forgot.

**Nothing is generated until you estimate the exam.** That is the on switch. The
dashboard will not invent a number for how long revision takes.

**To stop them for one exam**, tap *Stop making these for that exam* on any of its
sessions. That decision sticks — they will not come back. Sessions you have
already started or finished are never removed, by that button or by anything else.

If you move the exam and have not started any of them, the ladder rebuilds around
the new date.

---

## Sunday, five minutes

**Review** asks three questions in the order that is easiest to avoid:

1. **What slipped** — overdue work, with one tap each to mark it done, let it go,
   or move the date.
2. **What needs an estimate** — these cannot be ranked at all without one.
3. **What is coming**, against the hours the week actually holds.

It also shows what you finished and what each piece took against what you
guessed. That is the calibration data becoming something you can see rather than
a number in a table.

---

## Reminders on your phone

Every assignment with a due date becomes one item in Apple **Reminders**, carrying
all of its alert times. A worksheet gets two nudges, a paper five, an exam four —
the ladder depends on the kind of work, and you do not set any of it up.

Two things worth knowing:

- **Alerts fire from your phone, not from the server.** Once an alert is on the
  phone it goes off whether or not the dashboard is running. A server outage at 7am
  on a Saturday does not cost you the reminder.
- **Nothing arrives between 10:30pm and 7:30am.** Anything that would land in that
  window moves to the nearest edge — deadline warnings move earlier, "time to
  start" nudges move to the morning.

### Ticking one off in Reminders does nothing here

This is the one asymmetry to keep in mind. Marking a reminder done on your phone
does **not** mark the work done in the dashboard — reading that back needs work
that is not built.

**The dashboard is the record of what you have done.** Tap **Done** there, and the
reminder disappears from your phone at the next sync. Do it the other way round and
the dashboard will keep thinking the work is outstanding, and keep counting its
hours against your week.

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
| **Ask** | Questions about your schedule, your saved messages, or anything else |
| **Archive** | Everything you have saved, and the search over it |
| **Your week** | Hours per day, fixed commitments, and how good your estimates are |
| **Review** | The Sunday five minutes |
| **Review** | Forwarded mail waiting for you to keep or discard |
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
- **Nothing enters the archive on its own.** Forwarded mail is *collected*
  automatically but never *archived* automatically — it waits in the review queue
  until you keep it. So the chat finding nothing means *not kept*, never *not
  said*, and it is written to tell you which.
