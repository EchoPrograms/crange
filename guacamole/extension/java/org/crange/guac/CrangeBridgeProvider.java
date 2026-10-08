package org.crange.guac;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.time.Duration;
import java.time.Instant;
import java.util.HexFormat;
import java.util.UUID;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import javax.ws.rs.Consumes;
import javax.ws.rs.HeaderParam;
import javax.ws.rs.POST;
import javax.ws.rs.Path;
import javax.ws.rs.Produces;
import javax.ws.rs.core.MediaType;
import javax.ws.rs.core.Response;
import org.apache.guacamole.net.auth.AbstractAuthenticationProvider;

/** Same-origin Guacamole endpoint; all decisions stay on the CRANGE controller. */
public final class CrangeBridgeProvider extends AbstractAuthenticationProvider {

    @Override
    public String getIdentifier() {
        return "crange-control";
    }

    @Override
    public Object getResource() {
        return new Resource();
    }

    @Path("/")
    public static final class Resource {

        private static final HttpClient HTTP = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(3)).build();

        private static Response error(int status, String message) {
            return Response.status(status).type(MediaType.APPLICATION_JSON)
                    .entity("{\"error\":\"" + message + "\"}").build();
        }

        private static byte[] key() throws IOException {
            String name = System.getenv().getOrDefault("CRANGE_BRIDGE_KEY_FILE",
                    "/etc/guacamole/crange-bridge.key");
            byte[] value = HexFormat.of().parseHex(Files.readString(java.nio.file.Path.of(name)).trim());
            if (value.length != 32)
                throw new IOException("Invalid bridge key length");
            return value;
        }

        private static String sign(byte[] key, String timestamp, String nonce, String body) throws Exception {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(key, "HmacSHA256"));
            byte[] signed = (timestamp + "\n" + nonce + "\n" + body).getBytes(StandardCharsets.UTF_8);
            return HexFormat.of().formatHex(mac.doFinal(signed));
        }

        @POST
        @Path("control")
        @Consumes(MediaType.APPLICATION_JSON)
        @Produces(MediaType.APPLICATION_JSON)
        public Response control(@HeaderParam("Guacamole-Token") String token,
                                @HeaderParam("X-Guacamole-Data-Source") String source,
                                String request) {
            if (token == null || !token.matches("[A-Za-z0-9_-]{16,256}")
                    || source == null || !source.matches("[A-Za-z0-9_-]{1,64}"))
                return error(401, "Sign in to Guacamole first.");
            if (request == null || request.length() > 16384)
                return error(400, "Invalid management request.");

            try {
                // Guacamole itself establishes who owns this token. The browser
                // cannot choose a username for the controller request.
                URI self = URI.create("http://127.0.0.1:8080/api/session/data/" + source + "/self");
                HttpRequest verify = HttpRequest.newBuilder(self).timeout(Duration.ofSeconds(5))
                        .header("Guacamole-Token", token).GET().build();
                HttpResponse<String> user = HTTP.send(verify, HttpResponse.BodyHandlers.ofString());
                if (user.statusCode() != 200 || !user.body().stripLeading().startsWith("{"))
                    return error(401, "Guacamole session expired.");

                String body = "{\"identity\":" + user.body() + ",\"dataSource\":\"" + source
                        + "\",\"request\":" + request + "}";
                String timestamp = Long.toString(Instant.now().getEpochSecond());
                String nonce = UUID.randomUUID().toString().replace("-", "");
                String controller = System.getenv().getOrDefault("CRANGE_CONTROLLER_BRIDGE_URL",
                        "http://10.60.0.20:8000/api/guac-bridge");
                HttpRequest forward = HttpRequest.newBuilder(URI.create(controller))
                        .timeout(Duration.ofSeconds(15))
                        .header("Content-Type", MediaType.APPLICATION_JSON)
                        .header("X-Crange-Timestamp", timestamp)
                        .header("X-Crange-Nonce", nonce)
                        .header("X-Crange-Signature", sign(key(), timestamp, nonce, body))
                        .POST(HttpRequest.BodyPublishers.ofString(body)).build();
                HttpResponse<String> result = HTTP.send(forward, HttpResponse.BodyHandlers.ofString());
                int status = result.statusCode();
                if (status < 200 || status > 499)
                    return error(502, "Controller management service is unavailable.");
                return Response.status(status).type(MediaType.APPLICATION_JSON)
                        .entity(result.body()).build();
            }
            catch (Exception exc) {
                return error(502, "Guacamole management bridge is unavailable.");
            }
        }
    }
}
