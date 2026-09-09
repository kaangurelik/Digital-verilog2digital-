package digitalagent;

import org.json.JSONException;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.PrintStream;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.util.Set;

/** Persistent JSON-RPC 2.0 transport: one UTF-8 JSON object per line. */
public final class Main {
    static final int MAX_LINE_BYTES = 8 * 1024 * 1024;
    static final int MAX_JSON_DEPTH = 128;
    private static final Set<String> REQUEST_FIELDS = Set.of("jsonrpc", "id", "method", "params");

    private Main() { }

    public static void main(String[] args) throws Exception {
        if (args.length == 1 && "--smoke-test".equals(args[0])) {
            CompatibilitySmoke.main(new String[0]);
            return;
        }
        if (args.length != 0 && !(args.length == 1 && "--server".equals(args[0]))) {
            throw new IllegalArgumentException("Usage: digital-adapter [--server | --smoke-test]");
        }
        // Digital and dependency diagnostics must never enter the protocol stream.
        PrintStream protocol = new PrintStream(System.out, true, StandardCharsets.UTF_8);
        System.setOut(System.err);
        DigitalAdapter adapter = new DigitalAdapter();
        serve(System.in, protocol, adapter::dispatch);
    }

    @FunctionalInterface
    interface Dispatcher {
        JSONObject dispatch(String method, JSONObject params) throws Exception;
    }

    static void serve(InputStream input, PrintStream output, Dispatcher dispatcher) throws IOException {
        BufferedInputStream buffered = new BufferedInputStream(input);
        ByteArrayOutputStream line = new ByteArrayOutputStream(4096);
        boolean oversized = false;
        int next;
        while ((next = buffered.read()) != -1) {
            if (next == '\n') {
                processLine(line.toByteArray(), oversized, output, dispatcher);
                line.reset();
                oversized = false;
            } else if (!oversized) {
                if (line.size() == MAX_LINE_BYTES) {
                    oversized = true;
                    // Drain through the next newline to resynchronize subsequent requests.
                    line.reset();
                } else {
                    line.write(next);
                }
            }
        }
        if (oversized || line.size() != 0) {
            processLine(line.toByteArray(), oversized, output, dispatcher);
        }
    }

    private static void processLine(byte[] bytes, boolean oversized, PrintStream output, Dispatcher dispatcher) {
        if (oversized) {
            respond(output, error(JSONObject.NULL, -32600, "request_too_large",
                    "Request exceeds the 8 MiB line limit"));
            return;
        }
        Object parsed;
        try {
            String text = StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                    .decode(ByteBuffer.wrap(bytes)).toString();
            // org.json accepts JavaScript-like extensions. Validate RFC 8259 syntax first.
            new JsonSyntax(text).validate();
            parsed = new JSONTokener(text).nextValue();
        } catch (CharacterCodingException ex) {
            respond(output, error(JSONObject.NULL, -32700, "invalid_utf8", "Request must be valid UTF-8"));
            return;
        } catch (RpcException ex) {
            respond(output, error(JSONObject.NULL, ex.rpcCode, ex.code, ex.getMessage()));
            return;
        } catch (JSONException ex) {
            respond(output, error(JSONObject.NULL, -32700, "invalid_json", "Request must be valid JSON"));
            return;
        }
        if (!(parsed instanceof JSONObject request)) {
            respond(output, error(JSONObject.NULL, -32600, "invalid_request", "Expected one JSON-RPC request object"));
            return;
        }
        Object id = request.opt("id");
        if (!REQUEST_FIELDS.containsAll(request.keySet())
                || !"2.0".equals(request.opt("jsonrpc")) || !(request.opt("method") instanceof String)
                || (request.has("id") && id != JSONObject.NULL && !(id instanceof String) && !(id instanceof Number))) {
            respond(output, error(JSONObject.NULL, -32600, "invalid_request",
                    "Expected only jsonrpc 2.0, a string method, optional object params, and an optional string, number, or null id"));
            return;
        }
        boolean notification = !request.has("id");
        JSONObject response;
        try {
            if (request.has("params") && !(request.opt("params") instanceof JSONObject)) {
                throw RpcException.invalidParams("invalid_params", "params must be an object");
            }
            JSONObject params = request.has("params") ? request.getJSONObject("params") : new JSONObject();
            JSONObject result = dispatcher.dispatch(request.getString("method"), params);
            response = new JSONObject().put("jsonrpc", "2.0").put("id", id == null ? JSONObject.NULL : id)
                    .put("result", result == null ? JSONObject.NULL : result);
        } catch (RpcException ex) {
            response = error(id, ex.rpcCode, ex.code, ex.getMessage());
        } catch (Exception ex) {
            ex.printStackTrace(System.err);
            response = error(id, -32603, "internal_error", "Adapter operation failed; see stderr for details");
        }
        if (!notification) {
            respond(output, response);
        }
    }

    private static JSONObject error(Object id, int rpcCode, String code, String message) {
        return new JSONObject().put("jsonrpc", "2.0").put("id", id == null ? JSONObject.NULL : id)
                .put("error", new JSONObject().put("code", rpcCode).put("message", message)
                        .put("data", new JSONObject().put("code", code)));
    }

    private static void respond(PrintStream output, JSONObject response) {
        output.println(response);
        output.flush();
    }

    /** A bounded pre-validation pass, before org.json allocates the parsed object tree. */
    private static final class JsonSyntax {
        private final String text;
        private int offset;

        JsonSyntax(String text) {
            this.text = text;
        }

        void validate() throws RpcException {
            whitespace();
            value(0);
            whitespace();
            if (offset != text.length()) {
                invalid();
            }
        }

        private void value(int depth) throws RpcException {
            whitespace();
            if (offset == text.length()) {
                invalid();
            }
            char first = text.charAt(offset);
            if (first == '{' || first == '[') {
                if (depth >= MAX_JSON_DEPTH) {
                    throw new RpcException(-32700, "json_depth_exceeded", "JSON nesting exceeds 128 levels");
                }
                boolean object = first == '{';
                char end = object ? '}' : ']';
                offset++;
                whitespace();
                if (consume(end)) {
                    return;
                }
                while (true) {
                    if (object) {
                        string();
                        whitespace();
                        require(':');
                    }
                    value(depth + 1);
                    whitespace();
                    if (consume(end)) {
                        return;
                    }
                    require(',');
                    whitespace();
                }
            } else if (first == '"') {
                string();
            } else if (first == 't') {
                literal("true");
            } else if (first == 'f') {
                literal("false");
            } else if (first == 'n') {
                literal("null");
            } else {
                number();
            }
        }

        private void string() throws RpcException {
            require('"');
            while (offset < text.length()) {
                char next = text.charAt(offset++);
                if (next == '"') {
                    return;
                }
                if (next < 0x20) {
                    invalid();
                }
                if (next == '\\') {
                    if (offset == text.length()) {
                        invalid();
                    }
                    char escape = text.charAt(offset++);
                    if (escape == 'u') {
                        for (int i = 0; i < 4; i++) {
                            if (offset == text.length()) {
                                invalid();
                            }
                            char hex = text.charAt(offset++);
                            if (!((hex >= '0' && hex <= '9') || (hex >= 'a' && hex <= 'f') || (hex >= 'A' && hex <= 'F'))) {
                                invalid();
                            }
                        }
                    } else if ("\"\\/bfnrt".indexOf(escape) < 0) {
                        invalid();
                    }
                }
            }
            invalid();
        }

        private void number() throws RpcException {
            consume('-');
            if (!consume('0')) {
                if (offset == text.length() || text.charAt(offset) < '1' || text.charAt(offset) > '9') {
                    invalid();
                }
                digits();
            }
            if (consume('.')) {
                requireDigits();
            }
            if (consume('e') || consume('E')) {
                if (!consume('+')) {
                    consume('-');
                }
                requireDigits();
            }
        }

        private void requireDigits() throws RpcException {
            int start = offset;
            digits();
            if (offset == start) {
                invalid();
            }
        }

        private void digits() {
            while (offset < text.length() && text.charAt(offset) >= '0' && text.charAt(offset) <= '9') {
                offset++;
            }
        }

        private void literal(String literal) throws RpcException {
            if (!text.startsWith(literal, offset)) {
                invalid();
            }
            offset += literal.length();
        }

        private void whitespace() {
            while (offset < text.length() && " \t\r\n".indexOf(text.charAt(offset)) >= 0) {
                offset++;
            }
        }

        private boolean consume(char expected) {
            if (offset < text.length() && text.charAt(offset) == expected) {
                offset++;
                return true;
            }
            return false;
        }

        private void require(char expected) throws RpcException {
            if (!consume(expected)) {
                invalid();
            }
        }

        private void invalid() throws RpcException {
            throw new RpcException(-32700, "invalid_json", "Request must be valid JSON");
        }
    }
}
