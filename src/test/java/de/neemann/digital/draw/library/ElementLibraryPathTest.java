/*
 * Copyright (c) 2026.
 * Use of this source code is governed by the GPL v3 license
 * that can be found in the LICENSE file.
 */
package de.neemann.digital.draw.library;

import de.neemann.digital.integration.Resources;
import junit.framework.TestCase;

import java.io.File;

/**
 * Checks explicit library discovery for application hosts and isolated builds.
 */
public class ElementLibraryPathTest extends TestCase {
    private String previous;

    @Override
    protected void setUp() {
        previous = System.getProperty("digital.library.path");
    }

    @Override
    protected void tearDown() {
        if (previous == null)
            System.clearProperty("digital.library.path");
        else
            System.setProperty("digital.library.path", previous);
    }

    public void testConfiguredLibraryLoadsComponent() throws Exception {
        File library = new File(Resources.getRoot(), "../../main/dig/lib").getCanonicalFile();
        System.setProperty("digital.library.path", library.getPath());

        assertEquals(library, ElementLibrary.getLibPath());
        assertNotNull(new ElementLibrary().getElementType("7400.dig"));
    }

    public void testAbsentAndEmptyPropertyUseAutomaticDiscovery() {
        System.clearProperty("digital.library.path");
        File automatic = ElementLibrary.getLibPath();
        System.setProperty("digital.library.path", "");

        assertEquals(automatic, ElementLibrary.getLibPath());
    }

    public void testMissingConfiguredDirectoryExplainsFailure() {
        File missing = new File(Resources.getRoot(), "missing-library-path-test-directory");
        assertFalse(missing.exists());
        System.setProperty("digital.library.path", missing.getPath());

        try {
            ElementLibrary.getLibPath();
            fail("Expected an invalid library path to fail");
        } catch (IllegalArgumentException e) {
            assertTrue(e.getMessage().contains("digital.library.path"));
            assertTrue(e.getMessage().contains(missing.getName()));
        }
    }
}
