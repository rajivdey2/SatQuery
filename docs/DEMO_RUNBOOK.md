# Demo runbook — presenting SatQuery AI

Everything here works with **no GPU, no downloaded weights and no internet**.
Read §1 the night before, run §2 once, and keep §4 open while you present.

---

## 1. The one thing to lead with

> *"Only the observable execution trace is evaluated. Internal reasoning text is
> neither required nor evaluated."* — problem statement 26167

So the demo is not "look at the answer". It is **"look at the answer, then look at
how it was produced"**. Every query you run ends the same way: open the audit trace
panel and show the routed task, the registry entry chosen, the permitted parameters,
the input checks, and the tool sequence with timings. That panel is the deliverable.

The second thing to lead with: final scoring happens on **ISRO/SAC's own unseen
Cartosat-2S + RISAT pairs**. So this system is built to *refuse* what it cannot
verify, and to say which measurements were unavailable. Two of the eight showcase
examples exist specifically to demonstrate that.

---

## 2. Setup (run once, the night before)

**One command does everything and tells you what failed:**

```bash
cd C:\project\satquery
venv\Scripts\activate
python scripts/verify_all.py
```

It generates the demo inputs, bakes the showcase examples, routes all five
problem-statement queries, exercises the HTTP surface, runs the test suite, and
prints a `PASS`/`FAIL` line per stage. Exit code 0 means you are ready.

`--demo-ready` is the fast subset (data, controller, examples) if you just want to
confirm the demo will open. `--quick` skips pytest and the API smoke.

The individual steps, if you want to run them one at a time:

```bash
python scripts/make_demo_data.py              # inputs with known ground truth
python scripts/build_examples.py              # bake the dashboard's examples
python scripts/smoke_controller.py            # five queries + edge cases, with routing
python scripts/test_api.py                    # HTTP surface, in-process
python -m pytest tests -q                     # full test suite
python scripts/make_architecture_diagram.py   # docs/architecture.png for slides
```

Expected from `smoke_controller.py`: one row per case, `OK` in the last column, and
`REJECT mismatched footprints` routing to `rejected`. `build_examples.py` prints the
eight baked examples with their routed task, confidence and timing.

### Start it

Two terminals:

```bash
# terminal 1 — backend
cd C:\project\satquery
venv\Scripts\activate
uvicorn backend.api.main:app --port 8000

# terminal 2 — frontend
cd C:\project\satquery\frontend
npm install        # first time only
npm run dev        # → http://localhost:5173
```

Open <http://localhost:5173>. The header should read
`analysis v0.2.0 · cpu`. Eight showcase cards should be visible immediately.

> **Do not set `SATQUERY_MOCK=1` for the demo.** The default path runs the real
> measurement engine, so the numbers on screen are genuinely measured. Mock mode
> exists only for a UI-only walkthrough on a machine where you do not want any
> analysis to run; it labels every answer as mock in the trace, which is the last
> thing you want a judge to read.

---

## 3. Room-proof fallbacks

| If this breaks | Do this |
|---|---|
| `npm run dev` fails / node missing | `npm run build` was already committed to `frontend/dist` — serve it, or present from the pre-baked examples via the API directly |
| Backend won't start | `python scripts/smoke_controller.py --verbose` shows the same answers in the terminal, including the routing table |
| A live run is slow on the projector laptop | Use the **stored examples** — they render instantly and are the same recorded output. Say so out loud; it is not a cheat, it is a cached run |
| Someone asks for a real satellite image | Any GeoTIFF works. If it has no CRS or no NIR band, the system will *say so* — that is a feature, show it |
| Rendered evidence missing (moved runtime dir) | `python scripts/build_examples.py` again |

---

## 4. The 7-minute script

Times are cumulative. Each step ends by opening the audit trace — that is the beat
that scores.

### 0:00 — Frame the problem (45 s)

"Operational remote-sensing questions often cannot be answered from one optical
image. The answer may need a second date, or a radar image that sees through cloud.
Existing tools make the user pick the model. SatQuery AI takes a plain-language
question, decides which specialist to run, checks the inputs are actually usable,
and returns evidence plus an auditable record of what it did."

Point at the header: `analysis v0.2.0 · cpu`. "No GPU. Everything you're about to
see is measured on this laptop."

### 0:45 — Example 1: scene description (45 s)

Click card **1 — Describe the land cover and major objects**.

- Read the answer aloud. Stop on a number: *"vegetation 52.4% (7,731 ha)"*.
- Scroll to **Measurements**. "That percentage is a pixel count. The hectares come
  from the GeoTIFF's pixel size. The row next to it says how it was decided —
  `NDVI > +0.250`."
- Open **Audit trace → Routing**. "Task `single_caption`, registry entry
  `rs_vlm.caption`, and the tasks the input configuration ruled out."

### 1:30 — Example 2: grounding (60 s)

Click card **2 — Highlight the water body**.

- "This is the second single-image task. We chose grounding over captioning because
  it returns *visual evidence*."
- Show the box on the image. Then the **Measurements → region** row:
  `box [4.2, 8.9, 22.1, 31.7]`. "The same numbers. The overlay is not a separate
  drawing — it is the trace rendered."
- Trace → **Tool sequence**: `analysis.land_cover → analysis.grounding →
  render.grounding_overlay`. "Three tools, each timed."

### 2:30 — Example 3: change description (75 s)

Click card **3 — What changed, and where**.

- The before / after / change triptych. "Yellow outlines are the change clusters."
- **Measurements → per-class table**: built-up `increased`, with a delta in
  percentage points *and* hectares.
- Point at the line under it: *"change threshold X against an estimated noise floor
  of Y"*. "Change detection always produces a non-zero number. We estimate the noise
  floor from the imagery and only call something a change when it clears it — and
  when it clears the classifier's own flip rate on ground that didn't change."

### 3:45 — Example 4: optical + SAR fusion (90 s) ← **the differentiator**

Click card **4 — Use optical and SAR together**.

- "This is the hardest of the three mandatory regimes and where we put the most
  work."
- **Measurements → agreement table**: per class, the optical fraction, the SAR
  fraction, IoU, and **Cohen's kappa**. "Two physically independent sensors,
  compared rather than averaged."
- **Complementarity** line: *"SAR detected water over N% of the scene where the
  optical image is obscured."* "The optical image has cloud. The radar sees through
  it. That sentence is the reason the problem statement asks for cross-modal pairs."
- Note the vegetation row: `n/a`. "This is a single-polarisation product. Vegetation
  needs a volume-scattering discriminator we don't have here, so we don't claim it."
- Trace → **Confidence**: source `cross_modal_agreement`, and the components table.
  "Our confidence for this task *is* the sensor agreement. And `calibrated: no` —
  we're explicit that this is a composite of measured signals, not a fitted
  probability."

### 5:15 — Example 5: change VQA (30 s)

Click card **5 — Has the built-up area increased?**

"Same pair, different question, different registry entry — `change.vqa` instead of
`change.description`. A direct verdict, with the number behind it."

### 5:45 — The two examples most demos hide (75 s) ← **do not skip**

Click **A pair the controller refuses**.

- "Two scenes that don't cover the same area. It measures the overlap, refuses, and
  explains. Final scoring is on ISRO's own unseen pairs — a system that says *I
  can't do this reliably* scores better than one that silently fuses misaligned
  imagery."
- Trace → **Checks**: the co-registration row with the measured overlap.

Click **A benchmark PNG with no near-infrared**.

- "An 8-bit PNG. NDVI and NDWI are reported *unavailable*, not approximated. The
  answer carries the caveat and the confidence is penalised for it."

### 7:00 — Close on orchestration + downloads (45 s)

Click **Fusion plus localisation**.

- Trace → the plan is **two** entries: `fusion.joint → rs_vlm.grounding`. "The query
  asked for something the fusion entry doesn't produce, so the router added a second
  entry. That is the agentic orchestration the problem statement asks for — from a
  predefined registry, not invented at runtime."

Open the **Predefined model/tool registry** panel. "Six entries, each with the input
configuration it requires and a typed parameter schema. Anything a caller passes
outside that schema is rejected and named in the trace."

Finish: **Download report (PDF)** and **Audit trace (JSON)**. "Both deliverables the
problem statement lists. The PDF carries the answer, the checks, the tool sequence,
the measurement tables, the evidence images, and the trace verbatim."

---

## 5. Questions you will get

**"Is this a fine-tuned VLM?"**
Two components are adapted, and neither is required for correctness. A dual-branch
BigEarthNet-MM head (Sentinel-1 + Sentinel-2, 19 CORINE classes) provides learned
scene labels — that is the mandatory BigEarthNet adaptation. Optional Qwen3-VL LoRA
adapters improve the *wording*. The measurements are model-free by design, so the
system degrades in fluency rather than in accuracy. See `docs/TRAINING.md`.

**"So the answers are just thresholds?"**
They are physically-motivated indices with data-driven thresholds, and we record
the separability of every split so you can see when a threshold was well-posed.
Where the histogram is unimodal we fall back to the published physical threshold and
say so, rather than inventing a split. That is more auditable than a black box, and
it is why every number in an answer can be traced.

**"How do I know the confidence means anything?"**
Open the components table. Each signal is named with its weight and what it
measures. `calibrated` is always `no`, and the note says it is not a probability.
Where we have no measurable signal we report `not_available` instead of a number.

**"What happens on our Cartosat/RISAT data?"**
Band roles are resolved from product band descriptions first, then by sensor
convention, with a statistical tie-break for the ambiguous 4-band case — Cartosat-2S
MX order is handled explicitly. RISAT-style amplitude digital numbers carry an
unknown gain, so absolute dB thresholds don't apply; the system detects that and
switches to scene-relative thresholds, and says which it used. Complex I/Q products
are reduced to amplitude with a warning. If co-registration can't be verified, it
refuses.

**"What's not done?"**
The QLoRA narration adapters aren't trained yet, so answers are currently the
measured text — the trace says `narration_source: measurement` and no adapter. The
benchmark table is filled with the measurement-engine backend; the adapter column
comes after a GPU session. We'd rather show that honestly than claim a number we
haven't run.

---

## 6. Sixty-second version

If you get cut to one minute: card **4** (fusion — agreement table and the
complementarity sentence), then card **A pair the controller refuses**, then the
audit trace panel. Those three beats cover the differentiator, the honesty, and the
graded artefact.
