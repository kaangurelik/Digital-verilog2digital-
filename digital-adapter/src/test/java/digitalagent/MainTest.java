package digitalagent;

import org.json.JSONObject;
import org.junit.Test;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.Assert.*;

public class MainTest {
    private static final String REQUEST = "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"echo\"}";
    private static final Main.Dispatcher ECHO = (method, params) -> params;

    private List<JSONObject> exchange(String input, Main.Dispatcher dispatcher) throws Exception {
        return exchange(input.getBytes(StandardCharsets.UTF_8), dispatcher);
    }

    private List<JSONObject> exchange(byte[] input, Main.Dispatcher dispatcher) throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        Main.serve(new ByteArrayInputStream(input), new PrintStream(bytes, true, StandardCharsets.UTF_8), dispatcher);
        List<JSONObject> responses = new ArrayList<>();
        for (String line : bytes.toString(StandardCharsets.UTF_8).split("\\R")) {
            if (!line.isEmpty()) {
                responses.add(new JSONObject(line));
            }
        }
        return responses;
    }

    private void assertError(JSONObject response, int rpcCode, String code) {
        assertEquals("2.0", response.getString("jsonrpc"));
        assertFalse(response.has("result"));
        assertEquals(rpcCode, response.getJSONObject("error").getInt("code"));
        assertEquals(code, response.getJSONObject("error").getJSONObject("data").getString("code"));
    }

    @Test
    public void acceptsUtf8CrLfAndLastLineWithoutNewline() throws Exception {
        String first = "{\"jsonrpc\":\"2.0\",\"id\":\"çalışma🙂\",\"method\":\"echo\",\"params\":{\"label\":\"α\"}}";
        List<JSONObject> responses = exchange(first + "\r\n" + REQUEST, ECHO);
        assertEquals(2, responses.size());
        assertEquals("çalışma🙂", responses.get(0).getString("id"));
        assertEquals("α", responses.get(0).getJSONObject("result").getString("label"));
        assertEquals(0, responses.get(1).getJSONObject("result").length());
        assertTrue(exchange("", ECHO).isEmpty());
    }

    @Test
    public void notificationsAreDispatchedWithoutResponsesIncludingFailures() throws Exception {
        AtomicInteger calls = new AtomicInteger();
        String notification = "{\"jsonrpc\":\"2.0\",\"method\":\"notify\"}\n";
        List<JSONObject> responses = exchange(notification + notification + REQUEST, (method, params) -> {
            calls.incrementAndGet();
            if (method.equals("notify")) {
                throw new RpcException(-32601, "unknown_method", "Unknown method");
            }
            return params;
        });
        assertEquals(3, calls.get());
        assertEquals(1, responses.size());
        assertEquals(1, responses.get(0).getInt("id"));
    }

    @Test
    public void nullAndNumericIdsArePreserved() throws Exception {
        for (String id : List.of("null", "1.25", "9223372036854775808")) {
            String request = REQUEST.replace("\"id\":1", "\"id\":" + id);
            JSONObject response = exchange(request, ECHO).get(0);
            assertEquals(new JSONObject(request).get("id"), response.get("id"));
        }
    }

    @Test
    public void malformedEnvelopesHaveNullIdAndDoNotReachDispatcher() throws Exception {
        for (String invalid : List.of("[]", "true", "null", "{}", "{\"jsonrpc\":\"1.0\",\"id\":1,\"method\":\"echo\"}",
                "{\"jsonrpc\":\"2.0\",\"id\":true,\"method\":\"echo\"}",
                "{\"jsonrpc\":\"2.0\",\"id\":{},\"method\":\"echo\"}",
                "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":5}")) {
            JSONObject response = exchange(invalid, (method, params) -> { fail("Invalid envelope dispatched"); return null; }).get(0);
            assertError(response, -32600, "invalid_request");
            assertTrue(response.isNull("id"));
        }
    }

    @Test
    public void rejectsPermissiveJsonExtensionsAndRecovers() throws Exception {
        for (String invalid : List.of("", "{'jsonrpc':'2.0'}", "{method:1}", "{\"x\":1,}", "[1,]", "[1,,2]",
                "{\"x\":01}", "{\"x\":+1}", "{\"x\":.1}", "{\"x\":1.}", "{\"x\":1e}",
                "{\"x\":NaN}", "{\"x\":Infinity}", "{\"x\":undefined}", "{\"x\":1}{}", "{\"x\":1,\"x\":2}",
                "{\"x\":\"\\q\"}", "{\"x\":\"\\uZZZZ\"}", "{\"x\":\"raw\tcontrol\"}")) {
            List<JSONObject> responses = exchange(invalid + "\n" + REQUEST + "\n", ECHO);
            assertEquals(invalid, 2, responses.size());
            assertError(responses.get(0), -32700, "invalid_json");
            assertEquals(1, responses.get(1).getInt("id"));
        }
    }

    @Test
    public void rejectsUnknownEnvelopeFieldsAndContinues() throws Exception {
        String unknown = REQUEST.substring(0, REQUEST.length() - 1) + ",\"extra\":true}";
        AtomicInteger calls = new AtomicInteger();
        List<JSONObject> responses = exchange(unknown + "\n" + REQUEST, (method, params) -> {
            calls.incrementAndGet();
            return params;
        });
        assertEquals(1, calls.get());
        assertEquals(2, responses.size());
        assertError(responses.get(0), -32600, "invalid_request");
        assertTrue(responses.get(0).isNull("id"));
        assertTrue(responses.get(1).has("result"));
    }

    @Test
    public void invalidParamsAreStructuredAndNotificationsStaySilent() throws Exception {
        String bad = REQUEST.substring(0, REQUEST.length() - 1) + ",\"params\":[]}";
        List<JSONObject> responses = exchange(bad + "\n" + bad.replace("\"id\":1,", "") + "\n" + REQUEST, ECHO);
        assertEquals(2, responses.size());
        assertError(responses.get(0), -32602, "invalid_params");
        assertEquals(1, responses.get(0).getInt("id"));
    }

    @Test
    public void dispatchFailuresDoNotTerminateTheSession() throws Exception {
        AtomicInteger calls = new AtomicInteger();
        List<JSONObject> responses = exchange(REQUEST + "\n" + REQUEST + "\n" + REQUEST, (method, params) -> {
            int call = calls.incrementAndGet();
            if (call == 1) {
                throw new RpcException(-32601, "unknown_method", "Unknown method");
            }
            if (call == 2) {
                throw new IllegalStateException("Expected test failure");
            }
            return params;
        });
        assertEquals(3, responses.size());
        assertError(responses.get(0), -32601, "unknown_method");
        assertError(responses.get(1), -32603, "internal_error");
        assertTrue(responses.get(2).has("result"));
    }

    @Test
    public void malformedUtf8IsRejectedWithoutReplacingBytesAndSessionContinues() throws Exception {
        ByteArrayOutputStream input = new ByteArrayOutputStream();
        input.write(new byte[]{(byte) 0xc3, 0x28, '\n'});
        input.write(REQUEST.getBytes(StandardCharsets.UTF_8));
        List<JSONObject> responses = exchange(input.toByteArray(), ECHO);
        assertEquals(2, responses.size());
        assertError(responses.get(0), -32700, "invalid_utf8");
        assertTrue(responses.get(1).has("result"));
    }

    @Test
    public void oversizedLineIsDrainedBeforeProcessingNextRequest() throws Exception {
        String largest = REQUEST + " ".repeat(Main.MAX_LINE_BYTES - REQUEST.length());
        assertTrue(exchange(largest + "\n", ECHO).get(0).has("result"));
        List<JSONObject> responses = exchange(" ".repeat(Main.MAX_LINE_BYTES + 1) + REQUEST + "\n" + REQUEST, ECHO);
        assertEquals(2, responses.size());
        assertError(responses.get(0), -32600, "request_too_large");
        assertTrue(responses.get(1).has("result"));
        assertError(exchange(" ".repeat(Main.MAX_LINE_BYTES + 1), ECHO).get(0), -32600, "request_too_large");
    }

    @Test
    public void excessiveNestingIsRejectedBeforeRecursiveParsing() throws Exception {
        String accepted = "[".repeat(Main.MAX_JSON_DEPTH) + "0" + "]".repeat(Main.MAX_JSON_DEPTH);
        // Its syntax is accepted, then the unsupported top-level array is rejected.
        assertError(exchange(accepted, ECHO).get(0), -32600, "invalid_request");
        String nested = "[".repeat(Main.MAX_JSON_DEPTH + 1) + "0" + "]".repeat(Main.MAX_JSON_DEPTH + 1);
        List<JSONObject> responses = exchange(nested + "\n" + REQUEST, ECHO);
        assertEquals(2, responses.size());
        assertError(responses.get(0), -32700, "json_depth_exceeded");
        assertTrue(responses.get(1).has("result"));
    }
}
