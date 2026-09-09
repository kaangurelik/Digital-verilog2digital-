module smoke_tb;
    reg a, b;
    wire y;
    integer i;
    smoke dut(a, b, y);
    initial begin
        for (i = 0; i < 4; i = i + 1) begin
            {a, b} = i;
            #1;
            if (y !== (a & b)) $fatal(1, "AND smoke mismatch");
        end
        $display("PASS: all four AND inputs");
        $finish;
    end
endmodule
