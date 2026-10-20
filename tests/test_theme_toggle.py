import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _root_blocks(css):
    """Yield (selector, body) for each top-level :root{...} token block."""
    for match in re.finditer(r"(:root[^\{]*)\{(.*?)\}", css, re.DOTALL):
        yield match.group(1).strip(), match.group(2)


class ThemeToggleMarkupTest(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_the_topbar_button_is_the_only_theme_control(self):
        """One entry point for the theme: the topbar icon cycles auto → light → dark.

        The sidebar used to ship a second three-button segmented control for the
        same state, which duplicated the topbar toggle; it is gone for good.
        """
        self.assertIn('id="theme-toggle"', self.html)
        for removed in ("theme-mode-auto", "theme-mode-light", "theme-mode-dark", "theme-segment"):
            self.assertNotIn(removed, self.html, f"the removed sidebar control reappeared: {removed}")
            self.assertNotIn(removed, self.javascript)

    def test_the_button_cycles_through_all_three_modes(self):
        self.assertIn('const order=["auto","light","dark"]', self.javascript)
        self.assertIn("setThemeMode(order[(order.indexOf(themeMode)+1)%order.length])", self.javascript)
        # the third icon is what makes the "auto" state visible on the button
        self.assertIn('class="icon-auto"', self.html)

    def test_mode_icons_are_driven_by_the_persisted_mode(self):
        # showing the resolved light/dark icon would hide the fact that auto is still active
        css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn(':root[data-theme-mode="light"] .theme-toggle .icon-sun', css)
        self.assertIn(':root[data-theme-mode="dark"] .theme-toggle .icon-moon', css)
        self.assertIn(".theme-toggle .icon-auto { opacity: 1", css)

    def test_theme_choice_is_persisted_locally_and_applied_to_root(self):
        self.assertIn('localStorage.setItem(THEME_KEY,mode)', self.javascript)
        self.assertIn('const THEME_KEY="chat-theme"', self.javascript)
        self.assertIn('root.setAttribute("data-theme",dark?"dark":"light")', self.javascript)

    def test_auto_mode_follows_the_system_and_listens_for_changes(self):
        self.assertIn('matchMedia("(prefers-color-scheme: dark)")', self.javascript)
        self.assertIn('darkMedia.addEventListener("change",onSystemTheme)', self.javascript)
        # the listener must ignore system changes once the user pinned light or dark
        self.assertIn('if(themeMode==="auto")applyTheme(false)', self.javascript)

    def test_status_bar_color_tracks_the_active_theme(self):
        self.assertIn('id="meta-theme-color"', self.html)
        self.assertIn('metaThemeColor.setAttribute("content",dark?THEME_DARK_COLOR:THEME_LIGHT_COLOR)', self.javascript)

    def test_pre_paint_script_avoids_a_flash_of_the_wrong_theme(self):
        # the theme must be resolved in <head>, before the stylesheet paints
        head = self.html.split("</head>", 1)[0]
        self.assertIn('data-theme', head)
        self.assertIn("prefers-color-scheme", head)
        self.assertLess(head.index("localStorage"), head.index('rel="stylesheet"'))


class ThemeTokenCoverageTest(unittest.TestCase):
    """Dark mode only works if every color is a token with a dark override.

    These guards fail loudly when someone re-adds a hardcoded color or forgets
    to mirror a new token into the dark palette.
    """

    def setUp(self):
        self.css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    def _token_map(self, body):
        return {
            name: value.strip()
            for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body)
        }

    def _light_dark(self):
        light = dark = None
        for selector, body in _root_blocks(self.css):
            if "data-theme" in selector:
                if dark is None:
                    dark = self._token_map(body)
            elif light is None:
                # the first default :root is the palette; later ones are @media overrides
                light = self._token_map(body)
        self.assertIsNotNone(light, "expected a default :root token block")
        self.assertIsNotNone(dark, 'expected a :root[data-theme="dark"] token block')
        return light, dark

    def test_no_hardcoded_colors_outside_the_token_blocks(self):
        stripped = re.sub(r"/\*.*?\*/", "", self.css, flags=re.DOTALL)
        outside = re.sub(r":root[^\{]*\{.*?\}", "", stripped, flags=re.DOTALL)
        offenders = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", outside)
        self.assertEqual(offenders, [], f"colors must come from tokens, found {offenders[:5]}")

    def test_every_color_token_has_a_dark_override(self):
        light, dark = self._light_dark()
        color_tokens = [
            name
            for name, value in light.items()
            if re.match(r"#|rgba?\(|hsla?\(|linear-gradient", value)
        ]
        self.assertTrue(color_tokens, "expected the light palette to define color tokens")
        missing = [name for name in color_tokens if name not in dark]
        self.assertFalse(missing, f"tokens without a dark value leak the light color: {missing}")

    def test_color_scheme_is_declared_for_native_controls(self):
        self.assertRegex(self.css, r":root\s*\{[^}]*color-scheme:\s*light")
        self.assertRegex(self.css, r':root\[data-theme="dark"\]\s*\{[^}]*color-scheme:\s*dark')


class ServiceWorkerShellTest(unittest.TestCase):
    def test_the_cached_shell_version_was_bumped(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        match = re.search(r'CACHE="chat-app-v(\d+)"', source)
        self.assertIsNotNone(match)
        # v15 shipped before the theme work; the shell assets changed, so it must be higher
        self.assertGreaterEqual(int(match.group(1)), 16, "bump CACHE so clients refetch the new shell")


if __name__ == "__main__":
    unittest.main()
