module repeated_logic(input [3:0] a,b,c,d, output [3:0] x,y);
hier_xor first(.a(a),.b(b),.y(x));
hier_xor second(.a(c),.b(d),.y(y));
endmodule
module hier_xor(input [3:0] a,b, output [3:0] y);
assign y=a^b;
endmodule
