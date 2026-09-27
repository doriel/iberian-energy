# Diagrams

Every figure in the project README lives here as a PNG, next to the source it
was generated from. The PNG is what gets embedded; the source is what you edit.
Nothing here is hand drawn, so a diagram that disagrees with the code is a bug
you can fix by editing four lines rather than by opening a drawing tool.

| Figure | Source | What it shows |
| --- | --- | --- |
| `01-medallion.png` | `01-medallion.mmd` | The four publishers, the raw Volume on S3, and bronze to silver to gold ending at one table per persona |
| `02-loop.png` | `02-loop.mmd` | The forward path out to the application and the reverse path back into Delta through the Lakebase change data feed |
| `03-orchestration.png` | `03-orchestration.mmd` | The five tasks of `iberian-daily`, and the notebooks that are run by hand |
| `04-lakehouse-er.png` | `04-lakehouse-er.dot` | Every Delta table, raw to gold, in the layer it belongs to, with the key each hop joins on |
| `05-lakebase-er.png` | `05-lakebase-er.mmd` and `05-lakebase-er.dbml` | The four Lakebase application tables in Postgres, their constraints and the one foreign key |
| `06-market-splitting.png` | `06-market-splitting.dot` | What market splitting is, a coupled interval beside a split one, and the thresholds the code decides with |
| `07-agent-grounding.png` | `07-agent-grounding.dot` | How an explanation is produced and why a number in it can be trusted: the fact sheet, the model, the numeric check, and what is written down when it fails |

`04` and `05` are both entity relationship diagrams and they cover different
halves of the system. `04` is the analytical side: 21 Delta tables in Unity
Catalog, where the relationships are join keys rather than declared constraints,
because a lakehouse has no foreign keys to declare. `05` is the operational
side: four Postgres tables where the constraints are real and enforced at write
time. Embed `04` as a link rather than inline, or as a thumbnail that opens the
full size, because at README width the column names are too small to read.

The numbers in the two panels of `06` are illustrative and the figure says so.
The thresholds underneath are not: 0.01 EUR/MWh, the three severity bands and
0.98 come straight from `src/iberian/config.py` and
`src/iberian/analysis/interconnection.py`, so if either constant moves, the
figure is wrong.

## Editing, without installing anything

**Mermaid sources (`.mmd`)**, figures 01, 02, 03 and 05. Open
<https://mermaid.live>, paste the file in, edit on the left, watch the render on
the right. Free, no account. Export as PNG from the panel on the right and
overwrite the file here. Keep the `config:` block at the top of the file:
`wrappingWidth` is what stops long node labels wrapping into a diagram three
times taller than it needs to be.

**The Graphviz sources (`.dot`)**, figures 04, 06 and 07. Open <https://edotor.net> or
<https://dreampuf.github.io/GraphvizOnline>, paste the whole file in, edit, and
export PNG or SVG. Both are free with no account. This one is Graphviz rather
than Mermaid because Mermaid's ER layout has no concept of a layer: it placed
the four bronze tables on four different rows and ran the edges through the
middle of the silver boxes. Graphviz clusters give bronze, silver and gold a
column each, which is the whole point of the figure. `06` and `07` are Graphviz
for a simpler reason: they need side by side panels and a loop that goes
backwards, and Mermaid gives no useful control over either.

**The Lakebase ER as DBML (`.dbml`)**, also figure 05. Open
<https://dbdiagram.io>, paste it in, drag the boxes where you want them. Free to
edit without an account. It exports PNG and PDF, and it will also generate the
`CREATE TABLE` statements back out, which is a quick way to check the drawing
still matches `sql/001_application_tables.sql`. The `.mmd` and the `.dbml`
describe the same four tables, so if you change one, change the other.

## Regenerating locally

```bash
npm install -g @mermaid-js/mermaid-cli
sudo apt-get install -y graphviz

cd images_readme
for f in *.mmd; do
  mmdc -i "$f" -o "${f%.mmd}.png" -p p.json -s 3 -b white -t neutral -w 1600
done
dot -Tpng -Gdpi=110 04-lakehouse-er.dot   -o 04-lakehouse-er.png
dot -Tpng -Gdpi=120 06-market-splitting.dot -o 06-market-splitting.png
dot -Tpng -Gdpi=125 07-agent-grounding.dot  -o 07-agent-grounding.png
```

`p.json` passes `--no-sandbox` to the headless browser, which WSL and most
containers need. `-s 3` renders at three times the natural size so the text
survives a README at full width; the Mermaid PNGs here were then resized to
2400 px wide and every PNG quantised to 64 colours, which takes them from about
500 KB each to about 130 KB with no visible loss on flat colour diagrams.

## The source of truth

The diagrams are drawings of these files, not the other way round:

- `pipelines/transformations/iberian_medallion.py` for the bronze, silver and
  gold tables the declarative pipeline owns
- `pipelines/01b`, `01d`, `01e`, `01g` and `02` for the tables built outside it:
  `gold_transmission_notices`, `silver_generation_per_unit`,
  `gold_unit_hourly_output`, `silver_application_events`,
  `gold_application_activity` and `gold_episode_explanations`
- `resources/iberian_job.yml` for the Job DAG, the schedule and the retry counts
- `sql/001_application_tables.sql` for the Lakebase schema
- `src/iberian/config.py` and `src/iberian/analysis/` for the thresholds in `06`
- `src/iberian/agent/` for the flow in `07`: `batch.py` builds the fact sheet,
  `retrieval.py` finds the notices, `explain.py` runs the loop, `verify.py` is
  the check

If you change one of those, the matching figure needs a look.
