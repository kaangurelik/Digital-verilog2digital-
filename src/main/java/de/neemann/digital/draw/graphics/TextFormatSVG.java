/*
 * Copyright (c) 2019 Helmut Neemann.
 * Use of this source code is governed by the GPL v3 license
 * that can be found in the LICENSE file.
 */
package de.neemann.digital.draw.graphics;

import de.neemann.digital.draw.graphics.text.ParseException;
import de.neemann.digital.draw.graphics.text.Parser;
import de.neemann.digital.draw.graphics.text.formatter.SVGFormatter;
import de.neemann.digital.draw.graphics.text.text.Decorate;
import de.neemann.digital.draw.graphics.text.text.Text;

import java.awt.*;

final class TextFormatSVG implements GraphicSVG.TextStyle {
    @Override
    public String format(String text, Style style) {
        String formatted;
        try {
            Text t = new Parser(text).parse();
            if (style.getFontStyle() == Font.ITALIC)
                t = Decorate.math(t);
            formatted = SVGFormatter.format(t);
        } catch (ParseException e) {
            e.printStackTrace();
            formatted = text.replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;").replace("\"", "&quot;");
        }
        return xmlCharacters(formatted);
    }

    /**
     * XML 1.0 cannot represent most control characters, not even as references.
     * Both the formatted and the fallback path replace them to keep the SVG well-formed.
     */
    private static String xmlCharacters(String text) {
        StringBuilder sb = new StringBuilder(text.length());
        text.codePoints().forEach(c -> sb.appendCodePoint(c == '\t' || c == '\n' || c == '\r'
                || c >= 0x20 && c <= 0xD7FF || c >= 0xE000 && c <= 0xFFFD || c >= 0x10000 ? c : 0xFFFD));
        return sb.toString();
    }
}
