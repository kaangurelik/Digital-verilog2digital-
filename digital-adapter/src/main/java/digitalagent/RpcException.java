package digitalagent;

/** An expected RPC failure with a stable, machine-readable diagnostic code. */
public final class RpcException extends Exception {
    public final int rpcCode;
    public final String code;

    public RpcException(int rpcCode, String code, String message) {
        super(message);
        this.rpcCode = rpcCode;
        this.code = code;
    }

    public static RpcException invalidParams(String code, String message) {
        return new RpcException(-32602, code, message);
    }
}
