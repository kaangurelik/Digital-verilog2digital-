module generated(input [3:0] a, input enable, output [3:0] y);
  genvar i;
  generate
    for (i = 0; i < 4; i = i + 1) begin: g
      assign y[i] = a[i] & enable;
    end
  endgenerate
endmodule
