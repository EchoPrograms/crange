package org.crange.guac;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import javax.ws.rs.*;
import javax.ws.rs.core.MediaType;
import javax.ws.rs.core.Response;
import org.apache.guacamole.*;
import org.apache.guacamole.environment.LocalEnvironment;
import org.apache.guacamole.io.GuacamoleReader;
import org.apache.guacamole.net.*;
import org.apache.guacamole.net.auth.*;
import org.apache.guacamole.protocol.*;

/** Holds the owner of a remote session independently of browser/login tunnels.
 * Browsers join that owner through guacd's normal connection-sharing protocol.
 * The registry adds no credential or login-token storage of its own.
 */
public final class CrangeSessionProvider extends AbstractAuthenticationProvider {
    static final Map<Key, Session> SESSIONS = new ConcurrentHashMap<>();
    private static final Object[] LOCKS = new Object[64];
    static { Arrays.setAll(LOCKS, i -> new Object()); }
    record Key(String source, String user, String connection) { }

    @Override public String getIdentifier() { return "crange-resume"; }
    @Override public Object getResource() { return new Resource(); }
    @Override public void shutdown() {
        for (Session session : List.copyOf(SESSIONS.values())) session.end();
    }

    @Override public UserContext decorate(UserContext context, AuthenticatedUser user,
            Credentials credentials) {
        final String source = context.getAuthenticationProvider().getIdentifier();
        if (source.endsWith("-shared")) return context;
        final String username = context.self().getIdentifier();
        return new DelegatingUserContext(context) {
            @Override public Directory<Connection> getConnectionDirectory() throws GuacamoleException {
                return new DelegatingDirectory<Connection>(super.getConnectionDirectory()) {
                    private Connection wrap(Connection connection) {
                        if (connection == null) return null;
                        return new DelegatingConnection(connection) {
                            @Override public GuacamoleTunnel connect(GuacamoleClientInformation info,
                                    Map<String, String> tokens) throws GuacamoleException {
                                return attach(new Key(source, username, connection.getIdentifier()),
                                        connection, info, tokens);
                            }
                            @Override @Deprecated public GuacamoleTunnel connect(GuacamoleClientInformation info)
                                    throws GuacamoleException { return connect(info, Collections.emptyMap()); }
                        };
                    }
                    @Override public Connection get(String id) throws GuacamoleException {
                        return wrap(super.get(id));
                    }
                    @Override public Collection<Connection> getAll(Collection<String> ids) throws GuacamoleException {
                        List<Connection> result = new ArrayList<>();
                        for (Connection connection : super.getAll(ids)) result.add(wrap(connection));
                        return result;
                    }
                };
            }
        };
    }

    static GuacamoleTunnel attach(Key key, Connection connection, GuacamoleClientInformation info,
            Map<String, String> tokens) throws GuacamoleException {
        synchronized (LOCKS[Math.floorMod(key.hashCode(), LOCKS.length)]) {
            Session session = SESSIONS.get(key);
            if (session != null && !session.isOpen()) { session.end(); session = null; }
            boolean created = session == null;
            if (created) {
                // The original provider authorizes the connection and enforces
                // its connection limits. The owner remains in native history.
                GuacamoleProxyConfiguration proxy = proxy(connection);
                GuacamoleTunnel owner = connection.connect(info, tokens);
                if (!(owner.getSocket() instanceof ConfiguredGuacamoleSocket configured))
                    return owner; // Extensions with custom sockets retain native behavior.
                session = new Session(key, connection.getName(), owner, configured.getConnectionID(), proxy);
                SESSIONS.put(key, session);
                session.start();
            }
            try { return session.join(info); }
            catch (GuacamoleException e) {
                // Never silently replace a session whose resume failed.
                // A failed initial join must not leave an inaccessible owner.
                if (created) session.end();
                throw e;
            }
        }
    }

    private static GuacamoleProxyConfiguration proxy(Connection connection) throws GuacamoleException {
        GuacamoleProxyConfiguration defaults = LocalEnvironment.getInstance().getDefaultGuacamoleProxyConfiguration();
        Map<String, String> attrs = connection.getAttributes();
        if (attrs == null) return defaults;
        String host = attrs.get("guacd-hostname"), port = attrs.get("guacd-port"), encryption = attrs.get("guacd-encryption");
        return new GuacamoleProxyConfiguration(host == null || host.isEmpty() ? defaults.getHostname() : host,
                port == null || port.isEmpty() ? defaults.getPort() : Integer.parseInt(port),
                encryption == null || encryption.isEmpty() ? defaults.getEncryptionMethod()
                    : "ssl".equals(encryption) ? GuacamoleProxyConfiguration.EncryptionMethod.SSL
                    : GuacamoleProxyConfiguration.EncryptionMethod.NONE);
    }

    private static void closeTunnel(GuacamoleTunnel tunnel) throws GuacamoleException {
        // Explicitly detach the guacd user before dropping its transport. A
        // plain TCP close can remain pending while another reader is active.
        try {
            tunnel.acquireWriter();
            try { tunnel.getSocket().getWriter().writeInstruction(new GuacamoleInstruction("disconnect")); }
            finally { tunnel.releaseWriter(); }
        }
        finally { tunnel.close(); }
    }

    static final class Session {
        final Key key;
        final String id = UUID.randomUUID().toString();
        final String name, connectionID;
        final GuacamoleTunnel owner;
        final GuacamoleProxyConfiguration proxy;
        final long started = System.currentTimeMillis();
        volatile long lastUsed = started;
        final AtomicBoolean ended = new AtomicBoolean();
        final Set<GuacamoleTunnel> views = ConcurrentHashMap.newKeySet();
        Session(Key key, String name, GuacamoleTunnel owner, String connectionID, GuacamoleProxyConfiguration proxy) {
            this.key = key; this.name = name; this.owner = owner; this.connectionID = connectionID; this.proxy = proxy;
        }
        boolean isOpen() { return !ended.get() && owner.isOpen(); }
        void start() {
            Thread keeper = new Thread(() -> {
                GuacamoleReader reader = owner.acquireReader();
                try {
                    GuacamoleInstruction instruction;
                    while (!ended.get() && (instruction = reader.readInstruction()) != null) {
                        List<String> args = instruction.getArgs();
                        switch (instruction.getOpcode()) {
                            case "sync":
                                owner.acquireWriter();
                                try { owner.getSocket().getWriter().writeInstruction(new GuacamoleInstruction("sync", args.get(0))); }
                                finally { owner.releaseWriter(); } break;
                            case "blob":
                                // Consume streaming output without holding up
                                // the remote session while its browser is away.
                                owner.acquireWriter();
                                try { owner.getSocket().getWriter().writeInstruction(new GuacamoleInstruction("ack", args.get(0), "OK", "0")); }
                                finally { owner.releaseWriter(); } break;
                            case "error": case "disconnect": return;
                            default: break;
                        }
                    }
                }
                catch (GuacamoleException ignored) { /* The remote session ended. */ }
                finally { owner.releaseReader(); end(); }
            }, "crange-session-owner");
            keeper.setDaemon(true); keeper.start();
        }
        synchronized GuacamoleTunnel join(GuacamoleClientInformation info) throws GuacamoleException {
            if (!isOpen()) throw new GuacamoleResourceNotFoundException("This remote session has ended.");
            GuacamoleConfiguration config = new GuacamoleConfiguration(
                    ((ConfiguredGuacamoleSocket) owner.getSocket()).getConfiguration());
            config.setConnectionID(connectionID);
            GuacamoleSocket raw = proxy.getEncryptionMethod() == GuacamoleProxyConfiguration.EncryptionMethod.SSL
                    ? new SSLGuacamoleSocket(proxy.getHostname(), proxy.getPort())
                    : new InetGuacamoleSocket(proxy.getHostname(), proxy.getPort());
            final GuacamoleTunnel joined;
            try { joined = new SimpleGuacamoleTunnel(new ConfiguredGuacamoleSocket(raw, config, info)); }
            catch (GuacamoleException e) { raw.close(); throw e; }
            GuacamoleTunnel view = new DelegatingGuacamoleTunnel(joined) {
                final AtomicBoolean closed = new AtomicBoolean();
                @Override public void close() throws GuacamoleException {
                    if (closed.compareAndSet(false, true)) {
                        views.remove(this); lastUsed = System.currentTimeMillis(); closeTunnel(joined);
                    }
                }
            };
            if (!isOpen()) { view.close(); throw new GuacamoleResourceNotFoundException("This remote session has ended."); }
            views.add(view); lastUsed = System.currentTimeMillis(); return view;
        }
        synchronized void end() {
            if (!ended.compareAndSet(false, true)) return;
            SESSIONS.remove(key, this);
            for (GuacamoleTunnel view : List.copyOf(views)) try { view.close(); } catch (GuacamoleException ignored) { }
            try { closeTunnel(owner); } catch (GuacamoleException ignored) { }
        }
        Map<String, Object> summary() {
            return Map.of("id", id, "connection", key.connection(), "name", name,
                    "source", key.source(), "protocol", owner.getSocket().getProtocol(),
                    "started", started, "last_used", lastUsed, "attached", !views.isEmpty());
        }
    }

    @Path("/") public static final class Resource {
        private static final ObjectMapper JSON = new ObjectMapper();
        private static final HttpClient HTTP = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();
        private static String encode(String value) { return URLEncoder.encode(value, StandardCharsets.UTF_8).replace("+", "%20"); }
        private static Response error(int status, String text) {
            return Response.status(status).type(MediaType.APPLICATION_JSON).entity(Map.of("error", text)).build();
        }
        private static HttpResponse<String> get(String path, String token) throws Exception {
            return HTTP.send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:8080/api/session/data/" + path))
                .timeout(Duration.ofSeconds(5)).header("Guacamole-Token", token).GET().build(), HttpResponse.BodyHandlers.ofString());
        }
        private static String identity(String source, String token) throws Exception {
            if (token == null || !token.matches("[A-Za-z0-9_-]{16,256}")
                    || source == null || !source.matches("[A-Za-z0-9_-]{1,64}")) return null;
            HttpResponse<String> result = get(encode(source) + "/self", token);
            if (result.statusCode() != 200) return null;
            JsonNode identifier = JSON.readTree(result.body()).get("username");
            return identifier != null && identifier.isTextual() ? identifier.asText() : null;
        }
        @GET @Path("sessions") @Produces(MediaType.APPLICATION_JSON)
        public Response list(@HeaderParam("Guacamole-Token") String token,
                @HeaderParam("X-Guacamole-Data-Source") String source) {
            try {
                String user = identity(source, token);
                if (user == null) return error(401, "Sign in to Guacamole first.");
                List<Map<String, Object>> rows = new ArrayList<>();
                for (Session session : SESSIONS.values()) {
                    if (!session.key.source().equals(source) || !session.key.user().equals(user)) continue;
                    if (!session.isOpen()) { session.end(); continue; }
                    // Revalidate current access. Never expose sessions merely
                    // because a browser remembers an old connection identifier.
                    HttpResponse<String> access = get(encode(source) + "/connections/" + encode(session.key.connection()), token);
                    if (access.statusCode() == 200) rows.add(session.summary());
                    else if (access.statusCode() == 403 || access.statusCode() == 404) session.end();
                    else return error(503, "Could not verify access to your remote sessions. Refresh to try again.");
                }
                rows.sort(Comparator.comparingLong(row -> -(Long) row.get("last_used")));
                return Response.ok(Map.of("sessions", rows), MediaType.APPLICATION_JSON).build();
            }
            catch (Exception e) { return error(503, "Could not load your remote sessions. Refresh to try again."); }
        }
        @DELETE @Path("sessions/{id}") @Produces(MediaType.APPLICATION_JSON)
        public Response end(@HeaderParam("Guacamole-Token") String token,
                @HeaderParam("X-Guacamole-Data-Source") String source, @PathParam("id") String id) {
            try {
                String user = identity(source, token);
                if (user == null) return error(401, "Sign in to Guacamole first.");
                for (Session session : SESSIONS.values()) {
                    if (session.id.equals(id) && session.key.source().equals(source) && session.key.user().equals(user)) {
                        session.end(); return Response.ok(Map.of("ended", true), MediaType.APPLICATION_JSON).build();
                    }
                }
                return error(404, "This remote session has ended.");
            }
            catch (Exception e) { return error(503, "Could not end the remote session. Try again."); }
        }
    }
}
