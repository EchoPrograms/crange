# Local CRANGE documentation

The handbook uses Docusaurus with local search. It does not require a hosted
service or account. Navigation, images and the search index are served locally.

From the repository root, with Node.js 20+ and Python 3:

```sh
npm --prefix docs/website ci
npm --prefix docs/website start
```

Open http://127.0.0.1:8769. Edits to `docs/handbook/` reload in the browser.
For the complete static build, including local search:

```sh
npm --prefix docs/website run build
npm --prefix docs/website run serve
```

These preview commands bind only to loopback. Stop them with Ctrl+C. The entire
`docs/website/build/` directory can also be copied to an existing private static
web server. Package installation initially needs Internet/package access;
reading the completed static site needs no external service.

The installed controller service serves the static build on
`http://10.60.0.20:8769/`. Its systemd unit is
`ops/controller/crange-docs.service`. The intended proxy hostname is
`https://docs.range.brogano.dev/`; Control and Access link there. The network
must permit the proxy or reader to reach the controller. Port 8769 uses HTTP;
TLS, DNS and any Cloudflare Tunnel are configured separately. See
[Controller network service](handbook/reference/maintaining.md#controller-network-service)
for installation and update details.

The documentation server is independent of Control and never starts a worker,
reads credentials, submits jobs or restarts infrastructure. Generated CLI,
operation and route references parse source syntax trees without importing
controller modules. See **Reference → Maintaining** for screenshot reproduction.

Older guides and incident records in `docs/` remain supporting/historical
material; the handbook is the entry point for current use.
