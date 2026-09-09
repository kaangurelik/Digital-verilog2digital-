module always_if(input [3:0] a, input [3:0] b, input select_b, output reg [3:0] y);
  always @* begin
    if (select_b)
      y = b;
    else
      y = a;
  end
endmodule
