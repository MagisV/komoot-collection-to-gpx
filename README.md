# komoot-gpx-exporter

`komoot-gpx-exporter` is a small single-file Python script that exports the tours inside a public Komoot collection as GPX files.

It is useful if you want to:

- archive a Komoot collection locally;
- get one GPX per stage/tour for use in other apps or devices;
- generate one merged GPX per collection;
- export a CSV manifest with the stage order, tour IDs, distances, and output filenames.

The tool reads Komoot's public collection, compilation, and coordinates endpoints and reconstructs GPX tracks from those responses. It is not affiliated with Komoot.

## Status

This works against Komoot's public responses as of **April 20, 2026**.

This repository is **not being maintained**. If Komoot changes its public site or API behavior, the exporter may stop working and no fixes are planned.

## Setup

Python 3.10+ is required. There are no third-party dependencies.

Clone the repository and run the script directly:

```bash
python3 fetch_komoot_collections.py <collection-url>
```

If you want to run it as an executable script on Unix-like systems:

```bash
chmod +x fetch_komoot_collections.py
./fetch_komoot_collections.py <collection-url>
```

## Usage

Pass one or more Komoot collection URLs or numeric collection IDs:

```bash
python3 fetch_komoot_collections.py <collection-url>
```

```bash
python3 fetch_komoot_collections.py <collection-id>
```

```bash
python3 fetch_komoot_collections.py <collection-id-1> <collection-id-2> --output-dir ./export
```

```bash
python3 fetch_komoot_collections.py <collection-url> --max-workers 4
```

If you marked it as executable:

```bash
./fetch_komoot_collections.py <collection-url>
```

## Example Outputs

For each run, the exporter writes:

- `manifest.csv`
- `README.md`
- `merged/<collection-slug>.gpx`
- `combined.gpx` when you pass more than one collection
- `stages/<collection-slug>/<nn>-<tour-name>-<tour-id>.gpx`

The generated `README.md` in the output directory summarizes the exported collections and output files.

## What It Actually Does

For each supplied collection, the tool:

1. fetches collection metadata;
2. fetches the ordered list of items in the collection;
3. filters that list down to tour items;
4. fetches the public coordinate stream for each tour;
5. writes one GPX file per tour;
6. builds merged GPX files and a CSV manifest.

## Caveats

- This depends on undocumented public Komoot endpoints.
- Direct `.gpx` download links may return `403`; this tool reconstructs GPX tracks from public coordinate data instead.
- Output quality depends on the completeness of the public coordinate stream for each tour.
- Very large collections may take time because every tour requires a separate coordinates request.
