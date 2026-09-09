module always_case(input [3:0] a, input [3:0] b, input [1:0] mode, output reg [3:0] y);
  always @* begin
    case (mode)
      2'b00: y = a;
      2'b01: y = b;
      default: y = 4'b0011;
    endcase
  end
endmodule
