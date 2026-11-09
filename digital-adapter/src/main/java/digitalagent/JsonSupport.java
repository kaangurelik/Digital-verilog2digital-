package digitalagent;

import org.json.JSONArray;
import org.json.JSONObject;

import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.Set;
import java.util.TreeSet;

/** Strict JSON conversions shared by the adapter methods. */
final class JsonSupport {
    private JsonSupport() { }

    static RpcException invalid(String code, String message) {
        return new RpcException(-32602, code, message);
    }

    static void fields(JSONObject object, String... allowed) throws RpcException {
        Set<String> names = Set.of(allowed);
        for (String key : object.keySet()) {
            if (!names.contains(key)) throw invalid("UNKNOWN_FIELD", "Unknown field: " + key);
        }
    }

    static String string(JSONObject object, String key) throws RpcException {
        Object value = object.opt(key);
        if (!(value instanceof String text) || text.isEmpty() || text.length() > 16384) {
            throw invalid("INVALID_STRING", key + " must be a nonempty string of at most 16384 characters");
        }
        return text;
    }

    static JSONObject object(JSONObject parent, String key) throws RpcException {
        Object value = parent.opt(key);
        if (!(value instanceof JSONObject result)) throw invalid("INVALID_OBJECT", key + " must be an object");
        return result;
    }

    static JSONObject optionalObject(JSONObject parent, String key) throws RpcException {
        return parent.has(key) ? object(parent, key) : new JSONObject();
    }

    static JSONArray array(JSONObject parent, String key, int limit) throws RpcException {
        Object value = parent.opt(key);
        if (!(value instanceof JSONArray result)) throw invalid("INVALID_ARRAY", key + " must be an array");
        if (result.length() > limit) throw invalid("RESOURCE_LIMIT", key + " exceeds limit of " + limit);
        return result;
    }

    static JSONObject arrayObject(JSONArray array, int index) throws RpcException {
        Object value = array.get(index);
        if (!(value instanceof JSONObject result)) throw invalid("INVALID_OBJECT", "Array item " + index + " must be an object");
        return result;
    }

    static long integer(Object value, String name, long min, long max) throws RpcException {
        if (!(value instanceof Number)) throw invalid("INVALID_INTEGER", name + " must be an integer");
        BigDecimal number;
        try {
            number = new BigDecimal(value.toString());
            if (number.scale() > 0) number = number.stripTrailingZeros();
        } catch (NumberFormatException error) {
            throw invalid("INVALID_INTEGER", name + " must be an exact integer");
        }
        if (number.scale() > 0) throw invalid("INVALID_INTEGER", name + " must be an exact integer");
        // Compare before expansion: an exponent such as 1e99999999 must not materialize its digits.
        if (number.compareTo(BigDecimal.valueOf(min)) < 0 || number.compareTo(BigDecimal.valueOf(max)) > 0) {
            throw invalid("INTEGER_RANGE", name + " must be between " + min + " and " + max);
        }
        return number.longValueExact();
    }

    static Path absolutePath(JSONObject params, String key) throws RpcException {
        try {
            Path path = Path.of(string(params, key));
            if (!path.isAbsolute()) throw invalid("ABSOLUTE_PATH_REQUIRED", key + " must be absolute");
            return path.normalize();
        } catch (java.nio.file.InvalidPathException error) {
            throw invalid("INVALID_PATH", key + " is not a valid path");
        }
    }

    static String canonical(Object value) {
        if (value instanceof JSONObject object) {
            StringBuilder result = new StringBuilder("{");
            for (String key : new TreeSet<>(object.keySet())) {
                if (result.length() > 1) result.append(',');
                result.append(JSONObject.quote(key)).append(':').append(canonical(object.get(key)));
            }
            return result.append('}').toString();
        }
        if (value instanceof JSONArray array) {
            StringBuilder result = new StringBuilder("[");
            for (int i = 0; i < array.length(); i++) {
                if (i > 0) result.append(',');
                result.append(canonical(array.get(i)));
            }
            return result.append(']').toString();
        }
        return JSONObject.valueToString(value);
    }

    static String sha256(byte[] bytes) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
        } catch (java.security.NoSuchAlgorithmException impossible) {
            throw new IllegalStateException(impossible);
        }
    }

    static String sha256(String text) {
        return sha256(text.getBytes(StandardCharsets.UTF_8));
    }
}
