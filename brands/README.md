# Brand directories

Every brand lives in its own directory. Adding one only means creating a directory
in this layout — no code change is required.

```
brands/
  <brand-code>/            lowercase, no spaces (e.g. trilux, zumtobel)
    brand.json             {"name": "Display Name", "colour": "#003d6b"}
    data/                  BMEcat 2005 / ETIM XML file (if any)
    documents/
      Catalogue/           PDFs — the directory name becomes the category
      Certificate/
      ...
```

- **`data/` may be empty.** If the manufacturer publishes no BMEcat feed, only its
  documents become searchable and the product side stays empty.
- **`documents/` may be empty.** Product data alone is fine too.
- A directory whose name starts with `_` is ignored (used for templates).
- Without `brand.json` the directory name is used as the display name.

After adding files:

```bash
.venv/bin/python etl/build_db.py
.venv/bin/python etl/index_docs.py
.venv/bin/python etl/link_catalogs.py
```

Automatic downloading only exists for TRILUX (`etl/fetch_trilux.py`), because every
manufacturer portal is different. Other brands' files are copied in by hand.
