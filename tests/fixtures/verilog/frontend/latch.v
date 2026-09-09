module latch(input enable, input a, output reg y);
  always @* begin
    if (enable)
      y = a;
  end
endmodule
