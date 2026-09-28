# Reading a SAR chip

The rubric both readers work from. Written out because a shared rubric is
what makes an agreement figure mean "these chips are readable consistently"
rather than "these two people happened to be trained the same way" — and
because it has to be disclosed either way.

**Disclosure:** the second reader was given this document before reading. A
kappa measured with a shared rubric answers *can two readers applying the
same criteria agree*, which is the useful question. It is not a measure of
whether two people would independently invent the same criteria, and should
not be reported as one.

---

## The one question

**Is there a coherent, compact bright thing at the crosshair, separable from
the speckle around it?**

The red ring is 300 m across and is the field of judgement. Something bright
elsewhere in the 3 km chip is a different object and is not your answer.

## 1 — vessel

- A bright **head**: a core clearly brighter than anything else in the ring.
- **Shape**: a lozenge, a line with a bright end, a cluster with structure.
  You can draw a boundary around it.
- Often a **dark halo** — the sea immediately around a hull is calmed, so a
  ship frequently sits in a small dark patch. A strong cue.
- **Cross-shaped spikes** on a very bright target are range and azimuth
  sidelobes. They mean a strong point scatterer, which means a ship.
- **A vertical streak that tapers from a bright core is still a vessel.**
  That is azimuth smearing from the target's own motion, and on a fast or
  heavily-loaded ship it can run several hundred metres. Look for the taper:
  a smear has a bright middle and fades at both ends.
- Sanity-check against the panel. 58 px and 76 m should look like a boat, not
  like three loose pixels.

## 2 — fixed structure

Everything in (1), **plus something in the picture** that says it was built:
straight edges, a rectilinear outline, a regular repeating pattern of returns
(a wind farm, a pier, a bridge span, a platform).

**If the chip alone does not show it, do not use this.** A single image
usually cannot distinguish a moored ship from a structure. Call it `vessel`
and let the persistence filter — which sees the same coordinate across twelve
passes — make that call with evidence you do not have on screen.

The first reader's `fixed` verdicts were assigned exactly that way, from
cross-date persistence rather than from the image. The agreement analysis
therefore reports a **collapsed three-way kappa as primary**, with `fixed`
merged into `vessel`, so that both readers are scored on the question both can
answer: is a real target present. This was declared in the session's
pre-registration before the second read began, not chosen afterwards.

## 3 — clutter

Nothing coherent in the ring.

- **Speckle only.** The bright pixels are the same scale and brightness as
  the general texture. Salt-and-pepper everywhere, nothing at the centre.
- **A diffuse brightening with no head** — a wind streak or a patch of
  roughened sea. Bright but edgeless.
- **A long linear feature crossing the chip with no bright origin** — swell,
  a front, an internal wave train.
- **A dark band** is a slick or a wind shadow. Dark is not a target.

## 4 — ambiguous

Something is there and you cannot separate it. Use it.

- A small brightening barely above the neighbouring speckle peaks.
- A **soft-edged blob** with no crisp boundary — brighter than the background
  but shapeless.
- Sitting **at the edge of a slick or a front**, where the contrast step makes
  anything look bright.
- The crosshair on a **streak belonging to a much brighter neighbour** — a
  sidelobe or an azimuth ambiguity of a ship 100 m away, rather than a second
  object.
- The chip clipped at the scene edge with almost no context around the mark.

A reader who never says `ambiguous` is a reader whose hard cases silently
became whichever label was easiest, and the rate that comes out is a rate of
easy cases.

---

## Two traps worth knowing in advance

**Off-centre is not disqualifying.** A moving vessel is displaced along-track
by 400–470 m in this geometry. A bright target a little to one side of the
crosshair is normally the same object, not a different one.

**A bright neighbour casts streaks.** If there is a saturated target nearby
and a fainter linear feature at your crosshair, the faint one may be its
sidelobe. Compare brightness: a ship and its own sidelobe are wildly unequal;
two real vessels are comparable.

---

## Notes

Optional, and most valuable on the ones you found hard. If you hesitate,
write one.

**Describe what you saw, not what you decided.** The verdict is already
recorded; a note that restates it adds nothing.

| | |
|---|---|
| good | `compact bright head, dark halo` |
| good | `diffuse, no head — wind streak` |
| good | `soft blob at the edge of a slick` |
| good | `on the smear column of a brighter ship ~100 m N` |
| good | `two returns ~80 m apart, weaker one at the mark` |
| poor | `looks like a boat` |
| poor | `probably clutter` |
| poor | `not sure` |

The reason is adjudication. When two readers disagree, the notes are what let
a third person tell whether they **saw different things** or **called the same
thing differently**. Those are different failures with different remedies, and
without notes they are indistinguishable.

**Mechanics:** type the note, press **Enter** to hand the keyboard back, then
`1`–`4` to submit. `u` undoes the last verdict. The box clears itself between
chips.
