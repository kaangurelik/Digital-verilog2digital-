module lifetime_stress(input [31:0] a, b, input clk,
                       output reg [31:0] y, output reg q, output [7:0] g);
  function inner;
    input x, z;
    inner = x ^ z;
  endfunction
  function outer;
    input x, z;
    outer = inner(x, z);
  endfunction
  always @* y[0] = outer(a[0], b[0]);
  always @* y[1] = outer(a[1], b[1]);
  always @* y[2] = outer(a[2], b[2]);
  always @* y[3] = outer(a[3], b[3]);
  always @* y[4] = outer(a[4], b[4]);
  always @* y[5] = outer(a[5], b[5]);
  always @* y[6] = outer(a[6], b[6]);
  always @* y[7] = outer(a[7], b[7]);
  always @* y[8] = outer(a[8], b[8]);
  always @* y[9] = outer(a[9], b[9]);
  always @* y[10] = outer(a[10], b[10]);
  always @* y[11] = outer(a[11], b[11]);
  always @* y[12] = outer(a[12], b[12]);
  always @* y[13] = outer(a[13], b[13]);
  always @* y[14] = outer(a[14], b[14]);
  always @* y[15] = outer(a[15], b[15]);
  always @* y[16] = outer(a[16], b[16]);
  always @* y[17] = outer(a[17], b[17]);
  always @* y[18] = outer(a[18], b[18]);
  always @* y[19] = outer(a[19], b[19]);
  always @* y[20] = outer(a[20], b[20]);
  always @* y[21] = outer(a[21], b[21]);
  always @* y[22] = outer(a[22], b[22]);
  always @* y[23] = outer(a[23], b[23]);
  always @* y[24] = outer(a[24], b[24]);
  always @* y[25] = outer(a[25], b[25]);
  always @* y[26] = outer(a[26], b[26]);
  always @* y[27] = outer(a[27], b[27]);
  always @* y[28] = outer(a[28], b[28]);
  always @* y[29] = outer(a[29], b[29]);
  always @* y[30] = outer(a[30], b[30]);
  always @* y[31] = outer(a[31], b[31]);
  always @(posedge clk) q <= a[0];
  genvar i;
  generate
    for (i = 0; i < 8; i = i + 1) begin : generated
      assign g[i] = a[i] & b[i];
    end
  endgenerate
endmodule
