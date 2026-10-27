package org.crange.guac;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.ByteArrayOutputStream;
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
import org.apache.guacamole.io.GuacamoleWriter;
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

    /** A separate tracker for each direction prevents stream-index collisions.
     * Clipboard contents stay in RAM only and disappear when the session ends. */
    static final class ClipboardTracker {
        final Session session;
        final boolean writable;
        final Map<String, ByteArrayOutputStream> streams = new HashMap<>();
        final GuacamoleParser parser = new GuacamoleParser();
        ClipboardTracker(Session session, boolean writable) { this.session = session; this.writable = writable; }
        void accept(GuacamoleInstruction instruction) {
            List<String> args = instruction.getArgs();
            switch (instruction.getOpcode()) {
                case "clipboard":
                    if (args.size() >= 2) {
                        streams.remove(args.get(0));
                        if (args.get(1).startsWith("text/")) streams.put(args.get(0), new ByteArrayOutputStream());
                        else if (writable ? session.canPaste() : session.canCopy()) session.clipboardText = null;
                    }
                    break;
                case "blob":
                    if (args.size() >= 2 && streams.containsKey(args.get(0))) {
                        ByteArrayOutputStream value = streams.get(args.get(0));
                        try {
                            byte[] bytes = Base64.getDecoder().decode(args.get(1));
                            if (value.size() + bytes.length > 16 * 1024 * 1024) streams.remove(args.get(0));
                            else value.writeBytes(bytes);
                        }
                        catch (IllegalArgumentException ignored) { streams.remove(args.get(0)); }
                    }
                    break;
                case "end":
                    if (!args.isEmpty()) {
                        ByteArrayOutputStream value = streams.remove(args.get(0));
                        if (value != null && (writable ? session.canPaste() : session.canCopy()))
                            session.clipboardText = value.toString(StandardCharsets.UTF_8);
                    }
                    break;
                default: break;
            }
        }
        void accept(char[] data, int offset, int length) throws GuacamoleException {
            int end = offset + length;
            while (offset < end) {
                int count = parser.append(data, offset, end - offset);
                while (parser.hasNext()) accept(parser.next());
                if (count == 0) throw new GuacamoleServerException("Invalid clipboard protocol stream.");
                offset += count;
            }
        }
    }

    static final class Session {
        final Key key;
        final String id = UUID.randomUUID().toString();
        final String name, connectionID;
        final GuacamoleTunnel owner;
        final GuacamoleProxyConfiguration proxy;
        final long started = System.currentTimeMillis();
        volatile long lastUsed = started;
        volatile String clipboardText;
        final AtomicBoolean ended = new AtomicBoolean();
        final Set<GuacamoleTunnel> views = ConcurrentHashMap.newKeySet();
        Session(Key key, String name, GuacamoleTunnel owner, String connectionID, GuacamoleProxyConfiguration proxy) {
            this.key = key; this.name = name; this.owner = owner; this.connectionID = connectionID; this.proxy = proxy;
        }
        boolean isOpen() { return !ended.get() && owner.isOpen(); }
        boolean canCopy() { return !"true".equals(((ConfiguredGuacamoleSocket) owner.getSocket()).getConfiguration().getParameter("disable-copy")); }
        boolean canPaste() {
            GuacamoleConfiguration config = ((ConfiguredGuacamoleSocket) owner.getSocket()).getConfiguration();
            return !"true".equals(config.getParameter("disable-paste")) && !"true".equals(config.getParameter("read-only"));
        }
        void start() {
            Thread keeper = new Thread(() -> {
                GuacamoleReader reader = owner.acquireReader();
                ClipboardTracker clipboard = new ClipboardTracker(this, false);
                try {
                    GuacamoleInstruction instruction;
                    while (!ended.get() && (instruction = reader.readInstruction()) != null) {
                        clipboard.accept(instruction);
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
            try {
                GuacamoleSocket configured = new ConfiguredGuacamoleSocket(raw, config, info);
                ClipboardTracker incoming = new ClipboardTracker(this, false), outgoing = new ClipboardTracker(this, true);
                GuacamoleReader nativeReader = configured.getReader();
                GuacamoleWriter nativeWriter = configured.getWriter();
                GuacamoleSocket tracked = new DelegatingGuacamoleSocket(configured) {
                    final GuacamoleReader reader = new GuacamoleReader() {
                        @Override public boolean available() throws GuacamoleException { return nativeReader.available(); }
                        @Override public GuacamoleInstruction readInstruction() throws GuacamoleException {
                            GuacamoleInstruction instruction = nativeReader.readInstruction();
                            if (instruction != null) incoming.accept(instruction); return instruction;
                        }
                        @Override public char[] read() throws GuacamoleException {
                            char[] data = nativeReader.read();
                            // Native reads contain whole instructions; skip PNG,
                            // video and other streams when no clipboard is active.
                            if (data != null && (!incoming.streams.isEmpty() || new String(data).contains("9.clipboard,")))
                                incoming.accept(data, 0, data.length);
                            return data;
                        }
                    };
                    final GuacamoleWriter writer = new GuacamoleWriter() {
                        @Override public void writeInstruction(GuacamoleInstruction instruction) throws GuacamoleException {
                            nativeWriter.writeInstruction(instruction); outgoing.accept(instruction);
                        }
                        @Override public void write(char[] data) throws GuacamoleException { write(data, 0, data.length); }
                        @Override public void write(char[] data, int offset, int length) throws GuacamoleException {
                            nativeWriter.write(data, offset, length); outgoing.accept(data, offset, length);
                        }
                    };
                    @Override public GuacamoleReader getReader() { return reader; }
                    @Override public GuacamoleWriter getWriter() { return writer; }
                };
                joined = new SimpleGuacamoleTunnel(tracked);
            }
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
            clipboardText = null;
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
        @GET @Path("clipboard") @Produces(MediaType.APPLICATION_JSON)
        public Response clipboard(@HeaderParam("Guacamole-Token") String token,
                @HeaderParam("X-Guacamole-Data-Source") String source, @QueryParam("connection") String connection) {
            try {
                String user = identity(source, token);
                if (user == null) return error(401, "Sign in to Guacamole first.");
                if (connection == null || connection.length() > 1024) return error(400, "Choose a connection.");
                Session session = SESSIONS.get(new Key(source, user, connection));
                if (session == null || !session.isOpen()) return error(404, "This remote session has ended.");
                HttpResponse<String> access = get(encode(source) + "/connections/" + encode(connection), token);
                if (access.statusCode() != 200) return error(403, "This connection is unavailable.");
                if (!session.canCopy()) return error(403, "Clipboard access is disabled for this connection.");
                String text = session.clipboardText;
                return Response.ok(Map.of("known", text != null, "data", text == null ? "" : text,
                        "writable", session.canPaste()), MediaType.APPLICATION_JSON).build();
            }
            catch (Exception e) { return error(503, "Could not restore the connection clipboard."); }
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
