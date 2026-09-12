module nested_arithmetic(input [3:0] a,b,c, output [3:0] y);
hier_wrapper outer(.a(a),.b(b),.c(c),.y(y));
endmodule
module hier_wrapper(input [3:0] a,b,c, output [3:0] y);
wire [3:0] sum;
hier_add4 inner(.a(a),.b(b),.y(sum));
assign y=sum^c;
endmodule
module hier_add4(input [3:0] a,b, output [3:0] y);
assign y=a+b;
endmodule
