# Run and maintain this documentation

This is a local Docusaurus site, separate from Control and its workers. It has no analytics, remote fonts or hosted search dependency. It does not read controller secrets or make infrastructure calls.

## Local preview

With Node.js 20+ and Python 3, from the repository root:

```sh
npm --prefix docs/website ci
npm --prefix docs/website start
```

Open **http://127.0.0.1:8769**. The development server reloads edited pages. Initial package installation needs package/Internet access. Local search is built for the production static site; use the build/serve commands to test it.

```sh
npm --prefix docs/website run build
npm --prefix docs/website run serve
```

Both servers bind to loopback. Stop with Ctrl+C. The generated `docs/website/build/` folder includes the complete static site and search index. Copy it to another machine/private static server if needed; no Docusaurus account, Read the Docs hosting or lab controller is required. Serve the build folder, not the repository root.

## Edit content and references

Pages live in `docs/handbook/`, and `docs/website/sidebars.js` defines their navigation. Use normal Markdown. The config deliberately uses Markdown format so shell/JSON examples are not parsed as JSX. Keep new internal links relative to source pages and place screenshots in `docs/website/static/img/screenshots/`.

`docs/generate-reference.py` parses source syntax trees before start/build. It generates CLI declarations, typed operation keys and route inventory without importing controller modules. Generated files are ignored by Git. The installed controller's `COMMAND --help` remains the authority for its revision's exact expanded defaults/options.

The build rejects broken internal links. Check the changed content against current source, particularly workflows that changed desired-only behavior into complete jobs. Distinguish implemented behavior from intended architecture and live acceptance from synthetic illustrations.

## Reproduce screenshots safely

Run the existing localhost synthetic fixture in a separate terminal:

```sh
python3 tests/fixtures/user_dashboard.py 8768
```

Use the Playwright CLI to open `http://127.0.0.1:8768/`, then run its `run-code --filename tests/fixtures/handbook_screenshots.js` command in that same browser session. Use your installed browser/CLI configuration. The script refuses another origin, intercepts synthetic GET responses and rejects job submissions. It captures the real checked-out GUI; no live controller or guest is used.

Captures go to `output/playwright/handbook-*.png`. Review them visually, then copy the selected files into the static screenshot folder without the `handbook-` prefix. Keep the screenshot descriptions honest about synthetic data and never capture credentials. The fixture may be stopped after capture; the handbook continues to work.

## Validate a change

Run the static build, browse the getting-started path on desktop and mobile, open local search and test a distinctive query such as `cleanup` or `gateway`, and confirm no external network requests are required. Check screenshot readability and that referenced actions match current UI labels.

Commit source, configuration, dependency lock and safe published screenshots. Do not commit `node_modules`, `.docusaurus`, static build output, generated references, browser artifacts or live lab state. Source updates do not publish to any external hosting service.
