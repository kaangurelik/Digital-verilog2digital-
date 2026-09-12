module parameter_specializations(input [3:0] a,b, input [7:0] c,d, output [3:0] y, output [7:0] z);
hier_add #(.WIDTH(4)) narrow_instance(.a(a),.b(b),.y(y));
hier_add #(.WIDTH(8)) wide(.a(c),.b(d),.y(z));
endmodule
module hier_add #(parameter WIDTH=4)(input [WIDTH-1:0] a,b, output [WIDTH-1:0] y);
assign y=a+b;
endmodule
