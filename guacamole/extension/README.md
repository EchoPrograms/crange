# CRANGE Guacamole theme extension

This directory is a Guacamole extension source artifact. Build a JAR whose
root contains `guac-manifest.json` and `theme.css`, then install it in the
Guacamole `GUACAMOLE_HOME/extensions` directory. The manifest is pinned to
Guacamole 1.6.0, matching the live range deployment. Update the version field
when the Guacamole host is upgraded.

The extension only changes presentation. CRANGE access provisioning remains in
the controller worker and uses the configured Guacamole authentication
provider. Installing the extension requires a planned servlet-container
restart because Guacamole loads extensions at startup.
