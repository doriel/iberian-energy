# Roadmap

What is built, what is in progress, and what comes next.

Status keys: **done**, **partial**, **not started**.

## Data sources

The platform combines structured market data with an unstructured evidence
layer, and the sources deliberately differ in shape (XML, JSON, delimited
files, free text), not just in hostname.

| Source | Shape | Status | Notes |
|---|---|---|---|
| ENTSO-E Transparency | XML API | **done** | A44 prices, A09 schedules, A61 capacity, A78 transmission outages, A80 generation outages. |
| Open-Meteo | JSON API | **partial** | Client and parser written and unit tested. Not yet validated against the live API at scale. Explains *why* Spanish power was cheap: solar radiation, wind, temperature. |
| OMIE | Delimited files | **partial** | Client and parser written. Column order (which is PT, which is ES) is settled against ENTSO-E on a decoupled day, not assumed. |
| REE / ESIOS | JSON API | **not started** | Spanish generation mix. Requires an API token. |
| REN Datahub | API / files | **not started** | Portuguese generation mix. Open access. |
| REN / ERSE announcements | Unstructured text | **not started** | The narrative evidence layer. A78 notices partly cover this. |

## Platform and architecture

| Component | Status | Notes |
|---|---|---|
| Medallion bronze / silver / gold | **partial** | Logic exists and is tested, running locally as parquet rather than Delta tables. Raw payloads land byte for byte, which is the bronze contract. |
| S3 raw landing zone | **not started** | `data/raw/` is the local stand in and mirrors the intended partitioning. |
| Lakeflow declarative pipelines | **not started** | The ingestion and analysis modules import no Databricks, so they run inside a pipeline unchanged. |
| Delta, Unity Catalog | **not started** | |
| Lakebase, gold sync, CDF back to Delta | **not started** | |
| Databricks Vector Search | **not started** | For the unstructured notices. |
| Mosaic AI Agent Framework | **not started** | `scripts/explain_interval.py` is the fact assembly the agent will phrase. |
| MLflow | **not started** | |
| Asset Bundles, CI/CD | **not started** | |
| Web app on Render | **not started** | |

## The analytical core

| Capability | Status |
|---|---|
| Market splitting detection | **done**, with episode grouping and correct quarter hourly arithmetic |
| Interconnection saturation as the mechanism | **done**, verified interval by interval |
| Attribution to named transmission assets | **done**, with the unexplained remainder reported explicitly |
| Point in time correctness | **done**, enforced server side via `periodStartUpdate` and client side in `binding_assets` |
| Agent receives retrieved facts only | **by design**, the assembly is in Python and produces no numbers of its own |
| Numeric hallucination checked programmatically | **not started** |
| North star metric measured | **not started**, needs the labelled evaluation set |
| ~100 hand labelled outage notices | **not started** |
| Historical price spikes with known causes | **not started** |

## Three personas, three gold tables

Every gold table must serve one of these users. A table nobody needs can be
cut; a user with no table is a gap in the product.

1. **Manufacturer deciding when to run equipment.** Needs the daily capacity
   profile and the premium by interval. The border follows a predictable
   evening trough, which is directly actionable for this user.
2. **Journalist or regulator watcher needing a defensible number with a cause.**
   Needs the episode table plus the attribution, including the gap that the
   notices do not explain.
3. **Grid analyst tracking forecast error and interconnection saturation.**
   Needs utilisation over time and the A78 curves.

## Next steps

The parts that required judgement (detection, mechanism, attribution and point
in time correctness) are built and tested. What remains is mostly platform
work, in priority order:

1. Medallion pipeline in Databricks with ENTSO-E, Open-Meteo and OMIE.
2. Gold tables promoted to Delta for the first two personas.
3. The agent, fed by the fact assembly that already exists.
4. A small labelled evaluation set, with the point in time filter on.
5. ESIOS and REN Datahub for the generation mix on both sides of the border.
6. The web app that serves the agent and the gold tables.
