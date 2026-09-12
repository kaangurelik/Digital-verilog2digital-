module sized_parameters(output [7:0] narrow,wide);
hier_mask #(.MASK(4'shf)) narrow_instance(.y(narrow));
hier_mask #(.MASK(8'h0f)) wide_instance(.y(wide));
endmodule
module hier_mask #(parameter MASK=0)(output [7:0] y);
assign y=MASK>>1;
endmodule
